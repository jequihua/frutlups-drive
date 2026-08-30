"""Typed consumer for the frozen frutlups 0.2 drive seam.

This module is deliberately parallel to the released 0.1.8 observation and
governed-writer paths.  It owns only three things:

* explicit, finite-environment subprocess calls to the three frozen verbs;
* strict admission of their four response schemas; and
* construction of byte-stable corrective-publication proposal input.

It does not dispatch an agent, alter planning state, or write a project file.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


DRIVE_PAYLOAD_SCHEMA = "frutlups.drive_payload.v1"
SLICE_PAYLOAD_SCHEMA = "frutlups.slice_prompt_payload.v1"
FRONTIER_SCHEMA = "frutlups.frontier.v2"
CORRECTIVE_PROPOSAL_SCHEMA = "frutlups.corrective_publication_proposal.v1"
CORRECTIVE_RECEIPT_SCHEMA = "frutlups.corrective_publication_receipt.v1"
SEAM_REFUSAL_SCHEMA = "frutlups.drive_seam_refusal.v1"

MAX_SEAM_STDOUT_BYTES = 2_097_152
SEAM_STDOUT_READ_BOUND = MAX_SEAM_STDOUT_BYTES + 1
MAX_SEAM_STDERR_BYTES = 65_536
SEAM_STDERR_READ_BOUND = MAX_SEAM_STDERR_BYTES + 1
MAX_GOVERNED_INPUT_BYTES = 1_048_576
MAX_GOVERNED_PATH_BYTES = 4_096
MAX_DETAIL_BYTES = 1_024
ATTEMPT_PLACEHOLDER = "{attempt}"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_SLICE_ID = re.compile(r"M[0-9]+-S[0-9]+")
_ATTEMPT = re.compile(r"[0-9]{3}")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")

ROUTE_STEPS = {
    "advance_to_next_slice": "advance_slice",
    "milestone_complete": "complete_milestone",
    "recode_same_slice": "recode_slice",
    "unblock_same_slice": "unblock_slice",
    "human_override_required": "human_gate",
    "invalid": "stop_invalid",
}
VERDICTS = frozenset({"pass", "needs_work", "blocked", "override"})
OBJECTIVE_STATUSES = frozenset(
    {"achieved", "not_achieved", "not_applicable", "indeterminate"}
)
RECEIPT_OUTCOMES = frozenset(
    {"validated", "published", "refused", "recovery_required"}
)
RECEIPT_MODES = frozenset({"dry_run", "publish"})
OBSERVATION_STATES = frozenset({"absent", "present", "unreadable", "unsafe"})
SEAM_VERBS = frozenset({"drive-payload", "drive-frontier", "corrective-publish"})
SEAM_REFUSAL_CODES = frozenset(
    {
        "unsupported_version",
        "malformed_json",
        "project_root_unavailable",
        "sidecar_absent",
        "sidecar_unreadable",
        "sidecar_oversized",
        "sidecar_invalid",
        "slice_invalid",
        "routing_status_invalid",
        "prompt_absent",
        "prompt_unreadable",
        "prompt_oversized",
        "review_report_absent",
        "review_report_unreadable",
        "review_report_oversized",
        "proposal_empty",
        "proposal_oversized",
        "proposal_invalid",
        "proposal_target_mismatch",
        "payload_oversized",
        "layout_unresolved",
        "not_corrective",
        "entry_not_ready",
        "entry_unhealthy",
        "role_impure",
        "rework_context_unresolved",
        "target_unbound",
        "slice_not_in_sidecar",
        "history_unresolved",
        "attempt_not_fresh",
        "prompt_collision",
        "sidecar_update_invalid",
        "publish_write_failed",
        "recovery_required",
    }
)

_WRAPPER_FIELDS = {"schema", "version", "payload", "adoption"}
_PAYLOAD_FIELDS = {
    "schema",
    "contract_version",
    "slice",
    "milestone",
    "title",
    "status",
    "authored_by",
    "dispatch_authority",
    "attempt",
    "live",
    "corrective",
    "writes",
    "execution_envelope",
    "entry",
}
_ADOPTION_FIELDS = {
    "slice",
    "attempt",
    "prompt_path",
    "prompt_sha256",
    "self_report_path",
    "evidence_paths",
    "prior_evidence",
}
_FRONTIER_FIELDS = {
    "schema",
    "version",
    "milestone",
    "slice",
    "step",
    "outcome",
    "route",
    "reason",
    "milestone_complete",
    "receipt",
    "receipt_sha256",
}
_CORRECTIVE_RECEIPT_FIELDS = {
    "schema",
    "version",
    "mode",
    "transaction_id",
    "proposal_sha256",
    "slice",
    "attempt",
    "outcome",
    "sidecar_entry",
    "rendered_prompt",
    "refusal_codes",
    "before",
    "after",
    "receipt_sha256",
}
_SEAM_REFUSAL_FIELDS = {"schema", "version", "verb", "code", "detail"}
_ENVELOPE_FIELDS = {
    "timing_probe",
    "agent_budget_seconds",
    "subprocess_budget_seconds",
    "expected_wall_seconds",
    "hard_wall_seconds",
    "frozen_override",
    "environment_bindings",
    "identities",
    "retained_bytes_max",
    "local_output_root",
    "cleanup",
    "negative_result_handling",
    "stopped_result_handling",
}
_PROPOSAL_FIELDS = {
    "schema",
    "version",
    "slice",
    "sidecar_path",
    "prompt_path",
    "entry_template",
}


class SeamFailure(Exception):
    """Base class for one bounded, owned seam refusal."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class SeamTransportFailure(SeamFailure):
    """The subprocess did not produce one admissible response transport."""


class SeamUsageFailure(SeamFailure):
    """The producer returned its documented argparse usage exit."""


class SeamAdmissionFailure(SeamFailure):
    """A canonical response violated its selected schema contract."""


class EnvelopeAdmissionFailure(SeamAdmissionFailure):
    """A payload execution envelope exceeded drive authority."""


class CorrectiveProposalFailure(SeamAdmissionFailure):
    """Corrective proposal material violates the frozen input contract."""


@dataclass(frozen=True)
class PriorEvidenceIdentity:
    path: str
    sha256: str


