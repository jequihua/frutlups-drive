"""Planning-state consumer: v1 parser, frozen representation, plan providers.

Consumer side of the proposed ``frutlups_planning_state`` version 1 contract
(`02_analysis/cross_repo_convergence_contracts.md` §2).

Fail-closed rules:

- an unknown ``(contract, version)`` pair is refused with
  :class:`PlanningStateRefusal` before any field is interpreted (asymmetric:
  newer versions are refused, never parsed best-effort);
- every other structural or vocabulary violation yields a state whose outcome
  is ``invalid``, carrying one stable diagnostic code;
- unknown object fields are tolerated at every level;
- a state whose payload already declares ``outcome: "invalid"`` keeps the
  producer's diagnostics verbatim.

The parser performs no I/O beyond the bytes it is given; providers own any
transport.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from frutlups_drive.contracts import LoopStep, PlanOutcome
from frutlups_drive.dispatch.subprocess_agent import (
    checked_timeout_seconds,
    observe_command,
)

MAX_PLANNING_STATE_BYTES = 1_048_576

# Legacy mock contract (M002 fixtures only): the provisional serialization
# Phase A proposed. It is consumed exclusively by :class:`MockPlanProvider`
# and the accepted M002 mock-script convention; the live provider consumes
# only the released contract below and has no old-schema fallback.
ACCEPTED_CONTRACT = "frutlups_planning_state"
ACCEPTED_VERSION = 1

# Released frutlups 0.1.0 contract (M003-S02 Phase B): the atomic
# ``planning_frontier`` + ``loop_resume`` members of
# ``frutlups status <project> --json``
# (`02_analysis/cross_repo_convergence_contracts.md` §2).
RELEASED_CONTRACT_ID = "frutlups.planning_frontier"
RELEASED_CONTRACT_VERSION = "1"
MEMORY_MODE_CONTRACT_ID = "frutlups.memory_mode"
MEMORY_MODE_CONTRACT_VERSION = "1"
MEMORY_MODE_KEYS = frozenset(
    {
        "contract_id",
        "contract_version",
        "valid",
        "mode",
        "memory_root",
        "diagnostics",
    }
)
MEMORY_MODES = frozenset({"none", "lightweight", "llloom"})
_BACKSLASH = chr(92)

_RELEASED_READY_STEPS = frozenset(
    {
        "make_coding_prompt",
        "execute_coding_prompt",
        "fix_self_report",
        "make_review_prompt",
        "execute_review_prompt",
        "fix_review_report",
        "record_verdict",
        "frontier_recorded",
    }
)
_MAX_PRODUCER_DIAGNOSTIC_CHARS = 240
_MAX_PRODUCER_DIAGNOSTICS = 64

_ACTOR_VALUES = frozenset(
    {"orchestrator", "architect", "coder", "reviewer", "human", "none"}
)
_SEVERITY_VALUES = frozenset({"info", "warning", "error"})
_VERDICT_VALUES = frozenset({"pass", "needs_work", "blocked", "override"})
_ARTIFACT_KEYS = (
    "coding_prompt",
    "self_report",
    "review_prompt",
    "review_report",
    "verdict_record",
)


class PlanningStateRefusal(Exception):
    """Unknown planning-state contract/version: refused, nothing attempted."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class PlanProviderUnavailable(Exception):
    """Provider transport refusal: no planning state was obtained.

    Raised before any agent dispatch, attempt creation, verification, or
    project mutation when a provider's transport fails. Codes are stable and
    messages are owned bounded text that never echoes stderr, raw output,
    command arguments, or machine-local paths.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class MockScriptExhausted(Exception):
    """The scripted planning-state sequence has no further entries."""


class _Invalid(Exception):
    """Internal funnel: payload violation -> synthesized invalid state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    message: str


@dataclass(frozen=True)
class Frontier:
    milestone_id: str
    slice_id: str
    slice_title: str
    round: int


@dataclass(frozen=True)
class ArtifactPaths:
    coding_prompt: str | None
    self_report: str | None
    review_prompt: str | None
    review_report: str | None
    verdict_record: str | None


@dataclass(frozen=True)
class Verdict:
    value: str
    next_move: str
    report: str


@dataclass(frozen=True)
class BlockedRef:
    citation: str
    owner: str


