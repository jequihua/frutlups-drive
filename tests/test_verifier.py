"""Verifier lanes: evidence shape, provenance, dirty rule, bounded capture,
and runner-owned process-tree lifecycle.

Process ownership is exercised only through the production
:class:`SubprocessRunner` (R2-F4): tests spawn nothing directly, own no second
termination algorithm, and observe the lifecycle causally — captured stream
bytes, exit-status records, exclusively held lock files that become removable
only when every owned process is gone, and an observer around the production
termination boundary.
"""

import ast
import os
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

from frutlups_drive.runstore import RunStore, RunStoreRefusal
from frutlups_drive.verifier import (
    SubprocessRunner,
    VerificationCommand,
    VerificationPlan,
    Verifier,
)

from _scenario import FakeClock, FakeProcessRunner

MANIFEST = {"boundary": "slice_complete", "contract_version": 1}


class SideEffectRunner(FakeProcessRunner):
    """Fake runner that can mutate the workspace like a careless command."""

    def __init__(self, clock, effects=None, **kwargs):
        super().__init__(clock, **kwargs)
        self.effects = list(effects or [])

    def run(self, argv, cwd, env, timeout_seconds, stdout_path, stderr_path,
            max_stream_bytes=1_048_576):
        if self.calls < len(self.effects) and self.effects[self.calls] is not None:
            self.effects[self.calls]()
        return super().run(
            argv, cwd, env, timeout_seconds, stdout_path, stderr_path,
            max_stream_bytes=max_stream_bytes,
        )


class _HostileInt(int):
    """int subclass whose conversion hook must never run (R4-F1)."""

    def __float__(self):
        raise RuntimeError("conversion hook must never run")


class _HostileFloat(float):
    """float subclass whose conversion hook must never run (R4-F1)."""

    def __float__(self):
        raise RuntimeError("conversion hook must never run")


class FakeStreamFact:
    """Data-only fake pipe fact: scripted chunks, then EOF; can refuse
    read and/or close to drive the worker error channel."""

    def __init__(self, chunks=(), read_error=False, close_error=False):
        self._chunks = list(chunks)
        self._read_error = read_error
        self._close_error = close_error
        self.closed = False
        self.close_calls = 0

    def read(self, size):
        if self._read_error:
            raise OSError("read refused")
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    def close(self):
        self.close_calls += 1
        if self._close_error:
            raise OSError("close refused")
        self.closed = True


class FakeSpecStreamFact:
    """Fake anchor stdin recording spec delivery; can refuse it."""

    def __init__(self, events, fail=False):
        self._events = events
        self._fail = fail
        self.closed = False

    def write(self, data):
        if self._fail:
            raise OSError("spec delivery failed")
        self._events.append("spec_write")
        return len(data)

    def close(self):
        self.closed = True


class FakeProcessFact:
    """Fake process facts that drive the production lifecycle controllers.

    Any unbounded ``wait`` fails the test immediately: dangerous failure
    edges are proven with facts, never with a real orphanable process.
    ``kill_error``/``wait_error`` make the corresponding operation raise
    after being recorded, without marking the process dead.
    """

    def __init__(self, events, poll_script=None, stdout=None, stderr=None,
                 stdin=None, kill_error=False, wait_error=False):
        self._events = events
        self._poll_script = list(poll_script or [])
        self._kill_error = kill_error
        self._wait_error = wait_error
        self.pid = 4242
        self.stdin = stdin
        self.stdout = stdout if stdout is not None else FakeStreamFact()
        self.stderr = stderr if stderr is not None else FakeStreamFact()
        self.returncode = None
        self.poll_calls = 0
        self.kill_calls = 0
        self.wait_calls = []

    def poll(self):
        self.poll_calls += 1
        if self.returncode is None and self._poll_script:
            value = self._poll_script.pop(0)
            if value is not None:
                self.returncode = value
        return self.returncode

    def kill(self):
        self.kill_calls += 1
        self._events.append(("kill", self.returncode is None))
        if self._kill_error:
            raise RuntimeError("kill refused")

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        self._events.append(("reap", timeout))
        if timeout is None:
            raise AssertionError("an unbounded wait is forbidden")
        if self._wait_error:
            raise RuntimeError("reap refused")
        if self.returncode is None:
            self.returncode = 1
        return self.returncode


class FakeJobFact:
    """Fake Win32 job facts with scripted create/assign/terminate outcomes."""

    def __init__(self, events, create_error=False, assign_error=False,
                 terminate_result=True, close_result=True, close_error=False):
        self._events = events
        self._assign_error = assign_error
        self._terminate_result = terminate_result
        self._close_result = close_result
        self._close_error = close_error
        self.terminate_calls = 0
        self.close_calls = 0
        events.append("job_create")
        if create_error:
            raise OSError("could not create the verification job object")

    def assign(self, pid):
        self._events.append("job_assign")
        if self._assign_error:
            raise OSError("could not assign the anchor to the job")

    def terminate(self):
        self.terminate_calls += 1
        self._events.append(("job_terminate", self._terminate_result))
        return self._terminate_result

    def close(self):
        self.close_calls += 1
        self._events.append("job_close")
        if self._close_error:
            raise RuntimeError("synthetic close detail must stay bounded")
        return self._close_result


class FakeThreadFact:
    """Records that every drain-completion wait carries a bound."""

    def __init__(self, events):
        self._events = events
        self.join_calls = []

    def join(self, timeout=None):
        self.join_calls.append(timeout)
        self._events.append(("drain_join", timeout))

    def is_alive(self):
        return False


class VerifierTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.store = RunStore(self.dir / ".frutlups_drive")
        self.store.create_run("run_001", MANIFEST)
        self.attempt = self.store.create_attempt("run_001", "M001-S01")
        self.workspace = self.dir / "ws"
        self.workspace.mkdir()
        (self.workspace / "source.py").write_bytes(b"print('hello')\n")
        self.clock = FakeClock()

    def verifier(self, runner):
        return Verifier(self.store, runner, self.clock)

    def plan(self, commands=None, declared=()):
        return VerificationPlan(
            commands=tuple(
                commands
                or [VerificationCommand(argv=("validate",), timeout_seconds=30.0)]
            ),
            declared_regenerated=tuple(declared),
        )

    def record_excepthook(self):
        """Record any background thread exception; an owned worker failure
        must never reach threading.excepthook (R4-F2)."""
        hooks = []
        real = threading.excepthook
        threading.excepthook = lambda args: hooks.append(
            args.exc_type.__name__
        )
        self.addCleanup(setattr, threading, "excepthook", real)
        return hooks


