"""M003 Phase A role-seat identity lanes.

Each runtime role carries its exact configured adapter/model pair through
request construction, the write-once request record, journal dispatch facts,
and replay. Seat comparison is exact string equality — never model-family
inference — and the runtime privileges no development-model name.
"""

import re
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

from frutlups_drive.contracts import Role
from frutlups_drive.dispatch.mock import MockAgentAction
from frutlups_drive.policy import SCHEMA_VERSION, load_execution_policy
from frutlups_drive.supervisor import (
    EXTERNAL_ADAPTERS,
    LOCAL_ADAPTERS,
    RoleSeat,
    exact_seat_alias,
    policy_seat,
    seat_executable_issue,
)

from _scenario import (
    DEFAULT_VERBS,
    ROADMAP_BODY,
    REVIEW_REPORT,
    SELF_REPORT,
    Scenario,
    clean_pass_states,
)

PKG_SRC = Path(__file__).resolve().parents[1] / "src"

SEAT_POLICY = """\
[roles.architect]
adapter = "mock"
model = ""

[roles.coder]
adapter = "mock"
model = "model-alpha"

[roles.reviewer]
adapter = "mock"
model = "model-beta"
"""


class SeatFactTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def load_policy(self, body):
        path = self.dir / "frutlups_drive.toml"
        path.write_bytes(
            (f'schema_version = "{SCHEMA_VERSION}"\n' + body).encode("utf-8")
        )
        return load_execution_policy(path).policy

    def test_adapter_classes_partition_the_policy_vocabulary(self):
        self.assertEqual(LOCAL_ADAPTERS, ("manual", "mock"))
        self.assertEqual(
            EXTERNAL_ADAPTERS,
            ("api_call", "claude_cli", "codex_cli", "kimi_cli"),
        )

    def test_policy_seat_reads_the_exact_configured_pair_per_role(self):
        policy = self.load_policy(SEAT_POLICY)
        self.assertEqual(
            policy_seat(policy, Role.CODER),
            RoleSeat(Role.CODER, "mock", "model-alpha"),
        )
        self.assertEqual(
            policy_seat(policy, Role.REVIEWER),
            RoleSeat(Role.REVIEWER, "mock", "model-beta"),
        )
        self.assertEqual(
            policy_seat(policy, Role.ARCHITECT),
            RoleSeat(Role.ARCHITECT, "mock", ""),
        )

    def test_local_adapters_accept_the_frozen_empty_model_convention(self):
        for adapter in LOCAL_ADAPTERS:
            with self.subTest(adapter=adapter):
                self.assertIsNone(
                    seat_executable_issue(RoleSeat(Role.CODER, adapter, ""))
                )

    def test_external_class_blank_model_is_a_stable_issue(self):
        for adapter in EXTERNAL_ADAPTERS:
            for model in ("", "   "):
                with self.subTest(adapter=adapter, model=repr(model)):
                    self.assertEqual(
                        seat_executable_issue(
                            RoleSeat(Role.CODER, adapter, model)
                        ),
                        "external_model_missing",
                    )
        self.assertIsNone(
            seat_executable_issue(
                RoleSeat(Role.CODER, "api_call", "some-model")
            )
        )

    def test_exact_seat_alias_is_string_equality_only(self):
        base = RoleSeat(Role.CODER, "api_call", "vendor-model-1")
        cases = [
            (RoleSeat(Role.REVIEWER, "api_call", "vendor-model-1"), True),
            (RoleSeat(Role.REVIEWER, "api_call", "vendor-model-2"), False),
            (RoleSeat(Role.REVIEWER, "claude_cli", "vendor-model-1"), False),
            # No case normalization, no marketing-alias resolution, no
            # family inference in either direction.
            (RoleSeat(Role.REVIEWER, "api_call", "Vendor-Model-1"), False),
            (RoleSeat(Role.REVIEWER, "api_call", "vendor-model"), False),
            (RoleSeat(Role.REVIEWER, "api_call", "vendor-model-1 "), False),
        ]
        for other, expected in cases:
            with self.subTest(other=other.model, adapter=other.adapter):
                self.assertIs(exact_seat_alias(base, other), expected)

    def test_runtime_source_names_no_development_model(self):
        # Provider-neutrality pin: no runtime class, branch, default, error
        # message, or fixture privileges the model that develops this
        # repository.
        pattern = re.compile(r"fable", re.IGNORECASE)
        for path in sorted(PKG_SRC.rglob("*.py")):
            with self.subTest(file=path.name):
                self.assertIsNone(
                    pattern.search(path.read_bytes().decode("utf-8"))
                )


class SeatPropagationScenarioTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def clean_pass_kwargs(self):
        return dict(
            states=clean_pass_states(),
            coder=[
                MockAgentAction(
                    writes=((SELF_REPORT, "# Coder Self-Report\n"),)
                )
            ],
            reviewer=[
                MockAgentAction(
                    writes=((REVIEW_REPORT, "Verdict: pass — next: record\n"),)
                )
            ],
            verbs=DEFAULT_VERBS,
            policy_body=SEAT_POLICY,
        )

    def run_clean(self):
        scenario = Scenario(self.tmp, **self.clean_pass_kwargs())
        result = scenario.supervisor.run_until()
        self.assertEqual(result.kind, "boundary")
        return scenario

    def requests_by_role(self, scenario):
        requests = {}
        for slice_id in scenario.store.list_slices("run_001"):
            for attempt in scenario.store.list_attempts("run_001", slice_id):
                record = scenario.store.read_request(attempt)
                if record:
                    requests[record["role"]] = record
        return requests

    def test_write_once_requests_carry_each_roles_exact_pair(self):
        scenario = self.run_clean()
        requests = self.requests_by_role(scenario)
        self.assertEqual(
            (requests["coder"]["adapter"], requests["coder"]["model"]),
            ("mock", "model-alpha"),
        )
        self.assertEqual(
            (requests["reviewer"]["adapter"], requests["reviewer"]["model"]),
            ("mock", "model-beta"),
        )

    def test_journal_dispatch_facts_carry_adapter_and_model(self):
        scenario = self.run_clean()
        dispatches = {
            event["role"]: event
            for event in scenario.events()
            if event["kind"] == "dispatch"
        }
        self.assertEqual(dispatches["coder"]["adapter"], "mock")
        self.assertEqual(dispatches["coder"]["model"], "model-alpha")
        self.assertEqual(dispatches["reviewer"]["adapter"], "mock")
        self.assertEqual(dispatches["reviewer"]["model"], "model-beta")

    def test_replay_preserves_durable_identity_facts(self):
        scenario = self.run_clean()
        resumed = Scenario(
            self.tmp, project=scenario.project, **self.clean_pass_kwargs()
        )
        self.assertIsNone(resumed.supervisor.resume())
        requests = self.requests_by_role(resumed)
        self.assertEqual(requests["coder"]["model"], "model-alpha")
        self.assertEqual(requests["reviewer"]["model"], "model-beta")
        replayed_dispatches = [
            (event["role"], event["adapter"], event["model"])
            for event in resumed.events()
            if event["kind"] == "dispatch"
        ]
        self.assertIn(("coder", "mock", "model-alpha"), replayed_dispatches)
        self.assertIn(("reviewer", "mock", "model-beta"), replayed_dispatches)

    def test_architect_reconciliation_request_carries_its_own_seat(self):
        from _scenario import payload

        scenario = Scenario(
            self.tmp,
            states=[
                payload(
                    "needs_specification", None, actor="architect",
                    frontier_present=False,
                ),
                payload(),
            ],
            architect=[
                MockAgentAction(
                    writes=((
                        "roadmap_proposal.md",
                        ROADMAP_BODY.replace(
                            "Implement the fixture behavior.",
                            "Sharpen the fixture behavior.",
                        ),
                    ),)
                )
            ],
            policy_body=SEAT_POLICY,
        )
        result = scenario.supervisor.tick()
        self.assertEqual(result.detail, "reconciliation")
        requests = self.requests_by_role(scenario)
        self.assertEqual(
            (requests["architect"]["adapter"], requests["architect"]["model"]),
            ("mock", ""),
        )


if __name__ == "__main__":
    unittest.main()
