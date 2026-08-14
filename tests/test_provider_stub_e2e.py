"""Offline two-slice CLI loop with stub Codex/Kimi/Claude executables."""

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401

from frutlups_drive.cli import (
    _build_supervisor,
    _compile_mock_script,
    _required_run_authority,
    main,
)
from frutlups_drive.contracts import ExitCode
from frutlups_drive.dispatch.provider_cli import (
    PROVIDER_BINDING_RELATIVE_PATH,
    PROVIDER_BINDING_SCHEMA_VERSION,
    ProviderCliExecutor,
)
from frutlups_drive.runstore import RunStore

from _scenario import (
    CODING_PROMPT,
    REVIEW_PROMPT,
    REVIEW_PROMPT_2,
    REVIEW_REPORT,
    REVIEW_REPORT_2,
    SELF_REPORT,
    VERDICT_RECORD,
    build_project,
    payload,
)
from test_livegate import gate_markdown


SLICE_2 = "M001-S02"
CODING_PROMPT_2 = "prompts/for_coding_agent/004_m001_s02.md"
SELF_REPORT_2 = "05_governance/reviews/m001/m001_s02_self_report.md"
REVIEW_PROMPT_3 = "prompts/for_review_agent/005_m001_s02_review.md"
REVIEW_REPORT_3 = "05_governance/reviews/m001/m001_s02_review_report.md"
VERDICT_RECORD_2 = "05_governance/reviews/m001/m001_s02_verdict_record.md"
COMPLETION = "05_governance/reviews/m001/roadmap_closure.md"


EXTERNAL_POLICY = '''schema_version = "frutlups_drive_policy_v1"

[target]
stop_at = "roadmap_complete"
max_slices = 5
max_passes = 3

[roles.architect]
adapter = "claude_cli"
model = "claude-opus-5"
workspace_access = "workspace_write"

[roles.coder]
adapter = "codex_cli"
model = "gpt-5.6-sol"

[roles.reviewer]
adapter = "kimi_cli"
model = "kimi-code/k3"

[autonomy]
auto_continue_past_frontier_recorded = true
pass_boundary = "two_clean"

[limits]
max_coder_attempts_per_slice = 3
max_report_repairs = 2
max_total_cost_usd = 10.0
max_wall_clock_minutes = 240
max_consecutive_provider_failures = 3
'''


class SimulatedCrash(Exception):
    pass


