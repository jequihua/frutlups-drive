"""Escalation artifact writer (architecture contract §8.3).

Every mandatory stop produces exactly one complete escalation artifact under
``runs/<run_id>/escalations/``: a fenced TOML machine header plus the human
fields. Re-running the same stop converges on the existing artifact via the
escalation key; artifacts are write-once and never clobbered.
"""

from __future__ import annotations

import re
from pathlib import Path

from frutlups_drive.contracts import StopReason
from frutlups_drive.runstore import RunStore

_KEY_LINE = re.compile(r'^escalation_key = "(?P<key>[^"]*)"$', re.MULTILINE)


def write_escalation(
    store: RunStore,
    run_id: str,
    *,
    reason: StopReason,
    slice_id: str,
    attempt_id: str,
    planning_snapshot: str,
    attempts_summary: str,
    decision_required: str,
    safe_options: str,
    actions_not_taken: str,
    resume_command: str,
    fresh_launch_command: str = "",
    controlling_note_sha256: str = "",
    predecessor_run_id: str = "",
) -> Path:
    key = f"{reason.value}:{slice_id or '-'}:{attempt_id or '-'}"
    for existing in store.list_escalations(run_id):
        match = _KEY_LINE.search(existing.read_bytes().decode("utf-8"))
        if match and match.group("key") == key:
            return existing

    number = len(store.list_escalations(run_id)) + 1
    filename = f"{number:03d}_{reason.value}.md"
    fresh_header = ""
    final_command = f"## Resume Command\n\n`{resume_command}`\n"
    if fresh_launch_command:
        fresh_header = (
            "fresh_run_required = true\n"
            f'fresh_launch_command = "{fresh_launch_command}"\n'
            f'controlling_note_sha256 = "{controlling_note_sha256}"\n'
            f'predecessor_run_id = "{predecessor_run_id}"\n'
        )
        final_command = (
            "## Fresh Launch Command\n\n"
            f"`{fresh_launch_command}`\n\n"
            "Ordinary resume is refused for this lifecycle.\n"
        )
    body = (
        f"# Escalation: {reason.value}\n"
        "\n"
        "```toml\n"
        f'run_id = "{run_id}"\n'
        f'slice_id = "{slice_id}"\n'
        f'attempt_id = "{attempt_id}"\n'
        f'stop_reason = "{reason.value}"\n'
        f'escalation_key = "{key}"\n'
        f"{fresh_header}"
        "```\n"
        "\n"
        "## Planning-State Snapshot\n"
        "\n"
        f"{planning_snapshot.rstrip()}\n"
        "\n"
        "## Attempts Summary\n"
        "\n"
        f"{attempts_summary.rstrip()}\n"
        "\n"
        "## Decision Required\n"
        "\n"
        f"{decision_required.rstrip()}\n"
        "\n"
        "## Safe Options\n"
        "\n"
        f"{safe_options.rstrip()}\n"
        "\n"
        "## Actions Deliberately Not Taken\n"
        "\n"
        f"{actions_not_taken.rstrip()}\n"
        "\n"
        f"{final_command}"
    )
    return store.create_escalation(run_id, filename, body.encode("utf-8"))
