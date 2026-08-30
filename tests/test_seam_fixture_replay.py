"""Live replay of the frozen 73-row producer fixture oracle."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path, PurePosixPath

import _bootstrap  # noqa: F401

from frutlups_drive.seam_consumer import (
    CorrectiveReceipt,
    DrivePayload,
    Frontier,
    FrutlupsSeamConsumer,
    _run_bounded_process,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "drive_seam_v1"
SEAM_PYTHON = (
    REPO_ROOT.parent
    / "venvs"
    / "frutlups-drive-seam-87355a9"
    / "Scripts"
    / "python.exe"
)
CASE_FILES = (
    "payload_cases.json",
    "frontier_cases.json",
    "publication_cases.json",
    "dry_run_cases.json",
    "refusal_cases.json",
)


def load_cases() -> list[dict]:
    cases = []
    for name in CASE_FILES:
        document = json.loads((FIXTURE_ROOT / name).read_bytes().decode("utf-8"))
        cases.extend(document["cases"])
    return cases


def node_path(project_root: Path, relative: str) -> Path:
    return project_root.joinpath(*PurePosixPath(relative).parts)


def materialize(project_root: Path, nodes: list[dict]) -> None:
    project_root.mkdir(parents=True)
    for node in nodes:
        target = node_path(project_root, node["path"])
        if node["kind"] == "absent":
            if target.exists():
                raise AssertionError(f"fixture absent node unexpectedly exists: {node['path']}")
            continue
        if node["kind"] != "file":
            raise AssertionError(f"unsupported frozen fixture node: {node['kind']}")
        raw = node["content_utf8"].encode("utf-8")
        if hashlib.sha256(raw).hexdigest() != node["sha256"]:
            raise AssertionError(f"fixture node digest drift: {node['path']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)


def observe(project_root: Path, paths: list[str]) -> dict[str, dict[str, str]]:
    result = {}
    for relative in paths:
        target = node_path(project_root, relative)
        if not target.exists():
            result[relative] = {"state": "absent"}
        elif target.is_file() and not target.is_symlink():
            result[relative] = {
                "state": "present",
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
        elif target.is_symlink():
            result[relative] = {"state": "unsafe", "identity": "symbolic_link"}
        else:
            result[relative] = {"state": "unreadable"}
    return result


class FrozenProducerReplayTests(unittest.TestCase):
    def setUp(self):
        if not SEAM_PYTHON.is_file():
            self.skipTest(
                "governed frutlups seam qualification interpreter is absent on this host"
            )

    def test_all_73_frozen_rows_replay_exactly(self):
        cases = load_cases()
        self.assertEqual(len(cases), 73)
        for row in cases:
            with self.subTest(case=row["id"]), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary).resolve()
                project = base / "project"
                foreign_cwd = base / "foreign_cwd"
                foreign_cwd.mkdir()
                materialize(project, row["project_nodes"])

                expected_before = row["expected_before"]
                self.assertEqual(
                    observe(project, list(expected_before)),
                    expected_before,
                    "materialized fixture before-map differs",
                )
                substitutions = sum(
                    argument.count("$PROJECT_ROOT") for argument in row["argv"]
                )
                self.assertEqual(substitutions, 1)
                arguments = tuple(
                    argument.replace("$PROJECT_ROOT", str(project))
                    for argument in row["argv"]
                )
                stdin_utf8 = row["stdin_utf8"]
                result = _run_bounded_process(
                    (str(SEAM_PYTHON), *arguments),
                    cwd=foreign_cwd,
                    env={"PYTHONDONTWRITEBYTECODE": "1"},
                    timeout_seconds=120,
                    stdin_bytes=(
                        stdin_utf8.encode("utf-8")
                        if isinstance(stdin_utf8, str)
                        else None
                    ),
                )
                self.assertFalse(result.spawn_failed)
                self.assertFalse(result.timed_out)
                self.assertFalse(result.stdout_overflow)
                self.assertFalse(result.stderr_overflow)
                self.assertEqual(result.exit_code, row["expected_exit"])

                expected_stdout = row["expected_stdout"]
                if expected_stdout is None:
                    self.assertEqual(result.stdout, b"")
                else:
                    self.assertEqual(
                        json.loads(result.stdout.decode("utf-8")), expected_stdout
                    )
                if row["expected_stderr_class"] == "none":
                    self.assertEqual(result.stderr, b"")
                else:
                    self.assertEqual(row["expected_stderr_class"], "usage")
                    self.assertTrue(result.stderr.startswith(b"usage:"), result.stderr)
                expected_after = row["expected_after"]
                self.assertEqual(
                    observe(project, list(expected_after)),
                    expected_after,
                    "live producer after-map differs",
                )

    def test_typed_consumer_invokes_all_three_pinned_verbs_live(self):
        selected = {
            "payload_routine_entry_without_attempt": DrivePayload,
            "frontier_pass_achieved_advances": Frontier,
            "dry_run_validated": CorrectiveReceipt,
        }
        cases = {row["id"]: row for row in load_cases() if row["id"] in selected}
        self.assertEqual(set(cases), set(selected))
        for case_id, expected_type in selected.items():
            row = cases[case_id]
            with self.subTest(case=case_id), tempfile.TemporaryDirectory() as temporary:
                project = Path(temporary).resolve() / "project"
                materialize(project, row["project_nodes"])
                consumer = FrutlupsSeamConsumer(
                    python_executable=SEAM_PYTHON.resolve(),
                    project_root=project,
                    env={"PYTHONDONTWRITEBYTECODE": "1"},
                )
                argv = row["argv"]
                if row["verb"] == "drive-payload":
                    response = consumer.drive_payload(
                        sidecar_path=argv[argv.index("--sidecar") + 1],
                        slice_id=argv[argv.index("--slice") + 1],
                        prompt_path=argv[argv.index("--prompt") + 1],
                    )
                elif row["verb"] == "drive-frontier":
                    response = consumer.drive_frontier(
                        sidecar_path=argv[argv.index("--sidecar") + 1],
                        slice_id=argv[argv.index("--slice") + 1],
                        review_report_path=argv[argv.index("--review-report") + 1],
                    )
                else:
                    response = consumer.corrective_publish(
                        row["stdin_utf8"].encode("utf-8"), dry_run=True
                    )
                self.assertIsInstance(response, expected_type)


if __name__ == "__main__":
    unittest.main()
