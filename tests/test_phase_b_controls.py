"""Phase B freeze, second-pass, owner routing, resolver, and crash tests."""

import json
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

from frutlups_drive.budget import BudgetCounters
from frutlups_drive.contracts import StopReason
from frutlups_drive.dispatch.mock import MockAgentAction
from frutlups_drive.escalate import write_escalation
from frutlups_drive.runstore import RESOLUTION_MARKER_SUFFIX, RunStore, RunStoreRefusal

from _scenario import (
    ACTIVE_ROADMAP,
    ROADMAP_BODY,
    SELF_REPORT,
    Scenario,
    build_project,
    payload,
)


def complete_state():
    return payload(
        "complete",
        None,
        actor="none",
        frontier_present=False,
        completion_evidence={"path": "05_governance/completion.md"},
    )


def holistic(findings):
    return MockAgentAction(
        writes=(("holistic_review.json", json.dumps({"findings": findings})),)
    )


PHASE_B_POLICY = (
    "[target]\nmax_passes = 3\nmax_slices = 10\n"
    "[roles.reviewer]\nadapter = \"mock\"\n"
    "[autonomy]\npass_boundary = \"two_clean\"\n"
    "auto_continue_past_frontier_recorded = true\n"
)
NO_LEDGER_POLICY = 'index_mode = "no-ledger"\n' + PHASE_B_POLICY


class _ReworkRecordingWriter:
    def __init__(self, project_root):
        self._root = Path(project_root)
        self.calls = []

    def invoke(
        self,
        verb,
        declared_path,
        review_report=None,
        *,
        slice_id=None,
        pass_id=None,
        rework_slices=(),
    ):
        self.calls.append(
            {
                "verb": verb,
                "declared_path": declared_path,
                "review_report": review_report,
                "slice_id": slice_id,
                "pass_id": pass_id,
                "rework_slices": rework_slices,
            }
        )
        target = self._root / "prompts/for_coding_agent/999_test_rework.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# governed rework\n", encoding="utf-8")
        return target


class PhaseBLoopTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    @staticmethod
    def _record_acceptances(scenario, *slice_ids):
        for slice_id in slice_ids:
            scenario.supervisor._journal(
                "verb",
                verb="record-verdict",
                artifact=f"05_governance/reviews/{slice_id}_verdict.md",
                slice=slice_id,
            )

    def _declaration_scenario(self, findings, *reopenable):
        scenario = Scenario(
            self.root,
            states=[complete_state(), complete_state(), complete_state()],
            reviewer=[holistic(findings)],
            policy_body=PHASE_B_POLICY,
        )
        writer = _ReworkRecordingWriter(scenario.project)
        scenario.supervisor._verb_writer = writer
        scenario.supervisor._verb_supports_rework = True
        self._record_acceptances(scenario, *reopenable)
        self.assertEqual(scenario.supervisor.tick().detail, "pass_boundary")
        self.assertEqual(
            scenario.supervisor.tick().detail, "second_pass_worklist"
        )
        return scenario, writer

    def test_mixed_findings_declare_valid_subset_and_journal_each_unmappable(self):
        scenario, writer = self._declaration_scenario(
            ["M000", "M001-S01", "M999", "M001-S02"],
            "M001-S01",
            "M001-S02",
        )

        result = scenario.supervisor.tick()

        self.assertEqual((result.kind, result.detail), ("acted", "verb:declare-rework"))
        self.assertEqual(len(writer.calls), 1)
        self.assertEqual(writer.calls[0]["verb"], "declare-rework")
        self.assertEqual(writer.calls[0]["pass_id"], "holistic_pass_001")
        self.assertEqual(
            writer.calls[0]["rework_slices"], ("M001-S01", "M001-S02")
        )
        unmappable = [
            event
            for event in scenario.events()
            if event["kind"] == "holistic_finding_unmappable"
        ]
        self.assertEqual(
            [
                (event["pass_id"], event["finding_id"], event["progress"])
                for event in unmappable
            ],
            [
                ("holistic_pass_001", "M000", False),
                ("holistic_pass_001", "M999", False),
            ],
        )

    def test_all_invalid_findings_stop_governed_without_verb_attempt(self):
        scenario, writer = self._declaration_scenario(["M000", "M999"])

        result = scenario.supervisor.tick()

        self.assertEqual(result.stop_reason, StopReason.HOLISTIC_FINDINGS_UNMAPPABLE)
        self.assertEqual(writer.calls, [])
        escalation = result.escalation_path.read_text(encoding="utf-8")
        self.assertIn('stop_reason = "holistic_findings_unmappable"', escalation)
        self.assertIn("holistic_pass_001", escalation)
        self.assertIn("M000", escalation)
        self.assertIn("M999", escalation)

    def test_all_valid_findings_keep_declaration_behavior_unchanged(self):
        scenario, writer = self._declaration_scenario(
            ["M001-S02", "M001-S01"], "M001-S01", "M001-S02"
        )

        result = scenario.supervisor.tick()

        self.assertEqual((result.kind, result.detail), ("acted", "verb:declare-rework"))
        self.assertEqual(len(writer.calls), 1)
        self.assertEqual(
            writer.calls[0]["rework_slices"], ("M001-S02", "M001-S01")
        )
        self.assertFalse(
            any(
                event["kind"] == "holistic_finding_unmappable"
                for event in scenario.events()
            )
        )

    def test_duplicate_valid_findings_collapse_before_declaration(self):
        scenario, writer = self._declaration_scenario(
            ["M001-S01", "M001-S01"], "M001-S01"
        )

        result = scenario.supervisor.tick()

        self.assertEqual((result.kind, result.detail), ("acted", "verb:declare-rework"))
        self.assertEqual(len(writer.calls), 1)
        self.assertEqual(writer.calls[0]["rework_slices"], ("M001-S01",))

    def test_freeze_findings_worklist_and_two_consecutive_clean_closure(self):
        states = [
            complete_state(),
            complete_state(),
            payload("ready", "execute_coding_prompt", round_=2),
            payload("ready", "frontier_recorded", round_=2),
            complete_state(),
            complete_state(),
        ]
        scenario = Scenario(
            self.root,
            states=states,
            coder=[MockAgentAction(writes=((SELF_REPORT, "# second pass\n"),))],
            reviewer=[
                holistic(["M001-S01"]),
                holistic([]),
                holistic([]),
            ],
            boundary="roadmap_complete",
            policy_body=PHASE_B_POLICY,
        )
        self._record_acceptances(scenario, "M001-S01")
        first = scenario.supervisor.tick()
        self.assertEqual(first.detail, "pass_boundary")
        boundary_path = scenario.store.run_dir("run_001") / "pass_boundary.json"
        oracle_path = scenario.store.run_dir("run_001") / "pass_oracle.json"
        frozen = boundary_path.read_bytes()
        self.assertTrue(oracle_path.is_file())
        self.assertEqual(scenario.supervisor.tick().detail, "second_pass_worklist")
        self.assertEqual(scenario.supervisor.tick().detail, "coder_attempt_completed")
        self.assertEqual(scenario.supervisor.tick().detail, "continue_past_frontier")
        self.assertEqual(scenario.supervisor.tick().detail, "clean_pass")
        result = scenario.supervisor.tick()
        self.assertEqual((result.kind, result.detail), ("boundary", "complete"))
        self.assertEqual(boundary_path.read_bytes(), frozen)
        events = scenario.events()
        self.assertEqual(sum(e["kind"] == "pass_boundary" for e in events), 1)
        oracle_events = [e for e in events if e["kind"] == "pass_oracle"]
        self.assertEqual(len(oracle_events), 1)
        self.assertEqual(
            set(oracle_events[0]),
            {
                "kind", "t", "artifact", "contract_version", "run_id",
                "pass_boundary_sha256", "oracle_sha256", "observations",
            },
        )
        self.assertEqual(oracle_events[0]["artifact"], "pass_oracle.json")
        counters = BudgetCounters.from_events([{"kind": "run_created", "t": 1.0}])
        before = vars(counters).copy()
        counters.apply(oracle_events[0])
        self.assertEqual(vars(counters), before)
        reviews = [e for e in events if e["kind"] == "holistic_review"]
        self.assertEqual([e["clean"] for e in reviews], [False, True, True])
        self.assertEqual(scenario.counters().passes_completed, 3)
        with self.assertRaises(RunStoreRefusal):
            scenario.store.write_pass_boundary("run_001", {"contract_version": 2})

    def test_oracle_observations_do_not_become_a_worklist(self):
        scenario = Scenario(
            self.root,
            states=[complete_state(), complete_state()],
            reviewer=[holistic([])],
            policy_body=PHASE_B_POLICY,
        )
        index = scenario.project / "05_governance/reviews/INDEX.md"
        missing_self_report = (
            "05_governance/reviews/m001/missing_self_report" + ".md"
        )
        index.write_text(
            index.read_text(encoding="utf-8")
            + "| M001 | M001-S01 | 1 | "
              f"`{missing_self_report}` | - | - | "
              "pass | fixture |\n",
            encoding="utf-8",
        )

        self.assertEqual(scenario.supervisor.tick().detail, "pass_boundary")
        bundle = scenario.store.read_pass_oracle("run_001")
        self.assertTrue(bundle["observations"])
        result = scenario.supervisor.tick()
        self.assertEqual((result.kind, result.detail), ("acted", "clean_pass"))
        self.assertIsNone(scenario.supervisor._active_worklist())
        self.assertFalse(
            any(
                event.get("kind") == "verb"
                and event.get("verb") == "declare-rework"
                for event in scenario.events()
            )
        )
        reviewer_dispatches = [
            event
            for event in scenario.events()
            if event.get("kind") == "dispatch"
            and event.get("role") == "reviewer"
        ]
        self.assertEqual(len(reviewer_dispatches), 1)

    def test_holistic_prompt_carries_oracle_protocol_and_hash(self):
        scenario = Scenario(
            self.root,
            states=[complete_state(), complete_state()],
            reviewer=[holistic([])],
            policy_body=PHASE_B_POLICY,
        )
        scenario.supervisor.tick()
        scenario.supervisor.tick()
        attempt = scenario.store.list_attempts(
            "run_001", "holistic_pass_001"
        )[0]
        prompt = (attempt / "holistic_prompt.md").read_text(encoding="utf-8")
        oracle_event = next(
            event for event in scenario.events() if event["kind"] == "pass_oracle"
        )
        self.assertIn("../../../../pass_oracle.json", prompt)
        self.assertIn(oracle_event["oracle_sha256"], prompt)
        self.assertIn("confirm or refute", prompt)
        self.assertIn("primary sources", prompt)
        self.assertIn("Declared reviews INDEX mode: human-ledger", prompt)
        self.assertIn("annotation is a pointer", prompt)
        self.assertIn("never a pre-judgment", prompt)
        self.assertIn("attack beyond the bundle", prompt)
        self.assertIn("spot-check", prompt)
        self.assertIn("Produce exactly holistic_review.json", prompt)

    def test_no_ledger_holistic_prompt_teaches_tripwire_and_authority(self):
        scenario = Scenario(
            self.root,
            states=[complete_state(), complete_state()],
            reviewer=[holistic([])],
            policy_body=NO_LEDGER_POLICY,
        )
        scenario.supervisor.tick()
        bundle = scenario.store.read_pass_oracle("run_001")
        self.assertEqual(bundle["index_mode"], "no-ledger")
        scenario.supervisor.tick()
        attempt = scenario.store.list_attempts(
            "run_001", "holistic_pass_001"
        )[0]
        prompt = (attempt / "holistic_prompt.md").read_text(encoding="utf-8")
        self.assertIn("Declared reviews INDEX mode: no-ledger", prompt)
        self.assertIn("unindexed-artifact complaints are absent by contract", prompt)
        self.assertIn("ledger_row_in_no_ledger_project", prompt)
        self.assertIn("high-priority", prompt)
        self.assertIn("holistic_review.json remains the sole worklist authority", prompt)

    def test_no_ledger_still_requires_a_readable_index(self):
        scenario = Scenario(
            self.root,
            states=[complete_state()],
            reviewer=[holistic([])],
            policy_body=NO_LEDGER_POLICY,
        )
        (scenario.project / "05_governance/reviews/INDEX.md").unlink()
        result = scenario.supervisor.tick()
        self.assertEqual(result.stop_reason, StopReason.INVALID_STATE)
        self.assertEqual(
            scenario.store.list_attempts("run_001", "holistic_pass_001"), ()
        )

    def test_missing_index_stops_before_holistic_dispatch(self):
        scenario = Scenario(
            self.root,
            states=[complete_state()],
            reviewer=[holistic([])],
            policy_body=PHASE_B_POLICY,
        )
        (scenario.project / "05_governance/reviews/INDEX.md").unlink()
        result = scenario.supervisor.tick()
        self.assertEqual(result.stop_reason, StopReason.INVALID_STATE)
        self.assertTrue(result.escalation_path.is_file())
        self.assertEqual(
            scenario.store.list_attempts("run_001", "holistic_pass_001"), ()
        )

    def test_tampered_oracle_stops_before_holistic_dispatch(self):
        scenario = Scenario(
            self.root,
            states=[complete_state(), complete_state()],
            reviewer=[holistic([])],
            policy_body=PHASE_B_POLICY,
        )
        self.assertEqual(scenario.supervisor.tick().detail, "pass_boundary")
        oracle = scenario.store.run_dir("run_001") / "pass_oracle.json"
        oracle.write_text('{"observations":[]}\n', encoding="utf-8")
        result = scenario.supervisor.tick()
        self.assertEqual(result.stop_reason, StopReason.INVALID_STATE)
        self.assertTrue(result.escalation_path.is_file())
        self.assertEqual(
            scenario.store.list_attempts("run_001", "holistic_pass_001"), ()
        )

    def test_ready_frontier_outside_worklist_refuses(self):
        states = [
            complete_state(),
            complete_state(),
            payload(
                "ready",
                "execute_coding_prompt",
                slice_id="M001-S02",
            ),
        ]
        scenario = Scenario(
            self.root,
            states=states,
            reviewer=[holistic(["M001-S01"])],
            policy_body=PHASE_B_POLICY,
        )
        self._record_acceptances(scenario, "M001-S01")
        scenario.supervisor.tick()
        scenario.supervisor.tick()
        result = scenario.supervisor.tick()
        self.assertEqual(result.stop_reason, StopReason.INVALID_STATE)

    def test_worklist_drains_from_accepted_verdict_or_scripted_completion(self):
        scenario = Scenario(
            self.root,
            states=[complete_state(), complete_state()],
            reviewer=[holistic(["M001-S01"])],
            policy_body=PHASE_B_POLICY,
        )
        self._record_acceptances(scenario, "M001-S01")
        scenario.supervisor.tick()
        scenario.supervisor.tick()
        self.assertEqual(
            scenario.supervisor._missing_worklist_slices(), ("M001-S01",)
        )
        scenario.supervisor._journal(
            "verb",
            verb="record-verdict",
            artifact="05_governance/reviews/fresh_verdict.md",
            slice="M001-S01",
        )
        self.assertEqual(scenario.supervisor._missing_worklist_slices(), ())

        scenario.supervisor._journal(
            "holistic_review",
            pass_number=2,
            findings=["M001-S01"],
            clean=False,
            attempt="",
            slice="holistic_pass_002",
        )
        self.assertEqual(
            scenario.supervisor._missing_worklist_slices(), ("M001-S01",)
        )
        scenario.supervisor._journal(
            "slice_complete", slice="M001-S01", milestone="M001"
        )
        self.assertEqual(scenario.supervisor._missing_worklist_slices(), ())

    def test_new_owner_note_stops_without_interpreting_prose(self):
        scenario = Scenario(self.root, states=[payload()])
        notes = scenario.project / "05_governance/human_owner_notes"
        notes.mkdir(parents=True, exist_ok=True)
        secret_prose = "this prose must never enter control flow"
        (notes / "016_new.md").write_text(secret_prose, encoding="utf-8")
        result = scenario.supervisor.tick()
        self.assertEqual(result.stop_reason, StopReason.OWNER_NOTE)
        escalation = result.escalation_path.read_text(encoding="utf-8")
        self.assertIn("016_new.md", escalation)
        self.assertNotIn(secret_prose, escalation)
        self.assertNotIn("plan_read", scenario.event_kinds())

    def test_changed_owner_note_is_detected_by_hash_only(self):
        project = build_project(self.root)
        notes = project / "05_governance/human_owner_notes"
        notes.mkdir(parents=True)
        note = notes / "015_existing.md"
        note.write_bytes(b"admission bytes\n")
        scenario = Scenario(self.root, project=project, states=[payload()])
        note.write_bytes(b"changed bytes that remain uninterpreted\n")
        result = scenario.supervisor.tick()
        self.assertEqual(result.stop_reason, StopReason.OWNER_NOTE)
        self.assertIn("015_existing.md", result.detail)

    def test_max_passes_stops_before_an_unbounded_review(self):
        policy = PHASE_B_POLICY.replace("max_passes = 3", "max_passes = 1")
        scenario = Scenario(
            self.root,
            states=[complete_state(), complete_state(), complete_state()],
            reviewer=[holistic(["M001-S01"])],
            policy_body=policy,
        )
        self.assertEqual(scenario.supervisor.tick().detail, "pass_boundary")
        self.assertEqual(scenario.supervisor.tick().detail, "second_pass_worklist")
        result = scenario.supervisor.tick()
        self.assertEqual(result.stop_reason, StopReason.BUDGET_EXHAUSTED)
        self.assertIn("passes", result.detail)


class ResolutionMarkerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = RunStore(Path(self._tmp.name) / "store")

    def escalation(self, run_id):
        self.store.create_run(run_id, {"contract_version": 1})
        return write_escalation(
            self.store,
            run_id,
            reason=StopReason.NO_PROGRESS,
            slice_id="M001-S01",
            attempt_id="attempt_001",
            planning_snapshot="bounded",
            attempts_summary="bounded",
            decision_required="bounded",
            safe_options="bounded",
            actions_not_taken="bounded",
            resume_command="bounded",
        )

    def test_exact_empty_sibling_releases_old_run_for_rotation(self):
        escalation = self.escalation("run_001")
        self.store.create_run("run_002", {"contract_version": 1})
        with self.assertRaisesRegex(RunStoreRefusal, "run_store_full"):
            self.store.enforce_limits(
                "run_002", max_total_bytes=1_000_000, max_retained_runs=1
            )
        marker = escalation.with_name(escalation.name + RESOLUTION_MARKER_SUFFIX)
        marker.write_bytes(b"")
        result = self.store.enforce_limits(
            "run_002", max_total_bytes=1_000_000, max_retained_runs=1
        )
        self.assertEqual(result.deleted_runs, ("run_001",))

    def test_nonempty_or_extra_marker_shape_stays_protected(self):
        escalation = self.escalation("run_001")
        self.store.create_run("run_002", {"contract_version": 1})
        escalation.with_name(escalation.name + RESOLUTION_MARKER_SUFFIX).write_bytes(
            b"resolved\n"
        )
        escalation.with_name("extra.resolved").write_bytes(b"")
        with self.assertRaisesRegex(RunStoreRefusal, "run_store_full"):
            self.store.enforce_limits(
                "run_002", max_total_bytes=1_000_000, max_retained_runs=1
            )


