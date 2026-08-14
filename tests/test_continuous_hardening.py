"""M004 Phase A continuous-operation controls over deterministic seams."""

import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

from frutlups_drive import killswitch
from frutlups_drive.contracts import StopReason
from frutlups_drive.dispatch.mock import MockAgentAction
from frutlups_drive.runstore import RunStore, RunStoreRefusal
from frutlups_drive.watcher import Watcher

from _scenario import SELF_REPORT, Scenario, payload


CODER_WRITES = MockAgentAction(writes=((SELF_REPORT, "# Coder Self-Report\n"),))


class ContinuousControlTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def scenario(self, **kwargs):
        return Scenario(self.root, **kwargs)

    def assert_stop(self, result, reason):
        self.assertEqual(result.kind, "stopped")
        self.assertEqual(result.stop_reason, reason)
        self.assertTrue(result.escalation_path.is_file())

    def test_provider_retry_uses_policy_schedule_without_randomness(self):
        scenario = self.scenario(
            states=[payload(), payload()],
            coder=[MockAgentAction(raise_error=True), CODER_WRITES],
            policy_body=(
                "[limits]\n"
                "max_consecutive_provider_failures = 3\n"
                "provider_backoff_seconds = [1.25, 2.5]\n"
            ),
        )
        first = scenario.supervisor.tick()
        second = scenario.supervisor.tick()
        self.assertEqual(first.detail, "provider_failure")
        self.assertEqual(second.detail, "coder_attempt_completed")
        backoffs = [e for e in scenario.events() if e["kind"] == "backoff"]
        self.assertEqual(
            [(e["failure_streak"], e["seconds"]) for e in backoffs],
            [(1, 1.25)],
        )
        self.assertEqual(scenario.clock.now(), 1001.30)

    def test_consecutive_provider_failures_stop_with_escalation(self):
        scenario = self.scenario(
            states=[payload(), payload()],
            coder=[
                MockAgentAction(raise_error=True),
                MockAgentAction(raise_error=True),
            ],
            policy_body=(
                "[limits]\n"
                "max_consecutive_provider_failures = 2\n"
                "provider_backoff_seconds = [1, 2]\n"
            ),
        )
        self.assertEqual(scenario.supervisor.tick().detail, "provider_failure")
        self.assertEqual(scenario.supervisor.tick().detail, "provider_failure")
        stopped = scenario.supervisor.tick()
        self.assert_stop(stopped, StopReason.PROVIDER_FAILURE)
        self.assertEqual(
            [e["seconds"] for e in scenario.events() if e["kind"] == "backoff"],
            [1.0],
        )

    def test_general_no_progress_stop_is_separate_from_reconciliation(self):
        scenario = self.scenario(
            states=[payload(), payload()],
            coder=[CODER_WRITES, CODER_WRITES],
            verifier_exit_codes=[1, 1],
            policy_body=(
                "[limits]\n"
                "max_consecutive_no_progress = 2\n"
                "max_coder_attempts_per_slice = 3\n"
            ),
        )
        self.assertEqual(scenario.supervisor.tick().detail, "verification_failed")
        self.assertEqual(scenario.supervisor.tick().detail, "verification_failed")
        stopped = scenario.supervisor.tick()
        self.assert_stop(stopped, StopReason.NO_PROGRESS)
        self.assertIn("consecutive_loop_iterations", stopped.detail)

    def test_kill_during_watch_stops_within_policy_poll_interval(self):
        requested = {"done": False}

        def request_stop(scenario):
            if not requested["done"]:
                requested["done"] = True
                killswitch.request_stop(scenario.store.root)

        scenario = self.scenario(
            states=[payload()],
            coder=[MockAgentAction(writes=(), produced_override=())],
            watch_timeout=300.0,
            sleep_hook=request_stop,
            policy_body="[limits]\nwatch_poll_seconds = 0.25\n",
        )
        stopped = scenario.supervisor.tick()
        self.assert_stop(stopped, StopReason.KILL_SWITCH)
        self.assertEqual(scenario.clock.now(), 1000.25)
        self.assertNotIn("watch_timeout", scenario.event_kinds())


