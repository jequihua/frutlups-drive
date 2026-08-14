"""Independent verifier (architecture contract §10).

Commands come only from an injected, declaration-authoritative
:class:`VerificationPlan`; never from agent output, produced files, guessed
defaults, or roadmap prose. Execution uses explicit argv/cwd/env, UTF-8
capture, timeouts, and two explicit platform lifecycle controllers that
never trust a reaped numeric PID and never probe liveness with
``os.kill(pid, 0)``: on Windows the command runs under a live runner-owned
anchor inside a kill-on-close job object, on POSIX in its own session
group whose real ``poll()`` — never stream EOF — decides completion.
Evidence is machine-generated, hash-addressed, and published write-once
into the owned attempt at ``verification/``.

Dirty rule (bounded): any workspace file whose stat or content changed during
verification appears in the dirty list; the run fails unless every dirty file
is byte-identical to its pre-run content *and* explicitly declared as
regenerated output.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from frutlups_drive.budget import Clock
from frutlups_drive.runstore import RunStore, _toml_string
from frutlups_drive.workspace import WorkspaceManager


MAX_STREAM_CAPTURE_BYTES = 1_048_576


def _validate_timeout_seconds(value: object) -> float:
    """The one timeout boundary (R4-F1): a value whose *exact* type is the
    built-in ``int`` or ``float`` and whose conversion yields a finite
    ``float``. Booleans and numeric subclasses are invalid before any
    conversion — a subclass hook is never invoked — and conversion
    overflow, ``NaN``, and both infinities (including a raw JSON exponent
    such as ``1e400`` that parses to infinity) reach this same stable owned
    refusal, so no non-finite deadline can disable the verifier's bounded-
    time guarantee. Existing sign/range semantics are preserved and the
    invalid value is never echoed."""
    if type(value) is not int and type(value) is not float:
        raise ValueError("timeout_seconds must be a finite plain number")
    try:
        timeout = float(value)
    except OverflowError:
        raise ValueError(
            "timeout_seconds must be a finite plain number"
        ) from None
    if not math.isfinite(timeout):
        raise ValueError("timeout_seconds must be a finite plain number")
    return timeout


@dataclass(frozen=True)
class VerificationCommand:
    argv: tuple[str, ...]
    cwd: str = "."
    env_profile: str = "default"
    timeout_seconds: float = 120.0
    # Declaration-authoritative per-stream capture bound (R1-F4): exactly
    # 1 MiB by default; a declaration may set it only within [1, 1 MiB].
    max_stream_bytes: int = MAX_STREAM_CAPTURE_BYTES

    def __post_init__(self) -> None:
        # R4-F1: the declared deadline is normalized at construction by the
        # one shared timeout boundary; a raw declared value is either a
        # finite plain-number float here or one stable owned refusal.
        object.__setattr__(
            self,
            "timeout_seconds",
            _validate_timeout_seconds(self.timeout_seconds),
        )
        if type(self.max_stream_bytes) is not int or not (
            1 <= self.max_stream_bytes <= MAX_STREAM_CAPTURE_BYTES
        ):
            raise ValueError(
                "max_stream_bytes must be an integer between 1 and "
                f"{MAX_STREAM_CAPTURE_BYTES}"
            )


@dataclass(frozen=True)
class VerificationPlan:
    commands: tuple[VerificationCommand, ...]
    declared_regenerated: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProcessOutcome:
    started: float
    ended: float
    exit_code: int | None
    timed_out: bool
    stdout_overflow: bool = False
    stderr_overflow: bool = False


@dataclass(frozen=True)
class VerificationOutcome:
    passed: bool
    evidence_dir: Path
    dirty_files: tuple[str, ...]
    evidence_sha256: str = ""


class ProcessRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        env: Mapping[str, str] | None,
        timeout_seconds: float,
        stdout_path: Path,
        stderr_path: Path,
        max_stream_bytes: int = MAX_STREAM_CAPTURE_BYTES,
        stdin_bytes: bytes | None = None,
    ) -> ProcessOutcome:
        ...


# --------------------------------------------------------------------------
# Owned process-tree lifecycle (R2-F3, R2-F4, R3-F2).
#
# On Windows the direct child is a runner-owned Python *anchor* inside a
# kill-on-close job object. The anchor receives its work (argv/cwd/env and a
# status path) as strict JSON on stdin — so job assignment provably precedes
# any spawned work — launches the exact declared command without a shell,
# waits for it, atomically publishes one bounded strict exit-status record,
# closes its own capture handles, and stays alive until the runner ends the
# tree. Until assignment succeeds the retained ``Popen`` handle is the
# cleanup authority; afterwards the job is, and closing the job handle ends
# any straggler even after an internal failure. On POSIX the direct child
# leads a new session and the session group is the tree identity. The
# bounded ownership claim covers ordinary non-breakaway descendants created
# under the assigned job (Windows) or the owned session group (POSIX) — not
# unrelated, deliberately breakaway, externally re-parented, or hostile
# processes.


@dataclass(frozen=True)
class _NativeHandleFinalization:
    attempted: bool
    closed: bool
    failure: str | None


class _NativeHandleOwner:
    """One bounded owner for every Win32 handle acquired by this module.

    Acquisition returns no raw handle to the caller: a truthy identity is
    installed in the owner immediately. Finalization is attempted at most
    once, caches false/exception outcomes as one static code, and clears the
    identity only after a successful close.
    """

    def __init__(self, label: str, handle, closer) -> None:
        self._label = label
        self._handle = handle
        self._closer = closer
        self._outcome: _NativeHandleFinalization | None = None

    @classmethod
    def acquire(cls, label: str, acquire, closer):
        handle = acquire()
        if not handle:
            raise OSError(f"{label}_acquire_failed")
        return cls(label, handle, closer)

    @property
    def handle(self):
        return self._handle

    def finalize(self) -> _NativeHandleFinalization:
        if self._outcome is not None:
            return self._outcome
        if not self._handle:
            self._outcome = _NativeHandleFinalization(False, True, None)
            return self._outcome
        try:
            closed = bool(self._closer(self._handle))
        except Exception:
            closed = False
        failure = None if closed else f"{self._label}_close_failed"
        self._outcome = _NativeHandleFinalization(True, closed, failure)
        if closed:
            self._handle = None
        return self._outcome


def _causal_native_failure(cause: str, failures) -> OSError:
    bounded = [failure for failure in failures if failure]
    suffix = f"; cleanup={','.join(bounded)}" if bounded else ""
    return OSError(f"native handle transaction failed: {cause}{suffix}")

_ANCHOR_CODE = (
    "import base64, json, os, subprocess, sys, threading\n"
    "spec = json.load(sys.stdin)\n"
    "sys.stdin.close()\n"
    "stdin_bytes = (None if spec['stdin_base64'] is None else "
    "base64.b64decode(spec['stdin_base64'], validate=True))\n"
    "process = subprocess.Popen(\n"
    "    spec['argv'], cwd=spec['cwd'], env=spec['env'],\n"
    "    stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,\n"
    ")\n"
    "if stdin_bytes is None:\n"
    "    code = process.wait()\n"
    "else:\n"
    "    process.communicate(input=stdin_bytes)\n"
    "    code = process.returncode\n"
    "tmp = spec['status'] + '.tmp'\n"
    "with open(tmp, 'w', encoding='utf-8') as handle:\n"
    "    json.dump({'exit_code': code}, handle)\n"
    "    handle.flush()\n"
    "    os.fsync(handle.fileno())\n"
    "os.replace(tmp, spec['status'])\n"
    "os.close(1)\n"
    "os.close(2)\n"
    "threading.Event().wait()\n"
)

if os.name == "nt":
    import ctypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    _kernel32.SetInformationJobObject.restype = ctypes.c_int
    _kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
    ]
    _kernel32.OpenProcess.restype = ctypes.c_void_p
    _kernel32.OpenProcess.argtypes = [
        ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32,
    ]
    _kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    _kernel32.AssignProcessToJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    _kernel32.TerminateJobObject.restype = ctypes.c_int
    _kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _kernel32.CloseHandle.restype = ctypes.c_int
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount",
            )
        ]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    class _WindowsKillOnCloseJob:
        """Documented Win32 job object rooting the owned command tree at the
        live anchor: one terminate ends every member, including descendants
        whose recorded parent already exited, and closing the last handle
        ends any straggler."""

        def __init__(self) -> None:
            try:
                owner = _NativeHandleOwner.acquire(
                    "job_handle",
                    lambda: _kernel32.CreateJobObjectW(None, None),
                    _kernel32.CloseHandle,
                )
            except BaseException as error:
                raise _causal_native_failure("job_creation_failed", ()) from error
            self._owner = owner
            info = _ExtendedLimits()
            info.BasicLimitInformation.LimitFlags = (
                _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            cause: BaseException | None = None
            try:
                configured = bool(
                    _kernel32.SetInformationJobObject(
                        owner.handle,
                        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                        ctypes.byref(info),
                        ctypes.sizeof(info),
                    )
                )
                if not configured:
                    cause = OSError(
                        "could not configure the verification job object"
                    )
            except BaseException as error:
                cause = error
            if cause is not None:
                outcome = owner.finalize()
                raise _causal_native_failure(
                    "job_configuration_failed", (outcome.failure,)
                ) from cause

        @property
        def handle(self):
            return self._owner.handle

        def assign(self, pid: int) -> None:
            try:
                process = _NativeHandleOwner.acquire(
                    "assignment_process_handle",
                    lambda: _kernel32.OpenProcess(
                        _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, 0, pid
                    ),
                    _kernel32.CloseHandle,
                )
            except BaseException as error:
                raise _causal_native_failure(
                    "assignment_process_open_failed", ()
                ) from error
            cause: BaseException | None = None
            try:
                if not _kernel32.AssignProcessToJobObject(
                    self.handle, process.handle
                ):
                    cause = OSError("could not assign the anchor to the job")
            except BaseException as error:
                cause = error
            outcome = process.finalize()
            if cause is not None:
                raise _causal_native_failure(
                    "job_assignment_failed", (outcome.failure,)
                ) from cause
            if outcome.failure:
                raise _causal_native_failure(
                    "assignment_helper_finalization_failed", (outcome.failure,)
                )

        def terminate(self) -> bool:
            """Explicitly end every job member. The Win32 result is
            ownership-relevant (R3-F2): a failure routes to kill-on-close
            instead of being ignored."""
            return bool(_kernel32.TerminateJobObject(self.handle, 1))

        def close(self) -> _NativeHandleFinalization:
            return self._owner.finalize()
else:
    _WindowsKillOnCloseJob = None


_MAX_STATUS_BYTES = 4096


def _status_ready(path: Path) -> bool:
    return path.is_file()


def _read_status(path: Path) -> int | None:
    """Bounded strict read of the anchor's exit-status record; missing,
    malformed, or out-of-range records are an internal failure, never an
    invented exit code."""
    data = _bounded_read(path, _MAX_STATUS_BYTES)
    if data is None:
        return None
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    code = record.get("exit_code")
    if type(code) is not int or not (0 <= code < 2**32):
        return None
    return code


class _DrainState:
    __slots__ = ("captured", "total", "overflow", "done", "failures")

    def __init__(self) -> None:
        self.captured = bytearray()
        self.total = 0
        self.overflow = False
        self.done = False
        # Bounded worker-result channel (R4-F2): at most one read-failure
        # and one close-failure static code; never exception values or text.
        self.failures: list[str] = []


def _drain_stream(stream, limit: int, state: _DrainState, wake) -> None:
    """Data-only worker (R2-F3, R4-F2): captures at most ``limit`` bytes,
    owns its stream's closure, records read/close failures as bounded
    static codes on its state, signals the lifecycle owner in every case,
    and never terminates a process or lets an exception reach
    ``threading.excepthook``."""
    try:
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                state.total += len(chunk)
                if len(state.captured) < limit:
                    state.captured.extend(
                        chunk[: limit - len(state.captured)]
                    )
                if state.total > limit and not state.overflow:
                    state.overflow = True
                    wake.set()
        except Exception:
            state.failures.append("drain_read_failed")
        try:
            stream.close()
        except Exception:
            state.failures.append("drain_close_failed")
    finally:
        state.done = True
        wake.set()


# --------------------------------------------------------------------------
# Platform lifecycle controllers (R3-F2).
#
# Windows and POSIX have different completion and ownership authorities, so
# each platform gets its own explicit controller. They share only data-only
# capture helpers, bounded status parsing, result assembly, and the small
# resource seams below — never a completion predicate: stream EOF is a
# stream fact everywhere and never impersonates process completion.

_REAP_TIMEOUT_SECONDS = 10.0
_DRAIN_JOIN_SECONDS = 10.0
_LIFECYCLE_POLL_SECONDS = 0.05


def _spawn_anchor(cwd: Path):
    """Windows: spawn the runner-owned Python anchor; stdin carries the spec,
    so the anchor cannot start declared work before job assignment."""
    return subprocess.Popen(
        [sys.executable, "-c", _ANCHOR_CODE],
        cwd=str(cwd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _spawn_posix(argv, cwd, env, stdin_bytes=None):
    """POSIX: spawn the declared command leading its own session group."""
    stdin_stream = None
    try:
        if stdin_bytes is not None:
            stdin_stream = tempfile.TemporaryFile()
            stdin_stream.write(stdin_bytes)
            stdin_stream.seek(0)
        return subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=dict(env) if env is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=stdin_stream if stdin_stream is not None else subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        if stdin_stream is not None:
            stdin_stream.close()


def _start_drain(stream, limit: int, state: _DrainState, wake) -> threading.Thread:
    thread = threading.Thread(
        target=_drain_stream, args=(stream, limit, state, wake)
    )
    thread.start()
    return thread


def _terminate_anchor(process) -> None:
    """Direct-handle authority: end a live anchor that was never assigned."""
    process.kill()


def _reap_process(process) -> None:
    """The single bounded reap; the tree authority has already ended it."""
    process.wait(timeout=_REAP_TIMEOUT_SECONDS)


def _kill_posix_group(process) -> None:
    """Owned-group authority (POSIX): end the command's session group."""
    import signal

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _cleanup_failure_message(failures) -> str:
    """One owned aggregate outcome built from bounded static codes only —
    never exception values, tracebacks, PIDs, paths, or unbounded text."""
    return "verifier lifecycle cleanup failed: " + ",".join(failures)


