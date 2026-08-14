"""Generic bounded subprocess agent executor lanes (M003 Phase A, round 2).

Real local stub processes run only through the accepted production
``SubprocessRunner``; fakes implementing the declared ``ProcessRunner``
subset drive the spawn-failure and pre-spawn refusal branches. Nothing here
spawns directly, parses agent output for semantics, or sleeps.

Round-two reviewer-literal regressions (R1-F1/R1-F2) are final-form: the
``make_spec`` bridge builds specs with corrected-shape arguments only, and —
when run against product bytes that still expose the refuted round-one
``cwd`` field or reject ``command_id`` — points the duplicated cwd authority
at the test's adversarial target and drops the unsupported argument, so the
pre-fix red executes the exact causal branch the round-one review reported.
"""

import inspect
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

from frutlups_drive.dispatch.subprocess_agent import (
    AgentCommandSpec,
    CommandObservation,
    SubprocessAgentExecutor,
    SubprocessAgentFailure,
    observe_command,
)
from frutlups_drive.verifier import SubprocessRunner

from _scenario import FakeClock
from test_contracts import make_request

EXPECTED = Path("05_governance/reviews/m001/m001_s01_self_report.md")

_SPEC_PARAMETERS = frozenset(inspect.signature(AgentCommandSpec).parameters)


def make_spec(legacy_cwd=None, **kwargs):
    """Build a spec from corrected-shape arguments only.

    Against corrected product bytes this is a plain construction and
    ``legacy_cwd`` is ignored because no spec cwd authority exists. Against
    the refuted round-one bytes it supplies the then-required duplicated
    ``cwd`` (pointed at the adversarial ``legacy_cwd`` target) and drops the
    then-unknown ``command_id``, so the causal red branches run for real.
    """
    if "cwd" in _SPEC_PARAMETERS:
        kwargs.setdefault(
            "cwd", legacy_cwd if legacy_cwd is not None else Path(".")
        )
    kwargs = {
        name: value
        for name, value in kwargs.items()
        if name in _SPEC_PARAMETERS
    }
    return AgentCommandSpec(**kwargs)


class RecordingRunner:
    """Fake of the declared ProcessRunner subset: records calls; optional
    scripted raise models a spawn failure or exhausted transport."""

    def __init__(self, error=None):
        self.calls = []
        self._error = error

    def run(self, argv, cwd, env, timeout_seconds, stdout_path, stderr_path,
            max_stream_bytes=1_048_576):
        self.calls.append(
            {
                "argv": tuple(argv),
                "cwd": Path(cwd),
                "env": env,
                "timeout_seconds": timeout_seconds,
                "max_stream_bytes": max_stream_bytes,
            }
        )
        if self._error is not None:
            raise self._error
        raise AssertionError("scripted runner has no success path")


