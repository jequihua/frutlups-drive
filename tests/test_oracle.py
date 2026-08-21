"""Deterministic pass-boundary oracle contract lanes (M006-S07)."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

from frutlups_drive.oracle import (
    OracleRefusal,
    reconcile_pass_boundary,
    valid_oracle_bundle,
)
from frutlups_drive.runstore import (
    MAX_PASS_ORACLE_BYTES,
    RunStore,
    RunStoreRefusal,
)


INDEX = "05_governance/reviews/INDEX.md"
SELF_REPORT = "05_governance/reviews/m001/m001_s01_self_report.md"
REVIEW_PROMPT = "prompts/for_review_agent/002_review_m001_s01.md"
REVIEW_REPORT = "05_governance/reviews/m001/m001_s01_review_report.md"
VERDICT_RECORD = "05_governance/reviews/m001/m001_s01_verdict_record.md"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class OracleComputationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.project = root / "project"
        self.evidence = root / "evidence"
        self.evidence.mkdir(parents=True)
        files = {
            INDEX: (
                "# Review Index\n\n"
                "| Milestone | Slice | Round | Self-Report | Review Prompt | "
                "Review Report | Verdict | Commit |\n"
                "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
                f"| M001 | M001-S01 | 1 | `{SELF_REPORT}` | "
                f"`{REVIEW_PROMPT}` | `{REVIEW_REPORT}` | pass | fixture |\n"
            ),
            SELF_REPORT: "# Coder Self-Report\n",
            REVIEW_PROMPT: "# Review M001-S01\n",
            REVIEW_REPORT: (
                "# Review Report\n\n"
                "Verdict: pass - next: record the verdict\n"
            ),
            VERDICT_RECORD: (
                "# Verdict Record: M001-S01\n\n"
                f"Review report: `{REVIEW_REPORT}`\n\n"
                "Slice ID: `M001-S01`\n\n"
                "Verdict: `pass`\n"
            ),
        }
        for relative, content in files.items():
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        (self.evidence / "events.jsonl").write_bytes(
            b'{"kind":"slice_complete"}\n'
        )
        self.record = self._record()

    def _record(self) -> dict[str, object]:
        artifacts = []
        for path in sorted(item for item in self.project.rglob("*") if item.is_file()):
            artifacts.append(
                {
                    "path": path.relative_to(self.project).as_posix(),
                    "sha256": _sha(path),
                }
            )
        evidence = self.evidence / "events.jsonl"
        return {
            "contract_version": 1,
            "run_id": "run_001",
            "evidence": [{"path": "events.jsonl", "sha256": _sha(evidence)}],
            "artifacts": artifacts,
        }

    def reconcile(
        self,
        record: dict[str, object] | None = None,
        *,
        index_mode: str = "human-ledger",
    ) -> dict:
        return reconcile_pass_boundary(
            record or self.record,
            self.project,
            self.evidence,
            index_mode=index_mode,
        )

    def _historical_shape(self, mechanical: int) -> dict[str, object]:
        reviews = self.project / "05_governance/reviews/m999"
        if reviews.exists():
            for path in reviews.iterdir():
                path.unlink()
        reviews.mkdir(parents=True, exist_ok=True)
        for number in range(mechanical):
            (reviews / f"unindexed_{number:03d}_self_report.md").write_text(
                "# Historical unindexed report\n", encoding="utf-8"
            )
        record = self._record()
        record["artifacts"] = [
            member
            for member in record["artifacts"]
            if member["path"] != VERDICT_RECORD
        ]
        return record

    def _write_events(self, events: list[dict[str, object]]) -> None:
        data = b"".join(
            (
                json.dumps(event, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            for event in events
        )
        (self.evidence / "events.jsonl").write_bytes(data)

    def _drained_chain(self) -> list[dict[str, object]]:
        coding = "prompts/for_coding_agent/003_rework.md"
        review = "prompts/for_review_agent/004_rework.md"
        return [
            {
                "kind": "verb",
                "verb": "declare-rework",
                "slice": "",
                "slices": ["M001-S01"],
                "pass_id": "holistic_pass_001",
            },
            {
                "kind": "verb",
                "verb": "make-coding-prompt",
                "slice": "M001-S01",
                "artifact": coding,
            },
            {
                "kind": "dispatch",
                "role": "coder",
                "slice": "M001-S01",
                "attempt": "attempt_001",
                "repair": False,
                "prompt_source": coding,
            },
            {
                "kind": "collected",
                "role": "coder",
                "slice": "M001-S01",
                "attempt": "attempt_001",
                "status": "completed",
            },
            {
                "kind": "verification",
                "slice": "M001-S01",
                "attempt": "attempt_001",
                "passed": True,
            },
            {
                "kind": "verb",
                "verb": "make-review-prompt",
                "slice": "M001-S01",
                "artifact": review,
            },
            {
                "kind": "dispatch",
                "role": "reviewer",
                "slice": "M001-S01",
                "attempt": "attempt_002",
                "repair": False,
                "prompt_source": review,
            },
            {
                "kind": "collected",
                "role": "reviewer",
                "slice": "M001-S01",
                "attempt": "attempt_002",
                "status": "completed",
            },
            {
                "kind": "verb",
                "verb": "record-verdict",
                "slice": "M001-S01",
                "artifact": "05_governance/reviews/m001/fresh_verdict.md",
            },
        ]

    def test_clean_bundle_is_deterministic(self) -> None:
        first = self.reconcile()
        second = self.reconcile()
        self.assertEqual(first, second)
        self.assertEqual(first["contract_version"], 1)
        self.assertEqual(first["run_id"], "run_001")
        self.assertEqual(first["index_mode"], "human-ledger")
        self.assertEqual(first["observations"], [])

    def test_human_ledger_default_and_explicit_observations_are_byte_identical(self) -> None:
        record = self._historical_shape(40)
        implicit = reconcile_pass_boundary(record, self.project, self.evidence)
        explicit = self.reconcile(record, index_mode="human-ledger")
        self.assertEqual(
            json.dumps(implicit["observations"], sort_keys=True),
            json.dumps(explicit["observations"], sort_keys=True),
        )

    def test_historical_counts_remain_human_ledger_counts(self) -> None:
        for mechanical, total in ((40, 41), (45, 46)):
            with self.subTest(mechanical=mechanical):
                observations = self.reconcile(
                    self._historical_shape(mechanical),
                    index_mode="human-ledger",
                )["observations"]
                classes = [item["class"] for item in observations]
                self.assertEqual(len(observations), total)
                self.assertEqual(
                    classes.count("manifest_review_artifact_not_indexed"),
                    mechanical,
                )
                self.assertEqual(classes.count("verdict_record_missing"), 1)

    def test_no_ledger_clean_boundary_has_no_mechanical_noise(self) -> None:
        index = self.project / INDEX
        index.write_text(
            "# Review Index\n\n"
            "| Milestone | Slice | Round | Self-Report | Review Prompt | "
            "Review Report | Verdict | Commit |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- |\n",
            encoding="utf-8",
        )
        bundle = self.reconcile(
            self._historical_shape(45), index_mode="no-ledger"
        )
        self.assertEqual(bundle["index_mode"], "no-ledger")
        self.assertEqual(bundle["observations"], [])

    def test_no_ledger_row_is_one_loud_tripwire_and_t2_stays_live(self) -> None:
        observations = self.reconcile(
            self._historical_shape(0), index_mode="no-ledger"
        )["observations"]
        classes = [item["class"] for item in observations]
        self.assertEqual(classes.count("ledger_row_in_no_ledger_project"), 1)
        self.assertEqual(classes.count("verdict_record_missing"), 1)
        self.assertNotIn("index_path_not_in_manifest", classes)
        anomaly = next(
            item
            for item in observations
            if item["class"] == "ledger_row_in_no_ledger_project"
        )
        self.assertEqual(anomaly["slice_id"], "M001-S01")
        self.assertEqual(
            anomaly["paths"], [SELF_REPORT, REVIEW_PROMPT, REVIEW_REPORT]
        )

    def test_no_ledger_tripwire_subsumes_t1_and_catches_malformed_rows(self) -> None:
        missing = "05_governance/reviews/m001/not_frozen.md"
        index = self.project / INDEX
        index.write_text(
            index.read_text(encoding="utf-8").replace(
                "| pass | fixture |", f"| pass | `{missing}` |"
            )
            + f"| malformed row | `{missing}` |\n",
            encoding="utf-8",
        )
        observations = self.reconcile(
            self._record(), index_mode="no-ledger"
        )["observations"]
        anomalies = [
            item
            for item in observations
            if item["class"] == "ledger_row_in_no_ledger_project"
        ]
        self.assertEqual(len(anomalies), 2)
        self.assertTrue(all(missing in item["paths"] for item in anomalies))
        self.assertFalse(
            any(
                item["class"] == "index_path_not_in_manifest"
                for item in observations
            )
        )

    def test_strict_drained_chain_adds_pointer_annotation(self) -> None:
        self._write_events(self._drained_chain())
        bundle = self.reconcile(self._historical_shape(0))
        missing = next(
            item
            for item in bundle["observations"]
            if item["class"] == "verdict_record_missing"
        )
        self.assertEqual(
            missing["annotation"],
            "addressed by rework round 1; re-confirm or refute against it",
        )
        self.assertTrue(
            valid_oracle_bundle(
                bundle,
                "run_001",
                bundle["pass_boundary_sha256"],
                "human-ledger",
            )
        )
        malformed = {
            **bundle,
            "observations": [
                {**missing, "annotation": "already fixed"}
                if item is missing
                else item
                for item in bundle["observations"]
            ],
        }
        self.assertFalse(
            valid_oracle_bundle(
                malformed,
                "run_001",
                bundle["pass_boundary_sha256"],
                "human-ledger",
            )
        )

    def test_partial_or_ambiguous_chain_stays_unannotated(self) -> None:
        complete = self._drained_chain()
        cases = {
            "partial": [event for event in complete if event["kind"] != "verification"],
            "ambiguous": [complete[0], *complete],
            "ambiguous_collection": [
                *complete[:-1],
                {
                    "kind": "collected",
                    "role": "coder",
                    "slice": "M001-S01",
                    "attempt": "attempt_ambiguous",
                    "status": "completed",
                },
                complete[-1],
            ],
            "malformed_declaration": [
                {
                    **complete[0],
                    "slices": ["M001-S01", ["M001-S02"]],
                },
                *complete[1:],
            ],
        }
        for name, events in cases.items():
            with self.subTest(name=name):
                self._write_events(events)
                observations = self.reconcile(
                    self._historical_shape(0)
                )["observations"]
                missing = next(
                    item
                    for item in observations
                    if item["class"] == "verdict_record_missing"
                )
                self.assertNotIn("annotation", missing)

    def test_t1_index_path_missing_from_manifest_is_an_observation(self) -> None:
        index = self.project / INDEX
        missing = "05_governance/reviews/m001/missing" + ".md"
        index.write_text(
            index.read_text(encoding="utf-8").replace(
                "| pass | fixture |",
                f"| pass | `{missing}` |",
            ),
            encoding="utf-8",
        )
        record = self._record()
        record["artifacts"] = [
            member
            for member in record["artifacts"]
            if member["path"] != missing
        ]
        observations = self.reconcile(record)["observations"]
        self.assertIn("index_path_not_in_manifest", [item["class"] for item in observations])
        missing = next(
            item for item in observations
            if item["class"] == "index_path_not_in_manifest"
        )
        self.assertEqual(missing["slice_id"], "M001-S01")
        self.assertEqual(
            missing["paths"], ["05_governance/reviews/m001/missing" + ".md"]
        )

    def test_manifest_review_artifact_missing_from_index_is_an_observation(self) -> None:
        extra = self.project / "05_governance/reviews/m001/m001_s02_self_report.md"
        extra.write_text("# Unindexed Self-Report\n", encoding="utf-8")
        observations = self.reconcile(self._record())["observations"]
        reverse = next(
            item
            for item in observations
            if item["class"] == "manifest_review_artifact_not_indexed"
            and item["paths"] == [
                "05_governance/reviews/m001/m001_s02_self_report.md"
            ]
        )
        self.assertEqual(reverse["slice_id"], "M001-S02")

    def test_t2_verdict_contradiction_is_an_observation(self) -> None:
        verdict = self.project / VERDICT_RECORD
        verdict.write_text(
            verdict.read_text(encoding="utf-8").replace(
                "Verdict: `pass`", "Verdict: `needs_work`"
            ),
            encoding="utf-8",
        )
        record = self._record()
        observations = self.reconcile(record)["observations"]
        mismatch = next(
            item for item in observations if item["class"] == "verdict_value_mismatch"
        )
        self.assertEqual(mismatch["type"], "oracle_observation")
        self.assertEqual(mismatch["slice_id"], "M001-S01")
        self.assertEqual(
            mismatch["paths"], [VERDICT_RECORD, REVIEW_REPORT]
        )

    def test_accepted_slice_without_frozen_verdict_record_is_an_observation(self) -> None:
        record = self._record()
        record["artifacts"] = [
            member
            for member in record["artifacts"]
            if member["path"] != VERDICT_RECORD
        ]
        observations = self.reconcile(record)["observations"]
        missing = next(
            item for item in observations if item["class"] == "verdict_record_missing"
        )
        self.assertEqual(missing["slice_id"], "M001-S01")
        self.assertEqual(missing["paths"], [VERDICT_RECORD])

    def test_hash_drift_is_an_observation(self) -> None:
        (self.project / SELF_REPORT).write_bytes(b"changed after freeze\n")
        observations = self.reconcile()["observations"]
        drift = next(
            item for item in observations if item["class"] == "artifact_hash_drift"
        )
        self.assertEqual(drift["paths"], [SELF_REPORT])
        self.assertEqual(drift["hashes"][0]["recorded_sha256"], next(
            item["sha256"] for item in self.record["artifacts"]
            if item["path"] == SELF_REPORT
        ))
        self.assertEqual(drift["hashes"][0]["observed_sha256"], _sha(self.project / SELF_REPORT))

    def test_unreadable_named_artifact_and_index_fail_closed(self) -> None:
        (self.project / SELF_REPORT).unlink()
        with self.assertRaisesRegex(OracleRefusal, "artifact_unreadable"):
            self.reconcile()

        (self.project / INDEX).unlink()
        with self.assertRaisesRegex(OracleRefusal, "index_unreadable"):
            self.reconcile()


class OracleRunStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = RunStore(Path(self._tmp.name) / "store")
        self.store.create_run("run_001", {"contract_version": 1})

    def test_write_once_read_and_conflict(self) -> None:
        payload = {
            "contract_version": 1,
            "run_id": "run_001",
            "pass_boundary_sha256": "0" * 64,
            "observations": [],
        }
        path = self.store.write_pass_oracle("run_001", payload)
        frozen = path.read_bytes()
        self.store.write_pass_oracle("run_001", payload)
        self.assertEqual(path.read_bytes(), frozen)
        self.assertEqual(self.store.read_pass_oracle("run_001"), payload)
        with self.assertRaisesRegex(RunStoreRefusal, "pass_oracle_conflict"):
            self.store.write_pass_oracle(
                "run_001", {**payload, "observations": [{"x": "different"}]}
            )
        self.assertEqual(path.read_bytes(), frozen)

    def test_oversized_bundle_is_refused_before_publication(self) -> None:
        payload = {"padding": "x" * MAX_PASS_ORACLE_BYTES}
        with self.assertRaisesRegex(RunStoreRefusal, "pass_oracle_oversized"):
            self.store.write_pass_oracle("run_001", payload)
        self.assertFalse(
            (self.store.run_dir("run_001") / "pass_oracle.json").exists()
        )

    def test_oversized_existing_bundle_is_refused_on_read(self) -> None:
        path = self.store.run_dir("run_001") / "pass_oracle.json"
        path.write_bytes(b"{" + b" " * MAX_PASS_ORACLE_BYTES + b"}")
        with self.assertRaisesRegex(RunStoreRefusal, "pass_oracle_oversized"):
            self.store.read_pass_oracle("run_001")


if __name__ == "__main__":
    unittest.main()
