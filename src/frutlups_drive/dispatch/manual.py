"""Degrade-to-human executor.

Prints bounded instructions to an injected stream and waits for the expected
artifacts through the injected watcher. It never calls a model, parses
produced content, or bypasses the timeout; tests inject all effects and never
wait on a human.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from frutlups_drive.contracts import AgentRunRequest, AgentRunResult
from frutlups_drive.watcher import Watcher


class ManualAgentExecutor:
    def __init__(
        self,
        watcher: Watcher,
        instructions: TextIO,
        log_dir: Path,
        *,
        stop_requested: Callable[[], bool] | None = None,
        poll_seconds: float = 0.05,
    ) -> None:
        self._watcher = watcher
        self._instructions = instructions
        self._log_dir = Path(log_dir)
        self._stop_requested = stop_requested
        self._poll_seconds = poll_seconds

    def execute(self, request: AgentRunRequest) -> AgentRunResult:
        expected = [
            Path(request.workspace) / artifact
            for artifact in request.expected_artifacts
        ]
        self._instructions.write(
            f"manual {request.role.value} dispatch: execute prompt "
            f"'{request.prompt_path.as_posix()}' in the assigned workspace and "
            "produce: "
            + ", ".join(a.as_posix() for a in request.expected_artifacts)
            + "\n"
        )
        self._instructions.flush()
        outcome = self._watcher.wait_for(
            expected,
            request.max_seconds,
            poll_seconds=self._poll_seconds,
            stop_requested=self._stop_requested,
        )

        self._log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._log_dir / f"{request.run_id}_{request.attempt_id}.jsonl"
        log_path.write_bytes(
            (
                json.dumps(
                    {
                        "event": "manual_wait",
                        "ok": outcome.ok,
                        "stop_requested": outcome.stop_requested,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        )
        return AgentRunResult(
            status="completed" if outcome.ok else "timeout",
            event_log_path=log_path,
            changed_files=(),
            produced_artifacts=tuple(request.expected_artifacts)
            if outcome.ok
            else (),
            exit_reason=(
                "manual_artifacts_observed"
                if outcome.ok
                else (
                    "manual_stop_requested"
                    if outcome.stop_requested
                    else "manual_timeout"
                )
            ),
            tokens_in=None,
            tokens_out=None,
            cost_usd=None,
        )
