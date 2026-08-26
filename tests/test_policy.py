"""Execution-policy parser tests: schema, defaults, warnings, refusals."""

import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

from frutlups_drive.policy import (
    SCHEMA_VERSION,
    PolicyRefusal,
    load_execution_policy,
)

FULL_POLICY = """\
schema_version = "frutlups_drive_policy_v1"
architect_corrective_turn_enabled = false
max_architect_corrective_turns_per_run = 1

[target]
stop_at = "milestone_complete"
max_slices = 25
max_passes = 3

[roles.architect]
adapter = "manual"
model = ""
workspace_access = "read_only"

[roles.coder]
adapter = "mock"
model = "claude-fable-5"
workspace_access = "workspace_write"
resume_within_slice = true
resume_across_slices = false

[roles.reviewer]
adapter = "manual"
model = ""
workspace_access = "read_only"
fresh_session_per_invocation = true

[roles.shadow_reviewer]
enabled = false
adapter = "mock"
model = ""
workspace_access = "read_only"

[autonomy]
max_strictness_level = 3
auto_continue_past_frontier_recorded = false
pass_boundary = "human_gate"

[limits]
max_coder_attempts_per_slice = 3
max_report_repairs = 2
max_reconciliations_without_progress = 2
max_total_cost_usd = 0.0
max_wall_clock_minutes = 360
max_consecutive_provider_failures = 2
max_consecutive_no_progress = 3
provider_backoff_seconds = [1.0, 2.0, 4.0]
watch_poll_seconds = 0.05
max_run_store_bytes = 67108864
max_retained_runs = 25
max_shadow_attempts_per_slice = 1

[git]
worktree_per_slice = false
commit = "never"
pull_request = "never"

[network]
default = "deny"

[memory]
follow_project_state = true
on_read_failure = "continue_without_memory"
downgrade_flag_allowed = true
upgrade_via_flag = false

[frutlups]
provider = "mock"
contract_id = "frutlups.planning_frontier"
contract_version = "1"
package_identity = "frutlups==0.1.0"
layout_config = "frutlups.layout.yaml"
read_verbs = "status"
write_verbs = "declare-rework make-coding-prompt make-review-prompt record-verdict"
timeout_seconds = 120
max_stream_bytes = 1048576
binding_path = "local_state/frutlups_binding.toml"
"""

_FIELD_COUNT = 54


class PolicyTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write_policy(self, text: str) -> Path:
        path = self.dir / "frutlups_drive.toml"
        path.write_bytes(text.encode("utf-8"))
        return path

    def load(self, text: str):
        return load_execution_policy(self.write_policy(text))

    def assert_refused(self, text: str, code: str) -> PolicyRefusal:
        with self.assertRaises(PolicyRefusal) as caught:
            self.load(text)
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class ValidPolicyTests(PolicyTestCase):
    def test_full_policy_round_trip(self):
        result = self.load(FULL_POLICY)
        policy = result.policy
        self.assertEqual(policy.schema_version, SCHEMA_VERSION)
        self.assertFalse(policy.architect_corrective_turn_enabled)
        self.assertEqual(policy.max_architect_corrective_turns_per_run, 1)
        self.assertEqual(policy.index_mode, "human-ledger")
        self.assertIsNone(policy.campaign_id)
        self.assertEqual(policy.target.stop_at, "milestone_complete")
        self.assertEqual(policy.target.max_slices, 25)
        self.assertEqual(policy.target.max_passes, 3)
        self.assertEqual(policy.architect.adapter, "manual")
        self.assertEqual(policy.architect.model, "")
        self.assertEqual(policy.architect.workspace_access, "read_only")
        self.assertIsNone(policy.architect.corrective_effort)
        self.assertEqual(policy.coder.adapter, "mock")
        self.assertEqual(policy.coder.model, "claude-fable-5")
        self.assertEqual(policy.coder.workspace_access, "workspace_write")
        self.assertTrue(policy.coder.resume_within_slice)
        self.assertFalse(policy.coder.resume_across_slices)
        self.assertIsNone(policy.coder.corrective_effort)
        self.assertEqual(policy.reviewer.adapter, "manual")
        self.assertTrue(policy.reviewer.fresh_session_per_invocation)
        self.assertIsNone(policy.reviewer.corrective_effort)
        self.assertFalse(policy.shadow_reviewer.enabled)
        self.assertEqual(policy.shadow_reviewer.adapter, "mock")
        self.assertEqual(policy.shadow_reviewer.workspace_access, "read_only")
        self.assertEqual(policy.autonomy.max_strictness_level, 3)
        self.assertFalse(policy.autonomy.auto_continue_past_frontier_recorded)
        self.assertEqual(policy.autonomy.pass_boundary, "human_gate")
        self.assertEqual(policy.limits.max_coder_attempts_per_slice, 3)
        self.assertEqual(policy.limits.max_report_repairs, 2)
        self.assertEqual(policy.limits.max_reconciliations_without_progress, 2)
        self.assertEqual(policy.limits.max_total_cost_usd, 0.0)
        self.assertEqual(policy.limits.max_wall_clock_minutes, 360)
        self.assertEqual(policy.limits.max_consecutive_provider_failures, 2)
        self.assertEqual(policy.limits.max_consecutive_no_progress, 3)
        self.assertEqual(policy.limits.provider_backoff_seconds, (1.0, 2.0, 4.0))
        self.assertEqual(policy.limits.watch_poll_seconds, 0.05)
        self.assertEqual(policy.limits.max_run_store_bytes, 64 * 1024 * 1024)
        self.assertEqual(policy.limits.max_retained_runs, 25)
        self.assertEqual(policy.limits.max_shadow_attempts_per_slice, 1)
        self.assertFalse(policy.architect_corrective_turn_enabled)
        self.assertEqual(policy.max_architect_corrective_turns_per_run, 1)

        self.assertFalse(policy.git.worktree_per_slice)
        self.assertEqual(policy.git.commit, "never")
        self.assertEqual(policy.git.pull_request, "never")
        self.assertEqual(policy.network.default, "deny")
        self.assertTrue(policy.memory.follow_project_state)
        self.assertEqual(policy.memory.on_read_failure, "continue_without_memory")
        self.assertTrue(policy.memory.downgrade_flag_allowed)
        self.assertFalse(policy.memory.upgrade_via_flag)
        self.assertEqual(policy.frutlups.provider, "mock")
        self.assertEqual(
            policy.frutlups.contract_id, "frutlups.planning_frontier"
        )
        self.assertEqual(policy.frutlups.contract_version, "1")
        self.assertEqual(policy.frutlups.read_verbs, "status")
        self.assertEqual(
            policy.frutlups.write_verbs,
            "declare-rework make-coding-prompt make-review-prompt record-verdict",
        )
        self.assertEqual(
            policy.frutlups.binding_path, "local_state/frutlups_binding.toml"
        )
        self.assertEqual(result.defaulted, ())
        self.assertEqual(result.warnings, ())

    def test_campaign_id_is_optional_and_bounded(self):
        absent = self.load(f'schema_version = "{SCHEMA_VERSION}"\n')
        declared = self.load(
            f'schema_version = "{SCHEMA_VERSION}"\n'
            'campaign_id = "external-eval.v5"\n'
        )
        self.assertIsNone(absent.policy.campaign_id)
        self.assertNotIn("campaign_id", absent.defaulted)
        self.assertEqual(declared.policy.campaign_id, "external-eval.v5")
        for value in ("", "space value", "x" * 65):
            with self.subTest(value=value):
                self.assert_refused(
                    f'schema_version = "{SCHEMA_VERSION}"\n'
                    f'campaign_id = "{value}"\n',
                    "field_value_invalid",
                )

    def test_architect_corrective_turn_enablement_and_cap_are_bounded(self):
        enabled = self.load(
            f'schema_version = "{SCHEMA_VERSION}"\n'
            "architect_corrective_turn_enabled = true\n"
            "max_architect_corrective_turns_per_run = 2\n"
        ).policy
        self.assertTrue(enabled.architect_corrective_turn_enabled)
        self.assertEqual(enabled.max_architect_corrective_turns_per_run, 2)
        cases = (
            (0, "numeric_range_invalid"),
            (9, "numeric_range_invalid"),
            (True, "field_type_invalid"),
        )
        for value, code in cases:
            with self.subTest(value=value):
                literal = str(value).lower() if isinstance(value, bool) else str(value)
                self.assert_refused(
                    f'schema_version = "{SCHEMA_VERSION}"\n'
                    f"max_architect_corrective_turns_per_run = {literal}\n",
                    code,
                )

    def test_two_clean_pass_boundary_is_an_explicit_opt_in(self):
        result = self.load(
            f'schema_version = "{SCHEMA_VERSION}"\n'
            '[autonomy]\npass_boundary = "two_clean"\n'
        )
        self.assertEqual(result.policy.autonomy.pass_boundary, "two_clean")

    def test_index_mode_absent_and_declared_values(self):
        absent = self.load(f'schema_version = "{SCHEMA_VERSION}"\n')
        human = self.load(
            f'schema_version = "{SCHEMA_VERSION}"\n'
            'index_mode = "human-ledger"\n'
        )
        no_ledger = self.load(
            f'schema_version = "{SCHEMA_VERSION}"\n'
            'index_mode = "no-ledger"\n'
        )

        self.assertEqual(absent.policy.index_mode, "human-ledger")
        self.assertEqual(human.policy.index_mode, "human-ledger")
        self.assertEqual(no_ledger.policy.index_mode, "no-ledger")
        self.assertNotIn("index_mode", absent.defaulted)
        self.assertEqual(human.warnings, ())
        self.assertEqual(no_ledger.warnings, ())

    def test_shadow_reviewer_is_explicit_read_only_and_bounded(self):
        result = self.load(
            f'schema_version = "{SCHEMA_VERSION}"\n'
            "[roles.shadow_reviewer]\n"
            "enabled = true\n"
            'adapter = "manual"\n'
            'model = "second-seat"\n'
            "[limits]\nmax_shadow_attempts_per_slice = 2\n"
        )
        self.assertTrue(result.policy.shadow_reviewer.enabled)
        self.assertEqual(result.policy.shadow_reviewer.adapter, "manual")
        self.assertEqual(result.policy.shadow_reviewer.workspace_access, "read_only")
        self.assertEqual(result.policy.limits.max_shadow_attempts_per_slice, 2)

    def test_minimal_policy_takes_every_named_default(self):
        result = self.load(f'schema_version = "{SCHEMA_VERSION}"\n')
        self.assertEqual(len(result.defaulted), _FIELD_COUNT)
        self.assertIn("limits.max_total_cost_usd", result.defaulted)
        self.assertIn("target.max_slices", result.defaulted)
        policy = result.policy
        self.assertEqual(policy.target.stop_at, "milestone_complete")
        self.assertEqual(policy.target.max_slices, 25)
        self.assertEqual(policy.coder.adapter, "manual")
        self.assertEqual(policy.coder.workspace_access, "workspace_write")
        self.assertEqual(policy.limits.max_total_cost_usd, 0.0)
        self.assertEqual(policy.limits.max_wall_clock_minutes, 360)
        self.assertEqual(policy.limits.max_consecutive_no_progress, 3)
        self.assertEqual(policy.limits.provider_backoff_seconds, (1.0, 2.0, 4.0))
        self.assertEqual(policy.autonomy.max_strictness_level, 3)
        self.assertEqual(policy.git.commit, "never")
        self.assertEqual(policy.git.pull_request, "never")
        self.assertEqual(policy.network.default, "deny")
        self.assertFalse(policy.memory.upgrade_via_flag)
        self.assertEqual(policy.frutlups.package_identity, "")
        self.assertEqual(policy.index_mode, "human-ledger")
        self.assertNotIn("index_mode", result.defaulted)
        self.assertFalse(policy.shadow_reviewer.enabled)
        self.assertEqual(policy.limits.max_shadow_attempts_per_slice, 1)

    def test_partial_section_journals_only_omitted_fields(self):
        result = self.load(
            f'schema_version = "{SCHEMA_VERSION}"\n'
            "[limits]\n"
            "max_total_cost_usd = 1.5\n"
        )
        self.assertNotIn("limits.max_total_cost_usd", result.defaulted)
        self.assertIn("limits.max_report_repairs", result.defaulted)
        self.assertEqual(result.policy.limits.max_total_cost_usd, 1.5)

    def test_valid_corrective_efforts_load_without_becoming_defaults(self):
        result = self.load(
            f'schema_version = "{SCHEMA_VERSION}"\n'
            '[roles.architect]\nadapter = "claude_cli"\n'
            'model = "claude-opus-5"\ncorrective_effort = "xhigh"\n'
            '[roles.coder]\nadapter = "codex_cli"\n'
            'model = "gpt-5.6-sol"\ncorrective_effort = "high"\n'
            '[roles.reviewer]\nadapter = "kimi_cli"\n'
            'model = "kimi-code/k3"\ncorrective_effort = "high"\n'
        )
        self.assertEqual(result.policy.architect.corrective_effort, "xhigh")
        self.assertEqual(result.policy.coder.corrective_effort, "high")
        self.assertEqual(result.policy.reviewer.corrective_effort, "high")
        self.assertNotIn("roles.coder.corrective_effort", result.defaulted)

    def test_money_accepts_toml_integer(self):
        result = self.load(
            f'schema_version = "{SCHEMA_VERSION}"\n'
            "[limits]\n"
            "max_total_cost_usd = 0\n"
        )
        self.assertEqual(result.policy.limits.max_total_cost_usd, 0.0)
        self.assertIsInstance(result.policy.limits.max_total_cost_usd, float)

    def test_continuous_operation_bounds_are_typed_data(self):
        result = self.load(
            f'schema_version = "{SCHEMA_VERSION}"\n'
            "[limits]\n"
            "provider_backoff_seconds = [0, 0.5, 3]\n"
            "watch_poll_seconds = 0.25\n"
            "max_run_store_bytes = 4096\n"
            "max_retained_runs = 2\n"
        )
        self.assertEqual(
            result.policy.limits.provider_backoff_seconds, (0.0, 0.5, 3.0)
        )
        self.assertEqual(result.policy.limits.watch_poll_seconds, 0.25)
        self.assertEqual(result.policy.limits.max_run_store_bytes, 4096)
        self.assertEqual(result.policy.limits.max_retained_runs, 2)

    def test_optional_dispatch_environment_and_reporting_declarations(self):
        result = self.load(
            f'schema_version = "{SCHEMA_VERSION}"\n'
            'runtime_environment_bindings = [{name = "JAVA_TOOL_OPTIONS", value = "-Djava.io.tmpdir=.tmp"}]\n'
            '[dispatch]\n'
            'role_call_ceiling_seconds = {coder = 2400, reviewer = 900}\n'
            'slice_call_ceiling_overrides = [{slice_id = "M009-S03", ceiling_seconds = 7200}]\n'
            'scientific_subprocess_budget_seconds = 5400\n'
            'capture_truncation_disposition = "tolerate"\n'
            '[reporting]\n'
            'currency = "EUR"\n'
            'external_provider_ceilings = [{provider = "codex_cli", ceiling = 100}]\n'
        )
        policy = result.policy
        self.assertEqual(
            policy.runtime_environment_bindings,
            (("JAVA_TOOL_OPTIONS", "-Djava.io.tmpdir=.tmp"),),
        )
        self.assertEqual(
            policy.dispatch.call_ceiling("coder", "M001-S01"),
            (2400.0, "role"),
        )
        self.assertEqual(
            policy.dispatch.call_ceiling("coder", "M009-S03"),
            (7200.0, "slice"),
        )
        self.assertEqual(policy.dispatch.scientific_subprocess_budget_seconds, 5400.0)
        self.assertEqual(policy.dispatch.capture_truncation_disposition, "tolerate")
        self.assertEqual(policy.reporting.currency, "EUR")
        self.assertEqual(policy.reporting.external_provider_ceilings, (("codex_cli", 100.0),))
        for optional in (
            "runtime_environment_bindings",
            "dispatch.role_call_ceiling_seconds",
            "dispatch.slice_call_ceiling_overrides",
            "dispatch.scientific_subprocess_budget_seconds",
            "dispatch.capture_truncation_disposition",
            "reporting.currency",
            "reporting.external_provider_ceilings",
        ):
            self.assertNotIn(optional, result.defaulted)

    def test_unknown_keys_warn_but_load(self):
        result = self.load(
            f'schema_version = "{SCHEMA_VERSION}"\n'
            "surprise = 1\n"
            "[target]\n"
            "foo = 1\n"
            "[roles.janitor]\n"
            'adapter = "manual"\n'
        )
        warned_keys = sorted(w.key for w in result.warnings)
        self.assertEqual(warned_keys, ["roles.janitor", "surprise", "target.foo"])
        for warning in result.warnings:
            self.assertEqual(warning.code, "unknown_key")

    def test_empty_secret_shaped_value_is_tolerated_as_unknown_key(self):
        result = self.load(
            f'schema_version = "{SCHEMA_VERSION}"\ntoken = ""\n'
        )
        self.assertEqual([w.key for w in result.warnings], ["token"])


