"""Single-target guarded writer for architect roadmap reconciliation.

The architect supplies complete proposed roadmap bytes in an attempt-owned
staging workspace.  This module alone may publish them into the driven
project, after proving the bounded N04 path and content contract over the
whole proposal and rechecking the preimage immediately before one atomic
replacement.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path


MAX_ROADMAP_BYTES = 1_048_576
_MILESTONE = re.compile(r"^### (?P<id>M\d{3}):")
_SLICE = re.compile(r"^- (?P<id>M\d{3}-S\d{2}):")
_HEADING = re.compile(r"^\s*#{1,6}\s")
_LABELS = (
    "Status:",
    "Disposition:",
    "Slices:",
    "Implementation package:",
    "Objective:",
    "Expected artifacts:",
    "Active workspaces:",
    "Non-goals:",
    "Verification/evidence:",
    "Review strictness:",
    "Likely coding prompt:",
    "Done when:",
    "Opening gates:",
)
_EDITABLE = frozenset(
    {
        "Implementation package:",
        "Objective:",
        "Expected artifacts:",
        "Active workspaces:",
        "Non-goals:",
        "Verification/evidence:",
        "Review strictness:",
        "Likely coding prompt:",
        "Done when:",
        "Opening gates:",
    }
)
_FORBIDDEN_CHANGED_TEXT = re.compile(
    r"(?:\bdestination\b|\bruled[ -]out\b|\baccepted\b|\bcompleted\b|"
    r"\bclosed\b|\bclosure\b|\bverdict\b|\boverride\b|\bhistory\b|"
    r"\bregisters?\b|\bowner[ -]notes?\b|\breviews?\b|\bprompts?\b|"
    r"\bstate files?\b|PROJECT_STATE\.md|05_governance/(?:reviews|"
    r"human_owner_notes|current)/|prompts/)",
    re.IGNORECASE,
)


class ReconciliationRefusal(Exception):
    """Bounded fail-closed proposal refusal."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ReconciliationResult:
    target: str
    slice_id: str
    before_sha256: str
    after_sha256: str


@dataclass(frozen=True)
class _Section:
    milestone_id: str
    lines: tuple[str, ...]