@dataclass(frozen=True)
class CompletionEvidence:
    path: str


@dataclass(frozen=True)
class MemoryMode:
    """Strict declaration fact from ``frutlups.memory_mode`` version 1."""

    mode: str
    memory_root: str | None

    @classmethod
    def none(cls) -> "MemoryMode":
        """Explicit mock-provider declaration used by deterministic lanes."""

        return cls(mode="none", memory_root=None)

    def manifest_facts(self) -> dict[str, str]:
        return {
            "memory_mode": self.mode,
            "memory_root": self.memory_root or "",
        }


@dataclass(frozen=True)
class PlanningState:
    outcome: PlanOutcome
    step: LoopStep | None
    actor: str | None
    gate_state: str | None
    frontier: Frontier | None
    artifacts: ArtifactPaths
    verdict: Verdict | None
    blocked: BlockedRef | None
    completion_evidence: CompletionEvidence | None
    diagnostics: tuple[Diagnostic, ...]
    next_command: str | None


class PlanProvider(Protocol):
    def read_planning_state(self) -> PlanningState:
        """Return the current planning state, parsed and validated."""
        ...


class MockPlanProvider:
    """Replays an explicit scripted sequence of raw planning-state payloads.

    Each call to :meth:`read_planning_state` parses the next scripted payload
    through the same boundary a real provider would use. An exhausted script
    raises :class:`MockScriptExhausted` loudly rather than repeating state.
    """

    def __init__(self, payloads: Sequence[bytes]) -> None:
        self._payloads = tuple(payloads)
        self._next = 0

    def read_planning_state(self) -> PlanningState:
        if self._next >= len(self._payloads):
            raise MockScriptExhausted(
                f"scripted sequence exhausted after {len(self._payloads)} states"
            )
        payload = self._payloads[self._next]
        self._next += 1
        return parse_planning_state(payload)


def parse_released_frontier(
    frontier: object,
    resume: object,
    slice_identities: Mapping[str, str],
) -> PlanningState:
    """Parse one atomic released ``planning_frontier`` + ``loop_resume`` pair.

    The two members are one observation, validated together against the
    exact outcome/step table of the convergence contract. Contract identity
    is checked before any resume interpretation: an unknown
    ``contract_id`` or a non-type-strict ``contract_version`` raises
    :class:`PlanningStateRefusal`. Unknown object fields are tolerated;
    unknown vocabulary and invalid required-field combinations synthesize an
    ``invalid`` state with one stable drive-owned diagnostic. All artifact,
    citation, and evidence paths use the bounded canonical
    repository-relative POSIX grammar. Producer ``message``/``next_command``
    text is never carried; producer diagnostics become bounded opaque
    drive-owned envelopes that are never routed on. ``slice_identities`` is
    the structured slice→milestone mapping extracted from the same wrapper
    snapshot; the frontier round is always ``1`` because the drive run store
    owns the mechanical ladder for real providers.
    """

    if not isinstance(frontier, dict) or not isinstance(resume, dict):
        return _invalid_state(
            "member_not_object",
            "planning_frontier and loop_resume must be JSON objects",
        )
    if frontier.get("contract_id") != RELEASED_CONTRACT_ID or not (
        type(frontier.get("contract_version")) is str
        and frontier.get("contract_version") == RELEASED_CONTRACT_VERSION
    ):
        raise PlanningStateRefusal(
            "contract_version_refused",
            "planning-frontier contract/version is not implemented by this "
            f"consumer; accepted: {RELEASED_CONTRACT_ID} version "
            f"{RELEASED_CONTRACT_VERSION}",
        )

    try:
        return _parse_released_members(frontier, resume, slice_identities)
    except _Invalid as err:
        return _invalid_state(err.code, err.message)


def _released_str(member: dict, name: str, field: str) -> str:
    value = member.get(field, "")
    if not isinstance(value, str):
        raise _Invalid(
            "field_type_invalid", f"field '{name}.{field}' must be a string"
        )
    return value


def _released_path(member: dict, name: str, field: str) -> str | None:
    value = _released_str(member, name, field)
    if value == "":
        return None
    if not _is_valid_artifact_reference(value):
        raise _Invalid(
            "artifact_path_invalid",
            f"field '{name}.{field}' must be a repo-relative POSIX file path",
        )
    return value


