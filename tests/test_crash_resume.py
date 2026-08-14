"""Crash/resume matrix: kill at every transition marker, resume, and compare
with the uninterrupted run. Bounded claim: at-most-once collection/payment
loss — a crash before any external effect legitimately redispatches a fresh
unique attempt; an externally completed attempt is collected, never re-paid."""

import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

from frutlups_drive.contracts import StopReason
from frutlups_drive.dispatch.mock import MockAgentAction
from frutlups_drive.workspace import WorkspaceManager

from _scenario import (
    DEFAULT_VERBS,
    REVIEW_PROMPT,
    REVIEW_PROMPT_2,
    REVIEW_REPORT,
    REVIEW_REPORT_2,
    SELF_REPORT,
    VERDICT_RECORD,
    Scenario,
    clean_pass_states,
    payload,
)

TRANSITIONS = (
    "planned",
    "started",
    "externally_completed",
    "collected",
    "validated",
    "closed",
)


class SimulatedCrash(Exception):
    pass


def crash_hook(state_name, occurrence=1):
    seen = {"count": 0}

    def hook(state, attempt_dir):
        if state == state_name:
            seen["count"] += 1
            if seen["count"] == occurrence:
                raise SimulatedCrash(state)

    return hook


def clean_pass_kwargs():
    return dict(
        states=clean_pass_states(),
        coder=[MockAgentAction(writes=((SELF_REPORT, "# Coder Self-Report\n"),))],
        reviewer=[
            MockAgentAction(
                writes=((REVIEW_REPORT, "Verdict: pass — next: record\n"),)
            )
        ],
        verbs=DEFAULT_VERBS,
    )


def repair_pass_kwargs():
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
    return dict(
        states=states,
        coder=[
            MockAgentAction(writes=((SELF_REPORT, "# Coder Self-Report\n"),)),
            MockAgentAction(writes=((SELF_REPORT, "# Repaired report\n"),)),
        ],
        reviewer=[
            MockAgentAction(
                writes=((REVIEW_REPORT, "Verdict: needs_work — next: repair\n"),)
            ),
            MockAgentAction(
                writes=((REVIEW_REPORT_2, "Verdict: pass — next: record\n"),)
            ),
        ],
        verbs=verbs,
    )


def outcome_record(scenario, result):
    events = scenario.events()
    collected = [e["attempt"] for e in events if e["kind"] == "collected"]
    verifications = [e for e in events if e["kind"] == "verification"]
    manager = WorkspaceManager(scenario.project, scenario.store.root)
    project_snapshot = manager.snapshot(scenario.project)
    return {
        "final": (
            result.kind,
            result.stop_reason.value
            if result.stop_reason is not None
            else result.detail,
        ),
        "escalations": [
            p.name for p in scenario.store.list_escalations("run_001")
        ],
        "project_files": project_snapshot,
        "verifications_passed": sum(1 for v in verifications if v["passed"]),
        "collected_unique": len(collected) == len(set(collected)),
        "slices_completed": scenario.counters().slices_completed,
    }


class CrashMatrixTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def new_root(self, label):
        root = Path(self._tmp.name) / label
        root.mkdir()
        return root

    def run_uninterrupted(self, kwargs_factory):
        root = self.new_root("baseline")
        scenario = Scenario(root, **kwargs_factory())
        result = scenario.supervisor.run_until()
        return outcome_record(scenario, result)

    def run_with_crash(self, kwargs_factory, transition, label, occurrence=1):
        root = self.new_root(label)
        crashed = Scenario(
            root,
            transition_hook=crash_hook(transition, occurrence),
            **kwargs_factory(),
        )
        crash_seen = False
        try:
            result = crashed.supervisor.run_until()
        except SimulatedCrash:
            crash_seen = True
        if not crash_seen:
            # marker never fired in this scenario shape; treat baseline result
            return outcome_record(crashed, result), False
        resumed = Scenario(
            root, project=crashed.project, **kwargs_factory()
        )
        stop = resumed.supervisor.resume()
        result = stop if stop is not None else resumed.supervisor.run_until()
        return outcome_record(resumed, result), True

    def assert_equivalent(self, baseline, resumed, context):
        for key in (
            "final",
            "escalations",
            "project_files",
            "verifications_passed",
            "slices_completed",
        ):
            self.assertEqual(
                resumed[key], baseline[key], f"{context}: mismatch in {key}"
            )
        self.assertTrue(resumed["collected_unique"], f"{context}: double collection")


