"""Real-frutlups/mock-agent end-to-end lanes (M003-S02 Phase B).

These lanes exercise the actual installed frutlups CLI candidate through the
governed tool interpreter against the committed two-live-slice fixture
project: released planning transport, all three governed verb transactions,
mock agents, drive verification, crash/resume, and refusal families. They
skip honestly — with the observed host fact — when the governed tool
environment is absent or still holds the uncorrected released baseline.

No provider command, credential read, network, or cost is involved: the
"real" component is the local frutlups artifact tool only, launched through
an explicit absolute binding.
"""

import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

from frutlups_drive.cli import (
    _build_launch_identity,
    _build_supervisor,
    _compile_mock_script,
    main,
)
from frutlups_drive.contracts import ExitCode, StopReason
from frutlups_drive.frutlupscli import BINDING_SCHEMA_VERSION
from frutlups_drive.oracle import reconcile_pass_boundary
from frutlups_drive.policy import load_execution_policy
from frutlups_drive.runstore import RunStore
from frutlups_drive.workspace import WorkspaceManager

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "projects" / "frutlups_e2e"
FRUTLUPS_PYTHON = (
    REPO_ROOT.parent / "venvs" / "frutlups-drive-frutlups-0.1" / "Scripts" / "python.exe"
)

_CAPABILITY: dict[str, object] = {}


def _candidate_capability() -> str:
    """One cached probe: '' when usable, else the recorded skip reason."""

    if "reason" in _CAPABILITY:
        return str(_CAPABILITY["reason"])
    if not FRUTLUPS_PYTHON.is_file():
        _CAPABILITY["reason"] = (
            "governed frutlups tool interpreter is absent on this host"
        )
        return str(_CAPABILITY["reason"])
    probe = subprocess.run(
        [str(FRUTLUPS_PYTHON), "-m", "frutlups", "status", str(FIXTURE), "--json"],
        capture_output=True,
        text=True,
        timeout=120,
        env={},
    )
    reason = ""
    try:
        payload = json.loads(probe.stdout)
        if "M001-S01" not in payload.get("accepted_slice_ids", ()):
            reason = (
                "installed frutlups does not implement the two M003-S02 "
                "compatibility corrections (released 0.1.0 baseline)"
            )
    except ValueError:
        reason = "installed frutlups status probe did not return JSON"
    _CAPABILITY["reason"] = reason
    return reason


class SimulatedCrash(Exception):
    pass


class E2EBase(unittest.TestCase):
    def setUp(self):
        reason = _candidate_capability()
        if reason:
            self.skipTest(reason)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def make_project(self, name="project"):
        project = self.tmp / name
        shutil.copytree(FIXTURE, project)
        binding = project / "local_state" / "frutlups_binding.toml"
        binding.parent.mkdir(parents=True)
        escaped = str(FRUTLUPS_PYTHON).replace(chr(92), chr(92) * 2)
        binding.write_text(
            f'schema_version = "{BINDING_SCHEMA_VERSION}"\n'
            "[launch]\n"
            f'argv_prefix = ["{escaped}", "-m", "frutlups"]\n'
            'tool_identity = "frutlups==0.1.1"\n'
            "[env]\n",
            encoding="utf-8",
        )
        return project

    def edit_script(self, project, mutate):
        path = project / ".frutlups_drive_mock/script.json"
        script = json.loads(path.read_text(encoding="utf-8"))
        mutate(script)
        path.write_text(json.dumps(script, indent=1), encoding="utf-8")