def _released_diagnostics(member: dict, name: str) -> tuple[Diagnostic, ...]:
    raw = member.get("diagnostics", [])
    if not isinstance(raw, list):
        raise _Invalid(
            "field_type_invalid", f"field '{name}.diagnostics' must be an array"
        )
    envelopes = []
    for item in raw[:_MAX_PRODUCER_DIAGNOSTICS]:
        if not isinstance(item, str):
            raise _Invalid(
                "field_type_invalid",
                f"field '{name}.diagnostics' entries must be strings",
            )
        envelopes.append(
            Diagnostic(
                severity="info",
                code="producer_diagnostic",
                message=item[:_MAX_PRODUCER_DIAGNOSTIC_CHARS],
            )
        )
    return tuple(envelopes)


def _parse_released_members(
    frontier: dict, resume: dict, slice_identities: Mapping[str, str]
) -> PlanningState:
    outcome_raw = _released_str(frontier, "planning_frontier", "outcome")
    try:
        outcome = PlanOutcome(outcome_raw)
    except ValueError:
        raise _Invalid(
            "unknown_outcome",
            "planning frontier names an outcome this consumer does not "
            "implement",
        ) from None

    step_raw = _released_str(resume, "loop_resume", "step")
    try:
        step = LoopStep(step_raw)
    except ValueError:
        raise _Invalid(
            "unknown_step",
            "loop resume names a step this consumer does not implement",
        ) from None

    # The exact outcome/step table (convergence contract §2.3): ready binds
    # the eight work steps; needs_specification and complete bind
    # no_frontier; blocked and invalid accept any producer-valid step
    # because drive stops on them without executing the step.
    if outcome is PlanOutcome.READY:
        if step_raw not in _RELEASED_READY_STEPS:
            raise _Invalid(
                "step_combination_invalid",
                "outcome 'ready' requires one of the eight work steps",
            )
    elif outcome in (PlanOutcome.NEEDS_SPECIFICATION, PlanOutcome.COMPLETE):
        if step is not LoopStep.NO_FRONTIER:
            raise _Invalid(
                "step_combination_invalid",
                f"outcome '{outcome.value}' requires the no_frontier step",
            )

    actor_raw = _released_str(frontier, "planning_frontier", "actor")
    actor = actor_raw if actor_raw in _ACTOR_VALUES else None

    blocked: BlockedRef | None = None
    if outcome is PlanOutcome.BLOCKED:
        citation = _released_str(frontier, "planning_frontier", "block_citation")
        owner = _released_str(frontier, "planning_frontier", "block_owner")
        if not citation.strip() or not owner.strip():
            raise _Invalid(
                "blocked_fields_missing",
                "outcome 'blocked' requires a block citation and owner",
            )
        if not _is_valid_artifact_reference(citation):
            raise _Invalid(
                "artifact_path_invalid",
                "field 'planning_frontier.block_citation' must be a "
                "repo-relative POSIX file path",
            )
        blocked = BlockedRef(citation=citation, owner=owner)

    completion: CompletionEvidence | None = None
    if outcome is PlanOutcome.COMPLETE:
        evidence_ref = _released_str(
            frontier, "planning_frontier", "completion_evidence"
        )
        if not evidence_ref.strip():
            raise _Invalid(
                "completion_evidence_missing",
                "outcome 'complete' requires a completion-evidence reference",
            )
        if not _is_valid_artifact_reference(evidence_ref):
            raise _Invalid(
                "artifact_path_invalid",
                "field 'planning_frontier.completion_evidence' must be a "
                "repo-relative POSIX file path",
            )
        completion = CompletionEvidence(path=evidence_ref)

    artifacts = ArtifactPaths(
        coding_prompt=_released_path(resume, "loop_resume", "coding_prompt_path"),
        self_report=_released_path(resume, "loop_resume", "self_report_path"),
        review_prompt=_released_path(resume, "loop_resume", "review_prompt_path"),
        review_report=_released_path(resume, "loop_resume", "review_report_path"),
        verdict_record=_released_path(
            resume, "loop_resume", "verdict_record_path"
        ),
    )

    slice_id = _released_str(resume, "loop_resume", "frontier_slice_id")
    slice_title = _released_str(resume, "loop_resume", "frontier_slice_title")
    frontier_ref: Frontier | None = None
    if slice_id:
        milestone = slice_identities.get(slice_id, "")
        if type(milestone) is not str or not milestone:
            raise _Invalid(
                "frontier_identity_unresolved",
                "a nonempty frontier slice must have one nonempty milestone "
                "owner in the same released wrapper snapshot",
            )
        frontier_ref = Frontier(
            milestone_id=milestone,
            slice_id=slice_id,
            slice_title=slice_title,
            # The released v1 exposes no round; the drive run store is the
            # sole mechanical ladder authority for real providers.
            round=1,
        )

    diagnostics = _released_diagnostics(
        frontier, "planning_frontier"
    ) + _released_diagnostics(resume, "loop_resume")

    return PlanningState(
        outcome=outcome,
        step=step,
        actor=actor,
        gate_state=None,
        frontier=frontier_ref,
        artifacts=artifacts,
        # The released v1 exposes no typed verdict; blocked and
        # override-derived blocked states use drive's generic blocked
        # handling and no verdict is synthesized.
        verdict=None,
        blocked=blocked,
        completion_evidence=completion,
        diagnostics=diagnostics,
        # Producer next_command/message text is never carried or executed.
        next_command=None,
    )