def _finalize_job_handle(job) -> str | None:
    """Normalize the production owner and contract-conformant fakes into one
    bounded cleanup datum. No close exception escapes this boundary."""
    try:
        outcome = job.close()
    except Exception:
        return "job_handle_close_failed"
    if isinstance(outcome, _NativeHandleFinalization):
        return outcome.failure
    return None if bool(outcome) else "job_handle_close_failed"


def _finish_workers(workers) -> list[str]:
    """Bounded drain completion with explicit stream ownership (R4-F2).

    A successfully started worker owns its stream's closure; the lifecycle
    owner intervenes only when a worker never finished within its bound
    (its stream is left untouched — no concurrent double close) or when
    the worker reported it could not close. Worker read/close failure
    codes join the caller's aggregate."""
    failures: list[str] = []
    for thread, state, stream in workers:
        thread.join(_DRAIN_JOIN_SECONDS)
        if thread.is_alive():
            failures.append("drain_join_timeout")
            continue
        failures.extend(state.failures)
        if "drain_close_failed" in state.failures and stream is not None:
            try:
                if not stream.closed:
                    stream.close()
            except Exception:
                pass  # the worker already recorded the close failure
    return failures


def _close_unowned_streams(process, workers) -> list[str]:
    """Close capture streams no worker ever acquired; the lifecycle owner
    is the only closer for these, so no double-close path exists."""
    owned = {id(stream) for _, _, stream in workers}
    failures: list[str] = []
    for stream in (process.stdout, process.stderr):
        if stream is None or id(stream) in owned:
            continue
        try:
            if not stream.closed:
                stream.close()
        except Exception:
            failures.append("stream_close_failed")
    return failures


