"""Declaration-authoritative, subprocess-only llloom hook adapter.

The driven project's validated :class:`~frutlups_drive.planstate.MemoryMode`
is the only switch.  This module never imports llloom, never writes memory
bytes itself, and never interprets memory as runner control.  It binds only
the released ``liveness``, read-only ``query``, and proposal-inbox
``submit-update`` CLI surfaces.  Submission lets llloom add one pending
envelope for its own review; it grants the drive no memory authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from frutlups_drive.dispatch.subprocess_agent import (
    checked_timeout_seconds,
    observe_command,
)
from frutlups_drive.planstate import MemoryMode
from frutlups_drive.policy import ExecutionPolicy, _secret_shaped
from frutlups_drive.runstore import RunStore, RunStoreRefusal
from frutlups_drive.verifier import ProcessRunner

LIVENESS_SCHEMA = "llloom.liveness.v1"
UPDATE_PROPOSAL_SCHEMA = "llloom.update_proposal.v1"
UPDATE_SUBMISSION_SCHEMA = "llloom.update_submission_result.v1"
LLLOOM_BINDING_SCHEMA = "frutlups_drive_llloom_binding_v1"
LLLOOM_BINDING_RELATIVE = "local_state/llloom_binding.toml"
MAX_BINDING_BYTES = 65_536
MAX_CONTEXT_BYTES = 65_536
MAX_CONTEXT_QUESTION_CHARS = 2_048
MAX_UPDATE_PROPOSALS = 32
MAX_UPDATE_PROPOSAL_CHARS = 1_024
MAX_UPDATE_BYTES = 65_536
MAX_UPDATE_SUMMARY_CHARS = 8_000

_LIVENESS_KEYS = frozenset(
    {
        "schema",
        "llloom_version",
        "status",
        "reason",
        "root_present",
        "workspace_valid",
        "usable",
        "lock_held",
    }
)
_LIVENESS_REASONS = frozenset(
    {
        "healthy",
        "root_absent",
        "root_not_directory",
        "layout_invalid",
        "schema_invalid",
        "reparse_point",
        "lock_held",
        "transaction_pending",
        "permission_limited",
        "inaccessible",
    }
)
_UPDATE_SUBMISSION_KEYS = frozenset(
    {
        "schema",
        "llloom_version",
        "status",
        "reason",
        "proposal_id",
        "liveness_status",
        "liveness_reason",
    }
)
_UPDATE_EXIT_REASONS = {
    0: frozenset({"accepted"}),
    3: frozenset({"root_absent"}),
    4: _LIVENESS_REASONS - {"healthy", "root_absent"}
    | frozenset({"inbox_unavailable"}),
    5: frozenset({"input_inaccessible", "malformed_document"}),
    6: frozenset({"document_oversized"}),
    7: frozenset({"duplicate_proposal"}),
    8: frozenset({"unauthorized_action"}),
    9: frozenset({"publication_failed"}),
}
_UPDATE_FACTS = {
    0: ("healthy", "update_submitted"),
    3: ("refused", "update_submission_root_absent"),
    4: ("refused", "update_submission_root_unhealthy"),
    5: ("refused", "update_submission_document_malformed"),
    6: ("refused", "update_submission_document_oversized"),
    7: ("healthy", "update_already_submitted"),
    8: ("refused", "update_submission_unauthorized"),
    9: ("refused", "update_submission_publication_failed"),
}
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TOOL_IDENTITY = re.compile(
    r"llloom-(?P<version>(?:0|[1-9][0-9]{0,4})"
    r"(?:\.(?:0|[1-9][0-9]{0,4})){1,3})"
)
_SLICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_PROPOSAL_ID = re.compile(r"up\.[0-9a-f]{64}")


class MemoryHookRefusal(Exception):
    """Stable, bounded configuration or reconciliation refusal."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class _LlloomIdentityMismatch(Exception):
    """Structurally valid result from a different tool identity."""


