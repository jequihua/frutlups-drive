"""Supervisor scenario lanes: pass, repair, stops, budgets, ladder, journal."""

import hashlib
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

from frutlups_drive.budget import BudgetCounters
from frutlups_drive.contracts import LadderFailureClass, StopReason
from frutlups_drive.dispatch.mock import MockAgentAction
from frutlups_drive.runstore import RunStore
from frutlups_drive.supervisor import EVENT_KINDS

from _scenario import (
    CODING_PROMPT,
    DEFAULT_VERBS,
    ROADMAP_BODY,
    REVIEW_PROMPT,
    REVIEW_REPORT,
    SELF_REPORT,
    Scenario,
    build_project,
    clean_pass_states,
    payload,
)


class ScenarioTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def scenario(self, **kwargs):
        return Scenario(self.tmp, **kwargs)

    def assert_stop(self, result, reason):
        self.assertEqual(result.kind, "stopped")
        self.assertEqual(result.stop_reason, reason)
        self.assertIsNotNone(result.escalation_path)
        self.assertTrue(result.escalation_path.is_file())


CODER_WRITES_REPORT = MockAgentAction(
    writes=((SELF_REPORT, "# Coder Self-Report\n\nIntent:\ndone\n"),)
)
REVIEWER_WRITES_REPORT = MockAgentAction(
    writes=(
        (
            REVIEW_REPORT,
            "# Review\n\nVerdict: pass — next: record the verdict\n",
        ),
    )
)


class CleanPassScenarioTests(ScenarioTestCase):
    def run_clean(self):
        scenario = self.scenario(
            states=clean_pass_states(),
            coder=[CODER_WRITES_REPORT],
            reviewer=[REVIEWER_WRITES_REPORT],
            verbs=DEFAULT_VERBS,
        )
        return scenario, scenario.supervisor.run_until()

    def test_clean_pass_reaches_slice_boundary(self):
        scenario, result = self.run_clean()
        self.assertEqual(result.kind, "boundary")
        self.assertEqual(result.detail, "slice_complete")
        self.assertEqual(scenario.store.list_escalations("run_001"), ())
        self.assertTrue((scenario.project / SELF_REPORT).is_file())
        self.assertTrue((scenario.project / REVIEW_REPORT).is_file())

    def test_clean_pass_journal_is_complete_and_replayable(self):
        scenario, result = self.run_clean()
        events = scenario.events()
        kinds = [event["kind"] for event in events]
        for kind in kinds:
            self.assertIn(kind, EVENT_KINDS)
        self.assertEqual(kinds.count("dispatch"), 2)
        self.assertEqual(kinds.count("verification"), 1)
        self.assertEqual(kinds.count("verb"), 2)
        self.assertEqual(kinds.count("slice_complete"), 1)
        self.assertEqual(kinds.count("boundary"), 1)
        # every tick begins with a fresh planning read and journals once
        self.assertEqual(kinds.count("plan_read"), kinds.count("tick"))
        replayed = BudgetCounters.from_events(events)
        self.assertEqual(replayed.slices_completed, 1)
        self.assertEqual(replayed.coder_dispatches_for("M001-S01"), 1)

    def test_round_one_dispatches_journal_default_efforts(self):
        scenario = self.scenario(
            states=clean_pass_states(),
            coder=[CODER_WRITES_REPORT],
            reviewer=[REVIEWER_WRITES_REPORT],
            verbs=DEFAULT_VERBS,
            role_efforts={
                "coder": ("medium", "high"),
                "reviewer": ("high", "max"),
            },
        )
        scenario.supervisor.run_until()
        dispatches = [e for e in scenario.events() if e["kind"] == "dispatch"]
        self.assertEqual(
            [(e["role"], e["effort"]) for e in dispatches],
            [("coder", "medium"), ("reviewer", "high")],
        )

    def test_attempts_reach_terminal_transitions_with_evidence(self):
        scenario, _ = self.run_clean()
        attempts = scenario.store.list_attempts("run_001", "M001-S01")
        self.assertEqual(len(attempts), 2)
        for attempt in attempts:
            self.assertEqual(scenario.store.read_transition(attempt), "closed")
        coder_attempt = attempts[0]
        self.assertTrue((coder_attempt / "verification/evidence.toml").is_file())
        self.assertTrue((coder_attempt / "diff_manifest.json").is_file())
        self.assertTrue((coder_attempt / "provider_events.jsonl").is_file())


