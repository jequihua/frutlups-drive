"""Provider-neutral bounded subprocess agent executor (M003 Phase A).

One generic mechanism for running an explicitly declared local command as an
agent attempt. It is not a provider adapter: it has no provider names, no
default executable, no PATH discovery, no output parsing, and no model
semantics. Phase A exercises it only with deterministic local stubs launched
through the official interpreter; later thin provider bindings may reuse it
under a separately owner-approved live gate.

Invariants (round-two corrected):

- the exact argv, declared child environment, and timeout come from one
  immutable :class:`AgentCommandSpec` fixed before spawn; command text never
  comes from an agent artifact and ``shell=True`` is never used;
- execution-directory authority belongs to the request alone: the child
  always runs in the exact ``AgentRunRequest.workspace``, which must be an
  existing directory before any log creation or spawn; the spec carries no
  cwd field;
- the child environment is always the spec's explicit finite name/value
  pairs — the empty tuple means an explicitly empty child environment; there
  is no ambient-inheritance mode and this module never consults the parent
  environment;
- process lifecycle is owned entirely by the injected, accepted
  ``verifier.ProcessRunner`` (composition; no duplicated tree ownership);
- stdout/stderr captures and one canonical LF strict-JSON event log land
  under the executor's owned log root with unique never-clobbered names
  derived from the owned request identity;
- durable events identify the command only by the caller-declared bounded
  ``command_id`` and argument count — never argv, executable path or
  basename, workspace path, raw arguments, environment values, or child
  output;
- every transport outcome maps to a bounded typed :class:`AgentRunResult`
  fact or the one owned :class:`SubprocessAgentFailure`; stdout/stderr are
  never parsed for a verdict, token count, cost, or artifact validity, and
  tokens/cost stay ``None`` in Phase A.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from frutlups_drive.contracts import AgentRunRequest, AgentRunResult
from frutlups_drive.verifier import MAX_STREAM_CAPTURE_BYTES, ProcessRunner


class SubprocessAgentFailure(Exception):
    """The one stable owned executor failure (bounded code, owned message)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def checked_timeout_seconds(value: object) -> float:
    """The one shared plain-number timeout admission for Phase A commands.

    Accepts only values whose exact type is the built-in ``int`` or
    ``float`` and whose conversion — performed inside this owned overflow
    boundary — yields a finite, strictly positive ``float``. Every invalid
    value, including arbitrarily large integers of either sign, produces the
    same bounded ``ValueError`` without echoing the value; no subclass
    conversion hook ever runs.
    """
    if type(value) is not int and type(value) is not float:
        raise ValueError("timeout_seconds must be a positive finite number")
    try:
        timeout = float(value)
    except OverflowError:
        raise ValueError(
            "timeout_seconds must be a positive finite number"
        ) from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be a positive finite number")
    return timeout


_COMMAND_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


