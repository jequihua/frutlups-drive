"""M009-S04 gate-controlled architect corrective-turn proofs."""

import json
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

from frutlups_drive.contracts import CorrectiveFailureClass, StopReason
from frutlups_drive.corrective import (
    CORRECTIVE_PROPOSAL_NAME,
    CorrectiveProposalRefusal,
    CorrectiveProposalWriter,
)
from frutlups_drive.dispatch.mock import MockAgentAction
from frutlups_drive.frutlupscli import FrutlupsVerbError

from _scenario import CODING_PROMPT, REVIEW_PROMPT, SELF_REPORT, Scenario, payload


CORRECTIVE_POLICY = (
    "architect_corrective_turn_enabled = true\n"
    "max_architect_corrective_turns_per_run = 1\n"
    '[roles.architect]\nadapter = "mock"\n'
    '[roles.coder]\nadapter = "mock"\n'
    '[roles.reviewer]\nadapter = "mock"\n'
    "[dispatch]\nrole_call_ceiling_seconds = {architect = 17}\n"
    "[limits]\nmax_total_cost_usd = 1.0\n"
)


def proposal(target, content):
    return json.dumps(
        {"contract_version": 1, "target": target, "content": content},
        sort_keys=True,
    )


def rework_content(slice_id="M001-S01"):
    return json.dumps(
        {
            "contract_id": "frutlups.rework_declaration",
            "contract_version": "1",
            "declaration_sequence": 1,
            "pass_id": "holistic_pass_001",
            "baseline_prompt_sequence": 0,
            "slice_ids": [slice_id],
        },
        sort_keys=True,
    )


class FailingPromptWriter:
    def invoke(self, verb, declared_path):
        raise FrutlupsVerbError(
            "verb_dry_run_refused", "synthetic prompt-contract refusal"
        )


class ArchitectCorrectiveTurnTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    @staticmethod
    def state(scenario):
        return scenario.supervisor._plan_provider.read_planning_state()

    def scenario(self, name, *, action, policy=CORRECTIVE_POLICY, state=None):
        return Scenario(
            self.root / name,
            states=[state or payload()],
            architect=[action],
            policy_body=policy,
            role_efforts={"architect": ("default", "corrective")},
        )

    def invoke_direct(
        self,
        scenario,
        failure_class,
        code,
        target,
        reason=StopReason.PROVIDER_FAILURE,
    ):
        return scenario.supervisor._architect_corrective_or_stop(
            failure_class,
            code,
            target,
            reason=reason,
            detail=f"original governed stop: {code}",
            state=self.state(scenario),
            slice_id="M001-S01",
        )

    def test_each_closed_trigger_dispatches_at_most_once(self):
        cases = (
            (
                CorrectiveFailureClass.PROMPT_CONTRACT,
                "verb_dry_run_refused",
                CODING_PROMPT,
                "# Corrected Coding Prompt\n",
            ),
            (
                CorrectiveFailureClass.PROMPT_ADOPTION,
                "current_prompt_identity_unavailable",
                CODING_PROMPT,
                "# Corrected Adoption Prompt\n",
            ),
            (
                CorrectiveFailureClass.REWORK_DECLARATION_MAPPING,
                "rework_declaration_lineage_unmappable",
                "05_governance/rework_declarations/rework_001.json",
                rework_content(),
            ),
        )
        for index, (failure_class, code, target, content) in enumerate(cases):
            with self.subTest(failure_class=failure_class):
                scenario = self.scenario(
                    f"trigger_{index}",
                    action=MockAgentAction(
                        writes=((CORRECTIVE_PROPOSAL_NAME, proposal(target, content)),),
                        cost_usd=0.25,
                        tokens_in=11,
                        tokens_out=13,
                    ),
                )
                target_path = scenario.project / target
                target_path.parent.mkdir(parents=True, exist_ok=True)
                if failure_class is CorrectiveFailureClass.REWORK_DECLARATION_MAPPING:
                    target_path.write_text(rework_content("M009-S99"), encoding="utf-8")
                first = self.invoke_direct(
                    scenario, failure_class, code, target
                )
                self.assertEqual(
                    (first.kind, first.detail),
                    ("acted", "architect_corrective_turn_applied"),
                )
                self.assertEqual(target_path.read_bytes(), content.encode("utf-8"))
                dispatches = [
                    event
                    for event in scenario.events()
                    if event.get("kind") == "dispatch"
                    and event.get("architect_corrective_turn") is True
                ]
                self.assertEqual(len(dispatches), 1)
                self.assertEqual(dispatches[0]["role"], "architect")
                self.assertEqual(dispatches[0]["effort"], "corrective")
                self.assertEqual(dispatches[0]["call_ceiling_seconds"], 17)
                self.assertEqual(dispatches[0]["call_ceiling_source"], "role")
                self.assertEqual(scenario.counters().architect_corrective_turns, 1)
                self.assertEqual(scenario.counters().total_cost_usd, 0.25)
                repeated = scenario.supervisor._architect_corrective_or_stop(
                    failure_class,
                    code,
                    target,
                    reason=StopReason.PROVIDER_FAILURE,
                    detail=f"original governed stop: {code}",
                    state=self.state_from_payload(),
                    slice_id="M001-S01",
                )
                self.assertEqual(repeated.kind, "stopped")
                self.assertEqual(len([
                    event for event in scenario.events()
                    if event.get("kind") == "dispatch"
                    and event.get("architect_corrective_turn") is True
                ]), 1)

    @staticmethod
    def state_from_payload():
        from frutlups_drive.planstate import parse_planning_state

        return parse_planning_state(payload())

    def test_prompt_generation_contract_refusal_routes_through_the_turn(self):
        corrected = "# Corrected Generated Prompt\n"
        scenario = self.scenario(
            "prompt_route",
            state=payload(step="make_coding_prompt"),
            action=MockAgentAction(
                writes=((CORRECTIVE_PROPOSAL_NAME, proposal(CODING_PROMPT, corrected)),)
            ),
        )
        scenario.supervisor._verb_writer = FailingPromptWriter()
        result = scenario.supervisor.tick()
        self.assertEqual(result.detail, "architect_corrective_turn_applied")
        self.assertEqual(
            (scenario.project / CODING_PROMPT).read_text(encoding="utf-8"),
            corrected,
        )

    def test_disabled_path_is_the_original_stop_without_dispatch(self):
        omitted_policy = (
            '[roles.architect]\nadapter = "mock"\n'
            '[roles.coder]\nadapter = "mock"\n'
            '[roles.reviewer]\nadapter = "mock"\n'
        )
        explicit_off_policy = (
            "architect_corrective_turn_enabled = false\n"
            "max_architect_corrective_turns_per_run = 1\n"
            + omitted_policy
        )
        scenarios = (
            self.scenario(
                "disabled_omitted",
                policy=omitted_policy,
                action=MockAgentAction(
                    writes=((CORRECTIVE_PROPOSAL_NAME, proposal(CODING_PROMPT, "# X\n")),)
                ),
            ),
            self.scenario(
                "disabled_explicit",
                policy=explicit_off_policy,
                action=MockAgentAction(
                    writes=((CORRECTIVE_PROPOSAL_NAME, proposal(CODING_PROMPT, "# X\n")),)
                ),
            ),
        )
        results = [
            self.invoke_direct(
                scenario,
                CorrectiveFailureClass.PROMPT_CONTRACT,
                "verb_dry_run_refused",
                CODING_PROMPT,
            )
            for scenario in scenarios
        ]
        for scenario, result in zip(scenarios, results):
            self.assertEqual(result.kind, "stopped")
            self.assertEqual(result.stop_reason, StopReason.PROVIDER_FAILURE)
            self.assertFalse(any(
                event.get("kind") == "architect_corrective_turn"
                for event in scenario.events()
            ))
            self.assertEqual(scenario.store.list_attempts("run_001", "M001-S01"), ())
        self.assertEqual(scenarios[0].events(), scenarios[1].events())
        self.assertEqual(
            results[0].escalation_path.read_bytes(),
            results[1].escalation_path.read_bytes(),
        )

    def test_guarded_writer_refuses_accepted_history_and_sidecars(self):
        project = self.root / "writer"
        target = project / CODING_PROMPT
        target.parent.mkdir(parents=True)
        target.write_text("# Accepted Prompt\n", encoding="utf-8")
        proposal_path = project / CORRECTIVE_PROPOSAL_NAME
        proposal_path.write_text(
            proposal(CODING_PROMPT, "# Replacement\n"), encoding="utf-8"
        )
        writer = CorrectiveProposalWriter(
            project,
            accepted_target=lambda path: path == CODING_PROMPT,
        )
        validated = writer.validate(
            proposal_path,
            expected_target=CODING_PROMPT,
            failure_class=CorrectiveFailureClass.PROMPT_CONTRACT,
        )
        with self.assertRaisesRegex(
            CorrectiveProposalRefusal, "proposal_target_accepted"
        ):
            writer.apply(validated)
        self.assertEqual(target.read_text(encoding="utf-8"), "# Accepted Prompt\n")

        sidecar = "05_governance/rework_declarations/rework_001.slices.yaml"
        proposal_path.write_text(
            proposal(sidecar, rework_content()), encoding="utf-8"
        )
        with self.assertRaisesRegex(
            CorrectiveProposalRefusal, "proposal_target_out_of_bounds"
        ):
            writer.validate(
                proposal_path,
                expected_target=sidecar,
                failure_class=CorrectiveFailureClass.REWORK_DECLARATION_MAPPING,
            )

    def test_invalid_and_out_of_bounds_proposals_preserve_evidence_and_stop(self):
        cases = (
            ("invalid", "not-json", "proposal_shape_invalid"),
            (
                "out_of_bounds",
                proposal("PROJECT_STATE.md", "# Wrong Target\n"),
                "proposal_target_out_of_bounds",
            ),
        )
        for name, document, refusal_code in cases:
            with self.subTest(name=name):
                scenario = self.scenario(
                    name,
                    action=MockAgentAction(
                        writes=((CORRECTIVE_PROPOSAL_NAME, document),)
                    ),
                )
                before = (scenario.project / CODING_PROMPT).read_bytes()
                result = self.invoke_direct(
                    scenario,
                    CorrectiveFailureClass.PROMPT_CONTRACT,
                    "verb_dry_run_refused",
                    CODING_PROMPT,
                )
                self.assertEqual(result.kind, "stopped")
                self.assertEqual(result.stop_reason, StopReason.PROVIDER_FAILURE)
                self.assertEqual((scenario.project / CODING_PROMPT).read_bytes(), before)
                validation = next(
                    event for event in scenario.events()
                    if event.get("kind") == "architect_corrective_turn"
                    and event.get("phase") == "validation"
                )
                self.assertFalse(validation["valid"])
                self.assertEqual(validation["refusal_code"], refusal_code)
                evidence = scenario.store.root / validation["evidence_path"]
                self.assertTrue(evidence.is_file())
                escalation = result.escalation_path.read_text(encoding="utf-8")
                self.assertIn("Architect Corrective Turn", escalation)
                self.assertIn(validation["evidence_path"], escalation)

    def test_per_run_cap_refuses_second_distinct_trigger(self):
        scenario = self.scenario(
            "cap",
            action=MockAgentAction(
                writes=((CORRECTIVE_PROPOSAL_NAME, proposal(CODING_PROMPT, "# First\n")),)
            ),
        )
        first = self.invoke_direct(
            scenario,
            CorrectiveFailureClass.PROMPT_CONTRACT,
            "verb_dry_run_refused",
            CODING_PROMPT,
        )
        self.assertEqual(first.kind, "acted")
        review = scenario.project / REVIEW_PROMPT
        review.write_text("# Review Prompt\n", encoding="utf-8")
        second = scenario.supervisor._architect_corrective_or_stop(
            CorrectiveFailureClass.PROMPT_CONTRACT,
            "verb_target_mismatch",
            REVIEW_PROMPT,
            reason=StopReason.PROVIDER_FAILURE,
            detail="original governed stop: verb_target_mismatch",
            state=self.state_from_payload(),
            slice_id="M001-S01",
        )
        self.assertEqual(second.kind, "stopped")
        self.assertEqual(scenario.counters().architect_corrective_turns, 1)
        cap = next(
            event for event in scenario.events()
            if event.get("kind") == "architect_corrective_turn"
            and event.get("phase") == "cap_refused"
        )
        self.assertEqual(cap["budget_meter"], "architect_corrective_turns")

    def test_product_verification_failure_never_dispatches_architect(self):
        scenario = Scenario(
            self.root / "product_failure",
            states=[payload()],
            coder=[
                MockAgentAction(writes=((SELF_REPORT, "# Coder Self-Report\n"),))
            ],
            architect=[
                MockAgentAction(
                    writes=((CORRECTIVE_PROPOSAL_NAME, proposal(CODING_PROMPT, "# X\n")),)
                )
            ],
            policy_body=CORRECTIVE_POLICY,
            verifier_exit_codes=[1],
        )
        result = scenario.supervisor.tick()
        self.assertEqual(result.detail, "verification_failed")
        self.assertFalse(any(
            event.get("kind") == "dispatch"
            and event.get("role") == "architect"
            for event in scenario.events()
        ))


if __name__ == "__main__":
    unittest.main()
