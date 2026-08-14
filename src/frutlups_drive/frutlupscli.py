"""Released frutlups CLI binding and governed verb writer (M003-S02).

One bounded module for the two production frutlups transports:

- :func:`load_launch_binding` reads the operator's machine-local, ignored
  launch binding (absolute executable/argv prefix plus finite environment)
  and computes its identity hashes. It is loaded only when the committed
  policy selects the ``frutlups_cli`` provider, never contains credentials,
  and refuses before any store, attempt, verb write, or agent call.
- :class:`FrutlupsVerbWriter` performs the three governed artifact writes —
  ``make-coding-prompt``, ``make-review-prompt``, ``record-verdict`` — each
  as one dry-run → validate target → authorized real write without
  overwrite → post-effect fence → fresh-status transaction through the
  accepted bounded process runner. It never invokes ``orchestrator-run``,
  parses ``next_command``, uses a shell, or duplicates frutlups validators;
  drive's run store remains the sole execution journal.

Owned failures are stable and bounded: no machine-local path, environment
value, child output, or stack trace is echoed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from frutlups_drive.contracts import PlanOutcome
from frutlups_drive.dispatch.subprocess_agent import (
    checked_timeout_seconds,
    observe_command,
)
from frutlups_drive.mockverbs import ORCHESTRATOR_VERBS, VerbAuthorityDenied
from frutlups_drive.planstate import (
    PlanningState,
    _is_valid_artifact_reference,
)
from frutlups_drive.policy import _secret_shaped
from frutlups_drive.verifier import MAX_STREAM_CAPTURE_BYTES, ProcessRunner
from frutlups_drive.workspace import authorize_workspace_writes

BINDING_SCHEMA_VERSION = "frutlups_drive_binding_v1"
BINDING_MAX_BYTES = 65_536
BYTECODE_HYGIENE_ENV = ("PYTHONDONTWRITEBYTECODE", "1")
PENDING_VERB_SCHEMA_VERSION = 1
PENDING_VERB_MAX_BYTES = 16 * 1024 * 1024
PENDING_VERB_MAX_MEMBERS = 100_000
_ORDINARY_DIRECTORY_DIGEST = hashlib.sha256(b"ordinary-directory\0").hexdigest()

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TOOL_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+=-]{0,63}")
_DECLARED_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+=-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_IDENTITY_MANIFEST_FIELDS = (
    ("policy_hash", "policy_hash"),
    ("binding_sha256", "frutlups_binding_sha256"),
    ("executable_sha256", "frutlups_executable_sha256"),
    ("tool_identity", "frutlups_tool_identity"),
    ("layout_sha256", "frutlups_layout_sha256"),
    ("contract_id", "frutlups_contract_id"),
    ("contract_version", "frutlups_contract_version"),
    ("package_identity", "frutlups_package_identity"),
)


def _ordinary_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode) and not path.is_symlink()
    except OSError:
        return False


def _safe_text(value: object, *, allow_empty: bool = False, limit: int = 4096) -> bool:
    return (
        type(value) is str
        and (allow_empty or bool(value))
        and len(value) <= limit
        and all(ord(char) >= 32 and ord(char) != 127 for char in value)
    )


class FrutlupsBindingError(Exception):
    """Fail-closed launch-binding refusal with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class FrutlupsVerbError(Exception):
    """One owned bounded verb-transaction failure with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class FrutlupsCorrectiveRound(Exception):
    """The dry run typed the current report as a corrective round.

    Released acceptance semantics: a verdict record exists only for a
    ``pass`` report — recording a non-pass report creates typed
    contradictory durable state. When the sanctioned record-verdict dry run
    reports frutlups's own typed ``next_action.kind == "recode_same_slice"``,
    the record is deliberately not written and the caller routes the
    corrective coding round instead. This consumes only typed dry-run
    facts, never prose.
    """


_RECORD_KINDS_PROCEED = frozenset({"advance_to_next_slice", "milestone_complete"})
_RECORD_KINDS_KNOWN = _RECORD_KINDS_PROCEED | frozenset(
    {"recode_same_slice", "unblock_same_slice", "human_override_required", "invalid"}
)

# Closed per-verb target authority: each governed verb may propose a target
# only inside its template-v3 convention directory. The frutlups dry run is
# never its own authority — a proposed target outside the invoked verb's
# directory (governance state, roadmaps, arbitrary workspace files) is
# refused before the real write regardless of what the tool proposed.
_VERB_TARGET_PREFIXES = {
    "make-coding-prompt": ("prompts/for_coding_agent/",),
    "make-review-prompt": ("prompts/for_review_agent/",),
    "record-verdict": ("05_governance/reviews/",),
}

# The loop step each verb answers; used by the post-transaction fresh-state
# fence to detect a producer that cannot see its own governed product.
_VERB_STEPS = {
    "make-coding-prompt": "make_coding_prompt",
    "make-review-prompt": "make_review_prompt",
    "record-verdict": "record_verdict",
}


@dataclass(frozen=True)
class FrutlupsLaunchBinding:
    """Machine-local launch facts plus their identity hashes.

    ``argv_prefix`` is the exact absolute interpreter/argv prefix (for
    example ``(python, -m, frutlups)``); ``env`` is the finite explicit
    child environment. ``binding_sha256`` is the hash of the binding file
    bytes and ``executable_sha256`` the hash of the executable file bytes,
    so the run manifest can pin exact tool identity without recording any
    machine-local path or environment value.
    """

    argv_prefix: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    tool_identity: str
    binding_sha256: str
    executable_sha256: str


@dataclass(frozen=True)
class FrutlupsLaunchIdentity:
    """Finite durable identity for every released frutlups subprocess."""

    policy_hash: str
    binding_sha256: str
    executable_sha256: str
    tool_identity: str
    layout_sha256: str
    contract_id: str
    contract_version: str
    package_identity: str

    def __post_init__(self) -> None:
        for digest in (
            self.policy_hash,
            self.binding_sha256,
            self.executable_sha256,
            self.layout_sha256,
        ):
            if type(digest) is not str or not _SHA256.fullmatch(digest):
                raise ValueError("launch identity digest is invalid")
        if not _TOOL_IDENTITY.fullmatch(self.tool_identity):
            raise ValueError("launch tool identity is invalid")
        for value in (
            self.contract_id,
            self.contract_version,
            self.package_identity,
        ):
            if (
                type(value) is not str
                or not _DECLARED_IDENTITY.fullmatch(value)
                or _secret_shaped(value)
            ):
                raise ValueError("launch identity declaration is invalid")
        if _secret_shaped(self.tool_identity):
            raise ValueError("launch tool identity is secret-shaped")

    def manifest_facts(self) -> dict[str, str]:
        return {
            manifest_name: getattr(self, field_name)
            for field_name, manifest_name in _IDENTITY_MANIFEST_FIELDS
        }

    @classmethod
    def manifest_names(cls) -> frozenset[str]:
        return frozenset(
            manifest_name for _, manifest_name in _IDENTITY_MANIFEST_FIELDS
        )

    @classmethod
    def from_manifest(cls, facts: object) -> FrutlupsLaunchIdentity:
        manifest_names = cls.manifest_names()
        if type(facts) is not dict or set(facts) != manifest_names:
            raise ValueError("launch identity fields are invalid")
        if not all(type(facts[key]) is str for key in manifest_names):
            raise ValueError("launch identity values are invalid")
        return cls(
            **{
                field_name: facts[manifest_name]
                for field_name, manifest_name in _IDENTITY_MANIFEST_FIELDS
            }
        )


def load_launch_binding(path: Path | str) -> FrutlupsLaunchBinding:
    """Load and validate the local launch binding. Fail-closed, no echo."""

    binding_path = Path(path)
    if not _ordinary_file(binding_path):
        raise FrutlupsBindingError(
            "binding_missing",
            "the frutlups launch binding file does not exist; the "
            "frutlups_cli provider requires a machine-local binding under "
            "ignored local state",
        )
    raw = binding_path.read_bytes()
    if len(raw) > BINDING_MAX_BYTES:
        raise FrutlupsBindingError(
            "binding_oversized", "the frutlups launch binding exceeds 65536 bytes"
        )
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise FrutlupsBindingError(
            "binding_malformed", "the launch binding is not valid TOML"
        ) from None
    if document.get("schema_version") != BINDING_SCHEMA_VERSION:
        raise FrutlupsBindingError(
            "binding_schema_unknown",
            "the launch binding schema_version is not implemented; "
            f"accepted: {BINDING_SCHEMA_VERSION}",
        )
    launch = document.get("launch")
    if not isinstance(launch, dict):
        raise FrutlupsBindingError(
            "binding_field_invalid", "the binding must declare a [launch] table"
        )
    argv_prefix = launch.get("argv_prefix")
    if (
        not isinstance(argv_prefix, list)
        or not argv_prefix
        or len(argv_prefix) > 64
        or not all(_safe_text(part) for part in argv_prefix)
    ):
        raise FrutlupsBindingError(
            "binding_field_invalid",
            "launch.argv_prefix must be a non-empty list of strings",
        )
    executable = Path(argv_prefix[0])
    if not executable.is_absolute():
        raise FrutlupsBindingError(
            "binding_field_invalid",
            "launch.argv_prefix[0] must be an absolute executable path",
        )
    if not _ordinary_file(executable):
        raise FrutlupsBindingError(
            "binding_executable_missing",
            "the bound frutlups executable does not exist",
        )
    tool_identity = launch.get("tool_identity", "")
    if (
        type(tool_identity) is not str
        or not _TOOL_IDENTITY.fullmatch(tool_identity)
        or _secret_shaped(tool_identity)
    ):
        raise FrutlupsBindingError(
            "binding_field_invalid",
            "launch.tool_identity must match the bounded safe identity grammar",
        )
    env_table = document.get("env", {})
    if not isinstance(env_table, dict):
        raise FrutlupsBindingError(
            "binding_field_invalid", "the [env] table must be a table"
        )
    env_pairs: list[tuple[str, str]] = []
    for key, value in env_table.items():
        name = key
        if (
            type(name) is not str
            or not _ENV_NAME.fullmatch(name)
            or type(value) is not str
            or not _safe_text(value, allow_empty=True)
        ):
            raise FrutlupsBindingError(
                "binding_field_invalid",
                "env entries must map plain variable names to strings",
            )
        if _secret_shaped(name) and value:
            raise FrutlupsBindingError(
                "binding_secret_shaped",
                f"env entry '{name}' is secret-shaped and non-empty; "
                "credentials never appear in the launch binding",
            )
        env_pairs.append((name, value))
    env_by_name = dict(env_pairs)
    env_by_name[BYTECODE_HYGIENE_ENV[0]] = BYTECODE_HYGIENE_ENV[1]
    return FrutlupsLaunchBinding(
        argv_prefix=tuple(argv_prefix),
        env=tuple(sorted(env_by_name.items())),
        tool_identity=tool_identity,
        binding_sha256=hashlib.sha256(raw).hexdigest(),
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )


def binding_manifest_facts(
    binding: FrutlupsLaunchBinding,
    *,
    layout_path: Path | str,
    contract_id: str,
    contract_version: str,
    package_identity: str,
    policy_hash: str,
) -> dict[str, str]:
    """Manifest identity snapshot: hashes and declared identities only.

    Never includes an absolute path, argv text, or environment value.
    """

    return build_launch_identity(
        binding,
        layout_path=layout_path,
        contract_id=contract_id,
        contract_version=contract_version,
        package_identity=package_identity,
        policy_hash=policy_hash,
    ).manifest_facts()


def build_launch_identity(
    binding: FrutlupsLaunchBinding,
    *,
    layout_path: Path | str,
    contract_id: str,
    contract_version: str,
    package_identity: str,
    policy_hash: str,
) -> FrutlupsLaunchIdentity:
    layout_file = Path(layout_path)
    if not _ordinary_file(layout_file):
        raise FrutlupsBindingError(
            "layout_missing",
            "the committed frutlups layout file is absent or not an ordinary file",
        )
    try:
        return FrutlupsLaunchIdentity(
            policy_hash=policy_hash,
            binding_sha256=binding.binding_sha256,
            executable_sha256=binding.executable_sha256,
            tool_identity=binding.tool_identity,
            layout_sha256=hashlib.sha256(layout_file.read_bytes()).hexdigest(),
            contract_id=contract_id,
            contract_version=contract_version,
            package_identity=package_identity,
        )
    except ValueError:
        raise FrutlupsBindingError(
            "launch_identity_invalid",
            "the declared frutlups launch identity is not finite and safe",
        ) from None


_TRANSACTION_STAGES = (
    "selected",
    "dry_run",
    "validated",
    "written",
    "fenced",
    "reread",
    "journaled",
)

# Write-ahead verb-intent marker inside the run store. At most one verb
# transaction is in flight, so one pending file suffices. It is written
# after target authorization and cleared by the supervisor only after the
# journal event exists, so a crash between real effect and journaling is
# reconciled on resume without a duplicate write or a second journal.
PENDING_VERB_FILENAME = "pending_verb.json"


@dataclass(frozen=True)
class _PendingVerb:
    verb: str
    target: str
    declared_path: str | None
    review_report: str | None
    workspace_before: dict[str, str]
    launch_identity: FrutlupsLaunchIdentity

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": PENDING_VERB_SCHEMA_VERSION,
            "verb": self.verb,
            "target": self.target,
            "declared_path": self.declared_path,
            "review_report": self.review_report,
            "target_preexisted": False,
            "workspace_before": dict(sorted(self.workspace_before.items())),
            "launch_identity": self.launch_identity.manifest_facts(),
        }


class FrutlupsVerbWriter:
    """Governed three-verb writer over the released frutlups CLI.

    ``status_reader`` supplies the fresh-status observation that closes each
    transaction; only that fresh state proves progress. ``snapshot`` is the
    injected project snapshot function used for the post-effect fence.
    ``transaction_hook`` is a test-only crash injection point invoked as
    ``hook(stage, verb)`` after each durable transaction stage.
    """

    def __init__(
        self,
        *,
        project_root: Path,
        binding: FrutlupsLaunchBinding,
        runner: ProcessRunner,
        capture_root: Path,
        store_root: Path,
        timeout_seconds: float = 120.0,
        max_stream_bytes: int = MAX_STREAM_CAPTURE_BYTES,
        status_reader: Callable[[], PlanningState] | None = None,
        snapshot: Callable[[], dict[str, str]] | None = None,
        launch_identity: FrutlupsLaunchIdentity | None = None,
        identity_reader: Callable[[], FrutlupsLaunchIdentity] | None = None,
        transaction_hook: Callable[[str, str], None] | None = None,
        intent_path: Path | None = None,
    ) -> None:
        self._root = Path(project_root)
        self._binding = binding
        self._runner = runner
        self._capture_root = Path(capture_root)
        self._store_root = Path(store_root)
        self._timeout = checked_timeout_seconds(timeout_seconds)
        if type(max_stream_bytes) is not int or not (
            1 <= max_stream_bytes <= MAX_STREAM_CAPTURE_BYTES
        ):
            raise ValueError(
                "max_stream_bytes must be an integer between 1 and "
                f"{MAX_STREAM_CAPTURE_BYTES}"
            )
        self._max_stream = max_stream_bytes
        self._status_reader = status_reader
        self._snapshot = snapshot
        if launch_identity is None:
            raise ValueError("launch_identity is required for governed verbs")
        if snapshot is None:
            raise ValueError("snapshot is required for governed verbs")
        self._identity = launch_identity
        self._identity_reader = identity_reader or (lambda: launch_identity)
        self._hook = transaction_hook
        self._intent_path = Path(intent_path) if intent_path is not None else None

    def clear_intent(self) -> None:
        """Remove the pending-verb marker once the journal event exists."""

        if self._intent_path is not None:
            self._intent_path.unlink(missing_ok=True)

    def mark_journaled(self, verb: str) -> None:
        """Crash-injection boundary after the sole journal publication."""

        self._stage("journaled", verb)

    # ------------------------------------------------------------- helpers

    def _stage(self, stage: str, verb: str) -> None:
        if self._hook is not None:
            self._hook(stage, verb)

    def _check_identity(self) -> None:
        try:
            current = self._identity_reader()
        except Exception:
            raise FrutlupsVerbError(
                "verb_identity_unavailable",
                "the current frutlups launch identity could not be validated",
            ) from None
        if type(current) is not FrutlupsLaunchIdentity or current != self._identity:
            raise FrutlupsVerbError(
                "verb_identity_changed",
                "the policy, binding, executable, layout, or declared tool "
                "identity changed during the governed transaction",
            )

    def _workspace_snapshot(self) -> dict[str, str]:
        try:
            snapshot = self._snapshot()
        except Exception:
            raise FrutlupsVerbError(
                "verb_snapshot_failed", "the workspace manifest could not be read"
            ) from None
        if type(snapshot) is not dict or len(snapshot) > PENDING_VERB_MAX_MEMBERS:
            raise FrutlupsVerbError(
                "verb_snapshot_invalid", "the workspace manifest is not bounded"
            )
        checked: dict[str, str] = {}
        for path, digest in snapshot.items():
            if (
                type(path) is not str
                or not _is_valid_artifact_reference(path)
                or type(digest) is not str
                or not _SHA256.fullmatch(digest)
            ):
                raise FrutlupsVerbError(
                    "verb_snapshot_invalid",
                    "the workspace manifest contains an invalid member",
                )
            checked[path] = digest
        return checked

    @staticmethod
    def _changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
        return sorted(
            path
            for path in set(before) | set(after)
            if before.get(path) != after.get(path)
        )

    def _write_intent(self, pending: _PendingVerb) -> None:
        if self._intent_path is None:
            raise FrutlupsVerbError(
                "verb_intent_unavailable",
                "the governed verb has no durable write-ahead location",
            )
        try:
            raw = (
                json.dumps(
                    pending.payload(),
                    allow_nan=False,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
        except (TypeError, ValueError):
            raise FrutlupsVerbError(
                "verb_intent_invalid", "the pending verb witness is not serializable"
            ) from None
        if len(raw) > PENDING_VERB_MAX_BYTES:
            raise FrutlupsVerbError(
                "verb_intent_oversized", "the pending verb witness exceeds its bound"
            )
        self._intent_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self._intent_path.name}.",
            suffix=".tmp",
            dir=self._intent_path.parent,
        )
        temp_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self._intent_path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _read_intent(self) -> _PendingVerb | None:
        if self._intent_path is None:
            return None
        try:
            self._intent_path.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            raise FrutlupsVerbError(
                "verb_intent_invalid", "the pending verb witness cannot be inspected"
            ) from None
        if not _ordinary_file(self._intent_path):
            raise FrutlupsVerbError(
                "verb_intent_invalid", "the pending verb witness is not an ordinary file"
            )
        raw = self._intent_path.read_bytes()
        if not raw or len(raw) > PENDING_VERB_MAX_BYTES:
            raise FrutlupsVerbError(
                "verb_intent_invalid", "the pending verb witness is empty or oversized"
            )

        def reject_constant(value: str) -> None:
            raise ValueError(value)

        try:
            payload = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
            canonical = (
                json.dumps(
                    payload,
                    allow_nan=False,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
        except (UnicodeDecodeError, ValueError, TypeError):
            raise FrutlupsVerbError(
                "verb_intent_invalid", "the pending verb witness is not strict JSON"
            ) from None
        expected = {
            "schema_version",
            "verb",
            "target",
            "declared_path",
            "review_report",
            "target_preexisted",
            "workspace_before",
            "launch_identity",
        }
        if type(payload) is not dict or set(payload) != expected or raw != canonical:
            raise FrutlupsVerbError(
                "verb_intent_invalid", "the pending verb witness is not canonical"
            )
        verb = payload["verb"]
        target = payload["target"]
        declared_path = payload["declared_path"]
        review_report = payload["review_report"]
        if (
            payload["schema_version"] != PENDING_VERB_SCHEMA_VERSION
            or type(payload["schema_version"]) is not int
            or type(verb) is not str
            or verb not in ORCHESTRATOR_VERBS
            or type(target) is not str
            or not _is_valid_artifact_reference(target)
            or (declared_path is not None and type(declared_path) is not str)
            or (review_report is not None and type(review_report) is not str)
            or payload["target_preexisted"] is not False
        ):
            raise FrutlupsVerbError(
                "verb_intent_invalid", "the pending verb witness fields are invalid"
            )
        if declared_path is not None and (
            not _is_valid_artifact_reference(declared_path)
            or declared_path != target
        ):
            raise FrutlupsVerbError(
                "verb_intent_invalid", "the pending declared target is invalid"
            )
        if review_report is not None and not _is_valid_artifact_reference(review_report):
            raise FrutlupsVerbError(
                "verb_intent_invalid", "the pending review reference is invalid"
            )
        workspace = payload["workspace_before"]
        if type(workspace) is not dict or len(workspace) > PENDING_VERB_MAX_MEMBERS:
            raise FrutlupsVerbError(
                "verb_intent_invalid", "the pending workspace manifest is invalid"
            )
        for path, digest in workspace.items():
            if (
                type(path) is not str
                or not _is_valid_artifact_reference(path)
                or type(digest) is not str
                or not _SHA256.fullmatch(digest)
            ):
                raise FrutlupsVerbError(
                    "verb_intent_invalid",
                    "the pending workspace manifest contains an invalid member",
                )
        if target in workspace or (
            verb == "record-verdict" and review_report is None
        ) or (verb != "record-verdict" and review_report is not None):
            raise FrutlupsVerbError(
                "verb_intent_invalid",
                "the pending witness contradicts its declared pre-effect state",
            )
        try:
            identity = FrutlupsLaunchIdentity.from_manifest(
                payload["launch_identity"]
            )
        except ValueError:
            raise FrutlupsVerbError(
                "verb_intent_invalid", "the pending launch identity is invalid"
            ) from None
        return _PendingVerb(
            verb=verb,
            target=target,
            declared_path=declared_path,
            review_report=review_report,
            workspace_before=dict(workspace),
            launch_identity=identity,
        )

    def _authorize_pending(self, pending: _PendingVerb) -> None:
        if pending.launch_identity != self._identity:
            raise FrutlupsVerbError(
                "verb_identity_changed",
                "the pending verb launch identity does not match this run",
            )
        violations = authorize_workspace_writes(
            self._root,
            self._store_root,
            (pending.target,),
            workspace_access="workspace_write",
            allowed_prefixes=_VERB_TARGET_PREFIXES[pending.verb],
        )
        if violations:
            raise VerbAuthorityDenied(violations)

    def _validate_workspace_effect(self, pending: _PendingVerb) -> bool:
        after = self._workspace_snapshot()
        changed = self._changed_paths(pending.workspace_before, after)
        target = self._root / pending.target
        try:
            target.lstat()
            target_exists = True
        except OSError:
            target_exists = False
        if not target_exists:
            if changed:
                raise FrutlupsVerbError(
                    "verb_extra_effect",
                    "the governed verb changed paths without producing its target",
                )
            return False
        if not _ordinary_file(target):
            raise FrutlupsVerbError(
                "verb_effect_invalid", "the governed target is not an ordinary file"
            )
        target_parts = pending.target.split("/")
        target_ancestors = {
            "/".join(target_parts[:depth])
            for depth in range(1, len(target_parts))
        }
        allowed_effect = pending.target in changed and all(
            path == pending.target
            or (
                path in target_ancestors
                and path not in pending.workspace_before
                and after.get(path) == _ORDINARY_DIRECTORY_DIGEST
            )
            for path in changed
        )
        if not allowed_effect:
            raise FrutlupsVerbError(
                "verb_extra_effect",
                "the governed verb changed paths beyond its one authorized target",
            )
        return True

    def _validate_post_state(self, pending: _PendingVerb) -> None:
        if self._status_reader is None:
            return
        try:
            fresh = self._status_reader()
        except Exception:
            raise FrutlupsVerbError(
                "verb_post_state_unavailable",
                "fresh planning state could not confirm the governed write",
            ) from None
        if type(fresh) is not PlanningState or fresh.outcome is PlanOutcome.INVALID:
            raise FrutlupsVerbError(
                "verb_post_state_invalid",
                "fresh planning state is invalid and cannot certify the write",
            )
        fresh_step = fresh.step.value if fresh.step is not None else ""
        same_step = _VERB_STEPS[pending.verb] == fresh_step
        if pending.verb == "record-verdict":
            contradictory = (
                same_step
                and fresh.artifacts.review_report == pending.review_report
            )
        else:
            contradictory = same_step
        if contradictory:
            raise FrutlupsVerbError(
                "verb_post_state_contradictory",
                "the fresh state after the governed verb does not reflect its write",
            )

    def reconcile_pending(self) -> tuple[str, Path] | None:
        """Validate a surviving witness with normal-path transaction rules."""

        pending = self._read_intent()
        if pending is None:
            return None
        self._authorize_pending(pending)
        self._check_identity()
        effect_present = self._validate_workspace_effect(pending)
        self._check_identity()
        if not effect_present:
            # Authorized witness, exact identity, and byte-identical workspace:
            # no real effect happened, so the ordinary transaction may retry.
            self.clear_intent()
            return None
        self._validate_post_state(pending)
        self._check_identity()
        return pending.verb, self._root / pending.target

    _CAPTURE_INDEX = re.compile(r"verb_(\d{1,9})_")

    def _next_capture_index(self) -> int:
        highest = 0
        if self._capture_root.is_dir():
            for entry in self._capture_root.iterdir():
                match = self._CAPTURE_INDEX.match(entry.name)
                if match and len(match.group(1)) <= 9:
                    highest = max(highest, int(match.group(1)))
        return highest + 1

    def _invoke_cli(self, argv: tuple[str, ...], label: str) -> dict:
        # The exact current policy/binding/executable/layout identity is the
        # authority for every individual subprocess, not merely run creation.
        self._check_identity()
        self._capture_root.mkdir(parents=True, exist_ok=True)
        index = self._next_capture_index()
        stdout_path = self._capture_root / f"verb_{index:03d}_{label}_stdout.txt"
        stderr_path = self._capture_root / f"verb_{index:03d}_{label}_stderr.txt"
        observation = observe_command(
            argv,
            self._root,
            dict(self._binding.env),
            self._timeout,
            self._runner,
            stdout_path,
            stderr_path,
            max_stream_bytes=self._max_stream,
        )
        failures = {
            "missing_executable": "verb_executable_missing",
            "runner_failure": "verb_transport_failed",
            "timeout": "verb_timeout",
            "overflow": "verb_output_overflow",
            "no_status": "verb_status_unavailable",
        }
        if observation.kind in failures:
            raise FrutlupsVerbError(
                failures[observation.kind],
                f"the {label} verb invocation did not complete cleanly",
            )
        if observation.exit_code != 0:
            raise FrutlupsVerbError(
                "verb_exit_nonzero",
                f"the {label} verb invocation exited nonzero",
            )
        try:
            payload = json.loads(stdout_path.read_bytes().decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise FrutlupsVerbError(
                "verb_output_malformed",
                f"the {label} verb output is not one strict JSON document",
            ) from None
        if not isinstance(payload, dict):
            raise FrutlupsVerbError(
                "verb_output_malformed",
                f"the {label} verb output is not a JSON object",
            )
        return payload

    @staticmethod
    def _payload_errors(payload: dict) -> bool:
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            return True
        return payload.get("valid") is not True

    def _proposed_target(self, verb: str, payload: dict) -> str:
        if verb == "record-verdict":
            target = payload.get("target_path")
        else:
            preview = payload.get("preview")
            target = preview.get("target_path") if isinstance(preview, dict) else None
        if not isinstance(target, str) or not target:
            raise FrutlupsVerbError(
                "verb_target_missing",
                f"the {verb} plan names no proposed target",
            )
        normalized = target.replace("\\", "/")
        if not _is_valid_artifact_reference(normalized):
            raise FrutlupsVerbError(
                "verb_target_invalid",
                f"the {verb} proposed target is not a repo-relative POSIX path",
            )
        return normalized

    # ------------------------------------------------------------ transact

    def invoke(
        self,
        verb: str,
        declared_path: str | None,
        review_report: str | None = None,
    ) -> Path:
        """Run one governed dry-run/write/fence/re-read verb transaction."""

        if verb not in ORCHESTRATOR_VERBS:
            raise FrutlupsVerbError(
                "verb_not_allowed",
                "only the three governed artifact verbs are sanctioned",
            )
        self._check_identity()
        before = self._workspace_snapshot()
        base_argv = list(self._binding.argv_prefix) + [verb, "."]
        if verb == "record-verdict":
            if not review_report or not _is_valid_artifact_reference(
                review_report
            ):
                raise FrutlupsVerbError(
                    "verb_report_reference_invalid",
                    "record-verdict requires a validated repo-relative "
                    "review-report reference from the fresh state",
                )
            base_argv += ["--review-report", review_report]
        self._stage("selected", verb)

        try:
            dry_payload = self._invoke_cli(
                tuple(base_argv + ["--dry-run", "--json"]), "dry"
            )
        except FrutlupsVerbError:
            if self._workspace_snapshot() != before:
                raise FrutlupsVerbError(
                    "verb_dry_run_effect",
                    "the governed dry run changed the workspace",
                ) from None
            self._check_identity()
            raise
        if self._workspace_snapshot() != before:
            raise FrutlupsVerbError(
                "verb_dry_run_effect",
                "the governed dry run changed the workspace",
            )
        self._check_identity()
        if self._payload_errors(dry_payload):
            raise FrutlupsVerbError(
                "verb_dry_run_refused",
                f"the {verb} dry run reported a typed refusal",
            )
        if verb == "record-verdict":
            next_action = dry_payload.get("next_action")
            kind = (
                next_action.get("kind") if isinstance(next_action, dict) else None
            )
            if kind not in _RECORD_KINDS_KNOWN:
                raise FrutlupsVerbError(
                    "verb_next_action_unknown",
                    "the record-verdict dry run names a next-action kind "
                    "this consumer does not implement",
                )
            if kind == "recode_same_slice":
                # Released acceptance model: records exist only for pass.
                raise FrutlupsCorrectiveRound()
            if kind not in _RECORD_KINDS_PROCEED:
                raise FrutlupsVerbError(
                    "verb_record_not_recordable",
                    "the record-verdict dry run typed a non-recordable "
                    "verdict; drive stops instead of writing contradictory "
                    "state",
                )
        proposed = self._proposed_target(verb, dry_payload)
        self._stage("dry_run", verb)

        if declared_path is not None and proposed != declared_path:
            raise FrutlupsVerbError(
                "verb_target_mismatch",
                f"the {verb} dry-run target does not equal the fresh "
                "state's declared artifact",
            )
        violations = authorize_workspace_writes(
            self._root,
            self._store_root,
            (proposed,),
            workspace_access="workspace_write",
            allowed_prefixes=_VERB_TARGET_PREFIXES[verb],
        )
        if violations:
            raise VerbAuthorityDenied(violations)
        target_abs = self._root / proposed
        try:
            target_abs.lstat()
            target_exists = True
        except OSError:
            target_exists = False
        if target_exists:
            raise FrutlupsVerbError(
                "verb_target_preexists",
                f"the {verb} target already exists; a governed write never "
                "overwrites",
            )
        pending = _PendingVerb(
            verb=verb,
            target=proposed,
            declared_path=declared_path,
            review_report=review_report,
            workspace_before=before,
            launch_identity=self._identity,
        )
        self._check_identity()
        self._write_intent(pending)
        self._stage("validated", verb)

        try:
            real_payload = self._invoke_cli(tuple(base_argv + ["--json"]), "real")
        except FrutlupsVerbError:
            # Even a nonzero/timeout/malformed real invocation crossed the
            # effect boundary. Apply the same manifest authority before the
            # owned transport refusal is allowed to escape.
            self._validate_workspace_effect(pending)
            self._check_identity()
            raise
        effect_present = self._validate_workspace_effect(pending)
        self._check_identity()
        write_result = real_payload.get("write_result")
        if self._payload_errors(real_payload) or not isinstance(
            write_result, dict
        ):
            raise FrutlupsVerbError(
                "verb_write_refused",
                f"the {verb} write reported a typed refusal or no "
                "write_result",
            )
        if write_result.get("wrote") is not True or write_result.get(
            "overwrote"
        ) is not False:
            raise FrutlupsVerbError(
                "verb_write_facts_invalid",
                f"the {verb} write_result facts do not prove one fresh write",
            )
        if not effect_present or not _ordinary_file(target_abs):
            raise FrutlupsVerbError(
                "verb_effect_missing",
                f"the {verb} write reported success but the target does not "
                "exist",
            )
        self._stage("written", verb)

        if not self._validate_workspace_effect(pending):
            raise FrutlupsVerbError(
                "verb_effect_missing",
                f"the {verb} write reported success but produced no target",
            )
        self._check_identity()
        self._stage("fenced", verb)

        self._validate_post_state(pending)
        self._check_identity()
        self._stage("reread", verb)
        return target_abs
