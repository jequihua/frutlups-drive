"""M009-S02 oracle-scale, exclusion, and fence-escalation regressions."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401
from _scenario import Scenario, build_project, payload

from frutlups_drive.contracts import StopReason
from frutlups_drive.oracle import (
    MAX_ORACLE_INPUT_BYTES,
    OracleRefusal,
    reconcile_pass_boundary,
)
from frutlups_drive.policy import (
    SCHEMA_VERSION,
    PolicyRefusal,
    load_execution_policy,
)
from frutlups_drive.workspace import (
    BoundarySnapshotRefusal,
    RECONCILIATION_PREFIXES,
    WorkspaceManager,
)


INDEX = "05_governance/reviews/INDEX.md"
MANIFEST = "05_governance/oracle_exclusions.json"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest(*, exact=(), prefixes=()) -> bytes:
    return (
        json.dumps(
            {
                "contract_version": 1,
                "exact_paths": list(exact),
                "top_level_prefixes": list(prefixes),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def complete_state():
    return payload(
        "complete",
        None,
        actor="none",
        frontier_present=False,
        completion_evidence={"path": "05_governance/completion.md"},
    )


class OracleExclusionPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def load(self, body: str):
        path = self.root / "frutlups_drive.toml"
        path.write_text(
            f'schema_version = "{SCHEMA_VERSION}"\n{body}', encoding="utf-8"
        )
        return load_execution_policy(path)

    def test_manifest_declaration_is_optional_and_authoritative(self) -> None:
        absent = self.load("")
        declared = self.load(f'oracle_exclusion_manifest = "{MANIFEST}"\n')

        self.assertIsNone(absent.policy.oracle_exclusion_manifest)
        self.assertNotIn("oracle_exclusion_manifest", absent.defaulted)
        self.assertEqual(declared.policy.oracle_exclusion_manifest, MANIFEST)
        self.assertEqual(declared.warnings, ())

    def test_manifest_declaration_path_fails_closed(self) -> None:
        for value in (True, "../outside.json", "local" + chr(92) + "manifest.json"):
            with self.subTest(value=value):
                literal = "true" if value is True else json.dumps(value)
                with self.assertRaises(PolicyRefusal) as caught:
                    self.load(f"oracle_exclusion_manifest = {literal}\n")
                self.assertIn(
                    caught.exception.code,
                    ("field_type_invalid", "field_value_invalid"),
                )


class BoundarySnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "project"
        self.root.mkdir()
        self.manager = WorkspaceManager(self.root, self.root / ".frutlups_drive")

    def write(self, relative: str, data: bytes) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def snapshot(self, *, manifest=None, maximum=1024, members=20_000):
        return self.manager.pass_boundary_snapshot(
            self.root,
            exclusion_manifest=manifest,
            max_file_bytes=maximum,
            max_members=members,
        )

    def test_absent_manifest_preserves_ordinary_snapshot_shape(self) -> None:
        self.write("alpha.txt", b"alpha\n")
        self.write("nested/beta.bin", b"\x00\x01")
        self.write("local_state/ignored.bin", b"ignored")

        old = tuple(
            {"path": path, "sha256": digest}
            for path, digest in sorted(self.manager.snapshot(self.root).items())
        )
        self.assertEqual(self.snapshot(), old)

    def test_malformed_manifest_refuses_with_bounded_diagnostic(self) -> None:
        cases = (
            b"{not-json}\n",
            b'{"contract_version":1,"exact_paths":[]}\n',
            _manifest(prefixes=("nested/not-top-level/",)),
            _manifest(exact=(MANIFEST,)),
            _manifest(exact=(INDEX,)),
        )
        for data in cases:
            with self.subTest(data=data[:40]):
                self.write(MANIFEST, data)
                with self.assertRaises(BoundarySnapshotRefusal) as caught:
                    self.snapshot(manifest=MANIFEST)
                self.assertEqual(
                    caught.exception.code, "oracle_exclusion_manifest_invalid"
                )
                self.assertIn("pass boundary not frozen", caught.exception.message)
                self.assertIn("nothing was excluded", caught.exception.message)
                self.assertLessEqual(len(caught.exception.message), 16 * 1024)

    def test_oversize_refusal_names_exact_paths_sizes_and_declaration(self) -> None:
        self.write("build/cache.jar", b"x" * 9)
        self.write("large-local.bin", b"y" * 11)

        with self.assertRaises(BoundarySnapshotRefusal) as caught:
            self.snapshot(maximum=8)

        self.assertEqual(caught.exception.code, "oracle_input_oversized")
        for expected in (
            "'build/cache.jar' (9 bytes)",
            "'large-local.bin' (11 bytes)",
            "oracle_exclusion_manifest",
            "exact_paths: large-local.bin",
            "top_level_prefixes: build/",
            "nothing was auto-excluded",
        ):
            self.assertIn(expected, caught.exception.message)

    def test_snapshot_overflow_names_first_excess_path(self) -> None:
        self.write("a.txt", b"a")
        self.write("b.txt", b"bb")
        self.write("c.txt", b"ccc")

        with self.assertRaises(BoundarySnapshotRefusal) as caught:
            self.snapshot(members=2)

        self.assertEqual(caught.exception.code, "artifact_inventory_overflow")
        self.assertIn("'c.txt' (3 bytes)", caught.exception.message)
        self.assertIn("exact_paths: c.txt", caught.exception.message)

    def test_excluded_exact_file_and_tree_are_visible_but_not_reread(self) -> None:
        index = self.write(INDEX, b"# Review Index\n")
        manifest = self.write(
            MANIFEST,
            _manifest(exact=("large-local.bin",), prefixes=("build/",)),
        )
        exact_data = b"e" * 2_048
        self.write("large-local.bin", exact_data)
        self.write("build/cache/a.jar", b"a" * 1_500)
        self.write("build/classes/b.class", b"b" * 1_250)

        artifacts = self.snapshot(manifest=MANIFEST, maximum=1_024)
        exact = next(item for item in artifacts if item["path"] == "large-local.bin")
        tree = next(item for item in artifacts if item["path"] == "build/")
        self.assertEqual(
            exact,
            {
                "path": "large-local.bin",
                "sha256": _sha(exact_data),
                "size_bytes": len(exact_data),
                "type": "excluded",
            },
        )
        self.assertEqual(tree["type"], "excluded")
        self.assertEqual(tree["size_bytes"], 2_750)
        self.assertRegex(tree["sha256"], r"^[0-9a-f]{64}$")
        self.assertFalse(
            any(
                item["path"].startswith("build/")
                for item in artifacts
                if item is not tree
            )
        )
        self.assertIn(
            {"path": MANIFEST, "sha256": _sha(manifest.read_bytes())}, artifacts
        )

        evidence = self.root.parent / "evidence"
        evidence.mkdir()
        events = b"{}\n"
        (evidence / "events.jsonl").write_bytes(events)
        record = {
            "contract_version": 1,
            "run_id": "run_001",
            "evidence": [{"path": "events.jsonl", "sha256": _sha(events)}],
            "artifacts": list(artifacts),
        }
        (self.root / "large-local.bin").unlink()
        for child in sorted((self.root / "build").rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        (self.root / "build").rmdir()

        bundle = reconcile_pass_boundary(
            record, self.root, evidence, max_file_bytes=1_024
        )
        self.assertEqual(bundle["observations"], [])
        self.assertEqual(
            _sha(index.read_bytes()),
            next(item["sha256"] for item in artifacts if item["path"] == INDEX),
        )
        malformed = json.loads(json.dumps(record))
        malformed_exact = next(
            item
            for item in malformed["artifacts"]
            if item["path"] == "large-local.bin"
        )
        malformed_exact.pop("size_bytes")
        with self.assertRaisesRegex(OracleRefusal, "pass_boundary_invalid"):
            reconcile_pass_boundary(
                malformed, self.root, evidence, max_file_bytes=1_024
            )


class BoundaryAndEscalationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_actual_oracle_bound_refuses_before_boundary_freeze(self) -> None:
        project = build_project(self.root)
        oversized = project / "build/cache.jar"
        oversized.parent.mkdir()
        oversized.write_bytes(b"x" * (MAX_ORACLE_INPUT_BYTES + 1))
        scenario = Scenario(
            self.root,
            project=project,
            states=[complete_state()],
            policy_body=(
                "[autonomy]\n"
                'pass_boundary = "two_clean"\n'
                "auto_continue_past_frontier_recorded = true\n"
            ),
        )

        result = scenario.supervisor.tick()

        self.assertEqual(result.stop_reason, StopReason.INVALID_STATE)
        self.assertIn("build/cache.jar", result.detail)
        self.assertIn(str(MAX_ORACLE_INPUT_BYTES + 1), result.detail)
        self.assertFalse(
            (scenario.store.run_dir("run_001") / "pass_boundary.json").exists()
        )

    def test_declared_large_tree_freezes_one_typed_summary(self) -> None:
        project = build_project(self.root)
        manifest = project / MANIFEST
        manifest.write_bytes(_manifest(prefixes=("build/",)))
        large = project / "build/cache.jar"
        large.parent.mkdir()
        large.write_bytes(b"x" * (MAX_ORACLE_INPUT_BYTES + 1))
        scenario = Scenario(
            self.root,
            project=project,
            states=[complete_state()],
            policy_body=(
                f'oracle_exclusion_manifest = "{MANIFEST}"\n'
                "[autonomy]\n"
                'pass_boundary = "two_clean"\n'
                "auto_continue_past_frontier_recorded = true\n"
            ),
        )

        result = scenario.supervisor.tick()

        self.assertEqual((result.kind, result.detail), ("acted", "pass_boundary"))
        record = scenario.store.read_pass_boundary("run_001")
        excluded = [
            item for item in record["artifacts"] if item.get("type") == "excluded"
        ]
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]["path"], "build/")
        self.assertEqual(excluded[0]["size_bytes"], MAX_ORACLE_INPUT_BYTES + 1)
        self.assertIsNotNone(scenario.store.read_pass_oracle("run_001"))

    def test_path_escalation_pins_sorted_violations_and_allowed_prefixes(self) -> None:
        scenario = Scenario(self.root, states=[complete_state()])
        scenario.supervisor._journal(
            "fence",
            attempt="",
            violations=[
                {"code": "reconciliation_scope", "path": "zeta/out.md"},
                {"code": "path_escape", "path": "alpha/out.md"},
            ],
        )

        result = scenario.supervisor._stop(
            StopReason.PATH_VIOLATION, "fixture fence refusal"
        )
        text = result.escalation_path.read_text(encoding="utf-8")

        first = '- code = "path_escape"; path = "alpha/out.md"'
        second = '- code = "reconciliation_scope"; path = "zeta/out.md"'
        self.assertLess(text.index(first), text.index(second))
        self.assertIn("Expected allowed prefixes or exact paths:", text)
        for prefix in RECONCILIATION_PREFIXES:
            self.assertIn(f'- "{prefix}"', text)
        fence = next(event for event in scenario.events() if event["kind"] == "fence")
        self.assertEqual(
            fence["violations"],
            [
                {"code": "reconciliation_scope", "path": "zeta/out.md"},
                {"code": "path_escape", "path": "alpha/out.md"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
