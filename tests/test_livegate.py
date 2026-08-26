"""Live-gate declaration assessment and bounded Markdown-loading lanes.

The surface is pure in-memory validation: synthetic declarations prove both
ready and non-ready assessments, secret-shaped keys and values refuse
without echo. The Phase C loader reads only its explicit bounded path; the
module still has no environment read or execution authority.
"""

import ast
import dataclasses
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

from frutlups_drive.livegate import (
    ALLOWED_STOP_CATEGORIES,
    EXTERNAL_ADAPTERS,
    REQUIRED_STOP_CATEGORIES,
    LiveGateAssessment,
    LiveGateDeclaration,
    LiveGateIssue,
    LiveGateLoadError,
    MAX_LIVE_GATE_FILE_BYTES,
    assess_live_gate,
    load_live_gate,
)

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src" / "frutlups_drive" / "livegate.py"
)


def valid_declaration():
    return {
        "approval_state": "approved",
        "approval_reference": (
            "05_governance/human_owner_notes/099_synthetic_live_gate.md"
        ),
        "coder_adapter": "api_call",
        "coder_model": "vendor-coder-model-1",
        "reviewer_adapter": "claude_cli",
        "reviewer_model": "vendor-reviewer-model-2",
        "architect_adapter": "codex_cli",
        "architect_model": "vendor-architect-model-3",
        "credential_env_names": ["PROVIDER_CODER_KEY", "PROVIDER_REVIEWER_KEY"],
        "max_total_cost_usd": 25.0,
        "max_call_cost_usd": 2.5,
        "call_timeout_seconds": 600,
        "rollback_statement": (
            "Discard the disposable worktree and keep the accepted baseline."
        ),
        "kill_switch_statement": (
            "Create .frutlups_drive/STOP; the next tick stops gracefully."
        ),
        "stop_conditions": {
            "cost": "Stop when total spend reaches the recorded ceiling.",
            "time": "Stop when the wall clock exceeds the recorded bound.",
            "human": "Stop on any owner instruction.",
        },
    }


