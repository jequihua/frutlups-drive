"""Budget meters over an injected clock (architecture contract §4, §7).

The supervisor journals events; :class:`BudgetCounters` derives every meter
from those events, so a fresh process replaying the journal reaches the same
counters. Each limit is checked before the side effect it forbids and maps to
one stable stop reason:

- wall clock, total cost, slices, passes, coder attempts -> ``BUDGET_EXHAUSTED``;
- report repairs -> ``REPAIR_EXHAUSTED``;
- reconciliations without progress -> ``NO_PROGRESS``;
- consecutive loop iterations without progress -> ``NO_PROGRESS``;
- consecutive provider failures -> ``PROVIDER_FAILURE``.

Phase B counts a pass only from a durable ``holistic_review`` fact. Milestone
rollovers remain slice facts and no longer approximate pass completion.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Protocol

from frutlups_drive.contracts import StopReason
from frutlups_drive.policy import ExecutionPolicy


class Clock(Protocol):
    def now(self) -> float:
        """Seconds since an arbitrary fixed reference."""
        ...


def _validate_cost_fact(value: object) -> float | None:
    """The one total cost-fact boundary (R2-F2, R3-F1, R4-F1): ``None`` or a
    value whose *exact* type is the built-in ``int`` or ``float`` and whose
    conversion yields a finite non-negative ``float``. Booleans and every
    numeric subclass are invalid before any conversion — a subclass
    ``__float__`` hook is never invoked. Non-numbers, negative values,
    ``NaN``, both infinities, and conversion failures such as
    ``OverflowError`` on arbitrary-size integers reach this same owned
    invalid-cost decision; no raw conversion exception escapes and the
    invalid value is never echoed."""
    if value is None:
        return None
    if type(value) is not int and type(value) is not float:
        raise ValueError("cost fact must be a finite non-negative number")
    try:
        fact = float(value)
    except OverflowError:
        raise ValueError(
            "cost fact must be a finite non-negative number"
        ) from None
    if not math.isfinite(fact) or fact < 0.0:
        raise ValueError("cost fact must be a finite non-negative number")
    return fact


class BudgetCounters:
    """Meters reconstructed from journal events via :meth:`apply`."""

    def __init__(self) -> None:
        self.run_started_at: float | None = None
        self.coder_dispatches: dict[str, int] = {}
        self.coder_collected: dict[str, int] = {}
        self.lifecycle_coder_collected: dict[str, int] = {}
        self.repair_dispatches: dict[str, int] = {}
        self.reconciliation_dispatches: dict[str, int] = {}
        self.total_cost_usd = 0.0
        self.slices_completed = 0
        self.passes_completed = 0
        self.consecutive_provider_failures = 0
        self.consecutive_no_progress = 0
        self.reconciliations_without_progress = 0
        self.cost_fact_invalid = False

    @classmethod
    def from_events(cls, events: Iterable[Mapping[str, object]]) -> "BudgetCounters":
        counters = cls()
        for event in events:
            counters.apply(event)
        return counters

    def apply(self, event: Mapping[str, object]) -> None:
        kind = event.get("kind")
        if self.run_started_at is None and isinstance(event.get("t"), (int, float)):
            self.run_started_at = float(event["t"])
        if kind == "dispatch":
            slice_id = str(event.get("slice"))
            if event.get("repair"):
                self.repair_dispatches[slice_id] = (
                    self.repair_dispatches.get(slice_id, 0) + 1
                )
            elif event.get("role") == "coder":
                self.coder_dispatches[slice_id] = (
                    self.coder_dispatches.get(slice_id, 0) + 1
                )
            elif event.get("role") == "architect":
                self.reconciliation_dispatches[slice_id] = (
                    self.reconciliation_dispatches.get(slice_id, 0) + 1
                )
        elif kind == "verb":
            slices = event.get("slices")
            if event.get("verb") == "declare-rework" and isinstance(slices, list):
                for slice_id in slices:
                    if isinstance(slice_id, str):
                        self.lifecycle_coder_collected.pop(slice_id, None)
        elif kind == "collected":
            cost = event.get("cost_usd")
            if cost is not None:
                try:
                    self.total_cost_usd += _validate_cost_fact(cost)
                except ValueError:
                    # R2-F2: a tampered or legacy invalid durable fact fails
                    # closed. Spend never decreases and authorization never
                    # reopens; the gate stops before any further dispatch.
                    self.cost_fact_invalid = True
            if event.get("role") == "coder":
                slice_id = str(event.get("slice"))
                self.coder_collected[slice_id] = (
                    self.coder_collected.get(slice_id, 0) + 1
                )
                self.lifecycle_coder_collected[slice_id] = (
                    self.lifecycle_coder_collected.get(slice_id, 0) + 1
                )
            if event.get("status") == "completed":
                self.consecutive_provider_failures = 0
            else:
                self.consecutive_provider_failures += 1
        elif kind in ("attempt_abandoned", "watch_timeout"):
            self.consecutive_provider_failures += 1
        elif kind == "reconciliation":
            if event.get("progress"):
                self.reconciliations_without_progress = 0
            else:
                self.reconciliations_without_progress += 1
        elif kind == "slice_complete":
            self.slices_completed += 1
        elif kind == "holistic_review":
            self.passes_completed += 1
        elif kind == "tick":
            if event.get("result") in ("acted", "boundary"):
                if event.get("progress") is True:
                    self.consecutive_no_progress = 0
                else:
                    self.consecutive_no_progress += 1
        elif kind == "resume":
            self.consecutive_provider_failures = 0
            self.consecutive_no_progress = 0

    def coder_dispatches_for(self, slice_id: str) -> int:
        return self.coder_dispatches.get(slice_id, 0)

    def coder_collected_for(self, slice_id: str) -> int:
        return self.coder_collected.get(slice_id, 0)

    def lifecycle_coder_collected_for(self, slice_id: str) -> int:
        return self.lifecycle_coder_collected.get(slice_id, 0)

    def repairs_for(self, slice_id: str) -> int:
        return self.repair_dispatches.get(slice_id, 0)

    def reconciliations_for(self, slice_id: str) -> int:
        return self.reconciliation_dispatches.get(slice_id, 0)


class BudgetGate:
    def __init__(self, policy: ExecutionPolicy, clock: Clock) -> None:
        self._policy = policy
        self._clock = clock

    def check_global(self, counters: BudgetCounters) -> tuple[StopReason, str] | None:
        limits = self._policy.limits
        target = self._policy.target
        if counters.run_started_at is not None:
            elapsed_minutes = (self._clock.now() - counters.run_started_at) / 60.0
            if elapsed_minutes >= limits.max_wall_clock_minutes:
                return (StopReason.BUDGET_EXHAUSTED, "wall_clock")
        # R1-F5: the configured maximum is an authorization ceiling. A
        # positive accumulated total equal to a positive maximum is
        # exhaustion; 0.0 spent against a 0.0 maximum stays a zero-spend fact.
        if counters.cost_fact_invalid:
            return (StopReason.BUDGET_EXHAUSTED, "cost_fact_invalid")
        maximum = limits.max_total_cost_usd
        if (maximum > 0.0 and counters.total_cost_usd >= maximum) or (
            counters.total_cost_usd > maximum
        ):
            return (StopReason.BUDGET_EXHAUSTED, "total_cost")
        if counters.slices_completed >= target.max_slices:
            return (StopReason.BUDGET_EXHAUSTED, "slices")
        if counters.passes_completed >= target.max_passes:
            return (StopReason.BUDGET_EXHAUSTED, "passes")
        if (
            counters.consecutive_provider_failures
            >= limits.max_consecutive_provider_failures
        ):
            return (StopReason.PROVIDER_FAILURE, "consecutive_provider_failures")
        if (
            counters.consecutive_no_progress
            >= limits.max_consecutive_no_progress
        ):
            return (StopReason.NO_PROGRESS, "consecutive_loop_iterations")
        return None

    def check_coder_dispatch(
        self, counters: BudgetCounters, slice_id: str
    ) -> tuple[StopReason, str] | None:
        if (
            counters.coder_dispatches_for(slice_id)
            >= self._policy.limits.max_coder_attempts_per_slice
        ):
            return (StopReason.BUDGET_EXHAUSTED, "coder_attempts")
        return None

    def check_repair_dispatch(
        self, counters: BudgetCounters, slice_id: str
    ) -> tuple[StopReason, str] | None:
        if counters.repairs_for(slice_id) >= self._policy.limits.max_report_repairs:
            return (StopReason.REPAIR_EXHAUSTED, "report_repairs")
        return None

    def remaining_cost(self, counters: BudgetCounters) -> float:
        if counters.cost_fact_invalid:
            return 0.0
        return max(
            0.0, self._policy.limits.max_total_cost_usd - counters.total_cost_usd
        )

    def check_reconciliation(
        self, counters: BudgetCounters, slice_id: str
    ) -> tuple[StopReason, str] | None:
        if (
            counters.reconciliations_for(slice_id)
            >= self._policy.limits.max_reconciliations_without_progress
        ):
            return (StopReason.NO_PROGRESS, "reconciliation_attempts")
        if (
            counters.reconciliations_without_progress
            >= self._policy.limits.max_reconciliations_without_progress
        ):
            return (StopReason.NO_PROGRESS, "reconciliations_without_progress")
        return None
