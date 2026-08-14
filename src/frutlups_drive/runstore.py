"""Crash-tolerant, append-only run store (architecture contract §8.1).

The store records facts durably; it never interprets planning state or
accepts artifacts. Invariants:

- run directories and ``manifest.toml`` are created idempotently; a retry with
  the same content is a no-op, a retry with different content is a loud
  refusal that leaves the original intact;
- run and slice identifiers are distinct usable Windows path components:
  trailing-dot aliases and reserved device basenames (with or without an
  extension) are refused;
- attempt directories are unique and never reused or overwritten, and every
  request/result/transition operation first proves the supplied attempt
  directory is an existing canonical attempt of this store
  (``<root>/runs/<run-id>/slices/<slice-id>/attempt_<NNN>``);
- ``events.jsonl`` receives one canonical strict-JSON object (non-finite
  numbers refused) per LF-terminated line, appended with flush+fsync;
- write-once records publish through a unique temporary file plus an atomic
  link, so a synchronized same-content race converges idempotently, a
  conflicting race leaves one complete winner and an owned refusal, and no
  partial destination or temp residue remains;
- all text is written explicitly as UTF-8 with LF.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from frutlups_drive.contracts import AgentRunRequest, AgentRunResult

TRANSITION_STATES = (
    "planned",
    "started",
    "externally_completed",
    "collected",
    "validated",
    "closed",
)

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_ATTEMPT_NAME = re.compile(r"attempt_\d{3,}")
_BARE_TOML_KEY = re.compile(r"[A-Za-z0-9_-]+")
_BACKSLASH = chr(92)
_TOML_ESCAPES = {
    _BACKSLASH: _BACKSLASH * 2,
    '"': _BACKSLASH + '"',
    "\n": _BACKSLASH + "n",
    "\r": _BACKSLASH + "r",
    "\t": _BACKSLASH + "t",
}
_RESERVED_DEVICE_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{digit}" for digit in range(1, 10)}
    | {f"LPT{digit}" for digit in range(1, 10)}
)


class RunStoreRefusal(Exception):
    """Loud run-store refusal with a stable code and owned message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


_ATTEMPT_PROMPT_NAMES = (
    "repair_prompt.md",
    "reconciliation_prompt.md",
    "holistic_prompt.md",
    "shadow_prompt.md",
    "memory_prompt.md",
)
_VERIFICATION_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_ESCALATION_RUN_ID = re.compile(r'^run_id = "(?P<run_id>[^"]+)"$', re.MULTILINE)
_PASS_REVIEW_NAME = re.compile(r"pass_\d{3,}_holistic\.json")
RESOLUTION_MARKER_SUFFIX = ".resolved"


@dataclass(frozen=True)
class RunStoreControlResult:
    before_total_bytes: int
    total_bytes: int
    per_run_bytes: tuple[tuple[str, int], ...]
    deleted_runs: tuple[str, ...]