class ReadyAssessmentTests(unittest.TestCase):
    def test_valid_synthetic_declaration_is_ready(self):
        assessment = assess_live_gate(valid_declaration())
        self.assertTrue(assessment.ready)
        self.assertEqual(assessment.issues, ())
        declaration = assessment.declaration
        self.assertIsInstance(declaration, LiveGateDeclaration)
        self.assertEqual(declaration.coder_model, "vendor-coder-model-1")
        self.assertEqual(
            declaration.architect_model, "vendor-architect-model-3"
        )
        self.assertEqual(declaration.call_timeout_seconds, 600.0)
        self.assertEqual(
            declaration.credential_env_names,
            ("PROVIDER_CODER_KEY", "PROVIDER_REVIEWER_KEY"),
        )
        self.assertEqual(len(declaration.stop_conditions), 3)
        self.assertIsNone(declaration.coder_corrective_effort)
        self.assertIsNone(declaration.reviewer_corrective_effort)
        self.assertIsNone(declaration.architect_corrective_effort)
        self.assertEqual(declaration.role_call_ceiling_seconds, ())
        self.assertEqual(declaration.slice_call_ceiling_overrides, ())
        self.assertFalse(declaration.architect_corrective_turn_enabled)
        self.assertEqual(declaration.max_architect_corrective_turns_per_run, 1)

    def test_present_valid_corrective_efforts_are_frozen_declaration_facts(self):
        source = valid_declaration()
        source.update(
            coder_adapter="codex_cli",
            coder_model="gpt-5.6-sol",
            coder_corrective_effort="high",
            reviewer_adapter="kimi_cli",
            reviewer_model="kimi-code/k3",
            reviewer_corrective_effort="high",
            architect_adapter="claude_cli",
            architect_model="claude-opus-5",
            architect_corrective_effort="xhigh",
        )
        assessment = assess_live_gate(source)
        self.assertTrue(assessment.ready, assessment.issues)
        self.assertEqual(assessment.declaration.coder_corrective_effort, "high")
        self.assertEqual(
            assessment.declaration.architect_corrective_effort, "xhigh"
        )

    def test_runtime_bindings_and_reporting_ceilings_are_typed_facts(self):
        source = valid_declaration()
        source.update(
            runtime_environment_bindings=[
                {"name": "JAVA_TOOL_OPTIONS", "value": "-Djava.io.tmpdir=.tmp"}
            ],
            reporting_currency="EUR",
            external_provider_ceilings=[
                {"provider": "codex_cli", "ceiling": 100}
            ],
        )
        assessment = assess_live_gate(source)
        self.assertTrue(assessment.ready, assessment.issues)
        declaration = assessment.declaration
        self.assertEqual(
            declaration.runtime_environment_bindings,
            (("JAVA_TOOL_OPTIONS", "-Djava.io.tmpdir=.tmp"),),
        )
        self.assertEqual(declaration.reporting_currency, "EUR")
        self.assertEqual(
            declaration.external_provider_ceilings,
            (("codex_cli", 100.0),),
        )

    def test_dispatch_call_ceilings_are_canonical_typed_facts(self):
        source = valid_declaration()
        source.update(
            role_call_ceiling_seconds={"reviewer": 45, "coder": 30},
            slice_call_ceiling_overrides=[
                {"slice_id": "M002-S01", "ceiling_seconds": 90},
                {"slice_id": "M001-S01", "ceiling_seconds": 75},
            ],
        )
        assessment = assess_live_gate(source)
        self.assertTrue(assessment.ready, assessment.issues)
        self.assertEqual(
            assessment.declaration.role_call_ceiling_seconds,
            (("coder", 30.0), ("reviewer", 45.0)),
        )
        self.assertEqual(
            assessment.declaration.slice_call_ceiling_overrides,
            (("M001-S01", 75.0), ("M002-S01", 90.0)),
        )

    def test_dispatch_call_ceiling_fields_fail_closed_when_present(self):
        cases = (
            (
                {"role_call_ceiling_seconds": {"shadow_reviewer": 30}},
                "role_call_ceiling_invalid",
            ),
            (
                {"role_call_ceiling_seconds": {"coder": 604_801}},
                "numeric_range_invalid",
            ),
            (
                {
                    "slice_call_ceiling_overrides": [
                        {"slice_id": "M001-S01", "ceiling_seconds": 30},
                        {"slice_id": "M001-S01", "ceiling_seconds": 45},
                    ]
                },
                "slice_call_ceiling_invalid",
            ),
            (
                {
                    "slice_call_ceiling_overrides": [
                        {
                            "slice_id": "M001-S01",
                            "ceiling_seconds": 30,
                            "extra": True,
                        }
                    ]
                },
                "field_type_invalid",
            ),
        )
        for updates, code in cases:
            with self.subTest(code=code):
                source = valid_declaration()
                source.update(updates)
                assessment = assess_live_gate(source)
                self.assertFalse(assessment.ready)
                self.assertIn(code, {issue.code for issue in assessment.issues})

    def test_architect_corrective_turn_fields_are_typed_and_bounded(self):
        source = valid_declaration()
        source.update(
            architect_corrective_turn_enabled=True,
            max_architect_corrective_turns_per_run=2,
        )
        assessment = assess_live_gate(source)
        self.assertTrue(assessment.ready, assessment.issues)
        self.assertTrue(
            assessment.declaration.architect_corrective_turn_enabled
        )
        self.assertEqual(
            assessment.declaration.max_architect_corrective_turns_per_run, 2
        )
        cases = (
            ({"architect_corrective_turn_enabled": "yes"}, "field_type_invalid"),
            ({"max_architect_corrective_turns_per_run": 0}, "numeric_range_invalid"),
            ({"max_architect_corrective_turns_per_run": 9}, "numeric_range_invalid"),
        )
        for updates, code in cases:
            with self.subTest(updates=updates):
                broken = valid_declaration()
                broken.update(updates)
                result = assess_live_gate(broken)
                self.assertFalse(result.ready)
                self.assertIn(code, {issue.code for issue in result.issues})

    def test_runtime_and_reporting_optional_fields_fail_closed_when_present(self):
        cases = (
            (
                {"runtime_environment_bindings": [{"name": "API_KEY", "value": "x"}]},
                "environment_binding_name_invalid",
            ),
            (
                {"runtime_environment_bindings": [{"name": "JAVA_TOOL_OPTIONS", "value": "token=DO-NOT-ECHO-123"}]},
                "environment_binding_value_invalid",
            ),
            ({"reporting_currency": "EUR"}, "reporting_declaration_incomplete"),
            (
                {
                    "reporting_currency": "EUR",
                    "external_provider_ceilings": [{"provider": "manual", "ceiling": 1}],
                },
                "provider_ceiling_invalid",
            ),
        )
        for updates, code in cases:
            with self.subTest(code=code):
                source = valid_declaration()
                source.update(updates)
                assessment = assess_live_gate(source)
                self.assertIn(code, {issue.code for issue in assessment.issues})

    def test_assessment_and_declaration_are_frozen(self):
        assessment = assess_live_gate(valid_declaration())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            assessment.ready = False
        with self.assertRaises(dataclasses.FrozenInstanceError):
            assessment.declaration.coder_model = "other"

    def test_vocabulary_constants_are_pinned(self):
        self.assertEqual(
            EXTERNAL_ADAPTERS,
            ("api_call", "claude_cli", "codex_cli", "kimi_cli"),
        )
        self.assertEqual(REQUIRED_STOP_CATEGORIES, ("cost", "time", "human"))
        self.assertEqual(
            ALLOWED_STOP_CATEGORIES,
            frozenset({"cost", "time", "human", "provider", "integrity"}),
        )

    def test_non_mapping_input_is_a_frozen_bounded_assessment(self):
        # R1-F3 reviewer-literal regression: ordinary loadable non-mapping
        # shapes never raise; each returns exactly the one bounded
        # declaration issue with no declaration and no echo.
        for bad in (["not", "a", "mapping"], ("tuple",), 5, 2.5, "text",
                    None, True, {"a", "b"}):
            with self.subTest(value=type(bad).__name__):
                assessment = assess_live_gate(bad)
                self.assertFalse(assessment.ready)
                self.assertIsNone(assessment.declaration)
                self.assertEqual(
                    assessment.issues,
                    (LiveGateIssue("field_type_invalid", "declaration"),),
                )