class PhaseBCrashTests(unittest.TestCase):
    class Crash(Exception):
        pass

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_resume_after_reconciliation_journal_does_not_redispatch(self):
        crashed = {"done": False}

        def event_hook(kind, _run_id):
            if kind == "reconciliation" and not crashed["done"]:
                crashed["done"] = True
                raise self.Crash()

        proposed = ROADMAP_BODY.replace(
            "Implement the fixture behavior.", "Sharpen the fixture behavior."
        )
        states = [
            payload("needs_specification", None, actor="architect", frontier_present=False),
            payload(),
        ]
        first = Scenario(
            self.root,
            states=states,
            architect=[MockAgentAction(writes=(("roadmap_proposal.md", proposed),))],
            coder=[MockAgentAction(writes=((SELF_REPORT, "# resumed\n"),))],
            event_hook=event_hook,
        )
        with self.assertRaises(self.Crash):
            first.supervisor.tick()
        self.assertEqual(
            (first.project / ACTIVE_ROADMAP).read_text(encoding="utf-8"), proposed
        )
        resumed = Scenario(
            self.root,
            project=first.project,
            run_id="run_001",
            states=states,
            architect=[MockAgentAction(writes=(("roadmap_proposal.md", proposed),))],
            coder=[MockAgentAction(writes=((SELF_REPORT, "# resumed\n"),))],
        )
        self.assertIsNone(resumed.supervisor.resume())
        self.assertEqual(resumed.supervisor.tick().detail, "coder_attempt_completed")
        architect_dispatches = [
            e for e in resumed.events() if e.get("kind") == "dispatch" and e.get("role") == "architect"
        ]
        self.assertEqual(len(architect_dispatches), 1)

    def test_resume_after_pass_boundary_journal_preserves_frozen_bytes(self):
        crashed = {"done": False}

        def event_hook(kind, _run_id):
            if kind == "pass_boundary" and not crashed["done"]:
                crashed["done"] = True
                raise self.Crash()

        states = [complete_state(), complete_state()]
        first = Scenario(
            self.root,
            states=states,
            reviewer=[holistic([])],
            policy_body=PHASE_B_POLICY,
            event_hook=event_hook,
        )
        with self.assertRaises(self.Crash):
            first.supervisor.tick()
        boundary = first.store.run_dir("run_001") / "pass_boundary.json"
        frozen = boundary.read_bytes()
        resumed = Scenario(
            self.root,
            project=first.project,
            run_id="run_001",
            states=states,
            reviewer=[holistic([])],
        )
        self.assertIsNone(resumed.supervisor.resume())
        self.assertEqual(resumed.supervisor.tick().detail, "clean_pass")
        self.assertEqual(boundary.read_bytes(), frozen)
        self.assertEqual(
            sum(e["kind"] == "pass_boundary" for e in resumed.events()), 1
        )


if __name__ == "__main__":
    unittest.main()