@dataclass(frozen=True)
class LlloomBinding:
    argv_prefix: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    tool_identity: str
    tool_version: str
    binding_sha256: str
    executable_sha256: str

    def manifest_facts(self) -> dict[str, str]:
        return {
            "llloom_binding_sha256": self.binding_sha256,
            "llloom_executable_sha256": self.executable_sha256,
            "llloom_tool_identity": self.tool_identity,
        }

    @classmethod
    def manifest_names(cls) -> frozenset[str]:
        return frozenset(
            {
                "llloom_binding_sha256",
                "llloom_executable_sha256",
                "llloom_tool_identity",
            }
        )


@dataclass(frozen=True)
class MemoryHookFact:
    hook: str
    status: str
    reason: str
    evidence: str = ""
    proposal_id: str = ""
    proposal_document: str = ""
    queue_evidence: str = ""


@dataclass(frozen=True)
class ContextRead:
    context: bytes
    facts: tuple[MemoryHookFact, ...]


def reconcile_memory_mode(mode: MemoryMode, policy: ExecutionPolicy) -> None:
    """Reconcile declaration authority against the committed policy.

    The v1 policy can either follow project state or decline it.  A released
    frutlups declaration is admissible only when the committed policy says it
    follows project state; flags can never upgrade the declaration.
    """

    if not isinstance(mode, MemoryMode):
        raise MemoryHookRefusal(
            "memory_mode_missing", "no typed memory declaration was observed"
        )
    if policy.memory.follow_project_state is not True:
        raise MemoryHookRefusal(
            "memory_mode_policy_mismatch",
            "policy.memory.follow_project_state must be true for released declarations",
        )
    if policy.memory.upgrade_via_flag is not False:
        raise MemoryHookRefusal(
            "memory_upgrade_policy_invalid",
            "policy flags may not upgrade the declared memory mode",
        )


def load_llloom_binding(path: Path | str) -> LlloomBinding:
    """Load the ignored exact-executable binding without PATH discovery."""

    binding_path = Path(path)
    if not _ordinary_file(binding_path):
        raise MemoryHookRefusal(
            "llloom_binding_missing",
            "the machine-local llloom binding is absent or not an ordinary file",
        )
    raw = binding_path.read_bytes()
    if len(raw) > MAX_BINDING_BYTES:
        raise MemoryHookRefusal(
            "llloom_binding_oversized", "the llloom binding exceeds its byte bound"
        )
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise MemoryHookRefusal(
            "llloom_binding_malformed", "the llloom binding is not valid TOML"
        ) from None
    if set(document) - {"schema_version", "launch", "env"}:
        raise MemoryHookRefusal(
            "llloom_binding_shape_invalid", "the llloom binding has unknown keys"
        )
    if document.get("schema_version") != LLLOOM_BINDING_SCHEMA:
        raise MemoryHookRefusal(
            "llloom_binding_schema_unknown",
            "the llloom binding schema version is not implemented",
        )
    launch = document.get("launch")
    if not isinstance(launch, dict) or set(launch) != {
        "argv_prefix",
        "tool_identity",
    }:
        raise MemoryHookRefusal(
            "llloom_binding_field_invalid",
            "the llloom binding must declare the exact launch fields",
        )
    argv = launch.get("argv_prefix")
    if (
        not isinstance(argv, list)
        or not argv
        or len(argv) > 16
        or not all(_safe_text(part) for part in argv)
    ):
        raise MemoryHookRefusal(
            "llloom_binding_field_invalid",
            "launch.argv_prefix must be a bounded nonempty string array",
        )
    executable = Path(argv[0])
    if not executable.is_absolute() or not _ordinary_file(executable):
        raise MemoryHookRefusal(
            "llloom_executable_missing",
            "the exact bound llloom executable is absent or link-like",
        )
    tool_identity = launch.get("tool_identity")
    identity_match = (
        _TOOL_IDENTITY.fullmatch(tool_identity)
        if type(tool_identity) is str
        else None
    )
    if identity_match is None:
        raise MemoryHookRefusal(
            "llloom_tool_identity_invalid",
            "the binding tool identity must be llloom- plus 2 to 4 "
            "bounded dotted-numeric components",
        )
    env = document.get("env", {})
    if not isinstance(env, dict):
        raise MemoryHookRefusal(
            "llloom_binding_field_invalid", "the llloom env member must be a table"
        )
    child_env: dict[str, str] = {}
    for name, value in env.items():
        if (
            type(name) is not str
            or not _ENV_NAME.fullmatch(name)
            or type(value) is not str
            or not _safe_text(value, allow_empty=True)
        ):
            raise MemoryHookRefusal(
                "llloom_binding_field_invalid",
                "llloom env entries must map bounded names to strings",
            )
        if _secret_shaped(name) and value:
            raise MemoryHookRefusal(
                "llloom_binding_secret_shaped",
                "credentials are forbidden in the llloom binding",
            )
        child_env[name] = value
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    return LlloomBinding(
        argv_prefix=tuple(argv),
        env=tuple(sorted(child_env.items())),
        tool_identity=tool_identity,
        tool_version=identity_match.group("version"),
        binding_sha256=hashlib.sha256(raw).hexdigest(),
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )


class LlloomMemoryHooks:
    """The three optional hook seams for one declared-llloom run."""

    def __init__(
        self,
        *,
        project_root: Path,
        memory_mode: MemoryMode,
        binding: LlloomBinding | None,
        binding_refusal: str | None,
        store: RunStore,
        run_id: str,
        runner: ProcessRunner,
        timeout_seconds: float = 30.0,
    ) -> None:
        if memory_mode.mode != "llloom" or memory_mode.memory_root is None:
            raise ValueError("llloom hooks require a declared llloom root")
        if binding is not None and binding_refusal is not None:
            raise ValueError("binding and binding_refusal are mutually exclusive")
        self._project = Path(project_root)
        self._mode = memory_mode
        self._binding = binding
        self._binding_refusal = binding_refusal or "llloom_binding_missing"
        self._store = store
        self._run_id = run_id
        self._runner = runner
        self._timeout = checked_timeout_seconds(timeout_seconds)
        self._evidence_root = store.run_dir(run_id) / "memory_hooks"

    def preflight(self) -> tuple[MemoryHookFact, ...]:
        return (self._liveness("preflight"),)

    def read_context(self, prompt: bytes) -> ContextRead:
        liveness = self._liveness("context")
        if liveness.status != "healthy":
            return ContextRead(
                b"",
                (
                    liveness,
                    MemoryHookFact(
                        "bounded_context", "refused", liveness.reason
                    ),
                ),
            )
        try:
            question = _bounded_question(prompt)
        except (UnicodeDecodeError, ValueError):
            return ContextRead(
                b"",
                (
                    liveness,
                    MemoryHookFact(
                        "bounded_context", "refused", "context_prompt_invalid"
                    ),
                ),
            )
        stdout, observation = self._invoke(
            "query",
            ("query", question),
            max_stream_bytes=MAX_CONTEXT_BYTES,
        )
        failure = _transport_reason(observation)
        if failure is not None or observation.exit_code != 0:
            reason = failure or "context_exit_nonzero"
            return ContextRead(
                b"",
                (
                    liveness,
                    MemoryHookFact("bounded_context", "refused", reason),
                ),
            )
        try:
            context = _context_bytes(stdout.read_bytes())
        except (OSError, UnicodeDecodeError, ValueError):
            return ContextRead(
                b"",
                (
                    liveness,
                    MemoryHookFact(
                        "bounded_context", "refused", "context_output_malformed"
                    ),
                ),
            )
        return ContextRead(
            context,
            (
                liveness,
                MemoryHookFact(
                    "bounded_context",
                    "healthy",
                    "context_available" if context else "context_empty",
                    stdout.name,
                ),
            ),
        )

    def queue_updates(
        self, slice_id: str, proposals: tuple[str, ...] = ()
    ) -> tuple[MemoryHookFact, ...]:
        payload = _update_payload(slice_id, proposals)
        try:
            evidence = self._store.write_memory_update_queue(
                self._run_id, slice_id, payload
            )
        except (OSError, RunStoreRefusal, ValueError):
            return (
                MemoryHookFact(
                    "boundary_update_queue", "refused", "update_evidence_refused"
                ),
            )
        liveness = self._liveness("update")
        if liveness.status != "healthy":
            return (
                liveness,
                MemoryHookFact(
                    "boundary_update_submission",
                    "refused",
                    liveness.reason,
                    queue_evidence=evidence.name,
                ),
            )
        try:
            proposal, expected_proposal_id = _render_update_proposal(
                evidence,
                run_id=self._run_id,
                slice_id=slice_id,
            )
        except (OSError, UnicodeDecodeError, ValueError):
            return (
                liveness,
                MemoryHookFact(
                    "boundary_update_submission",
                    "refused",
                    "update_proposal_render_refused",
                    queue_evidence=evidence.name,
                ),
            )
        proposal_path = evidence.with_name(
            f"{evidence.stem}.submit-update.json"
        )
        try:
            _write_evidence_once(proposal_path, proposal)
        except OSError:
            return (
                liveness,
                MemoryHookFact(
                    "boundary_update_submission",
                    "refused",
                    "update_proposal_evidence_refused",
                    proposal_document=proposal_path.name,
                    queue_evidence=evidence.name,
                ),
            )
        stdout, observation = self._invoke(
            "submit_update",
            ("submit-update", str(proposal_path)),
            max_stream_bytes=16_384,
        )
        failure = _transport_reason(observation)
        if failure is not None:
            return (
                liveness,
                MemoryHookFact(
                    "boundary_update_submission",
                    "refused",
                    failure,
                    stdout.name,
                    proposal_document=proposal_path.name,
                    queue_evidence=evidence.name,
                ),
            )
        try:
            result = _strict_json_object(stdout.read_bytes())
            proposal_id = _validate_update_submission(
                result,
                observation.exit_code,
                self._binding.tool_version if self._binding else "",
                expected_proposal_id,
            )
        except _LlloomIdentityMismatch:
            return (
                liveness,
                MemoryHookFact(
                    "boundary_update_submission",
                    "refused",
                    "llloom_identity_mismatch",
                    stdout.name,
                    proposal_document=proposal_path.name,
                    queue_evidence=evidence.name,
                ),
            )
        except (OSError, UnicodeDecodeError, ValueError):
            return (
                liveness,
                MemoryHookFact(
                    "boundary_update_submission",
                    "refused",
                    "update_submission_output_malformed",
                    stdout.name,
                    proposal_document=proposal_path.name,
                    queue_evidence=evidence.name,
                ),
            )
        status, reason = _UPDATE_FACTS[observation.exit_code]
        return (
            liveness,
            MemoryHookFact(
                "boundary_update_submission",
                status,
                reason,
                stdout.name,
                proposal_id=proposal_id or "",
                proposal_document=proposal_path.name,
                queue_evidence=evidence.name,
            ),
        )

    def _liveness(self, purpose: str) -> MemoryHookFact:
        issue = _root_issue(self._project, self._mode.memory_root or "")
        if issue is not None:
            return MemoryHookFact("liveness", "refused", issue)
        if self._binding is None:
            return MemoryHookFact(
                "liveness", "refused", self._binding_refusal
            )
        stdout, observation = self._invoke(
            f"{purpose}_liveness", ("liveness",), max_stream_bytes=16_384
        )
        failure = _transport_reason(observation)
        if failure is not None:
            return MemoryHookFact("liveness", "refused", failure, stdout.name)
        try:
            result = json.loads(stdout.read_bytes().decode("utf-8"))
            reason = _validate_liveness(
                result,
                observation.exit_code,
                self._binding.tool_version,
            )
        except _LlloomIdentityMismatch:
            return MemoryHookFact(
                "liveness", "refused", "llloom_identity_mismatch", stdout.name
            )
        except (OSError, UnicodeDecodeError, ValueError):
            return MemoryHookFact(
                "liveness", "refused", "liveness_output_malformed", stdout.name
            )
        return MemoryHookFact(
            "liveness",
            "healthy" if reason == "healthy" else "refused",
            reason,
            stdout.name,
        )

    def _invoke(
        self,
        label: str,
        command: tuple[str, ...],
        *,
        max_stream_bytes: int,
    ):
        if self._binding is None:
            raise AssertionError("binding checked before invocation")
        self._evidence_root.mkdir(parents=True, exist_ok=True)
        index = _next_capture_index(self._evidence_root)
        stdout = self._evidence_root / f"{index:03d}_{label}_stdout.txt"
        stderr = self._evidence_root / f"{index:03d}_{label}_stderr.txt"
        root = self._project.joinpath(
            *PurePosixPath(self._mode.memory_root or "").parts
        )
        observation = observe_command(
            self._binding.argv_prefix
            + ("--root", str(root))
            + command,
            self._project,
            dict(self._binding.env),
            self._timeout,
            self._runner,
            stdout,
            stderr,
            max_stream_bytes=max_stream_bytes,
        )
        return stdout, observation


