"""H004 cross-run adoption and post-verification effect authority."""

import hashlib
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

from frutlups_drive.contracts import StopReason
from frutlups_drive.dispatch.mock import MockAgentAction

from _scenario import (
    DEFAULT_VERBS,
    REVIEW_PROMPT,
    REVIEW_REPORT,
    SELF_REPORT,
    Scenario,
    payload,
)


CODER_WRITES = MockAgentAction(writes=((SELF_REPORT, "# Preserved coder work\n"),))
REVIEWER_WRITES = MockAgentAction(
    writes=((REVIEW_REPORT, "Verdict: pass - next: record\n"),)
)


def attempt_bytes(attempt):
    return {
        path.relative_to(attempt).as_posix(): path.read_bytes()
        for path in attempt.rglob("*")
        if path.is_file()
    }


class RecoveryContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def failed_prior_run(self):
        scenario = Scenario(
            self.root,
            run_id="run_002",
            states=[payload("ready", "execute_coding_prompt")],
            coder=[CODER_WRITES],
            verifier_exit_codes=[1],
        )
        result = scenario.supervisor.tick()
        self.assertEqual(result.detail, "verification_failed")
        attempt = scenario.store.list_attempts("run_002", "M001-S01")[0]
        self.assertFalse(
            next(
                e["passed"]
                for e in scenario.events()
                if e["kind"] == "verification"
            )
        )
        return scenario, attempt

    def test_run_002_run_003_stranding_fixture_continues_by_fresh_verification(self):
        """Pre-H004 behavior stopped run_003 with verification_missing."""
        prior, prior_attempt = self.failed_prior_run()
        immutable_before = attempt_bytes(prior_attempt)
        current = Scenario(
            self.root,
            project=prior.project,
            run_id="run_003",
            states=[
                payload(
                    "ready",
                    "make_review_prompt",
                    actor="orchestrator",
                    review_prompt=REVIEW_PROMPT,
                ),
                payload(
                    "ready",
                    "execute_review_prompt",
                    actor="reviewer",
                    review_prompt=REVIEW_PROMPT,
                    review_report=REVIEW_REPORT,
                ),
            ],
            reviewer=[REVIEWER_WRITES],
            verbs=DEFAULT_VERBS,
        )

        first = current.supervisor.tick()
        second = current.supervisor.tick()
        self.assertEqual(first.detail, "verb:make-review-prompt")
        self.assertEqual(second.detail, "reviewer_attempt_completed")
        self.assertEqual(attempt_bytes(prior_attempt), immutable_before)

        adoptions = [e for e in current.events() if e["kind"] == "adoption"]
        self.assertEqual(len(adoptions), 1)
        adoption = adoptions[0]
        self.assertEqual(adoption["prior_run"], "run_002")
        self.assertEqual(adoption["prior_attempt"], "attempt_001")
        self.assertIn("request.json", adoption["evidence_sha256"])
        self.assertIn(
            "verification/evidence.toml", adoption["evidence_sha256"]
        )
        for name, digest in adoption["evidence_sha256"].items():
            self.assertEqual(
                hashlib.sha256((prior_attempt / name).read_bytes()).hexdigest(),
                digest,
            )

        attempts = current.store.list_attempts("run_003", "M001-S01")
        adopted = attempts[0]
        record = current.store.read_adoption(adopted)
        self.assertEqual(record["prior_run_id"], "run_002")
        verification = next(
            e
            for e in current.events()
            if e["kind"] == "verification" and e["attempt"] == adopted.name
        )
        self.assertTrue(verification["passed"])
        evidence = (adopted / "verification/evidence.toml").read_text("utf-8")
        self.assertIn('run_id = "run_003"', evidence)
        self.assertEqual(
            [
                e
                for e in current.events()
                if e["kind"] == "collected" and e.get("role") == "coder"
            ],
            [],
            "adoption never duplicates prior provider collection",
        )

    def test_failed_latest_verification_blocks_governed_verb_effect(self):
        prior, _ = self.failed_prior_run()
        resumed = Scenario(
            self.root,
            project=prior.project,
            run_id="run_002",
            states=[
                payload(
                    "ready",
                    "make_review_prompt",
                    actor="orchestrator",
                    review_prompt=REVIEW_PROMPT,
                )
            ],
            verbs=DEFAULT_VERBS,
        )
        result = resumed.supervisor.tick()
        self.assertEqual(result.stop_reason, StopReason.VERIFICATION_MISSING)
        self.assertFalse((resumed.project / REVIEW_PROMPT).exists())
        self.assertEqual(
            [e for e in resumed.events() if e["kind"] == "verb"], []
        )

    def test_tampered_prior_envelope_refuses_without_effect(self):
        prior, attempt = self.failed_prior_run()
        (attempt / "verification/cmd_000_stdout.txt").write_bytes(b"tampered\n")
        current = Scenario(
            self.root,
            project=prior.project,
            run_id="run_003",
            states=[
                payload(
                    "ready",
                    "make_review_prompt",
                    actor="orchestrator",
                    review_prompt=REVIEW_PROMPT,
                )
            ],
            verbs=DEFAULT_VERBS,
        )
        result = current.supervisor.tick()
        self.assertEqual(result.stop_reason, StopReason.VERIFICATION_MISSING)
        self.assertIn("evidence_stream_tampered", result.detail)
        self.assertFalse((current.project / REVIEW_PROMPT).exists())
        self.assertEqual(
            current.store.list_attempts("run_003", "M001-S01"), ()
        )


if __name__ == "__main__":
    unittest.main()