class FrutlupsPlanProvider:
    """Subprocess boundary for the released frutlups status wrapper.

    M003-S02 Phase B: the provider consumes exclusively the released atomic
    ``planning_frontier`` + ``loop_resume`` contract
    (`02_analysis/cross_repo_convergence_contracts.md` §2) emitted by
    ``frutlups status <project> --json``. The retired provisional
    ``planning_state`` member is not a fallback: a wrapper without the
    released members refuses before any effect.

    The provider owns transport only. It invokes one explicitly declared
    command (no default executable, no PATH discovery, no shell) through the
    accepted bounded process runner with an explicitly empty child
    environment, accepts exactly one strict-JSON top-level object, extracts
    the two released members plus the smallest structured slice→milestone
    identity mapping while tolerating unrelated wrapper fields, and hands
    them to :func:`parse_released_frontier` — the sole validator, including
    its asymmetric contract/version refusal and invalid-state diagnostics.
    The raw wrapper, machine-local paths, producer messages, and
    ``next_command`` are never retained, journaled, or executed.

    Every transport or wrapper failure raises :class:`PlanProviderUnavailable`
    before any agent dispatch. Capture files use monotonically discovered
    unique indexes under the bounded capture root and are retained. The
    supported identity claim is sequential: sequential reads and a fresh
    process discover either retained stdout/stderr member and do not clobber
    it. No concurrent-reader or hostile-filesystem guarantee is made.
    """

    MAX_STREAM_BYTES = MAX_PLANNING_STATE_BYTES

    def __init__(
        self,
        *,
        argv: tuple[str, ...],
        cwd: Path,
        capture_root: Path,
        timeout_seconds: float,
        runner,
        env: tuple[tuple[str, str], ...] = (),
    ) -> None:
        if not isinstance(argv, tuple) or not argv:
            raise ValueError("argv must be a non-empty tuple of strings")
        for part in argv:
            if not isinstance(part, str) or not part:
                raise ValueError("argv must be a non-empty tuple of strings")
        if not Path(argv[0]).is_absolute():
            raise ValueError(
                "argv[0] must be an absolute executable path; this provider "
                "never discovers an executable from PATH"
            )
        if not isinstance(env, tuple) or not all(
            isinstance(entry, tuple)
            and len(entry) == 2
            and isinstance(entry[0], str)
            and isinstance(entry[1], str)
            for entry in env
        ):
            raise ValueError("env must be a tuple of (name, value) pairs")
        # The one shared plain-number admission owned by the subprocess
        # module (R1-F3): identical bounded ValueError behavior for every
        # invalid built-in value, including arbitrarily large integers.
        self._timeout = checked_timeout_seconds(timeout_seconds)
        self._argv = argv
        self._cwd = Path(cwd)
        self._capture_root = Path(capture_root)
        self._runner = runner
        self._env = env
        self._expected_memory_mode: MemoryMode | None = None

    # Both retained capture members identify an observation (R1-F4). The
    # digit run is bounded before any integer conversion, so an
    # unreasonably long numeric filename of any length is skipped safely
    # rather than aborting index selection.
    _CAPTURE_INDEX = re.compile(r"status_(\d+)_(?:stdout|stderr)\.txt")
    _MAX_INDEX_DIGITS = 9

    def _next_capture_index(self) -> int:
        highest = 0
        if self._capture_root.is_dir():
            for entry in self._capture_root.iterdir():
                match = self._CAPTURE_INDEX.fullmatch(entry.name)
                if match and len(match.group(1)) <= self._MAX_INDEX_DIGITS:
                    highest = max(highest, int(match.group(1)))
        return highest + 1

    def bind_memory_mode(self, expected: MemoryMode) -> None:
        """Require every later status observation to repeat admission mode."""

        if not isinstance(expected, MemoryMode):
            raise TypeError("expected must be a MemoryMode")
        self._expected_memory_mode = expected

    def _read_wrapper(self) -> dict:
        self._capture_root.mkdir(parents=True, exist_ok=True)
        index = self._next_capture_index()
        stdout_path = self._capture_root / f"status_{index:03d}_stdout.txt"
        stderr_path = self._capture_root / f"status_{index:03d}_stderr.txt"
        # The child receives exactly the declared finite environment; the
        # default is explicitly empty and ambient inheritance never occurs.
        observation = observe_command(
            self._argv,
            self._cwd,
            dict(self._env),
            self._timeout,
            self._runner,
            stdout_path,
            stderr_path,
            max_stream_bytes=self.MAX_STREAM_BYTES,
        )
        refusals = {
            "missing_executable": (
                "frutlups_executable_missing",
                "the declared planning-state executable does not exist",
            ),
            "runner_failure": (
                "frutlups_transport_failed",
                "the bounded process runner could not complete the declared "
                "planning-state command",
            ),
            "timeout": (
                "frutlups_timeout",
                "the planning-state command exceeded its declared timeout",
            ),
            "overflow": (
                "frutlups_output_overflow",
                "the planning-state command exceeded the 1 MiB per-stream "
                "capture bound",
            ),
            "no_status": (
                "frutlups_status_unavailable",
                "the planning-state command produced no usable exit status",
            ),
        }
        if observation.kind in refusals:
            code, message = refusals[observation.kind]
            raise PlanProviderUnavailable(code, message)
        if observation.exit_code != 0:
            raise PlanProviderUnavailable(
                "frutlups_exit_nonzero",
                "the planning-state command exited with a nonzero status",
            )
        data = stdout_path.read_bytes()
        try:
            payload = json.loads(
                data.decode("utf-8"),
                parse_constant=_reject_nonstandard_constant,
            )
        except (UnicodeDecodeError, ValueError):
            raise PlanProviderUnavailable(
                "frutlups_output_malformed",
                "the planning-state command output is not one strict JSON "
                "document",
            ) from None
        if not isinstance(payload, dict):
            raise PlanProviderUnavailable(
                "frutlups_output_not_object",
                "the planning-state command output is not a JSON object",
            )
        return payload

    def read_memory_mode(self) -> MemoryMode:
        """Read and strictly validate the declaration before run creation."""

        payload = self._read_wrapper()
        if "memory_mode" not in payload:
            raise PlanProviderUnavailable(
                "memory_mode_member_missing",
                "the status wrapper has no frutlups.memory_mode member",
            )
        try:
            return parse_memory_mode(payload["memory_mode"])
        except PlanningStateRefusal as refusal:
            raise PlanProviderUnavailable(refusal.code, refusal.message) from None

    def read_planning_state(self) -> PlanningState:
        payload = self._read_wrapper()
        if "memory_mode" not in payload:
            raise PlanProviderUnavailable(
                "memory_mode_member_missing",
                "the status wrapper has no frutlups.memory_mode member",
            )
        try:
            memory_mode = parse_memory_mode(payload["memory_mode"])
        except PlanningStateRefusal as refusal:
            raise PlanProviderUnavailable(refusal.code, refusal.message) from None
        if (
            self._expected_memory_mode is not None
            and memory_mode != self._expected_memory_mode
        ):
            raise PlanProviderUnavailable(
                "memory_mode_changed",
                "the declared memory mode no longer matches the run manifest",
            )
        frontier_member = payload.get("planning_frontier")
        if frontier_member is None:
            raise PlanProviderUnavailable(
                "planning_frontier_member_missing",
                "the status wrapper has no planning_frontier member",
            )
        if not isinstance(frontier_member, dict):
            raise PlanProviderUnavailable(
                "planning_frontier_member_invalid",
                "the status wrapper planning_frontier member is not an object",
            )
        resume_member = payload.get("loop_resume")
        if resume_member is None:
            raise PlanProviderUnavailable(
                "loop_resume_member_missing",
                "the status wrapper has no loop_resume member",
            )
        if not isinstance(resume_member, dict):
            raise PlanProviderUnavailable(
                "loop_resume_member_invalid",
                "the status wrapper loop_resume member is not an object",
            )
        # The smallest structured identity extraction: the wrapper's slice
        # entries map slice id -> milestone id. A duplicate slice id with a
        # different milestone is not a unique identity and claims nothing.
        # Nothing else from the wrapper — including machine-local paths,
        # messages, next_command, or the raw payload — is retained.
        slice_identities: dict[str, str] = {}
        raw_slices = payload.get("slices")
        if isinstance(raw_slices, list):
            for entry in raw_slices[:1000]:
                if not isinstance(entry, dict):
                    continue
                slice_id = entry.get("id")
                milestone_id = entry.get("milestone_id")
                if not isinstance(slice_id, str) or not isinstance(
                    milestone_id, str
                ):
                    continue
                if (
                    slice_id in slice_identities
                    and slice_identities[slice_id] != milestone_id
                ):
                    slice_identities[slice_id] = ""
                elif slice_id:
                    slice_identities.setdefault(slice_id, milestone_id)
        return parse_released_frontier(
            frontier_member, resume_member, slice_identities
        )


