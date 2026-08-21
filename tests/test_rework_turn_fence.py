"""M007-S02 rework-turn snapshot, fence, and dispatch-envelope regressions."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

from frutlups_drive import oracle
from frutlups_drive.contracts import AgentRunRequest, AgentRunResult, StopReason
from frutlups_drive.dispatch.mock import MockAgentAction
from frutlups_drive.runstore import RunStore, RunStoreRefusal
from frutlups_drive.supervisor import _SEAT_CONDUCT_BLOCK

from _scenario import (
    CODING_PROMPT,
    PROMPT_BODY,
    REVIEW_PROMPT,
    REVIEW_REPORT,
    SELF_REPORT,
    VERDICT_RECORD,
    Scenario,
    build_project,
    payload,
)


REWORK_PROMPT = "prompts/for_coding_agent/002_m001_s01_rework.md"
REWORK_REPORT = (
    "05_governance/reviews/m001/"
    "m001_s01_rework_001_holistic_pass_001_002_self_report.md"
)
PRODUCT_PATH = "src/alpha.py"
UNRELATED_PATH = "docs/notes.md"
REWORK_PROMPT_BYTES = b"# Rework M001-S01\n\nCorrect the accepted slice.\n"
ACCEPTED_REPORT_BYTES = b"# Accepted self-report\n\nFrozen round-one evidence.\n"
REWORK_ENVELOPE_HEADING = b"## Rework Turn Write Authority"


class _SnapshotCrash(Exception):
    pass


class _DirectExecutor:
    """External-seat shape: effects are observed after transport completion."""

    def __init__(self, log_dir: Path, writes: tuple[tuple[str, str], ...]):
        self.log_dir = log_dir
        self.writes = writes

    def execute(self, request: AgentRunRequest) -> AgentRunResult:
        changed = []
        for relative, content in self.writes:
            target = Path(request.workspace) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
            changed.append(Path(relative))
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log = self.log_dir / f"{request.attempt_id}.jsonl"
        log.write_text('{"event":"direct test dispatch"}\n', encoding="utf-8")
        return AgentRunResult(
            status="completed",
            event_log_path=log,
            changed_files=tuple(changed),
            produced_artifacts=tuple(request.expected_artifacts),
            exit_reason="direct_test_completed",
            tokens_in=None,
            tokens_out=None,
            cost_usd=None,
        )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ReworkTurnFenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _seed_rework_project(self, name: str, manifest: str = "valid"):
        project = build_project(self.root / name)
        accepted = {
            CODING_PROMPT: PROMPT_BODY.encode("utf-8"),
            REVIEW_PROMPT: b"# Accepted review prompt\n",
            SELF_REPORT: ACCEPTED_REPORT_BYTES,
            REVIEW_REPORT: b"Verdict: pass - next: record the verdict\n",
            VERDICT_RECORD: b"# Accepted verdict record\n",
            PRODUCT_PATH: b"VALUE = 'accepted'\n",
            UNRELATED_PATH: b"# Ordinary project note\n",
        }
        for relative, data in accepted.items():
            target = project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

        store = RunStore(project / ".frutlups_drive")
        store.create_run(
            "run_001", {"boundary": "slice_complete", "contract_version": 1}
        )
        store.append_event(
            "run_001",
            {"kind": "run_created", "t": 900.0, "boundary": "slice_complete"},
        )
        record = {
            "contract_version": 1,
            "run_id": "run_001",
            "evidence": [],
            "artifacts": [
                {"path": relative, "sha256": _sha256(data)}
                for relative, data in sorted(accepted.items())
            ],
        }
        if manifest == "valid":
            boundary = store.write_pass_boundary("run_001", record)
            store.append_event(
                "run_001",
                {
                    "kind": "pass_boundary",
                    "t": 901.0,
                    "evidence_sha256": _sha256(boundary.read_bytes()),
                    "evidence_members": 0,
                    "artifact_members": len(record["artifacts"]),
                },
            )
        elif manifest == "unreadable":
            (store.run_dir("run_001") / "pass_boundary.json").write_bytes(
                b"{not-json\n"
            )
            store.append_event(
                "run_001",
                {
                    "kind": "pass_boundary",
                    "t": 901.0,
                    "evidence_sha256": "a" * 64,
                    "evidence_members": 0,
                    "artifact_members": len(record["artifacts"]),
                },
            )
        elif manifest != "missing":
            raise AssertionError(f"unknown manifest fixture: {manifest}")

        source = project / REWORK_PROMPT
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(REWORK_PROMPT_BYTES)
        store.append_event(
            "run_001",
            {
                "kind": "verb",
                "t": 902.0,
                "verb": "declare-rework",
                "artifact": (
                    "05_governance/rework_declarations/"
                    "001_holistic_pass_001.json"
                ),
                "slice": "",
                "pass_id": "holistic_pass_001",
                "slices": ["M001-S01"],
            },
        )
        return project, accepted

    def _scenario(
        self,
        name: str,
        action: MockAgentAction,
        manifest: str = "valid",
        *,
        direct: bool = False,
    ):
        project, accepted = self._seed_rework_project(name, manifest)
        scenario = Scenario(
            self.root / name,
            project=project,
            states=[
                payload(
                    "ready",
                    "execute_coding_prompt",
                    coding_prompt=REWORK_PROMPT,
                    self_report=REWORK_REPORT,
                )
            ],
            coder=[action],
        )
        if direct:
            scenario.supervisor._executors["coder"] = _DirectExecutor(
                scenario.store.run_dir("run_001") / "adapter_logs",
                action.writes,
            )
        return scenario, accepted

    def test_protected_family_predicate_is_exact(self):
        protected = (
            "05_governance/reviews/m001/x_self_report.md",
            "05_governance/reviews/m001/x_review_report.md",
            "05_governance/reviews/m001/x_verdict_record.md",
            "prompts/for_review_agent/002_review_x.md",
            "prompts/for_coding_agent/001_x.md",
            "prompts/for_coding_agent/accepted prompt.md",
        )
        unprotected = (
            "src/package.py",
            "tests/test_package.py",
            "05_governance/reviews/INDEX.md",
            "05_governance/reviews/m001/notes.md",
            "prompts/templates/coding_prompt.md",
            "05_governance/reviews/../x_self_report.md",
        )
        for path in protected:
            with self.subTest(path=path):
                self.assertTrue(oracle.rework_protected_artifact(path))
        for path in unprotected:
            with self.subTest(path=path):
                self.assertFalse(oracle.rework_protected_artifact(path))

    def test_snapshot_storage_is_idempotent_bounded_and_authenticated(self):
        store = RunStore(self.root / "store" / ".frutlups_drive")
        store.create_run("run_001", {"contract_version": 1})
        attempt = store.create_attempt("run_001", "M001-S01")
        members = {SELF_REPORT: ACCEPTED_REPORT_BYTES}

        first = store.write_accepted_snapshot(
            attempt,
            members,
            pass_boundary_sha256="a" * 64,
            max_total_bytes=64 * 1024 * 1024,
        )
        exact_size = store.run_size_bytes("run_001")
        second = store.write_accepted_snapshot(
            attempt,
            members,
            pass_boundary_sha256="a" * 64,
            max_total_bytes=exact_size,
        )
        self.assertEqual(second, first)
        snapshot_path = attempt / first["members"][0]["snapshot"]
        snapshot_path.write_bytes(b"tampered snapshot bytes\n")
        with self.assertRaises(RunStoreRefusal) as invalid:
            store.read_accepted_snapshot(attempt)
        self.assertEqual(invalid.exception.code, "accepted_snapshot_invalid")

        bounded = RunStore(self.root / "bounded" / ".frutlups_drive")
        bounded.create_run("run_001", {"contract_version": 1})
        bounded_attempt = bounded.create_attempt("run_001", "M001-S01")
        with self.assertRaises(RunStoreRefusal) as full:
            bounded.write_accepted_snapshot(
                bounded_attempt,
                members,
                pass_boundary_sha256="b" * 64,
                max_total_bytes=bounded.run_size_bytes("run_001"),
            )
        self.assertEqual(full.exception.code, "accepted_snapshot_store_full")
        self.assertIsNone(bounded.read_accepted_snapshot(bounded_attempt))

    def test_l2_overwrite_stops_on_first_poll_and_snapshot_restores_bytes(self):
        scenario, accepted = self._scenario(
            "overwrite",
            MockAgentAction(writes=((SELF_REPORT, "overwritten accepted report\n"),)),
            direct=True,
        )

        result = scenario.supervisor.tick()

        self.assertEqual(
            (result.kind, result.stop_reason),
            ("stopped", StopReason.PATH_VIOLATION),
            scenario.events(),
        )
        self.assertEqual(scenario.clock.value, 1000.0, "the first poll must stop")
        attempt = scenario.store.list_attempts("run_001", "M001-S01")[0]
        inventory = scenario.store.read_accepted_snapshot(attempt)
        self.assertIsNotNone(inventory)
        members = {item["path"]: item for item in inventory["members"]}
        self.assertEqual(
            set(members),
            {CODING_PROMPT, REVIEW_PROMPT, SELF_REPORT, REVIEW_REPORT, VERDICT_RECORD},
        )
        self.assertNotIn(PRODUCT_PATH, members)
        self.assertNotIn(UNRELATED_PATH, members)
        accepted_member = members[SELF_REPORT]
        snapshot = attempt / accepted_member["snapshot"]
        self.assertEqual(snapshot.read_bytes(), accepted[SELF_REPORT])
        self.assertEqual(accepted_member["sha256"], _sha256(accepted[SELF_REPORT]))

        escalation = result.escalation_path.read_text(encoding="utf-8")
        self.assertIn(SELF_REPORT, escalation)
        self.assertIn(accepted_member["sha256"], escalation)
        self.assertIn(accepted_member["snapshot"], escalation)
        self.assertIn("human", escalation.lower())
        self.assertIn("does not restore", escalation.lower())
        self.assertIn("09_ops/operators_manual.md", escalation)
        self.assertIn(
            "Governed filing protocol (stopped runs only)", escalation
        )

        (scenario.project / SELF_REPORT).write_bytes(snapshot.read_bytes())
        self.assertEqual((scenario.project / SELF_REPORT).read_bytes(), accepted[SELF_REPORT])

    def test_rework_envelope_is_captured_without_mutating_governed_prompt(self):
        action = MockAgentAction(
            writes=(
                (PRODUCT_PATH, "VALUE = 'corrected'\n"),
                (REWORK_REPORT, "# Rework self-report\n"),
            )
        )
        scenario, _ = self._scenario("envelope", action)
        source = scenario.project / REWORK_PROMPT
        before = source.read_bytes()

        result = scenario.supervisor.tick()

        self.assertEqual((result.kind, result.detail), ("acted", "coder_attempt_completed"))
        self.assertEqual(source.read_bytes(), before)
        attempt = scenario.store.list_attempts("run_001", "M001-S01")[0]
        request = scenario.store.read_request(attempt)
        captured = Path(request["prompt_path"]).read_bytes()
        self.assertEqual(captured[: len(_SEAT_CONDUCT_BLOCK)], _SEAT_CONDUCT_BLOCK)
        self.assertIn(REWORK_PROMPT_BYTES, captured)
        self.assertIn(REWORK_ENVELOPE_HEADING, captured)
        self.assertIn(REWORK_REPORT.encode("utf-8"), captured)
        self.assertIn(b"accepted artifact", captured.lower())
        self.assertEqual(request["prompt_sha256"], _sha256(captured))
        self.assertEqual((scenario.project / PRODUCT_PATH).read_text(encoding="utf-8"),
                         "VALUE = 'corrected'\n")

    def test_non_rework_dispatch_bytes_are_unchanged(self):
        scenario = Scenario(
            self.root / "ordinary",
            states=[payload("ready", "execute_coding_prompt")],
            coder=[MockAgentAction(writes=((SELF_REPORT, "ordinary report\n"),))],
        )

        result = scenario.supervisor.tick()

        self.assertEqual((result.kind, result.detail), ("acted", "coder_attempt_completed"))
        attempt = scenario.store.list_attempts("run_001", "M001-S01")[0]
        request = scenario.store.read_request(attempt)
        captured = Path(request["prompt_path"]).read_bytes()
        self.assertEqual(captured, _SEAT_CONDUCT_BLOCK + PROMPT_BODY.encode("utf-8"))
        self.assertNotIn(REWORK_ENVELOPE_HEADING, captured)

    def test_missing_or_unreadable_manifest_fails_closed(self):
        for manifest in ("missing", "unreadable"):
            with self.subTest(manifest=manifest):
                scenario, _ = self._scenario(
                    manifest,
                    MockAgentAction(writes=((REWORK_REPORT, "report\n"),)),
                    manifest=manifest,
                )
                result = scenario.supervisor.tick()
                self.assertEqual(
                    (result.kind, result.stop_reason),
                    ("stopped", StopReason.INVALID_STATE),
                    scenario.events(),
                )
                self.assertFalse(
                    any(event.get("kind") == "dispatch" for event in scenario.events())
                )

    def test_crash_after_snapshot_replays_to_same_path_violation(self):
        scenario, _ = self._scenario(
            "resume",
            MockAgentAction(writes=((SELF_REPORT, "overwritten accepted report\n"),)),
            direct=True,
        )
        write_snapshot = getattr(scenario.store, "write_accepted_snapshot", None)
        self.assertIsNotNone(write_snapshot)

        def crash_after_snapshot(*args, **kwargs):
            write_snapshot(*args, **kwargs)
            raise _SnapshotCrash("after snapshot")

        scenario.store.write_accepted_snapshot = crash_after_snapshot
        with self.assertRaises(_SnapshotCrash):
            scenario.supervisor.tick()

        resumed = Scenario(
            self.root / "resume",
            project=scenario.project,
            states=[
                payload(
                    "ready",
                    "execute_coding_prompt",
                    coding_prompt=REWORK_PROMPT,
                    self_report=REWORK_REPORT,
                )
            ],
            coder=[
                MockAgentAction(
                    writes=((SELF_REPORT, "overwritten accepted report\n"),)
                )
            ],
        )
        resumed.supervisor._executors["coder"] = _DirectExecutor(
            resumed.store.run_dir("run_001") / "adapter_logs",
            ((SELF_REPORT, "overwritten accepted report\n"),),
        )
        self.assertIsNone(resumed.supervisor.resume())
        result = resumed.supervisor.tick()
        self.assertEqual(
            (result.kind, result.stop_reason),
            ("stopped", StopReason.PATH_VIOLATION),
            resumed.events(),
        )
        attempts = resumed.store.list_attempts("run_001", "M001-S01")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(resumed.store.read_transition(attempts[0]), "closed")
        self.assertIsNotNone(resumed.store.read_accepted_snapshot(attempts[0]))
        self.assertIsNotNone(resumed.store.read_accepted_snapshot(attempts[1]))


if __name__ == "__main__":
    unittest.main()
