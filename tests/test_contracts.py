"""Contract tests: every public enum, dataclass, and exit code is pinned.

A failure here means the frozen public contract changed; that is a Level 4
contract change, never a test to loosen.
"""

import dataclasses
import unittest
from pathlib import Path
from typing import Literal, get_type_hints

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

from frutlups_drive.contracts import (
    AgentRunRequest,
    AgentRunResult,
    CorrectiveFailureClass,
    ExitCode,
    LadderFailureClass,
    LoopStep,
    PlanOutcome,
    Role,
    StopReason,
)


def make_request(**overrides):
    base = dict(
        run_id="run-001",
        attempt_id="attempt_001",
        role=Role.CODER,
        prompt_path=Path("prompts/for_coding_agent/001_m001_s01_package_scaffold.md"),
        prompt_sha256="a" * 64,
        workspace=Path("."),
        base_revision=None,
        adapter="mock",
        model="",
        effort="xhigh",
        workspace_access="workspace_write",
        expected_artifacts=(
            Path("05_governance/reviews/m001/m001_s01_self_report.md"),
        ),
        max_seconds=3600,
        max_cost_usd=None,
    )
    base.update(overrides)
    return AgentRunRequest(**base)


def make_result(**overrides):
    base = dict(
        status="completed",
        event_log_path=Path("provider_events.jsonl"),
        changed_files=(Path("08_pkg/src/frutlups_drive/contracts.py"),),
        produced_artifacts=(
            Path("05_governance/reviews/m001/m001_s01_self_report.md"),
        ),
        exit_reason="finished",
        tokens_in=100,
        tokens_out=200,
        cost_usd=None,
    )
    base.update(overrides)
    return AgentRunResult(**base)


class EnumContractTests(unittest.TestCase):
    def test_plan_outcome_members(self):
        self.assertEqual(
            [(m.name, m.value) for m in PlanOutcome],
            [
                ("READY", "ready"),
                ("NEEDS_SPECIFICATION", "needs_specification"),
                ("BLOCKED", "blocked"),
                ("COMPLETE", "complete"),
                ("INVALID", "invalid"),
            ],
        )

    def test_loop_step_members(self):
        self.assertEqual(
            [(m.name, m.value) for m in LoopStep],
            [
                ("NO_FRONTIER", "no_frontier"),
                ("MAKE_CODING_PROMPT", "make_coding_prompt"),
                ("EXECUTE_CODING_PROMPT", "execute_coding_prompt"),
                ("FIX_SELF_REPORT", "fix_self_report"),
                ("MAKE_REVIEW_PROMPT", "make_review_prompt"),
                ("EXECUTE_REVIEW_PROMPT", "execute_review_prompt"),
                ("FIX_REVIEW_REPORT", "fix_review_report"),
                ("RECORD_VERDICT", "record_verdict"),
                ("FRONTIER_RECORDED", "frontier_recorded"),
            ],
        )

    def test_role_members(self):
        self.assertEqual(
            [(m.name, m.value) for m in Role],
            [
                ("ARCHITECT", "architect"),
                ("CODER", "coder"),
                ("REVIEWER", "reviewer"),
            ],
        )

    def test_ladder_failure_class_members(self):
        self.assertEqual(
            [(m.name, m.value) for m in LadderFailureClass],
            [
                ("PRODUCT_FINDING", "product_finding"),
                ("TRANSPORT", "transport"),
                ("ENVIRONMENT", "environment"),
                ("PATH_CONTRACT", "path_contract"),
                ("OPERATOR_KILL_SWITCH", "operator_kill_switch"),
            ],
        )

    def test_corrective_failure_class_members(self):
        self.assertEqual(
            [(m.name, m.value) for m in CorrectiveFailureClass],
            [
                ("PROMPT_CONTRACT", "prompt_contract"),
                ("PROMPT_ADOPTION", "prompt_adoption"),
                (
                    "REWORK_DECLARATION_MAPPING",
                    "rework_declaration_mapping",
                ),
            ],
        )

    def test_stop_reason_members(self):
        self.assertEqual(
            [(m.name, m.value) for m in StopReason],
            [
                ("BLOCKED_VERDICT", "blocked_verdict"),
                ("OVERRIDE_REQUIRED", "override_required"),
                ("INVALID_STATE", "invalid_state"),
                ("REPAIR_EXHAUSTED", "repair_exhausted"),
                ("LADDER_ROUND3", "ladder_round3"),
                ("LADDER_ROUND4_UNAUTHORIZED", "ladder_round4_unauthorized"),
                ("BUDGET_EXHAUSTED", "budget_exhausted"),
                ("KILL_SWITCH", "kill_switch"),
                ("MEMORY_GATE", "memory_gate"),
                ("ENVIRONMENT_GATE", "environment_gate"),
                ("NO_PROGRESS", "no_progress"),
                ("PATH_VIOLATION", "path_violation"),
                ("VERIFICATION_MISSING", "verification_missing"),
                ("PROVIDER_FAILURE", "provider_failure"),
                ("RUN_STORE_FULL", "run_store_full"),
                ("OWNER_NOTE", "owner_note"),
                ("FRESH_RUN_REQUIRED", "fresh_run_required"),
                ("CONTRACT_VERSION_REFUSED", "contract_version_refused"),
                ("HUMAN_GATE", "human_gate"),
                (
                    "HOLISTIC_FINDINGS_UNMAPPABLE",
                    "holistic_findings_unmappable",
                ),
            ],
        )

    def test_enums_serialize_to_lowercase_values(self):
        for enum_type in (
            PlanOutcome,
            LoopStep,
            Role,
            LadderFailureClass,
            CorrectiveFailureClass,
            StopReason,
        ):
            for member in enum_type:
                with self.subTest(member=member):
                    self.assertEqual(str(member), member.value)
                    self.assertEqual(member.value, member.value.lower())

    def test_exit_codes(self):
        self.assertEqual(
            [(m.name, m.value) for m in ExitCode],
            [
                ("OK", 0),
                ("INTERNAL_ERROR", 1),
                ("REFUSED", 2),
                ("STOPPED_WITH_ESCALATION", 10),
            ],
        )
        for member in ExitCode:
            self.assertIsInstance(member, int)


