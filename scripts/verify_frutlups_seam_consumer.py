"""Run the portable, offline frutlups 0.2 seam-consumer proof."""

from __future__ import annotations

import argparse
import importlib
import io
import sys
import unittest
from pathlib import Path
from types import ModuleType


sys.dont_write_bytecode = True

PRODUCER_COMMIT = "87355a9189c5096826401f1468a488de2acb90ac"
FIXTURE_MANIFEST_SHA256 = (
    "f88336f1d70c6f3fbf05bec19bcbb36e0fbdae0ee412de9e4f0d961f8f839b93"
)
FIXTURE_CASE_COUNTS = (
    ("dry_run", 4),
    ("frontier", 21),
    ("payload", 12),
    ("publication", 4),
    ("refusal", 32),
)
EXPECTED_CONSUMER_TESTS = 22
EXPECTED_REPLAY_TESTS = 2
REQUIRED_TEST_MODULES = (
    "test_seam_consumer.py",
    "test_seam_fixture_replay.py",
)


class QualificationLayoutError(Exception):
    """The proof runner is not in a supported checkout layout."""


def qualification_test_root(script_path: Path | None = None) -> Path:
    resolved_script = (script_path or Path(__file__)).resolve()
    test_root = resolved_script.parent.parent / "tests"
    if not test_root.is_dir() or any(
        not (test_root / name).is_file() for name in REQUIRED_TEST_MODULES
    ):
        raise QualificationLayoutError(
            "qualification_layout_unavailable: required seam tests are not "
            "present beside the proof runner"
        )
    return test_root


def _qualification_modules(test_root: Path) -> tuple[ModuleType, ModuleType]:
    test_root_text = str(test_root)
    if test_root_text not in sys.path:
        sys.path.insert(0, test_root_text)
    return (
        importlib.import_module("test_seam_consumer"),
        importlib.import_module("test_seam_fixture_replay"),
    )


def qualification_suite(
    seam_python: Path,
    *,
    script_path: Path | None = None,
) -> tuple[unittest.TestSuite, int, int]:
    test_root = qualification_test_root(script_path)
    consumer_module, replay_module = _qualification_modules(test_root)
    replay_module.SEAM_PYTHON = seam_python.resolve()
    loader = unittest.defaultTestLoader
    consumer_suite = loader.loadTestsFromModule(consumer_module)
    replay_suite = loader.loadTestsFromModule(replay_module)
    consumer_tests = consumer_suite.countTestCases()
    replay_tests = replay_suite.countTestCases()
    return (
        unittest.TestSuite((consumer_suite, replay_suite)),
        consumer_tests,
        replay_tests,
    )


def proof_summary(*, passed: bool, consumer_tests: int, replay_tests: int) -> str:
    fixture_counts = ",".join(
        f"{name}:{count}" for name, count in FIXTURE_CASE_COUNTS
    )
    fixture_total = sum(count for _, count in FIXTURE_CASE_COUNTS)
    return (
        "frutlups-seam-consumer-proof/v1 "
        f"result={'PASS' if passed else 'FAIL'} "
        f"producer_commit={PRODUCER_COMMIT} "
        f"manifest_sha256={FIXTURE_MANIFEST_SHA256} "
        f"fixture_cases={fixture_counts},total:{fixture_total} "
        f"consumer_tests={consumer_tests} replay_tests={replay_tests} "
        f"total_tests={consumer_tests + replay_tests}"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seam-python",
        required=True,
        type=Path,
        help="explicit interpreter for the pinned frutlups producer",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    seam_python = args.seam_python.resolve()
    if not seam_python.is_file():
        _parser().error("--seam-python must resolve to an existing file")

    try:
        suite, consumer_tests, replay_tests = qualification_suite(seam_python)
    except QualificationLayoutError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    captured = io.StringIO()
    result = unittest.TextTestRunner(stream=captured, verbosity=1).run(suite)
    inventory_matches = (
        consumer_tests == EXPECTED_CONSUMER_TESTS
        and replay_tests == EXPECTED_REPLAY_TESTS
    )
    passed = result.wasSuccessful() and inventory_matches
    if not passed:
        sys.stderr.write(captured.getvalue())
        if not inventory_matches:
            sys.stderr.write(
                "qualification inventory mismatch: "
                f"consumer={consumer_tests}, replay={replay_tests}\n"
            )
    print(
        proof_summary(
            passed=passed,
            consumer_tests=consumer_tests,
            replay_tests=replay_tests,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
