"""M002 runtime primitives: watcher, budget, ladder, kill switch, escalation,
dispatch executors, mock verbs, and run-store runtime extensions."""

import io
import json
import tempfile
import tomllib
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

from frutlups_drive import killswitch, ladder
from frutlups_drive.budget import BudgetCounters, BudgetGate, _validate_cost_fact
from frutlups_drive.contracts import Role, StopReason
from frutlups_drive.dispatch.base import ExecutorScriptExhausted
from frutlups_drive.dispatch.manual import ManualAgentExecutor
from frutlups_drive.dispatch.mock import MockAgentAction, MockAgentExecutor
from frutlups_drive.escalate import write_escalation
from frutlups_drive.mockverbs import MockVerbWriter, VerbScriptExhausted
from frutlups_drive.policy import SCHEMA_VERSION, load_execution_policy
from frutlups_drive.runstore import RunStore, RunStoreRefusal
from frutlups_drive.watcher import Watcher

from test_contracts import make_request

MANIFEST = {"boundary": "slice_complete", "contract_version": 1}


class _HostileInt(int):
    """int subclass whose conversion hook must never run (R4-F1)."""

    def __float__(self):
        raise RuntimeError("conversion hook must never run")


class _HostileFloat(float):
    """float subclass whose conversion hook must never run (R4-F1)."""

    def __float__(self):
        raise RuntimeError("conversion hook must never run")


class _QuietInt(int):
    """Benign subclass: still outside the exact-type cost domain."""