class CleanPassFamilyTests(E2EBase):
    def test_two_slice_clean_pass_reaches_milestone_complete(self):
        project = self.make_project()
        code = main(
            ["run", str(project), "--until", "milestone_complete"]
        )
        self.assertEqual(code, int(ExitCode.OK))
        # Both live slices completed through real frutlups verbs.
        self.assertTrue(
            (project / "prompts/for_coding_agent/003_frutlups_m001_s02_alpha_work.md").is_file()
        )
        self.assertTrue(
            (project / "prompts/for_review_agent/004_review_frutlups_m001_s02_alpha_work.md").is_file()
        )
        self.assertTrue(
            (project / "05_governance/reviews/m001_s02_alpha_work_verdict_record.md").is_file()
        )
        self.assertTrue(
            (project / "05_governance/reviews/m001_s03_beta_work_verdict_record.md").is_file()
        )
        store = RunStore(project / ".frutlups_drive")
        events = store.read_events("run_001")
        verbs = [e["verb"] for e in events if e["kind"] == "verb"]
        self.assertEqual(
            verbs,
            [
                "make-coding-prompt",
                "make-review-prompt",
                "record-verdict",
                "make-coding-prompt",
                "make-review-prompt",
                "record-verdict",
            ],
        )
        verifications = [e for e in events if e["kind"] == "verification"]
        self.assertEqual([v["passed"] for v in verifications], [True, True])
        manifest = (project / ".frutlups_drive/runs/run_001/manifest.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn("frutlups_binding_sha256", manifest)
        self.assertIn("frutlups_contract_id", manifest)
        self.assertIn('frutlups_tool_identity = "frutlups==0.1.1"', manifest)
        self.assertIn('frutlups_package_identity = "frutlups==0.1.1"', manifest)
        self.assertNotIn(
            str(FRUTLUPS_PYTHON).replace(chr(92), chr(92) * 2), manifest
        )

    def test_released_hyphen_verdict_chain_is_oracle_clean(self):
        """Installed released planning and the oracle agree on its accepted seed."""
        project = self.make_project("oracle_contract")
        status = subprocess.run(
            [
                str(FRUTLUPS_PYTHON),
                "-m",
                "frutlups",
                "status",
                str(project),
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env={},
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("M001-S01", json.loads(status.stdout)["accepted_slice_ids"])
        artifacts = WorkspaceManager(
            project, project / ".frutlups_drive"
        ).snapshot(project)
        record = {
            "contract_version": 1,
            "run_id": "run_001",
            "evidence": [],
            "artifacts": [
                {"path": path, "sha256": digest}
                for path, digest in sorted(artifacts.items())
            ],
        }
        bundle = reconcile_pass_boundary(record, project, self.tmp)
        verdict_classes = {
            item["class"]
            for item in bundle["observations"]
            if item["slice_id"] == "M001-S01"
        }
        self.assertEqual(verdict_classes, set())

    def test_declared_package_identity_mismatch_refuses_before_store(self):
        project = self.make_project()
        policy_path = project / "frutlups_drive.toml"
        policy_path.write_text(
            policy_path.read_text(encoding="utf-8").replace(
                "[frutlups]\n",
                '[frutlups]\npackage_identity = "frutlups==0.1.0"\n',
            ),
            encoding="utf-8",
        )
        code = main(["run", str(project), "--until", "milestone_complete"])
        self.assertEqual(code, int(ExitCode.REFUSED))
        self.assertFalse((project / ".frutlups_drive").exists())

    def test_run_store_and_events_carry_no_raw_wrapper_or_machine_paths(self):
        project = self.make_project()
        code = main(["run", str(project), "--until", "milestone_complete"])
        self.assertEqual(code, int(ExitCode.OK))
        store_root = project / ".frutlups_drive"
        needle = str(FRUTLUPS_PYTHON)
        for path in store_root.rglob("*"):
            if not path.is_file():
                continue
            raw = path.read_bytes().decode("utf-8", errors="replace")
            self.assertNotIn(needle, raw, f"machine path leaked into {path.name}")
            # The manifest legitimately pins the DECLARED contract id string;
            # the raw wrapper is distinguishable by its JSON member key form.
            self.assertNotIn('"planning_frontier":', raw,
                             f"raw wrapper leaked into {path.name}")
            self.assertNotIn("next_command", raw,
                             f"next_command leaked into {path.name}")


class RepairFamilyTests(E2EBase):
    def test_needs_work_review_repair_second_review_pass(self):
        project = self.make_project()

        def mutate(script):
            reviewers = script["executors"]["reviewer"]
            first = json.loads(json.dumps(reviewers[0]))
            first["writes"][0]["content_file"] = "agent/review_needs_work.md"
            # Released frutlups 0.1.8 declares round-qualified output
            # paths for ordinary corrective rounds (the `_round_{N:03d}`
            # family, Q010); the corrective seats write where the
            # generated prompts point them.
            second = json.loads(json.dumps(reviewers[0]))
            second["writes"][0]["path"] = (
                "05_governance/reviews/"
                "m001_s02_alpha_work_round_002_review_report.md"
            )
            script["executors"]["reviewer"] = [first, second, reviewers[1]]
            coders = script["executors"]["coder"]
            repair = json.loads(json.dumps(coders[0]))
            repair["writes"][0]["path"] = (
                "05_governance/reviews/"
                "m001_s02_alpha_work_round_002_self_report.md"
            )
            script["executors"]["coder"] = [coders[0], repair, coders[1]]

        self.edit_script(project, mutate)
        code = main(["run", str(project), "--until", "milestone_complete"])
        self.assertEqual(code, int(ExitCode.OK))
        store = RunStore(project / ".frutlups_drive")
        events = store.read_events("run_001")
        verbs = [e["verb"] for e in events if e["kind"] == "verb"]
        # The needs_work round records nothing: frutlups's typed
        # recode_same_slice next-action routes one corrective coding prompt,
        # then the second round reviews and records the pass.
        self.assertEqual(
            verbs,
            [
                "make-coding-prompt",
                "make-review-prompt",
                "make-coding-prompt",
                "make-review-prompt",
                "record-verdict",
                "make-coding-prompt",
                "make-review-prompt",
                "record-verdict",
            ],
        )
        records = sorted(
            p.name
            for p in (project / "05_governance/reviews").glob("*_verdict_record.md")
        )
        self.assertEqual(
            records,
            [
                "m001_s02_alpha_work_round_002_verdict_record.md",
                "m001_s03_beta_work_verdict_record.md",
            ],
            "exactly one record per slice; the corrective acceptance is "
            "round-qualified under 0.1.8; no needs_work record exists",
        )
        coder_dispatches = [
            e for e in events
            if e["kind"] == "dispatch" and e["role"] == "coder"
        ]
        self.assertEqual(len(coder_dispatches), 3,
                         "round one, corrective round, and slice three")


class BlockedFamilyTests(E2EBase):
    def run_with_first_review(self, content_file):
        project = self.make_project()

        def mutate(script):
            script["executors"]["reviewer"][0]["writes"][0][
                "content_file"
            ] = content_file

        self.edit_script(project, mutate)
        code = main(["run", str(project), "--until", "milestone_complete"])
        return project, code

    def test_blocked_review_stops_with_escalation(self):
        project, code = self.run_with_first_review("agent/review_blocked.md")
        self.assertEqual(code, int(ExitCode.STOPPED_WITH_ESCALATION))
        store = RunStore(project / ".frutlups_drive")
        escalations = store.list_escalations("run_001")
        self.assertEqual(len(escalations), 1)
        self.assertIn("blocked", escalations[0].name)

    def test_override_derived_block_stops_generically(self):
        # The released v1 folds override into the one blocked shape; drive
        # uses its generic blocked stop and synthesizes no verdict.
        project, code = self.run_with_first_review("agent/review_override.md")
        self.assertEqual(code, int(ExitCode.STOPPED_WITH_ESCALATION))
        store = RunStore(project / ".frutlups_drive")
        events = store.read_events("run_001")
        stops = [e for e in events if e["kind"] == "stop"]
        self.assertEqual(stops[-1]["reason"], StopReason.BLOCKED_VERDICT.value)


class DryRunAndRefusalTests(E2EBase):
    def test_plan_dry_run_is_read_only_against_real_binding(self):
        project = self.make_project()
        before = sorted(
            p.relative_to(project).as_posix() for p in project.rglob("*")
        )
        code = main(["plan", str(project), "--dry-run"])
        self.assertEqual(code, int(ExitCode.OK))
        after = sorted(
            p.relative_to(project).as_posix() for p in project.rglob("*")
        )
        self.assertEqual(after, before)

    def test_missing_binding_refuses_before_any_store(self):
        project = self.make_project()
        (project / "local_state" / "frutlups_binding.toml").unlink()
        code = main(["run", str(project), "--until", "milestone_complete"])
        self.assertEqual(code, int(ExitCode.REFUSED))
        self.assertFalse((project / ".frutlups_drive").exists())

    def test_mutated_binding_refuses_resume(self):
        project = self.make_project()
        code = main(["run", str(project), "--until", "milestone_complete"])
        self.assertEqual(code, int(ExitCode.OK))
        binding = project / "local_state" / "frutlups_binding.toml"
        binding.write_text(
            binding.read_text(encoding="utf-8").replace(
                "frutlups==0.1.1", "frutlups==0.1.1-tampered"
            ),
            encoding="utf-8",
        )
        code = main(["resume", str(project), "run_001"])
        self.assertEqual(code, int(ExitCode.REFUSED))


class CrashMatrixTests(E2EBase):
    """Crash immediately before/after each verb-transaction stage and at the
    agent store transitions; resumed outcomes equal the uninterrupted run."""

    def build_supervisor(self, project, *, verb_hook=None, transition_hook=None):
        policy = load_execution_policy(project / "frutlups_drive.toml").policy
        compiled = _compile_mock_script(project / ".frutlups_drive_mock")
        from frutlups_drive.frutlupscli import load_launch_binding

        binding = load_launch_binding(
            project / "local_state" / "frutlups_binding.toml"
        )
        launch_identity = _build_launch_identity(
            project,
            policy,
            binding,
            (project / "frutlups_drive.toml").read_bytes(),
        )
        store = RunStore(
            project / ".frutlups_drive", transition_hook=transition_hook
        )
        if not store.run_exists("run_001"):
            store.create_run(
                "run_001",
                {
                    "boundary": "milestone_complete",
                    "contract_version": 1,
                    **launch_identity.manifest_facts(),
                },
            )
            store.append_event(
                "run_001",
                {"kind": "run_created", "t": time.time(),
                 "boundary": "milestone_complete"},
            )
        supervisor = _build_supervisor(
            project, store, "run_001", policy, "milestone_complete",
            compiled, binding=binding, launch_identity=launch_identity,
        )
        if verb_hook is not None:
            supervisor._verb_writer._hook = verb_hook
        return supervisor, store

    def outcome_record(self, project, store):
        manager = WorkspaceManager(project, store.root)
        events = store.read_events("run_001")
        # Attempt names restart per slice, so payment/collection identity is
        # the (slice, attempt) pair — the same key the journal dedupes on.
        collected = [
            (e["slice"], e["attempt"]) for e in events if e["kind"] == "collected"
        ]
        return {
            "project_files": manager.snapshot(project),
            "verbs": [e["verb"] for e in events if e["kind"] == "verb"],
            "verifications": [
                e["passed"] for e in events if e["kind"] == "verification"
            ],
            "collected_unique": len(collected) == len(set(collected)),
        }

    def run_uninterrupted(self):
        project = self.make_project("baseline")
        supervisor, store = self.build_supervisor(project)
        result = supervisor.run_until()
        self.assertEqual(result.kind, "boundary")
        return self.outcome_record(project, store)

    def crash_hook(self, stage_name, occurrence=1):
        seen = {"count": 0}

        def hook(stage, verb):
            if stage == stage_name:
                seen["count"] += 1
                if seen["count"] == occurrence:
                    raise SimulatedCrash(f"{stage}:{verb}")

        return hook

    def transition_crash_hook(self, state_name, occurrence=1):
        seen = {"count": 0}

        def hook(state, attempt_dir):
            if state == state_name:
                seen["count"] += 1
                if seen["count"] == occurrence:
                    raise SimulatedCrash(state)

        return hook

    def assert_crash_resume_equivalent(self, baseline, *, verb_stage=None,
                                       transition=None, label=""):
        project = self.make_project(f"crash_{label}")
        supervisor, store = self.build_supervisor(
            project,
            verb_hook=self.crash_hook(verb_stage) if verb_stage else None,
            transition_hook=(
                self.transition_crash_hook(transition) if transition else None
            ),
        )
        crashed = False
        try:
            supervisor.run_until()
        except SimulatedCrash:
            crashed = True
        self.assertTrue(crashed, f"{label}: crash hook never fired")
        resumed, store2 = self.build_supervisor(project)
        stop = resumed.resume()
        result = stop if stop is not None else resumed.run_until()
        self.assertEqual(result.kind, "boundary", f"{label}: {result.detail}")
        record = self.outcome_record(project, store2)
        for key in ("project_files", "verbs", "verifications"):
            self.assertEqual(
                record[key], baseline[key], f"{label}: mismatch in {key}"
            )
        self.assertTrue(record["collected_unique"], f"{label}: double collection")

    def test_crashes_at_every_verb_stage_resume_equivalently(self):
        baseline = self.run_uninterrupted()
        for stage in ("selected", "dry_run", "validated", "written",
                      "fenced", "reread", "journaled"):
            with self.subTest(stage=stage):
                self.assert_crash_resume_equivalent(
                    baseline, verb_stage=stage, label=stage
                )

    def test_crashes_at_agent_transitions_resume_equivalently(self):
        baseline = self.run_uninterrupted()
        for transition in ("planned", "started", "collected", "validated",
                           "closed"):
            with self.subTest(transition=transition):
                self.assert_crash_resume_equivalent(
                    baseline, transition=transition, label=transition
                )


if __name__ == "__main__":
    unittest.main()