class RunStore:
    """File-backed run store rooted at a ``.frutlups_drive`` directory.

    ``transition_hook`` is an optional injected effect invoked after each
    durable attempt-transition write with ``(state, attempt_dir)``; crash
    tests raise inside it to simulate process death exactly at a marker.
    """

    def __init__(
        self,
        root: Path | str,
        transition_hook: "Callable[[str, Path], None] | None" = None,
        event_hook: "Callable[[str, str], None] | None" = None,
    ) -> None:
        self.root = Path(root)
        self._transition_hook = transition_hook
        self._event_hook = event_hook

    def run_dir(self, run_id: str) -> Path:
        return self.root / "runs" / _checked_identifier("run_id", run_id)

    def create_run(
        self, run_id: str, manifest: Mapping[str, str | int | float | bool]
    ) -> Path:
        run_dir = self.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        data = _manifest_bytes(manifest)
        _write_once(run_dir / "manifest.toml", data, "manifest_conflict")
        return run_dir

    def append_event(self, run_id: str, event: Mapping[str, object]) -> None:
        run_dir = self._existing_run_dir(run_id)
        if not isinstance(event, Mapping):
            raise RunStoreRefusal(
                "event_not_serializable", "event must be a JSON-serializable mapping"
            )
        line = _serialized("event_not_serializable", dict(event))
        with open(run_dir / "events.jsonl", "ab") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        if self._event_hook is not None:
            self._event_hook(str(event.get("kind", "")), run_id)

    def create_attempt(self, run_id: str, slice_id: str) -> Path:
        return self._create_attempt_in(run_id, "slices", slice_id)

    def create_shadow_attempt(self, run_id: str, slice_id: str) -> Path:
        """Mint an attempt in the evidence-only shadow namespace."""
        return self._create_attempt_in(run_id, "shadow", slice_id)

    def _create_attempt_in(
        self, run_id: str, area: str, slice_id: str
    ) -> Path:
        run_dir = self._existing_run_dir(run_id)
        slice_dir = run_dir / area / _checked_identifier("slice_id", slice_id)
        slice_dir.mkdir(parents=True, exist_ok=True)
        number = 1 + max(
            (
                int(entry.name.removeprefix("attempt_"))
                for entry in slice_dir.iterdir()
                if entry.is_dir()
                and entry.name.startswith("attempt_")
                and entry.name.removeprefix("attempt_").isdigit()
            ),
            default=0,
        )
        while True:
            attempt_dir = slice_dir / f"attempt_{number:03d}"
            try:
                attempt_dir.mkdir()
                break
            except FileExistsError:
                number += 1
        _write_atomic(attempt_dir / "transition", b"planned\n")
        if self._transition_hook is not None:
            self._transition_hook("planned", attempt_dir)
        return attempt_dir

    def write_request(self, attempt_dir: Path, request: AgentRunRequest) -> Path:
        attempt = self._owned_attempt_dir(attempt_dir)
        data = _serialized("request_not_serializable", _request_payload(request))
        path = attempt / "request.json"
        _write_once(path, data, "request_conflict")
        return path

    def write_result(self, attempt_dir: Path, result: AgentRunResult) -> Path:
        attempt = self._owned_attempt_dir(attempt_dir)
        data = _serialized("result_not_serializable", _result_payload(result))
        path = attempt / "result.json"
        _write_once(path, data, "result_conflict")
        return path

    def read_transition(self, attempt_dir: Path) -> str:
        return _read_marker(self._owned_attempt_dir(attempt_dir))

    def advance_transition(self, attempt_dir: Path, state: str) -> None:
        if state not in TRANSITION_STATES:
            raise RunStoreRefusal(
                "transition_unknown",
                "requested transition state is not part of the lifecycle",
            )
        attempt = self._owned_attempt_dir(attempt_dir)
        current = _read_marker(attempt)
        current_index = TRANSITION_STATES.index(current)
        new_index = TRANSITION_STATES.index(state)
        if new_index == current_index:
            return
        if new_index < current_index:
            raise RunStoreRefusal(
                "transition_regression",
                f"transition cannot move backwards from '{current}' to '{state}'",
            )
        _write_atomic(attempt / "transition", state.encode("utf-8") + b"\n")
        if self._transition_hook is not None:
            self._transition_hook(state, attempt)

    def run_exists(self, run_id: str) -> bool:
        return self.run_dir(run_id).is_dir()

    def next_run_id(self) -> str:
        runs_dir = self.root / "runs"
        highest = 0
        if runs_dir.is_dir():
            for entry in runs_dir.iterdir():
                name = entry.name
                if entry.is_dir() and name.startswith("run_"):
                    suffix = name.removeprefix("run_")
                    if suffix.isdigit():
                        highest = max(highest, int(suffix))
        return f"run_{highest + 1:03d}"

    def list_runs(self) -> tuple[str, ...]:
        runs_dir = self.root / "runs"
        if not runs_dir.is_dir():
            return ()
        return tuple(
            sorted(
                entry.name
                for entry in runs_dir.iterdir()
                if not entry.is_symlink()
                and not _is_junction(entry)
                and entry.is_dir()
                and _is_valid_identifier(entry.name)
            )
        )

    def run_size_bytes(self, run_id: str) -> int:
        return _tree_size(self._existing_run_dir(run_id))

    def enforce_limits(
        self,
        active_run_id: str,
        *,
        max_total_bytes: int,
        max_retained_runs: int,
    ) -> RunStoreControlResult:
        """Apply one oldest-first bounded run-store rotation transaction.

        The active run and every run named by an unresolved escalation are
        protected. An escalation is resolved only by its exact empty sibling
        marker; malformed or extra marker shapes leave the run protected. A
        complete deletion plan is proven before any run is removed; if the
        limits cannot be met using only unprotected canonical run directories,
        the store refuses without deleting anything.
        """
        if type(max_total_bytes) is not int or max_total_bytes <= 0:
            raise RunStoreRefusal(
                "run_store_policy_invalid", "maximum store bytes must be positive"
            )
        if type(max_retained_runs) is not int or max_retained_runs <= 0:
            raise RunStoreRefusal(
                "run_store_policy_invalid", "retained run count must be positive"
            )
        self._existing_run_dir(active_run_id)
        runs = list(self.list_runs())
        sizes = {run_id: self.run_size_bytes(run_id) for run_id in runs}
        runs_root = self.root / "runs"
        before_total = _tree_size(runs_root)
        protected = {active_run_id} | self._unresolved_escalation_runs(runs)
        remaining = list(runs)
        planned: list[str] = []
        planned_total = before_total
        while (
            len(remaining) > max_retained_runs
            or planned_total > max_total_bytes
        ):
            candidate = next(
                (run_id for run_id in remaining if run_id not in protected), None
            )
            if candidate is None:
                raise RunStoreRefusal(
                    "run_store_full",
                    "run-store limits cannot be met without deleting the active "
                    "run or a run named by an unresolved escalation",
                )
            remaining.remove(candidate)
            planned.append(candidate)
            planned_total -= sizes[candidate]

        for run_id in planned:
            target = self._rotation_owned_run_dir(run_id)
            try:
                shutil.rmtree(target)
            except OSError:
                raise RunStoreRefusal(
                    "run_store_rotation_failed",
                    "an allowed old run could not be removed safely",
                ) from None

        final_runs = self.list_runs()
        final_sizes = tuple(
            (run_id, self.run_size_bytes(run_id)) for run_id in final_runs
        )
        total = _tree_size(runs_root)
        if len(final_runs) > max_retained_runs or total > max_total_bytes:
            raise RunStoreRefusal(
                "run_store_full", "run-store limits remain exceeded after rotation"
            )
        return RunStoreControlResult(
            before_total_bytes=before_total,
            total_bytes=total,
            per_run_bytes=final_sizes,
            deleted_runs=tuple(planned),
        )

    def read_events(self, run_id: str) -> tuple[dict, ...]:
        run_dir = self._existing_run_dir(run_id)
        path = run_dir / "events.jsonl"
        if not path.is_file():
            return ()
        events = []
        for line in path.read_bytes().decode("utf-8").splitlines():
            events.append(json.loads(line, parse_constant=_reject_constant))
        return tuple(events)

    def list_slices(self, run_id: str) -> tuple[str, ...]:
        slices_dir = self._existing_run_dir(run_id) / "slices"
        if not slices_dir.is_dir():
            return ()
        return tuple(
            sorted(
                entry.name
                for entry in slices_dir.iterdir()
                if not entry.is_symlink()
                and not _is_junction(entry)
                and entry.is_dir()
                and _is_valid_identifier(entry.name)
            )
        )

    def list_attempts(self, run_id: str, slice_id: str) -> tuple[Path, ...]:
        return self._list_attempts_in(run_id, "slices", slice_id)

    def list_shadow_attempts(
        self, run_id: str, slice_id: str
    ) -> tuple[Path, ...]:
        return self._list_attempts_in(run_id, "shadow", slice_id)

    def list_shadow_slices(self, run_id: str) -> tuple[str, ...]:
        shadow_dir = self._existing_run_dir(run_id) / "shadow"
        if not shadow_dir.is_dir():
            return ()
        return tuple(
            sorted(
                entry.name
                for entry in shadow_dir.iterdir()
                if not entry.is_symlink()
                and not _is_junction(entry)
                and entry.is_dir()
                and _is_valid_identifier(entry.name)
            )
        )

    def _list_attempts_in(
        self, run_id: str, area: str, slice_id: str
    ) -> tuple[Path, ...]:
        slice_dir = (
            self._existing_run_dir(run_id)
            / area
            / _checked_identifier("slice_id", slice_id)
        )
        if not slice_dir.is_dir():
            return ()
        return tuple(
            sorted(
                (
                    entry
                    for entry in slice_dir.iterdir()
                    if not entry.is_symlink()
                    and not _is_junction(entry)
                    and entry.is_dir()
                    and _ATTEMPT_NAME.fullmatch(entry.name)
                ),
                key=lambda entry: entry.name,
            )
        )

    def read_request(self, attempt_dir: Path) -> dict | None:
        attempt = self._owned_attempt_dir(attempt_dir)
        path = attempt / "request.json"
        if not path.is_file():
            return None
        return json.loads(
            path.read_bytes().decode("utf-8"), parse_constant=_reject_constant
        )

    def read_result(self, attempt_dir: Path) -> dict | None:
        attempt = self._owned_attempt_dir(attempt_dir)
        path = attempt / "result.json"
        if not path.is_file():
            return None
        return json.loads(
            path.read_bytes().decode("utf-8"), parse_constant=_reject_constant
        )

    def write_provider_events(self, attempt_dir: Path, data: bytes) -> Path:
        attempt = self._owned_attempt_dir(attempt_dir)
        path = attempt / "provider_events.jsonl"
        _write_once(path, data, "provider_events_conflict")
        return path

    def write_attempt_prompt(
        self, attempt_dir: Path, filename: str, data: bytes
    ) -> Path:
        if filename not in _ATTEMPT_PROMPT_NAMES:
            raise RunStoreRefusal(
                "attempt_file_invalid",
                "attempt prompt filename is not part of the owned layout",
            )
        attempt = self._owned_attempt_dir(attempt_dir)
        path = attempt / filename
        _write_once(path, data, "attempt_prompt_conflict")
        return path

    def write_diff_manifest(
        self, attempt_dir: Path, payload: Mapping[str, object]
    ) -> Path:
        attempt = self._owned_attempt_dir(attempt_dir)
        data = _serialized("diff_manifest_not_serializable", dict(payload))
        path = attempt / "diff_manifest.json"
        _write_once(path, data, "diff_manifest_conflict")
        return path

    def write_adoption(
        self, attempt_dir: Path, payload: Mapping[str, object]
    ) -> Path:
        attempt = self._owned_attempt_dir(attempt_dir)
        data = _serialized("adoption_not_serializable", dict(payload))
        path = attempt / "adoption.json"
        _write_once(path, data, "adoption_conflict")
        return path

    def publish_shadow_report(self, attempt_dir: Path, data: bytes) -> Path:
        attempt = self._owned_attempt_dir(attempt_dir)
        relative = attempt.relative_to(self.root.resolve())
        if len(relative.parts) != 5 or relative.parts[2] != "shadow":
            raise RunStoreRefusal(
                "shadow_attempt_unowned",
                "shadow report target is not an owned shadow attempt",
            )
        path = attempt / "shadow_report.md"
        _write_once(path, data, "shadow_report_conflict")
        return path

    def write_owner_notes_snapshot(self, run_id: str, payload: Mapping[str, object]) -> Path:
        run_dir = self._existing_run_dir(run_id)
        data = _serialized("owner_notes_snapshot_not_serializable", dict(payload))
        path = run_dir / "owner_notes_snapshot.json"
        _write_once(path, data, "owner_notes_snapshot_conflict")
        return path

    def read_owner_notes_snapshot(self, run_id: str) -> dict | None:
        path = self._existing_run_dir(run_id) / "owner_notes_snapshot.json"
        if not path.is_file():
            return None
        return json.loads(
            path.read_bytes().decode("utf-8"), parse_constant=_reject_constant
        )

    def write_pass_boundary(self, run_id: str, payload: Mapping[str, object]) -> Path:
        run_dir = self._existing_run_dir(run_id)
        data = _serialized("pass_boundary_not_serializable", dict(payload))
        path = run_dir / "pass_boundary.json"
        _write_once(path, data, "pass_boundary_conflict")
        return path

    def read_pass_boundary(self, run_id: str) -> dict | None:
        path = self._existing_run_dir(run_id) / "pass_boundary.json"
        if not path.is_file():
            return None
        return json.loads(
            path.read_bytes().decode("utf-8"), parse_constant=_reject_constant
        )

    def write_pass_review(
        self, run_id: str, pass_number: int, payload: Mapping[str, object]
    ) -> Path:
        if type(pass_number) is not int or pass_number <= 0:
            raise RunStoreRefusal(
                "pass_number_invalid", "pass number must be a positive integer"
            )
        run_dir = self._existing_run_dir(run_id)
        name = f"pass_{pass_number:03d}_holistic.json"
        data = _serialized("pass_review_not_serializable", dict(payload))
        path = run_dir / name
        _write_once(path, data, "pass_review_conflict")
        return path

    def read_pass_review(self, run_id: str, pass_number: int) -> dict | None:
        if type(pass_number) is not int or pass_number <= 0:
            raise RunStoreRefusal(
                "pass_number_invalid", "pass number must be a positive integer"
            )
        path = self._existing_run_dir(run_id) / f"pass_{pass_number:03d}_holistic.json"
        if not _PASS_REVIEW_NAME.fullmatch(path.name) or not path.is_file():
            return None
        return json.loads(
            path.read_bytes().decode("utf-8"), parse_constant=_reject_constant
        )

    def write_memory_update_queue(
        self,
        run_id: str,
        slice_id: str,
        payload: Mapping[str, object],
    ) -> Path:
        """Publish one write-once proposed-update record as run evidence.

        The target is inside the run store, never inside a memory root. A
        same-content crash/resume replay is idempotent and a conflicting
        replay refuses without replacing the accepted record.
        """

        checked_slice = _checked_identifier("slice_id", slice_id)
        data = _serialized("memory_update_not_serializable", dict(payload))
        target = self._existing_run_dir(run_id) / "memory_updates"
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{checked_slice}.json"
        _write_once(path, data, "memory_update_conflict")
        return path

    def read_memory_update_queue(
        self, run_id: str, slice_id: str
    ) -> dict | None:
        checked_slice = _checked_identifier("slice_id", slice_id)
        path = (
            self._existing_run_dir(run_id)
            / "memory_updates"
            / f"{checked_slice}.json"
        )
        if not path.is_file():
            return None
        return json.loads(
            path.read_bytes().decode("utf-8"), parse_constant=_reject_constant
        )

    def read_adoption(self, attempt_dir: Path) -> dict | None:
        attempt = self._owned_attempt_dir(attempt_dir)
        path = attempt / "adoption.json"
        if not path.is_file():
            return None
        return json.loads(
            path.read_bytes().decode("utf-8"), parse_constant=_reject_constant
        )

    def verification_dir(self, attempt_dir: Path) -> Path:
        return self._owned_attempt_dir(attempt_dir) / "verification"

    def publish_verification(
        self, attempt_dir: Path, files: Mapping[str, bytes]
    ) -> Path:
        attempt = self._owned_attempt_dir(attempt_dir)
        target = attempt / "verification"
        target.mkdir(exist_ok=True)
        for name in sorted(files):
            if not _VERIFICATION_FILE_NAME.fullmatch(name):
                raise RunStoreRefusal(
                    "verification_file_invalid",
                    "verification artifact name is not part of the owned layout",
                )
            _write_once(target / name, files[name], "verification_conflict")
        return target

    def create_escalation(self, run_id: str, filename: str, data: bytes) -> Path:
        run_dir = self._existing_run_dir(run_id)
        if not _VERIFICATION_FILE_NAME.fullmatch(filename) or not filename.endswith(
            ".md"
        ):
            raise RunStoreRefusal(
                "escalation_name_invalid",
                "escalation filename is not part of the owned layout",
            )
        target = run_dir / "escalations"
        target.mkdir(exist_ok=True)
        path = target / filename
        _write_once(path, data, "escalation_conflict")
        return path

    def list_escalations(self, run_id: str) -> tuple[Path, ...]:
        target = self._existing_run_dir(run_id) / "escalations"
        if not target.is_dir():
            return ()
        return tuple(
            sorted(
                (entry for entry in target.iterdir() if entry.name.endswith(".md")),
                key=lambda entry: entry.name,
            )
        )

    def _existing_run_dir(self, run_id: str) -> Path:
        run_dir = self.run_dir(run_id)
        if not run_dir.is_dir():
            raise RunStoreRefusal("run_missing", "run directory does not exist")
        return run_dir

    def _unresolved_escalation_runs(self, runs: list[str]) -> set[str]:
        protected: set[str] = set()
        for run_id in runs:
            escalation_dir = self.run_dir(run_id) / "escalations"
            if escalation_dir.is_symlink() or _is_junction(escalation_dir):
                raise RunStoreRefusal(
                    "run_store_rotation_refused",
                    "the unresolved escalation directory is link-like",
                )
            if not escalation_dir.is_dir():
                continue
            try:
                entries = tuple(escalation_dir.iterdir())
            except OSError:
                raise RunStoreRefusal(
                    "run_store_rotation_refused",
                    "unresolved escalation references could not be read safely",
                ) from None
            escalations = tuple(entry for entry in entries if entry.name.endswith(".md"))
            marker_names = {
                escalation.name + RESOLUTION_MARKER_SUFFIX for escalation in escalations
            }
            for entry in entries:
                if entry in escalations:
                    continue
                if entry.name not in marker_names or not _valid_resolution_marker(entry):
                    protected.add(run_id)
            for entry in escalations:
                if (
                    not entry.is_file()
                    or entry.is_symlink()
                    or _is_junction(entry)
                ):
                    raise RunStoreRefusal(
                        "run_store_rotation_refused",
                        "an unresolved escalation is not a regular file",
                    )
                try:
                    with open(entry, "rb") as stream:
                        raw = stream.read(64 * 1024 + 1)
                    if len(raw) > 64 * 1024:
                        raise RunStoreRefusal(
                            "run_store_rotation_refused",
                            "an unresolved escalation exceeds its rotation bound",
                        )
                    text = raw.decode("utf-8")
                except (OSError, UnicodeDecodeError):
                    raise RunStoreRefusal(
                        "run_store_rotation_refused",
                        "an unresolved escalation could not be decoded safely",
                    ) from None
                match = _ESCALATION_RUN_ID.search(text)
                if not match or not _is_valid_identifier(match.group("run_id")):
                    raise RunStoreRefusal(
                        "run_store_rotation_refused",
                        "an unresolved escalation has no valid run reference",
                    )
                marker = entry.with_name(entry.name + RESOLUTION_MARKER_SUFFIX)
                if not _valid_resolution_marker(marker):
                    protected.add(run_id)
                    protected.add(match.group("run_id"))
        return protected

    def _rotation_owned_run_dir(self, run_id: str) -> Path:
        target = self.run_dir(run_id)
        refusal = RunStoreRefusal(
            "run_store_rotation_refused",
            "rotation target is not a canonical owned run directory",
        )
        try:
            if target.is_symlink() or _is_junction(target):
                raise refusal
            resolved = target.resolve(strict=True)
            runs_root = (self.root / "runs").resolve(strict=True)
            if resolved.parent != runs_root or resolved.name != run_id:
                raise refusal
        except OSError:
            raise refusal from None
        return target

    def _owned_attempt_dir(self, attempt_dir: Path | str) -> Path:
        """Validation boundary shared by every attempt read/write (F3, round 3).

        The observable spelling is captured with ``os.fspath`` before any
        ``Path`` construction or normalization. After resolved containment and
        exact canonical-layout validation, the spelling this store mints for
        the identified run, slice, and attempt is reconstructed from the
        store's lexical root, and the boundary string must equal it exactly.
        An alternate spelling that merely resolves to the same directory has
        no authority.
        """
        refusal = RunStoreRefusal(
            "attempt_unowned",
            "attempt directory is not the existing canonical attempt "
            "spelling minted by this run store",
        )
        try:
            raw = os.fspath(attempt_dir)
        except TypeError:
            raise refusal from None
        if not isinstance(raw, str):
            raise refusal
        try:
            root = self.root.resolve()
            resolved = Path(raw).resolve()
            relative = resolved.relative_to(root)
        except (OSError, ValueError):
            raise refusal from None
        parts = relative.parts
        if (
            len(parts) != 5
            or parts[0] != "runs"
            or parts[2] not in ("slices", "shadow")
            or not _is_valid_identifier(parts[1])
            or not _is_valid_identifier(parts[3])
            or not _ATTEMPT_NAME.fullmatch(parts[4])
            or not resolved.is_dir()
        ):
            raise refusal
        minted = self.root / "runs" / parts[1] / parts[2] / parts[3] / parts[4]
        if raw != os.fspath(minted):
            raise refusal
        return resolved