@dataclass(frozen=True)
class AdoptionIdentity:
    """The exact evidence-adoption vocabulary consumed by the drive."""

    slice: str
    attempt: str | None
    prompt_path: str
    prompt_sha256: str
    self_report_path: str
    evidence_paths: tuple[str, ...]
    prior_evidence: tuple[PriorEvidenceIdentity, ...]


@dataclass(frozen=True)
class ExecutionEnvelope:
    timing_probe: Mapping[str, object]
    agent_budget_seconds: float
    subprocess_budget_seconds: float
    expected_wall_seconds: float
    hard_wall_seconds: float
    frozen_override: str | Mapping[str, str]
    environment_bindings: tuple[tuple[str, str], ...]
    identities: tuple[object, ...]
    retained_bytes_max: int
    local_output_root: str
    cleanup: str
    negative_result_handling: str
    stopped_result_handling: str


@dataclass(frozen=True)
class SlicePromptPayload:
    schema: str
    contract_version: int
    slice: str
    milestone: str
    title: str
    status: str
    authored_by: str
    dispatch_authority: str | None
    attempt: str | None
    live: bool
    corrective: bool
    writes: tuple[Mapping[str, object], ...]
    execution_envelope: ExecutionEnvelope | None
    entry: Mapping[str, object]


@dataclass(frozen=True)
class DrivePayload:
    schema: str
    version: int
    payload: SlicePromptPayload
    adoption: AdoptionIdentity

    @property
    def adoption_identity(self) -> AdoptionIdentity:
        return self.adoption


@dataclass(frozen=True)
class ClosureReceipt:
    verdict: str
    objective_status: str
    route: str


@dataclass(frozen=True)
class Frontier:
    schema: str
    version: int
    milestone: str
    slice: str
    step: str
    outcome: str
    route: str
    reason: str
    milestone_complete: bool
    receipt: ClosureReceipt | None
    receipt_sha256: str | None


@dataclass(frozen=True)
class ArtifactIdentity:
    path: str
    sha256: str


@dataclass(frozen=True)
class Observation:
    state: str
    sha256: str | None = None
    identity: str | None = None


@dataclass(frozen=True)
class CorrectiveReceipt:
    schema: str
    version: int
    mode: str
    transaction_id: str
    proposal_sha256: str
    slice: str
    attempt: str
    outcome: str
    sidecar_entry: ArtifactIdentity
    rendered_prompt: ArtifactIdentity
    refusal_codes: tuple[str, ...]
    before: Mapping[str, Observation]
    after: Mapping[str, Observation]
    receipt_sha256: str


@dataclass(frozen=True)
class DriveSeamRefusal:
    schema: str
    version: int
    verb: str
    code: str
    detail: str


SeamResponse = DrivePayload | Frontier | CorrectiveReceipt | DriveSeamRefusal


@dataclass(frozen=True)
class EnvelopeAuthority:
    """Finite drive-side ceilings and non-secret binding values.

    ``from_drive_authorities`` consumes already-admitted M009 policy/gate
    objects without making this module part of either parser.  The explicit
    constructor is useful for portable qualification and keeps all checks pure.
    """

    agent_budget_ceiling_seconds: float
    subprocess_budget_ceiling_seconds: float | None
    wall_ceiling_seconds: float
    environment_bindings: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _positive_number(
            self.agent_budget_ceiling_seconds,
            "envelope_authority_invalid",
        )
        if self.subprocess_budget_ceiling_seconds is not None:
            _positive_number(
                self.subprocess_budget_ceiling_seconds,
                "envelope_authority_invalid",
            )
        _positive_number(self.wall_ceiling_seconds, "envelope_authority_invalid")
        if type(self.environment_bindings) is not tuple:
            raise ValueError("environment_bindings must be a tuple")

    @classmethod
    def from_drive_authorities(
        cls,
        policy: object,
        gate: object,
        *,
        role: str,
        slice_id: str,
    ) -> EnvelopeAuthority:
        """Project M009 policy/gate facts as one envelope-admission view."""

        try:
            policy_bindings = tuple(policy.runtime_environment_bindings)  # type: ignore[attr-defined]
            gate_bindings = tuple(gate.runtime_environment_bindings)  # type: ignore[attr-defined]
            policy_roles = tuple(policy.dispatch.role_call_ceiling_seconds)  # type: ignore[attr-defined]
            policy_slices = tuple(policy.dispatch.slice_call_ceiling_overrides)  # type: ignore[attr-defined]
            gate_roles = tuple(gate.role_call_ceiling_seconds)  # type: ignore[attr-defined]
            gate_slices = tuple(gate.slice_call_ceiling_overrides)  # type: ignore[attr-defined]
            declared, _ = policy.dispatch.call_ceiling(role, slice_id)  # type: ignore[attr-defined]
            global_call = gate.call_timeout_seconds  # type: ignore[attr-defined]
            subprocess_ceiling = policy.dispatch.scientific_subprocess_budget_seconds  # type: ignore[attr-defined]
            wall_ceiling = policy.limits.max_wall_clock_minutes * 60  # type: ignore[attr-defined]
        except (AttributeError, TypeError, ValueError):
            raise EnvelopeAdmissionFailure(
                "envelope_authority_invalid",
                "drive policy and gate facts are unavailable",
            ) from None
        if (
            policy_bindings != gate_bindings
            or policy_roles != gate_roles
            or policy_slices != gate_slices
        ):
            raise EnvelopeAdmissionFailure(
                "envelope_authority_mismatch",
                "drive policy and gate declarations do not match",
            )
        return cls(
            agent_budget_ceiling_seconds=float(
                declared if declared is not None else global_call
            ),
            subprocess_budget_ceiling_seconds=(
                float(subprocess_ceiling)
                if subprocess_ceiling is not None
                else None
            ),
            wall_ceiling_seconds=float(wall_ceiling),
            environment_bindings=policy_bindings,
        )


@dataclass(frozen=True)
class SeamProcessResult:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    stdout_overflow: bool = False
    stderr_overflow: bool = False
    spawn_failed: bool = False


def canonical_json_bytes(document: object, *, final_lf: bool = False) -> bytes:
    """Frozen compact/sorted JSON recipe used by all seam digests."""

    raw = json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return raw + (b"\n" if final_lf else b"")


