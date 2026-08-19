"""Execution policy: required project-local ``frutlups_drive.toml``.

Implements `02_analysis/runner_architecture_and_authority_contract.md` §7.

- parses only with :mod:`tomllib`; the parser is pure apart from reading the
  explicitly supplied path;
- an absent file, malformed TOML, unknown schema version, type/range/vocabulary
  violation, fixed-boundary violation, or non-empty secret-shaped setting is a
  :class:`PolicyRefusal` with a stable code (secret values are never echoed);
- unknown keys warn; every omitted field takes its named default, and both the
  defaulted field paths and the warnings are returned structurally so a later
  run-store layer can journal them.
"""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

SCHEMA_VERSION = "frutlups_drive_policy_v1"

_ADAPTER_VALUES = ("manual", "mock", "api_call", "claude_cli", "codex_cli", "kimi_cli")
_ACCESS_VALUES = ("read_only", "workspace_write")
_STOP_AT_VALUES = (
    "slice_complete",
    "milestone_complete",
    "roadmap_complete",
    "pass_complete",
)

_SECRET_SEGMENTS = frozenset(
    {
        "key",
        "keys",
        "apikey",
        "token",
        "tokens",
        "secret",
        "secrets",
        "password",
        "passwords",
        "passwd",
        "pwd",
        "credential",
        "credentials",
        "auth",
        "bearer",
    }
)


