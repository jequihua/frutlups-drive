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
import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Machine-local, never-committed state is outside workspace-effect authority:
# repository metadata, the drive run store, and ignored `local_state/` (the
# frutlups launch binding and per-run transport captures). Effect fences and
# attempt diffs compare workspace artifacts only; local-state tampering is
# caught by the exact launch identity before/after governed subprocesses and
# again during resume rather than being mistaken for an artifact effect.
_IGNORED_TOP_LEVEL = (".git", ".frutlups_drive", "local_state")
_REQUIRED_ORACLE_PATHS = ("05_governance/reviews/INDEX.md",)
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
MAX_ORACLE_EXCLUSION_MANIFEST_BYTES = 64 * 1024
MAX_ORACLE_EXCLUSION_ENTRIES = 1_024
_MAX_BOUNDARY_DIAGNOSTIC_BYTES = 16 * 1024

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


class BoundarySnapshotRefusal(Exception):
    """Fail-closed pass-boundary snapshot refusal with a bounded diagnostic."""

    def __init__(self, code: str, message: str) -> None:
        message = message[:_MAX_BOUNDARY_DIAGNOSTIC_BYTES]
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class _OracleExclusions:
    exact_paths: frozenset[str]
    top_level_prefixes: tuple[str, ...]

    def excludes(self, relative: str) -> bool:
        return relative in self.exact_paths or relative.startswith(
            self.top_level_prefixes
        )


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
        def walk_error(error: OSError) -> None:
            raw_path = Path(error.filename) if error.filename else root
            try:
                relative = raw_path.relative_to(root).as_posix()
            except ValueError:
                relative = raw_path.name or "workspace"
            raise BoundarySnapshotRefusal(
                "artifact_unreadable",
                f"pass boundary not frozen; input '{relative}' is unreadable",
            )

        for current, dirnames, filenames in os.walk(root, onerror=walk_error):
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

    def pass_boundary_snapshot(
        self,
        root: Path,
        *,
        exclusion_manifest: str | None,
        max_file_bytes: int,
        max_members: int,
    ) -> tuple[dict[str, object], ...]:
        """Build the governed project inventory before its durable freeze.

        With no declaration this produces the same ordinary member shape as
        :meth:`snapshot`. A declared manifest is strict and project-local.
        Exact excluded files receive one visible marker each; a declared
        top-level directory prefix receives one streamed aggregate marker so a
        large build tree cannot consume the boundary member budget. Ordinary
        files over the oracle content bound refuse with their exact paths and
        sizes; no path is ever excluded by inference or by ``.gitignore``.
        """

        if type(max_file_bytes) is not int or max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be a positive integer")
        if type(max_members) is not int or max_members <= 0:
            raise ValueError("max_members must be a positive integer")
        root = Path(root)
        exclusions = _load_oracle_exclusions(root, exclusion_manifest)
        records: list[dict[str, object]] = []
        oversized: list[tuple[str, int]] = []
        overflow: list[tuple[str, int]] = []

        for current, dirnames, filenames in os.walk(root):
            relative_dir = Path(current).relative_to(root)
            if relative_dir.parts and relative_dir.parts[0] in _IGNORED_TOP_LEVEL:
                dirnames[:] = []
                continue
            dirnames.sort()
            filenames.sort()
            if not relative_dir.parts:
                allowed: list[str] = []
                for name in dirnames:
                    if name in _IGNORED_TOP_LEVEL:
                        continue
                    prefix = f"{name}/"
                    if prefix in exclusions.top_level_prefixes:
                        path = Path(current) / name
                        size, digest = _excluded_directory_summary(path)
                        records.append(
                            {
                                "path": prefix,
                                "sha256": digest,
                                "size_bytes": size,
                                "type": "excluded",
                            }
                        )
                        if len(records) > max_members:
                            overflow.append((prefix, size))
                    else:
                        allowed.append(name)
                dirnames[:] = allowed
            else:
                dirnames[:] = list(dirnames)

            for filename in filenames:
                path = Path(current) / filename
                relative = (relative_dir / filename).as_posix()
                excluded = exclusions.excludes(relative)
                try:
                    if path.is_symlink():
                        target = str(os.readlink(path)).encode(
                            "utf-8", errors="surrogatepass"
                        )
                        size = len(target)
                        digest = hashlib.sha256(b"symlink:" + target).hexdigest()
                    else:
                        size = path.stat().st_size
                        if not excluded and size > max_file_bytes:
                            oversized.append((relative, size))
                            continue
                        size, digest = _streamed_file_summary(path)
                        if not excluded and size > max_file_bytes:
                            oversized.append((relative, size))
                            continue
                except OSError:
                    raise BoundarySnapshotRefusal(
                        "artifact_unreadable",
                        f"pass boundary not frozen; input '{relative}' is unreadable",
                    ) from None

                if excluded:
                    records.append(
                        {
                            "path": relative,
                            "sha256": digest,
                            "size_bytes": size,
                            "type": "excluded",
                        }
                    )
                else:
                    records.append({"path": relative, "sha256": digest})
                if len(records) > max_members:
                    overflow.append((relative, size))

        if oversized:
            raise BoundarySnapshotRefusal(
                "oracle_input_oversized",
                _boundary_refusal_message(
                    oversized,
                    reason=(
                        f"files exceed the {max_file_bytes}-byte oracle content bound"
                    ),
                ),
            )
        if overflow:
            raise BoundarySnapshotRefusal(
                "artifact_inventory_overflow",
                _boundary_refusal_message(
                    overflow,
                    reason=f"artifact inventory exceeds its {max_members}-member bound",
                ),
            )
        records.sort(key=lambda item: str(item["path"]))
        return tuple(records)

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
    backslash = chr(92)
    if raw.startswith(("/", backslash)) or _DRIVE_PREFIX.match(raw):
        return "path_escape"
    if backslash in raw:
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