def _run_windows_lifecycle(
    argv,
    cwd,
    env,
    timeout_seconds: float,
    status_path: Path,
    out_state: _DrainState,
    err_state: _DrainState,
    max_stream_bytes: int,
    stdin_bytes=None,
) -> bool:
    """Explicit Windows lifecycle controller (R3-F2, R4-F2).

    Ownership transitions are explicit: the job is created and configured
    before anything spawns; from spawn until successful assignment the
    retained ``Popen`` handle is the cleanup authority; after assignment
    the job owns the tree. Cleanup is entered before the first post-spawn
    operation that can fail, attempts every applicable finalizer even after
    earlier failures, uses only bounded waits/joins, and ends in one owned
    aggregate failure whenever anything — including a drain worker —
    failed. Returns whether the declared deadline fired.
    """
    job = _WindowsKillOnCloseJob()
    try:
        process = _spawn_anchor(cwd)
    except BaseException as error:
        failure = _finalize_job_handle(job)
        if failure:
            raise _causal_native_failure(
                "anchor_spawn_failed", (failure,)
            ) from error
        raise
    assigned = False
    workers: list = []
    timed_out = False
    try:
        # Ownership transfer strictly precedes the anchor learning its work:
        # it blocks on stdin until the spec arrives.
        job.assign(process.pid)
        assigned = True
        spec = {
            "argv": list(argv),
            "cwd": str(cwd),
            "env": dict(env) if env is not None else None,
            "status": str(status_path),
            "stdin_base64": (
                None
                if stdin_bytes is None
                else base64.b64encode(stdin_bytes).decode("ascii")
            ),
        }
        process.stdin.write(json.dumps(spec, allow_nan=False).encode("utf-8"))
        process.stdin.close()
        wake = threading.Event()
        for stream, state in (
            (process.stdout, out_state),
            (process.stderr, err_state),
        ):
            workers.append(
                (
                    _start_drain(stream, max_stream_bytes, state, wake),
                    state,
                    stream,
                )
            )
        deadline = time.monotonic() + timeout_seconds
        while True:
            if out_state.failures or err_state.failures:
                break  # a worker failure authorizes cleanup (R4-F2)
            if out_state.overflow or err_state.overflow:
                break  # overflow authorizes tree cleanup
            if _status_ready(status_path):
                break  # the command finished; only the anchor tree remains
            if process.poll() is not None:
                break  # dead anchor without status is a runner failure
            if time.monotonic() >= deadline:
                timed_out = True
                break
            wake.wait(_LIFECYCLE_POLL_SECONDS)
            wake.clear()
    except BaseException as error:
        # Attempt-all finalization runs even when the body failed; a
        # non-empty aggregate becomes the owned outcome, chained to the
        # original failure.
        failures = _windows_cleanup(job, process, assigned, workers)
        if failures:
            raise OSError(_cleanup_failure_message(failures)) from error
        raise
    failures = _windows_cleanup(job, process, assigned, workers)
    if failures:
        raise OSError(_cleanup_failure_message(failures))
    return timed_out