def _strict_json(raw: bytes) -> object:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate object key")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(value)

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, TypeError):
        raise SeamTransportFailure(
            "seam_stdout_malformed",
            "the seam response is not strict UTF-8 JSON",
        ) from None


def _parse_canonical_stdout(stdout: bytes) -> dict[str, object]:
    if not stdout:
        raise SeamTransportFailure(
            "seam_stdout_empty", "the seam response stdout is empty"
        )
    if len(stdout) > MAX_SEAM_STDOUT_BYTES:
        raise SeamTransportFailure(
            "seam_stdout_oversized", "the seam response exceeds 2097152 bytes"
        )
    document = _strict_json(stdout)
    if type(document) is not dict:
        raise SeamTransportFailure(
            "seam_stdout_malformed", "the seam response is not a JSON object"
        )
    try:
        canonical = canonical_json_bytes(document, final_lf=True)
    except (TypeError, ValueError):
        raise SeamTransportFailure(
            "seam_stdout_malformed", "the seam response is not canonical JSON"
        ) from None
    if stdout != canonical:
        raise SeamTransportFailure(
            "seam_stdout_noncanonical",
            "the seam response is not one canonical JSON document",
        )
    return document


def _exact_object(
    value: object, fields: set[str], code: str
) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise SeamAdmissionFailure(code, "the response field set is invalid")
    return value