class CleanPassCrashMatrixTests(CrashMatrixTestCase):
    def test_crash_and_resume_matches_uninterrupted_at_every_marker(self):
        baseline = self.run_uninterrupted(clean_pass_kwargs)
        self.assertEqual(baseline["final"], ("boundary", "slice_complete"))
        fired = []
        for transition in ("planned", "started", "collected", "validated", "closed"):
            with self.subTest(transition=transition):
                resumed, crash_seen = self.run_with_crash(
                    clean_pass_kwargs, transition, f"crash_{transition}"
                )
                self.assertTrue(crash_seen, f"{transition} marker never fired")
                fired.append(transition)
                self.assert_equivalent(baseline, resumed, transition)

    def _plant_external_completion(self, scenario):
        # The dispatched agent finished while the runner was dead: the
        # expected artifact appears with exactly the bytes the scripted mock
        # would have produced.
        report = scenario.project / SELF_REPORT
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_bytes(b"# Coder Self-Report\n")

    def test_externally_completed_crash_via_double_crash(self):
        baseline = self.run_uninterrupted(clean_pass_kwargs)
        root = self.new_root("double_crash")
        first = Scenario(
            root, transition_hook=crash_hook("started"), **clean_pass_kwargs()
        )
        with self.assertRaises(SimulatedCrash):
            first.supervisor.run_until()
        self._plant_external_completion(first)
        # second process crashes exactly at the externally_completed marker
        second = Scenario(
            root,
            project=first.project,
            transition_hook=crash_hook("externally_completed"),
            **clean_pass_kwargs(),
        )
        with self.assertRaises(SimulatedCrash):
            second.supervisor.resume()
        third = Scenario(root, project=first.project, **clean_pass_kwargs())
        stop = third.supervisor.resume()
        result = stop if stop is not None else third.supervisor.run_until()
        self.assert_equivalent(
            baseline, outcome_record(third, result), "externally_completed"
        )

    def test_externally_completed_attempt_is_collected_not_repaid(self):
        root = self.new_root("no_repay")
        crashed = Scenario(
            root, transition_hook=crash_hook("started"), **clean_pass_kwargs()
        )
        with self.assertRaises(SimulatedCrash):
            crashed.supervisor.run_until()
        self._plant_external_completion(crashed)
        resumed = Scenario(root, project=crashed.project, **clean_pass_kwargs())
        self.assertIsNone(resumed.supervisor.resume())
        result = resumed.supervisor.run_until()
        self.assertEqual(result.kind, "boundary")
        # the external artifact bound to the same attempt; it was collected,
        # never re-dispatched, and never re-paid
        dispatches = [
            e
            for e in resumed.events()
            if e["kind"] == "dispatch" and e["role"] == "coder"
        ]
        self.assertEqual(len(dispatches), 1)
        reconciled = [
            e for e in resumed.events() if e["kind"] == "reconciled_attempt"
        ]
        self.assertEqual(len(reconciled), 1)
        coder_attempts = [
            a
            for a in resumed.store.list_attempts("run_001", "M001-S01")
            if (resumed.store.read_request(a) or {}).get("role") == "coder"
        ]
        self.assertEqual(len(coder_attempts), 1)


class RepairPassCrashMatrixTests(CrashMatrixTestCase):
    def test_crash_and_resume_matches_uninterrupted_at_every_marker(self):
        baseline = self.run_uninterrupted(repair_pass_kwargs)
        self.assertEqual(baseline["final"], ("boundary", "slice_complete"))
        for transition in ("planned", "started", "collected", "validated", "closed"):
            with self.subTest(transition=transition):
                resumed, crash_seen = self.run_with_crash(
                    repair_pass_kwargs, transition, f"repair_crash_{transition}"
                )
                self.assertTrue(crash_seen, f"{transition} marker never fired")
                self.assert_equivalent(baseline, resumed, transition)

    def test_crash_at_second_attempt_marker_also_recovers(self):
        baseline = self.run_uninterrupted(repair_pass_kwargs)
        resumed, crash_seen = self.run_with_crash(
            repair_pass_kwargs, "started", "second_attempt_crash", occurrence=2
        )
        self.assertTrue(crash_seen)
        self.assert_equivalent(baseline, resumed, "second started")


