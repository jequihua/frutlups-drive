"""Convergence-ladder cap over a supervisor-normalized recurrence count.

The supervisor supplies the number of prior, journaled product-finding events
for one normalized recurrence key. Mechanical transport, environment,
path-contract, and operator kill-switch events are excluded before this small
cap function is called. Legacy journals without typed ladder events retain the
prior frontier/collection interpretation. Round 3 stops for architect
reassessment; round 4 or later requires an explicitly injected recorded human
authorization and refuses conservatively without it.
"""

from __future__ import annotations

from frutlups_drive.contracts import StopReason


def check_ladder(
    frontier_round: int,
    prior_coder_dispatches: int,
    round4_authorized: bool,
) -> StopReason | None:
    effective_round = max(frontier_round, prior_coder_dispatches + 1)
    if effective_round >= 4:
        return None if round4_authorized else StopReason.LADDER_ROUND4_UNAUTHORIZED
    if effective_round == 3:
        return StopReason.LADDER_ROUND3
    return None
