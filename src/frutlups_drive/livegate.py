"""Live-gate declaration assessment and bounded Markdown loading.

Expresses — without any I/O, environment read, file discovery, or execution
authority — the shape of the owner decision: exact architect, coder, and
reviewer seat identities, credential environment-variable *names* only, cost
ceilings, per-call timeout, rollback/kill-switch statements, and explicit
stop conditions.

The declaration assessment remains a pure in-memory validation boundary:

- :func:`assess_live_gate` is total over ordinary built-in loadable input —
  mappings, non-mappings, containers, scalars, and non-string keys or values
  all return a frozen :class:`LiveGateAssessment` with stable bounded issue
  codes rather than raising, and no secret-shaped value is ever echoed
  (hostile user-defined ``Mapping``/``str`` subclasses and raising protocol
  objects remain outside the supported domain);
- the field vocabulary is closed; a non-empty unknown secret-shaped key
  (for example ``api_key``) refuses with its key name only;
- :func:`load_live_gate` reads only its explicit bounded path and requires
  exactly one fenced TOML block;
- nothing here reads ``os.environ`` or authorizes execution. A ``ready``
  assessment is a validation fact, not permission by itself.
"""

from __future__ import annotations

import math
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from frutlups_drive.planstate import _is_valid_artifact_reference
from frutlups_drive.policy import _ADAPTER_VALUES, _secret_shaped
from frutlups_drive.dispatch.provider_cli import (
    ProviderBindingError,
    catalog_effort_schedule,
)

_LOCAL_ADAPTERS = frozenset({"manual", "mock"})
EXTERNAL_ADAPTERS = tuple(
    adapter for adapter in _ADAPTER_VALUES if adapter not in _LOCAL_ADAPTERS
)

REQUIRED_STOP_CATEGORIES = ("cost", "time", "human")
ALLOWED_STOP_CATEGORIES = frozenset(
    {"cost", "time", "human", "provider", "integrity"}
)

MAX_CALL_TIMEOUT_SECONDS = 86_400.0
MAX_LIVE_GATE_FILE_BYTES = 131_072

_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{8,}"),
    re.compile(r"(?i)bearer\s+\S{8,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*\S+"),
)

_STRING_FIELDS = (
    "approval_state",
    "approval_reference",
    "coder_adapter",
    "coder_model",
    "reviewer_adapter",
    "reviewer_model",
    "architect_adapter",
    "architect_model",
    "rollback_statement",
    "kill_switch_statement",
)
_OPTIONAL_STRING_FIELDS = (
    "coder_corrective_effort",
    "reviewer_corrective_effort",
    "architect_corrective_effort",
)
_NUMERIC_FIELDS = (
    "max_total_cost_usd",
    "max_call_cost_usd",
    "call_timeout_seconds",
)
_FIELD_VOCABULARY = (
    "approval_state",
    "approval_reference",
    "coder_adapter",
    "coder_model",
    "reviewer_adapter",
    "reviewer_model",
    "architect_adapter",
    "architect_model",
    "credential_env_names",
    "max_total_cost_usd",
    "max_call_cost_usd",
    "call_timeout_seconds",
    "rollback_statement",
    "kill_switch_statement",
    "stop_conditions",
    *_OPTIONAL_STRING_FIELDS,
)
_REQUIRED_FIELDS = tuple(
    field for field in _FIELD_VOCABULARY if field not in _OPTIONAL_STRING_FIELDS
)


@dataclass(frozen=True)
class LiveGateIssue:
    code: str
    field: str


@dataclass(frozen=True)
class LiveGateDeclaration:
    approval_state: str
    approval_reference: str
    coder_adapter: str
    coder_model: str
    reviewer_adapter: str
    reviewer_model: str
    architect_adapter: str
    architect_model: str
    credential_env_names: tuple[str, ...]
    max_total_cost_usd: float
    max_call_cost_usd: float
    call_timeout_seconds: float
    rollback_statement: str
    kill_switch_statement: str
    stop_conditions: tuple[tuple[str, str], ...]
    coder_corrective_effort: str | None = None
    reviewer_corrective_effort: str | None = None
    architect_corrective_effort: str | None = None