class CostReplayTests(CrashMatrixTestCase):
    """R1-F5: spent/remaining authorization reconstructs identically after a
    crash, without double charging or reopened authorization."""

    def kwargs(self):
        return dict(
            states=[
                payload("ready", "execute_coding_prompt"),
                payload("ready", "execute_coding_prompt", round_=2),
            ],
            coder=[
                MockAgentAction(writes=((SELF_REPORT, "# one\n"),),
                                cost_usd=0.6),
                MockAgentAction(writes=((SELF_REPORT, "# two\n"),),
                                cost_usd=0.4),
            ],
            policy_body="[limits]\nmax_total_cost_usd = 1.0\n",
        )

    def test_remaining_authorization_survives_crash_and_resume(self):
        root = self.new_root("cost_replay")
        crashed = Scenario(
            root,
            transition_hook=crash_hook("closed"),
            **self.kwargs(),
        )
        with self.assertRaises(SimulatedCrash):
            crashed.supervisor.run_until()
        resumed = Scenario(root, project=crashed.project, **self.kwargs())
        self.assertIsNone(resumed.supervisor.resume())
        self.assertAlmostEqual(resumed.counters().total_cost_usd, 0.6)
        first = resumed.supervisor.tick()   # replays the completed round-1 tick
        second = resumed.supervisor.tick()  # round-2 dispatch
        self.assertEqual(first.detail, "coder_attempt_already_satisfied")
        self.assertEqual(second.detail, "coder_attempt_completed")
        attempts = resumed.store.list_attempts("run_001", "M001-S01")
        second_request = resumed.store.read_request(attempts[1])
        self.assertAlmostEqual(second_request["max_cost_usd"], 0.4)
        self.assertAlmostEqual(resumed.counters().total_cost_usd, 1.0)
        final = resumed.supervisor.tick()
        self.assertEqual(final.stop_reason, StopReason.BUDGET_EXHAUSTED)

    def forge_collected_event(self, scenario, cost):
        scenario.store.append_event(
            "run_001",
            {
                "kind": "collected",
                "t": 999.0,
                "attempt": "forged",
                "role": "coder",
                "slice": "M001-S01",
                "status": "completed",
                "cost_usd": cost,
            },
        )

    def exhausted_then_forged(self, cost, label=None):
        label = label if label is not None else str(cost).replace(".", "_")
        root = self.new_root(f"forged_{label}")
        first = Scenario(root, **self.kwargs())
        r1 = first.supervisor.tick()
        self.assertEqual(r1.detail, "coder_attempt_completed")
        self.forge_collected_event(first, cost)
        resumed = Scenario(root, project=first.project, **self.kwargs())
        self.assertIsNone(resumed.supervisor.resume())
        return resumed

    def assert_fails_closed_without_reopening(self, resumed):
        # R2-F2: replay is monotonic — an invalid durable fact must not
        # decrease spend, reopen authorization, or admit another dispatch.
        result = resumed.supervisor.run_until()
        self.assertEqual(result.stop_reason, StopReason.BUDGET_EXHAUSTED)
        self.assertIn("cost_fact_invalid", result.detail)
        self.assertGreaterEqual(resumed.counters().total_cost_usd, 0.6)
        dispatches = [
            e for e in resumed.events() if e["kind"] == "dispatch"
        ]
        self.assertEqual(len(dispatches), 1, "no dispatch after the forgery")
        self.assertEqual(
            (resumed.project / SELF_REPORT).read_bytes(), b"# one\n"
        )

    def test_negative_durable_cost_fact_fails_closed_on_replay(self):
        self.assert_fails_closed_without_reopening(
            self.exhausted_then_forged(-5.0)
        )

    def test_boolean_durable_cost_fact_fails_closed_on_replay(self):
        self.assert_fails_closed_without_reopening(
            self.exhausted_then_forged(True)
        )

    def test_huge_integer_durable_cost_fact_fails_closed_on_replay(self):
        # R3-F1 reviewer-literal probe: a 401-digit integer is admitted by
        # the journal's JSON parser; replay must fail closed through the
        # owned decision — never abort resume with a raw OverflowError,
        # never reduce spend, never admit another dispatch.
        self.assert_fails_closed_without_reopening(
            self.exhausted_then_forged(10**400, label="huge_401_digit")
        )


class StopIdempotencyAndStaleTests(CrashMatrixTestCase):
    def test_blocked_stop_is_idempotent_across_resume(self):
        blocked_kwargs = dict(
            states=[
                payload("blocked", None, actor="human",
                        blocked={"citation": "05_governance/decision_log.md",
                                 "owner": "Julian"})
            ]
        )
        root = self.new_root("blocked")
        scenario = Scenario(root, **blocked_kwargs)
        first = scenario.supervisor.run_until()
        self.assertEqual(first.stop_reason, StopReason.BLOCKED_VERDICT)
        resumed = Scenario(root, project=scenario.project, **blocked_kwargs)
        self.assertIsNone(resumed.supervisor.resume())
        second = resumed.supervisor.run_until()
        self.assertEqual(second.stop_reason, StopReason.BLOCKED_VERDICT)
        self.assertEqual(
            len(resumed.store.list_escalations("run_001")), 1,
            "resume must converge on the existing escalation artifact",
        )

    def test_stale_prompt_hash_refuses_collection_and_preserves_evidence(self):
        root = self.new_root("stale")
        crashed = Scenario(
            root, transition_hook=crash_hook("started"), **clean_pass_kwargs()
        )
        with self.assertRaises(SimulatedCrash):
            crashed.supervisor.run_until()
        report = crashed.project / SELF_REPORT
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_bytes(b"# Externally produced report\n")
        prompt = crashed.project / "prompts/for_coding_agent/001_m001_s01_fix.md"
        original_report = report.read_bytes()
        prompt.write_bytes(b"# A newer prompt the output cannot bind to\n")
        resumed = Scenario(root, project=crashed.project, **clean_pass_kwargs())
        stop = resumed.supervisor.resume()
        self.assertIsNotNone(stop)
        self.assertEqual(stop.stop_reason, StopReason.INVALID_STATE)
        self.assertIn("stale prompt hash", stop.detail)
        # ambiguous evidence is preserved, never deleted
        self.assertEqual(
            (crashed.project / SELF_REPORT).read_bytes(), original_report
        )
        attempt = resumed.store.list_attempts("run_001", "M001-S01")[0]
        self.assertTrue((attempt / "request.json").is_file())


if __name__ == "__main__":
    unittest.main()