def _windows_cleanup(job, process, assigned: bool, workers) -> list[str]:
    """Attempt-all bounded Windows finalization (R4-F2).

    Every applicable step is attempted even after earlier failures: close
    the spec input (so an anchor still blocked on input can exit), end the
    tree under its current authority — the direct handle before assignment,
    the job afterwards, with kill-on-close applied before any bounded wait
    when explicit termination fails or raises — finish drains, close
    streams under their correct owner, reap once with a bound, and close
    the job handle exactly once. Failures are recorded as bounded static
    codes and returned for the caller to own; nothing here raises, so no
    cleanup exception can skip a later finalizer."""
    failures: list[str] = []
    job_closed = False
    if process.stdin is not None and not process.stdin.closed:
        try:
            process.stdin.close()
        except Exception:
            failures.append("spec_close_failed")
    if not assigned:
        # Before successful assignment the direct process handle is the only
        # cleanup authority; terminating the (empty) job would not end the
        # still-live anchor.
        try:
            if process.poll() is None:
                _terminate_anchor(process)
        except Exception:
            failures.append("anchor_terminate_failed")
    else:
        terminated = False
        try:
            terminated = bool(job.terminate())
        except Exception:
            terminated = False
        if not terminated:
            # Explicit termination failed or raised: apply kill-on-close
            # before any bounded wait below depends on the tree being dead.
            failures.append("job_terminate_failed")
            job_closed = True
            failure = _finalize_job_handle(job)
            if failure:
                failures.append(failure)
    failures.extend(_finish_workers(workers))
    failures.extend(_close_unowned_streams(process, workers))
    try:
        _reap_process(process)
    except Exception:
        failures.append("reap_failed")
    if not job_closed:
        failure = _finalize_job_handle(job)
        if failure:
            failures.append(failure)
    return failures