def _canonical_relative(value: object) -> bool:
    if not isinstance(value, str) or not value or chr(92) in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and path.as_posix() == value
        and all(part not in ("", ".", "..") for part in path.parts)
    )


def _is_junction(path: Path) -> bool:
    return bool(getattr(path, "is_junction", lambda: False)())


def _load_oracle_exclusions(
    root: Path, manifest_relative: str | None
) -> _OracleExclusions:
    empty = _OracleExclusions(frozenset(), ())
    if manifest_relative is None:
        return empty
    if not _canonical_relative(manifest_relative):
        raise _manifest_refusal("the declared path is not canonical")
    manifest_path = Path(root) / manifest_relative
    if manifest_relative.split("/", 1)[0] in _IGNORED_TOP_LEVEL:
        raise _manifest_refusal("the declared file is outside the frozen surface")
    try:
        resolved_root = Path(root).resolve(strict=True)
        resolved = manifest_path.resolve(strict=True)
        resolved.relative_to(resolved_root)
        if (
            manifest_path.is_symlink()
            or _is_junction(manifest_path)
            or not resolved.is_file()
        ):
            raise OSError
        with open(resolved, "rb") as stream:
            data = stream.read(MAX_ORACLE_EXCLUSION_MANIFEST_BYTES + 1)
    except (OSError, ValueError):
        raise _manifest_refusal("the declared file is unavailable") from None
    if len(data) > MAX_ORACLE_EXCLUSION_MANIFEST_BYTES:
        raise _manifest_refusal(
            f"the declared file exceeds {MAX_ORACLE_EXCLUSION_MANIFEST_BYTES} bytes"
        )
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _manifest_refusal("the declared file is not valid UTF-8 JSON") from None
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {"contract_version", "exact_paths", "top_level_prefixes"}
        or payload.get("contract_version") != 1
        or not isinstance(payload.get("exact_paths"), list)
        or not isinstance(payload.get("top_level_prefixes"), list)
    ):
        raise _manifest_refusal("the manifest fields are malformed")
    exact = payload["exact_paths"]
    prefixes = payload["top_level_prefixes"]
    if len(exact) + len(prefixes) > MAX_ORACLE_EXCLUSION_ENTRIES:
        raise _manifest_refusal(
            f"the manifest exceeds {MAX_ORACLE_EXCLUSION_ENTRIES} entries"
        )
    if (
        any(not _canonical_relative(item) for item in exact)
        or len(set(exact)) != len(exact)
    ):
        raise _manifest_refusal("exact_paths must be unique canonical file paths")
    if any(item.split("/", 1)[0] in _IGNORED_TOP_LEVEL for item in exact):
        raise _manifest_refusal("exact_paths must remain inside the frozen surface")
    for item in exact:
        candidate = Path(root) / item
        if candidate.exists() and (
            candidate.is_dir() or _is_junction(candidate)
        ):
            raise _manifest_refusal("exact_paths must name files, not directories")
    checked_prefixes: list[str] = []
    for item in prefixes:
        if not isinstance(item, str) or not item.endswith("/"):
            raise _manifest_refusal(
                "top_level_prefixes must be unique top-level directories ending in '/'"
            )
        directory = item[:-1]
        if (
            not _canonical_relative(directory)
            or len(PurePosixPath(directory).parts) != 1
            or directory in _IGNORED_TOP_LEVEL
        ):
            raise _manifest_refusal(
                "top_level_prefixes must be unique top-level directories ending in '/'"
            )
        checked_prefixes.append(item)
    if len(set(checked_prefixes)) != len(checked_prefixes):
        raise _manifest_refusal(
            "top_level_prefixes must be unique top-level directories ending in '/'"
        )
    if any(manifest_relative == item for item in exact) or any(
        manifest_relative.startswith(prefix) for prefix in checked_prefixes
    ):
        raise _manifest_refusal("the manifest cannot exclude its own frozen bytes")
    if any(
        exact_path.startswith(prefix)
        for exact_path in exact
        for prefix in checked_prefixes
    ):
        raise _manifest_refusal(
            "exact_paths cannot duplicate a declared top-level prefix"
        )
    if any(
        required in exact
        or any(required.startswith(prefix) for prefix in checked_prefixes)
        for required in _REQUIRED_ORACLE_PATHS
    ):
        raise _manifest_refusal("the manifest cannot exclude a required oracle input")
    return _OracleExclusions(frozenset(exact), tuple(sorted(checked_prefixes)))


