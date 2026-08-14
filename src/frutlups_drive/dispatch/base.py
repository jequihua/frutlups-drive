"""Agent executor protocol (architecture contract §4).

Executors receive an :class:`AgentRunRequest` and return only an
:class:`AgentRunResult` of facts. They never recommend loop actions or
verdicts; an executor exception is a provider failure the supervisor journals
and bounds.
"""

from __future__ import annotations

from typing import Protocol

from frutlups_drive.contracts import AgentRunRequest, AgentRunResult


class AgentExecutor(Protocol):
    def execute(self, request: AgentRunRequest) -> AgentRunResult:
        ...


class ExecutorScriptExhausted(Exception):
    """A scripted executor ran out of scripted actions (provider failure)."""


class WorkspaceAuthorityDenied(Exception):
    """The intended action lacks workspace authority; nothing was written."""

    def __init__(self, violations) -> None:
        super().__init__("workspace authority denied before any effect")
        self.violations = tuple(violations)


class CostAuthorizationExceeded(Exception):
    """The scripted action costs more than the request authorizes."""

    def __init__(self, cost_usd: float, authorized_usd: float) -> None:
        super().__init__("scripted cost exceeds the remaining authorization")
        self.cost_usd = cost_usd
        self.authorized_usd = authorized_usd