class RepairScenarioTests(ScenarioTestCase):
    def test_needs_work_round_two_repair_then_pass(self):
        from _scenario import REVIEW_PROMPT_2, REVIEW_REPORT_2, VERDICT_RECORD

        states = [
            payload("ready", "execute_coding_prompt"),
            payload("ready", "make_review_prompt", review_prompt=REVIEW_PROMPT),
            payload("ready", "execute_review_prompt", review_prompt=REVIEW_PROMPT,
                    review_report=REVIEW_REPORT),
            payload("ready", "record_verdict", review_prompt=REVIEW_PROMPT,
                    review_report=REVIEW_REPORT,
                    verdict_record="05_governance/reviews/m001/needs_work_record.md",
                    verdict={"value": "needs_work", "next_move": "repair",
                             "report": REVIEW_REPORT}),
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
        ]
        verbs = {
            "make-review-prompt": (
                (REVIEW_PROMPT, "# Review round 1\n"),
                (REVIEW_PROMPT_2, "# Review round 2\n"),
            ),
            "record-verdict": (
                ("05_governance/reviews/m001/needs_work_record.md", "needs_work\n"),
                (VERDICT_RECORD, "pass\n"),
            ),
        }
        scenario = self.scenario(
            states=states,
            coder=[CODER_WRITES_REPORT,
                   MockAgentAction(writes=((SELF_REPORT, "# Repaired report\n"),))],
            reviewer=[
                MockAgentAction(writes=((REVIEW_REPORT,
                    "Verdict: needs_work — next: repair\n"),)),
                MockAgentAction(writes=((REVIEW_REPORT_2,
                    "Verdict: pass — next: record\n"),)),
            ],
            verbs=verbs,
            role_efforts={
                "coder": ("medium", "high"),
                "reviewer": ("high", "xhigh"),
            },
        )
        result = scenario.supervisor.run_until()
        self.assertEqual(result.kind, "boundary")
        counters = scenario.counters()
        self.assertEqual(counters.coder_dispatches_for("M001-S01"), 2)
        self.assertEqual(scenario.event_kinds().count("verification"), 2)
        dispatches = [e for e in scenario.events() if e["kind"] == "dispatch"]
        self.assertEqual(
            [(e["role"], e["effort"]) for e in dispatches],
            [
                ("coder", "medium"),
                ("reviewer", "high"),
                ("coder", "high"),
                ("reviewer", "xhigh"),
            ],
        )

    def test_fix_self_report_repair_carries_diagnostics_verbatim(self):
        diagnostics = (
            {"severity": "error", "code": "self_report_heading_missing",
             "message": "the report is missing 'Deviations From Prompt:'"},
        )
        states = [
            payload("ready", "execute_coding_prompt"),
            payload("ready", "fix_self_report", diagnostics=diagnostics),
        ]
        scenario = self.scenario(
            states=states,
            coder=[
                CODER_WRITES_REPORT,
                MockAgentAction(writes=((SELF_REPORT, "# Fixed report\n"),)),
            ],
            role_efforts={"coder": ("medium", "high")},
        )
        first = scenario.supervisor.tick()
        second = scenario.supervisor.tick()
        self.assertEqual(first.kind, "acted")
        self.assertEqual(second.kind, "acted")
        dispatches = [e for e in scenario.events() if e["kind"] == "dispatch"]
        self.assertEqual(dispatches[1]["repair"], True)
        self.assertEqual([e["effort"] for e in dispatches], ["medium", "medium"])
        attempts = scenario.store.list_attempts("run_001", "M001-S01")
        repair_prompt = attempts[1] / "repair_prompt.md"
        text = repair_prompt.read_bytes().decode("utf-8")
        self.assertIn("Repair Diagnostics (verbatim)", text)
        self.assertIn("self_report_heading_missing", text)
        self.assertIn("the report is missing 'Deviations From Prompt:'", text)
        self.assertIn("Do the fixture work.", text)

    def test_corrective_round_report_repair_inherits_corrective_effort(self):
        diagnostics = (
            {"severity": "error", "code": "round_two_report_incomplete",
             "message": "complete the current round report"},
        )
        scenario = self.scenario(
            states=[
                payload("ready", "execute_coding_prompt"),
                payload("ready", "execute_coding_prompt", round_=2),
                payload(
                    "ready", "fix_self_report", round_=2,
                    diagnostics=diagnostics,
                ),
            ],
            coder=[
                CODER_WRITES_REPORT,
                MockAgentAction(writes=((SELF_REPORT, "# Round two\n"),)),
                MockAgentAction(writes=((SELF_REPORT, "# Round two fixed\n"),)),
            ],
            role_efforts={"coder": ("medium", "high")},
        )
        for _ in range(3):
            self.assertEqual(scenario.supervisor.tick().kind, "acted")
        dispatches = [e for e in scenario.events() if e["kind"] == "dispatch"]
        self.assertEqual(
            [(e["repair"], e["effort"]) for e in dispatches],
            [(False, "medium"), (False, "high"), (True, "high")],
        )

    def test_repair_exhaustion_stops(self):
        diagnostics = (
            {"severity": "error", "code": "still_broken", "message": "still broken"},
        )
        states = [
            payload("ready", "fix_self_report", diagnostics=diagnostics),
            payload("ready", "fix_self_report", diagnostics=diagnostics),
        ]
        scenario = self.scenario(
            states=states,
            coder=[MockAgentAction(writes=((SELF_REPORT, "attempt\n"),))],
            policy_body="[limits]\nmax_report_repairs = 1\n",
        )
        result = scenario.supervisor.run_until()
        self.assert_stop(result, StopReason.REPAIR_EXHAUSTED)