@dataclass(frozen=True)
class LiveGateAssessment:
    """Frozen validation outcome. ``ready`` is a fact about the declaration,
    never execution permission: no code path consumes an assessment to run,
    spend, or contact anything."""

    ready: bool
    issues: tuple[LiveGateIssue, ...]
    declaration: LiveGateDeclaration | None


class LiveGateLoadError(Exception):
    """Bounded gate-source refusal; file content is never echoed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LoadedLiveGate:
    assessment: LiveGateAssessment
    source_sha256: str


_TOML_FENCE = re.compile(
    r"(?ms)^[ \t]*```toml[ \t]*\r?\n(.*?)^[ \t]*```[ \t]*(?:\r?\n|$)"
)


def load_live_gate(path: Path | str) -> LoadedLiveGate:
    """Load one bounded Markdown gate and assess its sole TOML fence.

    Stable load codes are ``gate_file_missing``, ``gate_file_oversized``,
    ``gate_file_not_utf8``, ``gate_fence_count_invalid``, and
    ``gate_toml_malformed``. Declaration problems remain structured on the
    returned :class:`LiveGateAssessment`.
    """

    gate_path = Path(path)
    try:
        with gate_path.open("rb") as stream:
            raw = stream.read(MAX_LIVE_GATE_FILE_BYTES + 1)
    except OSError:
        raise LiveGateLoadError(
            "gate_file_missing", "the declared live-gate file could not be read"
        ) from None
    if len(raw) > MAX_LIVE_GATE_FILE_BYTES:
        raise LiveGateLoadError(
            "gate_file_oversized", "the live-gate file exceeds its size bound"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise LiveGateLoadError(
            "gate_file_not_utf8", "the live-gate file is not valid UTF-8"
        ) from None
    fences = _TOML_FENCE.findall(text)
    if len(fences) != 1:
        raise LiveGateLoadError(
            "gate_fence_count_invalid",
            "the live-gate file must contain exactly one fenced TOML block",
        )
    try:
        document = tomllib.loads(fences[0])
    except tomllib.TOMLDecodeError:
        raise LiveGateLoadError(
            "gate_toml_malformed", "the live-gate declaration is not valid TOML"
        ) from None
    return LoadedLiveGate(assess_live_gate(document), sha256(raw).hexdigest())


def _secret_shaped_value(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS)


def _is_plain_number(value: object) -> bool:
    return type(value) is int or type(value) is float


def assess_live_gate(source: Mapping[str, object]) -> LiveGateAssessment:
    """Validate one live-gate declaration mapping into a frozen assessment.

    Stable issue codes: ``unknown_field``, ``secret_shaped_field``,
    ``field_missing``, ``field_type_invalid``, ``secret_shaped_value``,
    ``approval_missing``, ``approval_reference_invalid``,
    ``adapter_unknown``, ``adapter_not_live``, ``identity_missing``,
    ``identical_seats``, ``credential_names_missing``,
    ``credential_name_invalid``, ``numeric_range_invalid``,
    ``statement_missing``, ``stop_category_unknown``, and
    ``stop_category_missing``.
    """
    if not isinstance(source, Mapping):
        # Total admission (R1-F3): an ordinary loadable non-mapping shape is
        # the one bounded declaration issue, never a raised exception.
        return LiveGateAssessment(
            False,
            (LiveGateIssue("field_type_invalid", "declaration"),),
            None,
        )
    issues: list[LiveGateIssue] = []

    for key in source:
        if not isinstance(key, str):
            # A non-string top-level key is a bounded declaration issue
            # without coercion or key/value echo (R1-F3).
            issues.append(LiveGateIssue("field_type_invalid", "declaration"))
            continue
        if key in _FIELD_VOCABULARY:
            continue
        if _secret_shaped(key) and not _empty(source[key]):
            issues.append(LiveGateIssue("secret_shaped_field", key))
        else:
            issues.append(LiveGateIssue("unknown_field", key))

    values: dict[str, object] = {}
    for field in _REQUIRED_FIELDS:
        if field not in source:
            issues.append(LiveGateIssue("field_missing", field))
        else:
            values[field] = source[field]
    for field in _OPTIONAL_STRING_FIELDS:
        if field in source:
            values[field] = source[field]

    for field in (*_STRING_FIELDS, *_OPTIONAL_STRING_FIELDS):
        if field not in values:
            continue
        value = values[field]
        if not isinstance(value, str):
            issues.append(LiveGateIssue("field_type_invalid", field))
            values.pop(field)
        elif _secret_shaped_value(value):
            issues.append(LiveGateIssue("secret_shaped_value", field))
            values.pop(field)

    for field in _NUMERIC_FIELDS:
        if field not in values:
            continue
        value = values[field]
        if not _is_plain_number(value) or type(value) is bool:
            issues.append(LiveGateIssue("field_type_invalid", field))
            values.pop(field)
            continue
        try:
            number = float(value)
        except OverflowError:
            number = math.inf
        if not math.isfinite(number):
            issues.append(LiveGateIssue("numeric_range_invalid", field))
            values.pop(field)
        else:
            values[field] = number

    if "approval_state" in values and values["approval_state"] != "approved":
        issues.append(LiveGateIssue("approval_missing", "approval_state"))
    if "approval_reference" in values and not _is_valid_artifact_reference(
        str(values["approval_reference"])
    ):
        issues.append(
            LiveGateIssue("approval_reference_invalid", "approval_reference")
        )

    for prefix in ("coder", "reviewer", "architect"):
        adapter_field = f"{prefix}_adapter"
        model_field = f"{prefix}_model"
        adapter = values.get(adapter_field)
        if isinstance(adapter, str):
            if adapter not in _ADAPTER_VALUES:
                issues.append(LiveGateIssue("adapter_unknown", adapter_field))
            elif adapter in _LOCAL_ADAPTERS:
                issues.append(LiveGateIssue("adapter_not_live", adapter_field))
        model = values.get(model_field)
        if isinstance(model, str) and not model.strip():
            issues.append(LiveGateIssue("identity_missing", model_field))
        corrective_field = f"{prefix}_corrective_effort"
        corrective = values.get(corrective_field)
        if (
            isinstance(adapter, str)
            and isinstance(model, str)
            and isinstance(corrective, str)
            and adapter in EXTERNAL_ADAPTERS
            and model.strip()
        ):
            try:
                catalog_effort_schedule(adapter, model, corrective)
            except ProviderBindingError as refusal:
                issues.append(LiveGateIssue(refusal.code, corrective_field))

    coder_seat = (values.get("coder_adapter"), values.get("coder_model"))
    reviewer_seat = (
        values.get("reviewer_adapter"),
        values.get("reviewer_model"),
    )
    if (
        all(isinstance(part, str) and part for part in coder_seat)
        and coder_seat == reviewer_seat
    ):
        # Exact string equality only; never a family inference in either
        # direction, and never a family-independence claim when unequal.
        issues.append(LiveGateIssue("identical_seats", "reviewer_model"))

    architect_seat = (
        values.get("architect_adapter"),
        values.get("architect_model"),
    )
    if all(isinstance(part, str) and part for part in architect_seat) and (
        architect_seat == coder_seat or architect_seat == reviewer_seat
    ):
        issues.append(LiveGateIssue("identical_seats", "architect_model"))

    credentials: tuple[str, ...] = ()
    if "credential_env_names" in values:
        raw_names = values["credential_env_names"]
        if not isinstance(raw_names, (list, tuple)) or not all(
            isinstance(name, str) for name in raw_names
        ):
            issues.append(
                LiveGateIssue("field_type_invalid", "credential_env_names")
            )
        elif not raw_names:
            issues.append(
                LiveGateIssue("credential_names_missing", "credential_env_names")
            )
        else:
            for name in raw_names:
                if not _ENV_NAME.fullmatch(name) or _secret_shaped_value(name):
                    issues.append(
                        LiveGateIssue(
                            "credential_name_invalid", "credential_env_names"
                        )
                    )
                    break
            else:
                credentials = tuple(raw_names)

    for field in ("max_total_cost_usd", "max_call_cost_usd"):
        number = values.get(field)
        if isinstance(number, float) and number < 0:
            issues.append(LiveGateIssue("numeric_range_invalid", field))
    timeout = values.get("call_timeout_seconds")
    if isinstance(timeout, float) and not (
        0 < timeout <= MAX_CALL_TIMEOUT_SECONDS
    ):
        issues.append(
            LiveGateIssue("numeric_range_invalid", "call_timeout_seconds")
        )

    for field in ("rollback_statement", "kill_switch_statement"):
        statement = values.get(field)
        if isinstance(statement, str) and not statement.strip():
            issues.append(LiveGateIssue("statement_missing", field))

    stop_pairs: tuple[tuple[str, str], ...] = ()
    if "stop_conditions" in values:
        raw_stops = values["stop_conditions"]
        if not isinstance(raw_stops, Mapping):
            issues.append(LiveGateIssue("field_type_invalid", "stop_conditions"))
        else:
            # Iterate the original (key, value) pairs (R1-F3): a stringified
            # key is never used to re-index the mapping, and non-string keys
            # or values are bounded issues, never raw exceptions.
            collected = []
            for category, statement in raw_stops.items():
                if not isinstance(category, str):
                    issues.append(
                        LiveGateIssue("field_type_invalid", "stop_conditions")
                    )
                    continue
                if category not in ALLOWED_STOP_CATEGORIES:
                    issues.append(
                        LiveGateIssue("stop_category_unknown", "stop_conditions")
                    )
                    continue
                if not isinstance(statement, str):
                    issues.append(
                        LiveGateIssue("field_type_invalid", "stop_conditions")
                    )
                elif not statement.strip():
                    issues.append(
                        LiveGateIssue("statement_missing", "stop_conditions")
                    )
                elif _secret_shaped_value(statement):
                    issues.append(
                        LiveGateIssue("secret_shaped_value", "stop_conditions")
                    )
                else:
                    collected.append((category, statement))
            declared = {
                key for key in raw_stops if isinstance(key, str)
            }
            for required in REQUIRED_STOP_CATEGORIES:
                if required not in declared:
                    issues.append(
                        LiveGateIssue("stop_category_missing", "stop_conditions")
                    )
            stop_pairs = tuple(collected)

    frozen_issues = tuple(issues)
    if frozen_issues:
        return LiveGateAssessment(False, frozen_issues, None)
    declaration = LiveGateDeclaration(
        approval_state=str(values["approval_state"]),
        approval_reference=str(values["approval_reference"]),
        coder_adapter=str(values["coder_adapter"]),
        coder_model=str(values["coder_model"]),
        reviewer_adapter=str(values["reviewer_adapter"]),
        reviewer_model=str(values["reviewer_model"]),
        architect_adapter=str(values["architect_adapter"]),
        architect_model=str(values["architect_model"]),
        credential_env_names=credentials,
        max_total_cost_usd=float(values["max_total_cost_usd"]),
        max_call_cost_usd=float(values["max_call_cost_usd"]),
        call_timeout_seconds=float(values["call_timeout_seconds"]),
        rollback_statement=str(values["rollback_statement"]),
        kill_switch_statement=str(values["kill_switch_statement"]),
        stop_conditions=stop_pairs,
        coder_corrective_effort=values.get("coder_corrective_effort"),
        reviewer_corrective_effort=values.get("reviewer_corrective_effort"),
        architect_corrective_effort=values.get("architect_corrective_effort"),
    )
    return LiveGateAssessment(True, (), declaration)


def _empty(value: object) -> bool:
    return value in ("", (), [], {}, None)