def _run_posix_lifecycle(
    argv,
    cwd,
    env,
    timeout_seconds: float,
    out_state: _DrainState,
    err_state: _DrainState,
    max_stream_bytes: int,
    stdin_bytes=None,
) -> tuple[int | None, bool]:
    """Explicit POSIX lifecycle controller (R3-F2).

    Command completion is ``poll()``; capture EOF is only a stream fact. If
    both drains reach EOF while the command is still running, the controller
    remains in the lifecycle until the real exit or the declared deadline.
    Termination is authorized by timeout, overflow, internal failure, or
    owned-group cleanup after real completion — never by capture completion
    alone — and the command's real exit code is never replaced.
    """
    process = (
        _spawn_posix(argv, cwd, env)
        if stdin_bytes is None
        else _spawn_posix(argv, cwd, env, stdin_bytes)
    )
    workers: list = []
    exit_code: int | None = None
    timed_out = False
    try:
        wake = threading.Event()
        for stream, state in (
            (process.stdout, out_state),
            (process.stderr, err_state),
        ):
            workers.append(
                (
                    _start_drain(stream, max_stream_bytes, state, wake),
                    state,
                    stream,
                )
            )
        deadline = time.monotonic() + timeout_seconds
        while True:
            code = process.poll()
            if code is not None:
                exit_code = code  # the real completion authority
                break
            if out_state.failures or err_state.failures:
                break  # a worker failure authorizes cleanup (R4-F2)
            if out_state.overflow or err_state.overflow:
                break  # overflow authorizes ending the live group
            if time.monotonic() >= deadline:
                timed_out = True
                break
            wake.wait(_LIFECYCLE_POLL_SECONDS)
            wake.clear()
    except BaseException as error:
        failures = _posix_cleanup(process, workers)
        if failures:
            raise OSError(_cleanup_failure_message(failures)) from error
        raise
    failures = _posix_cleanup(process, workers)
    if failures:
        raise OSError(_cleanup_failure_message(failures))
    if exit_code is None and not timed_out:
        # Ended under the overflow authority before exit: the reaped code
        # is the honest outcome.
        exit_code = process.returncode
    return exit_code, timed_out


def _posix_cleanup(process, workers) -> list[str]:
    """Attempt-all bounded POSIX finalization (R4-F2).

    The group kill is authorized here by timeout, overflow, worker or
    internal failure while the command lives, and is owned descendant
    cleanup after real completion; a dead group is a no-op. If group
    termination raises while the direct process remains live, one direct
    termination through the retained handle is attempted. Either failure
    is recorded as a bounded static code and every remaining applicable
    finalizer — drains, ownership-correct stream closure, one bounded
    reap — still runs. The command's recorded exit code is never replaced,
    and no universal claim is made that an uncooperative operating system
    killed the external process; the guarantee is locally complete
    finalization plus one owned failure."""
    failures: list[str] = []
    group_ended = True
    try:
        _kill_posix_group(process)
    except Exception:
        group_ended = False
        failures.append("group_terminate_failed")
    if not group_ended:
        try:
            if process.poll() is None:
                process.kill()
        except Exception:
            failures.append("process_terminate_failed")
    failures.extend(_finish_workers(workers))
    failures.extend(_close_unowned_streams(process, workers))
    try:
        _reap_process(process)
    except Exception:
        failures.append("reap_failed")
    return failures


