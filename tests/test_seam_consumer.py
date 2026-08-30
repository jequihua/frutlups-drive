"""Contract tests for the frozen frutlups 0.2 seam consumer."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import _bootstrap  # noqa: F401

from frutlups_drive.seam_consumer import (
    ATTEMPT_PLACEHOLDER,
    CORRECTIVE_RECEIPT_SCHEMA,
    DRIVE_PAYLOAD_SCHEMA,
    FRONTIER_SCHEMA,
    ROUTE_STEPS,
    SEAM_REFUSAL_SCHEMA,
    AdoptionIdentity,
    CorrectiveProposalFailure,
    CorrectiveReceipt,
    DrivePayload,
    DriveSeamRefusal,
    EnvelopeAdmissionFailure,
    EnvelopeAuthority,
    Frontier,
    FrutlupsSeamConsumer,
    SeamAdmissionFailure,
    SeamProcessResult,
    SeamTransportFailure,
    SeamUsageFailure,
    admit_process_result,
    admit_seam_response,
    build_corrective_publication_proposal,
    canonical_json_bytes,
)


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "drive_seam_v1"
CASE_FILES = (
    "payload_cases.json",
    "frontier_cases.json",
    "publication_cases.json",
    "dry_run_cases.json",
    "refusal_cases.json",
)
EXPECTED_DIGESTS = {
    "manifest.json": "f88336f1d70c6f3fbf05bec19bcbb36e0fbdae0ee412de9e4f0d961f8f839b93",
    "payload_cases.json": "3db18b8a096678dc60e158265b2b5abeccd8c74b9860cc454583b977d84f3908",
    "frontier_cases.json": "aafcc669135d3ef700a5d29309c16f169113855e703302fda413813b3e656cff",
    "publication_cases.json": "25cac4e8bc6bd737b0a0ad09406148451bc2670fda9f21ad2a11f0a78f9c6523",
    "dry_run_cases.json": "847c75fb351895b99aca8465ba62b31d73b3e3292045647a7c99c49fc4ae940f",
    "refusal_cases.json": "8b67d8686ba955d94df346348f9f74d0a24eb4f084fd266c4314b67d1e18d27c",
}


def fixture(name: str) -> dict:
    return json.loads((FIXTURE_ROOT / name).read_bytes().decode("utf-8"))


def rows() -> list[dict]:
    result = []
    for name in CASE_FILES:
        result.extend(fixture(name)["cases"])
    return result


def schema_case(schema: str) -> tuple[dict, dict]:
    for row in rows():
        document = row["expected_stdout"]
        if isinstance(document, dict) and document.get("schema") == schema:
            return row, copy.deepcopy(document)
    raise AssertionError(f"fixture has no {schema} row")


def proposal_bytes(row: dict) -> bytes | None:
    value = row.get("stdin_utf8")
    return value.encode("utf-8") if isinstance(value, str) and value else None


def producer_json_bytes(document: object, *, final_lf: bool = False) -> bytes:
    raw = json.dumps(
        document,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return raw + (b"\n" if final_lf else b"")


def admit_document(
    document: dict,
    *,
    exit_code: int,
    proposal: bytes | None = None,
    authority: EnvelopeAuthority | None = None,
):
    return admit_seam_response(
        exit_code=exit_code,
        stdout=canonical_json_bytes(document, final_lf=True),
        proposal_bytes=proposal,
        envelope_authority=authority,
    )


def rehash_frontier(document: dict) -> None:
    receipt = document["receipt"]
    document["receipt_sha256"] = hashlib.sha256(
        canonical_json_bytes(receipt)
    ).hexdigest()


def rehash_corrective(document: dict) -> None:
    without = dict(document)
    without.pop("receipt_sha256", None)
    document["receipt_sha256"] = hashlib.sha256(
        canonical_json_bytes(without)
    ).hexdigest()


class FrozenFixtureIntegrityTests(unittest.TestCase):
    def test_all_six_fixture_digests_and_manifest_members_are_pinned(self):
        for name, expected in EXPECTED_DIGESTS.items():
            with self.subTest(name=name):
                self.assertEqual(
                    hashlib.sha256((FIXTURE_ROOT / name).read_bytes()).hexdigest(),
                    expected,
                )
        manifest = fixture("manifest.json")
        self.assertEqual(set(manifest), {"schema", "version", "members"})
        member_digests = {
            Path(member["path"]).name: member["sha256"]
            for member in manifest["members"]
        }
        self.assertEqual(
            member_digests,
            {name: EXPECTED_DIGESTS[name] for name in CASE_FILES},
        )
        self.assertEqual(sum(len(fixture(name)["cases"]) for name in CASE_FILES), 73)


class FrozenResponseAdmissionTests(unittest.TestCase):
    def test_non_ascii_producer_recipe_stdout_admits(self):
        document = {
            "schema": SEAM_REFUSAL_SCHEMA,
            "version": 1,
            "verb": "drive-payload",
            "code": "prompt_collision",
            "detail": "Entrée refusée — détail borné",
        }
        stdout = producer_json_bytes(document, final_lf=True)
        self.assertIn("Entrée refusée — détail borné".encode(), stdout)
        self.assertNotIn(b"\\u00e9", stdout)
        response = admit_seam_response(exit_code=3, stdout=stdout)
        self.assertIsInstance(response, DriveSeamRefusal)
        self.assertEqual(response.detail, document["detail"])

    def test_all_73_frozen_documents_and_usage_exits_admit(self):
        admitted = 0
        for row in rows():
            with self.subTest(case=row["id"]):
                expected = row["expected_stdout"]
                stdout = (
                    b""
                    if expected is None
                    else canonical_json_bytes(expected, final_lf=True)
                )
                stderr = (
                    b"usage: frozen fixture\n"
                    if row["expected_stderr_class"] == "usage"
                    else b""
                )
                try:
                    response = admit_seam_response(
                        exit_code=row["expected_exit"],
                        stdout=stdout,
                        stderr=stderr,
                        proposal_bytes=proposal_bytes(row),
                    )
                except SeamUsageFailure:
                    self.assertEqual(row["expected_exit"], 2)
                else:
                    self.assertIn(
                        type(response),
                        {DrivePayload, Frontier, CorrectiveReceipt, DriveSeamRefusal},
                    )
                admitted += 1
        self.assertEqual(admitted, 73)

    def test_refusal_corpus_is_table_driven_and_typed(self):
        refusal_rows = fixture("refusal_cases.json")["cases"]
        self.assertEqual(len(refusal_rows), 32)
        for row in refusal_rows:
            with self.subTest(case=row["id"]):
                if row["expected_exit"] == 2:
                    with self.assertRaises(SeamUsageFailure):
                        admit_seam_response(
                            exit_code=2,
                            stdout=b"",
                            stderr=b"usage: frozen fixture\n",
                            proposal_bytes=proposal_bytes(row),
                        )
                else:
                    response = admit_document(
                        row["expected_stdout"],
                        exit_code=3,
                        proposal=proposal_bytes(row),
                    )
                    self.assertIsInstance(response, DriveSeamRefusal)
                    self.assertEqual(response.code, row["expected_stdout"]["code"])

    def test_unknown_schema_version_and_field_sets_refuse_every_schema(self):
        schemas = (
            DRIVE_PAYLOAD_SCHEMA,
            FRONTIER_SCHEMA,
            CORRECTIVE_RECEIPT_SCHEMA,
            SEAM_REFUSAL_SCHEMA,
        )
        for schema in schemas:
            row, document = schema_case(schema)
            exit_code = row["expected_exit"]
            proposal = proposal_bytes(row)
            with self.subTest(schema=schema, mutation="version"):
                wrong = copy.deepcopy(document)
                wrong["version"] = 99
                with self.assertRaisesRegex(
                    SeamAdmissionFailure, "response_version_unknown"
                ):
                    admit_document(wrong, exit_code=exit_code, proposal=proposal)
            with self.subTest(schema=schema, mutation="field_set"):
                wrong = copy.deepcopy(document)
                wrong["unexpected"] = None
                with self.assertRaises(SeamAdmissionFailure):
                    admit_document(wrong, exit_code=exit_code, proposal=proposal)
        row, document = schema_case(DRIVE_PAYLOAD_SCHEMA)
        document["schema"] = "frutlups.future.v9"
        with self.assertRaisesRegex(SeamAdmissionFailure, "response_schema_unknown"):
            admit_document(document, exit_code=row["expected_exit"])

    def test_payload_identity_types_and_refusal_enums_fail_closed(self):
        _, payload = schema_case(DRIVE_PAYLOAD_SCHEMA)
        wrong_contract = copy.deepcopy(payload)
        wrong_contract["payload"]["contract_version"] = "1"
        with self.assertRaisesRegex(SeamAdmissionFailure, "payload_contract_unknown"):
            admit_document(wrong_contract, exit_code=0)
        wrong_attempt = copy.deepcopy(payload)
        wrong_attempt["adoption"]["attempt"] = False
        with self.assertRaisesRegex(SeamAdmissionFailure, "response_attempt_invalid"):
            admit_document(wrong_attempt, exit_code=0)
        wrong_entry = copy.deepcopy(payload)
        wrong_entry["payload"]["entry"]["status"] = "different"
        with self.assertRaisesRegex(SeamAdmissionFailure, "payload_entry_incoherent"):
            admit_document(wrong_entry, exit_code=0)

        _, refusal = schema_case(SEAM_REFUSAL_SCHEMA)
        for field, value, code in (
            ("verb", "future-verb", "refusal_verb_unknown"),
            ("code", "future_code", "refusal_code_unknown"),
        ):
            wrong = copy.deepcopy(refusal)
            wrong[field] = value
            with self.subTest(field=field):
                with self.assertRaisesRegex(SeamAdmissionFailure, code):
                    admit_document(wrong, exit_code=3)

    def test_noncanonical_truncated_empty_non_json_and_unknown_exit_fail_transport(self):
        _, document = schema_case(DRIVE_PAYLOAD_SCHEMA)
        canonical = canonical_json_bytes(document, final_lf=True)
        cases = (
            (0, canonical[:-2] + b"\n", "seam_stdout_malformed"),
            (0, canonical[:-1], "seam_stdout_noncanonical"),
            (0, b"", "seam_stdout_empty"),
            (0, b"not json\n", "seam_stdout_malformed"),
            (9, canonical, "seam_exit_unknown"),
        )
        for exit_code, stdout, code in cases:
            with self.subTest(code=code):
                with self.assertRaisesRegex(SeamTransportFailure, code):
                    admit_seam_response(exit_code=exit_code, stdout=stdout)
        with self.assertRaisesRegex(SeamTransportFailure, "seam_stderr_unexpected"):
            admit_seam_response(exit_code=0, stdout=canonical, stderr=b"prose")

    def test_route_step_mapping_is_total_and_outcome_mismatch_refuses(self):
        observed = {}
        for row in fixture("frontier_cases.json")["cases"]:
            document = row["expected_stdout"]
            if not isinstance(document, dict) or document.get("schema") != FRONTIER_SCHEMA:
                continue
            response = admit_document(document, exit_code=0)
            observed[response.route] = response.step
        self.assertEqual(observed, ROUTE_STEPS)
        _, document = schema_case(FRONTIER_SCHEMA)
        document["outcome"] = "invalid"
        with self.assertRaisesRegex(SeamAdmissionFailure, "frontier_outcome_mismatch"):
            admit_document(document, exit_code=0)

    def test_frontier_enums_receipt_digest_and_three_dimensions_refuse(self):
        _, document = schema_case(FRONTIER_SCHEMA)
        mutations = (
            ("route", lambda item: item.update(route="future_route"), "frontier_route_unknown"),
            ("step", lambda item: item.update(step="future_step"), "frontier_step_mismatch"),
            (
                "verdict",
                lambda item: item["receipt"].update(verdict="future_verdict"),
                "frontier_verdict_unknown",
            ),
            (
                "objective",
                lambda item: item["receipt"].update(objective_status="future_status"),
                "frontier_objective_status_unknown",
            ),
            (
                "receipt_route",
                lambda item: item["receipt"].update(route="milestone_complete"),
                "frontier_receipt_route_mismatch",
            ),
        )
        for name, mutate, code in mutations:
            wrong = copy.deepcopy(document)
            mutate(wrong)
            if name in {"verdict", "objective", "receipt_route"}:
                rehash_frontier(wrong)
            with self.subTest(name=name):
                with self.assertRaisesRegex(SeamAdmissionFailure, code):
                    admit_document(wrong, exit_code=0)
        wrong = copy.deepcopy(document)
        wrong["receipt_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            SeamAdmissionFailure, "frontier_receipt_digest_mismatch"
        ):
            admit_document(wrong, exit_code=0)

    def test_frontier_digest_recomputation_accepts_non_ascii_content(self):
        document = copy.deepcopy(
            next(
                row["expected_stdout"]
                for row in fixture("frontier_cases.json")["cases"]
                if isinstance(row["expected_stdout"], dict)
                and row["expected_stdout"].get("route") == "recode_same_slice"
            )
        )
        unicode_status = "nöt_achieved"
        document["receipt"]["objective_status"] = unicode_status
        receipt_bytes = producer_json_bytes(document["receipt"])
        document["receipt_sha256"] = hashlib.sha256(receipt_bytes).hexdigest()

        # Frozen frontier receipt values are ASCII-only. Extend the test authority
        # solely to reach the canonical digest path with Unicode receipt content.
        objective_statuses = {
            "achieved",
            "not_achieved",
            "not_applicable",
            "indeterminate",
            unicode_status,
        }
        with patch(
            "frutlups_drive.seam_consumer.OBJECTIVE_STATUSES", objective_statuses
        ):
            response = admit_seam_response(
                exit_code=0,
                stdout=producer_json_bytes(document, final_lf=True),
            )
        self.assertEqual(response.receipt.objective_status, unicode_status)
        self.assertEqual(response.receipt_sha256, hashlib.sha256(receipt_bytes).hexdigest())

    def test_milestone_completion_two_positive_families_and_negatives(self):
        frontier_rows = fixture("frontier_cases.json")["cases"]
        positive_ids = {
            "frontier_pass_achieved_last_slice_completes",
            "frontier_corrective_last_slice_completes",
            "frontier_pass_not_applicable_explicit_milestone_complete",
        }
        for row in frontier_rows:
            if row["id"] in positive_ids:
                response = admit_document(row["expected_stdout"], exit_code=0)
                self.assertTrue(response.milestone_complete)
        self.assertEqual(
            sum(row["id"] in positive_ids for row in frontier_rows), 3
        )

        complete_row = next(
            row
            for row in frontier_rows
            if row["id"] == "frontier_pass_achieved_last_slice_completes"
        )
        negatives = []
        false_complete = copy.deepcopy(complete_row["expected_stdout"])
        false_complete["milestone_complete"] = False
        negatives.append(false_complete)
        wrong_verdict = copy.deepcopy(complete_row["expected_stdout"])
        wrong_verdict["receipt"]["verdict"] = "needs_work"
        rehash_frontier(wrong_verdict)
        negatives.append(wrong_verdict)
        wrong_objective = copy.deepcopy(complete_row["expected_stdout"])
        wrong_objective["receipt"]["objective_status"] = "not_achieved"
        rehash_frontier(wrong_objective)
        negatives.append(wrong_objective)
        advance = copy.deepcopy(
            next(
                row["expected_stdout"]
                for row in frontier_rows
                if row["id"] == "frontier_pass_achieved_advances"
            )
        )
        advance["milestone_complete"] = True
        negatives.append(advance)
        for index, document in enumerate(negatives):
            with self.subTest(index=index):
                with self.assertRaises(SeamAdmissionFailure):
                    admit_document(document, exit_code=0)

    def test_corrective_receipt_digests_transaction_exit_and_enums_refuse(self):
        row, document = schema_case(CORRECTIVE_RECEIPT_SCHEMA)
        proposal = proposal_bytes(row)
        self.assertIsNotNone(proposal)
        mutations = (
            (
                "proposal",
                lambda item: item.update(proposal_sha256="0" * 64),
                "corrective_proposal_digest_mismatch",
            ),
            (
                "transaction",
                lambda item: item.update(transaction_id="cp." + "0" * 64),
                "corrective_transaction_id_mismatch",
            ),
            (
                "mode",
                lambda item: item.update(mode="future_mode"),
                "corrective_mode_unknown",
            ),
            (
                "outcome",
                lambda item: item.update(outcome="future_outcome"),
                "corrective_outcome_unknown",
            ),
        )
        for name, mutate, code in mutations:
            wrong = copy.deepcopy(document)
            mutate(wrong)
            with self.subTest(name=name):
                with self.assertRaisesRegex(SeamAdmissionFailure, code):
                    admit_document(wrong, exit_code=0, proposal=proposal)
        wrong = copy.deepcopy(document)
        wrong["receipt_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            SeamAdmissionFailure, "corrective_receipt_digest_mismatch"
        ):
            admit_document(wrong, exit_code=0, proposal=proposal)
        with self.assertRaisesRegex(SeamAdmissionFailure, "corrective_exit_incoherent"):
            admit_document(document, exit_code=3, proposal=proposal)
        wrong = copy.deepcopy(document)
        first_path = next(iter(wrong["before"]))
        wrong["before"][first_path] = {"state": "future_state"}
        with self.assertRaisesRegex(
            SeamAdmissionFailure, "receipt_observation_state_unknown"
        ):
            admit_document(wrong, exit_code=0, proposal=proposal)
        wrong = copy.deepcopy(document)
        wrong["rendered_prompt"]["path"] = "prompts/for_coding_agent/wrong.md"
        rehash_corrective(wrong)
        with self.assertRaisesRegex(
            SeamAdmissionFailure, "corrective_receipt_identity_mismatch"
        ):
            admit_document(wrong, exit_code=0, proposal=proposal)

    def test_clean_refused_corrective_receipt_admits_on_exit_three(self):
        row, document = schema_case(CORRECTIVE_RECEIPT_SCHEMA)
        document["outcome"] = "refused"
        document["refusal_codes"] = ["prompt_collision"]
        rehash_corrective(document)
        response = admit_document(
            document, exit_code=3, proposal=proposal_bytes(row)
        )
        self.assertEqual(response.outcome, "refused")

    def test_corrective_digest_recomputation_accepts_non_ascii_path(self):
        row, document = schema_case(CORRECTIVE_RECEIPT_SCHEMA)
        original_path = next(iter(document["before"]))
        unicode_path = "05_governance/reviews/m010/résumé_évidence.md"
        document["before"][unicode_path] = document["before"].pop(original_path)
        document["after"][unicode_path] = document["after"].pop(original_path)
        without_digest = dict(document)
        without_digest.pop("receipt_sha256")
        document["receipt_sha256"] = hashlib.sha256(
            producer_json_bytes(without_digest)
        ).hexdigest()

        response = admit_seam_response(
            exit_code=row["expected_exit"],
            stdout=producer_json_bytes(document, final_lf=True),
            proposal_bytes=proposal_bytes(row),
        )
        self.assertIsInstance(response, CorrectiveReceipt)
        self.assertIn(unicode_path, response.before)
        self.assertIn(unicode_path, response.after)


class EnvelopeAdmissionTests(unittest.TestCase):
    binding_value = "-Djava.io.tmpdir=.tmp"

    def authority(self, **updates) -> EnvelopeAuthority:
        values = {
            "agent_budget_ceiling_seconds": 120.0,
            "subprocess_budget_ceiling_seconds": 90.0,
            "wall_ceiling_seconds": 300.0,
            "environment_bindings": (("JAVA_TOOL_OPTIONS", self.binding_value),),
        }
        values.update(updates)
        return EnvelopeAuthority(**values)

    def envelope(self) -> dict:
        return {
            "timing_probe": {"command": "python -m unittest", "expected_seconds": 30},
            "agent_budget_seconds": 60,
            "subprocess_budget_seconds": 45,
            "expected_wall_seconds": 120,
            "hard_wall_seconds": 180,
            "frozen_override": "none",
            "environment_bindings": [
                {
                    "name": "JAVA_TOOL_OPTIONS",
                    "value_sha256": hashlib.sha256(
                        self.binding_value.encode("utf-8")
                    ).hexdigest(),
                }
            ],
            "identities": [{"kind": "candidate", "value": "frozen"}],
            "retained_bytes_max": 1048576,
            "local_output_root": ".frutlups_drive/local_output",
            "cleanup": "remove temporary output",
            "negative_result_handling": "preserve bounded evidence and stop",
            "stopped_result_handling": "preserve bounded evidence and stop",
        }

    def payload(self, envelope: dict | None) -> dict:
        _, document = schema_case(DRIVE_PAYLOAD_SCHEMA)
        if envelope is not None:
            document["payload"]["live"] = True
            document["payload"]["entry"]["live"] = True
            document["payload"]["execution_envelope"] = copy.deepcopy(envelope)
            document["payload"]["entry"]["execution_envelope"] = copy.deepcopy(envelope)
        return document

    def test_valid_envelope_and_absent_envelope_inertness(self):
        response = admit_document(
            self.payload(self.envelope()),
            exit_code=0,
            authority=self.authority(),
        )
        self.assertIsInstance(response.adoption_identity, AdoptionIdentity)
        self.assertEqual(
            response.payload.execution_envelope.environment_bindings[0][0],
            "JAVA_TOOL_OPTIONS",
        )
        absent = admit_document(self.payload(None), exit_code=0)
        self.assertIsNone(absent.payload.execution_envelope)

    def test_budget_wall_hash_and_undeclared_binding_refuse_typed(self):
        cases = []
        agent = self.envelope()
        agent["agent_budget_seconds"] = 121
        cases.append((agent, self.authority(), "envelope_agent_budget_exceeds_policy"))
        subprocess_envelope = self.envelope()
        subprocess_envelope["subprocess_budget_seconds"] = 91
        cases.append(
            (
                subprocess_envelope,
                self.authority(),
                "envelope_subprocess_budget_exceeds_policy",
            )
        )
        wall = self.envelope()
        wall["hard_wall_seconds"] = 301
        cases.append((wall, self.authority(), "envelope_wall_exceeds_policy"))
        mismatch = self.envelope()
        mismatch["environment_bindings"][0]["value_sha256"] = "0" * 64
        cases.append((mismatch, self.authority(), "envelope_binding_hash_mismatch"))
        undeclared = self.envelope()
        undeclared["environment_bindings"][0]["name"] = "JAVA_HOME"
        cases.append((undeclared, self.authority(), "envelope_binding_undeclared"))
        for envelope, authority, code in cases:
            with self.subTest(code=code):
                with self.assertRaisesRegex(EnvelopeAdmissionFailure, code):
                    admit_document(
                        self.payload(envelope),
                        exit_code=0,
                        authority=authority,
                    )

    def test_envelope_exact_shape_and_policy_gate_equality(self):
        envelope = self.envelope()
        envelope["extra"] = True
        with self.assertRaisesRegex(SeamAdmissionFailure, "envelope_shape_invalid"):
            admit_document(
                self.payload(envelope), exit_code=0, authority=self.authority()
            )

        dispatch = SimpleNamespace(
            role_call_ceiling_seconds=(("coder", 120.0),),
            slice_call_ceiling_overrides=(),
            scientific_subprocess_budget_seconds=90.0,
            call_ceiling=lambda role, slice_id: (120.0, "role"),
        )
        policy = SimpleNamespace(
            runtime_environment_bindings=(("JAVA_TOOL_OPTIONS", self.binding_value),),
            dispatch=dispatch,
            limits=SimpleNamespace(max_wall_clock_minutes=5),
        )
        gate = SimpleNamespace(
            runtime_environment_bindings=policy.runtime_environment_bindings,
            role_call_ceiling_seconds=dispatch.role_call_ceiling_seconds,
            slice_call_ceiling_overrides=(),
            call_timeout_seconds=300.0,
        )
        projected = EnvelopeAuthority.from_drive_authorities(
            policy, gate, role="coder", slice_id="M001-S01"
        )
        self.assertEqual(projected, self.authority())
        gate.runtime_environment_bindings = ()
        with self.assertRaisesRegex(
            EnvelopeAdmissionFailure, "envelope_authority_mismatch"
        ):
            EnvelopeAuthority.from_drive_authorities(
                policy, gate, role="coder", slice_id="M001-S01"
            )


class CorrectiveProposalAndTransportTests(unittest.TestCase):
    def valid_material(self) -> tuple[dict, dict]:
        row = fixture("dry_run_cases.json")["cases"][0]
        proposal = json.loads(row["stdin_utf8"])
        return row, proposal

    def test_proposal_builder_is_exact_attempt_free_and_byte_stable(self):
        _, material = self.valid_material()
        kwargs = {
            "slice_id": material["slice"],
            "sidecar_path": material["sidecar_path"],
            "prompt_path": material["prompt_path"],
            "entry_template": material["entry_template"],
        }
        first = build_corrective_publication_proposal(**kwargs)
        second = build_corrective_publication_proposal(**kwargs)
        self.assertEqual(first, second)
        self.assertTrue(first.endswith(b"\n"))
        document = json.loads(first)
        self.assertEqual(
            set(document),
            {"schema", "version", "slice", "sidecar_path", "prompt_path", "entry_template"},
        )
        self.assertNotIn("attempt", document["entry_template"])
        self.assertEqual(document["prompt_path"].count(ATTEMPT_PLACEHOLDER), 1)
        self.assertEqual(first, canonical_json_bytes(document, final_lf=True))

    def test_proposal_builder_round_trips_non_ascii_content_byte_stably(self):
        _, material = self.valid_material()
        entry_template = copy.deepcopy(material["entry_template"])
        entry_template["title"] = "Réparer l’entrée — sans perte"
        kwargs = {
            "slice_id": material["slice"],
            "sidecar_path": material["sidecar_path"],
            "prompt_path": material["prompt_path"],
            "entry_template": entry_template,
        }
        first = build_corrective_publication_proposal(**kwargs)
        second = build_corrective_publication_proposal(**kwargs)
        self.assertEqual(first, second)
        self.assertIn(entry_template["title"].encode(), first)
        self.assertNotIn(b"\\u00e9", first)
        self.assertEqual(json.loads(first)["entry_template"], entry_template)
        self.assertEqual(
            first,
            producer_json_bytes(json.loads(first), final_lf=True),
        )

    def test_proposal_builder_refuses_attempt_and_placeholder_errors(self):
        _, material = self.valid_material()
        attempted = copy.deepcopy(material["entry_template"])
        attempted["attempt"] = "001"
        with self.assertRaises(CorrectiveProposalFailure):
            build_corrective_publication_proposal(
                slice_id=material["slice"],
                sidecar_path=material["sidecar_path"],
                prompt_path=material["prompt_path"],
                entry_template=attempted,
            )
        with self.assertRaises(CorrectiveProposalFailure):
            build_corrective_publication_proposal(
                slice_id=material["slice"],
                sidecar_path=material["sidecar_path"],
                prompt_path=material["prompt_path"].replace(ATTEMPT_PLACEHOLDER, "001"),
                entry_template=material["entry_template"],
            )

    def test_process_result_flags_fail_closed(self):
        cases = (
            (SeamProcessResult(None, b"", b"", spawn_failed=True), "seam_spawn_failed"),
            (SeamProcessResult(None, b"", b"", timed_out=True), "seam_timeout"),
            (SeamProcessResult(0, b"", b"", stdout_overflow=True), "seam_stdout_oversized"),
            (SeamProcessResult(0, b"", b"", stderr_overflow=True), "seam_stderr_oversized"),
        )
        for result, code in cases:
            with self.subTest(code=code):
                with self.assertRaisesRegex(SeamTransportFailure, code):
                    admit_process_result(result)

    def test_explicit_argv_and_corrective_paths_come_only_from_proposal(self):
        payload_row, payload_document = schema_case(DRIVE_PAYLOAD_SCHEMA)
        refusal = {
            "schema": SEAM_REFUSAL_SCHEMA,
            "version": 1,
            "verb": "corrective-publish",
            "code": "prompt_collision",
            "detail": "bounded fixture refusal",
        }
        _, material = self.valid_material()
        proposal = build_corrective_publication_proposal(
            slice_id=material["slice"],
            sidecar_path=material["sidecar_path"],
            prompt_path=material["prompt_path"],
            entry_template=material["entry_template"],
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            consumer = FrutlupsSeamConsumer(
                python_executable=Path(sys.executable).resolve(),
                project_root=root,
                env={"PYTHONDONTWRITEBYTECODE": "1"},
            )
            payload_result = SeamProcessResult(
                payload_row["expected_exit"],
                canonical_json_bytes(payload_document, final_lf=True),
                b"",
            )
            with patch(
                "frutlups_drive.seam_consumer._run_bounded_process",
                return_value=payload_result,
            ) as runner:
                consumer.drive_payload(
                    sidecar_path="03_experiments/active_roadmap.slices.yaml",
                    slice_id="M001-S01",
                    prompt_path="prompts/for_coding_agent/001_m001_s01_ledger.md",
                )
            argv = runner.call_args.args[0]
            self.assertEqual(argv[:4], (sys.executable, "-m", "frutlups", "drive-payload"))
            self.assertEqual(argv[4], str(root))
            self.assertEqual(
                runner.call_args.kwargs["env"], {"PYTHONDONTWRITEBYTECODE": "1"}
            )

            refusal_result = SeamProcessResult(
                3, canonical_json_bytes(refusal, final_lf=True), b""
            )
            with patch(
                "frutlups_drive.seam_consumer._run_bounded_process",
                return_value=refusal_result,
            ) as runner:
                response = consumer.corrective_publish(proposal, dry_run=True)
            self.assertIsInstance(response, DriveSeamRefusal)
            argv = runner.call_args.args[0]
            self.assertEqual(argv[argv.index("--sidecar") + 1], material["sidecar_path"])
            self.assertEqual(argv[argv.index("--prompt") + 1], material["prompt_path"])
            self.assertEqual(runner.call_args.kwargs["stdin_bytes"], proposal)


if __name__ == "__main__":
    unittest.main()
