"""Differential proof: disabled and empty-llloom keep primary transitions."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

from frutlups_drive.dispatch.mock import MockAgentAction
from frutlups_drive.memory_hooks import (
    LlloomBinding,
    LlloomMemoryHooks,
    load_llloom_binding,
)
from frutlups_drive.planstate import MemoryMode
from frutlups_drive.verifier import SubprocessRunner

from _scenario import (
    ACTIVE_ROADMAP,
    ROADMAP_BODY,
    SELF_REPORT,
    Scenario,
    build_project,
    payload,
)
from test_endurance import long_scenario_kwargs
from test_memory_hooks import (
    FAKE_LLLOOM_IDENTITY,
    FAKE_LLLOOM_VERSION,
    LlloomRunner,
    real_llloom_executable,
    tree_identity,
)
from test_phase_b_controls import PHASE_B_POLICY, complete_state, holistic


def _empty_hooks(project, store, run_id, clock):
    # Test-only minimal documented-root substitute. Product code never creates
    # these bytes; the released llloom environment is separately governed.
    return LlloomMemoryHooks(
        project_root=project,
        memory_mode=MemoryMode("llloom", "memory/llloom"),
        binding=LlloomBinding(
            argv_prefix=(__import__("sys").executable,),
            env=(("PYTHONDONTWRITEBYTECODE", "1"),),
            tool_identity=FAKE_LLLOOM_IDENTITY,
            tool_version=FAKE_LLLOOM_VERSION,
            binding_sha256="a" * 64,
            executable_sha256="b" * 64,
        ),
        binding_refusal=None,
        store=store,
        run_id=run_id,
        runner=LlloomRunner(),
        timeout_seconds=5,
    )


def _normalized_attempts(scenario):
    replacements = (
        (str(scenario.store.root), "<STORE>"),
        (scenario.store.root.as_posix(), "<STORE>"),
        (str(scenario.project), "<PROJECT>"),
        (scenario.project.as_posix(), "<PROJECT>"),
    )

    def normalized(value):
        if isinstance(value, str):
            for old, new in replacements:
                value = value.replace(old, new)
            return value
        if isinstance(value, list):
            return [normalized(item) for item in value]
        if isinstance(value, dict):
            return {key: normalized(item) for key, item in value.items()}
        return value

    records = {}
    for slice_id in scenario.store.list_slices(scenario.run_id):
        for attempt in scenario.store.list_attempts(scenario.run_id, slice_id):
            for path in sorted(attempt.rglob("*")):
                if not path.is_file():
                    continue
                raw = path.read_bytes()
                if path.suffix == ".json":
                    raw = json.dumps(
                        normalized(json.loads(raw.decode("utf-8"))),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                elif path.suffix == ".jsonl":
                    raw = b"\n".join(
                        json.dumps(
                            normalized(json.loads(line.decode("utf-8"))),
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                        for line in raw.splitlines()
                    ) + (b"\n" if raw.endswith(b"\n") else b"")
                else:
                    text = raw.decode("utf-8", errors="surrogateescape")
                    for old, new in replacements:
                        text = text.replace(old, new)
                    raw = text.encode("utf-8", errors="surrogateescape")
                records[
                    (
                        slice_id,
                        attempt.name,
                        path.relative_to(attempt).as_posix(),
                    )
                ] = raw
    return records


def _primary_journal(scenario):
    return tuple(
        event for event in scenario.events() if event["kind"] != "memory_hook"
    )


def _representative_cases():
    def repair():
        diagnostics = (
            {
                "severity": "error",
                "code": "repair_needed",
                "message": "bounded repair",
            },
        )
        return {
            "states": [
                payload("ready", "execute_coding_prompt"),
                payload("ready", "fix_self_report", diagnostics=diagnostics),
                payload("ready", "frontier_recorded"),
            ],
            "coder": [
                MockAgentAction(writes=((SELF_REPORT, "# first\n"),)),
                MockAgentAction(writes=((SELF_REPORT, "# repaired\n"),)),
            ],
        }

    def reconciliation():
        proposal = ROADMAP_BODY.replace(
            "Implement the fixture behavior.",
            "Sharpen the fixture behavior.",
        )
        return {
            "states": [
                payload(
                    "needs_specification",
                    None,
                    actor="architect",
                    frontier_present=False,
                ),
                payload("ready", "frontier_recorded"),
                payload("ready", "frontier_recorded"),
            ],
            "architect": [
                MockAgentAction(writes=(("roadmap_proposal.md", proposal),))
            ],
        }

    def second_pass():
        return {
            "states": [
                complete_state(),
                complete_state(),
                payload("ready", "execute_coding_prompt", round_=2),
                payload("ready", "frontier_recorded", round_=2),
                complete_state(),
                complete_state(),
            ],
            "coder": [
                MockAgentAction(writes=((SELF_REPORT, "# second pass\n"),))
            ],
            "reviewer": [holistic(["M001-S01"]), holistic([]), holistic([])],
            "boundary": "roadmap_complete",
            "policy_body": PHASE_B_POLICY,
        }

    return (
        ("repair", repair),
        ("reconciliation", reconciliation),
        ("second_pass", second_pass),
        (
            "endurance",
            lambda: long_scenario_kwargs(
                slice_count=4, induced=True, shadow=False
            ),
        ),
    )


class MemoryModeEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def run_pair(
        self,
        case,
        kwargs_factory,
        *,
        hooks_factory=_empty_hooks,
        prepare_memory=None,
        allow_llloom_workspace_delta=False,
    ):
        outcomes = []
        for name, factory in (("none", None), ("llloom", hooks_factory)):
            case_root = self.root / f"{case}-{name}"
            project = build_project(case_root)
            memory = project / "memory" / "llloom"
            if prepare_memory is None:
                memory.mkdir(parents=True)
                (memory / "fixture.txt").write_bytes(
                    b"test-only minimal empty llloom root\n"
                )
            else:
                prepare_memory(project, memory)
            memory_before = tree_identity(memory)
            scenario = Scenario(
                case_root,
                project=project,
                memory_hooks_factory=factory,
                **kwargs_factory(),
            )
            scenario.supervisor.memory_preflight()
            result = scenario.supervisor.run_until()
            memory_after = tree_identity(memory)
            if name != "llloom" or not allow_llloom_workspace_delta:
                self.assertEqual(memory_after, memory_before, case)
            else:
                before_rows = {row[1]: row for row in memory_before}
                after_rows = {row[1]: row for row in memory_after}
                self.assertEqual(
                    {path: after_rows[path] for path in before_rows},
                    before_rows,
                    case,
                )
                added = set(after_rows) - set(before_rows)
                self.assertTrue(added, case)
                self.assertTrue(
                    all(
                        path.startswith("state/update_proposals/up.")
                        and path.endswith(".json")
                        and after_rows[path][0] == "file"
                        for path in added
                    ),
                    case,
                )
            outcomes.append(
                {
                    "result": (result.kind, result.detail, result.stop_reason),
                    "journal": _primary_journal(scenario),
                    "attempts": _normalized_attempts(scenario),
                    "counters": json.loads(
                        json.dumps(scenario.counters().__dict__, sort_keys=True)
                    ),
                    "scenario": scenario,
                    "memory_before": memory_before,
                    "memory_after": memory_after,
                }
            )
        disabled, empty = outcomes
        self.assertEqual(empty["result"], disabled["result"], case)
        self.assertEqual(empty["journal"], disabled["journal"], case)
        if empty["attempts"] != disabled["attempts"]:
            keys = sorted(set(empty["attempts"]) | set(disabled["attempts"]))
            differing = next(
                key
                for key in keys
                if empty["attempts"].get(key) != disabled["attempts"].get(key)
            )
            self.fail(
                f"{case} attempt mismatch at {differing}: "
                f"{empty['attempts'].get(differing)!r} != "
                f"{disabled['attempts'].get(differing)!r}"
            )
        self.assertEqual(empty["counters"], disabled["counters"], case)
        self.assertGreater(
            sum(e["kind"] == "memory_hook" for e in empty["scenario"].events()),
            0,
        )
        self.assertEqual(
            sum(
                e["kind"] == "memory_hook"
                for e in disabled["scenario"].events()
            ),
            0,
        )
        return outcomes

    def test_repair_reconciliation_second_pass_and_endurance_equivalence(self):
        for case, factory in _representative_cases():
            with self.subTest(case=case):
                self.run_pair(case, factory)

    def test_completion_boundary_queues_roadmap_complete_submission(self):
        # R7-F1: the two_clean completion boundary must invoke the
        # boundary submission hook under the stable boundary identifier;
        # the frontier site alone leaves single-slice completions
        # unsubmitted.
        factory = dict(_representative_cases())["second_pass"]
        outcomes = self.run_pair("completion-boundary", factory)
        self.assertEqual(
            outcomes[1]["result"], ("boundary", "complete", None)
        )
        hook_events = tuple(
            event
            for event in outcomes[1]["scenario"].events()
            if event["kind"] == "memory_hook"
        )
        self.assertIn(
            "roadmap_complete.json",
            {event.get("queue_evidence") for event in hook_events},
        )

    def test_real_llloom_differential_uses_binding_declared_identity(self):
        executable = real_llloom_executable(self)
        repository = Path(__file__).resolve().parents[2]
        binding = load_llloom_binding(
            repository / "local_state" / "llloom_binding.toml"
        )
        self.assertEqual(
            Path(binding.argv_prefix[0]).resolve(), executable.resolve()
        )
        self.assertEqual(binding.tool_identity, "llloom-0.1.2")
        version = subprocess.run(
            (str(executable), "--version"),
            cwd=repository,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        self.assertEqual(version.returncode, 0)
        self.assertEqual(
            version.stdout.decode("utf-8").strip(),
            "llloom " + binding.tool_version,
        )

        def prepare_memory(project, memory):
            initialized = subprocess.run(
                (str(executable), "--root", str(memory), "init"),
                cwd=project,
                env={"PYTHONDONTWRITEBYTECODE": "1"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            self.assertEqual(
                initialized.returncode,
                0,
                initialized.stderr.decode("utf-8", errors="replace"),
            )

        def real_hooks(project, store, run_id, clock):
            return LlloomMemoryHooks(
                project_root=project,
                memory_mode=MemoryMode("llloom", "memory/llloom"),
                binding=binding,
                binding_refusal=None,
                store=store,
                run_id=run_id,
                runner=SubprocessRunner(clock),
                timeout_seconds=10,
            )

        for case, factory in _representative_cases():
            with self.subTest(case=case):
                outcomes = self.run_pair(
                    "real-" + case,
                    factory,
                    hooks_factory=real_hooks,
                    prepare_memory=prepare_memory,
                    allow_llloom_workspace_delta=True,
                )
                real_events = tuple(
                    event
                    for event in outcomes[1]["scenario"].events()
                    if event["kind"] == "memory_hook"
                )
                self.assertGreater(len(real_events), 0)
                self.assertNotIn(
                    "liveness_output_malformed",
                    {event["reason"] for event in real_events},
                )
                self.assertTrue(
                    all(
                        event["reason"] == "healthy"
                        for event in real_events
                        if event["hook"] == "liveness"
                    )
                )
                self.assertIn(
                    "context_empty",
                    {event["reason"] for event in real_events},
                )
                self.assertIn(
                    "update_submitted",
                    {event["reason"] for event in real_events},
                )
                submitted = tuple(
                    event
                    for event in real_events
                    if event["reason"] == "update_submitted"
                )
                self.assertTrue(
                    all(
                        event["hook"] == "boundary_update_submission"
                        and event["proposal_id"].startswith("up.")
                        and event["proposal_document"].endswith(
                            ".submit-update.json"
                        )
                        and event["queue_evidence"].endswith(".json")
                        for event in submitted
                    )
                )
                if case == "second_pass":
                    # R7-F1: the two_clean completion boundary must submit
                    # under the stable boundary identifier into the real
                    # released inbox, not only the frontier site.
                    self.assertIn(
                        "roadmap_complete.json",
                        {event["queue_evidence"] for event in submitted},
                    )
                before_rows = {
                    row[1]: row for row in outcomes[1]["memory_before"]
                }
                after_rows = {
                    row[1]: row for row in outcomes[1]["memory_after"]
                }
                added = set(after_rows) - set(before_rows)
                proposal_ids = {event["proposal_id"] for event in submitted}
                self.assertEqual(
                    added,
                    {
                        f"state/update_proposals/{proposal_id}.json"
                        for proposal_id in proposal_ids
                    },
                )
                primary_text = json.dumps(
                    outcomes[1]["journal"], sort_keys=True
                )
                self.assertTrue(
                    all(proposal_id not in primary_text for proposal_id in proposal_ids)
                )


class MemoryHookCrashResumeTests(unittest.TestCase):
    class Crash(BaseException):
        pass

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_crash_after_memory_fact_replays_write_once_boundary_queue(self):
        project = build_project(self.root)
        memory = project / "memory" / "llloom"
        memory.mkdir(parents=True)
        (memory / "fixture.txt").write_bytes(b"test-only empty root\n")
        crashed = {"done": False}

        def event_hook(kind, _run_id):
            if kind == "memory_hook" and not crashed["done"]:
                crashed["done"] = True
                raise self.Crash()

        first = Scenario(
            self.root,
            project=project,
            states=[payload("ready", "frontier_recorded")],
            memory_hooks_factory=_empty_hooks,
            event_hook=event_hook,
        )
        with self.assertRaises(self.Crash):
            first.supervisor.tick()
        queued = first.store.read_memory_update_queue(
            "run_001", "M001-S01"
        )
        self.assertEqual(queued["proposals"], [])
        self.assertNotIn("slice_complete", first.event_kinds())

        resumed = Scenario(
            self.root,
            project=project,
            run_id="run_001",
            states=[payload("ready", "frontier_recorded")],
            memory_hooks_factory=_empty_hooks,
        )
        self.assertIsNone(resumed.supervisor.resume())
        result = resumed.supervisor.tick()
        self.assertEqual((result.kind, result.detail), ("boundary", "slice_complete"))
        self.assertEqual(
            resumed.store.read_memory_update_queue("run_001", "M001-S01"),
            queued,
        )
        self.assertEqual(resumed.event_kinds().count("slice_complete"), 1)


if __name__ == "__main__":
    unittest.main()