class SpecValidationTests(unittest.TestCase):
    def spec(self, **kwargs):
        base = dict(
            argv=(sys.executable, "-c", "print('ok')"),
            command_id="stub-agent",
        )
        base.update(kwargs)
        return make_spec(**base)

    def test_valid_spec_normalizes(self):
        spec = self.spec(timeout_seconds=5)
        self.assertEqual(spec.timeout_seconds, 5.0)

    def test_spec_has_no_cwd_authority(self):
        # R1-F1: the corrected spec carries no duplicated cwd field; the
        # request workspace is the only execution-directory authority.
        self.assertNotIn("cwd", _SPEC_PARAMETERS)
        self.assertIn("command_id", _SPEC_PARAMETERS)

    def test_argv_must_be_nonempty_string_tuple_with_absolute_head(self):
        for bad in (
            (),
            ("",),
            ("python", "-c", "x"),          # relative head: PATH discovery
            (123,),
            [sys.executable],               # list, not tuple
            (sys.executable, None),
        ):
            with self.subTest(argv=repr(bad)):
                with self.assertRaises(ValueError):
                    self.spec(argv=bad)

    def test_timeout_must_be_positive_finite_plain_number(self):
        cases = (
            ("zero", 0), ("negative", -1), ("infinity", float("inf")),
            ("nan", float("nan")), ("boolean", True), ("string", "5"),
            ("none", None), ("positive_401_digit", 10**400),
            ("negative_401_digit", -(10**400)),
            ("positive_10001_digit", 10**10_000),
            ("negative_10001_digit", -(10**10_000)),
        )
        for label, bad in cases:
            with self.subTest(value=label):
                with self.assertRaises(ValueError):
                    self.spec(timeout_seconds=bad)

    def test_stream_bound_is_type_and_range_strict(self):
        for bad in (0, -1, 1_048_577, True, "1024", 10.5):
            with self.subTest(value=repr(bad)):
                with self.assertRaises(ValueError):
                    self.spec(max_stream_bytes=bad)

    def test_env_default_is_the_explicit_empty_environment(self):
        self.assertEqual(self.spec().env, ())

    def test_env_rejects_none_duplicates_and_invalid_pairs(self):
        # R1-F1: ambient inheritance is not representable; None is refused
        # at construction, and pairs are exact finite name/value strings.
        for bad in (
            None,
            ((b"NAME", "v"),),
            (("", "v"),),
            (("NA=ME", "v"),),
            (("NAME", 3),),
            [("NAME", "v")],
            (("NAME",),),
            (("NAME", "v"), ("NAME", "w")),           # duplicate name
            (("NA\x00ME", "v"),),                     # NUL in name
            (("NAME", "va\x00lue"),),                 # NUL in value
        ):
            with self.subTest(env=repr(bad)[:40]):
                with self.assertRaises(ValueError):
                    self.spec(env=bad)
        self.assertIsNotNone(self.spec(env=(("NAME", "value"),)))
        self.assertIsNotNone(self.spec(env=()))

    def test_command_id_grammar_is_closed_and_bounded(self):
        for good in ("agent-stub", "A", "stub.helper_01", "a" * 64):
            with self.subTest(value=good):
                self.assertEqual(self.spec(command_id=good).command_id, good)
        for bad in ("", "a" * 65, "with space", "with/slash",
                    "with\\backslash", "m\u00fcnchen", "tab\tchar", None, 3):
            with self.subTest(value=repr(bad)):
                with self.assertRaises(ValueError):
                    self.spec(command_id=bad)


class ExecutorTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.workspace = self.dir / "ws"
        self.workspace.mkdir()
        self.log_root = self.dir / "agent_logs"
        self.clock = FakeClock()
        baseline = threading.active_count()
        self.addCleanup(
            lambda: self.assertEqual(
                threading.active_count(), baseline,
                "no background thread may outlive an execution",
            )
        )
        hooks = []
        real_hook = threading.excepthook
        threading.excepthook = lambda args: hooks.append(args.exc_type.__name__)
        self.addCleanup(setattr, threading, "excepthook", real_hook)
        self.hooks = hooks

    def executor(self, code=None, *, argv=None, runner=None, legacy_cwd=None,
                 **spec_kwargs):
        spec_argv = argv if argv is not None else (
            sys.executable, "-c", code
        )
        spec_kwargs.setdefault("timeout_seconds", 30.0)
        spec_kwargs.setdefault("command_id", "stub-agent")
        spec = make_spec(
            argv=tuple(spec_argv),
            legacy_cwd=legacy_cwd if legacy_cwd is not None else self.workspace,
            **spec_kwargs,
        )
        return SubprocessAgentExecutor(
            spec, runner or SubprocessRunner(self.clock), self.log_root
        )

    def request(self, **overrides):
        overrides.setdefault("workspace", self.workspace)
        overrides.setdefault("expected_artifacts", (EXPECTED,))
        overrides.setdefault("adapter", "mock")
        overrides.setdefault("model", "seat-model-1")
        return make_request(**overrides)

    def event_lines(self, result_or_path):
        path = (
            Path(result_or_path.event_log_path)
            if hasattr(result_or_path, "event_log_path")
            else Path(result_or_path)
        )
        raw = path.read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"\r", raw)
        return [
            json.loads(line)
            for line in raw.decode("utf-8").splitlines()
        ]

    def assert_no_semantic_facts(self, result):
        self.assertIsNone(result.tokens_in)
        self.assertIsNone(result.tokens_out)
        self.assertIsNone(result.cost_usd)
        self.assertEqual(result.changed_files, ())