class MandatoryStopScenarioTests(ScenarioTestCase):
    def test_invalid_planning_state_stops_fail_closed(self):
        states = [
            payload("invalid", None, actor="none",
                    diagnostics=({"severity": "error", "code": "state_drift",
                                  "message": "projection mismatch"},))
        ]
        scenario = self.scenario(states=states)
        result = scenario.supervisor.run_until()
        self.assert_stop(result, StopReason.INVALID_STATE)
        text = result.escalation_path.read_bytes().decode("utf-8")
        self.assertIn("state_drift", text)

    def test_unknown_contract_version_refuses_before_any_action(self):
        import json

        bad = json.dumps({"contract": "frutlups_planning_state", "version": 2,
                          "outcome": "ready"}).encode("utf-8")
        scenario = self.scenario(states=[bad])
        result = scenario.supervisor.run_until()
        self.assertEqual(result.kind, "refused")
        self.assertEqual(result.detail, "contract_version_refused")
        self.assertEqual(scenario.store.list_escalations("run_001"), ())
        self.assertEqual(scenario.store.list_slices("run_001"), ())

    def test_blocked_stop_cites_named_owner(self):
        states = [
            payload("blocked", None, actor="human",
                    blocked={"citation": "05_governance/decision_log.md",
                             "owner": "Julian"})
        ]
        scenario = self.scenario(states=states)
        result = scenario.supervisor.run_until()
        self.assert_stop(result, StopReason.BLOCKED_VERDICT)
        text = result.escalation_path.read_bytes().decode("utf-8")
        self.assertIn("Julian", text)
        self.assertIn("05_governance/decision_log.md", text)

    def test_override_verdict_stops_as_override_required(self):
        states = [
            payload("blocked", None, actor="human",
                    blocked={"citation": "05_governance/decision_log.md",
                             "owner": "Julian"},
                    verdict={"value": "override", "next_move": "owner decision",
                             "report": REVIEW_REPORT})
        ]
        scenario = self.scenario(states=states)
        self.assert_stop(
            scenario.supervisor.run_until(), StopReason.OVERRIDE_REQUIRED
        )

    def test_complete_reaches_boundary_only_with_evidence(self):
        good = self.scenario(
            states=[payload("complete", None, actor="none", frontier_present=False,
                            completion_evidence={
                                "path": "05_governance/reviews/m001/closure.md"})],
            boundary="milestone_complete",
        )
        result = good.supervisor.run_until()
        self.assertEqual(result.kind, "boundary")

    def test_complete_without_evidence_stops_invalid(self):
        scenario = self.scenario(
            states=[payload("complete", None, actor="none", frontier_present=False)],
            boundary="milestone_complete",
        )
        self.assert_stop(scenario.supervisor.run_until(), StopReason.INVALID_STATE)

    def test_no_frontier_is_never_interpreted(self):
        scenario = self.scenario(
            states=[payload("ready", "no_frontier", actor="orchestrator")]
        )
        self.assert_stop(scenario.supervisor.run_until(), StopReason.INVALID_STATE)

    def test_human_gate_at_frontier_recorded(self):
        scenario = self.scenario(
            states=[payload("ready", "frontier_recorded", actor="human")],
            boundary="milestone_complete",
        )
        self.assert_stop(scenario.supervisor.run_until(), StopReason.HUMAN_GATE)

    def test_kill_switch_stops_before_any_work_and_converges(self):
        from frutlups_drive import killswitch

        scenario = self.scenario(states=clean_pass_states())
        killswitch.request_stop(scenario.store.root)
        result = scenario.supervisor.run_until()
        self.assert_stop(result, StopReason.KILL_SWITCH)
        escalation = result.escalation_path.read_text(encoding="utf-8")
        self.assertIn(
            "resume only when the governing prompt or owner authority permits it",
            escalation,
        )
        self.assertNotIn("artifacts; then resume.\n", escalation)
        again = scenario.supervisor.tick()
        self.assertEqual(again.stop_reason, StopReason.KILL_SWITCH)
        self.assertEqual(len(scenario.store.list_escalations("run_001")), 1)
        self.assertEqual(scenario.store.list_slices("run_001"), ())


class VerificationScenarioTests(ScenarioTestCase):
    def test_verifier_failure_routes_to_repair_before_reviewer(self):
        states = [
            payload("ready", "execute_coding_prompt"),
            payload("ready", "execute_coding_prompt"),
            payload("ready", "make_review_prompt", review_prompt=REVIEW_PROMPT),
            payload("ready", "execute_review_prompt", review_prompt=REVIEW_PROMPT,
                    review_report=REVIEW_REPORT),
        ]
        scenario = self.scenario(
            states=states,
            coder=[CODER_WRITES_REPORT,
                   MockAgentAction(writes=((SELF_REPORT, "# Repaired\n"),))],
            reviewer=[REVIEWER_WRITES_REPORT],
            verbs=DEFAULT_VERBS,
            verifier_exit_codes=[1, 0],
        )
        outcomes = [scenario.supervisor.tick() for _ in range(4)]
        self.assertEqual(outcomes[0].detail, "verification_failed")
        events = scenario.events()
        verifications = [e for e in events if e["kind"] == "verification"]
        self.assertEqual([v["passed"] for v in verifications], [False, True])
        reviewer_positions = [
            i
            for i, e in enumerate(events)
            if e["kind"] == "dispatch" and e["role"] == "reviewer"
        ]
        verification_positions = [
            i for i, e in enumerate(events) if e["kind"] == "verification"
        ]
        self.assertEqual(len(reviewer_positions), 1)
        self.assertGreater(reviewer_positions[0], verification_positions[1])

    def test_review_without_any_coder_attempt_stops_verification_missing(self):
        states = [
            payload("ready", "execute_review_prompt", review_prompt=REVIEW_PROMPT,
                    review_report=REVIEW_REPORT)
        ]
        (Path(self.tmp) / "unused").mkdir(exist_ok=True)
        scenario = self.scenario(states=states)
        (scenario.project / REVIEW_PROMPT).parent.mkdir(parents=True, exist_ok=True)
        (scenario.project / REVIEW_PROMPT).write_bytes(b"# Review prompt\n")
        self.assert_stop(
            scenario.supervisor.run_until(), StopReason.VERIFICATION_MISSING
        )