def _ordinary_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode) and not _is_link_like(path)
    except OSError:
        return False


def _safe_text(value: object, *, allow_empty: bool = False) -> bool:
    return (
        type(value) is str
        and len(value) <= 4_096
        and (allow_empty or bool(value))
        and "\x00" not in value
        and all(ord(char) >= 32 and ord(char) != 127 for char in value)
    )


def _is_link_like(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse and attributes & reparse)


def _root_issue(project: Path, relative: str) -> str | None:
    candidate = project.joinpath(*PurePosixPath(relative).parts)
    cursor = project
    for part in PurePosixPath(relative).parts:
        cursor = cursor / part
        # ``exists()`` follows links and is false for a dangling link.  The
        # lstat-based predicate must therefore stand alone so every reparse
        # point is refused before containment resolution or subprocess use.
        if _is_link_like(cursor):
            return "memory_root_link_like"
    try:
        candidate.resolve(strict=False).relative_to(project.resolve())
    except (OSError, ValueError):
        return "memory_root_escapes"
    if candidate.exists() and not candidate.is_dir():
        return "memory_root_not_directory"
    return None


def _next_capture_index(root: Path) -> int:
    highest = 0
    for entry in root.iterdir():
        prefix = entry.name.split("_", 1)[0]
        if len(prefix) <= 9 and prefix.isdigit():
            highest = max(highest, int(prefix))
    return highest + 1


