"""Active-acceptance-lifecycle replay regressions for the ladder count."""

import unittest

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

from frutlups_drive import ladder
from frutlups_drive.budget import BudgetCounters
from frutlups_drive.contracts import StopReason


class LadderLifecycleReplayTests(unittest.TestCase):
    def test_rework_starts_fresh_ladder_lifecycle_at_every_replay_boundary(self):
        events = [
            {"kind": "dispatch", "t": 1.0, "role": "coder", "slice": "S1",
             "repair": False},
            {"kind": "collected", "t": 2.0, "role": "coder", "slice": "S1",
             "status": "completed", "cost_usd": None},
            {"kind": "verb", "t": 3.0, "verb": "record-verdict",
             "slice": "S1", "artifact": "accepted_verdict.md"},
            {"kind": "verb", "t": 4.0, "verb": "declare-rework",
             "slice": "", "pass_id": "holistic_pass_001", "slices": ["S1"]},
            {"kind": "dispatch", "t": 5.0, "role": "coder", "slice": "S1",
             "repair": False},
            {"kind": "collected", "t": 6.0, "role": "coder", "slice": "S1",
             "status": "completed", "cost_usd": None},
            {"kind": "dispatch", "t": 7.0, "role": "coder", "slice": "S1",
             "repair": False},
            {"kind": "collected", "t": 8.0, "role": "coder", "slice": "S1",
             "status": "completed", "cost_usd": None},
        ]

        incremental = BudgetCounters()
        for boundary, event in enumerate(events, start=1):
            incremental.apply(event)
            replayed = BudgetCounters.from_events(events[:boundary])
            self.assertEqual(
                replayed.lifecycle_coder_collected_for("S1"),
                incremental.lifecycle_coder_collected_for("S1"),
                f"journal replay diverged after transition boundary {boundary}",
            )
            self.assertEqual(
                replayed.coder_collected_for("S1"),
                incremental.coder_collected_for("S1"),
            )

        self.assertEqual(incremental.lifecycle_coder_collected_for("S1"), 2)
        self.assertEqual(incremental.coder_collected_for("S1"), 3)
        self.assertEqual(incremental.coder_dispatches_for("S1"), 3)
        self.assertEqual(
            ladder.check_ladder(
                1, incremental.lifecycle_coder_collected_for("S1"), False
            ),
            StopReason.LADDER_ROUND3,
        )


if __name__ == "__main__":
    unittest.main()