class EvidenceRevalidationTests(ScenarioTestCase):
    """R1-F3 regressions: reviewer dispatch requires present, intact,
    attempt-bound evidence; the journal boolean alone is routing metadata."""

    def advance_to_review_gate(self):
        scenario = self.scenario(
            states=clean_pass_states(),
            coder=[CODER_WRITES_REPORT],
            reviewer=[REVIEWER_WRITES_REPORT],
            verbs=DEFAULT_VERBS,
        )
        first = scenario.supervisor.tick()   # coder + verification
        second = scenario.supervisor.tick()  # make review prompt
        self.assertEqual(first.detail, "coder_attempt_completed")
        self.assertEqual(second.detail, "verb:make-review-prompt")
        attempt = scenario.store.list_attempts("run_001", "M001-S01")[0]
        return scenario, attempt

    def assert_gate_refuses(self, scenario, *, reason=StopReason.VERIFICATION_MISSING):
        result = scenario.supervisor.tick()
        self.assert_stop(result, reason)
        self.assertFalse(
            (scenario.project / REVIEW_REPORT).exists(),
            "no reviewer work may be spent after evidence refusal",
        )
        dispatches = [
            e
            for e in scenario.events()
            if e["kind"] == "dispatch" and e["role"] == "reviewer"
        ]
        self.assertEqual(dispatches, [])

    def test_deleted_evidence_directory_refuses_reviewer_dispatch(self):
        import shutil

        scenario, attempt = self.advance_to_review_gate()
        shutil.rmtree(attempt / "verification")
        self.assert_gate_refuses(scenario)

    def test_tampered_stream_bytes_refuse_reviewer_dispatch(self):
        scenario, attempt = self.advance_to_review_gate()
        stream = attempt / "verification/cmd_000_stdout.txt"
        stream.write_bytes(b"tampered stream contents\n")
        self.assert_gate_refuses(scenario)

    def test_tampered_evidence_record_refuses_reviewer_dispatch(self):
        scenario, attempt = self.advance_to_review_gate()
        evidence = attempt / "verification/evidence.toml"
        text = evidence.read_bytes().decode("utf-8")
        evidence.write_bytes(
            text.replace("passed = true", "passed = true # forged").encode("utf-8")
        )
        self.assert_gate_refuses(scenario)

    def test_malformed_evidence_record_refuses_reviewer_dispatch(self):
        scenario, attempt = self.advance_to_review_gate()
        (attempt / "verification/evidence.toml").write_bytes(b"passed = [broken\n")
        self.assert_gate_refuses(scenario)

    def test_missing_stream_file_refuses_reviewer_dispatch(self):
        scenario, attempt = self.advance_to_review_gate()
        (attempt / "verification/cmd_000_stderr.txt").unlink()
        self.assert_gate_refuses(scenario)

    def test_extra_planted_member_refuses_reviewer_dispatch(self):
        scenario, attempt = self.advance_to_review_gate()
        (attempt / "verification/extra_authority.toml").write_bytes(
            b"passed = true\n"
        )
        self.assert_gate_refuses(scenario)

    def test_planted_directory_with_payload_refuses_reviewer_dispatch(self):
        # R2-F1 reviewer-literal probe: evidence first proves intact, then an
        # undeclared directory containing a payload is planted. The complete
        # immediate member inventory, not a file filter, must refuse it.
        scenario, attempt = self.advance_to_review_gate()
        planted = attempt / "verification/planted_authority"
        planted.mkdir()
        (planted / "payload.toml").write_bytes(b"passed = true\n")
        self.assert_gate_refuses(scenario)

    def test_planted_directory_still_refuses_after_resume(self):
        from _scenario import DEFAULT_VERBS as verbs
        from _scenario import Scenario, clean_pass_states

        scenario, attempt = self.advance_to_review_gate()
        planted = attempt / "verification/planted_authority"
        planted.mkdir()
        (planted / "payload.toml").write_bytes(b"passed = true\n")
        resumed = Scenario(
            self.tmp,
            project=scenario.project,
            states=clean_pass_states(),
            coder=[CODER_WRITES_REPORT],
            reviewer=[REVIEWER_WRITES_REPORT],
            verbs=verbs,
        )
        self.assertIsNone(resumed.supervisor.resume())
        self.assert_gate_refuses(resumed)

    def test_link_like_stream_member_refuses_reviewer_dispatch(self):
        # A stream name whose no-follow type is a link (pointing at
        # byte-identical content, so every hash still matches) must refuse.
        scenario, attempt = self.advance_to_review_gate()
        stream = attempt / "verification/cmd_000_stderr.txt"
        original = stream.read_bytes()
        twin = attempt / "outside_twin.bin"
        twin.write_bytes(original)
        stream.unlink()
        try:
            stream.symlink_to(twin)
        except OSError as error:
            stream.write_bytes(original)
            self.skipTest(
                "symlink creation unavailable on this host: "
                f"{getattr(error, 'winerror', None) or error}"
            )
        self.assert_gate_refuses(scenario)

    def test_attempt_substituted_evidence_refuses_reviewer_dispatch(self):
        import shutil

        scenario, attempt = self.advance_to_review_gate()
        # Substitute the whole evidence directory with one generated for a
        # different attempt of the same shape.
        other = scenario.store.create_attempt("run_001", "M001-S01")
        scenario.runner.exit_codes.append(0)
        from frutlups_drive.verifier import (
            VerificationCommand,
            VerificationPlan,
            Verifier,
        )

        Verifier(scenario.store, scenario.runner, scenario.clock).verify(
            other,
            VerificationPlan(commands=(VerificationCommand(argv=("fake-validate",)),)),
            scenario.project,
        )
        shutil.rmtree(attempt / "verification")
        shutil.copytree(other / "verification", attempt / "verification")
        self.assert_gate_refuses(scenario)

    def test_intact_evidence_still_admits_reviewer(self):
        scenario, _ = self.advance_to_review_gate()
        result = scenario.supervisor.tick()
        self.assertEqual(result.detail, "reviewer_attempt_completed")


class ProviderAndTimeoutScenarioTests(ScenarioTestCase):
    def test_watch_timeout_then_provider_failure_stop(self):
        states = [payload("ready", "execute_coding_prompt")]
        scenario = self.scenario(
            states=states,
            coder=[MockAgentAction(
                writes=(), produced_override=(), cost_usd=0.0
            )],
            policy_body="[limits]\nmax_consecutive_provider_failures = 1\n",
            watch_timeout=2.0,
        )
        first = scenario.supervisor.tick()
        self.assertEqual(first.detail, "watch_timeout")
        events = scenario.store.read_events("run_001")
        cost_facts = [
            event for event in events
            if event["kind"] == "collected"
            and event["attempt"] == "attempt_001"
        ]
        self.assertEqual(len(cost_facts), 1)
        self.assertEqual(cost_facts[0]["cost_usd"], 0.0)
        attempt = scenario.store.list_attempts("run_001", "M001-S01")[0]
        self.assertEqual(scenario.store.read_result(attempt)["cost_usd"], 0.0)
        second = scenario.supervisor.tick()
        self.assert_stop(second, StopReason.PROVIDER_FAILURE)

    def test_executor_error_counts_as_provider_failure(self):
        states = [payload("ready", "execute_coding_prompt")]
        scenario = self.scenario(
            states=states,
            coder=[MockAgentAction(raise_error=True)],
            policy_body="[limits]\nmax_consecutive_provider_failures = 1\n",
        )
        first = scenario.supervisor.tick()
        self.assertEqual(first.detail, "provider_failure")
        self.assert_stop(scenario.supervisor.tick(), StopReason.PROVIDER_FAILURE)