def parse_memory_mode(member: object) -> MemoryMode:
    """Parse exactly the released six-key ``frutlups.memory_mode`` shape.

    Producer diagnostics are intentionally not carried across the boundary.
    Every refusal is one stable drive-owned code and bounded message.
    """

    if not isinstance(member, dict):
        raise PlanningStateRefusal(
            "memory_mode_member_invalid",
            "frutlups.memory_mode must be a JSON object",
        )
    if set(member) != MEMORY_MODE_KEYS:
        raise PlanningStateRefusal(
            "memory_mode_shape_invalid",
            "frutlups.memory_mode must contain exactly the six version-1 keys",
        )
    if member.get("contract_id") != MEMORY_MODE_CONTRACT_ID or not (
        type(member.get("contract_version")) is str
        and member.get("contract_version") == MEMORY_MODE_CONTRACT_VERSION
    ):
        raise PlanningStateRefusal(
            "memory_mode_contract_refused",
            "the memory-mode contract/version is not implemented by this consumer",
        )
    diagnostics = member.get("diagnostics")
    if not isinstance(diagnostics, list) or not all(
        isinstance(item, str) for item in diagnostics
    ):
        raise PlanningStateRefusal(
            "memory_mode_diagnostics_invalid",
            "frutlups.memory_mode diagnostics must be an array of strings",
        )
    if member.get("valid") is not True:
        raise PlanningStateRefusal(
            "memory_mode_invalid",
            "frutlups reported an invalid memory declaration",
        )
    mode = member.get("mode")
    if type(mode) is not str or mode not in MEMORY_MODES:
        raise PlanningStateRefusal(
            "memory_mode_value_invalid",
            "frutlups.memory_mode names no canonical version-1 mode",
        )
    memory_root = member.get("memory_root")
    if mode in ("none", "lightweight"):
        if memory_root is not None:
            raise PlanningStateRefusal(
                "memory_root_unexpected",
                "non-llloom declarations must have a null memory_root",
            )
        return MemoryMode(mode=mode, memory_root=None)
    if type(memory_root) is not str or not _is_valid_memory_root(memory_root):
        raise PlanningStateRefusal(
            "memory_root_invalid",
            "llloom declarations require a canonical repository-relative root",
        )
    return MemoryMode(mode=mode, memory_root=memory_root)