def _read_marker(attempt: Path) -> str:
    """Read the transition marker of an already-validated owned attempt."""
    path = attempt / "transition"
    if not path.is_file():
        raise RunStoreRefusal(
            "transition_missing", "attempt has no transition marker"
        )
    text = path.read_bytes().decode("utf-8")
    state = text[:-1] if text.endswith("\n") else text
    if state not in TRANSITION_STATES or text != state + "\n":
        raise RunStoreRefusal(
            "transition_unknown",
            "attempt transition marker holds an unknown state",
        )
    return state


def _is_junction(path: Path) -> bool:
    checker = getattr(os.path, "isjunction", None)
    return bool(checker is not None and checker(path))


def _valid_resolution_marker(path: Path) -> bool:
    """The complete prose-free resolver contract: exact name, empty file."""
    try:
        return (
            path.name.endswith(".md" + RESOLUTION_MARKER_SUFFIX)
            and not path.is_symlink()
            and not _is_junction(path)
            and path.is_file()
            and path.stat().st_size == 0
        )
    except OSError:
        return False


def _tree_size(root: Path) -> int:
    """Count ordinary file bytes without following links or junctions."""
    try:
        if not root.exists():
            return 0
        total = 0
        pending = [Path(root)]
        while pending:
            current = pending.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    child = Path(entry.path)
                    if entry.is_symlink() or _is_junction(child):
                        total += entry.stat(follow_symlinks=False).st_size
                    elif entry.is_dir(follow_symlinks=False):
                        pending.append(child)
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
        return total
    except OSError:
        raise RunStoreRefusal(
            "run_store_measurement_failed",
            "run-store size could not be measured safely",
        ) from None