class PolicyRefusalTests(PolicyTestCase):
    def test_absent_file_is_a_refusal(self):
        with self.assertRaises(PolicyRefusal) as caught:
            load_execution_policy(self.dir / "does_not_exist.toml")
        self.assertEqual(caught.exception.code, "policy_file_missing")

    def test_malformed_toml(self):
        self.assert_refused("schema_version = \n", "malformed_toml")

    def test_schema_version_missing(self):
        self.assert_refused("[target]\nmax_slices = 1\n", "schema_version_missing")

    def test_schema_version_unknown_is_refused_not_best_effort(self):
        self.assert_refused(
            'schema_version = "frutlups_drive_policy_v2"\n',
            "schema_version_unknown",
        )

    def test_enum_type_and_range_refusals(self):
        header = f'schema_version = "{SCHEMA_VERSION}"\n'
        cases = [
            ('index_mode = "inferred"\n', "enum_value_unknown"),
            ("index_mode = true\n", "field_type_invalid"),
            ('[target]\nstop_at = "when_done"\n', "enum_value_unknown"),
            ('[roles.coder]\nadapter = "gpt_cli"\n', "enum_value_unknown"),
            ('[roles.coder]\nworkspace_access = "admin"\n', "enum_value_unknown"),
            ('[autonomy]\npass_boundary = "none"\n', "enum_value_unknown"),
            ('[memory]\non_read_failure = "fail"\n', "enum_value_unknown"),
            ("[target]\nmax_slices = -1\n", "numeric_range_invalid"),
            ("[limits]\nmax_total_cost_usd = -0.5\n", "numeric_range_invalid"),
            ("[limits]\nmax_total_cost_usd = inf\n", "numeric_range_invalid"),
            ("[limits]\nmax_total_cost_usd = nan\n", "numeric_range_invalid"),
            ("[autonomy]\nmax_strictness_level = 5\n", "numeric_range_invalid"),
            ("[autonomy]\nmax_strictness_level = 0\n", "numeric_range_invalid"),
            ('[target]\nmax_slices = "25"\n', "field_type_invalid"),
            ("[target]\nmax_slices = true\n", "field_type_invalid"),
            ('[roles.coder]\nresume_within_slice = "yes"\n', "field_type_invalid"),
            ("[roles.coder]\nmodel = 3\n", "field_type_invalid"),
            ("target = 5\n", "field_type_invalid"),
            ("[limits]\nprovider_backoff_seconds = []\n", "field_type_invalid"),
            ("[limits]\nprovider_backoff_seconds = [1, 0]\n", "numeric_range_invalid"),
            ("[limits]\nprovider_backoff_seconds = [301]\n", "numeric_range_invalid"),
            ("[limits]\nprovider_backoff_seconds = [true]\n", "field_type_invalid"),
            ("[limits]\nwatch_poll_seconds = 0\n", "numeric_range_invalid"),
            ("[limits]\nwatch_poll_seconds = 61\n", "numeric_range_invalid"),
            ("[limits]\nmax_run_store_bytes = 0\n", "numeric_range_invalid"),
            ("[limits]\nmax_retained_runs = 0\n", "numeric_range_invalid"),
            ("[limits]\nmax_shadow_attempts_per_slice = -1\n", "numeric_range_invalid"),
        ]
        for body, code in cases:
            with self.subTest(body=body):
                self.assert_refused(header + body, code)

    def test_invalid_corrective_efforts_refuse_at_policy_load(self):
        header = f'schema_version = "{SCHEMA_VERSION}"\n'
        cases = (
            ('[roles.coder]\nadapter = "codex_cli"\nmodel = "gpt-5.6-sol"\n'
             'corrective_effort = "ultra"\n', "provider_effort_unknown"),
            ('[roles.coder]\nadapter = "codex_cli"\nmodel = "gpt-5.6-sol"\n'
             'corrective_effort = "max"\n',
             "provider_corrective_effort_too_high"),
            ('[roles.coder]\nadapter = "codex_cli"\nmodel = "gpt-5.6-sol"\n'
             'corrective_effort = "xhigh"\n',
             "provider_corrective_effort_not_allowed"),
            ('[roles.reviewer]\nadapter = "kimi_cli"\nmodel = "kimi-code/k3"\n'
             'corrective_effort = "max"\n',
             "provider_corrective_effort_unsupported"),
            ('[roles.coder]\nadapter = "manual"\ncorrective_effort = "high"\n',
             "provider_corrective_effort_not_applicable"),
        )
        for body, code in cases:
            with self.subTest(code=code):
                self.assert_refused(header + body, code)

    def test_fixed_policy_boundaries(self):
        header = f'schema_version = "{SCHEMA_VERSION}"\n'
        cases = [
            '[git]\ncommit = "on_pass"\n',
            '[git]\npull_request = "on_close"\n',
            '[network]\ndefault = "allow"\n',
            "[memory]\nupgrade_via_flag = true\n",
            '[roles.shadow_reviewer]\nworkspace_access = "workspace_write"\n',
            # The sanctioned frutlups surface is a fixed boundary:
            # orchestrator-run and other verbs can never be declared.
            '[frutlups]\nwrite_verbs = "orchestrator-run"\n',
            '[frutlups]\nread_verbs = "status next"\n',
            '[frutlups]\ncontract_id = "frutlups.other"\n',
            '[frutlups]\ncontract_version = "2"\n',
        ]
        for body in cases:
            with self.subTest(body=body):
                self.assert_refused(header + body, "fixed_boundary_violation")

    def test_frutlups_provider_vocabulary_is_closed(self):
        self.assert_refused(
            f'schema_version = "{SCHEMA_VERSION}"\n'
            '[frutlups]\nprovider = "orchestrator_run"\n',
            "enum_value_unknown",
        )

    def test_frutlups_paths_are_canonical_and_binding_stays_in_local_state(self):
        header = f'schema_version = "{SCHEMA_VERSION}"\n'
        cases = (
            ('[frutlups]\nlayout_config = "../escape.yaml"\n',
             "field_value_invalid"),
            ('[frutlups]\nlayout_config = "a' + chr(92) * 2 + 'b.yaml"\n',
             "field_value_invalid"),
            ('[frutlups]\nbinding_path = "elsewhere/binding.toml"\n',
             "fixed_boundary_violation"),
            ('[frutlups]\nbinding_path = "local_state/../binding.toml"\n',
             "field_value_invalid"),
        )
        for body, code in cases:
            with self.subTest(body=body):
                self.assert_refused(header + body, code)

    def test_frutlups_transport_bounds_are_positive(self):
        header = f'schema_version = "{SCHEMA_VERSION}"\n'
        for key in ("timeout_seconds", "max_stream_bytes"):
            with self.subTest(key=key):
                self.assert_refused(
                    header + f"[frutlups]\n{key} = 0\n",
                    "numeric_range_invalid",
                )

    def test_new_optional_declarations_fail_closed_when_present(self):
        header = f'schema_version = "{SCHEMA_VERSION}"\n'
        cases = (
            ('runtime_environment_bindings = [{name = "API_KEY", value = "x"}]\n', "environment_binding_name_invalid"),
            ('runtime_environment_bindings = [{name = "JAVA_TOOL_OPTIONS", value = "token=DO-NOT-ECHO-123"}]\n', "environment_binding_value_invalid"),
            ('[dispatch]\nrole_call_ceiling_seconds = {janitor = 5}\n', "field_type_invalid"),
            ('[dispatch]\nslice_call_ceiling_overrides = [{slice_id = "../bad", ceiling_seconds = 5}]\n', "field_value_invalid"),
            ('[dispatch]\ncapture_truncation_disposition = "ignore"\n', "enum_value_unknown"),
            ('[reporting]\ncurrency = "EUR"\n', "reporting_declaration_incomplete"),
            ('[reporting]\ncurrency = "eur"\nexternal_provider_ceilings = [{provider = "codex_cli", ceiling = 1}]\n', "field_value_invalid"),
        )
        for body, code in cases:
            with self.subTest(code=code):
                self.assert_refused(header + body, code)


