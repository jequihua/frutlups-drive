"""Deterministic mock frutlups orchestrator-verb writer.

Stands in for ``make-coding-prompt``, ``make-review-prompt``, and
``record-verdict``: one scripted, template-shaped artifact per invocation.
The scripted destination is bound to the planning state (R1-F1): it must
exactly equal the artifact path the current planning state declares for the
invoked verb and must pass the shared canonical project-relative authority
validator before any parent creation or content publication. The writer
never interprets loop state; the next fresh planning-state read decides what
happened. An exhausted verb script raises loudly and the supervisor stops
fail-closed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from frutlups_drive.workspace import FenceViolation, authorize_workspace_writes

ORCHESTRATOR_VERBS = ("make-coding-prompt", "make-review-prompt", "record-verdict")


class VerbScriptExhausted(Exception):
    """The scripted artifact sequence for a verb has no further entries."""


class VerbAuthorityDenied(Exception):
    """The scripted verb destination lacks authority; nothing was written."""

    def __init__(self, violations) -> None:
        super().__init__("verb destination refused before any effect")
        self.violations = tuple(violations)


class MockVerbWriter:
    def __init__(
        self,
        project_root: Path,
        scripts: Mapping[str, Sequence[tuple[str, str]]],
        consumed: Mapping[str, int] | None = None,
        store_root: Path | None = None,
    ) -> None:
        self._root = Path(project_root)
        self._store_root = (
            Path(store_root)
            if store_root is not None
            else self._root / ".frutlups_drive"
        )
        self._scripts = {verb: tuple(entries) for verb, entries in scripts.items()}
        self.consumed = {verb: 0 for verb in ORCHESTRATOR_VERBS}
        if consumed:
            self.consumed.update(consumed)

    def invoke(self, verb: str, declared_path: str | None) -> Path:
        if verb not in ORCHESTRATOR_VERBS:
            raise VerbScriptExhausted(f"unknown orchestrator verb: {verb}")
        entries = self._scripts.get(verb, ())
        index = self.consumed[verb]
        if index >= len(entries):
            raise VerbScriptExhausted(
                f"verb script exhausted for '{verb}' after {len(entries)} artifacts"
            )
        relative, content = entries[index]
        if declared_path is None:
            raise VerbAuthorityDenied(
                (FenceViolation("verb_unbound", verb),)
            )
        if relative != declared_path:
            raise VerbAuthorityDenied(
                (FenceViolation("verb_path_mismatch", declared_path),)
            )
        violations = authorize_workspace_writes(
            self._root,
            self._store_root,
            (relative,),
            workspace_access="workspace_write",
            expected_artifacts=(Path(relative),),
        )
        if violations:
            raise VerbAuthorityDenied(violations)
        self.consumed[verb] = index + 1
        target = self._root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
        return target