class IssueTestCase(unittest.TestCase):
    def assess(self, **overrides):
        source = valid_declaration()
        for key, value in overrides.items():
            if value is _ABSENT:
                source.pop(key, None)
            else:
                source[key] = value
        return assess_live_gate(source)

    def assert_issue(self, assessment, code, field):
        self.assertFalse(assessment.ready)
        self.assertIsNone(assessment.declaration)
        self.assertIn(LiveGateIssue(code, field), assessment.issues)
        return assessment


_ABSENT = object()


class NonReadyAssessmentTests(IssueTestCase):
    def test_missing_approval_is_an_issue(self):
        self.assert_issue(
            self.assess(approval_state="requested"),
            "approval_missing",
            "approval_state",
        )

    def test_every_required_field_is_required(self):
        for field in valid_declaration():
            with self.subTest(field=field):
                self.assert_issue(
                    self.assess(**{field: _ABSENT}), "field_missing", field
                )

    def test_approval_reference_must_be_repo_relative(self):
        slash = chr(47)
        backslash = chr(92)
        for bad in (
            slash.join(("C" + chr(58), "notes", "gate.md")),
            slash + slash.join(("abs", "gate.md")),
            "../gate.md",
            "a" + backslash + "b.md",
            "  ",
        ):
            with self.subTest(reference=repr(bad)):
                self.assert_issue(
                    self.assess(approval_reference=bad),
                    "approval_reference_invalid",
                    "approval_reference",
                )

    def test_identical_exact_seats_are_refused(self):
        assessment = self.assess(
            reviewer_adapter="api_call",
            reviewer_model="vendor-coder-model-1",
        )
        self.assert_issue(assessment, "identical_seats", "reviewer_model")

    def test_architect_equal_to_either_other_seat_is_refused(self):
        for adapter, model in (
            ("api_call", "vendor-coder-model-1"),
            ("claude_cli", "vendor-reviewer-model-2"),
        ):
            with self.subTest(adapter=adapter, model=model):
                assessment = self.assess(
                    architect_adapter=adapter,
                    architect_model=model,
                )
                self.assert_issue(
                    assessment, "identical_seats", "architect_model"
                )

    def test_different_exact_seats_are_not_a_family_claim(self):
        # Same adapter, different model strings: acceptable exact-seat
        # non-aliasing. Family independence is never computed here.
        assessment = self.assess(
            reviewer_adapter="api_call",
            reviewer_model="vendor-coder-model-2",
        )
        self.assertTrue(assessment.ready)

    def test_local_and_unknown_adapters_are_refused(self):
        self.assert_issue(
            self.assess(coder_adapter="mock"),
            "adapter_not_live",
            "coder_adapter",
        )
        self.assert_issue(
            self.assess(reviewer_adapter="manual"),
            "adapter_not_live",
            "reviewer_adapter",
        )
        self.assert_issue(
            self.assess(coder_adapter="direct_socket"),
            "adapter_unknown",
            "coder_adapter",
        )
        self.assert_issue(
            self.assess(architect_adapter="manual"),
            "adapter_not_live",
            "architect_adapter",
        )

    def test_blank_identities_are_refused(self):
        self.assert_issue(
            self.assess(coder_model="   "), "identity_missing", "coder_model"
        )

    def test_invalid_corrective_efforts_refuse_at_gate_admission(self):
        base = valid_declaration()
        base.update(
            coder_adapter="codex_cli",
            coder_model="gpt-5.6-sol",
            reviewer_adapter="kimi_cli",
            reviewer_model="kimi-code/k3",
            architect_adapter="claude_cli",
            architect_model="claude-opus-5",
        )
        cases = (
            ("coder_corrective_effort", "ultra", "provider_effort_unknown"),
            ("coder_corrective_effort", "max",
             "provider_corrective_effort_too_high"),
            ("coder_corrective_effort", "xhigh",
             "provider_corrective_effort_not_allowed"),
            ("reviewer_corrective_effort", "max",
             "provider_corrective_effort_unsupported"),
        )
        for field, effort, code in cases:
            with self.subTest(field=field, effort=effort):
                source = dict(base)
                source[field] = effort
                self.assert_issue(assess_live_gate(source), code, field)

    def test_bad_credential_names_are_refused(self):
        for bad_names in (["lower_case"], ["1STARTS_WITH_DIGIT"],
                          ["WITH SPACE"], ["GOOD_NAME", "bad name"]):
            with self.subTest(names=bad_names):
                self.assert_issue(
                    self.assess(credential_env_names=bad_names),
                    "credential_name_invalid",
                    "credential_env_names",
                )
        self.assert_issue(
            self.assess(credential_env_names=[]),
            "credential_names_missing",
            "credential_env_names",
        )
        self.assert_issue(
            self.assess(credential_env_names="PROVIDER_KEY"),
            "field_type_invalid",
            "credential_env_names",
        )

    def test_invalid_numbers_are_refused(self):
        for field in ("max_total_cost_usd", "max_call_cost_usd",
                      "call_timeout_seconds"):
            for bad, code in (
                (True, "field_type_invalid"),
                ("5", "field_type_invalid"),
                (None, "field_type_invalid"),
                (float("nan"), "numeric_range_invalid"),
                (float("inf"), "numeric_range_invalid"),
                (10**400, "numeric_range_invalid"),
                (-1.0, "numeric_range_invalid"),
            ):
                with self.subTest(field=field, value=repr(bad)):
                    self.assert_issue(
                        self.assess(**{field: bad}), code, field
                    )

    def test_timeout_must_be_positive_and_bounded(self):
        for bad in (0, 0.0, 86_401):
            with self.subTest(value=bad):
                self.assert_issue(
                    self.assess(call_timeout_seconds=bad),
                    "numeric_range_invalid",
                    "call_timeout_seconds",
                )

    def test_missing_rollback_kill_and_stop_facts_are_refused(self):
        self.assert_issue(
            self.assess(rollback_statement="  "),
            "statement_missing",
            "rollback_statement",
        )
        self.assert_issue(
            self.assess(kill_switch_statement=""),
            "statement_missing",
            "kill_switch_statement",
        )
        stops = valid_declaration()["stop_conditions"]
        stops.pop("human")
        self.assert_issue(
            self.assess(stop_conditions=stops),
            "stop_category_missing",
            "stop_conditions",
        )
        self.assert_issue(
            self.assess(stop_conditions=dict(stops, cost=" ", human="x")),
            "statement_missing",
            "stop_conditions",
        )
        self.assert_issue(
            self.assess(
                stop_conditions=dict(
                    valid_declaration()["stop_conditions"], surprise="x"
                )
            ),
            "stop_category_unknown",
            "stop_conditions",
        )
        self.assert_issue(
            self.assess(stop_conditions=["cost"]),
            "field_type_invalid",
            "stop_conditions",
        )