class PolicyRefusal(Exception):
    """Fail-closed policy refusal with a stable code and owned message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class TargetPolicy:
    stop_at: str
    max_slices: int
    max_passes: int


@dataclass(frozen=True)
class ArchitectPolicy:
    adapter: str
    model: str
    workspace_access: str


@dataclass(frozen=True)
class CoderPolicy:
    adapter: str
    model: str
    workspace_access: str
    resume_within_slice: bool
    resume_across_slices: bool


@dataclass(frozen=True)
class ReviewerPolicy:
    adapter: str
    model: str
    workspace_access: str
    fresh_session_per_invocation: bool


@dataclass(frozen=True)
class ShadowReviewerPolicy:
    enabled: bool
    adapter: str
    model: str
    workspace_access: str


@dataclass(frozen=True)
class AutonomyPolicy:
    max_strictness_level: int
    auto_continue_past_frontier_recorded: bool
    pass_boundary: str


@dataclass(frozen=True)
class LimitsPolicy:
    max_coder_attempts_per_slice: int
    max_report_repairs: int
    max_reconciliations_without_progress: int
    max_total_cost_usd: float
    max_wall_clock_minutes: int
    max_consecutive_provider_failures: int
    max_consecutive_no_progress: int
    provider_backoff_seconds: tuple[float, ...]
    watch_poll_seconds: float
    max_run_store_bytes: int
    max_retained_runs: int
    max_shadow_attempts_per_slice: int


@dataclass(frozen=True)
class GitPolicy:
    worktree_per_slice: bool
    commit: str
    pull_request: str


@dataclass(frozen=True)
class NetworkPolicy:
    default: str


@dataclass(frozen=True)
class MemoryPolicy:
    follow_project_state: bool
    on_read_failure: str
    downgrade_flag_allowed: bool
    upgrade_via_flag: bool


@dataclass(frozen=True)
class FrutlupsToolPolicy:
    """Committed frutlups-tool declaration (M003-S02 Phase B).

    The committed policy declares the provider mode, allowed contract
    identity, declared package identity, layout config path, sanctioned
    verb sets (fixed boundaries), transport bounds, and where the ignored
    machine-local launch binding lives. The binding itself — absolute
    executable/argv prefix and finite environment — is never committed and
    is loaded only when ``frutlups_cli`` is selected.
    """

    provider: str
    contract_id: str
    contract_version: str
    package_identity: str
    layout_config: str
    read_verbs: str
    write_verbs: str
    timeout_seconds: int
    max_stream_bytes: int
    binding_path: str


@dataclass(frozen=True)
class ExecutionPolicy:
    schema_version: str
    target: TargetPolicy
    architect: ArchitectPolicy
    coder: CoderPolicy
    reviewer: ReviewerPolicy
    shadow_reviewer: ShadowReviewerPolicy
    autonomy: AutonomyPolicy
    limits: LimitsPolicy
    git: GitPolicy
    network: NetworkPolicy
    memory: MemoryPolicy
    frutlups: FrutlupsToolPolicy


@dataclass(frozen=True)
class PolicyWarning:
    code: str
    key: str
    message: str


@dataclass(frozen=True)
class PolicyLoadResult:
    policy: ExecutionPolicy
    defaulted: tuple[str, ...]
    warnings: tuple[PolicyWarning, ...]


# Field spec: (kind, default, allowed). Kinds: "enum" (closed vocabulary),
# "fixed" (single-value fixed policy boundary), "fixed_false" (bool pinned to
# false), "str", "bool", "count" (int >= 0), "positive_count", "level"
# (int 1..4), "money" (finite number >= 0), bounded positive seconds, and a
# bounded non-decreasing seconds schedule.
_SPEC: dict[tuple[str, ...], dict[str, tuple[str, object, tuple[str, ...]]]] = {
    ("target",): {
        "stop_at": ("enum", "milestone_complete", _STOP_AT_VALUES),
        "max_slices": ("count", 25, ()),
        "max_passes": ("count", 3, ()),
    },
    ("roles", "architect"): {
        "adapter": ("enum", "manual", _ADAPTER_VALUES),
        "model": ("str", "", ()),
        "workspace_access": ("enum", "read_only", _ACCESS_VALUES),
    },
    ("roles", "coder"): {
        "adapter": ("enum", "manual", _ADAPTER_VALUES),
        "model": ("str", "", ()),
        "workspace_access": ("enum", "workspace_write", _ACCESS_VALUES),
        "resume_within_slice": ("bool", True, ()),
        "resume_across_slices": ("bool", False, ()),
    },
    ("roles", "reviewer"): {
        "adapter": ("enum", "manual", _ADAPTER_VALUES),
        "model": ("str", "", ()),
        "workspace_access": ("enum", "read_only", _ACCESS_VALUES),
        "fresh_session_per_invocation": ("bool", True, ()),
    },
    ("roles", "shadow_reviewer"): {
        "enabled": ("bool", False, ()),
        "adapter": ("enum", "mock", _ADAPTER_VALUES),
        "model": ("str", "", ()),
        "workspace_access": ("fixed", "read_only", ("read_only",)),
    },
    ("autonomy",): {
        "max_strictness_level": ("level", 3, ()),
        "auto_continue_past_frontier_recorded": ("bool", False, ()),
        "pass_boundary": (
            "enum",
            "human_gate",
            ("human_gate", "two_clean"),
        ),
    },
    ("limits",): {
        "max_coder_attempts_per_slice": ("count", 3, ()),
        "max_report_repairs": ("count", 2, ()),
        "max_reconciliations_without_progress": ("count", 2, ()),
        "max_total_cost_usd": ("money", 0.0, ()),
        "max_wall_clock_minutes": ("count", 360, ()),
        "max_consecutive_provider_failures": ("count", 2, ()),
        "max_consecutive_no_progress": ("count", 3, ()),
        "provider_backoff_seconds": (
            "seconds_schedule",
            (1.0, 2.0, 4.0),
            (),
        ),
        "watch_poll_seconds": ("positive_seconds", 0.05, ()),
        "max_run_store_bytes": ("positive_count", 64 * 1024 * 1024, ()),
        "max_retained_runs": ("positive_count", 25, ()),
        "max_shadow_attempts_per_slice": ("count", 1, ()),
    },
    ("git",): {
        "worktree_per_slice": ("bool", False, ()),
        "commit": ("fixed", "never", ("never",)),
        "pull_request": ("fixed", "never", ("never",)),
    },
    ("network",): {
        "default": ("fixed", "deny", ("deny",)),
    },
    ("memory",): {
        "follow_project_state": ("bool", True, ()),
        "on_read_failure": (
            "enum",
            "continue_without_memory",
            ("continue_without_memory",),
        ),
        "downgrade_flag_allowed": ("bool", True, ()),
        "upgrade_via_flag": ("fixed_false", False, ()),
    },
    # M003-S02 Phase B: committed frutlups-tool declaration. The verb sets
    # are fixed boundaries — exactly the sanctioned read/write surface —
    # and the launch binding path points at ignored local state only.
    ("frutlups",): {
        "provider": ("enum", "mock", ("mock", "frutlups_cli")),
        "contract_id": (
            "fixed",
            "frutlups.planning_frontier",
            ("frutlups.planning_frontier",),
        ),
        "contract_version": ("fixed", "1", ("1",)),
        # Empty means the policy does not make a separate package-identity
        # declaration.  Admission binds the durable package fact to the
        # proven tool identity and refuses any non-empty mismatch.
        "package_identity": ("str", "", ()),
        "layout_config": ("repo_path", "frutlups.layout.yaml", ()),
        "read_verbs": ("fixed", "status", ("status",)),
        "write_verbs": (
            "fixed",
            "declare-rework make-coding-prompt make-review-prompt record-verdict",
            (
                "declare-rework make-coding-prompt make-review-prompt record-verdict",
            ),
        ),
        "timeout_seconds": ("positive_count", 120, ()),
        "max_stream_bytes": ("positive_count", 1_048_576, ()),
        "binding_path": (
            "local_binding_path",
            "local_state/frutlups_binding.toml",
            (),
        ),
    },
}

_MISSING = object()


def load_execution_policy(path: Path | str) -> PolicyLoadResult:
    policy_path = Path(path)
    if not policy_path.is_file():
        raise PolicyRefusal(
            "policy_file_missing",
            "execution policy file is required and was not found; autonomy "
            "must be explicitly configured",
        )
    try:
        document = tomllib.loads(policy_path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise PolicyRefusal(
            "malformed_toml", "execution policy file is not valid TOML"
        ) from None

    _refuse_secret_shaped(document, ())

    schema_version = document.get("schema_version")
    if schema_version is None:
        raise PolicyRefusal(
            "schema_version_missing",
            "execution policy must declare schema_version",
        )
    if schema_version != SCHEMA_VERSION:
        raise PolicyRefusal(
            "schema_version_unknown",
            f"execution policy schema_version is not implemented; accepted: "
            f"{SCHEMA_VERSION}",
        )

    defaulted: list[str] = []
    values: dict[str, object] = {}
    for section_path, section_fields in _SPEC.items():
        table = _section_table(document, section_path)
        for key, (kind, default, allowed) in section_fields.items():
            dotted = ".".join((*section_path, key))
            if key not in table:
                values[dotted] = default
                defaulted.append(dotted)
            else:
                values[dotted] = _validate_field(dotted, kind, table[key], allowed)

    warnings: list[PolicyWarning] = []
    _collect_unknown_keys(document, (), _known_tree(), warnings)

    policy = ExecutionPolicy(
        schema_version=SCHEMA_VERSION,
        target=TargetPolicy(
            stop_at=values["target.stop_at"],
            max_slices=values["target.max_slices"],
            max_passes=values["target.max_passes"],
        ),
        architect=ArchitectPolicy(
            adapter=values["roles.architect.adapter"],
            model=values["roles.architect.model"],
            workspace_access=values["roles.architect.workspace_access"],
        ),
        coder=CoderPolicy(
            adapter=values["roles.coder.adapter"],
            model=values["roles.coder.model"],
            workspace_access=values["roles.coder.workspace_access"],
            resume_within_slice=values["roles.coder.resume_within_slice"],
            resume_across_slices=values["roles.coder.resume_across_slices"],
        ),
        reviewer=ReviewerPolicy(
            adapter=values["roles.reviewer.adapter"],
            model=values["roles.reviewer.model"],
            workspace_access=values["roles.reviewer.workspace_access"],
            fresh_session_per_invocation=values[
                "roles.reviewer.fresh_session_per_invocation"
            ],
        ),
        shadow_reviewer=ShadowReviewerPolicy(
            enabled=values["roles.shadow_reviewer.enabled"],
            adapter=values["roles.shadow_reviewer.adapter"],
            model=values["roles.shadow_reviewer.model"],
            workspace_access=values["roles.shadow_reviewer.workspace_access"],
        ),
        autonomy=AutonomyPolicy(
            max_strictness_level=values["autonomy.max_strictness_level"],
            auto_continue_past_frontier_recorded=values[
                "autonomy.auto_continue_past_frontier_recorded"
            ],
            pass_boundary=values["autonomy.pass_boundary"],
        ),
        limits=LimitsPolicy(
            max_coder_attempts_per_slice=values["limits.max_coder_attempts_per_slice"],
            max_report_repairs=values["limits.max_report_repairs"],
            max_reconciliations_without_progress=values[
                "limits.max_reconciliations_without_progress"
            ],
            max_total_cost_usd=values["limits.max_total_cost_usd"],
            max_wall_clock_minutes=values["limits.max_wall_clock_minutes"],
            max_consecutive_provider_failures=values[
                "limits.max_consecutive_provider_failures"
            ],
            max_consecutive_no_progress=values[
                "limits.max_consecutive_no_progress"
            ],
            provider_backoff_seconds=values[
                "limits.provider_backoff_seconds"
            ],
            watch_poll_seconds=values["limits.watch_poll_seconds"],
            max_run_store_bytes=values["limits.max_run_store_bytes"],
            max_retained_runs=values["limits.max_retained_runs"],
            max_shadow_attempts_per_slice=values[
                "limits.max_shadow_attempts_per_slice"
            ],
        ),
        git=GitPolicy(
            worktree_per_slice=values["git.worktree_per_slice"],
            commit=values["git.commit"],
            pull_request=values["git.pull_request"],
        ),
        network=NetworkPolicy(default=values["network.default"]),
        memory=MemoryPolicy(
            follow_project_state=values["memory.follow_project_state"],
            on_read_failure=values["memory.on_read_failure"],
            downgrade_flag_allowed=values["memory.downgrade_flag_allowed"],
            upgrade_via_flag=values["memory.upgrade_via_flag"],
        ),
        frutlups=FrutlupsToolPolicy(
            provider=values["frutlups.provider"],
            contract_id=values["frutlups.contract_id"],
            contract_version=values["frutlups.contract_version"],
            package_identity=values["frutlups.package_identity"],
            layout_config=values["frutlups.layout_config"],
            read_verbs=values["frutlups.read_verbs"],
            write_verbs=values["frutlups.write_verbs"],
            timeout_seconds=values["frutlups.timeout_seconds"],
            max_stream_bytes=values["frutlups.max_stream_bytes"],
            binding_path=values["frutlups.binding_path"],
        ),
    )
    return PolicyLoadResult(
        policy=policy, defaulted=tuple(defaulted), warnings=tuple(warnings)
    )


def _section_table(document: dict, section_path: tuple[str, ...]) -> dict:
    table: object = document
    for part in section_path:
        if not isinstance(table, dict):
            break
        table = table.get(part, {})
    if not isinstance(table, dict):
        dotted = ".".join(section_path)
        raise PolicyRefusal(
            "field_type_invalid", f"policy section '{dotted}' must be a table"
        )
    return table


def _validate_field(
    dotted: str, kind: str, value: object, allowed: tuple[str, ...]
) -> object:
    if kind == "bool":
        if type(value) is not bool:
            raise PolicyRefusal(
                "field_type_invalid", f"policy key '{dotted}' must be a boolean"
            )
        return value
    if kind == "fixed_false":
        if type(value) is not bool:
            raise PolicyRefusal(
                "field_type_invalid", f"policy key '{dotted}' must be a boolean"
            )
        if value:
            raise PolicyRefusal(
                "fixed_boundary_violation",
                f"policy key '{dotted}' must remain false in this version",
            )
        return False
    if kind == "str":
        if type(value) is not str:
            raise PolicyRefusal(
                "field_type_invalid", f"policy key '{dotted}' must be a string"
            )
        return value
    if kind in ("repo_path", "local_binding_path"):
        if type(value) is not str or not _canonical_policy_path(value):
            raise PolicyRefusal(
                "field_value_invalid",
                f"policy key '{dotted}' must be a canonical repo-relative "
                "POSIX path",
            )
        if kind == "local_binding_path" and not value.startswith("local_state/"):
            raise PolicyRefusal(
                "fixed_boundary_violation",
                f"policy key '{dotted}' must remain under local_state/",
            )
        return value
    if kind in ("enum", "fixed"):
        if type(value) is not str:
            raise PolicyRefusal(
                "field_type_invalid", f"policy key '{dotted}' must be a string"
            )
        if value not in allowed:
            code = "fixed_boundary_violation" if kind == "fixed" else "enum_value_unknown"
            raise PolicyRefusal(
                code,
                f"policy key '{dotted}' has an unsupported value; allowed: "
                f"{', '.join(allowed)}",
            )
        return value
    if kind == "count":
        if type(value) is not int:
            raise PolicyRefusal(
                "field_type_invalid", f"policy key '{dotted}' must be an integer"
            )
        if value < 0:
            raise PolicyRefusal(
                "numeric_range_invalid",
                f"policy key '{dotted}' must be non-negative",
            )
        return value
    if kind == "positive_count":
        if type(value) is not int:
            raise PolicyRefusal(
                "field_type_invalid", f"policy key '{dotted}' must be an integer"
            )
        if value <= 0:
            raise PolicyRefusal(
                "numeric_range_invalid",
                f"policy key '{dotted}' must be positive",
            )
        return value
    if kind == "positive_seconds":
        if type(value) not in (int, float):
            raise PolicyRefusal(
                "field_type_invalid", f"policy key '{dotted}' must be a number"
            )
        number = float(value)
        if not math.isfinite(number) or not (0.0 < number <= 60.0):
            raise PolicyRefusal(
                "numeric_range_invalid",
                f"policy key '{dotted}' must be greater than zero and at most 60",
            )
        return number
    if kind == "seconds_schedule":
        if type(value) not in (list, tuple) or not (1 <= len(value) <= 16):
            raise PolicyRefusal(
                "field_type_invalid",
                f"policy key '{dotted}' must be an array of 1 to 16 numbers",
            )
        schedule: list[float] = []
        for item in value:
            if type(item) not in (int, float):
                raise PolicyRefusal(
                    "field_type_invalid",
                    f"policy key '{dotted}' must contain only numbers",
                )
            seconds = float(item)
            if not math.isfinite(seconds) or not (0.0 <= seconds <= 300.0):
                raise PolicyRefusal(
                    "numeric_range_invalid",
                    f"policy key '{dotted}' entries must be between 0 and 300",
                )
            if schedule and seconds < schedule[-1]:
                raise PolicyRefusal(
                    "numeric_range_invalid",
                    f"policy key '{dotted}' must be non-decreasing",
                )
            schedule.append(seconds)
        return tuple(schedule)
    if kind == "level":
        if type(value) is not int:
            raise PolicyRefusal(
                "field_type_invalid", f"policy key '{dotted}' must be an integer"
            )
        if not 1 <= value <= 4:
            raise PolicyRefusal(
                "numeric_range_invalid",
                f"policy key '{dotted}' must be between 1 and 4",
            )
        return value
    if kind == "money":
        if type(value) not in (int, float):
            raise PolicyRefusal(
                "field_type_invalid", f"policy key '{dotted}' must be a number"
            )
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise PolicyRefusal(
                "numeric_range_invalid",
                f"policy key '{dotted}' must be a non-negative finite number",
            )
        return number
    raise AssertionError(f"unhandled spec kind: {kind}")


def _canonical_policy_path(value: str) -> bool:
    if not value or len(value) > 512 or chr(92) in value or "//" in value:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return False
    path = PurePosixPath(value)
    return (
        path.as_posix() == value
        and bool(path.parts)
        and all(part not in ("", ".", "..") for part in path.parts)
    )


# Word extraction for secret-shaped key detection (correction F2): after
# splitting on separators, each part is broken at lower-to-upper camelCase
# boundaries and acronym-to-word boundaries ("APIKey" -> "API", "Key"), then
# lowercased and matched exactly against the bounded credential vocabulary.
_KEY_WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def _secret_shaped(key: str) -> bool:
    for part in re.split(r"[^A-Za-z0-9]+", key):
        for word in _KEY_WORD.findall(part):
            if word.lower() in _SECRET_SEGMENTS:
                return True
    return False


def _refuse_secret_shaped(value: object, prefix: tuple[str, ...]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = (*prefix, str(key))
            if _secret_shaped(str(key)) and not _empty_value(child):
                raise PolicyRefusal(
                    "secret_shaped_value",
                    f"policy key '{'.'.join(child_prefix)}' is secret-shaped "
                    "and non-empty; credentials never appear in policy files",
                )
            _refuse_secret_shaped(child, child_prefix)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _refuse_secret_shaped(item, (*prefix, f"[{index}]"))


def _empty_value(value: object) -> bool:
    return value == "" or value == {} or value == []


def _known_tree() -> dict:
    tree: dict = {"schema_version": None}
    for section_path, section_fields in _SPEC.items():
        node = tree
        for part in section_path:
            node = node.setdefault(part, {})
        for key in section_fields:
            node[key] = None
    return tree


def _collect_unknown_keys(
    table: dict,
    prefix: tuple[str, ...],
    known: dict,
    warnings: list[PolicyWarning],
) -> None:
    for key, value in table.items():
        dotted = ".".join((*prefix, str(key)))
        child = known.get(str(key), _MISSING)
        if child is _MISSING:
            warnings.append(
                PolicyWarning(
                    "unknown_key", dotted, f"unknown policy key: {dotted}"
                )
            )
        elif isinstance(child, dict) and isinstance(value, dict):
            _collect_unknown_keys(value, (*prefix, str(key)), child, warnings)
