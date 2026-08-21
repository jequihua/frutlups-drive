"""Artifact watcher: existence, size stability, and timeout only.

No parsing, no validity decision — validity is the planning state's job on the
next tick (architecture contract §4). The clock and sleep effect are injected
so tests contain no real sleeps.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from frutlups_drive.budget import Clock


@dataclass(frozen=True)
class WatchResult:
    ok: bool
    waited_seconds: float
    stop_requested: bool = False
    protected_change: bool = False


class Watcher:
    def __init__(self, clock: Clock, sleep: Callable[[float], None]) -> None:
        self._clock = clock
        self._sleep = sleep

    def wait_for(
        self,
        paths: Sequence[Path],
        timeout_seconds: float,
        poll_seconds: float = 0.05,
        stability_checks: int = 2,
        stop_requested: Callable[[], bool] | None = None,
        protected_changed: Callable[[], bool] | None = None,
    ) -> WatchResult:
        started = self._clock.now()
        deadline = started + timeout_seconds
        previous_sizes: tuple[int, ...] | None = None
        stable_count = 0
        while True:
            if stop_requested is not None and stop_requested():
                return WatchResult(
                    False, self._clock.now() - started, stop_requested=True
                )
            if protected_changed is not None:
                try:
                    changed = protected_changed()
                except Exception:
                    # An unreadable or unhashable protected member cannot be
                    # distinguished safely from mutation, so observation is
                    # fail-closed.
                    changed = True
                if changed:
                    return WatchResult(
                        False,
                        self._clock.now() - started,
                        protected_change=True,
                    )
            sizes = _observe(paths)
            if sizes is not None:
                if sizes == previous_sizes:
                    stable_count += 1
                else:
                    stable_count = 1
                previous_sizes = sizes
                if stable_count >= stability_checks:
                    return WatchResult(True, self._clock.now() - started)
            else:
                previous_sizes = None
                stable_count = 0
            if self._clock.now() >= deadline:
                return WatchResult(False, self._clock.now() - started)
            self._sleep(poll_seconds)


def _observe(paths: Sequence[Path]) -> tuple[int, ...] | None:
    sizes = []
    for path in paths:
        try:
            sizes.append(Path(path).stat().st_size)
        except OSError:
            return None
    return tuple(sizes)