class EvidenceTests(VerifierTestCase):
    def test_evidence_toml_is_complete_and_hash_addressed(self):
        runner = FakeProcessRunner(self.clock, exit_codes=[0])
        outcome = self.verifier(runner).verify(
            self.attempt, self.plan(), self.workspace, base_revision="baserev"
        )
        self.assertTrue(outcome.passed)
        evidence_path = self.attempt / "verification/evidence.toml"
        parsed = tomllib.loads(evidence_path.read_bytes().decode("utf-8"))
        self.assertTrue(parsed["passed"])
        self.assertEqual(parsed["base_revision"], "baserev")
        self.assertEqual(parsed["dirty_files"], [])
        command = parsed["command"][0]
        self.assertEqual(command["argv"], ["validate"])
        self.assertEqual(command["env_profile"], "default")
        self.assertEqual(command["exit_code"], 0)
        self.assertFalse(command["timed_out"])
        stdout_file = self.attempt / "verification/cmd_000_stdout.txt"
        import hashlib

        self.assertEqual(
            command["stdout_sha256"],
            hashlib.sha256(stdout_file.read_bytes()).hexdigest(),
        )
        raw = evidence_path.read_bytes()
        self.assertNotIn(b"\r", raw)

    def test_failing_command_fails_verification(self):
        runner = FakeProcessRunner(self.clock, exit_codes=[1])
        outcome = self.verifier(runner).verify(
            self.attempt, self.plan(), self.workspace
        )
        self.assertFalse(outcome.passed)

    def test_timeout_outcome_fails_and_is_recorded(self):
        runner = FakeProcessRunner(self.clock, timed_out=[True])
        outcome = self.verifier(runner).verify(
            self.attempt, self.plan(), self.workspace
        )
        self.assertFalse(outcome.passed)
        parsed = tomllib.loads(
            (self.attempt / "verification/evidence.toml").read_bytes().decode("utf-8")
        )
        self.assertTrue(parsed["command"][0]["timed_out"])
        self.assertEqual(parsed["command"][0]["exit_code"], -1)

    def test_publication_is_write_once(self):
        runner = FakeProcessRunner(self.clock, exit_codes=[0])
        self.verifier(runner).verify(self.attempt, self.plan(), self.workspace)
        with self.assertRaises(RunStoreRefusal):
            self.verifier(FakeProcessRunner(self.clock, exit_codes=[1])).verify(
                self.attempt, self.plan(), self.workspace
            )

    def test_unknown_env_profile_and_cwd_escape_refuse(self):
        runner = FakeProcessRunner(self.clock)
        with self.assertRaises(ValueError):
            self.verifier(runner).verify(
                self.attempt,
                self.plan([VerificationCommand(argv=("x",), env_profile="prod")]),
                self.workspace,
            )
        with self.assertRaises(ValueError):
            self.verifier(runner).verify(
                self.attempt,
                self.plan([VerificationCommand(argv=("x",), cwd="../outside")]),
                self.workspace,
            )


class DirtyRuleTests(VerifierTestCase):
    def test_undeclared_new_file_fails_verification(self):
        def pollute():
            (self.workspace / "junk.tmp.txt").write_bytes(b"junk\n")

        runner = SideEffectRunner(self.clock, effects=[pollute], exit_codes=[0])
        outcome = self.verifier(runner).verify(
            self.attempt, self.plan(), self.workspace
        )
        self.assertFalse(outcome.passed)
        self.assertIn("junk.tmp.txt", outcome.dirty_files)

    def test_declared_byte_identical_regeneration_is_tolerated(self):
        target = self.workspace / "generated_navigation.md"
        target.write_bytes(b"# Navigation\n")

        def regenerate():
            os.utime(target, ns=(1, 1))  # rewrite metadata, identical bytes

        runner = SideEffectRunner(self.clock, effects=[regenerate], exit_codes=[0])
        outcome = self.verifier(runner).verify(
            self.attempt,
            self.plan(declared=("generated_navigation.md",)),
            self.workspace,
        )
        self.assertTrue(outcome.passed)
        self.assertEqual(outcome.dirty_files, ("generated_navigation.md",))

    def test_declared_file_with_changed_content_still_fails(self):
        target = self.workspace / "generated_navigation.md"
        target.write_bytes(b"# Navigation\n")

        def corrupt():
            target.write_bytes(b"# Different navigation\n")

        runner = SideEffectRunner(self.clock, effects=[corrupt], exit_codes=[0])
        outcome = self.verifier(runner).verify(
            self.attempt,
            self.plan(declared=("generated_navigation.md",)),
            self.workspace,
        )
        self.assertFalse(outcome.passed)


class BoundedCaptureTests(VerifierTestCase):
    """R1-F4 regressions: per-stream capture is physically bounded while the
    process runs; overflow fails verification with honest evidence."""

    def emit(self, stream, count):
        target = "sys.stdout" if stream == "stdout" else "sys.stderr"
        return (
            sys.executable,
            "-c",
            f"import sys; {target}.write('a' * {count}); {target}.flush()",
        )

    def run_real(self, argv, **command_kwargs):
        runner = SubprocessRunner(self.clock)
        plan = self.plan(
            [VerificationCommand(argv=argv, timeout_seconds=120.0,
                                 **command_kwargs)]
        )
        return self.verifier(runner).verify(self.attempt, plan, self.workspace)

    def evidence_command(self):
        return tomllib.loads(
            (self.attempt / "verification/evidence.toml").read_bytes().decode(
                "utf-8"
            )
        )["command"][0]

    def test_default_one_mib_bound_fails_two_mib_stdout(self):
        outcome = self.run_real(self.emit("stdout", 2_097_152))
        self.assertFalse(outcome.passed)
        stdout_file = self.attempt / "verification/cmd_000_stdout.txt"
        self.assertLessEqual(stdout_file.stat().st_size, 1_048_576)
        record = self.evidence_command()
        self.assertTrue(record["stdout_overflow"])
        self.assertEqual(record["stdout_captured_bytes"], 1_048_576)

    def test_stderr_overflow_fails_with_honest_evidence(self):
        outcome = self.run_real(self.emit("stderr", 4096),
                                max_stream_bytes=1024)
        self.assertFalse(outcome.passed)
        record = self.evidence_command()
        self.assertTrue(record["stderr_overflow"])
        self.assertFalse(record["stdout_overflow"])
        self.assertEqual(record["stderr_captured_bytes"], 1024)

    def test_simultaneous_stream_overflow_is_bounded(self):
        code = (
            "import sys\n"
            "sys.stdout.write('o' * 4096); sys.stdout.flush()\n"
            "sys.stderr.write('e' * 4096); sys.stderr.flush()\n"
        )
        outcome = self.run_real((sys.executable, "-c", code),
                                max_stream_bytes=1024)
        self.assertFalse(outcome.passed)
        for name in ("cmd_000_stdout.txt", "cmd_000_stderr.txt"):
            self.assertLessEqual(
                (self.attempt / "verification" / name).stat().st_size, 1024
            )

    def test_asymmetric_overflow_flags_only_the_overflowing_stream(self):
        # stderr stays within its bound and is written first, so its capture
        # is complete even though stdout overflow terminates the tree.
        code = (
            "import sys\n"
            "sys.stderr.write('e' * 512); sys.stderr.flush()\n"
            "sys.stdout.write('o' * 2048); sys.stdout.flush()\n"
        )
        outcome = self.run_real((sys.executable, "-c", code),
                                max_stream_bytes=1024)
        self.assertFalse(outcome.passed)
        record = self.evidence_command()
        self.assertTrue(record["stdout_overflow"])
        self.assertFalse(record["stderr_overflow"])
        self.assertEqual(record["stderr_captured_bytes"], 512)

    def test_exact_boundary_passes_without_overflow(self):
        outcome = self.run_real(self.emit("stdout", 1024),
                                max_stream_bytes=1024)
        self.assertTrue(outcome.passed)
        record = self.evidence_command()
        self.assertFalse(record["stdout_overflow"])
        self.assertEqual(record["stdout_captured_bytes"], 1024)

    def test_stderr_exact_boundary_passes_without_overflow(self):
        outcome = self.run_real(self.emit("stderr", 1024),
                                max_stream_bytes=1024)
        self.assertTrue(outcome.passed)
        record = self.evidence_command()
        self.assertFalse(record["stderr_overflow"])
        self.assertEqual(record["stderr_captured_bytes"], 1024)

    def test_one_byte_over_fails(self):
        outcome = self.run_real(self.emit("stdout", 1025),
                                max_stream_bytes=1024)
        self.assertFalse(outcome.passed)
        record = self.evidence_command()
        self.assertTrue(record["stdout_overflow"])

    def test_stderr_one_byte_over_fails(self):
        outcome = self.run_real(self.emit("stderr", 1025),
                                max_stream_bytes=1024)
        self.assertFalse(outcome.passed)
        record = self.evidence_command()
        self.assertTrue(record["stderr_overflow"])

    def test_ordinary_small_output_still_passes(self):
        outcome = self.run_real((sys.executable, "-c", "print('tiny')"))
        self.assertTrue(outcome.passed)
        record = self.evidence_command()
        self.assertFalse(record["stdout_overflow"])
        self.assertGreater(record["stdout_captured_bytes"], 0)

    def test_overflowing_process_tree_is_terminated(self):
        # Parent spawns a child sharing stdout; both write forever. The drain
        # ends only at pipe EOF, which requires every writer to be dead.
        parent_code = (
            "import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-c', "
            "\"import sys\\nwhile True: sys.stdout.write('c' * 65536); "
            "sys.stdout.flush()\"])\n"
            "while True:\n"
            "    sys.stdout.write('p' * 65536); sys.stdout.flush()\n"
        )
        outcome = self.run_real((sys.executable, "-c", parent_code),
                                max_stream_bytes=4096)
        self.assertFalse(outcome.passed)
        record = self.evidence_command()
        self.assertTrue(record["stdout_overflow"])
        self.assertEqual(record["stdout_captured_bytes"], 4096)

    def test_capture_bound_field_is_type_and_range_strict(self):
        for bad in (True, 0, -1, 1_048_577, "1024", 10.5):
            with self.subTest(value=repr(bad)):
                with self.assertRaises(ValueError):
                    VerificationCommand(
                        argv=("x",), max_stream_bytes=bad
                    )


