"""Workspace leases, snapshots, diff manifests, and authority fences.

Supports in-place operation and optional ``git.worktree_per_slice`` leases via
local Git subprocesses (explicit argv, explicit cwd, no shell). The runner
may create worktrees and inspect state; it never commits, stages, resets,
pushes, fetches, merges, or alters accepted history.

Fences are resolved against the exact owned workspace and declared role
access. Violations are reported with stable codes and workspace-relative
paths only; nothing external is mutated by detection.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

# Machine-local, never-committed state is outside workspace-effect authority:
# repository metadata, the drive run store, and ignored `local_state/` (the
# frutlups launch binding and per-run transport captures). Effect fences and
# attempt diffs compare workspace artifacts only; local-state tampering is
# caught by the exact launch identity before/after governed subprocesses and
# again during resume rather than being mistaken for an artifact effect.
_IGNORED_TOP_LEVEL = (".git", ".frutlups_drive", "local_state")
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")

RECONCILIATION_PREFIXES = (
    "03_experiments/",
    "prompts/",
    "05_governance/human_owner_notes/",
)


@dataclass(frozen=True)
class WorkspaceLease:
    root: Path
    base_revision: str | None
    is_worktree: bool


@dataclass(frozen=True)
class FenceViolation:
    code: str
    path: str


class GitRunner:
    """Local git subprocess wrapper; injectable for unit tests."""

    def run(self, argv: list[str], cwd: Path) -> tuple[int, str]:
        completed = subprocess.run(
            ["git", *argv],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return completed.returncode, completed.stdout


class WorkspaceManager:
    def __init__(
        self, project_root: Path, store_root: Path, git: GitRunner | None = None
    ) -> None:
        self.project_root = Path(project_root)
        self.store_root = Path(store_root)
        self._git = git or GitRunner()

    def lease(self, run_id: str, slice_id: str, worktree: bool) -> WorkspaceLease:
        revision = self.revision(self.project_root)
        if not worktree:
            return WorkspaceLease(self.project_root, revision, False)
        if revision is None:
            raise RuntimeError(
                "worktree_per_slice requires the driven project to be a git "
                "repository"
            )
        target = self.store_root / "runs" / run_id / "worktrees" / slice_id
        if not target.is_dir():
            target.parent.mkdir(parents=True, exist_ok=True)
            code, output = self._git.run(
                ["worktree", "add", "--detach", str(target), revision],
                cwd=self.project_root,
            )
            if code != 0:
                raise RuntimeError("git worktree creation failed")
        return WorkspaceLease(target, revision, True)

    def revision(self, root: Path) -> str | None:
        if not (Path(root) / ".git").exists():
            return None
        code, output = self._git.run(["rev-parse", "HEAD"], cwd=root)
        return output.strip() if code == 0 else None

    def status_entries(self, root: Path) -> tuple[str, ...]:
        if not (Path(root) / ".git").exists():
            return ()
        code, output = self._git.run(
            ["status", "--porcelain", "--untracked-files=all"], cwd=root
        )
        if code != 0:
            return ()
        return tuple(sorted(line for line in output.splitlines() if line))

    def snapshot(self, root: Path) -> dict[str, str]:
        """Map of workspace-relative POSIX path -> content SHA-256."""
        root = Path(root)
        result: dict[str, str] = {}
        for current, dirnames, filenames in os.walk(root):
            relative_dir = Path(current).relative_to(root)
            if relative_dir.parts and relative_dir.parts[0] in _IGNORED_TOP_LEVEL:
                dirnames[:] = []
                continue
            dirnames[:] = [
                name
                for name in dirnames
                if not (not relative_dir.parts and name in _IGNORED_TOP_LEVEL)
            ]
            for filename in filenames:
                path = Path(current) / filename
                relative = (relative_dir / filename).as_posix()
                if path.is_symlink():
                    target = os.readlink(path)
                    digest = hashlib.sha256(
                        b"symlink:" + str(target).encode("utf-8")
                    ).hexdigest()
                else:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                result[relative] = digest
        return result

    def transaction_snapshot(self, root: Path) -> dict[str, str]:
        """Non-lossy verb manifest including ordinary and link-like entries.

        General attempt diffs intentionally retain the accepted file-only
        surface above. A governed external verb needs a sharper witness:
        empty directories and directory links are project effects too. The
        manifest stores only repo-relative names and one-way entry digests.
        """

        root = Path(root)
        result: dict[str, str] = {}
        directory_digest = hashlib.sha256(b"ordinary-directory\0").hexdigest()
        for current, dirnames, filenames in os.walk(root, followlinks=False):
            relative_dir = Path(current).relative_to(root)
            if relative_dir.parts and relative_dir.parts[0] in _IGNORED_TOP_LEVEL:
                dirnames[:] = []
                continue
            allowed_dirs = [
                name
                for name in dirnames
                if not (not relative_dir.parts and name in _IGNORED_TOP_LEVEL)
            ]
            dirnames[:] = allowed_dirs
            for dirname in allowed_dirs:
                path = Path(current) / dirname
                relative = (relative_dir / dirname).as_posix()
                is_junction = getattr(path, "is_junction", lambda: False)()
                if path.is_symlink() or is_junction:
                    try:
                        target = os.readlink(path)
                    except OSError:
                        target = "<unreadable-link>"
                    result[relative] = hashlib.sha256(
                        b"link-like-directory\0"
                        + str(target).encode("utf-8", errors="surrogatepass")
                    ).hexdigest()
                else:
                    result[relative] = directory_digest
            for filename in filenames:
                path = Path(current) / filename
                relative = (relative_dir / filename).as_posix()
                if path.is_symlink():
                    target = os.readlink(path)
                    digest = hashlib.sha256(
                        b"symlink:" + str(target).encode("utf-8")
                    ).hexdigest()
                else:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                result[relative] = digest
        return result

    def diff_manifest_payload(
        self,
        lease: WorkspaceLease,
        before: Mapping[str, str],
        after: Mapping[str, str],
    ) -> dict[str, object]:
        changes = []
        for path in sorted(set(before) | set(after)):
            if path not in after:
                changes.append({"path": path, "kind": "deleted", "sha256": None})
            elif path not in before:
                changes.append({"path": path, "kind": "created", "sha256": after[path]})
            elif before[path] != after[path]:
                changes.append(
                    {"path": path, "kind": "modified", "sha256": after[path]}
                )
        return {
            "repository": self.project_root.name,
            "workspace_kind": "worktree" if lease.is_worktree else "in_place",
            "base_revision": lease.base_revision,
            "final_revision": self.revision(lease.root),
            "status_entries": list(self.status_entries(lease.root)),
            "changes": changes,
        }


def _spelling_violation(raw: object) -> str | None:
    """Reject every non-canonical repo-relative spelling (R1-F1)."""
    if not isinstance(raw, str) or not raw.strip():
        return "path_invalid"
    if "\x00" in raw:
        return "path_invalid"
    if raw.startswith(("/", "\\")) or _DRIVE_PREFIX.match(raw):
        return "path_escape"
    if "\\" in raw:
        return "path_invalid"
    segments = raw.split("/")
    if ".." in segments:
        return "path_escape"
    if any(segment in ("", ".") for segment in segments):
        return "path_invalid"
    return None


def _bounded_display(raw: object) -> str:
    """Bounded diagnostic text: never a machine-local absolute path."""
    if isinstance(raw, str) and _spelling_violation(raw) is None:
        return raw
    try:
        name = Path(str(raw)).name
    except (TypeError, ValueError):
        name = ""
    return name or "unprintable-path"


def protected_component(relative: str) -> str | None:
    for segment in relative.split("/"):
        lowered = segment.lower()
        if lowered == ".git":
            return "repository_metadata"
        if lowered == ".frutlups_drive":
            return "store_mutation"
    return None


def authorize_workspace_writes(
    workspace_root: Path,
    store_root: Path,
    raw_paths: Iterable[object],
    *,
    workspace_access: str,
    expected_artifacts: Iterable[Path] = (),
    allowed_prefixes: tuple[str, ...] | None = None,
) -> tuple[FenceViolation, ...]:
    """Pre-effect authority decision over one complete intended action.

    Bounded domain: the M002 deterministic mock runtime and runner-owned mock
    verbs, whose effects are runner-process-local. Every intended write path
    must be a canonical repo-relative spelling under the exact workspace;
    repository metadata (``.git`` in directory and worktree-file form), the
    runner store, and undeclared governance authority are protected;
    read-only roles may write exactly their declared expected artifacts. Any
    violation refuses the whole action before its first byte.
    """
    violations: list[FenceViolation] = []
    expected = {Path(p).as_posix() for p in expected_artifacts}
    root = Path(workspace_root)
    root_resolved = root.resolve()
    store_resolved = Path(store_root).resolve()
    for raw in raw_paths:
        display = _bounded_display(raw)
        code = _spelling_violation(raw)
        if code is not None:
            violations.append(FenceViolation(code, display))
            continue
        protected = protected_component(raw)
        if protected is not None:
            violations.append(FenceViolation(protected, raw))
            continue
        # Resolve through the deepest existing ancestor so link and alias
        # forms cannot smuggle the effect outside the workspace or into the
        # store before the target itself exists.
        probe = root / raw
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            resolved_probe = probe.resolve()
        except OSError:
            violations.append(FenceViolation("path_invalid", display))
            continue
        # Workspace membership is decided first: a worktree lease legitimately
        # lives under the store root, and its own subtree is the authorized
        # workspace.
        if not _within(resolved_probe, root_resolved):
            if _within(resolved_probe, store_resolved):
                violations.append(FenceViolation("store_mutation", raw))
            else:
                violations.append(FenceViolation("path_escape", raw))
            continue
        if allowed_prefixes is not None:
            if raw not in expected and not raw.startswith(allowed_prefixes):
                violations.append(FenceViolation("reconciliation_scope", raw))
            continue
        if workspace_access == "read_only":
            if raw not in expected:
                violations.append(FenceViolation("read_only_mutation", raw))
            continue
        if raw not in expected and (
            raw == "PROJECT_STATE.md"
            or raw.startswith(("03_experiments/", "05_governance/"))
        ):
            violations.append(FenceViolation("governance_mutation", raw))
    return tuple(violations)


def changed_paths(
    before: Mapping[str, str], after: Mapping[str, str]
) -> tuple[str, ...]:
    return tuple(
        sorted(
            path
            for path in set(before) | set(after)
            if before.get(path) != after.get(path)
        )
    )


def check_fences(
    lease: WorkspaceLease,
    *,
    workspace_access: str,
    expected_artifacts: Iterable[Path],
    reported_paths: Iterable[Path],
    before: Mapping[str, str],
    after: Mapping[str, str],
    store_root: Path,
    allowed_prefixes: tuple[str, ...] | None = None,
) -> tuple[FenceViolation, ...]:
    """Post-effect fence: independent detection over one completed attempt.

    Prevention lives in :func:`authorize_workspace_writes`; this closed-world
    check is the separate falsifier. Every rule — read-only changed paths,
    symlink targets, protected metadata/store components, governance and
    reconciliation scope, and executor-reported paths — is evaluated for
    every role with no early return.
    """
    violations: list[FenceViolation] = []
    changed = changed_paths(before, after)
    expected = {Path(p).as_posix() for p in expected_artifacts}
    root_resolved = lease.root.resolve()
    store_resolved = Path(store_root).resolve()
    for path in changed:
        protected = protected_component(path)
        if protected is not None:
            violations.append(FenceViolation(protected, path))
            continue
        absolute = lease.root / path
        if absolute.is_symlink():
            link_target = Path(os.readlink(absolute))
            resolved_target = (
                (absolute.parent / link_target).resolve()
                if not link_target.is_absolute()
                else link_target.resolve()
            )
            if not _within(resolved_target, root_resolved):
                violations.append(FenceViolation("symlink_escape", path))
                continue
        elif absolute.exists() and not _within(absolute.resolve(), root_resolved):
            violations.append(FenceViolation("path_escape", path))
            continue
        if allowed_prefixes is not None:
            if path not in expected and not path.startswith(allowed_prefixes):
                violations.append(FenceViolation("reconciliation_scope", path))
            continue
        if workspace_access == "read_only":
            if path not in expected:
                violations.append(FenceViolation("read_only_mutation", path))
            continue
        if path in expected:
            continue
        if path == "PROJECT_STATE.md" or path.startswith(
            ("03_experiments/", "05_governance/")
        ):
            violations.append(FenceViolation("governance_mutation", path))

    for reported in reported_paths:
        reported_path = Path(reported)
        if reported_path.is_absolute():
            try:
                resolved = reported_path.resolve()
            except OSError:
                violations.append(FenceViolation("path_escape", reported_path.name))
                continue
            if _within(resolved, store_resolved):
                violations.append(
                    FenceViolation(
                        "store_mutation",
                        resolved.name,
                    )
                )
            elif not _within(resolved, root_resolved):
                violations.append(FenceViolation("path_escape", resolved.name))
        elif any(part == ".." for part in reported_path.parts):
            violations.append(FenceViolation("path_escape", reported_path.as_posix()))
        else:
            protected = protected_component(reported_path.as_posix())
            if protected is not None:
                violations.append(
                    FenceViolation(protected, reported_path.as_posix())
                )
    return tuple(violations)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