class TotalAdmissionTests(IssueTestCase):
    """R1-F3: the assessor is total over ordinary built-in loadable input —
    non-string keys and values reach bounded issues, never raw conversion,
    lookup, or validation exceptions."""

    def test_non_string_top_level_key_is_a_bounded_declaration_issue(self):
        source = valid_declaration()
        source[1] = "integer-keyed entry"
        assessment = assess_live_gate(source)
        self.assert_issue(assessment, "field_type_invalid", "declaration")
        self.assertNotIn("integer-keyed entry", repr(assessment))

    def test_integer_stop_key_is_a_bounded_stop_conditions_issue(self):
        # Reviewer-literal probe: an integer key inside stop_conditions
        # previously escaped as raw KeyError('1'); it must produce the
        # bounded stop-conditions issue without raising.
        stops = valid_declaration()["stop_conditions"]
        stops[1] = "integer-keyed stop statement"
        assessment = self.assess(stop_conditions=stops)
        self.assert_issue(assessment, "field_type_invalid", "stop_conditions")
        self.assertNotIn("integer-keyed stop statement", repr(assessment))

    def test_non_string_stop_value_is_a_bounded_stop_conditions_issue(self):
        stops = valid_declaration()["stop_conditions"]
        stops["human"] = 42
        assessment = self.assess(stop_conditions=stops)
        self.assert_issue(assessment, "field_type_invalid", "stop_conditions")

    def test_ordinary_malformed_shapes_never_escape_as_raw_exceptions(self):
        probes = []
        mixed_top = valid_declaration()
        mixed_top[1] = "x"
        mixed_top[("tuple", "key")] = "y"
        probes.append(mixed_top)
        mixed_stops = valid_declaration()
        mixed_stops["stop_conditions"] = {1: "a", "cost": 2, None: None,
                                          "human": "Stop.", "time": "Stop."}
        probes.append(mixed_stops)
        for field in ("credential_env_names", "stop_conditions",
                      "approval_state"):
            for bad in ([1, 2], (None,), 3.5, {"nested": {}}):
                broken = valid_declaration()
                broken[field] = bad
                probes.append(broken)
        for index, probe in enumerate(probes):
            with self.subTest(probe=index):
                assessment = assess_live_gate(probe)
                self.assertFalse(assessment.ready)
                self.assertIsNone(assessment.declaration)
                self.assertGreaterEqual(len(assessment.issues), 1)