class SubprocessRunner:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def run(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        env: Mapping[str, str] | None,
        timeout_seconds: float,
        stdout_path: Path,
        stderr_path: Path,
        max_stream_bytes: int = MAX_STREAM_CAPTURE_BYTES,
        stdin_bytes: bytes | None = None,
    ) -> ProcessOutcome:
        # R4-F1: the defensive pre-effect timeout boundary — the same
        # normalizer as the declaration boundary, applied before anything
        # can spawn, so no non-finite or non-plain deadline reaches a
        # lifecycle controller even on a direct runner call.
        timeout_seconds = _validate_timeout_seconds(timeout_seconds)
        started = self._clock.now()
        stdout_path, stderr_path = Path(stdout_path), Path(stderr_path)
        status_path = stdout_path.with_name(
            stdout_path.name + ".anchor_status.json"
        )
        out_state, err_state = _DrainState(), _DrainState()
        timed_out = False
        exit_code: int | None = None
        try:
            if os.name == "nt":
                if stdin_bytes is None:
                    timed_out = _run_windows_lifecycle(
                        argv, cwd, env, timeout_seconds, status_path,
                        out_state, err_state, max_stream_bytes,
                    )
                else:
                    timed_out = _run_windows_lifecycle(
                        argv, cwd, env, timeout_seconds, status_path,
                        out_state, err_state, max_stream_bytes, stdin_bytes,
                    )
                if not timed_out:
                    exit_code = _read_status(status_path)
            else:
                if stdin_bytes is None:
                    exit_code, timed_out = _run_posix_lifecycle(
                        argv, cwd, env, timeout_seconds,
                        out_state, err_state, max_stream_bytes,
                    )
                else:
                    exit_code, timed_out = _run_posix_lifecycle(
                        argv, cwd, env, timeout_seconds,
                        out_state, err_state, max_stream_bytes, stdin_bytes,
                    )
        finally:
            for control in (status_path, Path(str(status_path) + ".tmp")):
                try:
                    control.unlink(missing_ok=True)
                except OSError:
                    pass
        stdout_path.write_bytes(bytes(out_state.captured))
        stderr_path.write_bytes(bytes(err_state.captured))
        return ProcessOutcome(
            started,
            self._clock.now(),
            exit_code,
            timed_out,
            stdout_overflow=out_state.overflow,
            stderr_overflow=err_state.overflow,
        )


class Verifier:
    def __init__(
        self,
        store: RunStore,
        runner: ProcessRunner,
        clock: Clock,
        env_profiles: Mapping[str, Mapping[str, str] | None] | None = None,
    ) -> None:
        self._store = store
        self._runner = runner
        self._clock = clock
        self._env_profiles = dict(env_profiles or {"default": None})
        self._workspace_tools = WorkspaceManager

    def verify(
        self,
        attempt_dir: Path,
        plan: VerificationPlan,
        workspace_root: Path,
        *,
        base_revision: str | None = None,
        final_revision_reader: Callable[[], str | None] = lambda: None,
    ) -> VerificationOutcome:
        workspace_root = Path(workspace_root)
        before_stats = _stat_snapshot(workspace_root)
        before_hashes = _hash_snapshot(workspace_root)

        files: dict[str, bytes] = {}
        command_records: list[dict[str, object]] = []
        all_ok = True
        with tempfile.TemporaryDirectory() as scratch:
            for index, command in enumerate(plan.commands):
                if command.env_profile not in self._env_profiles:
                    raise ValueError(
                        f"unknown verification env profile: {command.env_profile}"
                    )
                cwd = (workspace_root / command.cwd).resolve()
                if not _within(cwd, workspace_root.resolve()):
                    raise ValueError(
                        "verification command cwd escapes the workspace"
                    )
                stdout_tmp = Path(scratch) / f"cmd_{index:03d}_stdout.txt"
                stderr_tmp = Path(scratch) / f"cmd_{index:03d}_stderr.txt"
                profile_env = self._env_profiles[command.env_profile]
                env = dict(os.environ if profile_env is None else profile_env)
                env["PYTHONDONTWRITEBYTECODE"] = "1"
                outcome = self._runner.run(
                    tuple(command.argv),
                    cwd,
                    env,
                    command.timeout_seconds,
                    stdout_tmp,
                    stderr_tmp,
                    max_stream_bytes=command.max_stream_bytes,
                )
                stdout_bytes = stdout_tmp.read_bytes()
                stderr_bytes = stderr_tmp.read_bytes()
                stdout_name = f"cmd_{index:03d}_stdout.txt"
                stderr_name = f"cmd_{index:03d}_stderr.txt"
                files[stdout_name] = stdout_bytes
                files[stderr_name] = stderr_bytes
                overflowed = outcome.stdout_overflow or outcome.stderr_overflow
                ok = (
                    (not outcome.timed_out)
                    and (not overflowed)
                    and outcome.exit_code == 0
                )
                all_ok = all_ok and ok
                command_records.append(
                    {
                        "index": index,
                        "argv": command.argv,
                        "cwd": command.cwd,
                        "env_profile": command.env_profile,
                        "started": outcome.started,
                        "ended": outcome.ended,
                        "timeout_seconds": command.timeout_seconds,
                        "max_stream_bytes": command.max_stream_bytes,
                        "exit_code": outcome.exit_code,
                        "timed_out": outcome.timed_out,
                        "stdout_path": f"verification/{stdout_name}",
                        "stderr_path": f"verification/{stderr_name}",
                        "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
                        "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
                        "stdout_captured_bytes": len(stdout_bytes),
                        "stderr_captured_bytes": len(stderr_bytes),
                        "stdout_overflow": outcome.stdout_overflow,
                        "stderr_overflow": outcome.stderr_overflow,
                    }
                )

        after_stats = _stat_snapshot(workspace_root)
        after_hashes = _hash_snapshot(workspace_root)
        dirty = sorted(
            path
            for path in set(before_stats) | set(after_stats)
            if before_stats.get(path) != after_stats.get(path)
            or before_hashes.get(path) != after_hashes.get(path)
        )
        declared = set(plan.declared_regenerated)
        dirty_ok = all(
            path in declared
            and before_hashes.get(path) is not None
            and before_hashes.get(path) == after_hashes.get(path)
            for path in dirty
        )
        passed = all_ok and dirty_ok

        run_id, slice_id, attempt_id = _attempt_identity(self._store, attempt_dir)
        evidence = _evidence_toml(
            passed=passed,
            run_id=run_id,
            slice_id=slice_id,
            attempt_id=attempt_id,
            base_revision=base_revision,
            final_revision=final_revision_reader(),
            dirty_files=tuple(dirty),
            commands=command_records,
        )
        files["evidence.toml"] = evidence
        evidence_dir = self._store.publish_verification(attempt_dir, files)
        return VerificationOutcome(
            passed,
            evidence_dir,
            tuple(dirty),
            hashlib.sha256(evidence).hexdigest(),
        )