class ExecutorAuthorityTests(ExecutorTestCase):
    """R1-F1 reviewer-literal regressions: explicit child environment and
    request-derived workspace authority."""

    SENTINEL_NAME = "FRUTLUPS_REVIEW_SECRET_SENTINEL"
    SENTINEL_VALUE = "ambient-credential-value-must-not-inherit"

    def test_undeclared_ambient_variable_never_reaches_the_child(self):
        # Environment restoration opens in `finally` before the sentinel is
        # set. The spec declares no environment, which means an explicitly
        # empty child environment — never ambient inheritance.
        executor = self.executor(
            "import os; print(os.environ.get("
            f"{self.SENTINEL_NAME!r}, 'sentinel-absent'))"
        )
        original = os.environ.get(self.SENTINEL_NAME)
        try:
            os.environ[self.SENTINEL_NAME] = self.SENTINEL_VALUE
            result = executor.execute(self.request())
        finally:
            if original is None:
                os.environ.pop(self.SENTINEL_NAME, None)
            else:
                os.environ[self.SENTINEL_NAME] = original
        self.assertEqual(result.status, "completed")
        captured = (
            self.log_root / "run-001_attempt_001_stdout.txt"
        ).read_bytes()
        self.assertIn(b"sentinel-absent", captured)
        self.assertNotIn(
            self.SENTINEL_VALUE.encode("utf-8"), captured,
            "an undeclared ambient parent variable reached the child",
        )

    def test_declared_pairs_are_the_complete_child_environment(self):
        executor = self.executor(
            "import os; print('count', len(os.environ)); "
            "print('marker', os.environ.get('STUB_AGENT_MARKER', 'absent'))",
            env=(("STUB_AGENT_MARKER", "declared-marker-value"),),
        )
        result = executor.execute(self.request())
        self.assertEqual(result.status, "completed")
        captured = (
            self.log_root / "run-001_attempt_001_stdout.txt"
        ).read_bytes()
        self.assertIn(b"count 1", captured)
        self.assertIn(b"marker declared-marker-value", captured)

    def test_relative_write_lands_only_in_the_request_workspace(self):
        # The adversarial legacy_cwd points any residual spec cwd authority
        # at a sibling directory outside the request workspace; the corrected
        # executor must derive cwd from the request alone.
        sibling = self.dir / "outside"
        sibling.mkdir()
        sibling_before = sorted(p.name for p in sibling.iterdir())
        executor = self.executor(
            "open('confined-write.txt', 'w').write('agent write')",
            legacy_cwd=sibling,
        )
        result = executor.execute(self.request())
        self.assertEqual(result.status, "completed")
        self.assertTrue(
            (self.workspace / "confined-write.txt").is_file(),
            "the relative write must land in the request workspace",
        )
        self.assertEqual(
            sorted(p.name for p in sibling.iterdir()),
            sibling_before,
            "a non-request sibling directory must remain member-identical",
        )

    def test_missing_request_workspace_refuses_before_log_or_spawn(self):
        runner = RecordingRunner()
        executor = self.executor("print('never runs')", runner=runner)
        missing = self.dir / "no_such_workspace"
        with self.assertRaises(SubprocessAgentFailure) as caught:
            executor.execute(self.request(workspace=missing))
        self.assertEqual(caught.exception.code, "workspace_missing")
        self.assertNotIn(str(self.dir), str(caught.exception))
        self.assertEqual(runner.calls, [], "nothing may spawn")
        self.assertFalse(self.log_root.exists(), "no log root is created")

    def test_executor_source_never_consults_the_ambient_environment(self):
        import frutlups_drive.dispatch.subprocess_agent as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("os.environ", source)


