"""Planning-state v1 parser, fixture corpus, and mock provider tests."""

import dataclasses
import json
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

from frutlups_drive.contracts import LoopStep, PlanOutcome
from frutlups_drive.planstate import (
    ACCEPTED_CONTRACT,
    ACCEPTED_VERSION,
    MAX_PLANNING_STATE_BYTES,
    ArtifactPaths,
    Diagnostic,
    Frontier,
    MockPlanProvider,
    MockScriptExhausted,
    PlanningStateRefusal,
    parse_planning_state,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "planstate"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def payload(**overrides) -> bytes:
    base = {
        "contract": "frutlups_planning_state",
        "version": 1,
        "outcome": "ready",
        "step": "execute_coding_prompt",
        "actor": "coder",
        "artifacts": {},
        "diagnostics": [],
    }
    base.update(overrides)
    return json.dumps(base).encode("utf-8")


class ValidFixtureTests(unittest.TestCase):
    READY_FIXTURES = {
        "valid_ready_no_frontier.json": LoopStep.NO_FRONTIER,
        "valid_ready_make_coding_prompt.json": LoopStep.MAKE_CODING_PROMPT,
        "valid_ready_execute_coding_prompt.json": LoopStep.EXECUTE_CODING_PROMPT,
        "valid_ready_fix_self_report.json": LoopStep.FIX_SELF_REPORT,
        "valid_ready_make_review_prompt.json": LoopStep.MAKE_REVIEW_PROMPT,
        "valid_ready_execute_review_prompt.json": LoopStep.EXECUTE_REVIEW_PROMPT,
        "valid_ready_fix_review_report.json": LoopStep.FIX_REVIEW_REPORT,
        "valid_ready_record_verdict.json": LoopStep.RECORD_VERDICT,
        "valid_ready_frontier_recorded.json": LoopStep.FRONTIER_RECORDED,
    }

    def test_ready_fixtures_cover_all_nine_loop_steps(self):
        self.assertEqual(
            set(self.READY_FIXTURES.values()),
            set(LoopStep),
            "the ready fixture corpus must cover every loop step",
        )
        for name, step in self.READY_FIXTURES.items():
            with self.subTest(fixture=name):
                state = parse_planning_state(fixture_bytes(name))
                self.assertEqual(state.outcome, PlanOutcome.READY)
                self.assertEqual(state.step, step)

    def test_execute_coding_prompt_full_payload(self):
        state = parse_planning_state(
            fixture_bytes("valid_ready_execute_coding_prompt.json")
        )
        self.assertEqual(state.outcome, PlanOutcome.READY)
        self.assertEqual(state.step, LoopStep.EXECUTE_CODING_PROMPT)
        self.assertEqual(state.actor, "coder")
        self.assertEqual(state.gate_state, "open")
        self.assertEqual(
            state.frontier,
            Frontier(
                milestone_id="M001",
                slice_id="M001-S01",
                slice_title="Package scaffold",
                round=1,
            ),
        )
        self.assertEqual(
            state.artifacts,
            ArtifactPaths(
                coding_prompt=(
                    "prompts/for_coding_agent/001_m001_s01_package_scaffold.md"
                ),
                self_report="05_governance/reviews/m001/m001_s01_self_report.md",
                review_prompt=None,
                review_report=None,
                verdict_record=None,
            ),
        )
        self.assertIsNone(state.verdict)
        self.assertIsNone(state.blocked)
        self.assertIsNone(state.completion_evidence)
        self.assertEqual(state.diagnostics, ())
        self.assertEqual(state.next_command, "python -m frutlups status . --json")

    def test_needs_specification_fixture(self):
        state = parse_planning_state(fixture_bytes("valid_needs_specification.json"))
        self.assertEqual(state.outcome, PlanOutcome.NEEDS_SPECIFICATION)
        self.assertIsNone(state.step)
        self.assertEqual(state.actor, "architect")

    def test_blocked_fixture_carries_citation_and_owner(self):
        state = parse_planning_state(fixture_bytes("valid_blocked.json"))
        self.assertEqual(state.outcome, PlanOutcome.BLOCKED)
        self.assertEqual(state.blocked.citation, "05_governance/decision_log.md")
        self.assertEqual(state.blocked.owner, "Julian")

    def test_complete_fixture_carries_evidence(self):
        state = parse_planning_state(fixture_bytes("valid_complete.json"))
        self.assertEqual(state.outcome, PlanOutcome.COMPLETE)
        self.assertEqual(
            state.completion_evidence.path,
            "05_governance/reviews/m001/"
            "roadmap_frutlups_drive_closure_verdict_record.md",
        )

    def test_invalid_fixture_keeps_producer_diagnostics_verbatim(self):
        state = parse_planning_state(fixture_bytes("valid_invalid.json"))
        self.assertEqual(state.outcome, PlanOutcome.INVALID)
        self.assertEqual(
            state.diagnostics,
            (
                Diagnostic(
                    severity="error",
                    code="state_drift",
                    message=(
                        "PROJECT_STATE.md milestone claim contradicts "
                        "roadmap projection"
                    ),
                ),
                Diagnostic(
                    severity="info",
                    code="recompute_hint",
                    message=(
                        "re-run frutlups status after reconciling the projection"
                    ),
                ),
            ),
        )

    def test_record_verdict_fixture_parses_verdict(self):
        state = parse_planning_state(fixture_bytes("valid_ready_record_verdict.json"))
        self.assertEqual(state.verdict.value, "pass")
        self.assertEqual(
            state.verdict.report,
            "05_governance/reviews/m001/m001_s01_round1_review_report.md",
        )

    def test_unknown_extra_fields_tolerated_at_every_level(self):
        state = parse_planning_state(fixture_bytes("unknown_extra_fields.json"))
        self.assertEqual(state.outcome, PlanOutcome.READY)
        self.assertEqual(state.step, LoopStep.EXECUTE_CODING_PROMPT)
        self.assertEqual(
            state.frontier,
            Frontier(
                milestone_id="M001",
                slice_id="M001-S01",
                slice_title="Package scaffold",
                round=1,
            ),
        )
        self.assertEqual(
            state.artifacts,
            ArtifactPaths(
                coding_prompt=(
                    "prompts/for_coding_agent/001_m001_s01_package_scaffold.md"
                ),
                self_report="05_governance/reviews/m001/m001_s01_self_report.md",
                review_prompt=None,
                review_report=None,
                verdict_record=None,
            ),
        )
        self.assertEqual(
            state.diagnostics,
            (Diagnostic(severity="info", code="advisory", message="advisory only"),),
        )


class RefusalTests(unittest.TestCase):
    def assert_refused(self, data: bytes):
        with self.assertRaises(PlanningStateRefusal) as caught:
            parse_planning_state(data)
        self.assertEqual(caught.exception.code, "contract_version_refused")
        self.assertIn(ACCEPTED_CONTRACT, caught.exception.message)
        self.assertNotIn("Traceback", caught.exception.message)
        return caught.exception

    def test_refusal_fixtures(self):
        for name in (
            "unknown_version.json",
            "unknown_contract.json",
            "missing_contract.json",
        ):
            with self.subTest(fixture=name):
                self.assert_refused(fixture_bytes(name))

    def test_version_check_is_type_strict(self):
        self.assert_refused(payload(version="1"))
        self.assert_refused(payload(version=True))
        self.assert_refused(payload(version=1.0))

    def test_newer_version_refused_never_best_effort(self):
        self.assert_refused(payload(version=ACCEPTED_VERSION + 1))

    def test_refusal_precedes_field_validation(self):
        # Unknown version plus unknown outcome: the version refusal wins,
        # nothing else is interpreted.
        self.assert_refused(payload(version=99, outcome="paused"))


class InvalidStateTests(unittest.TestCase):
    def assert_invalid(self, data: bytes, code: str):
        state = parse_planning_state(data)
        self.assertEqual(state.outcome, PlanOutcome.INVALID)
        self.assertEqual(len(state.diagnostics), 1)
        diagnostic = state.diagnostics[0]
        self.assertEqual(diagnostic.severity, "error")
        self.assertEqual(diagnostic.code, code)
        self.assertNotIn("Traceback", diagnostic.message)
        self.assertNotIn("\\", diagnostic.message)
        return state

    def test_invalid_fixtures(self):
        cases = {
            "unknown_outcome.json": "unknown_outcome",
            "unknown_step.json": "unknown_step",
            "blocked_without_owner.json": "blocked_owner_missing",
            "complete_without_evidence.json": "completion_evidence_missing",
            "malformed_json.json": "malformed_json",
        }
        for name, code in cases.items():
            with self.subTest(fixture=name):
                self.assert_invalid(fixture_bytes(name), code)

    def test_unknown_enum_values_fail_closed(self):
        cases = {
            "actor": payload(actor="janitor"),
            "severity": payload(
                diagnostics=[{"severity": "fatal", "code": "x", "message": "y"}]
            ),
            "verdict": payload(
                verdict={"value": "approved", "next_move": "n", "report": "r"}
            ),
        }
        codes = {
            "actor": "unknown_actor",
            "severity": "unknown_severity",
            "verdict": "unknown_verdict",
        }
        for field, data in cases.items():
            with self.subTest(field=field):
                self.assert_invalid(data, codes[field])

    def test_structural_violations(self):
        with self.subTest(case="top level not an object"):
            self.assert_invalid(b"[1, 2]", "not_an_object")
        with self.subTest(case="ready without step"):
            self.assert_invalid(payload(step=None), "step_missing")
        with self.subTest(case="blocked with empty owner"):
            self.assert_invalid(
                payload(outcome="blocked", step=None,
                        blocked={"citation": "c", "owner": ""}),
                "blocked_owner_missing",
            )
        with self.subTest(case="blocked with null blocked"):
            self.assert_invalid(
                payload(outcome="blocked", step=None), "blocked_owner_missing"
            )
        with self.subTest(case="complete with empty path"):
            self.assert_invalid(
                payload(outcome="complete", step=None,
                        completion_evidence={"path": ""}),
                "artifact_path_invalid",
            )
        with self.subTest(case="frontier round is boolean"):
            self.assert_invalid(
                payload(frontier={"milestone_id": "M001", "slice_id": "S",
                                  "slice_title": "T", "round": True}),
                "field_type_invalid",
            )
        with self.subTest(case="frontier round is string"):
            self.assert_invalid(
                payload(frontier={"milestone_id": "M001", "slice_id": "S",
                                  "slice_title": "T", "round": "1"}),
                "field_type_invalid",
            )
        with self.subTest(case="outcome missing"):
            self.assert_invalid(payload(outcome=None), "field_type_invalid")

    def test_input_size_limit_is_exact(self):
        base = fixture_bytes("valid_ready_execute_coding_prompt.json")
        at_limit = base + b" " * (MAX_PLANNING_STATE_BYTES - len(base))
        self.assertEqual(len(at_limit), MAX_PLANNING_STATE_BYTES)
        state = parse_planning_state(at_limit)
        self.assertEqual(state.outcome, PlanOutcome.READY)
        self.assert_invalid(at_limit + b" ", "input_too_large")


class NonStandardJsonConstantTests(unittest.TestCase):
    """F1 regression: NaN/Infinity/-Infinity are not JSON and must fail closed."""

    def test_nonstandard_constants_rejected_as_malformed_json(self):
        # Hand-built raw payloads: json.dumps cannot produce these tokens.
        for token in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(token=token):
                raw = (
                    '{"contract": "frutlups_planning_state", "version": 1, '
                    '"outcome": "ready", "step": "no_frontier", '
                    '"unknown_extra": ' + token + "}"
                ).encode("utf-8")
                state = parse_planning_state(raw)
                self.assertEqual(state.outcome, PlanOutcome.INVALID)
                self.assertEqual(state.diagnostics[0].code, "malformed_json")

    def test_nonstandard_constant_in_known_field_rejected(self):
        raw = (
            '{"contract": "frutlups_planning_state", "version": 1, '
            '"outcome": "ready", "step": "no_frontier", '
            '"frontier": {"milestone_id": "M001", "slice_id": "S", '
            '"slice_title": "T", "round": NaN}}'
        ).encode("utf-8")
        state = parse_planning_state(raw)
        self.assertEqual(state.diagnostics[0].code, "malformed_json")


class ArtifactReferenceGrammarTests(unittest.TestCase):
    """F1 regression: every known artifact reference obeys the bounded
    repo-relative POSIX file-path grammar."""

    # Test-owned literals; not generated from the implementation.
    INVALID_REFERENCES = (
        "../../escape.md",
        "..",
        ".",
        "/absolute/report.md",
        "//server/share/report.md",
        "C:/evil/report.md",
        "C:\\evil\\report.md",
        "C:report.md",
        "a\\b.md",
        "\\\\server\\share\\report.md",
        "",
        "   ",
        "docs/./note.md",
        "docs/../note.md",
        "prompts/",
        "a//b.md",
        "a\x00b.md",
    )
    VALID_REFERENCES = (
        "README.md",
        "prompts/for_coding_agent/001_m001_s01_package_scaffold.md",
        "05_governance/reviews/m001/m001_s01_self_report.md",
    )
    ARTIFACT_FIELDS = (
        "coding_prompt",
        "self_report",
        "review_prompt",
        "review_report",
        "verdict_record",
    )

    def test_invalid_references_fail_closed_for_every_artifact_field(self):
        for field in self.ARTIFACT_FIELDS:
            for reference in self.INVALID_REFERENCES:
                with self.subTest(field=field, reference=repr(reference)):
                    state = parse_planning_state(
                        payload(artifacts={field: reference})
                    )
                    self.assertEqual(state.outcome, PlanOutcome.INVALID)
                    self.assertEqual(
                        state.diagnostics[0].code, "artifact_path_invalid"
                    )

    def test_valid_nested_references_are_positive_controls(self):
        for field in self.ARTIFACT_FIELDS:
            for reference in self.VALID_REFERENCES:
                with self.subTest(field=field, reference=reference):
                    state = parse_planning_state(
                        payload(artifacts={field: reference})
                    )
                    self.assertEqual(state.outcome, PlanOutcome.READY)
                    self.assertEqual(getattr(state.artifacts, field), reference)

    def test_completion_evidence_path_uses_the_same_grammar(self):
        for reference in ("   ", "../../fake.md", "/absolute/fake.md",
                          "C:\\fake.md", "evidence/"):
            with self.subTest(reference=repr(reference)):
                state = parse_planning_state(
                    payload(outcome="complete", step=None,
                            completion_evidence={"path": reference})
                )
                self.assertEqual(state.outcome, PlanOutcome.INVALID)
                self.assertEqual(
                    state.diagnostics[0].code, "artifact_path_invalid"
                )
        control = parse_planning_state(
            payload(outcome="complete", step=None,
                    completion_evidence={
                        "path": "05_governance/reviews/m001/closure.md"
                    })
        )
        self.assertEqual(control.outcome, PlanOutcome.COMPLETE)

    def test_verdict_report_reference_uses_the_same_grammar(self):
        state = parse_planning_state(
            payload(verdict={"value": "pass", "next_move": "advance",
                             "report": "../../outside_report.md"})
        )
        self.assertEqual(state.outcome, PlanOutcome.INVALID)
        self.assertEqual(state.diagnostics[0].code, "artifact_path_invalid")
        control = parse_planning_state(
            payload(verdict={"value": "pass", "next_move": "advance",
                             "report": "05_governance/reviews/m001/r.md"})
        )
        self.assertEqual(control.outcome, PlanOutcome.READY)


class OutcomeFieldCombinationTests(unittest.TestCase):
    """F1 regression: outcome/step and blocked/complete combinations."""

    def test_non_ready_outcomes_require_null_step(self):
        cases = {
            "needs_specification": {},
            "blocked": {"blocked": {"citation": "05_governance/decision_log.md",
                                    "owner": "Julian"}},
            "complete": {"completion_evidence":
                         {"path": "05_governance/reviews/m001/closure.md"}},
            "invalid": {},
        }
        for outcome, extra in cases.items():
            with self.subTest(outcome=outcome):
                state = parse_planning_state(
                    payload(outcome=outcome, step="execute_coding_prompt",
                            **extra)
                )
                self.assertEqual(state.outcome, PlanOutcome.INVALID)
                self.assertEqual(state.diagnostics[0].code, "step_forbidden")

    def test_blocked_owner_must_contain_non_whitespace_text(self):
        state = parse_planning_state(
            payload(outcome="blocked", step=None,
                    blocked={"citation": "05_governance/decision_log.md",
                             "owner": "   "})
        )
        self.assertEqual(state.outcome, PlanOutcome.INVALID)
        self.assertEqual(state.diagnostics[0].code, "blocked_owner_missing")


class RepresentationTests(unittest.TestCase):
    def test_planning_state_is_frozen(self):
        state = parse_planning_state(
            fixture_bytes("valid_ready_execute_coding_prompt.json")
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            state.outcome = PlanOutcome.COMPLETE
        with self.assertRaises(dataclasses.FrozenInstanceError):
            state.artifacts.self_report = "elsewhere.md"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            state.frontier.round = 2

    def test_diagnostics_are_immutable_tuples(self):
        state = parse_planning_state(fixture_bytes("valid_invalid.json"))
        self.assertIsInstance(state.diagnostics, tuple)


class MockPlanProviderTests(unittest.TestCase):
    def test_replays_scripted_sequence_in_order(self):
        provider = MockPlanProvider(
            [
                fixture_bytes("valid_ready_execute_coding_prompt.json"),
                fixture_bytes("valid_blocked.json"),
            ]
        )
        first = provider.read_planning_state()
        second = provider.read_planning_state()
        self.assertEqual(first.outcome, PlanOutcome.READY)
        self.assertEqual(second.outcome, PlanOutcome.BLOCKED)

    def test_exhausted_script_raises_loudly(self):
        provider = MockPlanProvider([])
        with self.assertRaises(MockScriptExhausted):
            provider.read_planning_state()

    def test_scripted_refusal_surfaces_at_read_time(self):
        provider = MockPlanProvider([fixture_bytes("unknown_version.json")])
        with self.assertRaises(PlanningStateRefusal):
            provider.read_planning_state()


if __name__ == "__main__":
    unittest.main()
