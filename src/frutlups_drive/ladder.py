"""Mechanical convergence-ladder cap (architecture contract §9, owner ruling).

The runner never computes invariant-level recurrence; it enforces the
conservative mechanical over-approximation: the effective round is the higher
of the planning state's scripted frontier round and the run store's corrective
coder-dispatch count for the slice (the dispatch about to happen included).
Round 3 stops for architect reassessment; round 4 or later requires an
explicitly injected recorded human authorization and refuses conservatively
without it.
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