class SecretRefusalTests(PolicyTestCase):
    def test_secret_shaped_values_are_refused_without_echo(self):
        header = f'schema_version = "{SCHEMA_VERSION}"\n'
        cases = [
            ('[roles.coder]\napi_key = "sk-not-a-real-value"\n',
             "roles.coder.api_key", "sk-not-a-real-value"),
            ('[provider]\nauth_token = "abc123"\n',
             "provider.auth_token", "abc123"),
            ('[a.b]\npassword = "hunter2"\n', "a.b.password", "hunter2"),
        ]
        for body, key, value in cases:
            with self.subTest(key=key):
                refusal = self.assert_refused(header + body, "secret_shaped_value")
                self.assertIn(key, refusal.message)
                self.assertNotIn(value, refusal.message)
                self.assertNotIn(value, str(refusal))

    def test_secret_shaped_table_is_refused(self):
        refusal = self.assert_refused(
            f'schema_version = "{SCHEMA_VERSION}"\n'
            "[credentials]\n"
            'user = "someone"\n',
            "secret_shaped_value",
        )
        self.assertIn("credentials", refusal.message)
        self.assertNotIn("someone", refusal.message)

    def test_camelcase_and_acronym_secret_keys_are_refused(self):
        # F2 regression: hard-coded ordinary credential-key forms; not
        # generated from the production token vocabulary.
        header = f'schema_version = "{SCHEMA_VERSION}"\n'
        sentinel = "DO-NOT-ECHO-123"
        cases = [
            (f'apiKey = "{sentinel}"\n', "apiKey"),
            (f'clientSecret = "{sentinel}"\n', "clientSecret"),
            (f'accessToken = "{sentinel}"\n', "accessToken"),
            (f'sessionToken = "{sentinel}"\n', "sessionToken"),
            (f'APIKey = "{sentinel}"\n', "APIKey"),
            (f'OAuthToken = "{sentinel}"\n', "OAuthToken"),
            (f'ServicePassword = "{sentinel}"\n', "ServicePassword"),
            (f'[provider]\nclientSecret = "{sentinel}"\n',
             "provider.clientSecret"),
        ]
        for body, key in cases:
            with self.subTest(key=key):
                refusal = self.assert_refused(header + body, "secret_shaped_value")
                self.assertIn(key, refusal.message)
                self.assertNotIn(sentinel, refusal.message)
                self.assertNotIn(sentinel, str(refusal))

    def test_secret_keys_refused_recursively_in_arrays_of_tables(self):
        header = f'schema_version = "{SCHEMA_VERSION}"\n'
        sentinel = "DO-NOT-ECHO-456"
        cases = [
            # inline-table array member
            f'wrap = [{{clientSecret = "{sentinel}"}}]\n',
            # array-of-tables member
            f'[[worker]]\naccessToken = "{sentinel}"\n',
            # nested plain table
            f'[a.b.c]\ndeployPassword = "{sentinel}"\n',
        ]
        for body in cases:
            with self.subTest(body=body):
                refusal = self.assert_refused(header + body, "secret_shaped_value")
                self.assertNotIn(sentinel, refusal.message)
                self.assertNotIn(sentinel, str(refusal))

    def test_near_match_keys_stay_unknown_key_warnings(self):
        # F2 regression: benign near-matches must not trip the secret refusal.
        result = self.load(
            f'schema_version = "{SCHEMA_VERSION}"\n'
            'tokenizer = "byte-pair"\n'
            'secretariat = "office"\n'
            'authorship = "coder"\n'
            'passwordless = "enabled"\n'
            'monkey = "wrench"\n'
        )
        warned = sorted(w.key for w in result.warnings)
        self.assertEqual(
            warned,
            ["authorship", "monkey", "passwordless", "secretariat", "tokenizer"],
        )
        for warning in result.warnings:
            self.assertEqual(warning.code, "unknown_key")

    def test_empty_camelcase_secret_value_keeps_empty_contract(self):
        result = self.load(
            f'schema_version = "{SCHEMA_VERSION}"\nclientSecret = ""\n'
        )
        self.assertEqual([w.key for w in result.warnings], ["clientSecret"])

    def test_secrets_refused_even_when_schema_version_is_unknown(self):
        refusal = self.assert_refused(
            'schema_version = "not_a_known_schema"\n'
            '[keys]\nservice = "value-that-must-not-leak"\n',
            "secret_shaped_value",
        )
        self.assertNotIn("value-that-must-not-leak", refusal.message)


if __name__ == "__main__":
    unittest.main()