class AgentRunRequestTests(unittest.TestCase):
    def test_field_names_and_order(self):
        self.assertEqual(
            [f.name for f in dataclasses.fields(AgentRunRequest)],
            [
                "run_id",
                "attempt_id",
                "role",
                "prompt_path",
                "prompt_sha256",
                "workspace",
                "base_revision",
                "adapter",
                "model",
                "effort",
                "workspace_access",
                "expected_artifacts",
                "max_seconds",
                "max_cost_usd",
            ],
        )

    def test_field_types(self):
        hints = get_type_hints(AgentRunRequest)
        self.assertEqual(hints["run_id"], str)
        self.assertEqual(hints["attempt_id"], str)
        self.assertEqual(hints["role"], Role)
        self.assertEqual(hints["prompt_path"], Path)
        self.assertEqual(hints["prompt_sha256"], str)
        self.assertEqual(hints["workspace"], Path)
        self.assertEqual(hints["base_revision"], str | None)
        self.assertEqual(hints["adapter"], str)
        self.assertEqual(hints["model"], str)
        self.assertEqual(hints["effort"], str)
        self.assertEqual(
            hints["workspace_access"], Literal["read_only", "workspace_write"]
        )
        self.assertEqual(hints["expected_artifacts"], tuple[Path, ...])
        self.assertEqual(hints["max_seconds"], int)
        self.assertEqual(hints["max_cost_usd"], float | None)

    def test_frozen(self):
        self.assertTrue(AgentRunRequest.__dataclass_params__.frozen)
        request = make_request()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            request.run_id = "other"

    def test_unknown_workspace_access_refused_at_construction(self):
        with self.assertRaises(ValueError):
            make_request(workspace_access="admin")

    def test_valid_construction(self):
        request = make_request()
        self.assertEqual(request.role, Role.CODER)
        self.assertEqual(request.workspace_access, "workspace_write")


class AgentRunResultTests(unittest.TestCase):
    def test_field_names_and_order(self):
        self.assertEqual(
            [f.name for f in dataclasses.fields(AgentRunResult)],
            [
                "status",
                "event_log_path",
                "changed_files",
                "produced_artifacts",
                "exit_reason",
                "tokens_in",
                "tokens_out",
                "cost_usd",
                "provider_duration_seconds",
                "observed_duration_seconds",
                "retry_class",
                "cost_knowledge",
                "capture_truncated",
            ],
        )

    def test_field_types(self):
        hints = get_type_hints(AgentRunResult)
        self.assertEqual(
            hints["status"],
            Literal[
                "completed",
                "blocked",
                "failed",
                "timeout",
                "budget_exhausted",
                "policy_violation",
            ],
        )
        self.assertEqual(hints["event_log_path"], Path)
        self.assertEqual(hints["changed_files"], tuple[Path, ...])
        self.assertEqual(hints["produced_artifacts"], tuple[Path, ...])
        self.assertEqual(hints["exit_reason"], str)
        self.assertEqual(hints["tokens_in"], int | None)
        self.assertEqual(hints["tokens_out"], int | None)
        self.assertEqual(hints["cost_usd"], float | None)
        self.assertEqual(hints["provider_duration_seconds"], float | None)
        self.assertEqual(hints["observed_duration_seconds"], float | None)
        self.assertEqual(
            hints["retry_class"],
            Literal[
                "not_applicable",
                "provider_retryable",
                "model_stream_active",
                "scientific_subprocess_running",
            ],
        )
        self.assertEqual(
            hints["cost_knowledge"],
            Literal["measured", "subscription_prepaid", "unknown"],
        )
        self.assertEqual(hints["capture_truncated"], bool)

    def test_frozen(self):
        self.assertTrue(AgentRunResult.__dataclass_params__.frozen)
        result = make_result()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.status = "failed"

    def test_all_status_values_accepted(self):
        for status in (
            "completed",
            "blocked",
            "failed",
            "timeout",
            "budget_exhausted",
            "policy_violation",
        ):
            with self.subTest(status=status):
                self.assertEqual(make_result(status=status).status, status)

    def test_unknown_status_refused_at_construction(self):
        with self.assertRaises(ValueError):
            make_result(status="succeeded")

    def test_result_knowledge_and_retry_vocabularies_are_closed(self):
        with self.assertRaises(ValueError):
            make_result(retry_class="retry")
        with self.assertRaises(ValueError):
            make_result(cost_knowledge="free")
        with self.assertRaises(ValueError):
            make_result(cost_knowledge="measured", cost_usd=None)
        prepaid = make_result(cost_knowledge="subscription_prepaid")
        self.assertIsNone(prepaid.cost_usd)


if __name__ == "__main__":
    unittest.main()