def _is_valid_memory_root(value: str) -> bool:
    if not value or len(value) > 512 or _BACKSLASH in value or "//" in value:
        return False
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    parts = value.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def parse_planning_state(data: bytes) -> PlanningState:
    if len(data) > MAX_PLANNING_STATE_BYTES:
        return _invalid_state(
            "input_too_large",
            "planning state exceeds the 1 MiB input limit",
        )
    try:
        payload = json.loads(data, parse_constant=_reject_nonstandard_constant)
    except (UnicodeDecodeError, ValueError):
        # ValueError covers json.JSONDecodeError and the non-standard-constant
        # rejection: NaN/Infinity/-Infinity are not JSON and fail closed here.
        return _invalid_state("malformed_json", "planning state is not valid JSON")
    if not isinstance(payload, dict):
        return _invalid_state(
            "not_an_object", "planning state top level is not a JSON object"
        )
    if payload.get("contract") != ACCEPTED_CONTRACT or not (
        type(payload.get("version")) is int
        and payload.get("version") == ACCEPTED_VERSION
    ):
        raise PlanningStateRefusal(
            "contract_version_refused",
            "planning-state contract/version is not implemented by this "
            f"consumer; accepted: {ACCEPTED_CONTRACT} version {ACCEPTED_VERSION}",
        )
    try:
        return _parse_payload(payload)
    except _Invalid as err:
        return _invalid_state(err.code, err.message)