def _manifest_refusal(message: str) -> BoundarySnapshotRefusal:
    return BoundarySnapshotRefusal(
        "oracle_exclusion_manifest_invalid",
        f"pass boundary not frozen; {message}; nothing was excluded",
    )


def _streamed_file_summary(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _excluded_directory_summary(path: Path) -> tuple[int, str]:
    if path.is_symlink() or _is_junction(path) or not path.is_dir():
        raise _manifest_refusal(
            f"declared top-level prefix '{path.name}/' is not an ordinary directory"
        )
    digest = hashlib.sha256()
    total_size = 0
    def walk_error(error: OSError) -> None:
        raw_path = Path(error.filename) if error.filename else path
        try:
            relative = raw_path.relative_to(path).as_posix()
        except ValueError:
            relative = raw_path.name or path.name
        raise BoundarySnapshotRefusal(
            "artifact_unreadable",
            f"pass boundary not frozen; excluded input "
            f"'{path.name}/{relative}' is unreadable",
        )

    for current, dirnames, filenames in os.walk(path, onerror=walk_error):
        dirnames.sort()
        filenames.sort()
        current_path = Path(current)
        for name in tuple(dirnames):
            candidate = current_path / name
            if candidate.is_symlink() or _is_junction(candidate):
                raise _manifest_refusal(
                    f"declared top-level prefix '{path.name}/' contains a link-like directory"
                )
        for name in filenames:
            candidate = current_path / name
            relative = candidate.relative_to(path).as_posix()
            try:
                if candidate.is_symlink():
                    target = str(os.readlink(candidate)).encode(
                        "utf-8", errors="surrogatepass"
                    )
                    size = len(target)
                    member_sha256 = hashlib.sha256(b"symlink:" + target).hexdigest()
                else:
                    size, member_sha256 = _streamed_file_summary(candidate)
            except OSError:
                raise BoundarySnapshotRefusal(
                    "artifact_unreadable",
                    "pass boundary not frozen; excluded input "
                    f"'{path.name}/{relative}' is unreadable",
                ) from None
            summary = json.dumps(
                {
                    "path": relative,
                    "sha256": member_sha256,
                    "size_bytes": size,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            digest.update(summary + b"\n")
            total_size += size
    return total_size, digest.hexdigest()


def _boundary_refusal_message(
    offenders: list[tuple[str, int]], *, reason: str
) -> str:
    ordered = sorted(set(offenders))
    rendered: list[str] = []
    used = 0
    for path, size in ordered:
        item = f"'{path}' ({size} bytes)"
        if used + len(item) + 2 > _MAX_BOUNDARY_DIAGNOSTIC_BYTES // 2:
            rendered.append(f"... {len(ordered) - len(rendered)} additional paths")
            break
        rendered.append(item)
        used += len(item) + 2
    exact_candidates = sorted(path for path, _ in ordered if "/" not in path)
    prefixes = sorted(
        {f"{path.split('/', 1)[0]}/" for path, _ in ordered if "/" in path}
    )
    candidates: list[str] = []
    if exact_candidates:
        candidates.append("exact_paths: " + ", ".join(exact_candidates))
    if prefixes:
        candidates.append("top_level_prefixes: " + ", ".join(prefixes))
    return (
        f"pass boundary not frozen; {reason}: {', '.join(rendered)}. "
        "Declare top-level policy key 'oracle_exclusion_manifest' and list "
        "only reviewed candidates under exact_paths or top_level_prefixes "
        f"(candidate declarations: {'; '.join(candidates)}); nothing was "
        "auto-excluded"
    )