class RunnerOwnedProcessTests(VerifierTestCase):
    """R2-F3/R2-F4: the production runner is the only process owner; tests
    observe the lifecycle causally and implement no termination of their own.

    Lock files are held open by helper descendants without delete sharing, so
    ``os.remove`` succeeds only after every holder is dead. Every helper that
    waits does so on a blocking event with a bounded leak-guard fallback; the
    green path always terminates it through the runner first.
    """

    def setUp(self):
        super().setUp()
        baseline = threading.active_count()
        self.addCleanup(
            lambda: self.assertEqual(
                threading.active_count(), baseline,
                "drain threads must be joined before the runner returns",
            )
        )

    def run_direct(self, argv, timeout_seconds=30.0,
                   max_stream_bytes=1_048_576):
        """Run through the production runner inside an owned temporary root
        whose cleanup boundary opens before the runner call."""
        runner = SubprocessRunner(self.clock)
        with tempfile.TemporaryDirectory(dir=self.dir) as scratch:
            out = Path(scratch) / "out.txt"
            err = Path(scratch) / "err.txt"
            outcome = runner.run(
                tuple(argv), self.workspace, None, timeout_seconds, out, err,
                max_stream_bytes=max_stream_bytes,
            )
            residue = sorted(
                p.name
                for p in Path(scratch).iterdir()
                if p.name not in ("out.txt", "err.txt")
            )
            return outcome, out.read_bytes(), err.read_bytes(), residue

    def observe_lifecycle(self):
        """Wrap the production authority seams with recorders; the
        production controllers still perform every operation themselves.
        ``("job_terminate", live)`` records whether the spawned anchor was
        still live when the job authority ended the tree."""
        import frutlups_drive.verifier as verifier_module

        events = []
        anchor = {}
        real_spawn = verifier_module._spawn_anchor

        def recording_spawn(cwd):
            process = real_spawn(cwd)
            anchor["process"] = process
            return process

        verifier_module._spawn_anchor = recording_spawn
        self.addCleanup(setattr, verifier_module, "_spawn_anchor", real_spawn)

        real_job = verifier_module._WindowsKillOnCloseJob
        if real_job is not None:
            class RecordingJob(real_job):
                def terminate(job_self):
                    process = anchor.get("process")
                    live = process is not None and process.poll() is None
                    events.append(("job_terminate", live))
                    return super().terminate()

                def close(job_self):
                    result = super().close()
                    events.append("job_close")
                    return result

            verifier_module._WindowsKillOnCloseJob = RecordingJob
            self.addCleanup(
                setattr, verifier_module, "_WindowsKillOnCloseJob", real_job
            )
        real_reap = verifier_module._reap_process

        def recording_reap(process):
            events.append("reap")
            return real_reap(process)

        verifier_module._reap_process = recording_reap
        self.addCleanup(setattr, verifier_module, "_reap_process", real_reap)
        real_kill = verifier_module._terminate_anchor

        def recording_kill(process):
            events.append(("anchor_kill", process.poll() is None))
            return real_kill(process)

        verifier_module._terminate_anchor = recording_kill
        self.addCleanup(
            setattr, verifier_module, "_terminate_anchor", real_kill
        )
        return events

    def test_static_surface_owns_no_direct_processes(self):
        # R2-F4 falsifier: this module must not import subprocess, spawn
        # processes directly, or reference a manual kill helper. Helper code
        # inside command strings is data for the runner, not test machinery.
        source = Path(__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
        self.assertNotIn("subprocess", imported)
        self.assertNotIn("terminate" + "_tree", imported)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertNotIn("spawn" + "_tree", names | attributes)
        self.assertNotIn("Popen", names | attributes)

    def test_ordinary_success_uses_the_command_status(self):
        outcome, out_bytes, _, residue = self.run_direct(
            (sys.executable, "-c", "print('verified-ok')")
        )
        self.assertEqual(outcome.exit_code, 0)
        self.assertFalse(outcome.timed_out)
        self.assertIn(b"verified-ok", out_bytes)
        self.assertEqual(residue, [], "no anchor control file may remain")

    def test_ordinary_nonzero_exit_is_the_commands_status(self):
        outcome, _, _, residue = self.run_direct(
            (sys.executable, "-c", "import sys; sys.exit(3)")
        )
        self.assertEqual(outcome.exit_code, 3)
        self.assertFalse(outcome.timed_out)
        self.assertEqual(residue, [])

    def test_repeated_runs_share_no_state_and_leave_no_residue(self):
        for index in range(2):
            outcome, out_bytes, _, residue = self.run_direct(
                (sys.executable, "-c", f"print('run-{index}')")
            )
            self.assertEqual(outcome.exit_code, 0)
            self.assertIn(f"run-{index}".encode("utf-8"), out_bytes)
            self.assertEqual(residue, [])

    @unittest.skipUnless(os.name == "nt", "job ownership is the Windows claim")
    def test_ordinary_success_terminates_the_job_once_before_one_reap(self):
        # R2-F3/R3-F2 probe: for a real successful command the job authority
        # ends the tree exactly once, the single reap sees a live anchor
        # (the anchor never exits on its own), and the job handle closes
        # exactly once, after the reap.
        events = self.observe_lifecycle()
        outcome, _, _, _ = self.run_direct(
            (sys.executable, "-c", "print('clean')")
        )
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(events.count(("job_terminate", True)), 1)
        self.assertEqual(
            [e for e in events if isinstance(e, tuple) and e[0] == "anchor_kill"],
            [],
            "the assigned tree is job-owned; no direct anchor kill",
        )
        self.assertEqual(events.count("reap"), 1, "exactly one bounded reap")
        self.assertEqual(events.count("job_close"), 1)
        self.assertLess(
            events.index(("job_terminate", True)), events.index("reap")
        )
        self.assertLess(events.index("reap"), events.index("job_close"))

    @unittest.skipUnless(os.name == "nt", "job ownership is the Windows claim")
    def test_timeout_termination_is_single_pre_reap_with_live_anchor(self):
        events = self.observe_lifecycle()
        outcome, out_bytes, _, residue = self.run_direct(
            (
                sys.executable,
                "-c",
                "print('hung-ready', flush=True); "
                "import threading; threading.Event().wait(60)",
            ),
            timeout_seconds=3.0,
        )
        self.assertTrue(outcome.timed_out)
        self.assertIsNone(outcome.exit_code)
        self.assertIn(b"hung-ready", out_bytes)
        self.assertEqual(
            events.count(("job_terminate", True)), 1,
            "one job termination, observing a live anchor",
        )
        self.assertEqual(events.count("reap"), 1)
        self.assertLess(
            events.index(("job_terminate", True)), events.index("reap")
        )
        self.assertEqual(events.count("job_close"), 1)
        self.assertEqual(residue, [])

    def test_timeout_with_parent_and_descendant_retaining_pipe(self):
        lock = self.dir / "tree.lock"
        child_code = (
            "import sys, threading\n"
            f"handle = open({str(lock)!r}, 'w')\n"
            "sys.stdout.write('child-ready\\n'); sys.stdout.flush()\n"
            "threading.Event().wait(60)\n"
        )
        parent_code = (
            "import subprocess, sys, threading\n"
            f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}], "
            "stdin=subprocess.DEVNULL)\n"
            "print('parent-ready', flush=True)\n"
            "threading.Event().wait(60)\n"
        )
        outcome, out_bytes, _, residue = self.run_direct(
            (sys.executable, "-c", parent_code), timeout_seconds=5.0
        )
        self.assertTrue(outcome.timed_out)
        self.assertIsNone(outcome.exit_code)
        self.assertIn(b"parent-ready", out_bytes)
        self.assertIn(b"child-ready", out_bytes)
        os.remove(lock)  # succeeds only when no owned process survives
        self.assertEqual(residue, [])

    def test_immediate_parent_exit_with_descendant_retaining_pipe(self):
        # The command completes; a descendant keeps the capture pipe open.
        # The runner must report the command's real status without waiting
        # for the declared timeout and must end the descendant.
        lock = self.dir / "descendant.lock"
        child_code = (
            "import sys, threading\n"
            f"handle = open({str(lock)!r}, 'w')\n"
            "sys.stdout.write('child-holding\\n'); sys.stdout.flush()\n"
            "sys.stderr.write('R'); sys.stderr.flush()\n"
            "threading.Event().wait(60)\n"
        )
        parent_code = (
            "import subprocess, sys\n"
            f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}], "
            "stdin=subprocess.DEVNULL, stderr=subprocess.PIPE)\n"
            "child.stderr.read(1)\n"
            "print('parent-exiting', flush=True)\n"
        )
        outcome, out_bytes, _, residue = self.run_direct(
            (sys.executable, "-c", parent_code), timeout_seconds=30.0
        )
        self.assertEqual(outcome.exit_code, 0)
        self.assertFalse(outcome.timed_out)
        self.assertIn(b"parent-exiting", out_bytes)
        self.assertIn(b"child-holding", out_bytes)
        os.remove(lock)
        self.assertEqual(residue, [])

    def test_descendant_closing_pipes_but_alive_is_ended_by_tree_cleanup(self):
        lock = self.dir / "detached.lock"
        child_code = (
            "import os, sys, threading\n"
            f"handle = open({str(lock)!r}, 'w')\n"
            "sys.stderr.write('R'); sys.stderr.flush()\n"
            "os.close(1)\n"
            "os.close(2)\n"
            "threading.Event().wait(60)\n"
        )
        parent_code = (
            "import subprocess, sys\n"
            f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}], "
            "stdin=subprocess.DEVNULL, stderr=subprocess.PIPE)\n"
            "child.stderr.read(1)\n"
            "print('parent-done', flush=True)\n"
        )
        outcome, out_bytes, _, residue = self.run_direct(
            (sys.executable, "-c", parent_code), timeout_seconds=30.0
        )
        self.assertEqual(outcome.exit_code, 0)
        self.assertFalse(outcome.timed_out)
        self.assertIn(b"parent-done", out_bytes)
        os.remove(lock)
        self.assertEqual(residue, [])

    @unittest.skipUnless(os.name == "nt", "anchor status is the Windows path")
    def test_injected_failure_before_anchor_status_cleans_up(self):
        import frutlups_drive.verifier as verifier_module

        real = verifier_module._status_ready
        state = {"calls": 0}

        def failing(path):
            state["calls"] += 1
            raise RuntimeError("injected lifecycle failure")

        verifier_module._status_ready = failing
        self.addCleanup(setattr, verifier_module, "_status_ready", real)
        lock = self.dir / "injected.lock"
        code = (
            "import sys, threading\n"
            f"handle = open({str(lock)!r}, 'w')\n"
            "print('ready', flush=True)\n"
            "threading.Event().wait(60)\n"
        )
        runner = SubprocessRunner(self.clock)
        with tempfile.TemporaryDirectory(dir=self.dir) as scratch:
            out = Path(scratch) / "out.txt"
            err = Path(scratch) / "err.txt"
            with self.assertRaises(RuntimeError):
                runner.run(
                    (sys.executable, "-c", code), self.workspace, None,
                    30.0, out, err,
                )
            leftovers = sorted(p.name for p in Path(scratch).iterdir())
            self.assertEqual(
                leftovers, [], "no control file or capture residue on failure"
            )
        self.assertGreaterEqual(state["calls"], 1)
        if lock.exists():
            os.remove(lock)  # the tree is dead either way