def _transport_reason(observation) -> str | None:
    return {
        "missing_executable": "llloom_executable_missing",
        "runner_failure": "llloom_spawn_failed",
        "timeout": "llloom_timeout",
        "overflow": "llloom_output_overflow",
        "no_status": "llloom_status_unavailable",
    }.get(observation.kind)


def _validate_liveness(
    result: object,
    exit_code: int | None,
    expected_version: str,
) -> str:
    if not isinstance(result, dict) or set(result) != _LIVENESS_KEYS:
        raise ValueError("liveness shape invalid")
    if result.get("schema") != LIVENESS_SCHEMA:
        raise ValueError("liveness schema invalid")
    payload_version = result.get("llloom_version")
    if type(payload_version) is not str:
        raise ValueError("llloom version type invalid")
    status = result.get("status")
    reason = result.get("reason")
    if status not in ("healthy", "absent", "unhealthy"):
        raise ValueError("liveness status invalid")
    if reason not in _LIVENESS_REASONS:
        raise ValueError("liveness reason invalid")
    for name in ("root_present", "workspace_valid", "usable", "lock_held"):
        if result.get(name) is not None and type(result.get(name)) is not bool:
            raise ValueError("liveness fact type invalid")
    expected_exit = {"healthy": 0, "absent": 3, "unhealthy": 4}[status]
    if exit_code != expected_exit:
        raise ValueError("liveness exit invalid")
    if status == "healthy" and not (
        reason == "healthy"
        and result.get("root_present") is True
        and result.get("workspace_valid") is True
        and result.get("usable") is True
        and result.get("lock_held") is False
    ):
        raise ValueError("healthy liveness combination invalid")
    if status == "absent" and reason != "root_absent":
        raise ValueError("absent liveness combination invalid")
    if status == "unhealthy" and reason in ("healthy", "root_absent"):
        raise ValueError("unhealthy liveness combination invalid")
    if payload_version != expected_version:
        raise _LlloomIdentityMismatch
    return str(reason)