class BudgetScenarioTests(ScenarioTestCase):
    def test_wall_clock_budget_stop(self):
        scenario = self.scenario(
            states=clean_pass_states(),
            policy_body="[limits]\nmax_wall_clock_minutes = 1\n",
        )
        scenario.clock.advance(61)
        self.assert_stop(
            scenario.supervisor.run_until(), StopReason.BUDGET_EXHAUSTED
        )

    def test_cost_above_authorization_refuses_before_any_write(self):
        # R1-F5: with a 0.0 ceiling, a positive scripted cost is refused
        # before the first mock write, as a budget stop — never a provider
        # failure and never a post-spend detection.
        states = [payload("ready", "execute_coding_prompt")]
        scenario = self.scenario(
            states=states,
            coder=[MockAgentAction(
                writes=((SELF_REPORT, "# report\n"),), cost_usd=0.75)],
        )
        result = scenario.supervisor.tick()
        self.assert_stop(result, StopReason.BUDGET_EXHAUSTED)
        self.assertIn("cost_authorization", result.detail)
        self.assertFalse((scenario.project / SELF_REPORT).exists())
        event = next(
            event
            for event in scenario.events()
            if event["kind"] == "ladder_event"
        )
        self.assertEqual(
            (event["failure_class"], event["counted"]),
            ("environment", False),
        )

    def test_exact_positive_ceiling_stops_before_next_dispatch(self):
        # R1-F5: accumulated == maximum (> 0) is exhaustion, not headroom.
        states = [
            payload("ready", "execute_coding_prompt"),
            payload("ready", "execute_coding_prompt", round_=2),
        ]
        scenario = self.scenario(
            states=states,
            coder=[MockAgentAction(
                writes=((SELF_REPORT, "# report\n"),), cost_usd=1.0)],
            policy_body="[limits]\nmax_total_cost_usd = 1.0\n",
        )
        first = scenario.supervisor.tick()
        self.assertEqual(first.detail, "coder_attempt_completed")
        result = scenario.supervisor.tick()
        self.assert_stop(result, StopReason.BUDGET_EXHAUSTED)
        self.assertIn("total_cost", result.detail)

    def test_next_request_is_authorized_only_for_the_remaining_total(self):
        states = [
            payload("ready", "execute_coding_prompt"),
            payload("ready", "execute_coding_prompt", round_=2),
            payload("ready", "make_review_prompt", review_prompt=REVIEW_PROMPT),
        ]
        scenario = self.scenario(
            states=states,
            coder=[
                MockAgentAction(writes=((SELF_REPORT, "# one\n"),), cost_usd=0.75),
                MockAgentAction(writes=((SELF_REPORT, "# two\n"),), cost_usd=0.25),
            ],
            policy_body="[limits]\nmax_total_cost_usd = 1.0\n",
        )
        first = scenario.supervisor.tick()
        second = scenario.supervisor.tick()
        self.assertEqual(first.detail, "coder_attempt_completed")
        self.assertEqual(second.detail, "coder_attempt_completed")
        attempts = scenario.store.list_attempts("run_001", "M001-S01")
        second_request = scenario.store.read_request(attempts[1])
        self.assertAlmostEqual(second_request["max_cost_usd"], 0.25)
        # the ceiling is now exactly reached; the next tick stops
        result = scenario.supervisor.tick()
        self.assert_stop(result, StopReason.BUDGET_EXHAUSTED)

    def test_cost_beyond_remaining_refuses_before_any_write(self):
        states = [
            payload("ready", "execute_coding_prompt"),
            payload("ready", "execute_coding_prompt", round_=2),
        ]
        scenario = self.scenario(
            states=states,
            coder=[
                MockAgentAction(writes=((SELF_REPORT, "# one\n"),), cost_usd=0.75),
                MockAgentAction(writes=((SELF_REPORT, "# two\n"),), cost_usd=0.26),
            ],
            policy_body="[limits]\nmax_total_cost_usd = 1.0\n",
        )
        first = scenario.supervisor.tick()
        self.assertEqual(first.detail, "coder_attempt_completed")
        result = scenario.supervisor.tick()
        self.assert_stop(result, StopReason.BUDGET_EXHAUSTED)
        self.assertIn("cost_authorization", result.detail)
        self.assertEqual(
            (scenario.project / SELF_REPORT).read_bytes(), b"# one\n",
            "the over-budget action must write nothing",
        )

    def test_costs_accumulate_across_roles_into_remaining_authorization(self):
        # R2-F2: valid facts from different roles reduce the same remaining
        # authorization; each later request carries only what is left.
        states = clean_pass_states()
        scenario = self.scenario(
            states=states,
            coder=[MockAgentAction(
                writes=((SELF_REPORT, "# report\n"),), cost_usd=0.4)],
            reviewer=[MockAgentAction(
                writes=((REVIEW_REPORT, "Verdict: pass — next: record\n"),),
                cost_usd=0.35)],
            verbs=DEFAULT_VERBS,
            policy_body="[limits]\nmax_total_cost_usd = 1.0\n",
        )
        result = scenario.supervisor.run_until()
        self.assertEqual(result.kind, "boundary")
        attempts = scenario.store.list_attempts("run_001", "M001-S01")
        requests = [scenario.store.read_request(a) for a in attempts]
        by_role = {r["role"]: r for r in requests if r}
        self.assertAlmostEqual(by_role["coder"]["max_cost_usd"], 1.0)
        self.assertAlmostEqual(by_role["reviewer"]["max_cost_usd"], 0.6)
        self.assertAlmostEqual(scenario.counters().total_cost_usd, 0.75)

    def test_attempt_budget_stop(self):
        states = [
            payload("ready", "execute_coding_prompt"),
            payload("ready", "execute_coding_prompt"),
        ]
        scenario = self.scenario(
            states=states,
            coder=[CODER_WRITES_REPORT],
            policy_body="[limits]\nmax_coder_attempts_per_slice = 1\n",
            verifier_exit_codes=[1],
        )
        first = scenario.supervisor.tick()
        self.assertEqual(first.detail, "verification_failed")
        result = scenario.supervisor.tick()
        self.assert_stop(result, StopReason.BUDGET_EXHAUSTED)
        self.assertIn("coder_attempts", result.detail)

    def test_slice_budget_stop(self):
        states = [
            payload("ready", "frontier_recorded"),
            payload("ready", "frontier_recorded", slice_id="M001-S02"),
        ]
        scenario = self.scenario(
            states=states,
            boundary="milestone_complete",
            policy_body=(
                "[target]\nmax_slices = 1\n"
                "[autonomy]\nauto_continue_past_frontier_recorded = true\n"
            ),
        )
        first = scenario.supervisor.tick()
        self.assertEqual(first.detail, "continue_past_frontier")
        result = scenario.supervisor.tick()
        self.assert_stop(result, StopReason.BUDGET_EXHAUSTED)
        self.assertIn("slices", result.detail)

    def test_milestone_rollover_no_longer_counts_as_a_pass(self):
        states = [
            payload("ready", "frontier_recorded", milestone="M001"),
            payload("ready", "frontier_recorded", milestone="M002",
                    slice_id="M002-S01"),
            payload("ready", "frontier_recorded", milestone="M002",
                    slice_id="M002-S02"),
        ]
        scenario = self.scenario(
            states=states,
            boundary="milestone_complete",
            policy_body=(
                "[target]\nmax_passes = 1\nmax_slices = 10\n"
                "[autonomy]\nauto_continue_past_frontier_recorded = true\n"
            ),
        )
        first = scenario.supervisor.tick()
        second = scenario.supervisor.tick()
        self.assertEqual((first.kind, second.kind), ("acted", "acted"))
        result = scenario.supervisor.tick()
        self.assertEqual(result.kind, "acted")
        self.assertEqual(scenario.counters().passes_completed, 0)