class ReconciliationWriter:
    """Discover and atomically update the one active roadmap."""

    def __init__(self, project_root: Path) -> None:
        self._root = Path(project_root)

    def target_path(self) -> Path:
        directory = self._root / "03_experiments"
        try:
            candidates = tuple(sorted(directory.glob("*active_roadmap*.md")))
        except OSError:
            raise ReconciliationRefusal("active_roadmap_unreadable") from None
        if len(candidates) != 1:
            raise ReconciliationRefusal("active_roadmap_ambiguous")
        target = candidates[0]
        if target.is_symlink() or _is_junction(target) or not target.is_file():
            raise ReconciliationRefusal("active_roadmap_not_regular")
        try:
            if target.resolve().parent != directory.resolve():
                raise ReconciliationRefusal("active_roadmap_outside_authority")
        except OSError:
            raise ReconciliationRefusal("active_roadmap_unreadable") from None
        return target

    def target_slice_id(self) -> str:
        current = _read_bounded(self.target_path())
        _, sections, _ = _parse_roadmap(current)
        active = [section for section in sections if _status(section) == "active"]
        if len(active) != 1:
            raise ReconciliationRefusal("active_milestone_ambiguous")
        slices = _slice_ids(active[0])
        if len(slices) != 1:
            raise ReconciliationRefusal("active_slice_ambiguous")
        return slices[0]

    def apply(self, proposal_path: Path) -> ReconciliationResult:
        target = self.target_path()
        current = _read_bounded(target)
        proposed = _read_bounded(Path(proposal_path))
        before_hash = hashlib.sha256(current).hexdigest()
        after_hash = hashlib.sha256(proposed).hexdigest()
        if before_hash == after_hash:
            raise ReconciliationRefusal("proposal_has_no_change")
        slice_id = _validate_proposal(current, proposed)
        if _read_bounded(target) != current:
            raise ReconciliationRefusal("roadmap_preimage_changed")
        descriptor, temporary = tempfile.mkstemp(
            prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(proposed)
                stream.flush()
                os.fsync(stream.fileno())
            if _read_bounded(target) != current:
                raise ReconciliationRefusal("roadmap_preimage_changed")
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        return ReconciliationResult(
            target=target.relative_to(self._root).as_posix(),
            slice_id=slice_id,
            before_sha256=before_hash,
            after_sha256=after_hash,
        )


def _read_bounded(path: Path) -> bytes:
    try:
        if path.is_symlink() or _is_junction(path) or not path.is_file():
            raise ReconciliationRefusal("proposal_not_regular")
        with open(path, "rb") as stream:
            data = stream.read(MAX_ROADMAP_BYTES + 1)
    except OSError:
        raise ReconciliationRefusal("proposal_unreadable") from None
    if len(data) > MAX_ROADMAP_BYTES:
        raise ReconciliationRefusal("proposal_too_large")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ReconciliationRefusal("proposal_not_utf8") from None
    if "\r" in text or "\x00" in text or not text.endswith("\n"):
        raise ReconciliationRefusal("proposal_text_invalid")
    return data


def _parse_roadmap(data: bytes) -> tuple[tuple[str, ...], tuple[_Section, ...], tuple[str, ...]]:
    lines = tuple(data.decode("utf-8").splitlines(keepends=True))
    starts = [index for index, line in enumerate(lines) if _MILESTONE.match(line)]
    ruled = [index for index, line in enumerate(lines) if line.rstrip("\n") == "## Ruled Out"]
    if not starts or len(ruled) != 1 or ruled[0] <= starts[-1]:
        raise ReconciliationRefusal("roadmap_structure_invalid")
    sections = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else ruled[0]
        match = _MILESTONE.match(lines[start])
        assert match is not None
        sections.append(_Section(match.group("id"), lines[start:end]))
    return lines[: starts[0]], tuple(sections), lines[ruled[0] :]


def _validate_proposal(current: bytes, proposed: bytes) -> str:
    before_prefix, before_sections, before_ruled = _parse_roadmap(current)
    after_prefix, after_sections, after_ruled = _parse_roadmap(proposed)
    if before_prefix != after_prefix or before_ruled != after_ruled:
        raise ReconciliationRefusal("protected_roadmap_region_changed")
    if [item.milestone_id for item in before_sections] != [
        item.milestone_id for item in after_sections
    ]:
        raise ReconciliationRefusal("milestone_inventory_changed")

    accepted_ids: set[str] = set()
    for section in before_sections:
        if _status(section) == "completed":
            accepted_ids.add(section.milestone_id)
            accepted_ids.update(_slice_ids(section))

    changed: list[tuple[_Section, _Section]] = []
    for before, after in zip(before_sections, after_sections, strict=True):
        if before.lines == after.lines:
            continue
        if _status(before) == "completed":
            raise ReconciliationRefusal("accepted_milestone_changed")
        changed.append((before, after))
    if len(changed) != 1:
        raise ReconciliationRefusal("proposal_scope_invalid")

    before, after = changed[0]
    if _status(before) not in ("active", "planned"):
        raise ReconciliationRefusal("milestone_not_editable")
    before_parts = _field_parts(before)
    after_parts = _field_parts(after)
    if tuple(before_parts) != tuple(after_parts):
        raise ReconciliationRefusal("roadmap_field_inventory_changed")
    for label in before_parts:
        old = before_parts[label]
        new = after_parts[label]
        if old == new:
            continue
        if label not in _EDITABLE:
            raise ReconciliationRefusal("protected_field_changed")
        _check_changed_lines(old, new, accepted_ids)
    slices = _slice_ids(before)
    if len(slices) != 1:
        raise ReconciliationRefusal("active_slice_ambiguous")
    return slices[0]


def _field_parts(section: _Section) -> dict[str, tuple[str, ...]]:
    starts: list[tuple[int, str]] = []
    for index, line in enumerate(section.lines):
        for label in _LABELS:
            if line.startswith(label):
                starts.append((index, label))
                break
    if not starts or starts[0][0] == 0 or len({label for _, label in starts}) != len(starts):
        raise ReconciliationRefusal("roadmap_field_structure_invalid")
    parts: dict[str, tuple[str, ...]] = {
        "__prefix__": section.lines[: starts[0][0]]
    }
    for position, (start, label) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(section.lines)
        parts[label] = section.lines[start:end]
    return parts


def _status(section: _Section) -> str:
    values = [
        line.removeprefix("Status:").strip().casefold()
        for line in section.lines
        if line.startswith("Status:")
    ]
    if len(values) != 1 or values[0] not in ("active", "planned", "completed"):
        raise ReconciliationRefusal("milestone_status_invalid")
    return values[0]


def _slice_ids(section: _Section) -> tuple[str, ...]:
    return tuple(
        match.group("id")
        for line in section.lines
        if (match := _SLICE.match(line)) is not None
    )


def _check_changed_lines(
    before: tuple[str, ...], after: tuple[str, ...], accepted_ids: set[str]
) -> None:
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    for operation, left, left_end, right, right_end in matcher.get_opcodes():
        if operation == "equal":
            continue
        for line in before[left:left_end] + after[right:right_end]:
            stripped = line.rstrip("\n")
            if _HEADING.match(stripped) or _FORBIDDEN_CHANGED_TEXT.search(stripped):
                raise ReconciliationRefusal("forbidden_marker_changed")
            if any(re.search(rf"\b{re.escape(identifier)}\b", stripped) for identifier in accepted_ids):
                raise ReconciliationRefusal("accepted_identity_changed")


def _is_junction(path: Path) -> bool:
    checker = getattr(os.path, "isjunction", None)
    return bool(checker is not None and checker(path))
