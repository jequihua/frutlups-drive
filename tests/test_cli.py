"""CLI lanes: all four verbs, frozen exit codes, refusals, resume, stop."""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

from frutlups_drive.cli import main
from frutlups_drive.contracts import ExitCode
from frutlups_drive.runstore import RunStoreRefusal

FIXTURE_PROJECT = (
    Path(__file__).resolve().parent / "fixtures" / "projects" / "minimal_v3"
)


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def copy_fixture(self) -> Path:
        target = self.tmp / "driven_project"
        shutil.copytree(FIXTURE_PROJECT, target)
        return target

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def tree_snapshot(self, project: Path):
        return {
            p.relative_to(project).as_posix(): (
                p.read_bytes() if p.is_file() else None
            )
            for p in sorted(project.rglob("*"))
        }

    def edit_script(self, project: Path, mutate):
        path = project / ".frutlups_drive_mock/script.json"
        path.write_text(
            mutate(path.read_text(encoding="utf-8")), encoding="utf-8"
        )

    def assert_zero_mutation_refusal(self, mutate, expected_code):
        project = self.copy_fixture()
        self.edit_script(project, mutate)
        before = self.tree_snapshot(project)
        code, _, err = self.invoke(
            "run", str(project), "--until", "slice_complete"
        )
        self.assertEqual(code, int(ExitCode.REFUSED), err)
        self.assertIn(expected_code, err)
        self.assertEqual(self.tree_snapshot(project), before)
        self.assertFalse((project / ".frutlups_drive").exists())
        return err


class PlanVerbTests(CliTestCase):
    def test_plan_tolerates_absent_policy_and_mutates_nothing(self):
        project = self.copy_fixture()
        (project / "frutlups_drive.toml").unlink()
        before = sorted(str(p) for p in project.rglob("*"))
        code, out, err = self.invoke("plan", str(project))
        self.assertEqual(code, int(ExitCode.OK))
        self.assertIn("policy: absent", out)
        self.assertIn("outcome=ready step=execute_coding_prompt", out)
        self.assertEqual(sorted(str(p) for p in project.rglob("*")), before)
        self.assertFalse((project / ".frutlups_drive").exists())

    def test_plan_dry_run_reports_and_mutates_nothing(self):
        project = self.copy_fixture()
        before = sorted(str(p) for p in project.rglob("*"))
        code, out, _ = self.invoke("plan", str(project), "--dry-run")
        self.assertEqual(code, int(ExitCode.OK))
        self.assertIn("policy: present", out)
        self.assertEqual(sorted(str(p) for p in project.rglob("*")), before)

    def test_plan_missing_project_refuses(self):
        code, _, err = self.invoke("plan", str(self.tmp / "missing"))
        self.assertEqual(code, int(ExitCode.REFUSED))
        self.assertIn("project_missing", err)


class RunVerbTests(CliTestCase):
    def test_in_run_store_refusal_stops_without_cli_refused_stderr(self):
        project = self.copy_fixture()
        refusal = RunStoreRefusal(
            "cli_in_run_refusal", "representative tick refusal"
        )
        with patch(
            "frutlups_drive.supervisor.Supervisor._tick_inner",
            side_effect=refusal,
        ):
            code, out, err = self.invoke(
                "run", str(project), "--until", "slice_complete"
            )
        self.assertEqual(code, int(ExitCode.STOPPED_WITH_ESCALATION), err)
        self.assertIn("stopped: invalid_state", out)
        self.assertNotIn("refused:", err)
        escalations = list(
            (project / ".frutlups_drive/runs/run_001/escalations").iterdir()
        )
        self.assertEqual(len(escalations), 1)
        self.assertIn(
            "cli_in_run_refusal",
            escalations[0].read_text(encoding="utf-8"),
        )

    def test_clean_run_reaches_slice_boundary_with_exit_zero(self):
        project = self.copy_fixture()
        code, out, err = self.invoke(
            "run", str(project), "--until", "slice_complete"
        )
        self.assertEqual(code, int(ExitCode.OK), err)
        self.assertIn("run started: run_001", out)
        self.assertIn("boundary reached: slice_complete", out)
        store_root = project / ".frutlups_drive"
        self.assertTrue(
            (store_root / "runs/run_001/manifest.toml").is_file()
        )
        self.assertTrue(
            (
                project
                / "05_governance/reviews/m001/m001_s01_self_report.md"
            ).is_file()
        )
        attempt = store_root / "runs/run_001/slices/M001-S01/attempt_001"
        self.assertTrue((attempt / "verification/evidence.toml").is_file())
        self.assertFalse((store_root / "runs/run_001/escalations").exists())

    def test_run_without_policy_refuses(self):
        project = self.copy_fixture()
        (project / "frutlups_drive.toml").unlink()
        code, _, err = self.invoke("run", str(project), "--until", "slice_complete")
        self.assertEqual(code, int(ExitCode.REFUSED))
        self.assertIn("policy_file_missing", err)

    def test_run_with_non_mock_adapter_refuses(self):
        project = self.copy_fixture()
        (project / "frutlups_drive.toml").write_bytes(
            b'schema_version = "frutlups_drive_policy_v1"\n'
            b'[roles.coder]\nadapter = "manual"\n'
        )
        code, _, err = self.invoke("run", str(project), "--until", "slice_complete")
        self.assertEqual(code, int(ExitCode.REFUSED))
        self.assertIn("adapter_unavailable", err)

    def test_run_without_mock_convention_refuses(self):
        project = self.copy_fixture()
        shutil.rmtree(project / ".frutlups_drive_mock")
        code, _, err = self.invoke("run", str(project), "--until", "slice_complete")
        self.assertEqual(code, int(ExitCode.REFUSED))
        self.assertIn("mock_script_missing", err)

    def test_invalid_arguments_refuse(self):
        code, _, _ = self.invoke("run")
        self.assertEqual(code, int(ExitCode.REFUSED))
        code, _, _ = self.invoke("run", ".", "--until", "whenever")
        self.assertEqual(code, int(ExitCode.REFUSED))
        code, _, _ = self.invoke()
        self.assertEqual(code, int(ExitCode.REFUSED))


