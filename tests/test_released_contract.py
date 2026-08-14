"""Released frutlups wrapper contract lanes (M003-S02 Phase B).

The live provider consumes only the released atomic
``planning_frontier`` + ``loop_resume`` contract
(`02_analysis/cross_repo_convergence_contracts.md` §2). These final-form
tests drive the released-shape corpus through the parser and the real
provider transport; against the round-one Phase A bytes they are red because
the provisional provider refuses a real 0.1.0-shaped wrapper with
``planning_state_member_missing``.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

from frutlups_drive.contracts import LoopStep, PlanOutcome
from frutlups_drive.cli import CliRefusal, _admit_memory_mode
from frutlups_drive.frutlupscli import FrutlupsLaunchBinding
from frutlups_drive.planstate import (
    RELEASED_CONTRACT_ID,
    RELEASED_CONTRACT_VERSION,
    FrutlupsPlanProvider,
    PlanningStateRefusal,
    PlanProviderUnavailable,
    parse_memory_mode,
    parse_released_frontier,
)
from frutlups_drive.policy import SCHEMA_VERSION, load_execution_policy
from frutlups_drive.verifier import SubprocessRunner

from _scenario import FakeClock
from test_subprocess_agent import RecordingRunner

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "frontier"
STUB = str(Path(__file__).resolve().parent / "stub_frutlups_status.py")


def wrapper_fixture(name: str) -> dict:
    wrapper = json.loads((FIXTURES / name).read_bytes().decode("utf-8"))
    wrapper["memory_mode"] = memory_member()
    return wrapper


def memory_member(**overrides) -> dict:
    base = {
        "contract_id": "frutlups.memory_mode",
        "contract_version": "1",
        "valid": True,
        "mode": "none",
        "memory_root": None,
        "diagnostics": [],
    }
    base.update(overrides)
    return base


def frontier_member(**overrides) -> dict:
    base = {
        "contract_id": RELEASED_CONTRACT_ID,
        "contract_version": RELEASED_CONTRACT_VERSION,
        "outcome": "ready",
        "action": "",
        "actor": "",
        "block_citation": "",
        "block_owner": "",
        "completion_evidence": "",
        "diagnostics": [],
    }
    base.update(overrides)
    return base


def resume_member(**overrides) -> dict:
    base = {
        "step": "execute_coding_prompt",
        "message": "human text",
        "next_command": "python -m frutlups status <project> --json",
        "frontier_slice_id": "M001-S01",
        "frontier_slice_title": "first fixture slice",
        "coding_prompt_path": "prompts/for_coding_agent/001_first.md",
        "self_report_path": "05_governance/reviews/m001/m001_s01_self_report.md",
        "review_prompt_path": "",
        "review_report_path": "",
        "verdict_record_path": "",
        "diagnostics": [],
    }
    base.update(overrides)
    return base


SLICE_IDENTITIES = {"M001-S01": "M001", "M001-S02": "M001"}


def parse(frontier=None, resume=None, identities=SLICE_IDENTITIES):
    return parse_released_frontier(
        frontier if frontier is not None else frontier_member(),
        resume if resume is not None else resume_member(),
        identities,
    )


class ContractIdentityTests(unittest.TestCase):
    def test_exact_contract_identity_is_accepted(self):
        state = parse()
        self.assertEqual(state.outcome, PlanOutcome.READY)
        self.assertEqual(state.step, LoopStep.EXECUTE_CODING_PROMPT)

    def test_unknown_contract_id_is_refused_before_resume(self):
        with self.assertRaises(PlanningStateRefusal) as caught:
            parse(frontier=frontier_member(contract_id="frutlups.other"))
        self.assertEqual(caught.exception.code, "contract_version_refused")

    def test_contract_version_is_type_strict_string(self):
        for bad in (1, 1.0, True, None, "2", "1.0", ""):
            with self.subTest(version=repr(bad)):
                with self.assertRaises(PlanningStateRefusal):
                    parse(frontier=frontier_member(contract_version=bad))

    def test_refusal_precedes_resume_interpretation(self):
        # Unknown version plus an unknown step: the version refusal wins and
        # the resume member is never interpreted.
        with self.assertRaises(PlanningStateRefusal):
            parse(
                frontier=frontier_member(contract_version="99"),
                resume=resume_member(step="teleport"),
            )


class MemoryModeContractTests(unittest.TestCase):
    def test_all_canonical_modes_parse_to_typed_facts(self):
        for mode, root in (
            ("none", None),
            ("lightweight", None),
            ("llloom", "memory/llloom"),
        ):
            with self.subTest(mode=mode):
                fact = parse_memory_mode(
                    memory_member(mode=mode, memory_root=root)
                )
                self.assertEqual(fact.mode, mode)
                self.assertEqual(fact.memory_root, root)

    def test_shape_version_validity_mode_and_root_refuse(self):
        cases = (
            ({**memory_member(), "extra": True}, "memory_mode_shape_invalid"),
            (memory_member(contract_version="2"), "memory_mode_contract_refused"),
            (memory_member(valid=False), "memory_mode_invalid"),
            (memory_member(mode="auto"), "memory_mode_value_invalid"),
            (
                memory_member(mode="llloom", memory_root="../memory"),
                "memory_root_invalid",
            ),
            (memory_member(memory_root="memory"), "memory_root_unexpected"),
        )
        for member, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(PlanningStateRefusal) as caught:
                    parse_memory_mode(member)
                self.assertEqual(caught.exception.code, code)


class OutcomeStepTableTests(unittest.TestCase):
    READY_STEPS = (
        "make_coding_prompt",
        "execute_coding_prompt",
        "fix_self_report",
        "make_review_prompt",
        "execute_review_prompt",
        "fix_review_report",
        "record_verdict",
        "frontier_recorded",
    )

    def test_ready_accepts_exactly_the_eight_work_steps(self):
        for step in self.READY_STEPS:
            with self.subTest(step=step):
                state = parse(resume=resume_member(step=step))
                self.assertEqual(state.outcome, PlanOutcome.READY)
                self.assertEqual(state.step, LoopStep(step))

    def test_ready_with_no_frontier_is_invalid(self):
        state = parse(resume=resume_member(step="no_frontier"))
        self.assertEqual(state.outcome, PlanOutcome.INVALID)
        self.assertEqual(state.diagnostics[0].code, "step_combination_invalid")

    def test_needs_specification_requires_no_frontier(self):
        good = parse(
            frontier=frontier_member(outcome="needs_specification"),
            resume=resume_member(step="no_frontier"),
        )
        self.assertEqual(good.outcome, PlanOutcome.NEEDS_SPECIFICATION)
        bad = parse(
            frontier=frontier_member(outcome="needs_specification"),
            resume=resume_member(step="execute_coding_prompt"),
        )
        self.assertEqual(bad.outcome, PlanOutcome.INVALID)
        self.assertEqual(bad.diagnostics[0].code, "step_combination_invalid")

    def test_complete_requires_no_frontier_and_contained_evidence(self):
        good = parse(
            frontier=frontier_member(
                outcome="complete",
                completion_evidence=(
                    "05_governance/reviews/m001/closure_verdict_record.md"
                ),
            ),
            resume=resume_member(step="no_frontier"),
        )
        self.assertEqual(good.outcome, PlanOutcome.COMPLETE)
        self.assertEqual(
            good.completion_evidence.path,
            "05_governance/reviews/m001/closure_verdict_record.md",
        )
        missing = parse(
            frontier=frontier_member(outcome="complete"),
            resume=resume_member(step="no_frontier"),
        )
        self.assertEqual(missing.outcome, PlanOutcome.INVALID)
        self.assertEqual(
            missing.diagnostics[0].code, "completion_evidence_missing"
        )
        escaped = parse(
            frontier=frontier_member(
                outcome="complete", completion_evidence="../../outside.md"
            ),
            resume=resume_member(step="no_frontier"),
        )
        self.assertEqual(escaped.outcome, PlanOutcome.INVALID)
        self.assertEqual(escaped.diagnostics[0].code, "artifact_path_invalid")

    def test_blocked_requires_citation_and_owner(self):
        good = parse(
            frontier=frontier_member(
                outcome="blocked",
                block_citation=(
                    "05_governance/reviews/m001/m001_s01_round1_review_report.md"
                ),
                block_owner="human",
            ),
            resume=resume_member(step="record_verdict"),
        )
        self.assertEqual(good.outcome, PlanOutcome.BLOCKED)
        self.assertEqual(good.blocked.owner, "human")
        self.assertIsNone(good.verdict, "no synthesized verdict for blocked")
        for overrides in (
            {"block_citation": "", "block_owner": "human"},
            {
                "block_citation": (
                    "05_governance/reviews/m001/m001_s01_round1_review_report.md"
                ),
                "block_owner": "",
            },
        ):
            with self.subTest(overrides=overrides):
                state = parse(
                    frontier=frontier_member(outcome="blocked", **overrides),
                    resume=resume_member(step="record_verdict"),
                )
                self.assertEqual(state.outcome, PlanOutcome.INVALID)
                self.assertEqual(
                    state.diagnostics[0].code, "blocked_fields_missing"
                )

    def test_invalid_outcome_accepts_any_producer_step(self):
        for step in ("no_frontier", "fix_review_report", "record_verdict"):
            with self.subTest(step=step):
                state = parse(
                    frontier=frontier_member(
                        outcome="invalid",
                        diagnostics=["contradictory durable state"],
                    ),
                    resume=resume_member(step=step),
                )
                self.assertEqual(state.outcome, PlanOutcome.INVALID)

    def test_unknown_vocabulary_fails_closed(self):
        unknown_outcome = parse(frontier=frontier_member(outcome="paused"))
        self.assertEqual(unknown_outcome.outcome, PlanOutcome.INVALID)
        self.assertEqual(unknown_outcome.diagnostics[0].code, "unknown_outcome")
        unknown_step = parse(resume=resume_member(step="teleport"))
        self.assertEqual(unknown_step.outcome, PlanOutcome.INVALID)
        self.assertEqual(unknown_step.diagnostics[0].code, "unknown_step")

    def test_unknown_object_fields_are_tolerated(self):
        state = parse(
            frontier=frontier_member(extra_field={"nested": True}),
            resume=resume_member(another_extra=[1, 2, 3]),
        )
        self.assertEqual(state.outcome, PlanOutcome.READY)


class ResumeFactTests(unittest.TestCase):
    def test_empty_path_fields_become_absent_after_validation(self):
        state = parse(
            resume=resume_member(
                review_prompt_path="", review_report_path=""
            )
        )
        self.assertIsNone(state.artifacts.review_prompt)
        self.assertIsNone(state.artifacts.review_report)
        self.assertEqual(
            state.artifacts.coding_prompt,
            "prompts/for_coding_agent/001_first.md",
        )

    def test_invalid_path_grammar_fails_closed(self):
        for bad in (
            "../../escape.md",
            "C:" + "/abs/x.md",
            "a//b.md",
            "a" + chr(92) + "b.md",
        ):
            with self.subTest(path=repr(bad)):
                state = parse(resume=resume_member(coding_prompt_path=bad))
                self.assertEqual(state.outcome, PlanOutcome.INVALID)
                self.assertEqual(
                    state.diagnostics[0].code, "artifact_path_invalid"
                )

    def test_slice_identity_comes_from_structured_wrapper_entries(self):
        state = parse()
        self.assertEqual(state.frontier.slice_id, "M001-S01")
        self.assertEqual(state.frontier.milestone_id, "M001")
        self.assertEqual(state.frontier.round, 1, "run store owns the ladder")
        unmatched = parse(identities={})
        self.assertEqual(unmatched.outcome, PlanOutcome.INVALID)
        self.assertEqual(
            unmatched.diagnostics[0].code, "frontier_identity_unresolved"
        )

    def test_nonempty_frontier_requires_one_nonempty_milestone_owner(self):
        for identities in (
            {"M001-S01": ""},
            {"M001-S01": 7},
            {"M001-S02": "M001"},
        ):
            with self.subTest(identities=identities):
                state = parse(identities=identities)
                self.assertEqual(state.outcome, PlanOutcome.INVALID)
                self.assertEqual(
                    state.diagnostics[0].code,
                    "frontier_identity_unresolved",
                )

    def test_every_ready_step_requires_the_same_snapshot_owner(self):
        for step in OutcomeStepTableTests.READY_STEPS:
            with self.subTest(step=step):
                state = parse(resume=resume_member(step=step), identities={})
                self.assertEqual(state.outcome, PlanOutcome.INVALID)
                self.assertEqual(
                    state.diagnostics[0].code,
                    "frontier_identity_unresolved",
                )

    def test_empty_frontier_needs_no_milestone_owner(self):
        state = parse(
            frontier=frontier_member(outcome="needs_specification"),
            resume=resume_member(step="no_frontier", frontier_slice_id=""),
            identities={},
        )
        self.assertEqual(state.outcome, PlanOutcome.NEEDS_SPECIFICATION)
        self.assertIsNone(state.frontier)

    def test_next_command_and_message_are_never_carried(self):
        state = parse(
            resume=resume_member(
                next_command="python -m frutlups make-coding-prompt <project>",
                message="do the thing",
            )
        )
        self.assertIsNone(state.next_command)

    def test_producer_diagnostics_become_bounded_opaque_envelopes(self):
        state = parse(
            frontier=frontier_member(diagnostics=["declared loop step x"]),
            resume=resume_member(diagnostics=["y" * 5000]),
        )
        self.assertEqual(
            {d.code for d in state.diagnostics}, {"producer_diagnostic"}
        )
        self.assertTrue(all(len(d.message) <= 240 for d in state.diagnostics))

    def test_non_string_diagnostics_fail_closed(self):
        state = parse(frontier=frontier_member(diagnostics=[{"x": 1}]))
        self.assertEqual(state.outcome, PlanOutcome.INVALID)
        self.assertEqual(state.diagnostics[0].code, "field_type_invalid")


class CapturedCorpusTests(unittest.TestCase):
    def test_captured_ready_make_coding_prompt_wrapper_parses(self):
        wrapper = wrapper_fixture("ready_make_coding_prompt.json")
        identities = {
            entry["id"]: entry["milestone_id"]
            for entry in wrapper.get("slices", ())
            if isinstance(entry, dict)
        }
        state = parse_released_frontier(
            wrapper["planning_frontier"], wrapper["loop_resume"], identities
        )
        self.assertEqual(state.outcome, PlanOutcome.READY)
        self.assertEqual(state.step, LoopStep.MAKE_CODING_PROMPT)
        self.assertEqual(state.frontier.slice_id, "M001-S01")
        self.assertEqual(state.frontier.milestone_id, "M001")
        self.assertIsNone(state.next_command)


class ReleasedProviderTransportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.clock = FakeClock()

    def provider(self, *stub_args, runner=None, timeout=30.0):
        return FrutlupsPlanProvider(
            argv=(sys.executable, STUB, *stub_args),
            cwd=self.dir,
            capture_root=self.dir / "captures",
            timeout_seconds=timeout,
            runner=runner or SubprocessRunner(self.clock),
        )

    def test_real_captured_wrapper_parses_through_the_provider(self):
        # Red against the round-one Phase A bytes: the provisional provider
        # refuses this real 0.1.0-shaped wrapper with
        # planning_state_member_missing.
        path = self.dir / "released-0.1.2.json"
        path.write_text(
            json.dumps(wrapper_fixture("ready_make_coding_prompt.json")),
            encoding="utf-8",
        )
        state = self.provider("raw", str(path)).read_planning_state()
        self.assertEqual(state.outcome, PlanOutcome.READY)
        self.assertEqual(state.step, LoopStep.MAKE_CODING_PROMPT)

    def test_provider_rejects_missing_or_conflicting_same_snapshot_owner(self):
        for mode in ("missing", "conflicting"):
            with self.subTest(mode=mode):
                wrapper = wrapper_fixture("ready_make_coding_prompt.json")
                frontier_id = wrapper["loop_resume"]["frontier_slice_id"]
                if mode == "missing":
                    wrapper["slices"] = [
                        item
                        for item in wrapper["slices"]
                        if item.get("id") != frontier_id
                    ]
                else:
                    wrapper["slices"].append(
                        {"id": frontier_id, "milestone_id": "M999"}
                    )
                path = self.dir / f"{mode}.json"
                path.write_text(json.dumps(wrapper), encoding="utf-8")
                state = self.provider("raw", str(path)).read_planning_state()
                self.assertEqual(state.outcome, PlanOutcome.INVALID)
                self.assertEqual(
                    state.diagnostics[0].code,
                    "frontier_identity_unresolved",
                )

    def test_provider_converges_identical_repeated_owner_evidence(self):
        wrapper = wrapper_fixture("ready_make_coding_prompt.json")
        frontier_id = wrapper["loop_resume"]["frontier_slice_id"]
        original = next(
            item for item in wrapper["slices"] if item.get("id") == frontier_id
        )
        wrapper["slices"].append(dict(original))
        path = self.dir / "identical-owner.json"
        path.write_text(json.dumps(wrapper), encoding="utf-8")
        state = self.provider("raw", str(path)).read_planning_state()
        self.assertEqual(state.outcome, PlanOutcome.READY)
        self.assertEqual(state.frontier.milestone_id, original["milestone_id"])

    def test_missing_members_refuse_before_any_effect(self):
        runner = RecordingRunner()
        cases = {
            "missing-frontier-member": "planning_frontier_member_missing",
            "missing-resume-member": "loop_resume_member_missing",
            "non-object-frontier-member": "planning_frontier_member_invalid",
        }
        for mode, code in cases.items():
            with self.subTest(mode=mode):
                provider = self.provider(mode)
                with self.assertRaises(PlanProviderUnavailable) as caught:
                    provider.read_planning_state()
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(runner.calls, [])

    def test_legacy_provisional_member_is_not_a_live_fallback(self):
        # A wrapper carrying only the retired provisional planning_state
        # member must refuse: production live selection has no old-schema
        # fallback.
        provider = self.provider("legacy-planning-state")
        with self.assertRaises(PlanProviderUnavailable) as caught:
            provider.read_planning_state()
        self.assertEqual(
            caught.exception.code, "planning_frontier_member_missing"
        )


class MemoryModeAdmissionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "project"
        self.project.mkdir()
        policy_path = self.project / "frutlups_drive.toml"
        policy_path.write_text(
            f'schema_version = "{SCHEMA_VERSION}"\n'
            '[frutlups]\nprovider = "frutlups_cli"\n',
            encoding="utf-8",
        )
        self.policy = load_execution_policy(policy_path).policy
        self.wrapper_path = self.project / "status.json"

    def binding(self):
        return FrutlupsLaunchBinding(
            argv_prefix=(
                sys.executable,
                STUB,
                "raw",
                str(self.wrapper_path),
            ),
            env=(("PYTHONDONTWRITEBYTECODE", "1"),),
            tool_identity="frutlups==0.1.2",
            binding_sha256="a" * 64,
            executable_sha256="b" * 64,
        )

    def write_wrapper(self, memory):
        wrapper = wrapper_fixture("ready_make_coding_prompt.json")
        if memory is None:
            wrapper.pop("memory_mode")
        else:
            wrapper["memory_mode"] = memory
        self.wrapper_path.write_text(json.dumps(wrapper), encoding="utf-8")

    def test_invalid_or_missing_declaration_refuses_before_run_store(self):
        cases = (
            (None, "memory_mode_member_missing"),
            (
                memory_member(extra="unknown"),
                "memory_mode_shape_invalid",
            ),
            (
                memory_member(valid=False, diagnostics=["invalid fixture"]),
                "memory_mode_invalid",
            ),
        )
        for memory, code in cases:
            with self.subTest(code=code):
                self.write_wrapper(memory)
                with self.assertRaises(CliRefusal) as caught:
                    _admit_memory_mode(
                        self.project,
                        self.policy,
                        self.binding(),
                        "run_001",
                    )
                self.assertEqual(caught.exception.code, code)
                self.assertFalse((self.project / ".frutlups_drive").exists())

    def test_admission_binds_manifest_facts_and_later_mode_stability(self):
        self.write_wrapper(memory_member())
        mode, provider = _admit_memory_mode(
            self.project,
            self.policy,
            self.binding(),
            "run_001",
        )
        self.assertEqual(
            mode.manifest_facts(),
            {"memory_mode": "none", "memory_root": ""},
        )
        self.assertFalse((self.project / ".frutlups_drive").exists())
        self.write_wrapper(
            memory_member(mode="llloom", memory_root="memory/llloom")
        )
        with self.assertRaises(PlanProviderUnavailable) as caught:
            provider.read_planning_state()
        self.assertEqual(caught.exception.code, "memory_mode_changed")


if __name__ == "__main__":
    unittest.main()
