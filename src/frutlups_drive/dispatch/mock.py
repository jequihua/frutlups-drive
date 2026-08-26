"""Deterministic scripted mock executor.

Each dispatch consumes one :class:`MockAgentAction`: relative writes land in
the request's workspace, absolute writes model adversarial escapes exactly as
scripted, and the returned :class:`AgentRunResult` carries only facts. A
transcript is written as the provider event log under the executor's owned
log directory. No network, real agent, or sleep is involved.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from frutlups_drive.budget import _validate_cost_fact
from frutlups_drive.contracts import AgentRunRequest, AgentRunResult
from frutlups_drive.dispatch.base import (
    CostAuthorizationExceeded,
    ExecutorScriptExhausted,
    WorkspaceAuthorityDenied,
)
from frutlups_drive.workspace import authorize_workspace_writes


@dataclass(frozen=True)
class MockAgentAction:
    writes: tuple[tuple[str, str], ...] = ()
    absolute_writes: tuple[tuple[str, str], ...] = ()
    status: str = "completed"
    exit_reason: str = "mock_completed"
    cost_usd: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    provider_duration_seconds: float | None = None
    observed_duration_seconds: float | None = None
    retry_class: str = "not_applicable"
    cost_knowledge: str | None = None
    capture_truncated: bool = False
    transcript: tuple[str, ...] = ("mock dispatch",)
    raise_error: bool = False
    changed_files_override: tuple[str, ...] | None = None
    produced_override: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        # R2-F2: a scripted cost fact is validated and normalized at
        # construction; an invalid declaration never becomes an action.
        object.__setattr__(self, "cost_usd", _validate_cost_fact(self.cost_usd))


@dataclass
class MockAgentExecutor:
    actions: Sequence[MockAgentAction]
    log_dir: Path
    consumed: int = 0
    store_root: Path | None = None
    allowed_prefixes: tuple[str, ...] | None = None
    _written: list[tuple[Path, str]] = field(default_factory=list)

    def execute(self, request: AgentRunRequest) -> AgentRunResult:
        if self.consumed >= len(self.actions):
            raise ExecutorScriptExhausted(
                f"mock executor script exhausted after {len(self.actions)} actions"
            )
        action = self.actions[self.consumed]
        self.consumed += 1
        if action.raise_error:
            raise RuntimeError("scripted provider failure")

        workspace = Path(request.workspace)
        # Pre-effect gates (R1-F1, R1-F5, R2-F2): the complete intended action
        # is authorized before its first byte. Only a validated finite
        # non-negative cost fact may be compared against the authorization;
        # a tampered fact refuses before any path creation, write, or log.
        try:
            cost_fact = _validate_cost_fact(action.cost_usd)
        except ValueError:
            raise CostAuthorizationExceeded(0.0, 0.0) from None
        if (
            cost_fact is not None
            and request.max_cost_usd is not None
            and cost_fact > request.max_cost_usd
        ):
            raise CostAuthorizationExceeded(cost_fact, request.max_cost_usd)
        intended = [relative for relative, _ in action.writes] + [
            absolute for absolute, _ in action.absolute_writes
        ]
        if intended:
            store_root = (
                self.store_root
                if self.store_root is not None
                else workspace / ".frutlups_drive"
            )
            violations = authorize_workspace_writes(
                workspace,
                store_root,
                intended,
                workspace_access=request.workspace_access,
                expected_artifacts=request.expected_artifacts,
                allowed_prefixes=self.allowed_prefixes,
            )
            if violations:
                raise WorkspaceAuthorityDenied(violations)
        changed: list[Path] = []
        for relative, content in action.writes:
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
            changed.append(Path(relative))
        for absolute, content in action.absolute_writes:
            target = Path(absolute)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
            changed.append(target)

        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{request.run_id}_{request.attempt_id}.jsonl"
        lines = [
            json.dumps({"event": line}, sort_keys=True, separators=(",", ":"))
            for line in action.transcript
        ]
        log_path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))

        if action.changed_files_override is not None:
            changed = [Path(p) for p in action.changed_files_override]
        produced = (
            tuple(Path(p) for p in action.produced_override)
            if action.produced_override is not None
            else tuple(request.expected_artifacts)
        )
        return AgentRunResult(
            status=action.status,
            event_log_path=log_path,
            changed_files=tuple(changed),
            produced_artifacts=produced,
            exit_reason=action.exit_reason,
            tokens_in=action.tokens_in,
            tokens_out=action.tokens_out,
            cost_usd=cost_fact,
            provider_duration_seconds=action.provider_duration_seconds,
            observed_duration_seconds=action.observed_duration_seconds,
            retry_class=action.retry_class,
            cost_knowledge=(
                action.cost_knowledge
                if action.cost_knowledge is not None
                else ("measured" if cost_fact is not None else "unknown")
            ),
            capture_truncated=action.capture_truncated,
        )