def _reject_nonstandard_constant(token: str) -> None:
    raise ValueError("non-standard JSON constant")


_DRIVE_QUALIFIED = re.compile(r"^[A-Za-z]:")


def _is_valid_artifact_reference(value: str) -> bool:
    """Bounded repo-relative POSIX file-path grammar for artifact references.

    ``/`` is the only separator; absolute, UNC, drive-qualified,
    backslash-containing, empty/blank, NUL-containing, ``.``/``..``-segment,
    and trailing-directory forms are invalid. M001 validates the serialized
    reference form only; nothing is resolved against a workspace.
    """
    if not value.strip() or "\x00" in value or _BACKSLASH in value:
        return False
    if value.startswith("/") or value.endswith("/") or _DRIVE_QUALIFIED.match(value):
        return False
    return all(segment not in ("", ".", "..") for segment in value.split("/"))


def _checked_artifact_reference(field: str, value: str | None) -> str | None:
    if value is not None and not _is_valid_artifact_reference(value):
        raise _Invalid(
            "artifact_path_invalid",
            f"field '{field}' must be a repo-relative POSIX file path",
        )
    return value


def _invalid_state(code: str, message: str) -> PlanningState:
    return PlanningState(
        outcome=PlanOutcome.INVALID,
        step=None,
        actor=None,
        gate_state=None,
        frontier=None,
        artifacts=ArtifactPaths(None, None, None, None, None),
        verdict=None,
        blocked=None,
        completion_evidence=None,
        diagnostics=(Diagnostic("error", code, message),),
        next_command=None,
    )


def _parse_payload(payload: dict) -> PlanningState:
    outcome_raw = _required_str(payload, "outcome")
    try:
        outcome = PlanOutcome(outcome_raw)
    except ValueError:
        raise _Invalid(
            "unknown_outcome",
            "planning state names an outcome this consumer does not implement",
        ) from None

    step_raw = _optional_str(payload, "step")
    step: LoopStep | None = None
    if step_raw is not None:
        try:
            step = LoopStep(step_raw)
        except ValueError:
            raise _Invalid(
                "unknown_step",
                "planning state names a loop step this consumer does not implement",
            ) from None
    if outcome is PlanOutcome.READY and step is None:
        raise _Invalid("step_missing", "outcome 'ready' requires a loop step")
    if outcome is not PlanOutcome.READY and step is not None:
        raise _Invalid(
            "step_forbidden",
            f"outcome '{outcome.value}' requires the loop step to be null",
        )

    actor = _optional_str(payload, "actor")
    if actor is not None and actor not in _ACTOR_VALUES:
        raise _Invalid(
            "unknown_actor",
            "planning state names an actor this consumer does not implement",
        )

    state = PlanningState(
        outcome=outcome,
        step=step,
        actor=actor,
        gate_state=_optional_str(payload, "gate_state"),
        frontier=_parse_frontier(payload.get("frontier")),
        artifacts=_parse_artifacts(payload.get("artifacts")),
        verdict=_parse_verdict(payload.get("verdict")),
        blocked=_parse_blocked(payload.get("blocked")),
        completion_evidence=_parse_completion_evidence(
            payload.get("completion_evidence")
        ),
        diagnostics=_parse_diagnostics(payload.get("diagnostics")),
        next_command=_optional_str(payload, "next_command"),
    )

    if outcome is PlanOutcome.BLOCKED and (
        state.blocked is None or not state.blocked.owner.strip()
    ):
        raise _Invalid(
            "blocked_owner_missing",
            "outcome 'blocked' requires a blocked reference naming its owner",
        )
    if outcome is PlanOutcome.COMPLETE and state.completion_evidence is None:
        raise _Invalid(
            "completion_evidence_missing",
            "outcome 'complete' requires a completion-evidence reference",
        )
    return state


