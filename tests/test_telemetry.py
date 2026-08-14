"""Phase C telemetry reconciliation and degradation proofs."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

from frutlups_drive.cli import main
from frutlups_drive.contracts import AgentRunRequest, AgentRunResult, ExitCode, Role
from frutlups_drive.runstore import RunStore
from frutlups_drive.telemetry import derive_report, render_json, render_text


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = RunStore(self.root / ".frutlups_drive")
        self.store.create_run("run_001", {"contract_version": 1})

    def attempt(self, role, *, tokens_in, tokens_out, cost, shadow=False):
        create = (
            self.store.create_shadow_attempt
            if shadow
            else self.store.create_attempt
        )
        attempt = create("run_001", "M001-S01")
        request = AgentRunRequest(
            run_id="run_001",
            attempt_id=attempt.name,
            role=role,
            prompt_path=Path("prompt.md"),
            prompt_sha256="a" * 64,
            workspace=Path("workspace"),
            base_revision=None,
            adapter="mock",
            model="",
            effort="",
            workspace_access="read_only" if role is Role.REVIEWER else "workspace_write",
            expected_artifacts=(Path("report.md"),),
            max_seconds=30,
            max_cost_usd=1.0,
        )
        result = AgentRunResult(
            status="completed",
            event_log_path=Path("events.jsonl"),
            changed_files=(),
            produced_artifacts=(Path("report.md"),),
            exit_reason="completed",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
        )
        self.store.write_request(attempt, request)
        self.store.write_result(attempt, result)
        if shadow:
            self.store.publish_shadow_report(attempt, b"adversarial evidence\n")
        self.store.advance_transition(attempt, "closed")
        return attempt

    def fixture(self):
        coder = self.attempt(Role.CODER, tokens_in=10, tokens_out=5, cost=0.25)
        reviewer = self.attempt(
            Role.REVIEWER, tokens_in=None, tokens_out=None, cost=0.10
        )
        shadow = self.attempt(
            Role.REVIEWER,
            tokens_in=None,
            tokens_out=None,
            cost=0.05,
            shadow=True,
        )
        facts = [
            {"kind": "run_created", "t": 100},
            {"kind": "dispatch", "t": 101, "slice": "M001-S01", "attempt": coder.name, "role": "coder", "repair": False},
            {"kind": "collected", "t": 103, "slice": "M001-S01", "attempt": coder.name, "role": "coder", "status": "completed", "cost_usd": 0.25},
            {"kind": "verification", "t": 104, "slice": "M001-S01", "attempt": coder.name, "passed": True},
            {"kind": "adoption", "t": 104.5, "slice": "M001-S01", "attempt": coder.name},
            {"kind": "dispatch", "t": 105, "slice": "M001-S01", "attempt": reviewer.name, "role": "reviewer", "repair": False},
            {"kind": "collected", "t": 107, "slice": "M001-S01", "attempt": reviewer.name, "role": "reviewer", "status": "completed", "cost_usd": 0.10},
            {"kind": "verb", "t": 108, "slice": "M001-S01", "verb": "record-verdict", "artifact": "verdict.md"},
            {"kind": "shadow_review", "t": 109, "slice": "M001-S01", "attempt": shadow.name, "primary_attempt": reviewer.name, "role": "shadow_reviewer", "dispatched": True, "dispatched_at": 107.5, "completed_at": 108.5, "status": "completed", "cost_usd": 0.05},
            {"kind": "reconciliation", "t": 110, "slice": "M001-S01", "attempt": "attempt_003", "progress": True},
            {"kind": "pass_boundary", "t": 111},
            {"kind": "stop", "t": 112, "slice": "M001-S01", "attempt": "", "reason": "owner_note", "detail": "changed", "escalation": "owner_note.md"},
            {"kind": "run_store_control", "t": 113, "deleted_runs": ["run_000"]},
            {"kind": "boundary", "t": 114, "boundary": "slice_complete"},
        ]
        for fact in facts:
            self.store.append_event("run_001", fact)
        return coder, reviewer, shadow

    def test_every_emitted_value_exactly_matches_independent_raw_recomputation(self):
        self.fixture()
        before = {
            path.relative_to(self.store.root).as_posix(): path.read_bytes()
            for path in self.store.root.rglob("*")
            if path.is_file()
        }
        report = derive_report(self.store, "run_001")
        attempts = [
            {
                "area": "primary", "attempt_id": "attempt_001", "cost_usd": 0.25,
                "dispatched_at": 101, "duration_seconds": 2, "outcome": "completed",
                "role": "coder", "tokens_in": 10, "tokens_out": 5, "transition": "closed",
            },
            {
                "area": "primary", "attempt_id": "attempt_002", "cost_usd": 0.10,
                "dispatched_at": 105, "duration_seconds": 2, "outcome": "completed",
                "role": "reviewer", "tokens_in": None, "tokens_out": None, "transition": "closed",
            },
            {
                "area": "shadow", "attempt_id": "attempt_001", "cost_usd": 0.05,
                "dispatched_at": 107.5, "duration_seconds": 1.0, "outcome": "completed",
                "role": "shadow_reviewer", "tokens_in": None, "tokens_out": None, "transition": "closed",
            },
        ]
        dispatches = [
            {"attempt_id": "attempt_001", "repair": False, "role": "coder", "slice_id": "M001-S01", "t": 101},
            {"attempt_id": "attempt_002", "repair": False, "role": "reviewer", "slice_id": "M001-S01", "t": 105},
            {"attempt_id": "attempt_001", "repair": False, "role": "shadow_reviewer", "slice_id": "M001-S01", "t": 107.5},
        ]
        verdicts = [{"artifact": "verdict.md", "slice_id": "M001-S01", "t": 108}]
        verifications = [{"attempt_id": "attempt_001", "passed": True, "slice_id": "M001-S01", "t": 104}]
        stops = [{"attempt_id": "", "detail": "changed", "escalation": "owner_note.md", "reason": "owner_note", "slice_id": "M001-S01", "t": 112}]
        summary = {
            "attempts": 3,
            "attempts_by_outcome": {"completed": 3},
            "attempts_by_role": {"coder": 1, "reviewer": 1, "shadow_reviewer": 1},
            "attempts_with_unknown_cost": 0,
            "attempts_with_unknown_tokens_in": 2,
            "attempts_with_unknown_tokens_out": 2,
            "cost_usd_known_sum": 0.4,
            "dispatches": 3,
            "dispatches_by_role": {"coder": 1, "reviewer": 1, "shadow_reviewer": 1},
            "escalations": 1,
            "journal_events_by_kind": {
                "adoption": 1, "boundary": 1, "collected": 2, "dispatch": 2,
                "pass_boundary": 1, "reconciliation": 1, "run_created": 1,
                "run_store_control": 1, "shadow_review": 1, "stop": 1,
                "verb": 1, "verification": 1,
            },
            "stops": 1,
            "tokens_in_known_sum": 10,
            "tokens_out_known_sum": 5,
            "verdicts_recorded": 1,
            "verifications": {"failed": 0, "passed": 1, "unknown": 0},
        }
        slice_summary = dict(summary)
        slice_summary["journal_events_by_kind"] = {
            "adoption": 1, "collected": 2, "dispatch": 2, "reconciliation": 1,
            "shadow_review": 1, "stop": 1, "verb": 1, "verification": 1,
        }
        independently_recomputed = {
            "dispatches": dispatches,
            "errors": [],
            "run_id": "run_001",
            "schema": "frutlups_drive_report_v1",
            "slices": [{"attempts": attempts, "slice_id": "M001-S01", "summary": slice_summary}],
            "stops": stops,
            "summary": summary,
            "verdicts": verdicts,
            "verifications": verifications,
            "wall_duration_seconds": 14,
        }
        self.assertEqual(report, independently_recomputed)
        self.assertEqual(json.loads(render_json(report)), independently_recomputed)
        self.assertEqual(render_json(report), render_json(derive_report(self.store, "run_001")))
        self.assertEqual(render_text(report), render_text(derive_report(self.store, "run_001")))
        after = {
            path.relative_to(self.store.root).as_posix(): path.read_bytes()
            for path in self.store.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before, "reporting is byte-read-only")

    def test_malformed_members_degrade_to_unknown_without_losing_other_facts(self):
        _, reviewer, _ = self.fixture()
        (reviewer / "result.json").write_bytes(b"{partial")
        with open(self.store.run_dir("run_001") / "events.jsonl", "ab") as stream:
            stream.write(b"not-json\n")
        report = derive_report(self.store, "run_001")
        observed = report["slices"][0]["attempts"][1]
        self.assertEqual(observed["outcome"], "completed")
        self.assertIsNone(observed["tokens_in"])
        self.assertIsNone(observed["cost_usd"])
        self.assertIn(
            {"code": "record_invalid", "member": "slices/M001-S01/attempt_002/result.json"},
            report["errors"],
        )
        self.assertIn(
            {"code": "event_invalid", "member": "events.jsonl:15"},
            report["errors"],
        )
        self.assertEqual(report["summary"]["tokens_in_known_sum"], 10)
        self.assertEqual(report["summary"]["attempts_with_unknown_tokens_in"], 2)

    def test_report_cli_text_and_json_are_deterministic_and_read_only(self):
        self.fixture()
        before = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        text_out = io.StringIO()
        with contextlib.redirect_stdout(text_out):
            code = main(["report", str(self.root), "run_001"])
        self.assertEqual(code, int(ExitCode.OK))
        self.assertIn('schema="frutlups_drive_report_v1"', text_out.getvalue())
        json_out = io.StringIO()
        with contextlib.redirect_stdout(json_out):
            code = main(["report", str(self.root), "run_001", "--json"])
        self.assertEqual(code, int(ExitCode.OK))
        self.assertEqual(json.loads(json_out.getvalue()), derive_report(self.store, "run_001"))
        after = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
