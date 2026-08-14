"""Frozen public contracts: closed enums, frozen dataclasses, exit codes.

Mirrors `02_analysis/runner_architecture_and_authority_contract.md` §6.
Every member, field, and value here is test-pinned; adding, renaming, or
removing one is a Level 4 contract change.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Literal


class PlanOutcome(StrEnum):
    READY = "ready"
    NEEDS_SPECIFICATION = "needs_specification"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    INVALID = "invalid"


class LoopStep(StrEnum):
    NO_FRONTIER = "no_frontier"
    MAKE_CODING_PROMPT = "make_coding_prompt"
    EXECUTE_CODING_PROMPT = "execute_coding_prompt"
    FIX_SELF_REPORT = "fix_self_report"
    MAKE_REVIEW_PROMPT = "make_review_prompt"
    EXECUTE_REVIEW_PROMPT = "execute_review_prompt"
    FIX_REVIEW_REPORT = "fix_review_report"
    RECORD_VERDICT = "record_verdict"
    FRONTIER_RECORDED = "frontier_recorded"


class Role(StrEnum):
    ARCHITECT = "architect"
    CODER = "coder"
    REVIEWER = "reviewer"


class StopReason(StrEnum):
    BLOCKED_VERDICT = "blocked_verdict"
    OVERRIDE_REQUIRED = "override_required"
    INVALID_STATE = "invalid_state"
    REPAIR_EXHAUSTED = "repair_exhausted"
    LADDER_ROUND3 = "ladder_round3"
    LADDER_ROUND4_UNAUTHORIZED = "ladder_round4_unauthorized"
    BUDGET_EXHAUSTED = "budget_exhausted"
    KILL_SWITCH = "kill_switch"
    MEMORY_GATE = "memory_gate"
    ENVIRONMENT_GATE = "environment_gate"
    NO_PROGRESS = "no_progress"
    PATH_VIOLATION = "path_violation"
    VERIFICATION_MISSING = "verification_missing"
    PROVIDER_FAILURE = "provider_failure"
    RUN_STORE_FULL = "run_store_full"
    OWNER_NOTE = "owner_note"
    CONTRACT_VERSION_REFUSED = "contract_version_refused"
    HUMAN_GATE = "human_gate"


class ExitCode(IntEnum):
    """Pinned CLI exit codes (architecture contract §6)."""

    OK = 0
    INTERNAL_ERROR = 1
    REFUSED = 2
    STOPPED_WITH_ESCALATION = 10


_WORKSPACE_ACCESS_VALUES = ("read_only", "workspace_write")
_RUN_STATUS_VALUES = (
    "completed",
    "blocked",
    "failed",
    "timeout",
    "budget_exhausted",
    "policy_violation",
)


@dataclass(frozen=True)
class AgentRunRequest:
    run_id: str
    attempt_id: str
    role: Role
    prompt_path: Path
    prompt_sha256: str
    workspace: Path
    base_revision: str | None
    adapter: str
    model: str
    effort: str
    workspace_access: Literal["read_only", "workspace_write"]
    expected_artifacts: tuple[Path, ...]
    max_seconds: int
    max_cost_usd: float | None

    def __post_init__(self) -> None:
        if self.workspace_access not in _WORKSPACE_ACCESS_VALUES:
            raise ValueError(
                f"unknown workspace_access value: {self.workspace_access!r}"
            )


@dataclass(frozen=True)
class AgentRunResult:
    status: Literal[
        "completed",
        "blocked",
        "failed",
        "timeout",
        "budget_exhausted",
        "policy_violation",
    ]
    event_log_path: Path
    changed_files: tuple[Path, ...]
    produced_artifacts: tuple[Path, ...]
    exit_reason: str
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None

    def __post_init__(self) -> None:
        if self.status not in _RUN_STATUS_VALUES:
            raise ValueError(f"unknown status value: {self.status!r}")