def _required_str(obj: dict, key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise _Invalid(
            "field_type_invalid", f"field '{key}' must be a non-null string"
        )
    return value


def _optional_str(obj: dict, key: str) -> str | None:
    value = obj.get(key)
    if value is not None and not isinstance(value, str):
        raise _Invalid("field_type_invalid", f"field '{key}' must be a string or null")
    return value


def _member_str(obj: dict, parent: str, key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise _Invalid(
            "field_type_invalid", f"field '{parent}.{key}' must be a string"
        )
    return value


def _parse_frontier(raw: object) -> Frontier | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _Invalid("field_type_invalid", "field 'frontier' must be an object or null")
    round_value = raw.get("round")
    if type(round_value) is not int:
        raise _Invalid("field_type_invalid", "field 'frontier.round' must be an integer")
    return Frontier(
        milestone_id=_member_str(raw, "frontier", "milestone_id"),
        slice_id=_member_str(raw, "frontier", "slice_id"),
        slice_title=_member_str(raw, "frontier", "slice_title"),
        round=round_value,
    )


def _parse_artifacts(raw: object) -> ArtifactPaths:
    if raw is None:
        return ArtifactPaths(None, None, None, None, None)
    if not isinstance(raw, dict):
        raise _Invalid(
            "field_type_invalid", "field 'artifacts' must be an object or null"
        )
    values: dict[str, str | None] = {}
    for key in _ARTIFACT_KEYS:
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            raise _Invalid(
                "field_type_invalid",
                f"field 'artifacts.{key}' must be a string or null",
            )
        values[key] = _checked_artifact_reference(f"artifacts.{key}", value)
    return ArtifactPaths(**values)


def _parse_verdict(raw: object) -> Verdict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _Invalid("field_type_invalid", "field 'verdict' must be an object or null")
    value = _member_str(raw, "verdict", "value")
    if value not in _VERDICT_VALUES:
        raise _Invalid(
            "unknown_verdict",
            "planning state names a verdict value this consumer does not implement",
        )
    return Verdict(
        value=value,
        next_move=_member_str(raw, "verdict", "next_move"),
        report=_checked_artifact_reference(
            "verdict.report", _member_str(raw, "verdict", "report")
        ),
    )


def _parse_blocked(raw: object) -> BlockedRef | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _Invalid("field_type_invalid", "field 'blocked' must be an object or null")
    owner = raw.get("owner")
    if owner is not None and not isinstance(owner, str):
        raise _Invalid("field_type_invalid", "field 'blocked.owner' must be a string")
    return BlockedRef(
        citation=_member_str(raw, "blocked", "citation"),
        owner=owner or "",
    )


def _parse_completion_evidence(raw: object) -> CompletionEvidence | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _Invalid(
            "field_type_invalid",
            "field 'completion_evidence' must be an object or null",
        )
    return CompletionEvidence(
        path=_checked_artifact_reference(
            "completion_evidence.path",
            _member_str(raw, "completion_evidence", "path"),
        )
    )


def _parse_diagnostics(raw: object) -> tuple[Diagnostic, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _Invalid(
            "field_type_invalid", "field 'diagnostics' must be an array or null"
        )
    entries = []
    for item in raw:
        if not isinstance(item, dict):
            raise _Invalid(
                "field_type_invalid", "field 'diagnostics' entries must be objects"
            )
        severity = _member_str(item, "diagnostics", "severity")
        if severity not in _SEVERITY_VALUES:
            raise _Invalid(
                "unknown_severity",
                "planning state names a diagnostic severity this consumer "
                "does not implement",
            )
        entries.append(
            Diagnostic(
                severity=severity,
                code=_member_str(item, "diagnostics", "code"),
                message=_member_str(item, "diagnostics", "message"),
            )
        )
    return tuple(entries)