class NativeHandleOwnerTests(unittest.TestCase):
    def setUp(self):
        import frutlups_drive.verifier as verifier_module

        self.module = verifier_module

    def test_false_and_exception_close_are_cached_bounded_data(self):
        for label, closer in (
            ("false", lambda handle: False),
            ("raise", lambda handle: (_ for _ in ()).throw(RuntimeError("raw"))),
        ):
            with self.subTest(close=label):
                calls = []

                def close(handle):
                    calls.append(handle)
                    return closer(handle)

                owner = self.module._NativeHandleOwner.acquire(
                    "test_handle", lambda: 123, close
                )
                first = owner.finalize()
                second = owner.finalize()
                self.assertEqual(first, second)
                self.assertEqual(first.failure, "test_handle_close_failed")
                self.assertEqual(calls, [123], "a finalizer is attempted once")
                self.assertEqual(owner.handle, 123, "failed close retains identity")

    def test_successful_close_clears_identity_once(self):
        calls = []
        owner = self.module._NativeHandleOwner.acquire(
            "test_handle", lambda: 456, lambda handle: calls.append(handle) or True
        )
        outcome = owner.finalize()
        self.assertTrue(outcome.closed)
        self.assertIsNone(outcome.failure)
        self.assertIsNone(owner.handle)
        self.assertEqual(owner.finalize(), outcome)
        self.assertEqual(calls, [456])