@dataclass(frozen=True)
class AgentCommandSpec:
    """Immutable declaration of the exact local command an executor may run.

    ``command_id`` is the caller-declared bounded audit label published in
    durable evidence in place of argv; it is never inferred from an
    executable path or provider output. ``env`` is the complete explicit
    child environment as unique ``(name, value)`` pairs; the default empty
    tuple declares an explicitly empty child environment. Ambient
    inheritance is not representable.
    """

    argv: tuple[str, ...]
    command_id: str
    env: tuple[tuple[str, str], ...] = ()
    timeout_seconds: float = 300.0
    max_stream_bytes: int = MAX_STREAM_CAPTURE_BYTES
    prompt_capture_name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.argv, tuple) or not self.argv:
            raise ValueError("argv must be a non-empty tuple of strings")
        for part in self.argv:
            if not isinstance(part, str) or not part:
                raise ValueError("argv must be a non-empty tuple of strings")
        if not Path(self.argv[0]).is_absolute():
            raise ValueError(
                "argv[0] must be an absolute executable path; PATH "
                "discovery is not part of this executor"
            )
        if not isinstance(self.command_id, str) or not (
            self.command_id.isascii() and _COMMAND_ID.fullmatch(self.command_id)
        ):
            raise ValueError(
                "command_id must be 1-64 ASCII letters, digits, dot, "
                "underscore, or dash"
            )
        if not isinstance(self.env, tuple):
            raise ValueError(
                "env must be a tuple of unique (name, value) string pairs; "
                "ambient inheritance is not available"
            )
        seen_names = set()
        for entry in self.env:
            if (
                not isinstance(entry, tuple)
                or len(entry) != 2
                or not isinstance(entry[0], str)
                or not isinstance(entry[1], str)
                or not entry[0]
                or "=" in entry[0]
                or "\x00" in entry[0]
                or "\x00" in entry[1]
                or entry[0] in seen_names
            ):
                raise ValueError(
                    "env must be a tuple of unique (name, value) string "
                    "pairs; ambient inheritance is not available"
                )
            seen_names.add(entry[0])
        object.__setattr__(
            self, "timeout_seconds", checked_timeout_seconds(self.timeout_seconds)
        )
        if type(self.max_stream_bytes) is not int or not (
            1 <= self.max_stream_bytes <= MAX_STREAM_CAPTURE_BYTES
        ):
            raise ValueError(
                "max_stream_bytes must be an integer between 1 and "
                f"{MAX_STREAM_CAPTURE_BYTES}"
            )
        if self.prompt_capture_name is not None and (
            not isinstance(self.prompt_capture_name, str)
            or not self.prompt_capture_name.isascii()
            or not _COMMAND_ID.fullmatch(self.prompt_capture_name)
        ):
            raise ValueError(
                "prompt_capture_name must use the bounded command-id grammar"
            )


@dataclass(frozen=True)
class CommandObservation:
    """Bounded classification of one command run through the accepted runner.

    ``kind`` is one of: ``missing_executable`` (no spawn happened),
    ``runner_failure`` (the runner raised: spawn failure, exhausted scripted
    transport, or runner-owned cleanup/capture failure), ``timeout``,
    ``overflow``, ``no_status`` (the runner reported neither an exit code
    nor a timeout), or ``exit`` (a real exit code in ``exit_code``).
    """

    kind: str
    exit_code: int | None = None
    stdout_overflow: bool = False
    stderr_overflow: bool = False


def observe_command(
    argv: tuple[str, ...],
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: float,
    runner: ProcessRunner,
    stdout_path: Path,
    stderr_path: Path,
    max_stream_bytes: int = MAX_STREAM_CAPTURE_BYTES,
) -> CommandObservation:
    """Run one declared command and classify its transport outcome.

    Shared by the agent executor and the provisional plan provider because
    it carries only process facts — no planning or agent semantics. The
    child environment is always the caller's explicit mapping (possibly
    empty); ambient inheritance is not available. The injected runner owns
    the process tree; this helper never spawns, kills, or waits itself.
    """
    if not Path(argv[0]).is_file():
        return CommandObservation("missing_executable")
    try:
        outcome = runner.run(
            tuple(argv),
            Path(cwd),
            dict(env),
            timeout_seconds,
            Path(stdout_path),
            Path(stderr_path),
            max_stream_bytes=max_stream_bytes,
        )
    except Exception:
        # The accepted runner already reduced its failure to bounded owned
        # text; this layer records only the classification, never the text.
        return CommandObservation("runner_failure")
    if outcome.timed_out:
        return CommandObservation(
            "timeout",
            stdout_overflow=outcome.stdout_overflow,
            stderr_overflow=outcome.stderr_overflow,
        )
    if outcome.stdout_overflow or outcome.stderr_overflow:
        return CommandObservation(
            "overflow",
            exit_code=outcome.exit_code,
            stdout_overflow=outcome.stdout_overflow,
            stderr_overflow=outcome.stderr_overflow,
        )
    if outcome.exit_code is None:
        return CommandObservation("no_status")
    return CommandObservation("exit", exit_code=outcome.exit_code)


_OBSERVATION_FACTS = {
    # kind -> (AgentRunResult.status, exit_reason)
    "missing_executable": ("failed", "agent_executable_missing"),
    "timeout": ("timeout", "agent_timeout"),
    "overflow": ("failed", "agent_stream_overflow"),
    "no_status": ("failed", "agent_status_unavailable"),
}