class MockCostGateTests(CliTestCase):
    """R2-F2 regressions: the complete script and every scripted cost fact are
    validated before ``run`` creates the store, a manifest, or a journal
    event; refusal is exit 2 with a byte/member-identical driven project."""

    def test_negative_scripted_cost_refuses_before_any_mutation(self):
        err = self.assert_zero_mutation_refusal(
            lambda text: text.replace('"cost_usd": 0.0', '"cost_usd": -0.25', 1),
            "mock_cost_invalid",
        )
        self.assertNotIn("-0.25", err, "the invalid value must not be echoed")

    def test_non_finite_scripted_cost_refuses_before_any_mutation(self):
        self.assert_zero_mutation_refusal(
            lambda text: text.replace('"cost_usd": 0.0', '"cost_usd": NaN', 1),
            "mock_script_invalid",
        )

    def test_negative_infinity_scripted_cost_refuses_before_any_mutation(self):
        self.assert_zero_mutation_refusal(
            lambda text: text.replace(
                '"cost_usd": 0.0', '"cost_usd": -Infinity', 1
            ),
            "mock_script_invalid",
        )

    def test_boolean_scripted_cost_refuses_before_any_mutation(self):
        self.assert_zero_mutation_refusal(
            lambda text: text.replace('"cost_usd": 0.0', '"cost_usd": true', 1),
            "mock_cost_invalid",
        )

    def test_401_digit_integer_cost_is_an_owned_validator_refusal(self):
        # R3-F1 reviewer-literal probe: a 401-digit positive integer is a
        # syntactically valid JSON cost admitted by the parser; the owned
        # outcome is exit 2 mock_cost_invalid with zero mutation, never a
        # raw OverflowError as internal exit 1.
        digits = "1" + "0" * 400
        err = self.assert_zero_mutation_refusal(
            lambda text: text.replace(
                '"cost_usd": 0.0', f'"cost_usd": {digits}', 1
            ),
            "mock_cost_invalid",
        )
        self.assertNotIn(digits, err, "the invalid value must not be echoed")
        self.assertNotIn("OverflowError", err)
        self.assertNotIn("internal error", err)

    def test_10001_digit_integer_cost_is_an_owned_parser_refusal(self):
        # The host JSON parser refuses integers above its digit limit before
        # the cost validator sees them; that distinct parser refusal is
        # still the owned exit 2 with zero mutation.
        digits = "1" + "0" * 10_000
        err = self.assert_zero_mutation_refusal(
            lambda text: text.replace(
                '"cost_usd": 0.0', f'"cost_usd": {digits}', 1
            ),
            "mock_script_invalid",
        )
        self.assertNotIn(digits, err)
        self.assertNotIn("internal error", err)


