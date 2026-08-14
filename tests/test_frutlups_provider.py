"""FrutlupsPlanProvider transport lanes (M003-S02 released contract).

Every process is the reviewer-visible local stub CLI
``stub_frutlups_status.py`` launched through the accepted production
runner with an explicit absolute argv — never a real frutlups module, a
sibling repository read, or PATH discovery. Released-wrapper member
semantics live in ``test_released_contract.py``; this lane owns the
transport refusal families, capture identity, and refusal-before-effect
proofs through the real supervisor.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

from frutlups_drive.contracts import PlanOutcome
from frutlups_drive.planstate import (
    FrutlupsPlanProvider,
    PlanProviderUnavailable,
)
from frutlups_drive.verifier import SubprocessRunner
from frutlups_drive.workspace import WorkspaceManager

from _scenario import FakeClock, Scenario, clean_pass_states
from test_subprocess_agent import RecordingRunner

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "frontier"
STUB = str(Path(__file__).resolve().parent / "stub_frutlups_status.py")
READY_WRAPPER = FIXTURES / "ready_make_coding_prompt.json"


class ProviderTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.captures = self.dir / "captures"
        self.clock = FakeClock()
        wrapper = json.loads(READY_WRAPPER.read_text(encoding="utf-8"))
        wrapper["memory_mode"] = {
            "contract_id": "frutlups.memory_mode",
            "contract_version": "1",
            "valid": True,
            "mode": "none",
            "memory_root": None,
            "diagnostics": [],
        }
        ready = self.dir / "released-0.1.2-wrapper.json"
        ready.write_text(json.dumps(wrapper), encoding="utf-8")
        self.ready_wrapper = str(ready)

    def provider(self, *stub_args, argv=None, timeout=30.0, runner=None,
                 captures=None):
        return FrutlupsPlanProvider(
            argv=tuple(argv)
            if argv is not None
            else (sys.executable, STUB, *stub_args),
            cwd=self.dir,
            capture_root=captures if captures is not None else self.captures,
            timeout_seconds=timeout,
            runner=runner or SubprocessRunner(self.clock),
        )

    def assert_unavailable(self, provider, code):
        with self.assertRaises(PlanProviderUnavailable) as caught:
            provider.read_planning_state()
        self.assertEqual(caught.exception.code, code)
        message = str(caught.exception)
        self.assertNotIn(str(self.dir), message, "no machine-local path")
        self.assertNotIn(STUB, message, "no command argument echo")
        self.assertNotIn("scripted failure", message, "no stderr echo")
        self.assertNotIn("Traceback", message)
        return caught.exception


class ProviderConstructionTests(ProviderTestCase):
    def test_argv_must_be_explicit_nonempty_with_absolute_head(self):
        for bad in ((), ("frutlups", "status"), ("python",), (3,),
                    [sys.executable]):
            with self.subTest(argv=repr(bad)):
                with self.assertRaises(ValueError):
                    FrutlupsPlanProvider(
                        argv=bad,
                        cwd=self.dir,
                        capture_root=self.captures,
                        timeout_seconds=30.0,
                        runner=RecordingRunner(),
                    )

    def test_timeout_must_be_positive_finite_plain_number(self):
        for bad in (0, -5, float("inf"), float("nan"), True, "30"):
            with self.subTest(value=repr(bad)):
                with self.assertRaises(ValueError):
                    self.provider("raw", "x", timeout=bad)

    def test_huge_integer_timeouts_are_owned_valueerrors_without_effect(self):
        # R1-F3 regression (round two): exact built-in positive and negative
        # 401- and 10,001-digit integers reach the one owned bounded
        # ValueError — never a raw OverflowError — with no value echo, no
        # spawn, and no capture-root creation.
        runner = RecordingRunner()
        for label, bad in (
            ("positive_401_digit", 10**400),
            ("negative_401_digit", -(10**400)),
            ("positive_10001_digit", 10**10_000),
            ("negative_10001_digit", -(10**10_000)),
        ):
            with self.subTest(value=label):
                with self.assertRaises(ValueError) as caught:
                    self.provider("raw", "x", timeout=bad, runner=runner)
                message = str(caught.exception)
                self.assertNotIn("00000", message,
                                 "the invalid value must not be echoed")
                self.assertNotIn("OverflowError", message)
        self.assertEqual(runner.calls, [], "nothing may spawn")
        self.assertFalse(self.captures.exists(),
                         "no capture root may be created")


class ReleasedWrapperTransportTests(ProviderTestCase):
    def test_captured_released_wrapper_parses(self):
        state = self.provider("raw", self.ready_wrapper).read_planning_state()
        self.assertEqual(state.outcome, PlanOutcome.READY)

    def test_missing_executable_refuses_without_spawn(self):
        runner = RecordingRunner()
        provider = self.provider(
            argv=(str(self.dir / "no_such_frutlups.exe"), "status"),
            runner=runner,
        )
        self.assert_unavailable(provider, "frutlups_executable_missing")
        self.assertEqual(runner.calls, [], "nothing may spawn")

    def test_runner_failure_and_exhausted_transport_refuse(self):
        for error in (OSError("spawn refused"),
                      RuntimeError("scripted transport exhausted")):
            with self.subTest(error=str(error)):
                provider = self.provider(
                    "raw", "unused",
                    runner=RecordingRunner(error=error),
                    captures=self.captures / str(type(error).__name__),
                )
                self.assert_unavailable(provider, "frutlups_transport_failed")

    def test_nonzero_exit_refuses_without_stderr_echo(self):
        self.assert_unavailable(
            self.provider("nonzero"), "frutlups_exit_nonzero"
        )

    def test_timeout_refuses(self):
        self.assert_unavailable(
            self.provider("hang", timeout=3.0), "frutlups_timeout"
        )

    def test_stdout_overflow_refuses(self):
        self.assert_unavailable(
            self.provider("huge-stdout"), "frutlups_output_overflow"
        )

    def test_stderr_overflow_refuses(self):
        self.assert_unavailable(
            self.provider("huge-stderr", self.ready_wrapper),
            "frutlups_output_overflow",
        )

    def test_malformed_output_refuses(self):
        self.assert_unavailable(
            self.provider("malformed"), "frutlups_output_malformed"
        )

    def test_nonstandard_constants_refuse(self):
        self.assert_unavailable(
            self.provider("constants"), "frutlups_output_malformed"
        )

    def test_two_documents_refuse(self):
        self.assert_unavailable(
            self.provider("two-documents", self.ready_wrapper),
            "frutlups_output_malformed",
        )

    def test_non_object_top_level_refuses(self):
        payload = self.dir / "array.json"
        payload.write_bytes(b"[1, 2]")
        self.assert_unavailable(
            self.provider("raw", str(payload)), "frutlups_output_not_object"
        )

    def test_missing_and_invalid_members_refuse(self):
        cases = {
            "missing-frontier-member": "planning_frontier_member_missing",
            "missing-resume-member": "loop_resume_member_missing",
            "non-object-frontier-member": "planning_frontier_member_invalid",
        }
        for mode, code in cases.items():
            with self.subTest(mode=mode):
                self.assert_unavailable(
                    self.provider(mode, captures=self.captures / mode), code
                )

    def test_legacy_provisional_member_is_not_a_live_fallback(self):
        # Production live selection has no old-schema fallback: a wrapper
        # carrying only the retired provisional planning_state member
        # refuses before any effect.
        self.assert_unavailable(
            self.provider("legacy-planning-state"),
            "planning_frontier_member_missing",
        )

    def test_state_never_carries_wrapper_machine_paths_or_next_command(self):
        # Wrapper privacy: the parsed state carries no absolute machine
        # paths from unrelated wrapper fields and never the producer's
        # next_command text.
        state = self.provider("raw", self.ready_wrapper).read_planning_state()
        self.assertIsNone(state.next_command)
        for value in (
            state.artifacts.coding_prompt,
            state.artifacts.self_report,
            state.frontier.slice_id if state.frontier else "",
        ):
            if value:
                self.assertNotIn(":", value)
                self.assertNotIn(chr(92), value)


class CaptureIdentityTests(ProviderTestCase):
    def test_reads_use_unique_monotonic_capture_identities(self):
        provider = self.provider("raw", self.ready_wrapper)
        provider.read_planning_state()
        provider.read_planning_state()
        names = sorted(p.name for p in self.captures.iterdir())
        self.assertEqual(
            names,
            [
                "status_001_stderr.txt",
                "status_001_stdout.txt",
                "status_002_stderr.txt",
                "status_002_stdout.txt",
            ],
        )

    def test_a_fresh_process_preserves_a_retained_stdout_member(self):
        self.captures.mkdir(parents=True)
        earlier = self.captures / "status_001_stdout.txt"
        earlier.write_bytes(b"earlier observation\n")
        self.provider("raw", self.ready_wrapper).read_planning_state()
        self.assertEqual(earlier.read_bytes(), b"earlier observation\n")
        self.assertTrue((self.captures / "status_002_stdout.txt").is_file())
        self.assertTrue((self.captures / "status_002_stderr.txt").is_file())

    def test_a_fresh_process_preserves_a_retained_stderr_only_member(self):
        # R1-F4 regression (round two): the allocator scans both member
        # names, so stderr-only residue is preserved and the new read
        # publishes a complete pair at the next index.
        self.captures.mkdir(parents=True)
        earlier = self.captures / "status_001_stderr.txt"
        earlier.write_bytes(b"retained stderr-only observation\n")
        self.provider("raw", self.ready_wrapper).read_planning_state()
        self.assertEqual(
            earlier.read_bytes(), b"retained stderr-only observation\n",
            "a retained stderr-only member must never be clobbered",
        )
        self.assertTrue((self.captures / "status_002_stdout.txt").is_file())
        self.assertFalse((self.captures / "status_001_stdout.txt").exists())

    def test_a_fresh_process_preserves_a_retained_complete_pair(self):
        self.captures.mkdir(parents=True)
        stdout_member = self.captures / "status_001_stdout.txt"
        stderr_member = self.captures / "status_001_stderr.txt"
        stdout_member.write_bytes(b"retained stdout\n")
        stderr_member.write_bytes(b"retained stderr\n")
        self.provider("raw", self.ready_wrapper).read_planning_state()
        self.assertEqual(stdout_member.read_bytes(), b"retained stdout\n")
        self.assertEqual(stderr_member.read_bytes(), b"retained stderr\n")
        self.assertTrue((self.captures / "status_002_stdout.txt").is_file())

    def test_unreasonably_long_numeric_filename_is_handled_without_raw_error(self):
        # R1-F4: digit runs beyond the bounded index grammar never reach an
        # integer conversion; the creatable 30-digit probe drives the
        # over-bound branch and the odd member is preserved.
        self.captures.mkdir(parents=True)
        huge = self.captures / f"status_{'9' * 30}_stdout.txt"
        huge.write_bytes(b"oddly named retained bytes\n")
        state = self.provider("raw", self.ready_wrapper).read_planning_state()
        self.assertEqual(state.outcome, PlanOutcome.READY)
        self.assertEqual(huge.read_bytes(), b"oddly named retained bytes\n")
        self.assertTrue((self.captures / "status_001_stdout.txt").is_file())


class RefusalBeforeEffectTests(ProviderTestCase):
    """Every transport refusal precedes executor calls, attempt creation,
    verifier dispatch, and project mutation — proven through the real
    supervisor with the provider injected."""

    def scenario_with_provider(self, provider):
        scenario = Scenario(self.dir, states=clean_pass_states())
        scenario.supervisor._plan_provider = provider
        return scenario

    def assert_refused_without_effect(self, stub_args, code, argv=None,
                                      runner=None):
        provider = self.provider(
            *stub_args, argv=argv, runner=runner,
            captures=self.captures / code,
        )
        scenario = self.scenario_with_provider(provider)
        manager = WorkspaceManager(scenario.project, scenario.store.root)
        before = manager.snapshot(scenario.project)
        result = scenario.supervisor.tick()
        self.assertEqual(result.kind, "refused")
        self.assertEqual(result.detail, code)
        self.assertEqual(scenario.store.list_slices("run_001"), ())
        self.assertEqual(scenario.store.list_escalations("run_001"), ())
        kinds = scenario.event_kinds()
        self.assertNotIn("dispatch", kinds)
        self.assertNotIn("verification", kinds)
        self.assertNotIn("verb", kinds)
        self.assertEqual(manager.snapshot(scenario.project), before)

    def test_nonzero_exit_refuses_before_any_effect(self):
        self.assert_refused_without_effect(
            ("nonzero",), "frutlups_exit_nonzero"
        )

    def test_missing_frontier_member_refuses_before_any_effect(self):
        self.assert_refused_without_effect(
            ("missing-frontier-member",), "planning_frontier_member_missing"
        )

    def test_missing_executable_refuses_before_any_effect(self):
        self.assert_refused_without_effect(
            (),
            "frutlups_executable_missing",
            argv=(str(self.dir / "no_such_frutlups.exe"), "status"),
            runner=RecordingRunner(),
        )


if __name__ == "__main__":
    unittest.main()
