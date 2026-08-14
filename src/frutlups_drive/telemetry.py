"""Deterministic read-only telemetry derived from one durable run store."""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

from frutlups_drive.runstore import RunStore, RunStoreRefusal, TRANSITION_STATES

REPORT_SCHEMA = "frutlups_drive_report_v1"
_MAX_RECORD_BYTES = 16 * 1024 * 1024
_MAX_EVENTS_BYTES = 64 * 1024 * 1024
_MAX_EVENTS = 200_000


class TelemetryRefusal(Exception):
    """A bounded refusal on the read-only reporting surface."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def derive_report(store: RunStore, run_id: str) -> dict[str, object]:
    """Derive every reported value from durable members at call time.

    Malformed individual records become stable error entries and unknown
    values.  The function never repairs, caches, or writes telemetry.
    """
    try:
        run_dir = store.run_dir(run_id)
    except RunStoreRefusal as refusal:
        raise TelemetryRefusal(refusal.code, refusal.message) from None
    if not run_dir.is_dir() or run_dir.is_symlink():
        raise TelemetryRefusal("run_missing", "run directory does not exist")

    errors: list[dict[str, str]] = []
    events = _read_events(run_dir / "events.jsonl", errors)
    event_times = [
        value
        for event in events
        if (value := _time_value(event.get("t"))) is not None
    ]
    wall_duration = (
        max(event_times) - min(event_times) if event_times else None
    )

    dispatches = _dispatch_records(events)
    verdicts = [
        {
            "artifact": _optional_string(event.get("artifact")),
            "slice_id": _optional_string(event.get("slice")),
            "t": _time_value(event.get("t")),
        }
        for event in events
        if event.get("kind") == "verb" and event.get("verb") == "record-verdict"
    ]
    verifications = [
        {
            "attempt_id": _optional_string(event.get("attempt")),
            "passed": (
                event.get("passed") if type(event.get("passed")) is bool else None
            ),
            "slice_id": _optional_string(event.get("slice")),
            "t": _time_value(event.get("t")),
        }
        for event in events
        if event.get("kind") == "verification"
    ]
    stops = [
        {
            "attempt_id": _optional_string(event.get("attempt")),
            "detail": _optional_string(event.get("detail")),
            "escalation": _optional_string(event.get("escalation")),
            "reason": _optional_string(event.get("reason")),
            "slice_id": _optional_string(event.get("slice")),
            "t": _time_value(event.get("t")),
        }
        for event in events
        if event.get("kind") == "stop"
    ]

    primary_slices = _listed(lambda: store.list_slices(run_id), errors, "slices")
    shadow_slices = _listed(
        lambda: store.list_shadow_slices(run_id), errors, "shadow"
    )
    slice_ids = sorted(set(primary_slices) | set(shadow_slices))
    slices: list[dict[str, object]] = []
    all_attempts: list[dict[str, object]] = []
    for slice_id in slice_ids:
        primary = _listed(
            lambda value=slice_id: store.list_attempts(run_id, value),
            errors,
            f"slices/{slice_id}",
        )
        shadow = _listed(
            lambda value=slice_id: store.list_shadow_attempts(run_id, value),
            errors,
            f"shadow/{slice_id}",
        )
        attempts = [
            _attempt_record(
                run_dir,
                Path(attempt),
                slice_id,
                "primary",
                events,
                errors,
            )
            for attempt in primary
        ] + [
            _attempt_record(
                run_dir,
                Path(attempt),
                slice_id,
                "shadow",
                events,
                errors,
            )
            for attempt in shadow
        ]
        attempts.sort(key=lambda item: (str(item["area"]), str(item["attempt_id"])))
        all_attempts.extend(attempts)
        slice_dispatches = [d for d in dispatches if d["slice_id"] == slice_id]
        slice_verdicts = [v for v in verdicts if v["slice_id"] == slice_id]
        slice_verifications = [v for v in verifications if v["slice_id"] == slice_id]
        slice_stops = [s for s in stops if s["slice_id"] == slice_id]
        slices.append(
            {
                "attempts": attempts,
                "slice_id": slice_id,
                "summary": _summary(
                    attempts,
                    slice_dispatches,
                    slice_verdicts,
                    slice_verifications,
                    slice_stops,
                    [event for event in events if event.get("slice") == slice_id],
                ),
            }
        )

    return {
        "dispatches": dispatches,
        "errors": errors,
        "run_id": run_id,
        "schema": REPORT_SCHEMA,
        "slices": slices,
        "stops": stops,
        "summary": _summary(
            all_attempts, dispatches, verdicts, verifications, stops, events
        ),
        "verdicts": verdicts,
        "verifications": verifications,
        "wall_duration_seconds": wall_duration,
    }


def render_json(report: Mapping[str, object]) -> str:
    return json.dumps(
        report,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"


def render_text(report: Mapping[str, object]) -> str:
    """Render the same data as stable path/value plain text."""
    lines: list[str] = []
    _flatten("", report, lines)
    return "\n".join(lines) + "\n"


def _flatten(prefix: str, value: object, lines: list[str]) -> None:
    if isinstance(value, Mapping):
        if not value:
            lines.append(f"{prefix}={{}}")
            return
        for key in sorted(value):
            child = f"{prefix}.{key}" if prefix else str(key)
            _flatten(child, value[key], lines)
        return
    if isinstance(value, list):
        if not value:
            lines.append(f"{prefix}=[]")
            return
        for index, member in enumerate(value):
            _flatten(f"{prefix}[{index}]", member, lines)
        return
    lines.append(
        f"{prefix}="
        + json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    )


def _read_events(path: Path, errors: list[dict[str, str]]) -> list[dict]:
    if not path.exists():
        return []
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError
        if path.stat().st_size > _MAX_EVENTS_BYTES:
            errors.append({"code": "member_over_bound", "member": "events.jsonl"})
            return []
        raw_lines = path.read_bytes().splitlines()
    except OSError:
        errors.append({"code": "member_unreadable", "member": "events.jsonl"})
        return []
    events: list[dict] = []
    for index, raw in enumerate(raw_lines[:_MAX_EVENTS], 1):
        try:
            value = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
            if not isinstance(value, dict):
                raise ValueError
        except (UnicodeDecodeError, ValueError):
            errors.append(
                {"code": "event_invalid", "member": f"events.jsonl:{index}"}
            )
            continue
        events.append(value)
    if len(raw_lines) > _MAX_EVENTS:
        errors.append({"code": "events_over_bound", "member": "events.jsonl"})
    return events


def _listed(call, errors: list[dict[str, str]], member: str) -> tuple:
    try:
        return tuple(call())
    except (OSError, RunStoreRefusal, ValueError):
        errors.append({"code": "member_unreadable", "member": member})
        return ()


def _attempt_record(
    run_dir: Path,
    attempt: Path,
    slice_id: str,
    area: str,
    events: list[dict],
    errors: list[dict[str, str]],
) -> dict[str, object]:
    relative = attempt.relative_to(run_dir).as_posix()
    request = _read_record(attempt / "request.json", relative + "/request.json", errors)
    result = _read_record(attempt / "result.json", relative + "/result.json", errors)
    transition = _read_transition(attempt / "transition", relative, errors)
    request = request or {}
    result = result or {}
    role = (
        "shadow_reviewer"
        if area == "shadow"
        else _optional_string(request.get("role"))
    )

    if area == "shadow":
        facts = [
            event
            for event in events
            if event.get("kind") == "shadow_review"
            and event.get("slice") == slice_id
            and event.get("attempt") == attempt.name
        ]
        dispatch_t = _time_value(facts[-1].get("dispatched_at")) if facts else None
        completed_t = _time_value(facts[-1].get("completed_at")) if facts else None
        event_outcome = _optional_string(facts[-1].get("status")) if facts else None
    else:
        dispatched = [
            event
            for event in events
            if event.get("kind") == "dispatch"
            and event.get("slice") == slice_id
            and event.get("attempt") == attempt.name
        ]
        collected = [
            event
            for event in events
            if event.get("kind") == "collected"
            and event.get("slice") == slice_id
            and event.get("attempt") == attempt.name
        ]
        dispatch_t = _time_value(dispatched[0].get("t")) if dispatched else None
        completed_t = _time_value(collected[-1].get("t")) if collected else None
        event_outcome = (
            _optional_string(collected[-1].get("status")) if collected else None
        )

    result_outcome = _optional_string(result.get("status"))
    outcome = (
        (event_outcome or result_outcome)
        if area == "shadow"
        else (result_outcome or event_outcome)
    ) or "unknown"
    tokens_in = _usage_value(result.get("tokens_in"))
    tokens_out = _usage_value(result.get("tokens_out"))
    cost = _cost_value(result.get("cost_usd"))
    if "tokens_in" in result and result.get("tokens_in") is not None and tokens_in is None:
        errors.append({"code": "usage_invalid", "member": relative + "/result.json"})
    if "tokens_out" in result and result.get("tokens_out") is not None and tokens_out is None:
        errors.append({"code": "usage_invalid", "member": relative + "/result.json"})
    if "cost_usd" in result and result.get("cost_usd") is not None and cost is None:
        errors.append({"code": "cost_invalid", "member": relative + "/result.json"})
    duration = (
        completed_t - dispatch_t
        if dispatch_t is not None
        and completed_t is not None
        and completed_t >= dispatch_t
        else None
    )
    return {
        "area": area,
        "attempt_id": attempt.name,
        "cost_usd": cost,
        "dispatched_at": dispatch_t,
        "duration_seconds": duration,
        "outcome": outcome,
        "role": role,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "transition": transition,
    }


def _read_record(
    path: Path, member: str, errors: list[dict[str, str]]
) -> dict | None:
    if not path.exists():
        return None
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_RECORD_BYTES:
            raise OSError
        value = json.loads(path.read_bytes().decode("utf-8"), parse_constant=_reject_constant)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, UnicodeDecodeError, ValueError):
        errors.append({"code": "record_invalid", "member": member})
        return None


def _read_transition(
    path: Path, member: str, errors: list[dict[str, str]]
) -> str | None:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        value = text[:-1] if text.endswith("\n") else text
        if path.is_symlink() or text != value + "\n" or value not in TRANSITION_STATES:
            raise ValueError
        return value
    except (OSError, UnicodeDecodeError, ValueError):
        errors.append({"code": "transition_invalid", "member": member + "/transition"})
        return None


def _dispatch_records(events: list[dict]) -> list[dict[str, object]]:
    records = [
        {
            "attempt_id": _optional_string(event.get("attempt")),
            "repair": event.get("repair") if type(event.get("repair")) is bool else None,
            "role": _optional_string(event.get("role")),
            "slice_id": _optional_string(event.get("slice")),
            "t": _time_value(event.get("t")),
        }
        for event in events
        if event.get("kind") == "dispatch"
    ]
    records.extend(
        {
            "attempt_id": _optional_string(event.get("attempt")),
            "repair": False,
            "role": "shadow_reviewer",
            "slice_id": _optional_string(event.get("slice")),
            "t": _time_value(event.get("dispatched_at")),
        }
        for event in events
        if event.get("kind") == "shadow_review" and event.get("dispatched") is True
    )
    return records


def _summary(
    attempts: list[dict[str, object]],
    dispatches: list[dict[str, object]],
    verdicts: list[dict[str, object]],
    verifications: list[dict[str, object]],
    stops: list[dict[str, object]],
    events: list[dict],
) -> dict[str, object]:
    return {
        "attempts": len(attempts),
        "attempts_by_outcome": _counts(attempts, "outcome"),
        "attempts_by_role": _counts(attempts, "role"),
        "attempts_with_unknown_cost": sum(a["cost_usd"] is None for a in attempts),
        "attempts_with_unknown_tokens_in": sum(a["tokens_in"] is None for a in attempts),
        "attempts_with_unknown_tokens_out": sum(a["tokens_out"] is None for a in attempts),
        "cost_usd_known_sum": math.fsum(
            float(a["cost_usd"]) for a in attempts if a["cost_usd"] is not None
        ),
        "dispatches": len(dispatches),
        "dispatches_by_role": _counts(dispatches, "role"),
        "escalations": sum(stop["escalation"] is not None for stop in stops),
        "journal_events_by_kind": dict(
            sorted(Counter(str(event.get("kind", "unknown")) for event in events).items())
        ),
        "stops": len(stops),
        "tokens_in_known_sum": sum(
            int(a["tokens_in"]) for a in attempts if a["tokens_in"] is not None
        ),
        "tokens_out_known_sum": sum(
            int(a["tokens_out"]) for a in attempts if a["tokens_out"] is not None
        ),
        "verdicts_recorded": len(verdicts),
        "verifications": {
            "failed": sum(item["passed"] is False for item in verifications),
            "passed": sum(item["passed"] is True for item in verifications),
            "unknown": sum(item["passed"] is None for item in verifications),
        },
    }


def _counts(records: list[dict[str, object]], field: str) -> dict[str, int]:
    values = Counter(
        str(record[field]) if record.get(field) is not None else "unknown"
        for record in records
    )
    return dict(sorted(values.items()))


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _time_value(value: object) -> int | float | None:
    if type(value) not in (int, float):
        return None
    numeric = float(value)
    return value if math.isfinite(numeric) else None


def _usage_value(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _cost_value(value: object) -> int | float | None:
    if type(value) not in (int, float):
        return None
    numeric = float(value)
    return value if math.isfinite(numeric) and numeric >= 0.0 else None


def _reject_constant(_token: str) -> None:
    raise ValueError("non-standard JSON constant")