def _safe_text(value: object, *, allow_empty: bool = False, limit: int = 4096) -> str:
    if (
        type(value) is not str
        or (not allow_empty and not value)
        or len(value.encode("utf-8")) > limit
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise SeamAdmissionFailure(
            "response_field_type_invalid", "a bounded text field is invalid"
        )
    return value


def _digest(value: object) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise SeamAdmissionFailure(
            "response_digest_invalid", "a response digest is invalid"
        )
    return value


def _attempt(value: object, *, nullable: bool = True) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or not _ATTEMPT.fullmatch(value):
        raise SeamAdmissionFailure(
            "response_attempt_invalid", "a response attempt is invalid"
        )
    return value


def _slice(value: object) -> str:
    if type(value) is not str or not _SLICE_ID.fullmatch(value):
        raise SeamAdmissionFailure(
            "response_slice_invalid", "a response slice identity is invalid"
        )
    return value


def _repo_path(value: object) -> str:
    if type(value) is not str or not value:
        raise SeamAdmissionFailure(
            "response_path_invalid", "a governed path is invalid"
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        encoded = b"x" * (MAX_GOVERNED_PATH_BYTES + 1)
    path = PurePosixPath(value)
    if (
        len(encoded) > MAX_GOVERNED_PATH_BYTES
        or chr(92) in value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value)
        or path.as_posix() != value
        or not path.parts
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise SeamAdmissionFailure(
            "response_path_invalid", "a governed path is invalid"
        )
    return value


def _positive_number(value: object, code: str) -> float:
    if type(value) not in (int, float):
        raise SeamAdmissionFailure(code, "a positive numeric field is invalid")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise SeamAdmissionFailure(code, "a positive numeric field is invalid")
    return number


def _artifact_identity(value: object) -> ArtifactIdentity:
    item = _exact_object(value, {"path", "sha256"}, "response_identity_invalid")
    return ArtifactIdentity(_repo_path(item["path"]), _digest(item["sha256"]))


def _admit_envelope(
    value: object, authority: EnvelopeAuthority | None
) -> ExecutionEnvelope:
    if authority is None:
        raise EnvelopeAdmissionFailure(
            "envelope_authority_required",
            "a live execution envelope requires drive policy and gate authority",
        )
    item = _exact_object(value, _ENVELOPE_FIELDS, "envelope_shape_invalid")
    timing = _exact_object(
        item["timing_probe"], {"command", "expected_seconds"}, "envelope_shape_invalid"
    )
    command = _safe_text(timing["command"], limit=MAX_DETAIL_BYTES)
    timing_seconds = _positive_number(
        timing["expected_seconds"], "envelope_shape_invalid"
    )
    timing = {"command": command, "expected_seconds": timing_seconds}

    agent_budget = _positive_number(
        item["agent_budget_seconds"], "envelope_shape_invalid"
    )
    subprocess_budget = _positive_number(
        item["subprocess_budget_seconds"], "envelope_shape_invalid"
    )
    expected_wall = _positive_number(
        item["expected_wall_seconds"], "envelope_shape_invalid"
    )
    hard_wall = _positive_number(
        item["hard_wall_seconds"], "envelope_shape_invalid"
    )
    if expected_wall > hard_wall:
        raise EnvelopeAdmissionFailure(
            "envelope_wall_order_invalid",
            "the expected wall exceeds the hard wall",
        )
    if agent_budget > authority.agent_budget_ceiling_seconds:
        raise EnvelopeAdmissionFailure(
            "envelope_agent_budget_exceeds_policy",
            "the agent budget exceeds drive authority",
        )
    if (
        authority.subprocess_budget_ceiling_seconds is not None
        and subprocess_budget > authority.subprocess_budget_ceiling_seconds
    ):
        raise EnvelopeAdmissionFailure(
            "envelope_subprocess_budget_exceeds_policy",
            "the subprocess budget exceeds drive authority",
        )
    if expected_wall > authority.wall_ceiling_seconds or hard_wall > authority.wall_ceiling_seconds:
        raise EnvelopeAdmissionFailure(
            "envelope_wall_exceeds_policy",
            "the declared wall exceeds drive authority",
        )
    if (
        authority.subprocess_budget_ceiling_seconds is not None
        and timing_seconds > authority.subprocess_budget_ceiling_seconds
    ):
        raise EnvelopeAdmissionFailure(
            "envelope_timing_probe_exceeds_policy",
            "the timing probe exceeds drive subprocess authority",
        )

    frozen_override = item["frozen_override"]
    if frozen_override != "none":
        override = _exact_object(
            frozen_override, {"authority"}, "envelope_shape_invalid"
        )
        frozen_override = {"authority": _repo_path(override["authority"])}

    bindings_value = item["environment_bindings"]
    admitted_bindings: list[tuple[str, str]] = []
    if bindings_value != "none":
        if type(bindings_value) is not list:
            raise EnvelopeAdmissionFailure(
                "envelope_shape_invalid", "environment bindings are invalid"
            )
        policy_bindings = dict(authority.environment_bindings)
        seen: set[str] = set()
        for raw_binding in bindings_value:
            binding = _exact_object(
                raw_binding, {"name", "value_sha256"}, "envelope_shape_invalid"
            )
            name = binding["name"]
            if (
                type(name) is not str
                or not _ENV_NAME.fullmatch(name)
                or name in seen
            ):
                raise EnvelopeAdmissionFailure(
                    "envelope_binding_name_invalid",
                    "an envelope binding name is invalid",
                )
            declared_digest = _digest(binding["value_sha256"])
            if name not in policy_bindings:
                raise EnvelopeAdmissionFailure(
                    "envelope_binding_undeclared",
                    "an envelope binding is absent from drive policy",
                )
            actual_digest = hashlib.sha256(
                policy_bindings[name].encode("utf-8")
            ).hexdigest()
            if declared_digest != actual_digest:
                raise EnvelopeAdmissionFailure(
                    "envelope_binding_hash_mismatch",
                    "an envelope binding digest does not match drive policy",
                )
            seen.add(name)
            admitted_bindings.append((name, declared_digest))

    identities = item["identities"]
    if type(identities) is not list:
        raise EnvelopeAdmissionFailure(
            "envelope_shape_invalid", "envelope identities must be an array"
        )
    retained = item["retained_bytes_max"]
    if type(retained) is not int or retained < 0:
        raise EnvelopeAdmissionFailure(
            "envelope_shape_invalid", "retained_bytes_max is invalid"
        )
    local_output_root = _repo_path(item["local_output_root"])
    cleanup = _safe_text(item["cleanup"], limit=MAX_DETAIL_BYTES)
    negative = _safe_text(
        item["negative_result_handling"], limit=MAX_DETAIL_BYTES
    )
    stopped = _safe_text(
        item["stopped_result_handling"], limit=MAX_DETAIL_BYTES
    )
    return ExecutionEnvelope(
        timing_probe=timing,
        agent_budget_seconds=agent_budget,
        subprocess_budget_seconds=subprocess_budget,
        expected_wall_seconds=expected_wall,
        hard_wall_seconds=hard_wall,
        frozen_override=frozen_override,
        environment_bindings=tuple(admitted_bindings),
        identities=tuple(identities),
        retained_bytes_max=retained,
        local_output_root=local_output_root,
        cleanup=cleanup,
        negative_result_handling=negative,
        stopped_result_handling=stopped,
    )


def _admit_payload(
    document: dict[str, object], authority: EnvelopeAuthority | None
) -> DrivePayload:
    wrapper = _exact_object(document, _WRAPPER_FIELDS, "payload_wrapper_invalid")
    payload = _exact_object(
        wrapper["payload"], _PAYLOAD_FIELDS, "payload_field_set_invalid"
    )
    adoption = _exact_object(
        wrapper["adoption"], _ADOPTION_FIELDS, "adoption_field_set_invalid"
    )
    if payload["schema"] != SLICE_PAYLOAD_SCHEMA or payload["contract_version"] != 1:
        raise SeamAdmissionFailure(
            "payload_contract_unknown",
            "the embedded slice payload contract is not implemented",
        )
    slice_id = _slice(payload["slice"])
    attempt = _attempt(payload["attempt"])
    for field in ("milestone", "title", "status", "authored_by"):
        _safe_text(payload[field])
    dispatch_authority = payload["dispatch_authority"]
    if dispatch_authority is not None:
        _repo_path(dispatch_authority)
    if type(payload["live"]) is not bool or type(payload["corrective"]) is not bool:
        raise SeamAdmissionFailure(
            "payload_field_type_invalid", "payload boolean fields are invalid"
        )
    if type(payload["writes"]) is not list or any(
        type(write) is not dict for write in payload["writes"]
    ):
        raise SeamAdmissionFailure(
            "payload_field_type_invalid", "payload writes are invalid"
        )
    entry = payload["entry"]
    if type(entry) is not dict:
        raise SeamAdmissionFailure(
            "payload_field_type_invalid", "payload entry is invalid"
        )
    mirrored_entry_fields = (
        "slice",
        "milestone",
        "title",
        "status",
        "authored_by",
        "dispatch_authority",
        "live",
        "corrective",
        "writes",
    )
    if any(entry.get(field) != payload[field] for field in mirrored_entry_fields):
        raise SeamAdmissionFailure(
            "payload_entry_incoherent",
            "the lossless entry differs from the resolved payload",
        )
    entry_attempt = entry.get("attempt")
    if (attempt is None and "attempt" in entry) or (
        attempt is not None and entry_attempt != attempt
    ):
        raise SeamAdmissionFailure(
            "payload_attempt_mismatch", "payload entry attempt is incoherent"
        )
    envelope_value = payload["execution_envelope"]
    if envelope_value is None:
        envelope = None
    else:
        if payload["live"] is not True:
            raise SeamAdmissionFailure(
                "payload_envelope_incoherent",
                "a non-live payload cannot carry an execution envelope",
            )
        if entry.get("execution_envelope") != envelope_value:
            raise SeamAdmissionFailure(
                "payload_envelope_incoherent",
                "the lossless entry envelope does not match the payload envelope",
            )
        envelope = _admit_envelope(envelope_value, authority)
    if payload["live"] is True and envelope is None:
        raise SeamAdmissionFailure(
            "payload_envelope_incoherent", "a live payload has no execution envelope"
        )

    adoption_slice = _slice(adoption["slice"])
    adoption_attempt = _attempt(adoption["attempt"])
    if adoption_slice != slice_id or adoption_attempt != attempt:
        raise SeamAdmissionFailure(
            "adoption_identity_mismatch",
            "adoption slice or attempt differs from the payload",
        )
    evidence_value = adoption["evidence_paths"]
    prior_value = adoption["prior_evidence"]
    if type(evidence_value) is not list or type(prior_value) is not list:
        raise SeamAdmissionFailure(
            "adoption_field_type_invalid", "ordered adoption evidence is invalid"
        )
    evidence_paths = tuple(_repo_path(path) for path in evidence_value)
    prior: list[PriorEvidenceIdentity] = []
    for raw_identity in prior_value:
        identity = _exact_object(
            raw_identity, {"path", "sha256"}, "adoption_field_type_invalid"
        )
        prior.append(
            PriorEvidenceIdentity(
                path=_repo_path(identity["path"]),
                sha256=_digest(identity["sha256"]),
            )
        )
    typed_payload = SlicePromptPayload(
        schema=SLICE_PAYLOAD_SCHEMA,
        contract_version=1,
        slice=slice_id,
        milestone=_safe_text(payload["milestone"]),
        title=_safe_text(payload["title"]),
        status=_safe_text(payload["status"]),
        authored_by=_safe_text(payload["authored_by"]),
        dispatch_authority=dispatch_authority,
        attempt=attempt,
        live=payload["live"],
        corrective=payload["corrective"],
        writes=tuple(payload["writes"]),
        execution_envelope=envelope,
        entry=dict(entry),
    )
    typed_adoption = AdoptionIdentity(
        slice=adoption_slice,
        attempt=adoption_attempt,
        prompt_path=_repo_path(adoption["prompt_path"]),
        prompt_sha256=_digest(adoption["prompt_sha256"]),
        self_report_path=_repo_path(adoption["self_report_path"]),
        evidence_paths=evidence_paths,
        prior_evidence=tuple(prior),
    )
    return DrivePayload(DRIVE_PAYLOAD_SCHEMA, 1, typed_payload, typed_adoption)


def _admit_frontier(document: dict[str, object]) -> Frontier:
    item = _exact_object(document, _FRONTIER_FIELDS, "frontier_field_set_invalid")
    route = item["route"]
    if type(route) is not str or route not in ROUTE_STEPS:
        raise SeamAdmissionFailure(
            "frontier_route_unknown", "the frontier route is not implemented"
        )
    if item["outcome"] != route:
        raise SeamAdmissionFailure(
            "frontier_outcome_mismatch", "frontier outcome does not equal route"
        )
    if item["step"] != ROUTE_STEPS[route]:
        raise SeamAdmissionFailure(
            "frontier_step_mismatch", "frontier step does not match its route"
        )
    if type(item["milestone_complete"]) is not bool:
        raise SeamAdmissionFailure(
            "frontier_field_type_invalid", "milestone_complete is not boolean"
        )
    reason = _safe_text(item["reason"], allow_empty=True, limit=MAX_DETAIL_BYTES)
    receipt_value = item["receipt"]
    receipt_digest = item["receipt_sha256"]
    if route == "invalid":
        if receipt_value is not None or receipt_digest is not None or item["milestone_complete"]:
            raise SeamAdmissionFailure(
                "frontier_receipt_incoherent", "an invalid frontier carries receipt facts"
            )
        receipt = None
        digest = None
    else:
        raw_receipt = _exact_object(
            receipt_value,
            {"verdict", "objective_status", "route"},
            "frontier_receipt_invalid",
        )
        verdict = raw_receipt["verdict"]
        objective = raw_receipt["objective_status"]
        receipt_route = raw_receipt["route"]
        if type(verdict) is not str or verdict not in VERDICTS:
            raise SeamAdmissionFailure(
                "frontier_verdict_unknown", "the closure verdict is not implemented"
            )
        if type(objective) is not str or objective not in OBJECTIVE_STATUSES:
            raise SeamAdmissionFailure(
                "frontier_objective_status_unknown",
                "the closure objective status is not implemented",
            )
        if receipt_route != route:
            raise SeamAdmissionFailure(
                "frontier_receipt_route_mismatch",
                "the closure receipt route differs from the frontier",
            )
        route_compatible = (
            (verdict == "needs_work" and route == "recode_same_slice")
            or (verdict == "blocked" and route == "unblock_same_slice")
            or (
                verdict in {"pass", "override"}
                and (
                    (
                        objective == "achieved"
                        and route
                        in {"advance_to_next_slice", "milestone_complete"}
                    )
                    or (
                        objective in {"not_achieved", "indeterminate"}
                        and route == "human_override_required"
                    )
                    or (
                        objective == "not_applicable"
                        and route
                        in {
                            "advance_to_next_slice",
                            "milestone_complete",
                            "human_override_required",
                        }
                    )
                )
            )
        )
        if not route_compatible:
            raise SeamAdmissionFailure(
                "frontier_receipt_route_incoherent",
                "the closure receipt dimensions do not support the route",
            )
        digest = _digest(receipt_digest)
        if digest != hashlib.sha256(canonical_json_bytes(raw_receipt)).hexdigest():
            raise SeamAdmissionFailure(
                "frontier_receipt_digest_mismatch",
                "the closure receipt digest does not match its canonical bytes",
            )
        should_complete = (
            route == "milestone_complete"
            and verdict in {"pass", "override"}
            and objective in {"achieved", "not_applicable"}
        )
        if item["milestone_complete"] is not should_complete:
            raise SeamAdmissionFailure(
                "frontier_completion_incoherent",
                "milestone completion violates the frozen two-case rule",
            )
        receipt = ClosureReceipt(verdict, objective, receipt_route)
    return Frontier(
        schema=FRONTIER_SCHEMA,
        version=2,
        milestone=_safe_text(item["milestone"]),
        slice=_slice(item["slice"]),
        step=item["step"],
        outcome=route,
        route=route,
        reason=reason,
        milestone_complete=item["milestone_complete"],
        receipt=receipt,
        receipt_sha256=digest,
    )


def _observation(value: object) -> Observation:
    if type(value) is not dict:
        raise SeamAdmissionFailure(
            "receipt_observation_invalid", "an observation is not an object"
        )
    state = value.get("state")
    if type(state) is not str or state not in OBSERVATION_STATES:
        raise SeamAdmissionFailure(
            "receipt_observation_state_unknown",
            "an observation state is not implemented",
        )
    expected = {
        "absent": {"state"},
        "present": {"state", "sha256"},
        "unreadable": {"state"},
        "unsafe": {"state", "identity"},
    }[state]
    if set(value) != expected:
        raise SeamAdmissionFailure(
            "receipt_observation_invalid", "an observation field set is invalid"
        )
    digest = _digest(value["sha256"]) if state == "present" else None
    identity = (
        _safe_text(value["identity"], limit=MAX_DETAIL_BYTES)
        if state == "unsafe"
        else None
    )
    return Observation(state, digest, identity)


def _observation_map(value: object) -> dict[str, Observation]:
    if type(value) is not dict or not value:
        raise SeamAdmissionFailure(
            "receipt_observation_map_invalid", "an observation map is invalid"
        )
    return {_repo_path(path): _observation(observation) for path, observation in value.items()}


def _admit_corrective_receipt(
    document: dict[str, object], *, exit_code: int, proposal_bytes: bytes | None
) -> CorrectiveReceipt:
    item = _exact_object(
        document, _CORRECTIVE_RECEIPT_FIELDS, "corrective_receipt_field_set_invalid"
    )
    if proposal_bytes is None:
        raise SeamAdmissionFailure(
            "corrective_proposal_bytes_required",
            "receipt admission requires the exact proposal bytes",
        )
    proposal = _validated_proposal_document(proposal_bytes)
    proposal_digest = hashlib.sha256(proposal_bytes).hexdigest()
    if item["proposal_sha256"] != proposal_digest:
        raise SeamAdmissionFailure(
            "corrective_proposal_digest_mismatch",
            "the receipt proposal digest does not match stdin bytes",
        )
    if item["transaction_id"] != f"cp.{proposal_digest}":
        raise SeamAdmissionFailure(
            "corrective_transaction_id_mismatch",
            "the corrective transaction id is invalid",
        )
    mode = item["mode"]
    outcome = item["outcome"]
    if type(mode) is not str or mode not in RECEIPT_MODES:
        raise SeamAdmissionFailure(
            "corrective_mode_unknown", "the corrective receipt mode is not implemented"
        )
    if type(outcome) is not str or outcome not in RECEIPT_OUTCOMES:
        raise SeamAdmissionFailure(
            "corrective_outcome_unknown",
            "the corrective receipt outcome is not implemented",
        )
    coherent = {
        "validated": exit_code == 0 and mode == "dry_run",
        "published": exit_code == 0 and mode == "publish",
        "refused": exit_code == 3,
        "recovery_required": exit_code == 4 and mode == "publish",
    }[outcome]
    if not coherent:
        raise SeamAdmissionFailure(
            "corrective_exit_incoherent",
            "the corrective outcome, mode, and exit class disagree",
        )
    refusal_codes = item["refusal_codes"]
    if type(refusal_codes) is not list or any(
        type(code) is not str or code not in SEAM_REFUSAL_CODES
        for code in refusal_codes
    ):
        raise SeamAdmissionFailure(
            "corrective_refusal_code_unknown",
            "a corrective refusal code is not implemented",
        )
    if outcome in {"validated", "published"} and refusal_codes:
        raise SeamAdmissionFailure(
            "corrective_refusal_codes_incoherent",
            "a successful corrective receipt carries refusal codes",
        )
    if outcome in {"refused", "recovery_required"} and not refusal_codes:
        raise SeamAdmissionFailure(
            "corrective_refusal_codes_incoherent",
            "a failed corrective receipt carries no refusal code",
        )
    before = _observation_map(item["before"])
    after = _observation_map(item["after"])
    if set(before) != set(after):
        raise SeamAdmissionFailure(
            "receipt_observation_map_incomplete",
            "corrective before/after maps cover different paths",
        )
    if outcome == "validated" and before != after:
        raise SeamAdmissionFailure(
            "corrective_dry_run_effect",
            "a validated dry run changed an observed path",
        )
    receipt_digest = _digest(item["receipt_sha256"])
    without_digest = dict(item)
    del without_digest["receipt_sha256"]
    if receipt_digest != hashlib.sha256(canonical_json_bytes(without_digest)).hexdigest():
        raise SeamAdmissionFailure(
            "corrective_receipt_digest_mismatch",
            "the receipt digest does not match its canonical bytes",
        )
    receipt_slice = _slice(item["slice"])
    receipt_attempt = _attempt(item["attempt"], nullable=False)
    sidecar_entry = _artifact_identity(item["sidecar_entry"])
    rendered_prompt = _artifact_identity(item["rendered_prompt"])
    expected_prompt = proposal["prompt_path"].replace(
        ATTEMPT_PLACEHOLDER, receipt_attempt
    )
    if (
        receipt_slice != proposal["slice"]
        or sidecar_entry.path != proposal["sidecar_path"]
        or rendered_prompt.path != expected_prompt
    ):
        raise SeamAdmissionFailure(
            "corrective_receipt_identity_mismatch",
            "the receipt identities do not materialize the exact proposal",
        )
    return CorrectiveReceipt(
        schema=CORRECTIVE_RECEIPT_SCHEMA,
        version=1,
        mode=mode,
        transaction_id=item["transaction_id"],
        proposal_sha256=proposal_digest,
        slice=receipt_slice,
        attempt=receipt_attempt,  # type: ignore[arg-type]
        outcome=outcome,
        sidecar_entry=sidecar_entry,
        rendered_prompt=rendered_prompt,
        refusal_codes=tuple(refusal_codes),
        before=before,
        after=after,
        receipt_sha256=receipt_digest,
    )


def _admit_refusal(document: dict[str, object]) -> DriveSeamRefusal:
    item = _exact_object(document, _SEAM_REFUSAL_FIELDS, "refusal_field_set_invalid")
    verb = item["verb"]
    code = item["code"]
    if type(verb) is not str or verb not in SEAM_VERBS:
        raise SeamAdmissionFailure(
            "refusal_verb_unknown", "the refusal verb is not implemented"
        )
    if type(code) is not str or code not in SEAM_REFUSAL_CODES:
        raise SeamAdmissionFailure(
            "refusal_code_unknown", "the refusal code is not implemented"
        )
    return DriveSeamRefusal(
        schema=SEAM_REFUSAL_SCHEMA,
        version=1,
        verb=verb,
        code=code,
        detail=_safe_text(item["detail"], allow_empty=True, limit=MAX_DETAIL_BYTES),
    )


def admit_seam_response(
    *,
    exit_code: int,
    stdout: bytes,
    stderr: bytes = b"",
    proposal_bytes: bytes | None = None,
    envelope_authority: EnvelopeAuthority | None = None,
) -> SeamResponse:
    """Admit one completed seam subprocess result, schema first."""

    if type(exit_code) is not int or exit_code not in {0, 2, 3, 4}:
        raise SeamTransportFailure(
            "seam_exit_unknown", "the seam process returned an unknown exit class"
        )
    if len(stderr) > MAX_SEAM_STDERR_BYTES:
        raise SeamTransportFailure(
            "seam_stderr_oversized", "the seam response stderr exceeds its bound"
        )
    if exit_code == 2:
        if stdout or not stderr.startswith(b"usage:"):
            raise SeamTransportFailure(
                "seam_usage_malformed",
                "the usage exit did not carry only argparse usage stderr",
            )
        raise SeamUsageFailure("seam_usage", "the seam process reported CLI usage")
    if stderr:
        raise SeamTransportFailure(
            "seam_stderr_unexpected",
            "the seam response mixed stderr with a machine document",
        )
    document = _parse_canonical_stdout(stdout)
    schema = document.get("schema")
    if type(schema) is not str or schema not in {
        DRIVE_PAYLOAD_SCHEMA,
        FRONTIER_SCHEMA,
        CORRECTIVE_RECEIPT_SCHEMA,
        SEAM_REFUSAL_SCHEMA,
    }:
        raise SeamAdmissionFailure(
            "response_schema_unknown", "the seam response schema is not implemented"
        )
    expected_version = 2 if schema == FRONTIER_SCHEMA else 1
    if type(document.get("version")) is not int or document["version"] != expected_version:
        raise SeamAdmissionFailure(
            "response_version_unknown", "the seam response version is not implemented"
        )
    if schema == DRIVE_PAYLOAD_SCHEMA:
        if exit_code != 0:
            raise SeamAdmissionFailure(
                "payload_exit_incoherent", "a payload response did not exit zero"
            )
        return _admit_payload(document, envelope_authority)
    if schema == FRONTIER_SCHEMA:
        if exit_code != 0:
            raise SeamAdmissionFailure(
                "frontier_exit_incoherent", "a frontier response did not exit zero"
            )
        return _admit_frontier(document)
    if schema == SEAM_REFUSAL_SCHEMA:
        if exit_code != 3:
            raise SeamAdmissionFailure(
                "refusal_exit_incoherent", "a seam refusal did not exit three"
            )
        return _admit_refusal(document)
    return _admit_corrective_receipt(
        document, exit_code=exit_code, proposal_bytes=proposal_bytes
    )


def admit_process_result(
    result: SeamProcessResult,
    *,
    proposal_bytes: bytes | None = None,
    envelope_authority: EnvelopeAuthority | None = None,
) -> SeamResponse:
    if result.spawn_failed:
        raise SeamTransportFailure(
            "seam_spawn_failed", "the seam process could not be started"
        )
    if result.timed_out:
        raise SeamTransportFailure("seam_timeout", "the seam process timed out")
    if result.stdout_overflow:
        raise SeamTransportFailure(
            "seam_stdout_oversized", "the seam response exceeds 2097152 bytes"
        )
    if result.stderr_overflow:
        raise SeamTransportFailure(
            "seam_stderr_oversized", "the seam response stderr exceeds its bound"
        )
    if result.exit_code is None:
        raise SeamTransportFailure(
            "seam_status_unavailable", "the seam process has no exit status"
        )
    return admit_seam_response(
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        proposal_bytes=proposal_bytes,
        envelope_authority=envelope_authority,
    )


def _run_bounded_process(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: float,
    stdin_bytes: bytes | None,
) -> SeamProcessResult:
    """Run with two bounded pipe readers; never inherit an ambient env."""

    if not argv or not Path(argv[0]).is_absolute() or not Path(argv[0]).is_file():
        return SeamProcessResult(None, b"", b"", spawn_failed=True)
    timeout = _positive_number(timeout_seconds, "seam_timeout_invalid")
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except (OSError, ValueError):
        return SeamProcessResult(None, b"", b"", spawn_failed=True)

    streams: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = {"stdout": False, "stderr": False}

    def terminate() -> None:
        try:
            process.kill()
        except OSError:
            pass

    def read_stream(name: str, bound: int) -> None:
        stream = process.stdout if name == "stdout" else process.stderr
        assert stream is not None
        try:
            while True:
                chunk = stream.read(65_536)
                if not chunk:
                    break
                remaining = bound - len(streams[name])
                if remaining > 0:
                    streams[name].extend(chunk[:remaining])
                if len(chunk) > remaining or len(streams[name]) >= bound:
                    overflow[name] = True
                    terminate()
                    break
        finally:
            stream.close()

    stdout_reader = threading.Thread(
        target=read_stream, args=("stdout", SEAM_STDOUT_READ_BOUND), daemon=True
    )
    stderr_reader = threading.Thread(
        target=read_stream, args=("stderr", SEAM_STDERR_READ_BOUND), daemon=True
    )
    stdout_reader.start()
    stderr_reader.start()
    try:
        assert process.stdin is not None
        if stdin_bytes is not None:
            process.stdin.write(stdin_bytes)
            process.stdin.flush()
        process.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate()
        process.wait()
    stdout_reader.join(timeout=10)
    stderr_reader.join(timeout=10)
    if stdout_reader.is_alive() or stderr_reader.is_alive():
        terminate()
        return SeamProcessResult(None, b"", b"", spawn_failed=True)
    return SeamProcessResult(
        exit_code=process.returncode,
        stdout=bytes(streams["stdout"]),
        stderr=bytes(streams["stderr"]),
        timed_out=timed_out,
        stdout_overflow=overflow["stdout"],
        stderr_overflow=overflow["stderr"],
    )


def _validated_proposal_document(proposal_bytes: bytes) -> dict[str, object]:
    if not proposal_bytes or len(proposal_bytes) > MAX_GOVERNED_INPUT_BYTES:
        raise CorrectiveProposalFailure(
            "corrective_proposal_oversized",
            "the corrective proposal is empty or oversized",
        )
    try:
        document = _strict_json(proposal_bytes)
    except SeamTransportFailure:
        raise CorrectiveProposalFailure(
            "corrective_proposal_invalid", "the corrective proposal is not strict JSON"
        ) from None
    item = _exact_object(document, _PROPOSAL_FIELDS, "corrective_proposal_invalid")
    if item["schema"] != CORRECTIVE_PROPOSAL_SCHEMA or item["version"] != 1:
        raise CorrectiveProposalFailure(
            "corrective_proposal_contract_unknown",
            "the corrective proposal contract is not implemented",
        )
    slice_id = _slice(item["slice"])
    sidecar_path = _repo_path(item["sidecar_path"])
    prompt_path = _repo_path(item["prompt_path"])
    if prompt_path.count(ATTEMPT_PLACEHOLDER) != 1:
        raise CorrectiveProposalFailure(
            "corrective_prompt_placeholder_invalid",
            "the corrective prompt path must contain one attempt placeholder",
        )
    entry_template = item["entry_template"]
    if (
        type(entry_template) is not dict
        or "attempt" in entry_template
        or entry_template.get("slice") != slice_id
    ):
        raise CorrectiveProposalFailure(
            "corrective_entry_template_invalid",
            "the corrective entry template is not attempt-free and slice-bound",
        )
    return {
        "schema": CORRECTIVE_PROPOSAL_SCHEMA,
        "version": 1,
        "slice": slice_id,
        "sidecar_path": sidecar_path,
        "prompt_path": prompt_path,
        "entry_template": dict(entry_template),
    }


def build_corrective_publication_proposal(
    *,
    slice_id: str,
    sidecar_path: str,
    prompt_path: str,
    entry_template: Mapping[str, object],
) -> bytes:
    """Build stable stdin bytes for dry-run and publish from one material set."""

    document = {
        "schema": CORRECTIVE_PROPOSAL_SCHEMA,
        "version": 1,
        "slice": slice_id,
        "sidecar_path": sidecar_path,
        "prompt_path": prompt_path,
        "entry_template": dict(entry_template),
    }
    try:
        raw = canonical_json_bytes(document, final_lf=True)
    except (TypeError, ValueError):
        raise CorrectiveProposalFailure(
            "corrective_proposal_invalid",
            "the corrective proposal material is not finite JSON",
        ) from None
    admitted = _validated_proposal_document(raw)
    if admitted != document:
        raise CorrectiveProposalFailure(
            "corrective_proposal_invalid",
            "the corrective proposal material changed during admission",
        )
    return raw


class FrutlupsSeamConsumer:
    """Explicit subprocess client for the three frozen seam verbs."""

    def __init__(
        self,
        *,
        python_executable: Path,
        project_root: Path,
        env: Mapping[str, str],
        timeout_seconds: float = 120.0,
        envelope_authority: EnvelopeAuthority | None = None,
    ) -> None:
        executable = Path(python_executable)
        root = Path(project_root)
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("python_executable must be an absolute ordinary file")
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("project_root must be an absolute existing directory")
        if type(env) is not dict or any(
            type(name) is not str
            or not name
            or type(value) is not str
            or "\x00" in name
            or "\x00" in value
            for name, value in env.items()
        ):
            raise ValueError("env must be one finite explicit string mapping")
        self._python = executable
        self._root = root
        self._env = dict(env)
        self._timeout = _positive_number(timeout_seconds, "seam_timeout_invalid")
        self._envelope_authority = envelope_authority

    def _invoke(
        self, arguments: Sequence[str], *, proposal_bytes: bytes | None = None
    ) -> SeamResponse:
        argv = (str(self._python), "-m", "frutlups", *arguments)
        result = _run_bounded_process(
            argv,
            cwd=self._root,
            env=self._env,
            timeout_seconds=self._timeout,
            stdin_bytes=proposal_bytes,
        )
        return admit_process_result(
            result,
            proposal_bytes=proposal_bytes,
            envelope_authority=self._envelope_authority,
        )

    def drive_payload(
        self, *, sidecar_path: str, slice_id: str, prompt_path: str
    ) -> DrivePayload | DriveSeamRefusal:
        _repo_path(sidecar_path)
        _slice(slice_id)
        _repo_path(prompt_path)
        response = self._invoke(
            (
                "drive-payload",
                str(self._root),
                "--sidecar",
                sidecar_path,
                "--slice",
                slice_id,
                "--prompt",
                prompt_path,
                "--version",
                "1",
            )
        )
        if not isinstance(response, (DrivePayload, DriveSeamRefusal)):
            raise SeamAdmissionFailure(
                "payload_response_schema_incoherent",
                "the payload verb returned another verb's schema",
            )
        return response

    def drive_frontier(
        self,
        *,
        sidecar_path: str,
        slice_id: str,
        review_report_path: str,
        explicit_routing_status: str | None = None,
    ) -> Frontier | DriveSeamRefusal:
        _repo_path(sidecar_path)
        _slice(slice_id)
        _repo_path(review_report_path)
        arguments = [
            "drive-frontier",
            str(self._root),
            "--sidecar",
            sidecar_path,
            "--slice",
            slice_id,
            "--review-report",
            review_report_path,
            "--version",
            "2",
        ]
        if explicit_routing_status is not None:
            if explicit_routing_status not in ROUTE_STEPS:
                raise SeamAdmissionFailure(
                    "routing_status_invalid",
                    "the explicit routing status is not implemented",
                )
            arguments += ["--explicit-routing-status", explicit_routing_status]
        response = self._invoke(tuple(arguments))
        if not isinstance(response, (Frontier, DriveSeamRefusal)):
            raise SeamAdmissionFailure(
                "frontier_response_schema_incoherent",
                "the frontier verb returned another verb's schema",
            )
        return response

    def corrective_publish(
        self, proposal_bytes: bytes, *, dry_run: bool
    ) -> CorrectiveReceipt | DriveSeamRefusal:
        proposal = _validated_proposal_document(proposal_bytes)
        arguments = [
            "corrective-publish",
            str(self._root),
            "--sidecar",
            proposal["sidecar_path"],
            "--prompt",
            proposal["prompt_path"],
            "--version",
            "1",
        ]
        if dry_run:
            arguments.append("--dry-run")
        response = self._invoke(tuple(arguments), proposal_bytes=proposal_bytes)
        if not isinstance(response, (CorrectiveReceipt, DriveSeamRefusal)):
            raise SeamAdmissionFailure(
                "corrective_response_schema_incoherent",
                "the corrective verb returned another verb's schema",
            )
        return response