class MockTimeoutGateTests(CliTestCase):
    """R4-F1 regressions: a declared verification timeout is normalized at
    the shared verifier boundary during compilation — every invalid value is
    an owned exit-2 refusal with zero mutation, and no non-finite deadline
    can disable the verifier's bounded-time guarantee."""

    def test_401_digit_timeout_is_an_owned_normalizer_refusal(self):
        # R4-F1 reviewer-literal probe: a 401-digit integer timeout must
        # reach the owned exit-2 family, never exit 1 with raw
        # OverflowError.
        digits = "1" + "0" * 400
        err = self.assert_zero_mutation_refusal(
            lambda text: text.replace(
                '"timeout_seconds": 60', f'"timeout_seconds": {digits}', 1
            ),
            "verification_capture_invalid",
        )
        self.assertNotIn(digits, err, "the invalid value must not be echoed")
        self.assertNotIn("OverflowError", err)
        self.assertNotIn("internal error", err)

    def test_exponent_overflow_timeout_refuses_instead_of_infinite_deadline(self):
        # R4-F1 reviewer-literal probe: raw JSON 1e400 parses to positive
        # infinity without touching parse_constant; accepting it would
        # convert the declared deadline to infinity. It must refuse with
        # exit 2 and zero mutation, never run cleanly.
        err = self.assert_zero_mutation_refusal(
            lambda text: text.replace(
                '"timeout_seconds": 60', '"timeout_seconds": 1e400', 1
            ),
            "verification_capture_invalid",
        )
        self.assertNotIn("internal error", err)

    def test_10001_digit_timeout_is_an_owned_parser_refusal(self):
        # The host parser refuses integers above its digit limit before the
        # normalizer sees them; that distinct refusal is still exit 2.
        digits = "1" + "0" * 10_000
        self.assert_zero_mutation_refusal(
            lambda text: text.replace(
                '"timeout_seconds": 60', f'"timeout_seconds": {digits}', 1
            ),
            "mock_script_invalid",
        )

    def test_boolean_timeout_refuses_before_any_mutation(self):
        self.assert_zero_mutation_refusal(
            lambda text: text.replace(
                '"timeout_seconds": 60', '"timeout_seconds": true', 1
            ),
            "verification_capture_invalid",
        )

    def test_string_timeout_refuses_before_any_mutation(self):
        self.assert_zero_mutation_refusal(
            lambda text: text.replace(
                '"timeout_seconds": 60', '"timeout_seconds": "60"', 1
            ),
            "verification_capture_invalid",
        )


class CompiledScriptSnapshotTests(CliTestCase):
    """R3-F1: each run/resume invocation reads the mock script exactly once,
    compiles one immutable snapshot before any store mutation, and executes
    exactly that checked configuration."""

    def sequenced_loader(self, project, second_value):
        import json as json_module

        from frutlups_drive import cli as cli_module

        script_path = project / ".frutlups_drive_mock/script.json"
        first_value = json_module.loads(script_path.read_text(encoding="utf-8"))
        serial = {"calls": 0}
        real_loader = cli_module._load_script

        def loader(script_dir):
            serial["calls"] += 1
            return first_value if serial["calls"] == 1 else second_value

        cli_module._load_script = loader
        self.addCleanup(setattr, cli_module, "_load_script", real_loader)
        return serial

    def poisoned_script(self, project):
        import json as json_module

        script_path = project / ".frutlups_drive_mock/script.json"
        poisoned = json_module.loads(script_path.read_text(encoding="utf-8"))
        poisoned["executors"]["coder"][0]["cost_usd"] = -0.25
        return poisoned

    def test_run_reads_the_script_once_and_executes_that_snapshot(self):
        # Reviewer-literal falsifier: the loader serves a valid snapshot
        # first and an invalid one on any second read. One compiled
        # snapshot means the run executes the checked configuration to the
        # boundary and the poisoned value is never observed.
        project = self.copy_fixture()
        serial = self.sequenced_loader(project, self.poisoned_script(project))
        code, out, err = self.invoke(
            "run", str(project), "--until", "slice_complete"
        )
        self.assertEqual(
            serial["calls"], 1, "exactly one script read per invocation"
        )
        self.assertEqual(code, int(ExitCode.OK), err)
        self.assertIn("boundary reached: slice_complete", out)

    def test_resume_reads_the_script_once(self):
        project = self.copy_fixture()
        self.invoke("stop", str(project))
        code, _, _ = self.invoke("run", str(project), "--until", "slice_complete")
        self.assertEqual(code, int(ExitCode.STOPPED_WITH_ESCALATION))
        (project / ".frutlups_drive/STOP").unlink()
        serial = self.sequenced_loader(project, self.poisoned_script(project))
        code, out, err = self.invoke("resume", str(project), "run_001")
        self.assertEqual(
            serial["calls"], 1, "exactly one script read per invocation"
        )
        self.assertEqual(code, int(ExitCode.OK), err)
        self.assertIn("boundary reached: slice_complete", out)

    def test_missing_referenced_planstate_member_refuses_before_any_mutation(self):
        # R3-F1: every script-referenced planstate/content member is read
        # during compilation, before the run store, a manifest, or the
        # first journal event exists.
        project = self.copy_fixture()
        (project / ".frutlups_drive_mock/planstate/003.json").unlink()
        before = self.tree_snapshot(project)
        code, _, err = self.invoke(
            "run", str(project), "--until", "slice_complete"
        )
        self.assertEqual(code, int(ExitCode.REFUSED), err)
        self.assertIn("mock_script_invalid", err)
        self.assertEqual(self.tree_snapshot(project), before)
        self.assertFalse((project / ".frutlups_drive").exists())