@unittest.skipUnless(os.name == "nt", "the Windows controller exists on Windows")
class WindowsNativeHandleTransactionTests(unittest.TestCase):
    class KernelFacts:
        def __init__(self):
            self.events = []
            self.create_result = 101
            self.configure_result = True
            self.configure_raises = False
            self.open_result = 202
            self.open_raises = False
            self.assign_result = True
            self.close_results = {101: True, 202: True}
            self.close_raises = set()

        def CreateJobObjectW(self, security, name):
            self.events.append("create")
            return self.create_result

        def SetInformationJobObject(self, handle, info_class, info, size):
            self.events.append(("configure", handle))
            if self.configure_raises:
                raise RuntimeError("unbounded configure detail")
            return self.configure_result

        def OpenProcess(self, access, inherit, pid):
            self.events.append(("open", pid))
            if self.open_raises:
                raise RuntimeError("unbounded open detail")
            return self.open_result

        def AssignProcessToJobObject(self, job, process):
            self.events.append(("assign", job, process))
            return self.assign_result

        def TerminateJobObject(self, handle, code):
            self.events.append(("terminate", handle))
            return True

        def CloseHandle(self, handle):
            self.events.append(("close", handle))
            if handle in self.close_raises:
                raise RuntimeError("unbounded synthetic close detail")
            return self.close_results.get(handle, True)

    def setUp(self):
        import frutlups_drive.verifier as verifier_module

        self.module = verifier_module
        self.kernel = self.KernelFacts()
        real = verifier_module._kernel32
        verifier_module._kernel32 = self.kernel
        self.addCleanup(setattr, verifier_module, "_kernel32", real)

    def test_creation_failure_has_no_handle_to_finalize(self):
        self.kernel.create_result = 0
        with self.assertRaises(OSError) as caught:
            self.module._WindowsKillOnCloseJob()
        self.assertIn("job_creation_failed", str(caught.exception))
        self.assertEqual(self.kernel.events, ["create"])

    def test_configuration_cause_survives_false_close(self):
        self.kernel.configure_result = False
        self.kernel.close_results[101] = False
        with self.assertRaises(OSError) as caught:
            self.module._WindowsKillOnCloseJob()
        self.assertIn("job_configuration_failed", str(caught.exception))
        self.assertIn("job_handle_close_failed", str(caught.exception))
        self.assertIsNotNone(caught.exception.__cause__)
        self.assertEqual(self.kernel.events.count(("close", 101)), 1)

    def test_raising_configuration_still_finalizes_and_bounds_the_outcome(self):
        self.kernel.configure_raises = True
        self.kernel.close_raises.add(101)
        with self.assertRaises(OSError) as caught:
            self.module._WindowsKillOnCloseJob()
        message = str(caught.exception)
        self.assertIn("job_configuration_failed", message)
        self.assertIn("job_handle_close_failed", message)
        self.assertNotIn("unbounded configure", message)
        self.assertEqual(self.kernel.events.count(("close", 101)), 1)

    def test_assignment_cause_and_helper_false_close_are_both_preserved(self):
        job = self.module._WindowsKillOnCloseJob()
        self.kernel.assign_result = False
        self.kernel.close_results[202] = False
        with self.assertRaises(OSError) as caught:
            job.assign(4242)
        self.assertIn("job_assignment_failed", str(caught.exception))
        self.assertIn(
            "assignment_process_handle_close_failed", str(caught.exception)
        )
        self.assertIsNotNone(caught.exception.__cause__)
        self.assertEqual(self.kernel.events.count(("close", 202)), 1)
        self.assertIsNone(job.close().failure)

    def test_successful_assignment_with_raising_helper_close_is_owned(self):
        job = self.module._WindowsKillOnCloseJob()
        self.kernel.close_raises.add(202)
        with self.assertRaises(OSError) as caught:
            job.assign(4242)
        message = str(caught.exception)
        self.assertIn("assignment_helper_finalization_failed", message)
        self.assertIn("assignment_process_handle_close_failed", message)
        self.assertNotIn("unbounded synthetic", message)
        self.assertEqual(self.kernel.events.count(("close", 202)), 1)
        job.close()

    def test_raising_helper_acquisition_preserves_open_cause(self):
        job = self.module._WindowsKillOnCloseJob()
        self.kernel.open_raises = True
        with self.assertRaises(OSError) as caught:
            job.assign(4242)
        self.assertIn("assignment_process_open_failed", str(caught.exception))
        self.assertIsNotNone(caught.exception.__cause__)
        self.assertEqual(self.kernel.events.count(("close", 202)), 0)
        job.close()

    def test_job_close_false_retains_identity_and_is_not_retried(self):
        job = self.module._WindowsKillOnCloseJob()
        self.kernel.close_results[101] = False
        first = job.close()
        second = job.close()
        self.assertEqual(first.failure, "job_handle_close_failed")
        self.assertEqual(first, second)
        self.assertEqual(job.handle, 101)
        self.assertEqual(self.kernel.events.count(("close", 101)), 1)