class ProviderStubE2ETests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.project = build_project(self.root)
        (self.project / "frutlups_drive.toml").write_text(
            EXTERNAL_POLICY, encoding="utf-8"
        )
        self.gate = self.root / "live_validation_gate.md"
        self.gate.write_text(gate_markdown(), encoding="utf-8")
        self._write_fixture_sources()
        self._write_prompts()
        self._write_stub_and_binding()
        self._write_mock_convention()

    def _write_fixture_sources(self):
        files = {
            "CLAUDE.md": (
                "# Fixture Instructions\n\nWork only inside this fixture. "
                "Use stdlib unittest and write the required self-report.\n"
            ),
            "00_brief/CONTEXT.md": (
                "# Fixture Brief\n\nTwo small pure-stdlib behavior slices.\n"
            ),
            "src/fruitmath.py": (
                '"""Small pure-stdlib fixture functions."""\n\n'
                "def clamp(value, lower, upper):\n"
                "    raise NotImplementedError\n\n"
                "def arithmetic_mean(values):\n"
                "    raise NotImplementedError\n"
            ),
            "tests/test_fruitmath.py": (
                "# The live fixture supplies concrete unittest expectations.\n"
            ),
        }
        for relative, content in files.items():
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def _write_prompts(self):
        prompts = {
            CODING_PROMPT: (
                "# Coding round one\n\nRequired Reading:\n"
                "- `CLAUDE.md`\n- `PROJECT_STATE.md`\n"
                "- `00_brief/CONTEXT.md`\n"
                "- `03_experiments/roadmap.md`\n"
                "- `src/fruitmath.py`\n- `tests/test_fruitmath.py`\n\n"
                "Implement `clamp(value, lower, upper)` and its stdlib "
                "unittest coverage. Write the self-report at "
                f"`{SELF_REPORT}`.\n"
            ),
            CODING_PROMPT_2: (
                "# Coding slice two\n\nRequired Reading:\n"
                "- `CLAUDE.md`\n- `PROJECT_STATE.md`\n"
                "- `00_brief/CONTEXT.md`\n"
                "- `03_experiments/roadmap.md`\n"
                "- `src/fruitmath.py`\n- `tests/test_fruitmath.py`\n\n"
                "Implement `arithmetic_mean(values)` with an empty-input "
                "`ValueError` and its stdlib unittest coverage. Write the "
                f"self-report at `{SELF_REPORT_2}`.\n"
            ),
        }
        for relative, content in prompts.items():
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def _write_stub_and_binding(self):
        local = self.project / "local_state"
        local.mkdir()
        stub = local / "provider_stub.py"
        stub.write_text(
            """import re
import sys
from pathlib import Path

args = sys.argv[1:]
if "exec" in args:
    role = "coder"
    prompt = sys.stdin.read()
    suffix = "_self_report.md"
elif "--model" in args and args[args.index("--model") + 1] == "claude-opus-5":
    role = "architect"
    prompt = sys.stdin.read()
    suffix = ""
else:
    role = "reviewer"
    prompt = args[args.index("--prompt") + 1]
    suffix = "_review_report.md"
if role == "architect":
    marker = "## Current Active Roadmap (verbatim)\\n\\n"
    if marker not in prompt:
        raise SystemExit(4)
    proposal = prompt.split(marker, 1)[1].replace(
        "Implement the fixture behavior.",
        "Sharpen the fixture behavior.",
        1,
    )
    Path("roadmap_proposal.md").write_bytes(proposal.encode("utf-8"))
    print('{"usage":"stub-subscription","tokens":7}')
    raise SystemExit(0)
if role == "reviewer" and "holistic_review.json" in prompt:
    Path("holistic_review.json").write_bytes(b'{"findings":[]}')
    print('{"usage":"stub-subscription","tokens":7}')
    raise SystemExit(0)
matches = [item for item in re.findall(r"`([^`]+\\.md)`", prompt)
           if item.endswith(suffix)]
if not matches:
    raise SystemExit(4)
target = Path(matches[-1])
target.parent.mkdir(parents=True, exist_ok=True)
if role == "coder":
    content = "# Coder Self-Report\\n\\nIntent:\\nStub fixture work.\\n"
else:
    counter = Path("local_state/reviewer_count.txt")
    count = int(counter.read_text() or "0") if counter.exists() else 0
    counter.write_text(str(count + 1))
    verdict = "needs_work" if count == 0 else "pass"
    content = f"# Review Report\\n\\nVerdict: {verdict} - stub fixture\\n"
target.write_text(content, encoding="utf-8")
print('{"usage":"stub-subscription","tokens":7}')
print("stub provider progress", file=sys.stderr)
""",
            encoding="utf-8",
        )
        config = local / "kimi_config.toml"
        config.write_text(
            '[thinking]\neffort = "high"\n'
            '[models."kimi-code/k3"]\n'
            'support_efforts = ["low", "high", "max"]\n'
            'default_effort = "high"\n',
            encoding="utf-8",
        )

        def value(path):
            return json.dumps(str(path).replace("\\", "/"))

        binding = self.project / PROVIDER_BINDING_RELATIVE_PATH
        binding.write_text(
            f'schema_version = "{PROVIDER_BINDING_SCHEMA_VERSION}"\n'
            f'kimi_config_path = {value(config)}\n'
            "[codex_cli]\n"
            f'argv_prefix = [{value(Path(sys.executable))}, {value(stub)}, "codex"]\n'
            "[kimi_cli]\n"
            f'argv_prefix = [{value(Path(sys.executable))}, {value(stub)}, "kimi"]\n'
            "[claude_cli]\n"
            f'argv_prefix = [{value(Path(sys.executable))}, {value(stub)}, "claude"]\n',
            encoding="utf-8",
        )

    def _states(self):
        return [
            payload(
                "needs_specification",
                None,
                actor="architect",
                frontier_present=False,
                diagnostics=({
                    "severity": "error",
                    "code": "underspecified",
                    "message": "the frontier needs specification",
                },),
            ),
            # The guarded writer performs one fresh planning read after the
            # proposal; the next tick recomputes the same released frontier.
            payload("ready", "execute_coding_prompt"),
            payload("ready", "execute_coding_prompt"),
            payload("ready", "make_review_prompt", review_prompt=REVIEW_PROMPT),
            payload("ready", "execute_review_prompt", review_prompt=REVIEW_PROMPT,
                    review_report=REVIEW_REPORT),
            # The seeded needs_work report routes a fresh coder round; it is
            # model output evidence, never parsed by the drive for control.
            payload("ready", "execute_coding_prompt", round_=2),
            payload("ready", "make_review_prompt", round_=2,
                    review_prompt=REVIEW_PROMPT_2),
            payload("ready", "execute_review_prompt", round_=2,
                    review_prompt=REVIEW_PROMPT_2, review_report=REVIEW_REPORT_2),
            payload("ready", "record_verdict", round_=2,
                    review_prompt=REVIEW_PROMPT_2, review_report=REVIEW_REPORT_2,
                    verdict_record=VERDICT_RECORD,
                    verdict={"value": "pass", "next_move": "advance",
                             "report": REVIEW_REPORT_2}),
            payload("ready", "frontier_recorded", round_=2,
                    verdict_record=VERDICT_RECORD,
                    verdict={"value": "pass", "next_move": "advance",
                             "report": REVIEW_REPORT_2}),
            payload("ready", "execute_coding_prompt", slice_id=SLICE_2,
                    coding_prompt=CODING_PROMPT_2, self_report=SELF_REPORT_2),
            payload("ready", "make_review_prompt", slice_id=SLICE_2,
                    coding_prompt=CODING_PROMPT_2, self_report=SELF_REPORT_2,
                    review_prompt=REVIEW_PROMPT_3),
            payload("ready", "execute_review_prompt", slice_id=SLICE_2,
                    coding_prompt=CODING_PROMPT_2, self_report=SELF_REPORT_2,
                    review_prompt=REVIEW_PROMPT_3, review_report=REVIEW_REPORT_3),
            payload("ready", "record_verdict", slice_id=SLICE_2,
                    coding_prompt=CODING_PROMPT_2, self_report=SELF_REPORT_2,
                    review_prompt=REVIEW_PROMPT_3, review_report=REVIEW_REPORT_3,
                    verdict_record=VERDICT_RECORD_2,
                    verdict={"value": "pass", "next_move": "advance",
                             "report": REVIEW_REPORT_3}),
            payload("ready", "frontier_recorded", slice_id=SLICE_2,
                    coding_prompt=CODING_PROMPT_2, self_report=SELF_REPORT_2,
                    review_prompt=REVIEW_PROMPT_3, review_report=REVIEW_REPORT_3,
                    verdict_record=VERDICT_RECORD_2,
                    verdict={"value": "pass", "next_move": "advance",
                             "report": REVIEW_REPORT_3}),
            payload("complete", None, frontier_present=False,
                    completion_evidence={"path": COMPLETION}, actor="none"),
            payload("complete", None, frontier_present=False,
                    completion_evidence={"path": COMPLETION}, actor="none"),
            payload("complete", None, frontier_present=False,
                    completion_evidence={"path": COMPLETION}, actor="none"),
        ]

    def _write_mock_convention(self):
        root = self.project / ".frutlups_drive_mock"
        root.mkdir()
        states = []
        for index, body in enumerate(self._states(), start=1):
            name = f"state_{index:02d}.json"
            (root / name).write_bytes(body)
            states.append(name)
        contents = {
            "review_1.md": (
                "# Review round one\n\nWrite the review report at "
                f"`{REVIEW_REPORT}`.\n"
            ),
            "review_2.md": (
                "# Review round two\n\nWrite the review report at "
                f"`{REVIEW_REPORT_2}`.\n"
            ),
            "review_3.md": (
                "# Review slice two\n\nWrite the review report at "
                f"`{REVIEW_REPORT_3}`.\n"
            ),
            "verdict_1.md": "# Verdict Record\n\npass\n",
            "verdict_2.md": "# Verdict Record\n\npass\n",
        }
        for name, content in contents.items():
            (root / name).write_text(content, encoding="utf-8")
        script = {
            "planstate": states,
            "executors": {"architect": [], "coder": [], "reviewer": []},
            "verbs": {
                "make-review-prompt": [
                    {"path": REVIEW_PROMPT, "content_file": "review_1.md"},
                    {"path": REVIEW_PROMPT_2, "content_file": "review_2.md"},
                    {"path": REVIEW_PROMPT_3, "content_file": "review_3.md"},
                ],
                "record-verdict": [
                    {"path": VERDICT_RECORD, "content_file": "verdict_1.md"},
                    {"path": VERDICT_RECORD_2, "content_file": "verdict_2.md"},
                ],
            },
            "verification": {
                "commands": [
                    {"argv": ["{python}", "-c", "print('stub-e2e-verified')"],
                     "cwd": ".", "timeout_seconds": 30}
                ],
                "declared_regenerated": [],
            },
        }
        (root / "script.json").write_text(
            json.dumps(script, indent=2), encoding="utf-8"
        )

    def run_main(self, *argv):
        with patch("frutlups_drive.cli.LIVE_GATE_PATH", self.gate):
            return main(list(argv))

    def test_ready_gate_drives_architect_two_slices_and_seeded_repair_with_stub_clis(self):
        code = self.run_main(
            "run", str(self.project), "--until", "roadmap_complete"
        )
        self.assertEqual(code, int(ExitCode.OK))
        store = RunStore(self.project / ".frutlups_drive")
        events = store.read_events("run_001")
        dispatches = [event for event in events if event["kind"] == "dispatch"]
        self.assertEqual(
            [(event["role"], event["adapter"]) for event in dispatches],
            [
                ("architect", "claude_cli"),
                ("coder", "codex_cli"),
                ("reviewer", "kimi_cli"),
                ("coder", "codex_cli"),
                ("reviewer", "kimi_cli"),
                ("coder", "codex_cli"),
                ("reviewer", "kimi_cli"),
                ("reviewer", "kimi_cli"),
                ("reviewer", "kimi_cli"),
            ],
        )
        collected = [event for event in events if event["kind"] == "collected"]
        self.assertEqual([event["cost_usd"] for event in collected], [0.0] * 9)
        reconciliation = [
            event for event in events if event["kind"] == "reconciliation"
        ]
        self.assertEqual(len(reconciliation), 1)
        self.assertTrue(reconciliation[0]["progress"])
        self.assertIn("needs_work", (self.project / REVIEW_REPORT).read_text())
        self.assertIn("pass", (self.project / REVIEW_REPORT_2).read_text())
        self.assertTrue((self.project / VERDICT_RECORD_2).is_file())
        captures = list(
            (store.run_dir("run_001") / "adapter_logs").rglob("*_stdout.txt")
        )
        prompts = list(
            (store.run_dir("run_001") / "adapter_logs").rglob("prompt.md")
        )
        self.assertEqual(len(captures), 9)
        self.assertEqual(len(prompts), 9)
        for capture in captures:
            self.assertIn(b'"usage":"stub-subscription"', capture.read_bytes())

    def test_relative_root_staged_run_and_resume_dispatch_absolute_workspaces(self):
        original_cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, original_cwd)
        observed: list[str] = []
        real_execute = ProviderCliExecutor.execute

        def recording_execute(executor, request):
            observed.append(str(request.workspace))
            if not request.workspace.is_absolute():
                raise AssertionError("dispatch request workspace is relative")
            return real_execute(executor, request)

        relative = self.project.relative_to(self.root)
        with patch.object(ProviderCliExecutor, "execute", recording_execute):
            code = self.run_main(
                "run", str(relative), "--until", "slice_complete"
            )
            self.assertEqual(code, int(ExitCode.OK))
            run_count = len(observed)
            self.assertGreater(run_count, 0)
            self.assertTrue(all(Path(workspace).is_absolute()
                                for workspace in observed))

            code = self.run_main(
                "resume", str(relative), "run_001", "--until", "roadmap_complete"
            )
            self.assertEqual(code, int(ExitCode.OK))

        resumed = observed[run_count:]
        self.assertTrue(resumed, "resume must exercise the staged dispatch path")
        self.assertTrue(all(Path(workspace).is_absolute()
                            for workspace in resumed))

    def test_coding_prompt_required_reading_exists_and_task_is_behavioral(self):
        for relative, behavior in (
            (CODING_PROMPT, "clamp(value, lower, upper)"),
            (CODING_PROMPT_2, "arithmetic_mean(values)"),
        ):
            prompt = (self.project / relative).read_text(encoding="utf-8")
            self.assertIn(behavior, prompt)
            reading = [
                line.removeprefix("- `").removesuffix("`")
                for line in prompt.splitlines()
                if line.startswith("- `") and line.endswith("`")
            ]
            self.assertTrue(reading)
            for required in reading:
                self.assertTrue(
                    (self.project / required).is_file(),
                    f"missing required reading: {required}",
                )

    def test_captured_spawn_failure_journals_one_subscription_cost_fact(self):
        authority_patch = patch("frutlups_drive.cli.LIVE_GATE_PATH", self.gate)
        authority_patch.start()
        self.addCleanup(authority_patch.stop)
        authority = _required_run_authority(self.project)
        compiled = _compile_mock_script(self.project / ".frutlups_drive_mock")
        store = RunStore(self.project / ".frutlups_drive")
        store.create_run(
            "run_001",
            {
                "boundary": "roadmap_complete",
                "contract_version": 1,
                **authority.manifest_facts(),
            },
        )
        store.append_event(
            "run_001",
            {
                "kind": "run_created",
                "t": time.time(),
                "boundary": "roadmap_complete",
            },
        )
        supervisor = _build_supervisor(
            self.project,
            store,
            "run_001",
            authority.policy,
            "roadmap_complete",
            compiled,
            authority=authority,
        )
        with patch(
            "frutlups_drive.verifier.SubprocessRunner.run",
            side_effect=OSError("simulated spawn failure"),
        ):
            result = supervisor.tick()
        self.assertEqual(result.detail, "attempt_failed")
        cost_facts = [
            event
            for event in store.read_events("run_001")
            if event["kind"] == "collected"
        ]
        self.assertEqual(len(cost_facts), 1)
        self.assertEqual(cost_facts[0]["status"], "failed")
        self.assertEqual(cost_facts[0]["cost_usd"], 0.0)
        attempt = store.list_attempts("run_001", "M001-S01")[0]
        self.assertEqual(store.read_result(attempt)["cost_usd"], 0.0)

    def test_collected_transition_crash_resumes_without_redispatch(self):
        authority_patch = patch("frutlups_drive.cli.LIVE_GATE_PATH", self.gate)
        authority_patch.start()
        self.addCleanup(authority_patch.stop)
        authority = _required_run_authority(self.project)
        compiled = _compile_mock_script(self.project / ".frutlups_drive_mock")
        crashed = {"done": False}

        def hook(state, attempt):
            if state == "collected" and not crashed["done"]:
                crashed["done"] = True
                raise SimulatedCrash("collected")

        store = RunStore(self.project / ".frutlups_drive", transition_hook=hook)
        store.create_run(
            "run_001",
            {"boundary": "roadmap_complete", "contract_version": 1,
             **authority.manifest_facts()},
        )
        store.append_event(
            "run_001",
            {"kind": "run_created", "t": time.time(),
             "boundary": "roadmap_complete"},
        )
        supervisor = _build_supervisor(
            self.project, store, "run_001", authority.policy,
            "roadmap_complete", compiled, authority=authority,
        )
        with self.assertRaises(SimulatedCrash):
            supervisor.run_until()
        resumed_store = RunStore(self.project / ".frutlups_drive")
        resumed = _build_supervisor(
            self.project, resumed_store, "run_001", authority.policy,
            "roadmap_complete", compiled, authority=authority,
        )
        self.assertIsNone(resumed.resume())
        result = resumed.run_until()
        self.assertEqual(result.kind, "boundary")
        events = resumed_store.read_events("run_001")
        first_slice_coder = [
            event for event in events
            if event["kind"] == "dispatch" and event["role"] == "coder"
            and event["slice"] == "M001-S01"
        ]
        self.assertEqual(len(first_slice_coder), 2)
        collected = [
            (event["slice"], event["attempt"])
            for event in events if event["kind"] == "collected"
        ]
        self.assertEqual(len(collected), len(set(collected)))


if __name__ == "__main__":
    unittest.main()
