"""Workspace lanes: snapshots, diff manifests, worktrees, and fence units."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

from frutlups_drive.workspace import (
    FenceViolation,
    WorkspaceLease,
    WorkspaceManager,
    changed_paths,
    check_fences,
)

GIT_AVAILABLE = shutil.which("git") is not None


def git(argv, cwd):
    completed = subprocess.run(
        ["git", *argv], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {argv} failed: {completed.stderr.strip()}")
    return completed.stdout


def init_repo(root: Path):
    git(["init", "--quiet"], root)
    git(["config", "user.email", "fixture@example.invalid"], root)
    git(["config", "user.name", "Fixture"], root)
    git(["add", "-A"], root)
    git(["commit", "--quiet", "-m", "fixture base"], root)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "project"
        self.root.mkdir()
        (self.root / "a.md").write_bytes(b"alpha\n")
        self.manager = WorkspaceManager(self.root, self.root / ".frutlups_drive")

    def test_snapshot_excludes_store_and_git_dirs(self):
        (self.root / ".frutlups_drive").mkdir()
        (self.root / ".frutlups_drive/junk.txt").write_bytes(b"x")
        (self.root / ".git").mkdir()
        (self.root / ".git/config").write_bytes(b"x")
        snapshot = self.manager.snapshot(self.root)
        self.assertEqual(sorted(snapshot), ["a.md"])

    def test_changed_paths_and_diff_manifest_kinds(self):
        before = self.manager.snapshot(self.root)
        (self.root / "a.md").write_bytes(b"alpha changed\n")
        (self.root / "b.md").write_bytes(b"beta\n")
        after = self.manager.snapshot(self.root)
        self.assertEqual(changed_paths(before, after), ("a.md", "b.md"))
        lease = WorkspaceLease(self.root, None, False)
        payload = self.manager.diff_manifest_payload(lease, before, after)
        kinds = {c["path"]: c["kind"] for c in payload["changes"]}
        self.assertEqual(kinds, {"a.md": "modified", "b.md": "created"})
        (self.root / "a.md").unlink()
        final = self.manager.snapshot(self.root)
        deleted = self.manager.diff_manifest_payload(lease, after, final)
        self.assertEqual(deleted["changes"][0]["kind"], "deleted")
        self.assertIsNone(deleted["changes"][0]["sha256"])

    def test_in_place_lease(self):
        lease = self.manager.lease("run_001", "M001-S01", worktree=False)
        self.assertEqual(lease.root, self.root)
        self.assertFalse(lease.is_worktree)
        self.assertIsNone(lease.base_revision)


@unittest.skipUnless(GIT_AVAILABLE, "git executable is required for worktree lanes")
class WorktreeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "project"
        self.root.mkdir()
        (self.root / "code.py").write_bytes(b"print('base')\n")
        init_repo(self.root)
        self.store_root = self.root / ".frutlups_drive"
        self.manager = WorkspaceManager(self.root, self.store_root)

    def test_worktree_lease_records_base_revision_and_isolates_writes(self):
        source_before = self.manager.snapshot(self.root)
        lease = self.manager.lease("run_001", "M001-S01", worktree=True)
        self.assertTrue(lease.is_worktree)
        self.assertEqual(lease.base_revision, self.manager.revision(self.root))
        self.assertTrue((lease.root / "code.py").is_file())
        (lease.root / "new_artifact.md").write_bytes(b"# produced\n")
        source_after = self.manager.snapshot(self.root)
        self.assertEqual(source_before, source_after)

    def test_worktree_lease_is_reused_per_slice(self):
        first = self.manager.lease("run_001", "M001-S01", worktree=True)
        second = self.manager.lease("run_001", "M001-S01", worktree=True)
        self.assertEqual(first.root, second.root)

    def test_diff_manifest_records_revisions_and_status(self):
        lease = self.manager.lease("run_001", "M001-S01", worktree=True)
        before = self.manager.snapshot(lease.root)
        (lease.root / "new_artifact.md").write_bytes(b"# produced\n")
        after = self.manager.snapshot(lease.root)
        payload = self.manager.diff_manifest_payload(lease, before, after)
        self.assertEqual(payload["base_revision"], lease.base_revision)
        self.assertEqual(payload["final_revision"], lease.base_revision)
        self.assertTrue(
            any("new_artifact.md" in entry for entry in payload["status_entries"])
        )

    def test_worktree_requires_git_repository(self):
        plain = Path(self._tmp.name) / "plain"
        plain.mkdir()
        manager = WorkspaceManager(plain, plain / ".frutlups_drive")
        with self.assertRaises(RuntimeError):
            manager.lease("run_001", "M001-S01", worktree=True)


class PreEffectAuthorityUnitTests(unittest.TestCase):
    """R1-F1 unit regressions for the shared pre-effect authority vocabulary."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir()
        self.store_root = Path(self._tmp.name) / ".frutlups_drive"

    def authorize(self, paths, *, access="workspace_write", expected=()):
        from frutlups_drive.workspace import authorize_workspace_writes

        return authorize_workspace_writes(
            self.root,
            self.store_root,
            paths,
            workspace_access=access,
            expected_artifacts=tuple(Path(p) for p in expected),
        )

    def test_non_canonical_spellings_are_refused(self):
        for raw in (
            "/absolute/file.txt",
            "C:/drive/file.txt",
            "C:\\drive\\file.txt",
            "//server/share/file.txt",
            "a\\b.txt",
            "",
            "   ",
            ".",
            "..",
            "../escape.txt",
            "a/./b.txt",
            "a//b.txt",
            "a/\x00b.txt",
        ):
            with self.subTest(raw=repr(raw)):
                self.assertNotEqual(self.authorize([raw]), ())

    def test_repository_metadata_and_store_components_are_protected(self):
        for raw in (
            ".git/config",
            ".GIT/config",
            "nested/.git/hooks",
            ".frutlups_drive/runs/run_001/x",
            "nested/.frutlups_drive/x",
        ):
            with self.subTest(raw=raw):
                violations = self.authorize([raw])
                self.assertNotEqual(violations, ())

    def test_worktree_git_file_form_is_protected(self):
        (self.root / ".git").write_bytes(b"gitdir: elsewhere\n")
        violations = self.authorize([".git"])
        self.assertNotEqual(violations, ())

    def test_one_invalid_path_refuses_the_complete_action(self):
        violations = self.authorize(
            ["ordinary.py", "../escape.txt"],
            expected=("ordinary.py",),
        )
        self.assertNotEqual(violations, ())

    def test_read_only_roles_may_write_exactly_expected_artifacts(self):
        report = "05_governance/reviews/m001/review_report.md"
        self.assertEqual(
            self.authorize([report], access="read_only", expected=(report,)), ()
        )
        self.assertNotEqual(
            self.authorize(["src/extra.py"], access="read_only",
                           expected=(report,)),
            (),
        )

    def test_workspace_write_ordinary_and_expected_paths_pass(self):
        self.assertEqual(
            self.authorize(
                ["src/fix.py",
                 "05_governance/reviews/m001/m001_s01_self_report.md"],
                expected=("05_governance/reviews/m001/m001_s01_self_report.md",),
            ),
            (),
        )

    def test_workspace_write_governance_is_refused_pre_effect(self):
        for raw in ("PROJECT_STATE.md", "03_experiments/roadmap.md",
                    "05_governance/decision_log.md"):
            with self.subTest(raw=raw):
                self.assertNotEqual(self.authorize([raw]), ())


class FenceUnitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir()
        self.store_root = Path(self._tmp.name) / ".frutlups_drive"
        self.lease = WorkspaceLease(self.root, None, False)

    def fences(self, *, access="workspace_write", expected=(), reported=(),
               before=None, after=None):
        return check_fences(
            self.lease,
            workspace_access=access,
            expected_artifacts=tuple(Path(p) for p in expected),
            reported_paths=tuple(Path(p) for p in reported),
            before=before or {},
            after=after or {},
            store_root=self.store_root,
        )

    def test_expected_artifact_writes_are_allowed(self):
        violations = self.fences(
            expected=("05_governance/reviews/m001/m001_s01_self_report.md",),
            after={"05_governance/reviews/m001/m001_s01_self_report.md": "h1"},
        )
        self.assertEqual(violations, ())

    def test_governance_mutation_is_refused(self):
        violations = self.fences(
            after={"PROJECT_STATE.md": "h1", "03_experiments/roadmap.md": "h2"},
        )
        codes = {(v.code, v.path) for v in violations}
        self.assertEqual(
            codes,
            {
                ("governance_mutation", "PROJECT_STATE.md"),
                ("governance_mutation", "03_experiments/roadmap.md"),
            },
        )

    def test_read_only_allows_only_expected_artifacts(self):
        violations = self.fences(
            access="read_only",
            expected=("05_governance/reviews/m001/review_report.md",),
            after={
                "05_governance/reviews/m001/review_report.md": "h1",
                "src/tampered.py": "h2",
            },
        )
        self.assertEqual(
            violations, (FenceViolation("read_only_mutation", "src/tampered.py"),)
        )

    def test_reported_absolute_escape_and_store_reach_are_refused(self):
        outside = Path(self._tmp.name) / "outside" / "stolen.txt"
        store_file = self.store_root / "runs/run_001/slices/S/attempt_001/request.json"
        violations = self.fences(reported=(outside, store_file))
        codes = sorted(v.code for v in violations)
        self.assertEqual(codes, ["path_escape", "store_mutation"])

    def test_read_only_role_reported_paths_are_still_evaluated(self):
        # R1-F1: the read-only branch must not return before reported-path
        # checks; an absolute reported path is an escape for every role.
        outside = Path(self._tmp.name) / "outside" / "stolen.txt"
        violations = self.fences(access="read_only", reported=(outside,))
        self.assertIn("path_escape", [v.code for v in violations])

    def test_read_only_role_reported_store_reach_is_evaluated(self):
        store_file = self.store_root / "runs/run_001/slices/S/attempt_001/x.json"
        violations = self.fences(access="read_only", reported=(store_file,))
        self.assertIn("store_mutation", [v.code for v in violations])

    def test_reported_relative_traversal_and_store_paths_are_refused(self):
        violations = self.fences(
            reported=("../escape.md", ".frutlups_drive/runs/run_001/x")
        )
        codes = sorted(v.code for v in violations)
        self.assertEqual(codes, ["path_escape", "store_mutation"])

    def test_symlink_escape_is_detected_when_creation_is_permitted(self):
        target_dir = Path(self._tmp.name) / "outside"
        target_dir.mkdir()
        link = self.root / "sneaky_link"
        try:
            os.symlink(str(target_dir), str(link), target_is_directory=True)
        except OSError as error:
            code = getattr(error, "winerror", None) or error.errno
            self.skipTest(
                f"host refuses link creation without elevation (error {code})"
            )
        violations = self.fences(after={"sneaky_link": "h1"})
        self.assertIn("symlink_escape", [v.code for v in violations])


if __name__ == "__main__":
    unittest.main()