class FakeClock:
    def __init__(self, start=1000.0):
        self.value = start

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def make_policy(tmp: Path, body: str = "") -> object:
    path = tmp / "frutlups_drive.toml"
    path.write_bytes((f'schema_version = "{SCHEMA_VERSION}"\n' + body).encode("utf-8"))
    return load_execution_policy(path).policy


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.clock = FakeClock()

    def watcher(self, on_poll=None):
        def sleep(seconds):
            self.clock.advance(seconds)
            if on_poll is not None:
                on_poll()

        return Watcher(self.clock, sleep)

    def test_existing_stable_files_are_observed_without_sleeping_forever(self):
        target = self.dir / "artifact.md"
        target.write_bytes(b"content\n")
        outcome = self.watcher().wait_for([target], timeout_seconds=10)
        self.assertTrue(outcome.ok)

    def test_timeout_when_artifact_never_appears(self):
        outcome = self.watcher().wait_for(
            [self.dir / "never.md"], timeout_seconds=5, poll_seconds=1
        )
        self.assertFalse(outcome.ok)
        self.assertGreaterEqual(outcome.waited_seconds, 5)

    def test_file_appearing_mid_watch_is_observed(self):
        target = self.dir / "late.md"
        polls = {"count": 0}

        def on_poll():
            polls["count"] += 1
            if polls["count"] == 2:
                target.write_bytes(b"late content\n")

        outcome = self.watcher(on_poll).wait_for(
            [target], timeout_seconds=60, poll_seconds=1
        )
        self.assertTrue(outcome.ok)

    def test_growing_file_waits_for_size_stability(self):
        target = self.dir / "growing.md"
        target.write_bytes(b"a")
        polls = {"count": 0}

        def on_poll():
            polls["count"] += 1
            if polls["count"] == 1:
                target.write_bytes(b"a" * 100)

        outcome = self.watcher(on_poll).wait_for(
            [target], timeout_seconds=60, poll_seconds=1, stability_checks=2
        )
        self.assertTrue(outcome.ok)
        self.assertGreaterEqual(polls["count"], 2)

    def test_watcher_never_validates_content(self):
        target = self.dir / "garbage.bin"
        target.write_bytes(b"\x00\xff not a report")
        self.assertTrue(self.watcher().wait_for([target], timeout_seconds=5).ok)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clock = FakeClock()

    def gate(self, body: str = ""):
        return BudgetGate(make_policy(Path(self._tmp.name), body), self.clock)

    def counters(self, events):
        return BudgetCounters.from_events(events)

    def test_wall_clock_stop(self):
        gate = self.gate("[limits]\nmax_wall_clock_minutes = 1\n")
        counters = self.counters([{"kind": "run_created", "t": 1000.0}])
        self.assertIsNone(gate.check_global(counters))
        self.clock.advance(61)
        self.assertEqual(
            gate.check_global(counters),
            (StopReason.BUDGET_EXHAUSTED, "wall_clock"),
        )

    def test_total_cost_stop_only_above_limit(self):
        gate = self.gate()
        zero_spend = self.counters(
            [{"kind": "collected", "t": 1.0, "attempt": "a", "status": "completed",
              "cost_usd": None}]
        )
        self.assertIsNone(gate.check_global(zero_spend))
        overspent = self.counters(
            [{"kind": "collected", "t": 1.0, "attempt": "a", "status": "completed",
              "cost_usd": 0.5}]
        )
        self.assertEqual(
            gate.check_global(overspent),
            (StopReason.BUDGET_EXHAUSTED, "total_cost"),
        )

    def test_consecutive_provider_failures_stop_and_reset(self):
        gate = self.gate("[limits]\nmax_consecutive_provider_failures = 2\n")
        failing = self.counters(
            [
                {"kind": "attempt_abandoned", "t": 1.0, "attempt": "a"},
                {"kind": "watch_timeout", "t": 2.0, "attempt": "b"},
            ]
        )
        self.assertEqual(
            gate.check_global(failing),
            (StopReason.PROVIDER_FAILURE, "consecutive_provider_failures"),
        )
        recovered = self.counters(
            [
                {"kind": "attempt_abandoned", "t": 1.0, "attempt": "a"},
                {"kind": "collected", "t": 2.0, "attempt": "b",
                 "status": "completed", "cost_usd": None},
            ]
        )
        self.assertIsNone(gate.check_global(recovered))

    def test_resume_resets_only_reconstructed_streaks(self):
        gate = self.gate(
            "[limits]\nmax_consecutive_provider_failures = 3\n"
            "max_total_cost_usd = 1.0\n"
        )
        monotone = [
            {"kind": "dispatch", "t": 1.0, "role": "coder", "slice": "S1",
             "repair": False},
            {"kind": "dispatch", "t": 2.0, "role": "coder", "slice": "S1",
             "repair": True},
            {"kind": "dispatch", "t": 3.0, "role": "architect", "slice": "S1",
             "repair": False},
            {"kind": "collected", "t": 4.0, "role": "coder", "slice": "S1",
             "attempt": "a", "status": "completed", "cost_usd": 0.25},
            {"kind": "slice_complete", "t": 5.0, "slice": "S1",
             "milestone": "M001"},
            {"kind": "holistic_review", "t": 6.0, "pass": 1,
             "clean": True},
            {"kind": "reconciliation", "t": 7.0, "slice": "S1",
             "progress": False},
        ]
        failures = [
            {"kind": "watch_timeout", "t": 8.0, "attempt": "b"},
            {"kind": "watch_timeout", "t": 9.0, "attempt": "c"},
            {"kind": "watch_timeout", "t": 10.0, "attempt": "d"},
            {"kind": "tick", "t": 11.0, "result": "acted",
             "progress": False},
        ]

        stopped = self.counters(monotone + failures)
        self.assertEqual(stopped.consecutive_provider_failures, 3)
        self.assertEqual(stopped.consecutive_no_progress, 1)
        self.assertEqual(
            gate.check_global(stopped),
            (StopReason.PROVIDER_FAILURE, "consecutive_provider_failures"),
        )

        resumed = self.counters(
            monotone + failures + [{"kind": "resume", "t": 12.0, "run": "run_001"}]
        )
        self.assertEqual(resumed.consecutive_provider_failures, 0)
        self.assertEqual(resumed.consecutive_no_progress, 0)
        self.assertEqual(resumed.coder_dispatches_for("S1"), 1)
        self.assertEqual(resumed.coder_collected_for("S1"), 1)
        self.assertEqual(resumed.repairs_for("S1"), 1)
        self.assertEqual(resumed.reconciliations_for("S1"), 1)
        self.assertEqual(resumed.reconciliations_without_progress, 1)
        self.assertAlmostEqual(resumed.total_cost_usd, 0.25)
        self.assertEqual(resumed.slices_completed, 1)
        self.assertEqual(resumed.passes_completed, 1)

    def test_slice_and_pass_meters(self):
        gate = self.gate("[target]\nmax_slices = 1\n")
        counters = self.counters(
            [{"kind": "slice_complete", "t": 1.0, "slice": "S1", "milestone": "M001"}]
        )
        self.assertEqual(
            gate.check_global(counters), (StopReason.BUDGET_EXHAUSTED, "slices")
        )
        pass_gate = self.gate("[target]\nmax_passes = 1\n")
        rollover = self.counters(
            [
                {"kind": "slice_complete", "t": 1.0, "slice": "S1", "milestone": "M001"},
                {"kind": "slice_complete", "t": 2.0, "slice": "S2", "milestone": "M002"},
            ]
        )
        self.assertIsNone(pass_gate.check_global(rollover))
        reviewed = self.counters(
            [
                {
                    "kind": "holistic_review",
                    "t": 3.0,
                    "pass": 1,
                    "clean": True,
                }
            ]
        )
        self.assertEqual(
            pass_gate.check_global(reviewed),
            (StopReason.BUDGET_EXHAUSTED, "passes"),
        )

    def test_coder_attempt_and_repair_caps(self):
        gate = self.gate(
            "[limits]\nmax_coder_attempts_per_slice = 1\nmax_report_repairs = 1\n"
        )
        counters = self.counters(
            [
                {"kind": "dispatch", "t": 1.0, "role": "coder", "slice": "S1",
                 "repair": False},
                {"kind": "dispatch", "t": 2.0, "role": "coder", "slice": "S1",
                 "repair": True},
            ]
        )
        self.assertEqual(
            gate.check_coder_dispatch(counters, "S1"),
            (StopReason.BUDGET_EXHAUSTED, "coder_attempts"),
        )
        self.assertIsNone(gate.check_coder_dispatch(counters, "S2"))
        self.assertEqual(
            gate.check_repair_dispatch(counters, "S1"),
            (StopReason.REPAIR_EXHAUSTED, "report_repairs"),
        )

    def test_invalid_durable_cost_facts_fail_closed_monotonically(self):
        # R2-F2: a non-null invalid durable fact must never decrease spend,
        # reopen authorization, or pass the global gate.
        gate = self.gate("[limits]\nmax_total_cost_usd = 1.0\n")
        base = {"kind": "collected", "t": 1.0, "attempt": "a",
                "status": "completed"}
        valid_then_invalid = self.counters(
            [dict(base, cost_usd=0.6), dict(base, attempt="b", cost_usd=-5.0)]
        )
        self.assertEqual(
            gate.check_global(valid_then_invalid),
            (StopReason.BUDGET_EXHAUSTED, "cost_fact_invalid"),
        )
        self.assertAlmostEqual(valid_then_invalid.total_cost_usd, 0.6)
        self.assertEqual(gate.remaining_cost(valid_then_invalid), 0.0)
        for bad in (True, "0.25", float("nan"), float("inf"), float("-inf"),
                    -0.01):
            with self.subTest(value=repr(bad)):
                counters = self.counters([dict(base, cost_usd=bad)])
                self.assertEqual(
                    gate.check_global(counters),
                    (StopReason.BUDGET_EXHAUSTED, "cost_fact_invalid"),
                )
        intact = self.counters([dict(base, cost_usd=0.25)])
        self.assertIsNone(gate.check_global(intact))
        self.assertAlmostEqual(gate.remaining_cost(intact), 0.75)

    def test_huge_integer_durable_cost_facts_fail_closed_without_throwing(self):
        # R3-F1 reviewer-literal probe: 401- and 10,001-digit integers are
        # ordinary values for Python objects loaded from a journal; replay
        # must reach the same owned fail-closed decision as any other
        # invalid fact — never a raw OverflowError — and a valid prior
        # total must remain unchanged with zero remaining authorization.
        gate = self.gate("[limits]\nmax_total_cost_usd = 1.0\n")
        base = {"kind": "collected", "t": 1.0, "attempt": "a",
                "status": "completed"}
        for exponent in (400, 10_000):
            with self.subTest(exponent=exponent):
                counters = self.counters(
                    [dict(base, cost_usd=0.6),
                     dict(base, attempt="b", cost_usd=10**exponent)]
                )
                self.assertTrue(counters.cost_fact_invalid)
                self.assertAlmostEqual(counters.total_cost_usd, 0.6)
                self.assertEqual(gate.remaining_cost(counters), 0.0)
                self.assertEqual(
                    gate.check_global(counters),
                    (StopReason.BUDGET_EXHAUSTED, "cost_fact_invalid"),
                )

    def test_numeric_subclass_durable_cost_facts_fail_closed_without_hooks(self):
        # R4-F1 reviewer-literal probe: the cost domain is exact built-in
        # int/float. A numeric subclass in a loaded event reaches the same
        # owned fail-closed decision as any other invalid fact, without its
        # conversion hook ever running, and prior spend stays unchanged.
        gate = self.gate("[limits]\nmax_total_cost_usd = 1.0\n")
        base = {"kind": "collected", "t": 1.0, "attempt": "a",
                "status": "completed"}
        for bad in (_HostileInt(1), _HostileFloat(1.0), _QuietInt(1)):
            with self.subTest(value=type(bad).__name__):
                counters = self.counters(
                    [dict(base, cost_usd=0.6),
                     dict(base, attempt="b", cost_usd=bad)]
                )
                self.assertTrue(counters.cost_fact_invalid)
                self.assertAlmostEqual(counters.total_cost_usd, 0.6)
                self.assertEqual(gate.remaining_cost(counters), 0.0)
                self.assertEqual(
                    gate.check_global(counters),
                    (StopReason.BUDGET_EXHAUSTED, "cost_fact_invalid"),
                )

    def test_reconciliation_no_progress(self):
        gate = self.gate()
        stuck = self.counters(
            [
                {"kind": "reconciliation", "t": 1.0, "attempt": "a", "progress": False},
                {"kind": "reconciliation", "t": 2.0, "attempt": "b", "progress": False},
            ]
        )
        self.assertEqual(
            gate.check_reconciliation(stuck, "S1"),
            (StopReason.NO_PROGRESS, "reconciliations_without_progress"),
        )
        progressing = self.counters(
            [
                {"kind": "reconciliation", "t": 1.0, "attempt": "a", "progress": False},
                {"kind": "reconciliation", "t": 2.0, "attempt": "b", "progress": True},
            ]
        )
        self.assertIsNone(gate.check_reconciliation(progressing, "S1"))