def _strict_json_object(raw: bytes) -> dict[str, object]:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("non-finite JSON value")

    document = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )
    if not isinstance(document, dict):
        raise ValueError("JSON result must be an object")
    return document


def _render_update_proposal(
    evidence: Path,
    *,
    run_id: str,
    slice_id: str,
) -> tuple[bytes, str]:
    summary_bytes = evidence.read_bytes()
    if len(summary_bytes) > MAX_UPDATE_BYTES:
        raise ValueError("queue evidence exceeds its bound")
    stored_text = summary_bytes.decode("utf-8")
    if not stored_text.endswith("\n"):
        raise ValueError("queue evidence lacks its canonical terminator")
    summary = stored_text.removesuffix("\n")
    if (
        not summary
        or summary != summary.strip()
        or len(summary) > MAX_UPDATE_SUMMARY_CHARS
    ):
        raise ValueError("queue evidence cannot fit the proposal summary")
    title = f"Review boundary update queue for {slice_id}"
    evidence_refs = [
        f"run:{run_id}",
        f"artifact:memory_updates/{evidence.name}",
    ]
    if len(title) > 200 or any(len(ref) > 512 for ref in evidence_refs):
        raise ValueError("proposal metadata exceeds its bound")
    identity = f"{run_id}\0{slice_id}\0slice_complete".encode("utf-8")
    client_proposal_id = "boundary:" + hashlib.sha256(identity).hexdigest()
    document = {
        "schema": UPDATE_PROPOSAL_SCHEMA,
        "submitter_id": "frutlups-drive",
        "client_proposal_id": client_proposal_id,
        "requested_action": "review",
        "title": title,
        "summary": summary,
        "evidence_refs": evidence_refs,
    }
    proposal = (
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(proposal) > MAX_UPDATE_BYTES:
        raise ValueError("proposal document exceeds its byte bound")
    proposal_identity = (
        document["submitter_id"]
        + "\0"
        + document["client_proposal_id"]
    ).encode("utf-8")
    return proposal, "up." + hashlib.sha256(proposal_identity).hexdigest()


def _write_evidence_once(path: Path, payload: bytes) -> None:
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if existing != payload:
            raise OSError("proposal evidence conflicts with existing bytes")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".memory-proposal-", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", buffering=0) as stream:
            stream.write(payload)
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise OSError("proposal evidence conflicts with concurrent bytes")
    finally:
        temporary.unlink(missing_ok=True)


def _validate_update_submission(
    result: dict[str, object],
    exit_code: int | None,
    expected_version: str,
    expected_proposal_id: str,
) -> str | None:
    if set(result) != _UPDATE_SUBMISSION_KEYS:
        raise ValueError("submission result shape invalid")
    if result.get("schema") != UPDATE_SUBMISSION_SCHEMA:
        raise ValueError("submission result schema invalid")
    payload_version = result.get("llloom_version")
    if type(payload_version) is not str:
        raise ValueError("submission version type invalid")
    status = result.get("status")
    reason = result.get("reason")
    proposal_id = result.get("proposal_id")
    liveness_status = result.get("liveness_status")
    liveness_reason = result.get("liveness_reason")
    if status not in ("accepted", "refused") or type(reason) is not str:
        raise ValueError("submission disposition invalid")
    if proposal_id is not None and (
        type(proposal_id) is not str
        or not _PROPOSAL_ID.fullmatch(proposal_id)
        or proposal_id != expected_proposal_id
    ):
        raise ValueError("submission proposal identity invalid")
    if liveness_status not in ("healthy", "absent", "unhealthy"):
        raise ValueError("submission liveness status invalid")
    if liveness_reason not in _LIVENESS_REASONS:
        raise ValueError("submission liveness reason invalid")
    if exit_code not in _UPDATE_EXIT_REASONS:
        raise ValueError("submission exit invalid")
    if reason not in _UPDATE_EXIT_REASONS[exit_code]:
        raise ValueError("submission exit reason invalid")
    if exit_code == 0 and not (
        status == "accepted"
        and proposal_id == expected_proposal_id
        and liveness_status == "healthy"
        and liveness_reason == "healthy"
    ):
        raise ValueError("accepted submission combination invalid")
    if exit_code != 0 and status != "refused":
        raise ValueError("refused submission combination invalid")
    if exit_code == 3 and not (
        liveness_status == "absent" and liveness_reason == "root_absent"
    ):
        raise ValueError("absent submission combination invalid")
    if exit_code == 4:
        healthy_inbox_refusal = (
            reason == "inbox_unavailable"
            and liveness_status == "healthy"
            and liveness_reason == "healthy"
        )
        unhealthy_refusal = (
            reason != "inbox_unavailable"
            and liveness_status == "unhealthy"
            and liveness_reason == reason
        )
        if not (healthy_inbox_refusal or unhealthy_refusal):
            raise ValueError("unhealthy submission combination invalid")
    if exit_code in (5, 6, 7, 8, 9) and not (
        liveness_status == "healthy" and liveness_reason == "healthy"
    ):
        raise ValueError("admission submission combination invalid")
    if exit_code in (5, 6, 8) and proposal_id is not None:
        raise ValueError("pre-identity refusal carried a proposal id")
    if exit_code in (7, 9) and proposal_id != expected_proposal_id:
        raise ValueError("post-identity refusal omitted the proposal id")
    if payload_version != expected_version:
        raise _LlloomIdentityMismatch
    return proposal_id


def _bounded_question(prompt: bytes) -> str:
    text = prompt.decode("utf-8")
    question = " ".join(text[:MAX_CONTEXT_QUESTION_CHARS].split())
    if not question or any(ord(char) < 32 for char in question):
        raise ValueError("prompt cannot form a bounded query")
    return question


def _context_bytes(raw: bytes) -> bytes:
    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict):
        raise ValueError("query output must be an object")
    evidence_fields = (
        "citations",
        "used_verbatim_spans",
        "used_structure_items",
    )
    if not all(isinstance(document.get(name), list) for name in evidence_fields):
        raise ValueError("query evidence fields missing")
    answer = document.get("answer")
    if type(answer) is not str:
        raise ValueError("query answer missing")
    if not any(document[name] for name in evidence_fields):
        return b""
    answer_bytes = answer.encode("utf-8")
    prefix = (
        b"\n\n--- BEGIN OPTIONAL LLLOOM CONTEXT (NON-AUTHORITATIVE) ---\n"
    )
    suffix = b"\n--- END OPTIONAL LLLOOM CONTEXT ---\n"
    if len(prefix) + len(answer_bytes) + len(suffix) > MAX_CONTEXT_BYTES:
        raise ValueError("context exceeds injection bound")
    return prefix + answer_bytes + suffix


def _update_payload(slice_id: str, proposals: tuple[str, ...]) -> dict[str, object]:
    if type(slice_id) is not str or not _SLICE_ID.fullmatch(slice_id):
        raise ValueError("slice id invalid")
    if not isinstance(proposals, tuple) or len(proposals) > MAX_UPDATE_PROPOSALS:
        raise ValueError("proposals invalid")
    if not all(
        type(item) is str
        and 0 < len(item) <= MAX_UPDATE_PROPOSAL_CHARS
        and "\x00" not in item
        for item in proposals
    ):
        raise ValueError("proposals invalid")
    payload: dict[str, object] = {
        "contract_id": "frutlups_drive.memory_update_queue",
        "contract_version": "1",
        "slice_id": slice_id,
        "proposals": list(proposals),
    }
    if len(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ) > MAX_UPDATE_BYTES:
        raise ValueError("proposals exceed byte bound")
    return payload
