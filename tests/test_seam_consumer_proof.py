"""Contract tests for the portable frutlups seam-consumer proof."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


TEST_ROOT = Path(__file__).resolve().parent
PACKAGE_ROOT = TEST_ROOT.parent
CHECKOUT_ROOT = PACKAGE_ROOT.parent if PACKAGE_ROOT.name == "08_pkg" else PACKAGE_ROOT
PROOF_PATH = PACKAGE_ROOT / "scripts" / "verify_frutlups_seam_consumer.py"
ANSWER_PATH = (
    CHECKOUT_ROOT / "02_analysis" / "frutlups_0_2_consumer_identity_answer.md"
    if PACKAGE_ROOT.name == "08_pkg"
    else None
)
README_PATH = PACKAGE_ROOT / "README.md"
SEAM_PYTHON = (
    CHECKOUT_ROOT.parent
    / "venvs"
    / "frutlups-drive-seam-87355a9"
    / "Scripts"
    / "python.exe"
)


def load_proof_module():
    spec = importlib.util.spec_from_file_location("seam_consumer_proof", PROOF_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("portable proof module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SeamConsumerProofContractTests(unittest.TestCase):
    def test_summary_content_is_pinned(self):
        proof = load_proof_module()
        self.assertEqual(
            proof.proof_summary(passed=True, consumer_tests=22, replay_tests=2),
            "frutlups-seam-consumer-proof/v1 result=PASS "
            "producer_commit=87355a9189c5096826401f1468a488de2acb90ac "
            "manifest_sha256="
            "f88336f1d70c6f3fbf05bec19bcbb36e0fbdae0ee412de9e4f0d961f8f839b93 "
            "fixture_cases=dry_run:4,frontier:21,payload:12,publication:4,"
            "refusal:32,total:73 consumer_tests=22 replay_tests=2 total_tests=24",
        )

    def test_qualification_inventory_and_seam_binding_are_exact(self):
        if not SEAM_PYTHON.is_file():
            self.skipTest(
                "governed frutlups seam qualification interpreter is absent"
            )
        proof = load_proof_module()
        suite, consumer_tests, replay_tests = proof.qualification_suite(SEAM_PYTHON)
        self.assertEqual((consumer_tests, replay_tests), (22, 2))
        self.assertEqual(suite.countTestCases(), 24)

    def test_answer_placeholders_and_operational_boundary_are_pinned(self):
        readme = README_PATH.read_text(encoding="utf-8")
        development_invocation = (
            "& $projectPython 08_pkg/scripts/verify_frutlups_seam_consumer.py "
            "--seam-python $seamPython"
        )
        front_invocation = (
            "& $projectPython scripts/verify_frutlups_seam_consumer.py "
            "--seam-python $seamPython"
        )
        self.assertIn(development_invocation, readme)
        self.assertIn(front_invocation, readme)
        self.assertIn("run on released\nfrutlups 0.2.1", readme)
        if ANSWER_PATH is not None:
            # The answer is COMPLETED at M010 closure (owner note 056 release
            # postscript): no placeholder survives and the remote-verified
            # v0.6.0 identity is pinned exactly.
            answer = ANSWER_PATH.read_text(encoding="utf-8")
            self.assertEqual(answer.count("OWNER_COMPLETES_AT_CLOSURE"), 0)
            self.assertIn("github.com/jequihua/frutlups-drive", answer)
            self.assertIn("frutlups-drive 0.6.0", answer)
            self.assertIn(
                "c6a9f685f1b13317f34f548426978dfca9cf9885", answer
            )
            self.assertIn(
                "adf7092f51b2e5cffceba271f9d723f50b0d4028", answer
            )
            self.assertIn(front_invocation, answer)
            self.assertIn(
                "frutlups-drive 0.5.0 is explicitly not\nan answer", answer
            )

    def test_development_and_front_layout_probes_and_bounded_refusal(self):
        proof = load_proof_module()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            for relative in (
                Path("development") / "08_pkg" / "scripts" / "proof.py",
                Path("front") / "scripts" / "proof.py",
            ):
                script_path = base / relative
                test_root = script_path.parent.parent / "tests"
                test_root.mkdir(parents=True)
                for name in proof.REQUIRED_TEST_MODULES:
                    (test_root / name).write_text("# probe\n", encoding="utf-8")
                self.assertEqual(
                    proof.qualification_test_root(script_path),
                    test_root,
                )

            missing_script = base / "missing" / "scripts" / "proof.py"
            with self.assertRaisesRegex(
                proof.QualificationLayoutError,
                "^qualification_layout_unavailable: required seam tests are not "
                "present beside the proof runner$",
            ):
                proof.qualification_test_root(missing_script)


if __name__ == "__main__":
    unittest.main()