@unittest.skipUnless(os.name == "nt", "the Windows controller exists on Windows")
class WindowsLifecycleStateTests(VerifierTestCase):
    """R3-F2/R3-F3: causal state-transition proof for the production Windows
    lifecycle controller. Fake process/job/stream facts drive the real
    controller through every enumerated ownership transition; any unbounded
    wait fails immediately and nothing real is spawned."""

    def setUp(self):
        super().setUp()
        import frutlups_drive.verifier as verifier_module

        self.verifier_module = verifier_module
        baseline = threading.active_count()
        self.addCleanup(
            lambda: self.assertEqual(
                threading.active_count(), baseline,
                "drain threads must be joined before the runner returns",
            )
        )

    def patch(self, name, value):
        real = getattr(self.verifier_module, name)
        setattr(self.verifier_module, name, value)
        self.addCleanup(setattr, self.verifier_module, name, real)

    def fresh_facts(self, spec_fail=False, stdout=None, process_kwargs=None,
                    **job_kwargs):
        self.events = []
        self.fake_job = None

        def make_job():
            self.fake_job = FakeJobFact(self.events, **job_kwargs)
            return self.fake_job

        self.patch("_WindowsKillOnCloseJob", make_job)
        self.fake_process = FakeProcessFact(
            self.events,
            stdin=FakeSpecStreamFact(self.events, fail=spec_fail),
            stdout=stdout,
            **dict(process_kwargs or {}),
        )

        def spawn(cwd):
            self.events.append("spawn")
            return self.fake_process

        self.patch("_spawn_anchor", spawn)

    def run_lifecycle(self, timeout_seconds=30.0, status=None,
                      max_stream_bytes=1_048_576):
        runner = SubprocessRunner(self.clock)
        with tempfile.TemporaryDirectory(dir=self.dir) as scratch:
            out = Path(scratch) / "out.txt"
            err = Path(scratch) / "err.txt"
            if status is not None:
                (Path(scratch) / "out.txt.anchor_status.json").write_bytes(
                    status
                )
            return runner.run(
                ("declared-command",), self.workspace, None, timeout_seconds,
                out, err, max_stream_bytes=max_stream_bytes,
            )

    def assert_bounded_waits(self):
        self.assertTrue(self.fake_process.wait_calls, "the anchor was reaped")
        self.assertTrue(
            all(bound is not None for bound in self.fake_process.wait_calls),
            "every reap wait carries a bound",
        )
        self.assertEqual(len(self.fake_process.wait_calls), 1, "one reap only")

    def index_of(self, event):
        self.assertIn(event, self.events)
        return self.events.index(event)

    def test_job_creation_failure_spawns_nothing(self):
        self.events = []
        self.patch(
            "_WindowsKillOnCloseJob",
            lambda: FakeJobFact(self.events, create_error=True),
        )
        spawned = []
        self.patch("_spawn_anchor", lambda cwd: spawned.append(cwd))
        with self.assertRaises(OSError):
            self.run_lifecycle()
        self.assertEqual(spawned, [], "creation failure precedes any spawn")
        self.assertEqual(self.events, ["job_create"])

    def test_anchor_spawn_failure_closes_the_job_exactly_once(self):
        self.events = []
        job_holder = []

        def make_job():
            job = FakeJobFact(self.events)
            job_holder.append(job)
            return job

        self.patch("_WindowsKillOnCloseJob", make_job)

        def failing_spawn(cwd):
            self.events.append("spawn_raises")
            raise OSError("anchor spawn failed")

        self.patch("_spawn_anchor", failing_spawn)
        with self.assertRaises(OSError):
            self.run_lifecycle()
        self.assertEqual(job_holder[0].close_calls, 1)
        self.assertEqual(job_holder[0].terminate_calls, 0)

    def test_anchor_spawn_cause_survives_false_job_close(self):
        self.events = []
        job_holder = []

        def make_job():
            job = FakeJobFact(self.events, close_result=False)
            job_holder.append(job)
            return job

        self.patch("_WindowsKillOnCloseJob", make_job)

        def failing_spawn(cwd):
            raise OSError("anchor spawn failed")

        self.patch("_spawn_anchor", failing_spawn)
        with self.assertRaises(OSError) as caught:
            self.run_lifecycle()
        self.assertIn("anchor_spawn_failed", str(caught.exception))
        self.assertIn("job_handle_close_failed", str(caught.exception))
        self.assertEqual(str(caught.exception.__cause__), "anchor spawn failed")
        self.assertEqual(job_holder[0].close_calls, 1)

    def test_assignment_failure_directly_ends_the_unassigned_live_anchor(self):
        # R3-F2 reviewer-literal falsifier: before successful assignment the
        # direct process handle is the cleanup authority. The refuted
        # round-three behavior terminated the empty job and waited,
        # unbounded, on the still-live unassigned anchor.
        self.fresh_facts(assign_error=True)
        with self.assertRaises(OSError):
            self.run_lifecycle()
        self.assertEqual(
            self.events.count(("kill", True)), 1,
            "the unassigned anchor is ended directly, exactly once, live",
        )
        self.assertEqual(
            self.fake_job.terminate_calls, 0,
            "terminating an empty job is not an ownership action",
        )
        self.assertNotIn("spec_write", self.events,
                         "assignment strictly precedes spec delivery")
        self.assert_bounded_waits()
        self.assertEqual(self.fake_job.close_calls, 1)
        kill_index = self.index_of(("kill", True))
        reap_index = min(
            i for i, e in enumerate(self.events)
            if isinstance(e, tuple) and e[0] == "reap"
        )
        self.assertLess(kill_index, reap_index)
        self.assertLess(reap_index, self.index_of("job_close"))

    def test_spec_delivery_failure_uses_the_job_authority(self):
        self.fresh_facts(spec_fail=True)
        with self.assertRaises(OSError):
            self.run_lifecycle()
        self.assertEqual(self.fake_job.terminate_calls, 1)
        self.assertEqual(
            self.fake_process.kill_calls, 0,
            "after assignment the job, not the direct handle, ends the tree",
        )
        self.assert_bounded_waits()
        self.assertEqual(self.fake_job.close_calls, 1)
        self.assertLess(
            self.index_of("job_assign"),
            self.index_of(("job_terminate", True)),
        )
        self.assertLess(
            self.index_of(("job_terminate", True)),
            self.index_of("job_close"),
        )

    def test_failed_job_termination_applies_kill_on_close_before_reap(self):
        # R3-F2 reviewer-literal falsifier: a failed Win32 termination call
        # must not be ignored — kill-on-close applies before any bounded
        # wait and the run reports an owned failure, never success.
        self.fresh_facts(terminate_result=False)
        with self.assertRaises(OSError):
            self.run_lifecycle(status=b'{"exit_code": 0}')
        self.assertEqual(self.fake_job.terminate_calls, 1)
        self.assertEqual(self.fake_job.close_calls, 1)
        reap_index = min(
            i for i, e in enumerate(self.events)
            if isinstance(e, tuple) and e[0] == "reap"
        )
        self.assertLess(
            self.index_of(("job_terminate", False)),
            self.index_of("job_close"),
        )
        self.assertLess(self.index_of("job_close"), reap_index)
        self.assert_bounded_waits()

    def test_status_records_are_strictly_bounded_and_owned(self):
        cases = (
            ("malformed", b"not-json{"),
            ("out_of_range_negative", b'{"exit_code": -1}'),
            ("out_of_range_wide", b'{"exit_code": 4294967296}'),
            ("wrong_type", b'{"exit_code": true}'),
        )
        for label, status in cases:
            with self.subTest(status=label):
                self.fresh_facts()
                outcome = self.run_lifecycle(status=status)
                self.assertIsNone(outcome.exit_code)
                self.assertFalse(outcome.timed_out)

    def test_missing_status_after_readiness_is_never_an_invented_exit(self):
        self.fresh_facts()
        self.patch("_status_ready", lambda path: True)
        outcome = self.run_lifecycle()
        self.assertIsNone(outcome.exit_code)
        self.assertFalse(outcome.timed_out)
        self.assertEqual(self.fake_job.terminate_calls, 1)

    def test_normal_completion_orders_assign_spec_terminate_reap_close(self):
        self.fresh_facts()
        outcome = self.run_lifecycle(status=b'{"exit_code": 0}')
        self.assertEqual(outcome.exit_code, 0)
        self.assertFalse(outcome.timed_out)
        self.assertEqual(self.fake_process.kill_calls, 0)
        self.assertEqual(self.fake_job.terminate_calls, 1)
        self.assertEqual(self.fake_job.close_calls, 1)
        self.assert_bounded_waits()
        order = [
            self.index_of("job_assign"),
            self.index_of("spec_write"),
            self.index_of(("job_terminate", True)),
            min(i for i, e in enumerate(self.events)
                if isinstance(e, tuple) and e[0] == "reap"),
            self.index_of("job_close"),
        ]
        self.assertEqual(order, sorted(order), "lifecycle order is explicit")

    def test_nonzero_completion_returns_the_commands_status(self):
        self.fresh_facts()
        outcome = self.run_lifecycle(status=b'{"exit_code": 3}')
        self.assertEqual(outcome.exit_code, 3)
        self.assertFalse(outcome.timed_out)

    def test_deadline_authorizes_cleanup_when_no_status_appears(self):
        self.fresh_facts()
        outcome = self.run_lifecycle(timeout_seconds=0.2)
        self.assertTrue(outcome.timed_out)
        self.assertIsNone(outcome.exit_code)
        self.assertEqual(self.fake_job.terminate_calls, 1)
        self.assertEqual(self.fake_job.close_calls, 1)
        self.assert_bounded_waits()

    def test_dead_anchor_without_status_fails_within_one_poll_and_keeps_streams(self):
        self.fresh_facts(
            stdout=FakeStreamFact([b"anchor stdout\n"]),
            process_kwargs={
                "poll_script": [17],
                "stderr": FakeStreamFact([b"anchor stderr\n"]),
            },
        )
        runner = SubprocessRunner(self.clock)
        out = self.dir / "dead_anchor_stdout.txt"
        err = self.dir / "dead_anchor_stderr.txt"
        started = time.monotonic()
        outcome = runner.run(
            ("declared-command",), self.workspace, None, 0.2, out, err
        )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.15, "dead anchors must not consume the deadline")
        self.assertFalse(outcome.timed_out)
        self.assertIsNone(outcome.exit_code)
        self.assertEqual(out.read_bytes(), b"anchor stdout\n")
        self.assertEqual(err.read_bytes(), b"anchor stderr\n")
        self.assertEqual(self.fake_process.poll_calls, 1)
        self.assert_bounded_waits()

    def test_stream_overflow_authorizes_cleanup_before_completion(self):
        self.fresh_facts(stdout=FakeStreamFact([b"x" * 2048]))
        outcome = self.run_lifecycle(max_stream_bytes=1024)
        self.assertTrue(outcome.stdout_overflow)
        self.assertFalse(outcome.stderr_overflow)
        self.assertFalse(outcome.timed_out)
        self.assertIsNone(outcome.exit_code)
        self.assertEqual(self.fake_job.terminate_calls, 1)
        self.assert_bounded_waits()

    def test_drain_startup_failure_retains_job_cleanup(self):
        self.fresh_facts()

        def failing_start(stream, limit, state, wake):
            raise RuntimeError("drain startup failed")

        self.patch("_start_drain", failing_start)
        with self.assertRaises(RuntimeError):
            self.run_lifecycle()
        self.assertEqual(self.fake_job.terminate_calls, 1)
        self.assertEqual(self.fake_job.close_calls, 1)
        self.assert_bounded_waits()

    def test_unassigned_anchor_kill_failure_still_finalizes_everything(self):
        # R4-F2 reviewer-literal falsifier: a direct kill() that raises must
        # not abandon acquired resources. Spec close, owner stream closure,
        # bounded reap, and job close are all still attempted, and the run
        # ends in one owned aggregate failure, not the raw kill exception.
        self.fresh_facts(assign_error=True,
                         process_kwargs={"kill_error": True})
        with self.assertRaises(OSError) as caught:
            self.run_lifecycle()
        message = str(caught.exception)
        self.assertIn("anchor_terminate_failed", message)
        process = self.fake_process
        self.assertTrue(process.stdin.closed,
                        "the spec input is closed before termination")
        self.assertEqual(process.kill_calls, 1,
                         "direct termination attempted exactly once")
        self.assertEqual(len(process.wait_calls), 1,
                         "bounded reap attempted despite kill failure")
        self.assertIsNotNone(process.wait_calls[0])
        self.assertEqual(self.fake_job.close_calls, 1,
                         "job handle closed despite kill failure")
        self.assertEqual(self.fake_job.terminate_calls, 0)
        self.assertTrue(process.stdout.closed and process.stderr.closed,
                        "owner closes streams no worker acquired")
        kill_index = self.index_of(("kill", True))
        reap_index = min(
            i for i, e in enumerate(self.events)
            if isinstance(e, tuple) and e[0] == "reap"
        )
        self.assertLess(kill_index, reap_index)
        self.assertLess(reap_index, self.index_of("job_close"))

    def test_compound_cleanup_failures_attempt_every_finalizer(self):
        # R4-F2/R4-F3 compound falsifier: failed job termination, a failing
        # stdout drain read, and a failing bounded reap together must still
        # produce the complete ordered attempt trace and one bounded
        # aggregate failure — never an early return or success.
        hooks = self.record_excepthook()
        self.fresh_facts(
            terminate_result=False,
            stdout=FakeStreamFact(read_error=True),
            process_kwargs={"wait_error": True},
        )
        with self.assertRaises(OSError) as caught:
            self.run_lifecycle(status=b'{"exit_code": 0}', timeout_seconds=5.0)
        message = str(caught.exception)
        for code in ("job_terminate_failed", "drain_read_failed",
                     "reap_failed"):
            self.assertIn(code, message)
        self.assertEqual(hooks, [], "no background thread exception escapes")
        self.assertEqual(self.fake_job.terminate_calls, 1)
        self.assertEqual(self.fake_job.close_calls, 1,
                         "kill-on-close applied exactly once")
        reap_index = min(
            i for i, e in enumerate(self.events)
            if isinstance(e, tuple) and e[0] == "reap"
        )
        self.assertLess(self.index_of(("job_terminate", False)),
                        self.index_of("job_close"))
        self.assertLess(self.index_of("job_close"), reap_index,
                        "kill-on-close precedes the bounded reap")
        self.assertEqual(len(self.fake_process.wait_calls), 1)
        self.assertIsNotNone(self.fake_process.wait_calls[0])
        self.assertTrue(self.fake_process.stdout.closed,
                        "the failed-read worker still closed its stream")
        self.assertTrue(self.fake_process.stderr.closed)

    def test_drain_read_failure_publishes_no_partial_capture(self):
        # R4-F2 reviewer-literal falsifier: a worker read failure must reach
        # the lifecycle owner as an owned verifier failure — no
        # threading.excepthook, no partial stdout/stderr publication, no
        # successful ProcessOutcome.
        hooks = self.record_excepthook()
        self.fresh_facts(stdout=FakeStreamFact(read_error=True))
        runner = SubprocessRunner(self.clock)
        with tempfile.TemporaryDirectory(dir=self.dir) as scratch:
            out = Path(scratch) / "out.txt"
            err = Path(scratch) / "err.txt"
            with self.assertRaises(OSError) as caught:
                runner.run(
                    ("declared-command",), self.workspace, None, 0.5,
                    out, err,
                )
            self.assertFalse(out.exists(),
                             "no partial capture may be published")
            self.assertFalse(err.exists())
        self.assertIn("drain_read_failed", str(caught.exception))
        self.assertEqual(hooks, [], "no background thread exception escapes")
        self.assertEqual(self.fake_job.terminate_calls, 1)
        self.assertEqual(self.fake_job.close_calls, 1)

    def test_one_drain_started_failure_joins_the_started_drain_bounded(self):
        self.fresh_facts()
        started = []

        def second_fails(stream, limit, state, wake):
            if started:
                raise RuntimeError("second drain failed to start")
            thread = FakeThreadFact(self.events)
            started.append(thread)
            return thread

        self.patch("_start_drain", second_fails)
        with self.assertRaises(RuntimeError):
            self.run_lifecycle()
        self.assertEqual(len(started), 1)
        self.assertTrue(started[0].join_calls, "the started drain was joined")
        self.assertTrue(
            all(bound is not None for bound in started[0].join_calls),
            "every drain join carries a bound",
        )
        self.assertEqual(self.fake_job.terminate_calls, 1)
        self.assertEqual(self.fake_job.close_calls, 1)
        self.assert_bounded_waits()