class SecretHandlingTests(IssueTestCase):
    def test_unknown_secret_shaped_keys_refuse_without_echo(self):
        sentinel = "DO-NOT-ECHO-LIVE-GATE"
        for key in ("api_key", "accessToken", "clientSecret"):
            with self.subTest(key=key):
                assessment = self.assess(**{key: sentinel})
                self.assert_issue(assessment, "secret_shaped_field", key)
                self.assertNotIn(sentinel, repr(assessment))

    def test_empty_unknown_secret_shaped_key_is_only_unknown(self):
        assessment = self.assess(api_key="")
        self.assert_issue(assessment, "unknown_field", "api_key")
        self.assertNotIn(
            LiveGateIssue("secret_shaped_field", "api_key"), assessment.issues
        )

    def test_unknown_plain_keys_are_unknown_fields(self):
        self.assert_issue(
            self.assess(surprise=1), "unknown_field", "surprise"
        )

    def test_secret_shaped_values_refuse_without_echo(self):
        for field, value in (
            ("coder_model", "sk-abcdef1234567890"),
            ("rollback_statement", "use api_key=DO-NOT-ECHO-VALUE"),
            ("kill_switch_statement", "Bearer DO-NOT-ECHO-TOKEN"),
        ):
            with self.subTest(field=field):
                assessment = self.assess(**{field: value})
                self.assert_issue(assessment, "secret_shaped_value", field)
                self.assertNotIn("DO-NOT-ECHO", repr(assessment))
                self.assertNotIn("sk-abcdef", repr(assessment))

    def test_secret_shaped_stop_statement_is_refused(self):
        stops = valid_declaration()["stop_conditions"]
        stops["human"] = "stop token: DO-NOT-ECHO-EIGHTPLUS"
        assessment = self.assess(stop_conditions=stops)
        self.assert_issue(
            assessment, "secret_shaped_value", "stop_conditions"
        )