class ExecutorOutcomeTests(ExecutorTestCase):
    def test_clean_exit_completes_with_bounded_facts(self):
        executor = self.executor("print('agent-ran')")
        result = executor.execute(self.request())
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.exit_reason, "agent_exit_clean")
        self.assert_no_semantic_facts(result)
        self.assertEqual(result.produced_artifacts, ())
        lines = self.event_lines(result)
        self.assertEqual(lines[1]["exit_code"], 0)
        self.assertEqual(self.hooks, [])

    def test_nonzero_exit_is_a_failed_fact_not_an_exception(self):
        executor = self.executor("import sys; sys.exit(3)")
        result = executor.execute(self.request())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.exit_reason, "agent_exit_nonzero")
        self.assertEqual(self.event_lines(result)[1]["exit_code"], 3)

    def test_timeout_is_bounded_and_owned(self):
        executor = self.executor(
            "print('agent-ready', flush=True); "
            "import threading; threading.Event().wait(60)",
            timeout_seconds=3.0,
        )
        result = executor.execute(self.request())
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.exit_reason, "agent_timeout")
        self.assertEqual(self.event_lines(result)[1]["kind"], "timeout")

    def test_stdout_overflow_fails_with_bounded_capture(self):
        executor = self.executor(
            "import sys; sys.stdout.write('o' * 4096); sys.stdout.flush()",
            max_stream_bytes=1024,
        )
        result = executor.execute(self.request())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.exit_reason, "agent_stream_overflow")
        stdout_capture = self.log_root / (
            "run-001_attempt_001_stdout.txt"
        )
        self.assertLessEqual(stdout_capture.stat().st_size, 1024)
        self.assertTrue(self.event_lines(result)[1]["stdout_overflow"])

    def test_stderr_overflow_fails_with_bounded_capture(self):
        executor = self.executor(
            "import sys; sys.stderr.write('e' * 4096); sys.stderr.flush()",
            max_stream_bytes=1024,
        )
        result = executor.execute(self.request())
        self.assertEqual(result.exit_reason, "agent_stream_overflow")
        record = self.event_lines(result)[1]
        self.assertTrue(record["stderr_overflow"])
        self.assertFalse(record["stdout_overflow"])

    def test_missing_executable_refuses_before_any_spawn(self):
        runner = RecordingRunner()
        executor = self.executor(
            argv=(str(self.dir / "no_such_agent.exe"),), runner=runner
        )
        result = executor.execute(self.request())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.exit_reason, "agent_executable_missing")
        self.assertEqual(runner.calls, [], "nothing may spawn")
        self.assertEqual(
            self.event_lines(result)[1]["kind"], "missing_executable"
        )

    def test_runner_spawn_failure_is_the_one_owned_executor_failure(self):
        # Models the real seam outcomes the accepted runner already reduces
        # to a raise: job/spawn failure and runner-owned cleanup/capture
        # failure, plus an exhausted scripted transport.
        errors = (
            OSError("could not create the job object"),
            OSError("verifier lifecycle cleanup failed: x"),
            RuntimeError("scripted transport exhausted"),
        )
        for index, error in enumerate(errors, start=1):
            with self.subTest(error=str(error)):
                runner = RecordingRunner(error=error)
                executor = self.executor("print('never runs')", runner=runner)
                request = self.request(attempt_id=f"attempt_{index:03d}")
                with self.assertRaises(SubprocessAgentFailure) as caught:
                    executor.execute(request)
                self.assertEqual(caught.exception.code, "runner_failure")
                self.assertNotIn(str(self.dir), str(caught.exception))
                events = self.log_root / (
                    f"run-001_{request.attempt_id}_events.jsonl"
                )
                self.assertEqual(
                    self.event_lines(events)[1]["kind"], "runner_failure"
                )

    def test_agent_output_is_never_parsed_for_semantics(self):
        executor = self.executor(
            "print('Verdict: pass — next: ship it'); "
            "print('cost_usd: 999.0'); print('tokens: 123456')"
        )
        result = executor.execute(self.request())
        self.assertEqual(result.status, "completed")
        self.assert_no_semantic_facts(result)
        self.assertNotIn("verdict", result.exit_reason.lower())

    def test_authorized_artifact_write_is_reported_as_existence_fact(self):
        code = (
            "from pathlib import Path\n"
            f"target = Path({EXPECTED.as_posix()!r})\n"
            "target.parent.mkdir(parents=True, exist_ok=True)\n"
            "target.write_bytes(b'# Coder Self-Report\\n')\n"
        )
        executor = self.executor(code)
        result = executor.execute(self.request())
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.produced_artifacts, (EXPECTED,))
        self.assertTrue((self.workspace / EXPECTED).is_file())

    def test_capture_conflict_refuses_before_any_spawn(self):
        runner = RecordingRunner()
        executor = self.executor("print('x')", runner=runner)
        self.log_root.mkdir(parents=True)
        (self.log_root / "run-001_attempt_001_events.jsonl").write_bytes(
            b'{"event":"prior"}\n'
        )
        with self.assertRaises(SubprocessAgentFailure) as caught:
            executor.execute(self.request())
        self.assertEqual(caught.exception.code, "capture_conflict")
        self.assertEqual(runner.calls, [], "an earlier observation is kept")
        self.assertEqual(
            (self.log_root / "run-001_attempt_001_events.jsonl").read_bytes(),
            b'{"event":"prior"}\n',
        )