class PosixLifecycleStateTests(VerifierTestCase):
    """R3-F2: stream EOF is a stream fact, not command completion. Fake
    process facts drive the production POSIX controller through dual-EOF
    survival, real exit, deadline, and overflow on any host platform."""

    def setUp(self):
        super().setUp()
        import frutlups_drive.verifier as verifier_module

        self.verifier_module = verifier_module
        self.events = []
        self.group_kill_error = False
        real_kill = verifier_module._kill_posix_group

        def recording_kill(process):
            self.events.append(("group_kill", process.returncode is None))
            if self.group_kill_error:
                raise PermissionError("group kill refused")

        verifier_module._kill_posix_group = recording_kill
        self.addCleanup(
            setattr, verifier_module, "_kill_posix_group", real_kill
        )
        baseline = threading.active_count()
        self.addCleanup(
            lambda: self.assertEqual(
                threading.active_count(), baseline,
                "drain threads must be joined before the controller returns",
            )
        )

    def run_posix(self, process, timeout_seconds=30.0,
                  max_stream_bytes=1_048_576):
        real_spawn = self.verifier_module._spawn_posix
        self.verifier_module._spawn_posix = lambda argv, cwd, env: process
        self.addCleanup(
            setattr, self.verifier_module, "_spawn_posix", real_spawn
        )
        out_state = self.verifier_module._DrainState()
        err_state = self.verifier_module._DrainState()
        exit_code, timed_out = self.verifier_module._run_posix_lifecycle(
            ("declared-command",), self.workspace, None, timeout_seconds,
            out_state, err_state, max_stream_bytes,
        )
        return exit_code, timed_out, out_state, err_state

    def test_both_stream_eof_while_live_waits_for_the_real_exit(self):
        # R3-F2 reviewer-literal falsifier: immediate dual EOF with
        # ``poll()`` still None must not be killed or reported as a
        # non-timeout completion; the real exit code decides the outcome.
        process = FakeProcessFact(
            self.events,
            poll_script=[None, None, None, 0],
            stdout=FakeStreamFact([b"posix-out"]),
        )
        exit_code, timed_out, out_state, err_state = self.run_posix(process)
        self.assertEqual(exit_code, 0)
        self.assertFalse(timed_out)
        self.assertTrue(out_state.done and err_state.done,
                        "both captures reached EOF")
        self.assertGreaterEqual(
            process.poll_calls, 4,
            "the controller kept observing the command after capture EOF",
        )
        self.assertEqual(
            self.events.count(("group_kill", False)), 1,
            "group cleanup ran once, after the real exit",
        )
        self.assertNotIn(("group_kill", True), self.events,
                         "no live kill merely because capture completed")
        self.assertEqual(bytes(out_state.captured), b"posix-out")
        self.assertTrue(
            all(bound is not None for bound in process.wait_calls),
            "every reap wait carries a bound",
        )

    def test_deadline_not_eof_authorizes_termination(self):
        process = FakeProcessFact(self.events)  # poll stays None: still live
        exit_code, timed_out, _, _ = self.run_posix(
            process, timeout_seconds=0.2
        )
        self.assertTrue(timed_out)
        self.assertIsNone(exit_code)
        self.assertEqual(
            self.events.count(("group_kill", True)), 1,
            "the live group is ended once, under the timeout authority",
        )
        self.assertTrue(
            all(bound is not None for bound in process.wait_calls)
        )

    def test_overflow_authorizes_termination_before_exit(self):
        process = FakeProcessFact(
            self.events, stdout=FakeStreamFact([b"y" * 2048])
        )
        exit_code, timed_out, out_state, _ = self.run_posix(
            process, max_stream_bytes=1024
        )
        self.assertTrue(out_state.overflow)
        self.assertFalse(timed_out)
        self.assertEqual(self.events.count(("group_kill", True)), 1)
        self.assertEqual(
            exit_code, 1,
            "the reaped code after an overflow kill is reported honestly",
        )

    def test_normal_exit_reports_the_commands_own_code(self):
        process = FakeProcessFact(self.events, poll_script=[3])
        exit_code, timed_out, _, _ = self.run_posix(process)
        self.assertEqual(exit_code, 3)
        self.assertFalse(timed_out)
        self.assertEqual(self.events.count(("group_kill", False)), 1)

    def test_group_termination_raising_uses_direct_fallback_and_finalizes(self):
        # R4-F2 reviewer-literal falsifier: a raising group kill must not
        # abandon the process or skip local finalizers — one direct-process
        # fallback is attempted through the retained handle, drains and
        # streams finish, the reap stays bounded, and the outcome is one
        # owned aggregate failure naming the group-termination code.
        self.group_kill_error = True
        process = FakeProcessFact(self.events)  # live: poll stays None
        with self.assertRaises(OSError) as caught:
            self.run_posix(process, timeout_seconds=0.2)
        message = str(caught.exception)
        self.assertIn("group_terminate_failed", message)
        self.assertEqual(process.kill_calls, 1,
                         "exactly one direct-process fallback")
        self.assertEqual(len(process.wait_calls), 1,
                         "bounded reap attempted despite kill failure")
        self.assertIsNotNone(process.wait_calls[0])
        self.assertTrue(process.stdout.closed and process.stderr.closed,
                        "worker-owned streams still reached closure")

    def test_drain_read_failure_cannot_become_empty_success(self):
        # R4-F2 reviewer-literal falsifier: dual read failures previously
        # escaped only via threading.excepthook while the controller
        # returned (0, False) — indistinguishable from empty successful
        # output. They must surface as one owned verifier failure.
        hooks = self.record_excepthook()
        process = FakeProcessFact(
            self.events,
            poll_script=[0],
            stdout=FakeStreamFact(read_error=True),
            stderr=FakeStreamFact(read_error=True),
        )
        with self.assertRaises(OSError) as caught:
            self.run_posix(process)
        self.assertIn("drain_read_failed", str(caught.exception))
        self.assertEqual(hooks, [], "no background thread exception escapes")
        self.assertEqual(len(process.wait_calls), 1)
        self.assertIsNotNone(process.wait_calls[0])
        self.assertTrue(process.stdout.closed and process.stderr.closed,
                        "failed-read workers still closed their streams")

    def test_drain_close_failure_is_owned(self):
        hooks = self.record_excepthook()
        process = FakeProcessFact(
            self.events,
            poll_script=[0],
            stdout=FakeStreamFact([b"data"], close_error=True),
        )
        with self.assertRaises(OSError) as caught:
            self.run_posix(process)
        self.assertIn("drain_close_failed", str(caught.exception))
        self.assertEqual(hooks, [])

    def test_drain_read_and_close_failure_together_are_owned(self):
        hooks = self.record_excepthook()
        process = FakeProcessFact(
            self.events,
            poll_script=[0],
            stdout=FakeStreamFact(read_error=True, close_error=True),
        )
        with self.assertRaises(OSError) as caught:
            self.run_posix(process)
        message = str(caught.exception)
        self.assertIn("drain_read_failed", message)
        self.assertIn("drain_close_failed", message)
        self.assertEqual(hooks, [])