class SubprocessAgentExecutor:
    """Generic bounded subprocess executor behind the ``AgentExecutor``
    protocol. The spec is injected and immutable; the request contributes
    identity, the one execution workspace, expected-artifact names, and its
    own time budget (the effective timeout is the smaller of the spec and
    request bounds)."""

    def __init__(
        self, spec: AgentCommandSpec, runner: ProcessRunner, log_root: Path
    ) -> None:
        self._spec = spec
        self._runner = runner
        self._log_root = Path(log_root)

    def execute(self, request: AgentRunRequest) -> AgentRunResult:
        workspace = Path(request.workspace)
        if not workspace.is_dir():
            # Before any log creation or spawn; the owned message never
            # echoes the missing path.
            raise SubprocessAgentFailure(
                "workspace_missing",
                "the request workspace is not an existing directory; "
                "nothing was created or spawned",
            )
        base = f"{request.run_id}_{request.attempt_id}"
        events_path = self._log_root / f"{base}_events.jsonl"
        stdout_path = self._log_root / f"{base}_stdout.txt"
        stderr_path = self._log_root / f"{base}_stderr.txt"
        for existing in (events_path, stdout_path, stderr_path):
            if existing.exists():
                raise SubprocessAgentFailure(
                    "capture_conflict",
                    "a capture for this request identity already exists; "
                    "an earlier observation is never clobbered",
                )
        self._log_root.mkdir(parents=True, exist_ok=True)
        timeout = min(self._spec.timeout_seconds, float(request.max_seconds))
        observation = observe_command(
            self._spec.argv,
            workspace,
            dict(self._spec.env),
            timeout,
            self._runner,
            stdout_path,
            stderr_path,
            max_stream_bytes=self._spec.max_stream_bytes,
        )
        self._write_event_log(events_path, request, timeout, observation)
        if observation.kind == "runner_failure":
            raise SubprocessAgentFailure(
                "runner_failure",
                "the bounded process runner failed before or during the "
                "declared command; no output was interpreted",
            )
        if observation.kind == "exit":
            status = "completed" if observation.exit_code == 0 else "failed"
            exit_reason = (
                "agent_exit_clean"
                if observation.exit_code == 0
                else "agent_exit_nonzero"
            )
        else:
            status, exit_reason = _OBSERVATION_FACTS[observation.kind]
        produced = tuple(
            artifact
            for artifact in request.expected_artifacts
            if (workspace / artifact).is_file()
        )
        return AgentRunResult(
            status=status,
            event_log_path=events_path,
            changed_files=(),
            produced_artifacts=produced,
            exit_reason=exit_reason,
            tokens_in=None,
            tokens_out=None,
            cost_usd=None,
        )

    def _write_event_log(
        self,
        events_path: Path,
        request: AgentRunRequest,
        timeout: float,
        observation: CommandObservation,
    ) -> None:
        lines = [
            {
                "event": "agent_dispatch",
                "run_id": request.run_id,
                "attempt_id": request.attempt_id,
                "role": request.role.value,
                "adapter": request.adapter,
                "model": request.model,
                "effort": request.effort,
                "command_id": self._spec.command_id,
                "argument_count": len(self._spec.argv),
                "timeout_seconds": timeout,
                "max_stream_bytes": self._spec.max_stream_bytes,
                "env_names": sorted(name for name, _ in self._spec.env),
            },
            {
                "event": "agent_outcome",
                "kind": observation.kind,
                "exit_code": observation.exit_code,
                "stdout_overflow": observation.stdout_overflow,
                "stderr_overflow": observation.stderr_overflow,
                "stdout_capture": events_path.name.replace(
                    "_events.jsonl", "_stdout.txt"
                ),
                "stderr_capture": events_path.name.replace(
                    "_events.jsonl", "_stderr.txt"
                ),
            },
        ]
        if self._spec.prompt_capture_name is not None:
            lines[0]["prompt_capture"] = self._spec.prompt_capture_name
        serialized = "\n".join(
            json.dumps(
                line,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            for line in lines
        )
        events_path.write_bytes((serialized + "\n").encode("utf-8"))