class LadderScenarioTests(ScenarioTestCase):
    def test_excluded_failure_classes_do_not_advance_product_recurrence(self):
        for failure_class in (
            LadderFailureClass.TRANSPORT,
            LadderFailureClass.ENVIRONMENT,
            LadderFailureClass.PATH_CONTRACT,
            LadderFailureClass.OPERATOR_KILL_SWITCH,
        ):
            with self.subTest(failure_class=failure_class.value):
                scenario = Scenario(
                    self.tmp / failure_class.value,
                    states=[payload("ready", "execute_coding_prompt", round_=3)],
                    coder=[CODER_WRITES_REPORT],
                )
                scenario.supervisor._journal(
                    "ladder_event",
                    slice="M001-S01",
                    attempt="attempt_prior",
                    failure_class=failure_class.value,
                    recurrence_key=failure_class.value,
                    counted=False,
                )

                result = scenario.supervisor.tick()

                self.assertEqual(
                    (result.kind, result.detail),
                    ("acted", "coder_attempt_completed"),
                )
                events = [
                    event
                    for event in scenario.events()
                    if event["kind"] == "ladder_event"
                ]
                self.assertEqual(
                    (
                        events[0]["failure_class"],
                        events[0]["recurrence_key"],
                        events[0]["counted"],
                    ),
                    (failure_class.value, failure_class.value, False),
                )

    def test_same_product_invariant_still_stops_at_round_three(self):
        scenario = self.scenario(
            states=[payload("ready", "execute_coding_prompt", round_=1)]
        )
        for number in (1, 2):
            scenario.supervisor._journal(
                "ladder_event",
                slice="M001-S01",
                attempt=f"attempt_{number:03d}",
                failure_class=LadderFailureClass.PRODUCT_FINDING.value,
                recurrence_key="product_finding:M001-S01",
                counted=True,
            )

        result = scenario.supervisor.tick()

        self.assert_stop(result, StopReason.LADDER_ROUND3)

    def test_stream_overflow_is_journaled_as_excluded_transport(self):
        scenario = self.scenario(
            states=[payload("ready", "execute_coding_prompt")],
            coder=[
                MockAgentAction(
                    status="failed", exit_reason="agent_stream_overflow"
                )
            ],
        )

        result = scenario.supervisor.tick()

        self.assertEqual((result.kind, result.detail), ("acted", "attempt_failed"))
        event = next(
            event
            for event in scenario.events()
            if event["kind"] == "ladder_event"
        )
        self.assertEqual(
            {
                key: event[key]
                for key in (
                    "failure_class",
                    "recurrence_key",
                    "counted",
                )
            },
            {
                "failure_class": "transport",
                "recurrence_key": "transport",
                "counted": False,
            },
        )

    def test_accepted_history_does_not_consume_fresh_rework_ladder_rounds(self):
        project = build_project(self.tmp)
        store = RunStore(project / ".frutlups_drive")
        store.create_run(
            "run_001", {"boundary": "slice_complete", "contract_version": 1}
        )
        coding_bytes = (project / CODING_PROMPT).read_bytes()
        boundary = store.write_pass_boundary(
            "run_001",
            {
                "contract_version": 1,
                "run_id": "run_001",
                "evidence": [],
                "artifacts": [
                    {
                        "path": CODING_PROMPT,
                        "sha256": hashlib.sha256(coding_bytes).hexdigest(),
                    }
                ],
            },
        )
        for event in (
            {"kind": "run_created", "t": 900.0, "boundary": "slice_complete"},
            {"kind": "dispatch", "t": 901.0, "role": "coder",
             "slice": "M001-S01", "repair": False},
            {"kind": "collected", "t": 902.0, "role": "coder",
             "slice": "M001-S01", "status": "completed", "cost_usd": None},
            {"kind": "verb", "t": 903.0, "verb": "record-verdict",
             "slice": "M001-S01", "artifact": "accepted_verdict.md"},
            {"kind": "pass_boundary", "t": 903.5,
             "evidence_sha256": hashlib.sha256(boundary.read_bytes()).hexdigest(),
             "evidence_members": 0, "artifact_members": 1},
            {"kind": "verb", "t": 904.0, "verb": "declare-rework",
             "slice": "", "pass_id": "holistic_pass_001",
             "slices": ["M001-S01"]},
        ):
            store.append_event("run_001", event)

        scenario = self.scenario(
            project=project,
            states=[
                payload("ready", "execute_coding_prompt", round_=1),
                payload("ready", "execute_coding_prompt", round_=2),
            ],
            coder=[CODER_WRITES_REPORT, CODER_WRITES_REPORT],
            role_efforts={"coder": ("medium", "high")},
        )

        first = scenario.supervisor.tick()
        second = scenario.supervisor.tick()
        self.assertEqual((first.kind, first.detail), ("acted", "coder_attempt_completed"))
        self.assertEqual(
            (second.kind, second.detail),
            ("acted", "coder_attempt_completed"),
            scenario.events(),
        )
        counters = scenario.counters()
        self.assertEqual(counters.lifecycle_coder_collected_for("M001-S01"), 2)
        self.assertEqual(counters.coder_collected_for("M001-S01"), 3)
        self.assertEqual(counters.coder_dispatches_for("M001-S01"), 3)
        fresh_dispatches = [
            event for event in scenario.events()
            if event.get("kind") == "dispatch" and event.get("t", 0) > 904.0
        ]
        self.assertEqual(
            [event["effort"] for event in fresh_dispatches],
            ["medium", "high"],
        )

    def test_ladder_round3_stop(self):
        states = [payload("ready", "execute_coding_prompt", round_=3)]
        scenario = self.scenario(states=states, coder=[CODER_WRITES_REPORT])
        self.assert_stop(scenario.supervisor.run_until(), StopReason.LADDER_ROUND3)

    def test_ladder_round3_escalation_carries_chain_and_mandatory_fork(self):
        project = build_project(self.tmp)
        store = RunStore(project / ".frutlups_drive")
        store.create_run(
            "run_001", {"boundary": "slice_complete", "contract_version": 1}
        )
        for event in (
            {"kind": "run_created", "t": 1.0, "boundary": "slice_complete"},
            {"kind": "dispatch", "t": 2.0, "role": "coder",
             "slice": "M001-S01", "attempt": "attempt_001", "repair": False,
             "adapter": "codex_cli", "model": "gpt-5.6-sol",
             "effort": "medium", "prompt_source": CODING_PROMPT},
            {"kind": "collected", "t": 3.0, "role": "coder",
             "slice": "M001-S01", "status": "completed", "cost_usd": None},
            {"kind": "dispatch", "t": 4.0, "role": "reviewer",
             "slice": "M001-S01", "attempt": "attempt_002", "repair": False,
             "adapter": "kimi_cli", "model": "kimi-code/k3",
             "effort": "high", "prompt_source": REVIEW_PROMPT},
            {"kind": "dispatch", "t": 5.0, "role": "coder",
             "slice": "M001-S01", "attempt": "attempt_003", "repair": False,
             "adapter": "codex_cli", "model": "gpt-5.6-sol",
             "effort": "high", "prompt_source": CODING_PROMPT},
            {"kind": "collected", "t": 6.0, "role": "coder",
             "slice": "M001-S01", "status": "completed", "cost_usd": None},
        ):
            store.append_event("run_001", event)
        state = payload(
            "ready", "execute_coding_prompt", round_=3,
            review_prompt=REVIEW_PROMPT, review_report=REVIEW_REPORT,
            verdict={"value": "needs_work", "next_move": "repair",
                     "report": REVIEW_REPORT},
        )
        scenario = self.scenario(project=project, states=[state])
        result = scenario.supervisor.run_until()
        self.assert_stop(result, StopReason.LADDER_ROUND3)
        text = result.escalation_path.read_text(encoding="utf-8")
        for expected in (
            "Active Lifecycle Chain",
            "round 1",
            "round 2",
            "codex_cli/gpt-5.6-sol effort=medium",
            "codex_cli/gpt-5.6-sol effort=high",
            "kimi_cli/kimi-code/k3 effort=high",
            "verdict.value=needs_work",
            CODING_PROMPT,
            REVIEW_PROMPT,
            REVIEW_REPORT,
            "product-plane code defects",
            "envelope-expansion change control",
            "evidence/documentation-plane issues",
            "before any resume",
        ):
            self.assertIn(expected, text)

    def test_non_ladder_escalation_has_no_reassessment_fork(self):
        scenario = self.scenario(states=[payload("invalid", None)])
        result = scenario.supervisor.run_until()
        self.assert_stop(result, StopReason.INVALID_STATE)
        text = result.escalation_path.read_text(encoding="utf-8")
        self.assertNotIn("Active Lifecycle Chain", text)
        self.assertNotIn("envelope-expansion change control", text)

    def test_ladder_round4_unauthorized(self):
        states = [payload("ready", "execute_coding_prompt", round_=4)]
        scenario = self.scenario(states=states)
        self.assert_stop(
            scenario.supervisor.run_until(),
            StopReason.LADDER_ROUND4_UNAUTHORIZED,
        )

    def test_ladder_round4_with_injected_authority_dispatches(self):
        states = [payload("ready", "execute_coding_prompt", round_=4)]
        scenario = self.scenario(
            states=states,
            coder=[CODER_WRITES_REPORT],
            round4_authority=lambda: True,
        )
        result = scenario.supervisor.tick()
        self.assertEqual(result.kind, "acted")
        self.assertIn("dispatch", scenario.event_kinds())