class WatcherKillTests(unittest.TestCase):
    def test_stop_predicate_short_circuits_before_the_full_timeout(self):
        clock = type(
            "Clock",
            (),
            {"value": 0.0, "now": lambda self: self.value},
        )()
        stopped = {"value": False}

        def sleep(seconds):
            clock.value += seconds
            stopped["value"] = True

        result = Watcher(clock, sleep).wait_for(
            [Path("never-created")],
            timeout_seconds=60.0,
            poll_seconds=0.5,
            stop_requested=lambda: stopped["value"],
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.stop_requested)
        self.assertEqual(result.waited_seconds, 0.5)


class RunStoreRotationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = RunStore(Path(self._tmp.name) / ".frutlups_drive")

    def create_run(self, run_id, payload=b""):
        run = self.store.create_run(run_id, {"contract_version": 1})
        if payload:
            (run / "payload.bin").write_bytes(payload)
        return run

    def test_oldest_unprotected_run_rotates_and_sizes_are_accounted(self):
        self.create_run("run_001", b"a" * 20)
        self.create_run("run_002", b"b" * 30)
        active = self.create_run("run_003", b"c" * 40)
        result = self.store.enforce_limits(
            "run_003", max_total_bytes=10_000, max_retained_runs=2
        )
        self.assertEqual(result.deleted_runs, ("run_001",))
        self.assertEqual([name for name, _ in result.per_run_bytes], ["run_002", "run_003"])
        self.assertFalse(self.store.run_dir("run_001").exists())
        self.assertTrue(active.is_dir())
        self.assertEqual(result.total_bytes, sum(size for _, size in result.per_run_bytes))

    def test_unresolved_escalation_and_active_run_are_never_deleted(self):
        protected = self.create_run("run_001", b"a" * 20)
        self.store.create_escalation(
            "run_001",
            "001_blocked.md",
            b'# Escalation\n\n```toml\nrun_id = "run_001"\n```\n',
        )
        self.create_run("run_002", b"b" * 20)
        active = self.create_run("run_003", b"c" * 20)
        result = self.store.enforce_limits(
            "run_003", max_total_bytes=10_000, max_retained_runs=2
        )
        self.assertEqual(result.deleted_runs, ("run_002",))
        self.assertTrue(protected.is_dir())
        self.assertTrue(active.is_dir())

    def test_impossible_rotation_refuses_before_deleting_anything(self):
        first = self.create_run("run_001", b"a" * 20)
        self.store.create_escalation(
            "run_001",
            "001_blocked.md",
            b'# Escalation\n\n```toml\nrun_id = "run_001"\n```\n',
        )
        active = self.create_run("run_002", b"b" * 20)
        with self.assertRaises(RunStoreRefusal) as caught:
            self.store.enforce_limits(
                "run_002", max_total_bytes=10_000, max_retained_runs=1
            )
        self.assertEqual(caught.exception.code, "run_store_full")
        self.assertTrue(first.is_dir())
        self.assertTrue(active.is_dir())

    def test_supervisor_store_refusal_is_a_standard_stop(self):
        scenario = Scenario(
            Path(self._tmp.name),
            states=[payload()],
            coder=[CODER_WRITES],
            policy_body="[limits]\nmax_run_store_bytes = 1\n",
        )
        result = scenario.supervisor.tick()
        self.assertEqual(result.stop_reason, StopReason.RUN_STORE_FULL)
        self.assertTrue(result.escalation_path.is_file())
        self.assertEqual(
            [e["reason"] for e in scenario.events() if e["kind"] == "stop"],
            ["run_store_full"],
        )


if __name__ == "__main__":
    unittest.main()
