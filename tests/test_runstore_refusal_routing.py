"""M007-S03 refusal routing and resume-aware lifecycle regressions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

from frutlups_drive.contracts import (
    AgentRunRequest,
    AgentRunResult,
    StopReason,
)
from frutlups_drive.dispatch.mock import MockAgentAction
from frutlups_drive.runstore import RunStoreRefusal

from _scenario import (
    DEFAULT_VERBS,
    REVIEW_PROMPT,
    SELF_REPORT,
    Scenario,
    payload,
)


FABRICATED_REVIEW = (
    "05_governance/reviews/m001/fabricated_review_report.md"
)


class _DirectExecutor:
    """External-seat timing: effects are inspected after collection."""

    def __init__(self, log_dir: Path):
        self.log_dir = log_dir

    def execute(self, request: AgentRunRequest) -> AgentRunResult:
        writes = (
            (SELF_REPORT, "# Coder Self-Report\n"),
            (FABRICATED_REVIEW, "Verdict: pass\n"),
        )
        changed = []
        for relative, content in writes:
            target = Path(request.workspace) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
            changed.append(Path(relative))
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log = self.log_dir / f"{request.attempt_id}.jsonl"
        log.write_text('{"event":"direct dispatch"}\n', encoding="utf-8")
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


class RunStoreRefusalRoutingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_first_resume_after_closed_collected_stop_needs_no_retry(self):
        stopped = Scenario(
            self.root / "resume",
            states=[payload("ready", "execute_coding_prompt")],
            coder=[MockAgentAction()],
        )
        stopped.supervisor._executors["coder"] = _DirectExecutor(
            stopped.store.run_dir("run_001") / "adapter_logs"
        )
        first = stopped.supervisor.tick()
        self.assertEqual(
            (first.kind, first.stop_reason),
            ("stopped", StopReason.PATH_VIOLATION),
        )
        attempt = stopped.store.list_attempts("run_001", "M001-S01")[0]
        self.assertEqual(stopped.store.read_transition(attempt), "closed")
        self.assertFalse(
            any(event["kind"] == "verification" for event in stopped.events())
        )
        (stopped.project / FABRICATED_REVIEW).unlink()
        stops_before = sum(
            event["kind"] == "stop" for event in stopped.events()
        )
        escalations_before = len(
            stopped.store.list_escalations("run_001")
        )

        resumed = Scenario(
            self.root / "resume",
            project=stopped.project,
            states=[
                payload(
                    "ready",
                    "make_review_prompt",
                    review_prompt=REVIEW_PROMPT,
                )
            ],
            verbs=DEFAULT_VERBS,
        )
        self.assertIsNone(resumed.supervisor.resume())
        escaped: RunStoreRefusal | None = None
        result = None
        try:
            result = resumed.supervisor.tick()
        except RunStoreRefusal as refusal:
            escaped = refusal

        if escaped is not None:
            # Pin the two live campaigns' exact pre-fix shape: verification
            # became durable, no governed stop was added, and a plain second
            # process then consumed the same planning state successfully.
            self.assertEqual(escaped.code, "transition_regression")
            self.assertTrue(
                any(
                    event["kind"] == "verification" and event["passed"]
                    for event in resumed.events()
                )
            )
            self.assertEqual(
                sum(event["kind"] == "stop" for event in resumed.events()),
                stops_before,
            )
            self.assertEqual(
                len(resumed.store.list_escalations("run_001")),
                escalations_before,
            )
            retry = Scenario(
                self.root / "resume",
                project=stopped.project,
                states=[
                    payload(
                        "ready",
                        "make_review_prompt",
                        review_prompt=REVIEW_PROMPT,
                    )
                ],
                verbs=DEFAULT_VERBS,
            )
            self.assertIsNone(retry.supervisor.resume())
            recovered = retry.supervisor.tick()
            self.assertEqual(recovered.detail, "verb:make-review-prompt")

        self.assertIsNone(
            escaped,
            "the first resume escaped; the second process recovered only "
            "because verification had become durable",
        )
        self.assertEqual(result.detail, "verb:make-review-prompt")
        self.assertTrue(
            any(
                event["kind"] == "verification" and event["passed"]
                for event in resumed.events()
            )
        )

    def test_injected_tick_refusal_becomes_journaled_invalid_state_stop(self):
        scenario = Scenario(
            self.root / "tick_refusal",
            states=[payload("ready", "execute_coding_prompt")],
        )

        def refuse():
            raise RunStoreRefusal(
                "injected_tick_refusal", "representative in-run refusal"
            )

        scenario.supervisor._tick_inner = refuse
        result = scenario.supervisor.tick()

        self.assertEqual(
            (result.kind, result.stop_reason),
            ("stopped", StopReason.INVALID_STATE),
        )
        self.assertIn("injected_tick_refusal", result.detail)
        stop = [event for event in scenario.events() if event["kind"] == "stop"]
        self.assertEqual(len(stop), 1)
        self.assertIn("injected_tick_refusal", stop[0]["detail"])
        escalation = result.escalation_path.read_text(encoding="utf-8")
        self.assertIn("injected_tick_refusal", escalation)

    def test_transition_seam_refusal_is_routed_by_tick(self):
        scenario = Scenario(
            self.root / "transition_refusal",
            states=[payload("ready", "execute_coding_prompt")],
            coder=[MockAgentAction(writes=((SELF_REPORT, "report\n"),))],
        )
        advance = scenario.store.advance_transition
        injected = {"done": False}

        def refuse_started(attempt, state):
            if state == "started" and not injected["done"]:
                injected["done"] = True
                raise RunStoreRefusal(
                    "injected_transition_refusal",
                    "transition seam refused",
                )
            return advance(attempt, state)

        scenario.store.advance_transition = refuse_started
        result = scenario.supervisor.tick()

        self.assertEqual(result.stop_reason, StopReason.INVALID_STATE)
        self.assertIn("injected_transition_refusal", result.detail)
        self.assertTrue(
            any(event["kind"] == "dispatch" for event in scenario.events())
        )
        self.assertTrue(
            any(
                event["kind"] == "stop"
                and "injected_transition_refusal" in event["detail"]
                for event in scenario.events()
            )
        )

    def test_run_until_routes_a_typed_refusal_from_its_tick_seam(self):
        scenario = Scenario(
            self.root / "loop_refusal",
            states=[payload("ready", "execute_coding_prompt")],
        )

        def refuse_tick():
            raise RunStoreRefusal(
                "injected_loop_refusal", "substituted tick refused"
            )

        scenario.supervisor.tick = refuse_tick
        result = scenario.supervisor.run_until()
        self.assertEqual(result.stop_reason, StopReason.INVALID_STATE)
        self.assertIn("injected_loop_refusal", result.detail)
        self.assertTrue(
            any(
                event["kind"] == "stop"
                and "injected_loop_refusal" in event["detail"]
                for event in scenario.events()
            )
        )

    def test_dead_stop_journal_is_attempted_once_then_refusal_propagates(self):
        scenario = Scenario(
            self.root / "dead_journal",
            states=[payload("ready", "execute_coding_prompt")],
        )
        attempts = []

        def dead_journal(run_id, event):
            attempts.append((run_id, event.get("kind")))
            raise RunStoreRefusal("journal_dead", "journal is unavailable")

        scenario.store.append_event = dead_journal
        scenario.supervisor._tick_inner = lambda: scenario.supervisor._stop(
            StopReason.INVALID_STATE, "trigger governed stop"
        )
        with self.assertRaises(RunStoreRefusal) as refused:
            scenario.supervisor.run_until()
        self.assertEqual(refused.exception.code, "journal_dead")
        self.assertEqual(attempts, [("run_001", "stop")])

    def test_non_run_store_exception_still_propagates(self):
        scenario = Scenario(
            self.root / "other_exception",
            states=[payload("ready", "execute_coding_prompt")],
        )

        def fail():
            raise ValueError("untyped failure")

        scenario.supervisor._tick_inner = fail
        with self.assertRaisesRegex(ValueError, "untyped failure"):
            scenario.supervisor.tick()


if __name__ == "__main__":
    unittest.main()
