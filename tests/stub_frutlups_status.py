"""Reviewer-visible local stub CLI for FrutlupsPlanProvider tests.

This is NOT frutlups: it deterministically emits released-shaped
``frutlups status --json`` wrappers (the atomic ``planning_frontier`` +
``loop_resume`` members) or one scripted transport failure so the released
provider's refusal surface can be exercised offline. Valid released wrappers
come from committed captured/curated fixture files served byte-for-byte via
``raw``.

Usage (always launched by the tests through the official interpreter with an
explicit absolute script path — never discovered from PATH):

    python stub_frutlups_status.py raw <wrapper-file>     # file bytes as-is
    python stub_frutlups_status.py missing-frontier-member
    python stub_frutlups_status.py missing-resume-member
    python stub_frutlups_status.py non-object-frontier-member
    python stub_frutlups_status.py legacy-planning-state  # retired member only
    python stub_frutlups_status.py malformed              # not JSON
    python stub_frutlups_status.py constants              # NaN in wrapper
    python stub_frutlups_status.py two-documents <wrapper-file>
    python stub_frutlups_status.py huge-stdout            # > 1 MiB stdout
    python stub_frutlups_status.py huge-stderr <wrapper-file>
    python stub_frutlups_status.py nonzero                # exit 3
    python stub_frutlups_status.py hang                   # wait until killed

No sleeps: ``hang`` blocks on an Event until the bounded runner ends it.
"""

import json
import sys
import threading

RELEASED_FRONTIER = {
    "contract_id": "frutlups.planning_frontier",
    "contract_version": "1",
    "outcome": "ready",
    "action": "",
    "actor": "",
    "block_citation": "",
    "block_owner": "",
    "completion_evidence": "",
    "diagnostics": [],
}

RELEASED_RESUME = {
    "step": "make_coding_prompt",
    "message": "",
    "next_command": "",
    "frontier_slice_id": "M001-S01",
    "frontier_slice_title": "first fixture slice",
    "coding_prompt_path": "",
    "self_report_path": "",
    "review_prompt_path": "",
    "review_report_path": "",
    "verdict_record_path": "",
    "diagnostics": [],
}

MEMORY_MODE = {
    "contract_id": "frutlups.memory_mode",
    "contract_version": "1",
    "valid": True,
    "mode": "none",
    "memory_root": None,
    "diagnostics": [],
}


def _file_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def main() -> int:
    mode = sys.argv[1]
    if mode == "raw":
        sys.stdout.buffer.write(_file_bytes(sys.argv[2]))
        return 0
    if mode == "missing-frontier-member":
        sys.stdout.write(
            json.dumps(
                {
                    "ok": True,
                    "memory_mode": MEMORY_MODE,
                    "loop_resume": RELEASED_RESUME,
                }
            )
        )
        return 0
    if mode == "missing-resume-member":
        sys.stdout.write(
            json.dumps(
                {
                    "ok": True,
                    "memory_mode": MEMORY_MODE,
                    "planning_frontier": RELEASED_FRONTIER,
                }
            )
        )
        return 0
    if mode == "non-object-frontier-member":
        sys.stdout.write(
            json.dumps(
                {
                    "memory_mode": MEMORY_MODE,
                    "planning_frontier": [1, 2],
                    "loop_resume": RELEASED_RESUME,
                }
            )
        )
        return 0
    if mode == "legacy-planning-state":
        sys.stdout.write(
            json.dumps(
                {
                    "memory_mode": MEMORY_MODE,
                    "planning_state": {
                        "contract": "frutlups_planning_state",
                        "version": 1,
                        "outcome": "ready",
                        "step": "make_coding_prompt",
                    }
                }
            )
        )
        return 0
    if mode == "malformed":
        sys.stdout.write("{not-json")
        return 0
    if mode == "constants":
        sys.stdout.write(
            '{"planning_frontier": {"contract_id": '
            '"frutlups.planning_frontier", "contract_version": "1", '
            '"outcome": "ready", "round": NaN}, "loop_resume": {}}'
        )
        return 0
    if mode == "two-documents":
        document = _file_bytes(sys.argv[2]).decode("utf-8")
        sys.stdout.write(document + "\n" + document)
        return 0
    if mode == "huge-stdout":
        sys.stdout.write("x" * (1_048_576 + 1))
        return 0
    if mode == "huge-stderr":
        sys.stderr.write("e" * (1_048_576 + 1))
        sys.stderr.flush()
        sys.stdout.buffer.write(_file_bytes(sys.argv[2]))
        return 0
    if mode == "nonzero":
        sys.stderr.write("scripted failure\n")
        return 3
    if mode == "hang":
        sys.stdout.write("hang-ready")
        sys.stdout.flush()
        threading.Event().wait(60)
        return 0
    sys.stderr.write(f"unknown stub mode: {mode}\n")
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
