"""M009-S03 admission, dispatch-ceiling, and retry-refusal proofs."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _bootstrap  # noqa: F401

from frutlups_drive.cli import main
from frutlups_drive.contracts import ExitCode, StopReason
from frutlups_drive.dispatch.mock import MockAgentAction

from _scenario import SELF_REPORT, Scenario, payload


class DispatchBudgetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_slice_ceiling_override_is_honored_and_journaled(self):
        scenario = Scenario(
            self.root,
            states=[payload()],
            coder=[
                MockAgentAction(writes=((SELF_REPORT, "# Coder Self-Report\n"),))
            ],
            policy_body=(
                "[roles.coder]\nadapter = \"mock\"\n"
                "[roles.reviewer]\nadapter = \"mock\"\n"
                "[dispatch]\n"
                "role_call_ceiling_seconds = {coder = 30}\n"
                "slice_call_ceiling_overrides = [{slice_id = \"M001-S01\", ceiling_seconds = 75}]\n"
            ),
            watch_timeout=5.0,
        )
        result = scenario.supervisor.tick()
        self.assertEqual(result.detail, "coder_attempt_completed")
        attempt = scenario.store.list_attempts("run_001", "M001-S01")[0]
        self.assertEqual(scenario.store.read_request(attempt)["max_seconds"], 75)
        dispatch = next(
            event for event in scenario.events() if event["kind"] == "dispatch"
        )
        self.assertEqual(dispatch["call_ceiling_seconds"], 75)
        self.assertEqual(dispatch["call_ceiling_source"], "slice")

    def test_running_subprocess_timeout_refuses_same_envelope_retry(self):
        scenario = Scenario(
            self.root,
            states=[payload()],
            coder=[
                MockAgentAction(
                    status="timeout",
                    exit_reason="agent_timeout",
                    observed_duration_seconds=5.0,
                    retry_class="scientific_subprocess_running",
                )
            ],
            policy_body=(
                "[roles.coder]\nadapter = \"mock\"\n"
                "[roles.reviewer]\nadapter = \"mock\"\n"
            ),
            watch_timeout=5.0,
        )
        result = scenario.supervisor.run_until()
        self.assertEqual(result.kind, "stopped")
        self.assertEqual(result.stop_reason, StopReason.BUDGET_EXHAUSTED)
        self.assertIn("same_envelope_retry_refused", result.detail)
        self.assertIn("measured_duration_seconds=5", result.detail)
        self.assertIn("ceiling_seconds=5", result.detail)
        self.assertEqual(
            len(scenario.store.list_attempts("run_001", "M001-S01")), 1
        )
        classified = next(
            event
            for event in scenario.events()
            if event["kind"] == "timeout_classification"
        )
        self.assertEqual(classified["retry_disposition"], "same_envelope_refused")

    def test_scientific_verification_budget_stops_without_redispatch(self):
        scenario = Scenario(
            self.root,
            states=[payload()],
            coder=[
                MockAgentAction(writes=((SELF_REPORT, "# Coder Self-Report\n"),))
            ],
            policy_body=(
                "[roles.coder]\nadapter = \"mock\"\n"
                "[roles.reviewer]\nadapter = \"mock\"\n"
                "[dispatch]\nscientific_subprocess_budget_seconds = 3\n"
            ),
            verifier_timed_out=[True],
        )
        result = scenario.supervisor.run_until()
        self.assertEqual(result.kind, "stopped")
        self.assertEqual(result.stop_reason, StopReason.BUDGET_EXHAUSTED)
        self.assertIn("measured_duration_seconds=3", result.detail)
        self.assertIn("ceiling_seconds=3", result.detail)
        verification = next(
            event for event in scenario.events() if event["kind"] == "verification"
        )
        self.assertEqual(verification["timeout_class"], "scientific_subprocess_budget")
        self.assertEqual(verification["retry_disposition"], "same_envelope_refused")
        self.assertEqual(
            len(scenario.store.list_attempts("run_001", "M001-S01")), 1
        )


class RuntimeBindingAdmissionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "project"
        self.project.mkdir()
        self.project.joinpath("frutlups_drive.toml").write_text(
            'schema_version = "frutlups_drive_policy_v1"\n'
            '[roles.architect]\nadapter = "claude_cli"\nmodel = "claude-opus-5"\n'
            '[roles.coder]\nadapter = "codex_cli"\nmodel = "gpt-5.6-sol"\n'
            '[roles.reviewer]\nadapter = "kimi_cli"\nmodel = "kimi-code/k3"\n',
            encoding="utf-8",
        )
        self.gate = self.project / "gate.md"
        self.gate.write_text(
            "# Gate\n\n```toml\n"
            'approval_state = "approved"\n'
            'approval_reference = "05_governance/human_owner_notes/099_test.md"\n'
            'coder_adapter = "codex_cli"\ncoder_model = "gpt-5.6-sol"\n'
            'reviewer_adapter = "kimi_cli"\nreviewer_model = "kimi-code/k3"\n'
            'architect_adapter = "claude_cli"\narchitect_model = "claude-opus-5"\n'
            'credential_env_names = ["USERPROFILE"]\n'
            'runtime_environment_bindings = [{name = "JAVA_TOOL_OPTIONS", value = "-Djava.io.tmpdir=.tmp"}]\n'
            'max_total_cost_usd = 10\nmax_call_cost_usd = 2\ncall_timeout_seconds = 600\n'
            'rollback_statement = "Delete fixture."\nkill_switch_statement = "Create STOP."\n'
            '[stop_conditions]\ncost = "Stop."\ntime = "Stop."\nhuman = "Stop."\n'
            "```\n",
            encoding="utf-8",
        )

    def test_plan_and_run_refuse_declared_binding_absent_from_policy(self):
        for command in (
            ["plan", str(self.project)],
            ["run", str(self.project), "--until", "slice_complete"],
        ):
            with self.subTest(command=command[0]), mock.patch(
                "frutlups_drive.cli.LIVE_GATE_PATH", self.gate
            ):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    code = main(command)
                self.assertEqual(code, int(ExitCode.REFUSED))
                self.assertIn("runtime_environment_binding_missing", stderr.getvalue())


class DispatchCeilingAdmissionTests(unittest.TestCase):
    POLICY_BASE = (
        'schema_version = "frutlups_drive_policy_v1"\n'
        '[roles.architect]\nadapter = "claude_cli"\nmodel = "claude-opus-5"\n'
        '[roles.coder]\nadapter = "codex_cli"\nmodel = "gpt-5.6-sol"\n'
        '[roles.reviewer]\nadapter = "kimi_cli"\nmodel = "kimi-code/k3"\n'
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "project"
        self.project.mkdir()
        self.policy_path = self.project / "frutlups_drive.toml"
        self.gate = self.project / "gate.md"

    def write_declarations(self, policy_dispatch="", gate_dispatch=""):
        self.policy_path.write_text(
            self.POLICY_BASE + policy_dispatch, encoding="utf-8"
        )
        self.gate.write_text(
            "# Gate\n\n```toml\n"
            'approval_state = "approved"\n'
            'approval_reference = "05_governance/human_owner_notes/099_test.md"\n'
            'coder_adapter = "codex_cli"\ncoder_model = "gpt-5.6-sol"\n'
            'reviewer_adapter = "kimi_cli"\nreviewer_model = "kimi-code/k3"\n'
            'architect_adapter = "claude_cli"\narchitect_model = "claude-opus-5"\n'
            'credential_env_names = ["USERPROFILE"]\n'
            'max_total_cost_usd = 10\nmax_call_cost_usd = 2\n'
            'call_timeout_seconds = 600\n'
            'rollback_statement = "Delete fixture."\n'
            'kill_switch_statement = "Create STOP."\n'
            + gate_dispatch
            + '[stop_conditions]\ncost = "Stop."\ntime = "Stop."\n'
            'human = "Stop."\n```\n',
            encoding="utf-8",
        )

    def invoke(self, command):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("frutlups_drive.cli.LIVE_GATE_PATH", self.gate), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(command)
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_plan_and_run_ceiling_refusal(self):
        commands = (
            ["plan", str(self.project)],
            ["run", str(self.project), "--until", "slice_complete"],
        )
        for command in commands:
            with self.subTest(command=command[0]):
                code, _, stderr = self.invoke(command)
                self.assertEqual(code, int(ExitCode.REFUSED))
                self.assertIn("dispatch_ceiling_authority_mismatch", stderr)
                self.assertFalse((self.project / ".frutlups_drive").exists())

    def test_plan_and_run_refuse_each_mismatch_direction_and_value(self):
        mismatches = (
            (
                '[dispatch]\nrole_call_ceiling_seconds = {coder = 30}\n',
                "",
            ),
            (
                "",
                'slice_call_ceiling_overrides = [{slice_id = "M001-S01", ceiling_seconds = 75}]\n',
            ),
            (
                '[dispatch]\nslice_call_ceiling_overrides = [{slice_id = "M001-S01", ceiling_seconds = 30}]\n',
                'slice_call_ceiling_overrides = [{slice_id = "M001-S01", ceiling_seconds = 45}]\n',
            ),
        )
        for index, (policy_dispatch, gate_dispatch) in enumerate(mismatches):
            with self.subTest(mismatch=index):
                self.write_declarations(policy_dispatch, gate_dispatch)
                self.assert_plan_and_run_ceiling_refusal()

    def test_canonical_equality_admits_at_plan_and_run_gate_boundary(self):
        self.write_declarations(
            '[dispatch]\n'
            'role_call_ceiling_seconds = {reviewer = 45, coder = 30}\n'
            'slice_call_ceiling_overrides = ['
            '{slice_id = "M002-S01", ceiling_seconds = 90}, '
            '{slice_id = "M001-S01", ceiling_seconds = 75}]\n',
            'role_call_ceiling_seconds = {coder = 30, reviewer = 45}\n'
            'slice_call_ceiling_overrides = ['
            '{slice_id = "M001-S01", ceiling_seconds = 75}, '
            '{slice_id = "M002-S01", ceiling_seconds = 90}]\n',
        )
        plan_code, _, plan_stderr = self.invoke(["plan", str(self.project)])
        self.assertEqual(plan_code, int(ExitCode.OK), plan_stderr)
        run_code, _, run_stderr = self.invoke(
            ["run", str(self.project), "--until", "slice_complete"]
        )
        self.assertEqual(run_code, int(ExitCode.REFUSED))
        self.assertIn("provider_binding_missing", run_stderr)
        self.assertNotIn("dispatch_ceiling_authority_mismatch", run_stderr)

    def test_undeclared_on_both_sides_preserves_gate_admission(self):
        self.write_declarations()
        plan_code, _, plan_stderr = self.invoke(["plan", str(self.project)])
        self.assertEqual(plan_code, int(ExitCode.OK), plan_stderr)
        run_code, _, run_stderr = self.invoke(
            ["run", str(self.project), "--until", "slice_complete"]
        )
        self.assertEqual(run_code, int(ExitCode.REFUSED))
        self.assertIn("provider_binding_missing", run_stderr)
        self.assertNotIn("dispatch_ceiling_authority_mismatch", run_stderr)


if __name__ == "__main__":
    unittest.main()