def gate_markdown(approval="approved", extra=""):
    return f'''# Test Gate

```toml
approval_state = "{approval}"
approval_reference = "05_governance/human_owner_notes/099_test.md"
coder_adapter = "codex_cli"
coder_model = "gpt-5.6-sol"
reviewer_adapter = "kimi_cli"
reviewer_model = "kimi-code/k3"
architect_adapter = "claude_cli"
architect_model = "claude-opus-5"
credential_env_names = ["USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA"]
max_total_cost_usd = 10.0
max_call_cost_usd = 2.0
call_timeout_seconds = 1800.0
rollback_statement = "Delete the disposable fixture."
kill_switch_statement = "Create the declared stop file."
{extra}
[stop_conditions]
cost = "Stop at the cost ceiling."
time = "Stop at the time ceiling."
human = "Stop on owner instruction."
```
'''


class GateLoaderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "live_validation_gate.md"

    def write(self, text):
        self.path.write_text(text, encoding="utf-8")

    def assert_load_error(self, code):
        with self.assertRaises(LiveGateLoadError) as caught:
            load_live_gate(self.path)
        self.assertEqual(caught.exception.code, code)

    def test_one_ready_toml_fence_is_loaded_and_hashed(self):
        self.write(gate_markdown())
        loaded = load_live_gate(self.path)
        self.assertTrue(loaded.assessment.ready)
        self.assertRegex(loaded.source_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(
            loaded.assessment.declaration.reviewer_model, "kimi-code/k3"
        )

    def test_unapproved_declaration_loads_as_not_ready(self):
        self.write(gate_markdown("proposed"))
        loaded = load_live_gate(self.path)
        self.assertFalse(loaded.assessment.ready)
        self.assertIn("approval_missing", {i.code for i in loaded.assessment.issues})

    def test_missing_duplicate_malformed_and_oversized_refuse(self):
        self.assert_load_error("gate_file_missing")
        cases = (
            (gate_markdown() + gate_markdown(), "gate_fence_count_invalid"),
            ("```toml\nnot = [valid\n```\n", "gate_toml_malformed"),
            ("x" * (MAX_LIVE_GATE_FILE_BYTES + 1), "gate_file_oversized"),
        )
        for text, code in cases:
            with self.subTest(code=code):
                self.write(text)
                self.assert_load_error(code)


class NoAuthorityTests(unittest.TestCase):
    def test_module_reads_no_environment_and_has_no_execution_surface(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        self.assertNotIn("os", imported)
        self.assertNotIn("subprocess", imported)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        attributes = {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
        }
        self.assertNotIn("environ", names | attributes)
        self.assertNotIn("Popen", names | attributes)

    def test_a_ready_assessment_carries_no_execution_hook(self):
        assessment = assess_live_gate(valid_declaration())
        field_names = {
            field.name for field in dataclasses.fields(LiveGateAssessment)
        }
        self.assertEqual(field_names, {"ready", "issues", "declaration"})
        self.assertFalse(
            any(callable(getattr(assessment, name)) for name in field_names),
            "an assessment holds facts only, never an executable authority",
        )


if __name__ == "__main__":
    unittest.main()
