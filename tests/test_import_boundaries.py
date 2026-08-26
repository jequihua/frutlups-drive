"""Import-boundary tests (architecture contract §3, scaffold rule extended).

- product code and product tests never import frutlups or llloom;
- the development repo's root scaffold test tree, when present, never imports
  frutlups_drive;
- the package imports cleanly when both siblings are made unimportable.
"""

import os
import re
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

PKG_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PKG_ROOT.parent

_SIBLING_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+(?:frutlups|llloom)\b", re.MULTILINE
)
_PRODUCT_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+frutlups_drive\b", re.MULTILINE
)


def python_files(root: Path):
    return [
        path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    ]


class StaticImportBoundaryTests(unittest.TestCase):
    def test_product_code_and_tests_never_import_siblings(self):
        scanned = python_files(PKG_ROOT / "src") + python_files(PKG_ROOT / "tests")
        self.assertGreaterEqual(len(scanned), 10)
        for path in scanned:
            with self.subTest(file=path.relative_to(PKG_ROOT).as_posix()):
                text = path.read_bytes().decode("utf-8")
                self.assertIsNone(
                    _SIBLING_IMPORT.search(text),
                    "product code must not import frutlups or llloom",
                )

    def test_root_scaffold_tests_never_import_the_product(self):
        root_tests = REPO_ROOT / "tests"
        if not root_tests.is_dir():
            self.skipTest("curated package repository has no root scaffold suite")
        scanned = python_files(root_tests)
        self.assertGreaterEqual(len(scanned), 1)
        for path in scanned:
            with self.subTest(file=path.relative_to(REPO_ROOT).as_posix()):
                text = path.read_bytes().decode("utf-8")
                self.assertIsNone(
                    _PRODUCT_IMPORT.search(text),
                    "the root scaffold suite must not import frutlups_drive",
                )


class RuntimeImportBoundaryTests(unittest.TestCase):
    def test_package_imports_with_siblings_blocked(self):
        script = textwrap.dedent(
            """
            import sys

            class BlockSiblings:
                def find_spec(self, name, path=None, target=None):
                    if name.split(".")[0] in {"frutlups", "llloom"}:
                        raise ImportError(f"blocked sibling import: {name}")
                    return None

            sys.meta_path.insert(0, BlockSiblings())

            import frutlups_drive
            import frutlups_drive.__main__
            import frutlups_drive.budget
            import frutlups_drive.cli
            import frutlups_drive.contracts
            import frutlups_drive.corrective
            import frutlups_drive.dispatch.base
            import frutlups_drive.dispatch.manual
            import frutlups_drive.dispatch.mock
            import frutlups_drive.dispatch.provider_cli
            import frutlups_drive.dispatch.subprocess_agent
            import frutlups_drive.escalate
            import frutlups_drive.killswitch
            import frutlups_drive.ladder
            import frutlups_drive.livegate
            import frutlups_drive.mockverbs
            import frutlups_drive.planstate
            import frutlups_drive.policy
            import frutlups_drive.reconciliation
            import frutlups_drive.runstore
            import frutlups_drive.supervisor
            import frutlups_drive.telemetry
            import frutlups_drive.verifier
            import frutlups_drive.watcher
            import frutlups_drive.workspace

            print("ok")
            """
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(PKG_ROOT / "src"), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "ok")

    def test_importing_the_product_does_not_load_siblings(self):
        import frutlups_drive.contracts  # noqa: F401
        import frutlups_drive.planstate  # noqa: F401
        import frutlups_drive.policy  # noqa: F401
        import frutlups_drive.runstore  # noqa: F401

        self.assertNotIn("frutlups", sys.modules)
        self.assertNotIn("llloom", sys.modules)


if __name__ == "__main__":
    unittest.main()