def _is_valid_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(_IDENTIFIER_PATTERN.fullmatch(value))
        and not value.endswith(".")
        and value.split(".")[0].upper() not in _RESERVED_DEVICE_BASENAMES
    )


def _checked_identifier(field: str, value: str) -> str:
    if not _is_valid_identifier(value):
        raise RunStoreRefusal(
            "identifier_invalid",
            f"{field} must match [A-Za-z0-9][A-Za-z0-9._-]* and be a "
            "Windows-safe path component (no trailing dot, no reserved "
            "device basename)",
        )
    return value


def _reject_constant(token: str) -> None:
    raise ValueError("non-standard JSON constant in stored record")


def _serialized(refusal_code: str, payload: object) -> bytes:
    try:
        return _canonical_json_bytes(payload)
    except (TypeError, ValueError):
        raise RunStoreRefusal(
            refusal_code,
            "record contains values that cannot be serialized as strict "
            "canonical JSON",
        ) from None


def _canonical_json_bytes(payload: object) -> bytes:
    text = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return (text + "\n").encode("utf-8")


def _request_payload(request: AgentRunRequest) -> dict[str, object]:
    return {
        "run_id": request.run_id,
        "attempt_id": request.attempt_id,
        "role": request.role.value,
        "prompt_path": request.prompt_path.as_posix(),
        "prompt_sha256": request.prompt_sha256,
        "workspace": request.workspace.as_posix(),
        "base_revision": request.base_revision,
        "adapter": request.adapter,
        "model": request.model,
        "effort": request.effort,
        "workspace_access": request.workspace_access,
        "expected_artifacts": [p.as_posix() for p in request.expected_artifacts],
        "max_seconds": request.max_seconds,
        "max_cost_usd": request.max_cost_usd,
    }