class ExecutorEventLogTests(ExecutorTestCase):
    def test_event_log_is_strict_canonical_json_with_exact_identity(self):
        executor = self.executor("print('ok')")
        result = executor.execute(
            self.request(adapter="mock", model="seat-model-1")
        )
        lines = self.event_lines(result)
        self.assertEqual(len(lines), 2)
        dispatch = lines[0]
        self.assertEqual(dispatch["event"], "agent_dispatch")
        self.assertEqual(dispatch["run_id"], "run-001")
        self.assertEqual(dispatch["attempt_id"], "attempt_001")
        self.assertEqual(dispatch["role"], "coder")
        self.assertEqual(dispatch["adapter"], "mock")
        self.assertEqual(dispatch["model"], "seat-model-1")
        self.assertEqual(dispatch["command_id"], "stub-agent")
        self.assertEqual(dispatch["argument_count"], 3)
        self.assertEqual(dispatch["env_names"], [])
        self.assertNotIn("argv", dispatch)

    def test_durable_event_contains_no_path_argument_or_value_sentinels(self):
        # R1-F2 reviewer-literal regression: an absolute interpreter plus
        # distinctive path/argument/value sentinels; the parsed event carries
        # only the approved facts and neither raw nor parsed content
        # reconstructs paths, arguments, or values.
        argument_sentinel = "sentinel-argument-payload-DO-NOT-LOG"
        value_sentinel = "api_key=sentinel-value-DO-NOT-LOG"
        executor = self.executor(
            argv=(sys.executable, "-c", "print('ok')",
                  argument_sentinel, value_sentinel),
            env=(("SENTINEL_NAME_ONLY", "env-value-DO-NOT-LOG"),),
        )
        result = executor.execute(self.request())
        self.assertEqual(result.status, "completed")
        raw = Path(result.event_log_path).read_bytes().decode("utf-8")
        interpreter = str(Path(sys.executable))
        for forbidden in (
            interpreter,
            interpreter.replace("\\", "\\\\"),
            interpreter.replace("\\", "/"),
            Path(sys.executable).name,
            str(self.workspace),
            str(self.workspace).replace("\\", "\\\\"),
            argument_sentinel,
            value_sentinel,
            "env-value-DO-NOT-LOG",
        ):
            self.assertNotIn(forbidden, raw)
        dispatch = self.event_lines(result)[0]
        self.assertEqual(dispatch["command_id"], "stub-agent")
        self.assertEqual(dispatch["argument_count"], 5)
        self.assertEqual(dispatch["env_names"], ["SENTINEL_NAME_ONLY"])
        self.assertNotIn("argv", dispatch)
        for value in dispatch.values():
            if isinstance(value, str):
                self.assertNotIn("DO-NOT-LOG", value)

    def test_env_names_are_recorded_without_values(self):
        executor = self.executor(
            "import os; print(os.environ.get('STUB_AGENT_MARKER', 'absent'))",
            env=(("STUB_AGENT_MARKER", "marker-value-do-not-log"),),
        )
        result = executor.execute(self.request())
        self.assertEqual(result.status, "completed")
        raw = Path(result.event_log_path).read_bytes().decode("utf-8")
        self.assertIn("STUB_AGENT_MARKER", raw)
        self.assertNotIn("marker-value-do-not-log", raw)
        stdout_capture = self.log_root / "run-001_attempt_001_stdout.txt"
        self.assertIn(b"marker-value-do-not-log", stdout_capture.read_bytes())

    def test_effective_timeout_is_the_smaller_of_spec_and_request(self):
        executor = self.executor("print('ok')", timeout_seconds=300.0)
        result = executor.execute(self.request(max_seconds=7))
        self.assertEqual(
            self.event_lines(result)[0]["timeout_seconds"], 7.0
        )
        other = self.executor("print('ok')", timeout_seconds=2.0)
        second = other.execute(
            self.request(attempt_id="attempt_002", max_seconds=600)
        )
        self.assertEqual(
            self.event_lines(second)[0]["timeout_seconds"], 2.0
        )


class ObserveCommandTests(ExecutorTestCase):
    def test_observation_kinds_are_the_declared_finite_set(self):
        self.assertEqual(
            CommandObservation("exit", exit_code=0).kind, "exit"
        )
        observation = observe_command(
            (str(self.dir / "missing.exe"),),
            self.workspace,
            {},
            5.0,
            RecordingRunner(),
            self.dir / "out.txt",
            self.dir / "err.txt",
        )
        self.assertEqual(observation.kind, "missing_executable")

    def test_runner_exception_becomes_a_bounded_classification(self):
        observation = observe_command(
            (sys.executable, "-c", "print('x')"),
            self.workspace,
            {},
            5.0,
            RecordingRunner(error=OSError("boom with C:/secret/path")),
            self.dir / "out.txt",
            self.dir / "err.txt",
        )
        self.assertEqual(observation.kind, "runner_failure")


if __name__ == "__main__":
    unittest.main()