class ReconciliationScenarioTests(ScenarioTestCase):
    NEEDS_SPEC = payload(
        "needs_specification", None, actor="architect", frontier_present=False,
        diagnostics=({"severity": "error", "code": "underspecified",
                      "message": "the frontier needs specification"},),
    )

    def test_reconciliation_with_progress_continues(self):
        states = [self.NEEDS_SPEC, payload("ready", "execute_coding_prompt")]
        scenario = self.scenario(
            states=states,
            architect=[MockAgentAction(
                writes=((
                    "roadmap_proposal.md",
                    ROADMAP_BODY.replace(
                        "Implement the fixture behavior.",
                        "Sharpen the fixture behavior.",
                    ),
                ),))],
            coder=[CODER_WRITES_REPORT],
        )
        first = scenario.supervisor.tick()
        self.assertEqual(first.detail, "reconciliation")
        reconciliations = [
            e for e in scenario.events() if e["kind"] == "reconciliation"
        ]
        self.assertTrue(reconciliations[0]["progress"])

    def test_staged_reconciliation_prompt_pins_structure_preservation_guidance(self):
        scenario = self.scenario(
            states=[self.NEEDS_SPEC, payload("ready", "execute_coding_prompt")],
            architect=[MockAgentAction(
                writes=((
                    "roadmap_proposal.md",
                    ROADMAP_BODY.replace(
                        "Implement the fixture behavior.",
                        "Sharpen the fixture behavior.",
                    ),
                ),)
            )],
            coder=[CODER_WRITES_REPORT],
        )

        result = scenario.supervisor.tick()

        self.assertEqual(result.detail, "reconciliation")
        attempt = scenario.store.list_attempts("run_001", "M001-S01")[0]
        prompt = (attempt / "reconciliation_prompt.md").read_text(
            encoding="utf-8"
        )
        for sentence in (
            "The current roadmap structure is authoritative even where it "
            "appears anomalous.",
            "Complete the one existing untitled slice line at its exact "
            "current location.",
            "Reproduce every other line byte-identically, including empty or "
            "unusually placed fields.",
            "Never relocate, normalize, or repair structure; a proposal that "
            "fixes layout will be refused.",
        ):
            self.assertIn(sentence, prompt)

    def test_no_progress_reconciliation_stops(self):
        states = [self.NEEDS_SPEC]
        scenario = self.scenario(
            states=states,
            architect=[MockAgentAction(
                writes=(("roadmap_proposal.md", ROADMAP_BODY),)
            )],
        )
        result = scenario.supervisor.run_until()
        self.assert_stop(result, StopReason.NO_PROGRESS)
        reconciliations = [
            e for e in scenario.events() if e["kind"] == "reconciliation"
        ]
        self.assertEqual([r["progress"] for r in reconciliations], [False])
        dispatches = [
            e for e in scenario.events()
            if e["kind"] == "dispatch" and e["role"] == "architect"
        ]
        self.assertEqual(len(dispatches), 1)

    def test_guard_refusal_retries_once_with_feedback_and_completes(self):
        refused = ROADMAP_BODY.replace("Status: active", "Status: planned", 1)
        compliant = ROADMAP_BODY.replace(
            "Implement the fixture behavior.",
            "Sharpen the fixture behavior.",
        )
        scenario = self.scenario(
            states=[self.NEEDS_SPEC, payload("ready", "execute_coding_prompt")],
            architect=[
                MockAgentAction(writes=(("roadmap_proposal.md", refused),)),
                MockAgentAction(writes=(("roadmap_proposal.md", compliant),)),
            ],
        )

        result = scenario.supervisor.tick()

        self.assertEqual(result.detail, "reconciliation")
        attempts = scenario.store.list_attempts("run_001", "M001-S01")
        self.assertEqual(len(attempts), 2)
        first_prompt = (attempts[0] / "reconciliation_prompt.md").read_bytes()
        second_prompt = (attempts[1] / "reconciliation_prompt.md").read_bytes()
        self.assertNotIn(b"Guard Refusal Feedback", first_prompt)
        self.assertTrue(second_prompt.startswith(first_prompt))
        for expected in (
            b"## Guard Refusal Feedback",
            b"protected_field_changed",
            b"Editable fields:",
            b"Protected regions:",
            b"In-place rule:",
        ):
            self.assertIn(expected, second_prompt)
        dispatches = [
            e for e in scenario.events()
            if e["kind"] == "dispatch" and e["role"] == "architect"
        ]
        self.assertEqual(len(dispatches), 2)

    def test_second_guard_refusal_stops_at_cap_with_v1_shape(self):
        refused = ROADMAP_BODY.replace("Status: active", "Status: planned", 1)
        scenario = self.scenario(
            states=[self.NEEDS_SPEC],
            architect=[
                MockAgentAction(writes=(("roadmap_proposal.md", refused),)),
                MockAgentAction(writes=(("roadmap_proposal.md", refused),)),
            ],
        )

        result = scenario.supervisor.tick()

        self.assert_stop(result, StopReason.PATH_VIOLATION)
        self.assertIn("reconciliation proposal refused", result.detail)
        attempts = scenario.store.list_attempts("run_001", "M001-S01")
        self.assertEqual(len(attempts), 2)
        fences = [e for e in scenario.events() if e["kind"] == "fence"]
        self.assertEqual(len(fences), 2)

    def test_reconciliation_outside_scope_is_a_path_violation(self):
        states = [self.NEEDS_SPEC]
        scenario = self.scenario(
            states=states,
            architect=[MockAgentAction(
                writes=(("PROJECT_STATE.md", "tampered\n"),))],
        )
        self.assert_stop(
            scenario.supervisor.run_until(), StopReason.PATH_VIOLATION
        )


if __name__ == "__main__":
    unittest.main()