def _result_payload(result: AgentRunResult) -> dict[str, object]:
    return {
        "status": result.status,
        "event_log_path": result.event_log_path.as_posix(),
        "changed_files": [p.as_posix() for p in result.changed_files],
        "produced_artifacts": [p.as_posix() for p in result.produced_artifacts],
        "exit_reason": result.exit_reason,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "cost_usd": result.cost_usd,
    }


def _manifest_bytes(manifest: Mapping[str, str | int | float | bool]) -> bytes:
    if not isinstance(manifest, Mapping):
        raise RunStoreRefusal("manifest_invalid", "manifest must be a flat mapping")
    lines = []
    for key in sorted(manifest):
        if not isinstance(key, str) or not key:
            raise RunStoreRefusal(
                "manifest_invalid", "manifest keys must be non-empty strings"
            )
        lines.append(f"{_toml_key(key)} = {_toml_value(key, manifest[key])}")
    return ("\n".join(lines) + "\n" if lines else "").encode("utf-8")


def _toml_key(key: str) -> str:
    return key if _BARE_TOML_KEY.fullmatch(key) else _toml_string(key)


def _toml_value(key: str, value: object) -> str:
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):
            raise RunStoreRefusal(
                "manifest_invalid", f"manifest key '{key}' must be a finite number"
            )
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    raise RunStoreRefusal(
        "manifest_invalid",
        f"manifest key '{key}' must hold a string, integer, float, or boolean",
    )


