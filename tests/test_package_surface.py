"""Packaging surface tests: metadata, export policy, module inventory, CLI."""

import contextlib
import io
import os
import re
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

import frutlups_drive
from frutlups_drive.__main__ import main
from frutlups_drive.contracts import ExitCode

PKG_ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = PKG_ROOT / "src" / "frutlups_drive"


def run_module(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(PKG_ROOT / "src"), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return subprocess.run(
        [sys.executable, "-m", "frutlups_drive", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


class PackagingMetadataTests(unittest.TestCase):
    def setUp(self):
        self.pyproject = tomllib.loads(
            (PKG_ROOT / "pyproject.toml").read_bytes().decode("utf-8")
        )

    def test_distribution_name_and_module_layout(self):
        self.assertEqual(self.pyproject["project"]["name"], "frutlups-drive")
        self.assertEqual(
            self.pyproject["tool"]["setuptools"]["package-dir"], {"": "src"}
        )
        self.assertEqual(
            self.pyproject["tool"]["setuptools"]["packages"],
            ["frutlups_drive", "frutlups_drive.dispatch"],
        )

    def test_python_requirement(self):
        self.assertEqual(self.pyproject["project"]["requires-python"], ">=3.11")

    def test_zero_runtime_dependencies(self):
        self.assertEqual(self.pyproject["project"]["dependencies"], [])

    def test_version_is_single_sourced_from_the_module(self):
        self.assertEqual(self.pyproject["project"]["dynamic"], ["version"])
        self.assertEqual(
            self.pyproject["tool"]["setuptools"]["dynamic"]["version"],
            {"attr": "frutlups_drive.__version__"},
        )

    def test_build_backend_is_setuptools(self):
        self.assertEqual(
            self.pyproject["build-system"]["build-backend"],
            "setuptools.build_meta",
        )

    def test_py_typed_marker_ships(self):
        self.assertTrue((MODULE_DIR / "py.typed").is_file())
        self.assertEqual(
            self.pyproject["tool"]["setuptools"]["package-data"],
            {"frutlups_drive": ["py.typed"]},
        )


class ExportPolicyTests(unittest.TestCase):
    def test_top_level_exports_version_only(self):
        self.assertEqual(frutlups_drive.__all__, ["__version__"])

    def test_version_shape(self):
        self.assertRegex(frutlups_drive.__version__, r"^\d+\.\d+\.\d+$")

    def test_module_inventory_is_exactly_the_m003_runtime(self):
        # Provider-specific adapter modules remain absent; the one bounded
        # declaration-authoritative memory hook module is the M005 surface.
        # the dispatch subpackage carries the protocol, deterministic mock,
        # manual, the provider-neutral subprocess executor, and the two-seat
        # provider CLI binding. The released-frutlups boundary remains its
        # own module; M006 adds the pure holistic oracle module without a
        # top-level export.
        modules = sorted(p.name for p in MODULE_DIR.iterdir() if p.name != "__pycache__")
        self.assertEqual(
            modules,
            [
                "__init__.py",
                "__main__.py",
                "budget.py",
                "cli.py",
                "contracts.py",
                "corrective.py",
                "dispatch",
                "escalate.py",
                "frutlupscli.py",
                "killswitch.py",
                "ladder.py",
                "livegate.py",
                "memory_hooks.py",
                "mockverbs.py",
                "oracle.py",
                "planstate.py",
                "policy.py",
                "py.typed",
                "reconciliation.py",
                "runstore.py",
                "supervisor.py",
                "telemetry.py",
                "verifier.py",
                "watcher.py",
                "workspace.py",
            ],
        )
        dispatch = sorted(
            p.name
            for p in (MODULE_DIR / "dispatch").iterdir()
            if p.name != "__pycache__"
        )
        self.assertEqual(
            dispatch,
            ["__init__.py", "base.py", "manual.py", "mock.py",
             "provider_cli.py", "subprocess_agent.py"],
        )


class CliTests(unittest.TestCase):
    def test_version_via_subprocess(self):
        completed = run_module("--version")
        self.assertEqual(completed.returncode, int(ExitCode.OK))
        self.assertEqual(
            completed.stdout.strip(),
            f"frutlups-drive {frutlups_drive.__version__}",
        )
        self.assertEqual(completed.stderr, "")

    def test_bare_invocation_refuses(self):
        completed = run_module()
        self.assertEqual(completed.returncode, int(ExitCode.REFUSED))
        self.assertIn("usage:", completed.stderr)

    def test_unknown_argument_refuses(self):
        completed = run_module("run")
        self.assertEqual(completed.returncode, int(ExitCode.REFUSED))

    def test_main_in_process(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["--version"])
        self.assertEqual(code, ExitCode.OK)
        self.assertEqual(
            stdout.getvalue(), f"frutlups-drive {frutlups_drive.__version__}\n"
        )
        help_out = io.StringIO()
        with contextlib.redirect_stdout(help_out):
            code = main(["--help"])
        self.assertEqual(code, ExitCode.OK)
        for verb in ("plan", "report", "run", "resume", "stop"):
            self.assertIn(verb, help_out.getvalue())

    def test_version_constant_matches_init_source(self):
        # Guards the dynamic-version wiring target named in pyproject.
        source = (MODULE_DIR / "__init__.py").read_bytes().decode("utf-8")
        match = re.search(r'^__version__ = "([^"]+)"$', source, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), frutlups_drive.__version__)


if __name__ == "__main__":
    unittest.main()