def _attempt_identity(store: RunStore, attempt_dir: Path) -> tuple[str, str, str]:
    resolved = store._owned_attempt_dir(attempt_dir)
    return (
        resolved.parent.parent.parent.name,
        resolved.parent.name,
        resolved.name,
    )


def _evidence_toml(
    *,
    passed: bool,
    run_id: str,
    slice_id: str,
    attempt_id: str,
    base_revision: str | None,
    final_revision: str | None,
    dirty_files: tuple[str, ...],
    commands: list[dict[str, object]],
) -> bytes:
    lines = [
        f"passed = {'true' if passed else 'false'}",
        f"run_id = {_toml_string(run_id)}",
        f"slice_id = {_toml_string(slice_id)}",
        f"attempt_id = {_toml_string(attempt_id)}",
        f"base_revision = {_toml_string(base_revision or '')}",
        f"final_revision = {_toml_string(final_revision or '')}",
        "dirty_files = ["
        + ", ".join(_toml_string(path) for path in dirty_files)
        + "]",
    ]
    for record in commands:
        lines.append("")
        lines.append("[[command]]")
        lines.append(f"index = {record['index']}")
        lines.append(
            "argv = ["
            + ", ".join(_toml_string(part) for part in record["argv"])
            + "]"
        )
        lines.append(f"cwd = {_toml_string(str(record['cwd']))}")
        lines.append(f"env_profile = {_toml_string(str(record['env_profile']))}")
        lines.append(f"started = {float(record['started'])!r}")
        lines.append(f"ended = {float(record['ended'])!r}")
        lines.append(f"timeout_seconds = {float(record['timeout_seconds'])!r}")
        exit_code = record["exit_code"]
        lines.append(f"exit_code = {exit_code if exit_code is not None else -1}")
        lines.append(f"timed_out = {'true' if record['timed_out'] else 'false'}")
        lines.append(f"stdout_path = {_toml_string(str(record['stdout_path']))}")
        lines.append(f"stderr_path = {_toml_string(str(record['stderr_path']))}")
        lines.append(f"stdout_sha256 = {_toml_string(str(record['stdout_sha256']))}")
        lines.append(f"stderr_sha256 = {_toml_string(str(record['stderr_sha256']))}")
        lines.append(f"max_stream_bytes = {int(record['max_stream_bytes'])}")
        lines.append(
            f"stdout_captured_bytes = {int(record['stdout_captured_bytes'])}"
        )
        lines.append(
            f"stderr_captured_bytes = {int(record['stderr_captured_bytes'])}"
        )
        lines.append(
            f"stdout_overflow = {'true' if record['stdout_overflow'] else 'false'}"
        )
        lines.append(
            f"stderr_overflow = {'true' if record['stderr_overflow'] else 'false'}"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


_MAX_EVIDENCE_RECORD_BYTES = 4 * 1024 * 1024
_MAX_STREAM_READ_BYTES = 2 * 1024 * 1024


def _bounded_read(path: Path, cap: int) -> bytes | None:
    try:
        with open(path, "rb") as stream:
            data = stream.read(cap + 1)
    except OSError:
        return None
    if len(data) > cap:
        return None
    return data


def validate_evidence(
    store: RunStore, attempt_dir: Path, expected_sha256: str | None
) -> str | None:
    return _validate_evidence(
        store, attempt_dir, expected_sha256, require_passed=True
    )


def validate_evidence_envelope(
    store: RunStore, attempt_dir: Path, expected_sha256: str | None
) -> str | None:
    """Validate an immutable prior evidence envelope without adopting its
    pass/fail decision. H004 uses this before current-run re-verification."""
    return _validate_evidence(
        store, attempt_dir, expected_sha256, require_passed=False
    )


def _validate_evidence(
    store: RunStore,
    attempt_dir: Path,
    expected_sha256: str | None,
    *,
    require_passed: bool,
) -> str | None:
    """Durable evidence-validation boundary (R1-F3, R2-F1).

    Returns a stable violation code, or None when the published evidence is
    present, well-formed, passing, hash-bound to the journaled verification
    event, bound to this exact attempt, an exact inventory of ordinary-file
    immediate entries, and byte-consistent with every recorded command
    stream. The journal boolean alone is routing metadata. The claim covers
    the member names and no-follow entry types observed during this one
    validation pass; it does not model a concurrently mutating hostile
    filesystem.
    """
    import tomllib

    run_id, slice_id, attempt_id = _attempt_identity(store, attempt_dir)
    evidence_dir = store.verification_dir(attempt_dir)
    evidence_path = evidence_dir / "evidence.toml"
    if not evidence_path.is_file():
        return "evidence_missing"
    raw = _bounded_read(evidence_path, _MAX_EVIDENCE_RECORD_BYTES)
    if raw is None:
        return "evidence_malformed"
    if expected_sha256 is None or hashlib.sha256(raw).hexdigest() != expected_sha256:
        return "evidence_hash_mismatch"
    try:
        record = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return "evidence_malformed"
    if type(record.get("passed")) is not bool:
        return "evidence_malformed"
    if require_passed and record.get("passed") is not True:
        return "evidence_failed"
    if (
        record.get("run_id") != run_id
        or record.get("slice_id") != slice_id
        or record.get("attempt_id") != attempt_id
    ):
        return "evidence_attempt_mismatch"
    commands = record.get("command", [])
    if not isinstance(commands, list):
        return "evidence_malformed"
    expected_members = {"evidence.toml"}
    for index, command in enumerate(commands):
        if not isinstance(command, dict) or command.get("index") != index:
            return "evidence_command_confused"
        for stream_kind in ("stdout", "stderr"):
            name = f"cmd_{index:03d}_{stream_kind}.txt"
            if command.get(f"{stream_kind}_path") != f"verification/{name}":
                return "evidence_command_confused"
            expected_members.add(name)

    # Closed inventory (R2-F1): every immediate entry participates in the
    # decision. One bounded pass — at most the expected count plus the first
    # unexpected entry — refuses unknown names immediately, requires each
    # entry's no-follow type to be an ordinary file (directories, links and
    # other reparse entries, devices, and renames all refuse), and then
    # requires every expected name to have been observed. Only after the
    # inventory closes are the recorded stream bytes read and re-hashed.
    observed: set[str] = set()
    try:
        with os.scandir(evidence_dir) as entries:
            for entry in entries:
                if entry.name not in expected_members or entry.name in observed:
                    return "evidence_extra_member"
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    return "evidence_member_not_regular"
                observed.add(entry.name)
    except OSError:
        return "evidence_missing"
    if observed != expected_members:
        return "evidence_stream_missing"

    for index, command in enumerate(commands):
        for stream_kind in ("stdout", "stderr"):
            name = f"cmd_{index:03d}_{stream_kind}.txt"
            data = _bounded_read(evidence_dir / name, _MAX_STREAM_READ_BYTES)
            if data is None or hashlib.sha256(data).hexdigest() != command.get(
                f"{stream_kind}_sha256"
            ):
                return "evidence_stream_tampered"
    return None


def _stat_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for current, dirnames, filenames in os.walk(root):
        relative_dir = Path(current).relative_to(root)
        if relative_dir.parts and relative_dir.parts[0] in (".git", ".frutlups_drive"):
            dirnames[:] = []
            continue
        dirnames[:] = [
            name
            for name in dirnames
            if not (not relative_dir.parts and name in (".git", ".frutlups_drive"))
        ]
        for filename in filenames:
            path = Path(current) / filename
            try:
                stat = path.stat()
            except OSError:
                continue
            result[(relative_dir / filename).as_posix()] = (
                stat.st_size,
                stat.st_mtime_ns,
            )
    return result


def _hash_snapshot(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for current, dirnames, filenames in os.walk(root):
        relative_dir = Path(current).relative_to(root)
        if relative_dir.parts and relative_dir.parts[0] in (".git", ".frutlups_drive"):
            dirnames[:] = []
            continue
        dirnames[:] = [
            name
            for name in dirnames
            if not (not relative_dir.parts and name in (".git", ".frutlups_drive"))
        ]
        for filename in filenames:
            path = Path(current) / filename
            try:
                data = path.read_bytes()
            except OSError:
                continue
            result[(relative_dir / filename).as_posix()] = hashlib.sha256(
                data
            ).hexdigest()
    return result


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