def _toml_string(text: str) -> str:
    parts = ['"']
    for char in text:
        if char in _TOML_ESCAPES:
            parts.append(_TOML_ESCAPES[char])
        elif ord(char) < 0x20 or char == "\x7f":
            parts.append(f"{_BACKSLASH}u{ord(char):04X}")
        else:
            parts.append(char)
    parts.append('"')
    return "".join(parts)


def _conflict_refusal(code: str, name: str) -> RunStoreRefusal:
    return RunStoreRefusal(
        code, f"refusing to overwrite existing '{name}' with different content"
    )


def _read_if_exists(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _write_once(path: Path, data: bytes, conflict_code: str) -> None:
    existing = _read_if_exists(path)
    if existing is not None:
        if existing == data:
            return
        raise _conflict_refusal(conflict_code, path.name)
    _publish_once(path, data, conflict_code)


def _publish_once(path: Path, data: bytes, conflict_code: str) -> None:
    """Collision-safe write-once publication (F5).

    The payload is written to a uniquely named temporary file, then claimed
    with ``os.link``, which atomically fails when the destination already
    exists — so the destination only ever appears fully formed. A concurrent
    same-content publisher converges idempotently; a different-content
    publisher receives the operation's owned conflict refusal.
    """
    descriptor, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(tmp_name, path)
        except FileExistsError:
            winner = path.read_bytes()
            if winner != data:
                raise _conflict_refusal(conflict_code, path.name) from None
    finally:
        os.unlink(tmp_name)


def _write_atomic(path: Path, data: bytes) -> None:
    descriptor, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