class TimeoutBoundaryTests(VerifierTestCase):
    """R4-F1: one exact-type finite timeout normalizer at the shared
    declaration/pre-effect boundary — no non-finite deadline can reach a
    lifecycle controller and no subclass conversion hook ever runs."""

    def test_command_timeout_rejects_non_plain_or_non_finite_values(self):
        cases = (
            ("huge_401_digit", 10**400),
            ("huge_10001_digit", 10**10_000),
            ("exponent_overflow_infinity", float("inf")),
            ("negative_infinity", float("-inf")),
            ("nan", float("nan")),
            ("boolean", True),
            ("string", "60"),
            ("none", None),
            ("hostile_int_subclass", _HostileInt(60)),
            ("hostile_float_subclass", _HostileFloat(60.0)),
        )
        for label, bad in cases:
            with self.subTest(value=label):
                with self.assertRaises(ValueError):
                    VerificationCommand(argv=("x",), timeout_seconds=bad)

    def test_command_timeout_preserves_existing_plain_semantics(self):
        # No new range is invented in this correction: plain values keep
        # their current sign semantics and normalize to float.
        self.assertEqual(
            VerificationCommand(argv=("x",), timeout_seconds=60)
            .timeout_seconds,
            60.0,
        )
        self.assertEqual(
            VerificationCommand(argv=("x",), timeout_seconds=-1.0)
            .timeout_seconds,
            -1.0,
        )

    def test_runner_refuses_invalid_timeout_before_any_spawn(self):
        import frutlups_drive.verifier as verifier_module

        spawned = []
        for name in ("_spawn_anchor", "_spawn_posix"):
            real = getattr(verifier_module, name)
            setattr(
                verifier_module, name,
                lambda *args, _n=name, **kwargs: spawned.append(_n),
            )
            self.addCleanup(setattr, verifier_module, name, real)
        if verifier_module._WindowsKillOnCloseJob is not None:
            events = []
            real_job = verifier_module._WindowsKillOnCloseJob
            verifier_module._WindowsKillOnCloseJob = (
                lambda: FakeJobFact(events)
            )
            self.addCleanup(
                setattr, verifier_module, "_WindowsKillOnCloseJob", real_job
            )
        runner = SubprocessRunner(self.clock)
        with tempfile.TemporaryDirectory(dir=self.dir) as scratch:
            out = Path(scratch) / "out.txt"
            err = Path(scratch) / "err.txt"
            for label, bad in (
                ("exponent_overflow_infinity", float("inf")),
                ("huge_401_digit", 10**400),
                ("boolean", True),
            ):
                with self.subTest(value=label):
                    with self.assertRaises(ValueError):
                        runner.run(
                            ("x",), self.workspace, None, bad, out, err
                        )
        self.assertEqual(
            spawned, [], "an invalid timeout must refuse before any spawn"
        )


class RealSubprocessTests(VerifierTestCase):
    def test_green_python_verification_leaves_workspace_byte_identical(self):
        tests = self.workspace / "tests"
        tests.mkdir()
        (tests / "test_clean.py").write_text(
            "import unittest\n\n"
            "class CleanTests(unittest.TestCase):\n"
            "    def test_green(self):\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )

        def snapshot():
            return {
                path.relative_to(self.workspace).as_posix(): path.read_bytes()
                for path in self.workspace.rglob("*")
                if path.is_file()
            }

        before = snapshot()
        runner = SubprocessRunner(self.clock)
        plan = self.plan(
            [
                VerificationCommand(
                    argv=(
                        sys.executable,
                        "-m",
                        "unittest",
                        "discover",
                        "-s",
                        "tests",
                    ),
                    timeout_seconds=60.0,
                )
            ]
        )
        outcome = self.verifier(runner).verify(self.attempt, plan, self.workspace)

        self.assertTrue(outcome.passed)
        self.assertEqual(outcome.dirty_files, ())
        self.assertEqual(snapshot(), before)

    def test_real_command_evidence_round_trip(self):
        runner = SubprocessRunner(self.clock)
        plan = self.plan(
            [
                VerificationCommand(
                    argv=(sys.executable, "-c", "print('verified-ok')"),
                    timeout_seconds=60.0,
                )
            ]
        )
        outcome = self.verifier(runner).verify(self.attempt, plan, self.workspace)
        self.assertTrue(outcome.passed)
        stdout = (self.attempt / "verification/cmd_000_stdout.txt").read_bytes()
        self.assertIn(b"verified-ok", stdout)

    def test_subprocess_runner_timeout_is_bounded_and_owned(self):
        runner = SubprocessRunner(self.clock)
        plan = self.plan(
            [
                VerificationCommand(
                    argv=(
                        sys.executable,
                        "-c",
                        "import threading; print('hung-ready', flush=True); "
                        "threading.Event().wait(60)",
                    ),
                    timeout_seconds=3.0,
                )
            ]
        )
        outcome = self.verifier(runner).verify(self.attempt, plan, self.workspace)
        self.assertFalse(outcome.passed)
        parsed = tomllib.loads(
            (self.attempt / "verification/evidence.toml").read_bytes().decode("utf-8")
        )
        self.assertTrue(parsed["command"][0]["timed_out"])
        stdout = (self.attempt / "verification/cmd_000_stdout.txt").read_bytes()
        self.assertIn(b"hung-ready", stdout)


if __name__ == "__main__":
    unittest.main()