class StopPolicyGateTests(CliTestCase):
    """R1-F2 regressions: the mandatory policy boundary precedes any STOP
    mutation; refusal leaves the project tree byte/member-identical."""

    def test_missing_policy_stop_refuses_with_zero_mutation(self):
        project = self.copy_fixture()
        (project / "frutlups_drive.toml").unlink()
        before = self.tree_snapshot(project)
        code, _, err = self.invoke("stop", str(project))
        self.assertEqual(code, int(ExitCode.REFUSED))
        self.assertIn("policy_file_missing", err)
        self.assertEqual(self.tree_snapshot(project), before)
        self.assertFalse((project / ".frutlups_drive").exists())

    def test_refused_policy_stop_refuses_with_zero_mutation(self):
        project = self.copy_fixture()
        (project / "frutlups_drive.toml").write_bytes(
            b'schema_version = "frutlups_drive_policy_v1"\n'
            b'api_key = "DO-NOT-ECHO"\n'
        )
        before = self.tree_snapshot(project)
        code, _, err = self.invoke("stop", str(project))
        self.assertEqual(code, int(ExitCode.REFUSED))
        self.assertIn("secret_shaped_value", err)
        self.assertNotIn("DO-NOT-ECHO", err)
        self.assertEqual(self.tree_snapshot(project), before)

    def test_stop_accepts_manual_adapter_policy(self):
        # Stopping an already configured manual/future run is a safe control
        # action; only the policy schema/fixed boundary is mandatory.
        project = self.copy_fixture()
        (project / "frutlups_drive.toml").write_bytes(
            b'schema_version = "frutlups_drive_policy_v1"\n'
            b'[roles.coder]\nadapter = "manual"\n'
        )
        code, _, _ = self.invoke("stop", str(project))
        self.assertEqual(code, int(ExitCode.OK))
        self.assertTrue((project / ".frutlups_drive/STOP").is_file())

    def test_accepted_policy_stop_remains_idempotent(self):
        project = self.copy_fixture()
        first, _, _ = self.invoke("stop", str(project))
        second, _, _ = self.invoke("stop", str(project))
        self.assertEqual((first, second), (int(ExitCode.OK), int(ExitCode.OK)))
        self.assertTrue((project / ".frutlups_drive/STOP").is_file())


class StopAndResumeTests(CliTestCase):
    def test_stop_creates_sentinel_and_run_stops_with_exit_ten(self):
        project = self.copy_fixture()
        code, out, _ = self.invoke("stop", str(project))
        self.assertEqual(code, int(ExitCode.OK))
        self.assertTrue((project / ".frutlups_drive/STOP").is_file())
        code, out, _ = self.invoke("run", str(project), "--until", "slice_complete")
        self.assertEqual(code, int(ExitCode.STOPPED_WITH_ESCALATION))
        self.assertIn("kill_switch", out)
        escalations = list(
            (project / ".frutlups_drive/runs/run_001/escalations").iterdir()
        )
        self.assertEqual(len(escalations), 1)

    def test_resume_after_stop_release_reaches_boundary(self):
        project = self.copy_fixture()
        self.invoke("stop", str(project))
        code, _, _ = self.invoke("run", str(project), "--until", "slice_complete")
        self.assertEqual(code, int(ExitCode.STOPPED_WITH_ESCALATION))
        (project / ".frutlups_drive/STOP").unlink()
        code, out, err = self.invoke("resume", str(project), "run_001")
        self.assertEqual(code, int(ExitCode.OK), err)
        self.assertIn("boundary reached: slice_complete", out)

    def test_resume_unknown_run_refuses(self):
        project = self.copy_fixture()
        code, _, err = self.invoke("resume", str(project), "run_042")
        self.assertEqual(code, int(ExitCode.REFUSED))
        self.assertIn("run_missing", err)


class ModuleEntryTests(CliTestCase):
    def test_module_invocation_end_to_end(self):
        project = self.copy_fixture()
        env = dict(os.environ)
        src = str(Path(__file__).resolve().parents[1] / "src")
        env["PYTHONPATH"] = os.pathsep.join(
            [src, env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "frutlups_drive",
                "run",
                str(project),
                "--until",
                "slice_complete",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("boundary reached: slice_complete", completed.stdout)


if __name__ == "__main__":
    unittest.main()