class LadderTests(unittest.TestCase):
    def test_ladder_table(self):
        cases = [
            (1, 0, False, None),
            (1, 1, False, None),
            (2, 0, False, None),
            (3, 0, False, StopReason.LADDER_ROUND3),
            (1, 2, False, StopReason.LADDER_ROUND3),
            (4, 0, False, StopReason.LADDER_ROUND4_UNAUTHORIZED),
            (1, 3, False, StopReason.LADDER_ROUND4_UNAUTHORIZED),
            (4, 0, True, None),
        ]
        for frontier_round, dispatches, authorized, expected in cases:
            with self.subTest(round=frontier_round, dispatches=dispatches,
                              authorized=authorized):
                self.assertEqual(
                    ladder.check_ladder(frontier_round, dispatches, authorized),
                    expected,
                )


class KillSwitchTests(unittest.TestCase):
    def test_request_and_check_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".frutlups_drive"
            self.assertFalse(killswitch.stop_requested(root))
            first = killswitch.request_stop(root)
            second = killswitch.request_stop(root)
            self.assertEqual(first, second)
            self.assertTrue(killswitch.stop_requested(root))
            self.assertEqual(first.name, "STOP")


class EscalationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = RunStore(Path(self._tmp.name) / ".frutlups_drive")
        self.store.create_run("run_001", MANIFEST)

    def write(self, reason, slice_id="M001-S01", attempt_id="attempt_001"):
        return write_escalation(
            self.store,
            "run_001",
            reason=reason,
            slice_id=slice_id,
            attempt_id=attempt_id,
            planning_snapshot="outcome=blocked; step=null; diagnostic_codes=none",
            attempts_summary="- attempt_001: closed",
            decision_required="Decide.",
            safe_options="Inspect and resume.",
            actions_not_taken="No redispatch.",
            resume_command="python -m frutlups_drive resume . run_001",
        )

    def test_complete_escalation_artifact_shape(self):
        path = self.write(StopReason.BLOCKED_VERDICT)
        text = path.read_bytes().decode("utf-8")
        self.assertEqual(path.name, "001_blocked_verdict.md")
        for heading in (
            "## Planning-State Snapshot",
            "## Attempts Summary",
            "## Decision Required",
            "## Safe Options",
            "## Actions Deliberately Not Taken",
            "## Resume Command",
        ):
            self.assertIn(heading, text)
        header = text.split("```toml\n")[1].split("```")[0]
        parsed = tomllib.loads(header)
        self.assertEqual(parsed["stop_reason"], "blocked_verdict")
        self.assertEqual(parsed["run_id"], "run_001")
        self.assertNotIn("\\", text)

    def test_same_stop_converges_without_duplicate(self):
        first = self.write(StopReason.KILL_SWITCH)
        second = self.write(StopReason.KILL_SWITCH)
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.list_escalations("run_001")), 1)

    def test_distinct_stops_get_distinct_artifacts(self):
        self.write(StopReason.KILL_SWITCH)
        second = self.write(StopReason.BUDGET_EXHAUSTED)
        self.assertEqual(second.name, "002_budget_exhausted.md")
        self.assertEqual(len(self.store.list_escalations("run_001")), 2)


class MockExecutorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.workspace = self.dir / "ws"
        self.workspace.mkdir()

    def test_scripted_writes_and_facts(self):
        executor = MockAgentExecutor(
            [
                MockAgentAction(
                    writes=(("notes/report.md", "# Report\n"),),
                    cost_usd=0.25,
                )
            ],
            self.dir / "logs",
        )
        request = make_request(workspace=self.workspace, max_cost_usd=1.0)
        result = executor.execute(request)
        self.assertEqual(result.status, "completed")
        self.assertEqual(
            (self.workspace / "notes/report.md").read_bytes(),
            b"# Report\n",
        )
        self.assertEqual(result.cost_usd, 0.25)
        self.assertTrue(Path(result.event_log_path).is_file())
        self.assertEqual(result.changed_files, (Path("notes/report.md"),))

    def tamper_cost(self, action, value):
        object.__setattr__(action, "cost_usd", value)
        return action

    def assert_pre_effect_cost_refusal(self, tampered_cost, max_cost_usd):
        from frutlups_drive.dispatch.base import CostAuthorizationExceeded

        action = self.tamper_cost(
            MockAgentAction(writes=(("notes/report.md", "# r\n"),)),
            tampered_cost,
        )
        executor = MockAgentExecutor([action], self.dir / "logs")
        request = make_request(
            workspace=self.workspace, max_cost_usd=max_cost_usd
        )
        with self.assertRaises(CostAuthorizationExceeded):
            executor.execute(request)
        self.assertFalse(
            (self.workspace / "notes").exists(),
            "an unauthorizable cost fact must refuse before any write",
        )
        self.assertFalse(
            (self.dir / "logs").exists(),
            "an unauthorizable cost fact must refuse before any adapter log",
        )

    def test_negative_cost_fact_refuses_before_any_write(self):
        # R2-F2 reviewer-literal probe: cost -0.25 against authorization 1.0.
        self.assert_pre_effect_cost_refusal(-0.25, 1.0)

    def test_nan_cost_fact_refuses_before_any_write(self):
        # R2-F2 reviewer-literal probe: NaN against authorization 0.0.
        self.assert_pre_effect_cost_refusal(float("nan"), 0.0)

    def test_non_finite_and_boolean_cost_facts_refuse_before_any_write(self):
        for bad in (float("inf"), float("-inf"), True, "0.25"):
            with self.subTest(value=repr(bad)):
                self.assert_pre_effect_cost_refusal(bad, 1.0)

    def test_action_construction_rejects_invalid_cost_facts(self):
        for bad in (-0.25, float("nan"), float("inf"), float("-inf"), True,
                    "0.25"):
            with self.subTest(value=repr(bad)):
                with self.assertRaises(ValueError):
                    MockAgentAction(cost_usd=bad)
        for good in (None, 0, 0.0, 0.25, 1):
            with self.subTest(value=repr(good)):
                action = MockAgentAction(cost_usd=good)
                if good is None:
                    self.assertIsNone(action.cost_usd)
                else:
                    self.assertIsInstance(action.cost_usd, float)
                    self.assertEqual(action.cost_usd, float(good))

    def test_huge_integer_cost_facts_refuse_at_every_internal_boundary(self):
        # R3-F1: float() of a huge plain integer raises OverflowError; the
        # one cost boundary must own that conversion at the validator,
        # immutable construction, and executor pre-effect revalidation —
        # the same stable invalid-cost decision as negative/non-finite.
        for exponent in (400, 10_000):
            with self.subTest(boundary="validator", exponent=exponent):
                with self.assertRaises(ValueError):
                    _validate_cost_fact(10**exponent)
            with self.subTest(boundary="validator-negative", exponent=exponent):
                with self.assertRaises(ValueError):
                    _validate_cost_fact(-(10**exponent))
            with self.subTest(boundary="construction", exponent=exponent):
                with self.assertRaises(ValueError):
                    MockAgentAction(cost_usd=10**exponent)
        self.assert_pre_effect_cost_refusal(10**400, 1.0)

    def test_ten_thousand_digit_tampered_cost_refuses_before_any_write(self):
        # R3-F1: a tampered frozen action carrying an unconvertible integer
        # takes the owned cost-authorization refusal, not OverflowError.
        self.assert_pre_effect_cost_refusal(10**10_000, 1.0)

    def test_numeric_subclass_cost_facts_refuse_without_conversion_hooks(self):
        # R4-F1 reviewer-literal probe: admission is by exact type, so a
        # subclass is invalid before any conversion. A hostile __float__
        # raising RuntimeError proves the hook never runs at the validator
        # or the immutable-construction boundary.
        for bad in (_HostileInt(1), _HostileFloat(1.0), _QuietInt(1)):
            with self.subTest(boundary="validator", value=type(bad).__name__):
                with self.assertRaises(ValueError):
                    _validate_cost_fact(bad)
            with self.subTest(boundary="construction",
                              value=type(bad).__name__):
                with self.assertRaises(ValueError):
                    MockAgentAction(cost_usd=bad)
        self.assert_pre_effect_cost_refusal(_HostileInt(1), 1.0)

    def test_hostile_float_subclass_tampered_cost_refuses_before_any_write(self):
        # R4-F1: an executor-tampered hostile float subclass takes the owned
        # cost-authorization refusal without invoking its conversion hook.
        self.assert_pre_effect_cost_refusal(_HostileFloat(1.0), 1.0)

    def test_script_exhaustion_raises(self):
        executor = MockAgentExecutor([], self.dir / "logs")
        with self.assertRaises(ExecutorScriptExhausted):
            executor.execute(make_request(workspace=self.workspace))

    def test_scripted_provider_failure_raises(self):
        executor = MockAgentExecutor(
            [MockAgentAction(raise_error=True)], self.dir / "logs"
        )
        with self.assertRaises(RuntimeError):
            executor.execute(make_request(workspace=self.workspace))

    def test_executor_returns_facts_never_verdicts(self):
        executor = MockAgentExecutor([MockAgentAction()], self.dir / "logs")
        result = executor.execute(make_request(workspace=self.workspace))
        payload = json.loads(
            json.dumps(
                {
                    "status": result.status,
                    "exit_reason": result.exit_reason,
                }
            )
        )
        self.assertNotIn("verdict", payload["exit_reason"])
        self.assertIn(result.status, ("completed",))


class ManualExecutorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.workspace = self.dir / "ws"
        self.workspace.mkdir()
        self.clock = FakeClock()

    def executor(self, on_poll=None, *, stop_requested=None, poll_seconds=0.05):
        def sleep(seconds):
            self.clock.advance(seconds)
            if on_poll is not None:
                on_poll()

        self.instructions = io.StringIO()
        return ManualAgentExecutor(
            Watcher(self.clock, sleep),
            self.instructions,
            self.dir / "logs",
            stop_requested=stop_requested,
            poll_seconds=poll_seconds,
        )

    def test_manual_completion_when_human_supplies_artifact(self):
        expected = Path("05_governance/reviews/m001/m001_s01_self_report.md")

        def on_poll():
            target = self.workspace / expected
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"# Coder Self-Report\n")

        executor = self.executor(on_poll)
        request = make_request(
            workspace=self.workspace, expected_artifacts=(expected,), max_seconds=30
        )
        result = executor.execute(request)
        self.assertEqual(result.status, "completed")
        self.assertIn("manual coder dispatch", self.instructions.getvalue())
        self.assertIn(expected.as_posix(), self.instructions.getvalue())

    def test_manual_timeout_is_a_fact_not_an_exception(self):
        executor = self.executor()
        request = make_request(
            workspace=self.workspace,
            expected_artifacts=(Path("never.md"),),
            max_seconds=5,
        )
        result = executor.execute(request)
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.produced_artifacts, ())

    def test_manual_wait_honors_stop_within_one_poll_interval(self):
        started = self.clock.now()
        executor = self.executor(
            stop_requested=lambda: self.clock.now() >= started + 0.25,
            poll_seconds=0.25,
        )
        request = make_request(
            workspace=self.workspace,
            expected_artifacts=(Path("never.md"),),
            max_seconds=30,
        )
        result = executor.execute(request)
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.exit_reason, "manual_stop_requested")
        self.assertEqual(self.clock.now() - started, 0.25)
        payload = json.loads(result.event_log_path.read_bytes())
        self.assertTrue(payload["stop_requested"])


class MockVerbWriterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name)

    def test_one_template_shaped_artifact_per_invocation(self):
        prompt = (
            "# Review M001-S01\n\n```yaml\nmilestone: M001\nslice: M001-S01\n"
            "role: reviewer\nround: 1\nstatus: ready\n```\n"
        )
        writer = MockVerbWriter(
            self.project,
            {
                "make-review-prompt": (
                    ("prompts/for_review_agent/002_review.md", prompt),
                )
            },
        )
        path = writer.invoke(
            "make-review-prompt", "prompts/for_review_agent/002_review.md"
        )
        self.assertEqual(
            path, self.project / "prompts/for_review_agent/002_review.md"
        )
        self.assertIn("```yaml", path.read_bytes().decode("utf-8"))
        with self.assertRaises(VerbScriptExhausted):
            writer.invoke(
                "make-review-prompt", "prompts/for_review_agent/002_review.md"
            )

    def test_unknown_verb_refused(self):
        writer = MockVerbWriter(self.project, {})
        with self.assertRaises(VerbScriptExhausted):
            writer.invoke("commit-everything", "somewhere.md")

    def test_resume_consumption_offsets_are_honored(self):
        writer = MockVerbWriter(
            self.project,
            {
                "record-verdict": (
                    ("05_governance/reviews/m001/first.md", "first"),
                    ("05_governance/reviews/m001/second.md", "second"),
                )
            },
            consumed={"record-verdict": 1},
        )
        path = writer.invoke(
            "record-verdict", "05_governance/reviews/m001/second.md"
        )
        self.assertTrue(path.name == "second.md")


class RunStoreRuntimeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = RunStore(Path(self._tmp.name) / ".frutlups_drive")
        self.store.create_run("run_001", MANIFEST)

    def test_event_replay_round_trip(self):
        self.store.append_event("run_001", {"kind": "run_created", "t": 1.0})
        self.store.append_event(
            "run_001", {"kind": "dispatch", "t": 2.0, "role": "coder"}
        )
        events = self.store.read_events("run_001")
        self.assertEqual([e["kind"] for e in events], ["run_created", "dispatch"])

    def test_next_run_id_is_sequential(self):
        self.assertEqual(self.store.next_run_id(), "run_002")
        self.store.create_run("run_002", MANIFEST)
        self.assertEqual(self.store.next_run_id(), "run_003")

    def test_slice_and_attempt_listing(self):
        a1 = self.store.create_attempt("run_001", "M001-S01")
        a2 = self.store.create_attempt("run_001", "M001-S01")
        self.store.create_attempt("run_001", "M001-S02")
        self.assertEqual(self.store.list_slices("run_001"), ("M001-S01", "M001-S02"))
        self.assertEqual(
            [a.name for a in self.store.list_attempts("run_001", "M001-S01")],
            [a1.name, a2.name],
        )

    def test_request_and_result_durable_reads(self):
        attempt = self.store.create_attempt("run_001", "M001-S01")
        self.assertIsNone(self.store.read_request(attempt))
        self.store.write_request(attempt, make_request())
        request = self.store.read_request(attempt)
        self.assertEqual(request["role"], "coder")
        self.assertIsNone(self.store.read_result(attempt))

    def test_attempt_runtime_records_are_write_once_and_owned(self):
        attempt = self.store.create_attempt("run_001", "M001-S01")
        self.store.write_provider_events(attempt, b'{"event":"x"}\n')
        self.assert_refused(
            "provider_events_conflict",
            self.store.write_provider_events,
            attempt,
            b'{"event":"y"}\n',
        )
        self.store.write_diff_manifest(attempt, {"changes": []})
        self.assert_refused(
            "diff_manifest_conflict",
            self.store.write_diff_manifest,
            attempt,
            {"changes": ["x"]},
        )
        self.store.write_attempt_prompt(attempt, "repair_prompt.md", b"# prompt\n")
        self.assert_refused(
            "attempt_file_invalid",
            self.store.write_attempt_prompt,
            attempt,
            "evil.md",
            b"x",
        )
        with tempfile.TemporaryDirectory() as outside:
            self.assert_refused(
                "attempt_unowned",
                self.store.write_provider_events,
                Path(outside),
                b"x",
            )

    def test_publish_verification_is_write_once_and_fenced(self):
        attempt = self.store.create_attempt("run_001", "M001-S01")
        target = self.store.publish_verification(
            attempt, {"evidence.toml": b"passed = true\n"}
        )
        self.assertEqual(target.name, "verification")
        self.assert_refused(
            "verification_conflict",
            self.store.publish_verification,
            attempt,
            {"evidence.toml": b"passed = false\n"},
        )
        self.assert_refused(
            "verification_file_invalid",
            self.store.publish_verification,
            attempt,
            {"../escape.txt": b"x"},
        )

    def test_escalation_records_are_fenced_and_write_once(self):
        self.store.create_escalation("run_001", "001_kill_switch.md", b"body\n")
        self.assert_refused(
            "escalation_conflict",
            self.store.create_escalation,
            "run_001",
            "001_kill_switch.md",
            b"other\n",
        )
        self.assert_refused(
            "escalation_name_invalid",
            self.store.create_escalation,
            "run_001",
            "../escape.md",
            b"x",
        )
        self.assertEqual(
            [p.name for p in self.store.list_escalations("run_001")],
            ["001_kill_switch.md"],
        )

    def test_transition_hook_fires_after_each_durable_transition(self):
        seen = []
        store = RunStore(
            Path(self._tmp.name) / ".frutlups_drive",
            transition_hook=lambda state, path: seen.append(state),
        )
        attempt = store.create_attempt("run_001", "M001-S01")
        store.advance_transition(attempt, "started")
        store.advance_transition(attempt, "collected")
        self.assertEqual(seen, ["planned", "started", "collected"])

    def assert_refused(self, code, operation, *args):
        with self.assertRaises(RunStoreRefusal) as caught:
            operation(*args)
        self.assertEqual(caught.exception.code, code)


if __name__ == "__main__":
    unittest.main()
