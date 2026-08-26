"""Deterministic read-only telemetry derived from one durable run store."""

from __future__ import annotations

import json
import math
import re
import tomllib
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

from frutlups_drive.runstore import RunStore, RunStoreRefusal, TRANSITION_STATES

REPORT_SCHEMA = "frutlups_drive_report_v1"
CAMPAIGN_REPORT_SCHEMA = "frutlups_drive_campaign_report_v1"
_MAX_RECORD_BYTES = 16 * 1024 * 1024
_MAX_EVENTS_BYTES = 64 * 1024 * 1024
_MAX_EVENTS = 200_000
_CAMPAIGN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


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
    manifest = _read_manifest(run_dir / "manifest.toml", errors)
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

    reporting = _reporting_facts(manifest, all_attempts)
    return {
        "cost_reporting": reporting,
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


def derive_campaign_report(
    store: RunStore,
    *,
    campaign_id: str | None = None,
    all_runs: bool = False,
) -> dict[str, object]:
    """Derive a cross-run campaign story without mutating the run store.

    Exactly one selector is required. Historical manifests without campaign or
    predecessor fields remain valid and are included only by ``all_runs``.
    """

    if (campaign_id is None) == (not all_runs):
        raise TelemetryRefusal(
            "campaign_selection_invalid",
            "select exactly one campaign id or all runs",
        )
    if campaign_id is not None and not _CAMPAIGN_ID.fullmatch(campaign_id):
        raise TelemetryRefusal(
            "campaign_id_invalid",
            "campaign id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}",
        )
    try:
        available = store.list_runs()
    except (OSError, RunStoreRefusal):
        raise TelemetryRefusal(
            "run_store_unreadable", "the run inventory could not be read"
        ) from None

    selected: list[tuple[str, dict, list[dict]]] = []
    selection_errors: list[dict[str, str]] = []
    for run_id in available:
        local_errors: list[dict[str, str]] = []
        manifest = _read_manifest(
            store.run_dir(run_id) / "manifest.toml", local_errors
        )
        if local_errors:
            selection_errors.extend(
                {**error, "run_id": run_id} for error in local_errors
            )
        if campaign_id is not None and manifest.get("campaign_id") != campaign_id:
            continue
        events = _read_events(
            store.run_dir(run_id) / "events.jsonl", local_errors := []
        )
        if local_errors:
            selection_errors.extend(
                {**error, "run_id": run_id} for error in local_errors
            )
        selected.append((run_id, manifest, events))
    if campaign_id is not None and not selected:
        raise TelemetryRefusal(
            "campaign_missing", "no run in this project declares that campaign id"
        )

    errors = list(selection_errors)
    run_records: list[dict[str, object]] = []
    all_attempts: list[dict[str, object]] = []
    predecessor_by_run: dict[str, str | None] = {}
    stop_by_run: dict[str, dict[str, object] | None] = {}
    progress_by_run: dict[str, dict[str, object] | None] = {}
    for run_id, manifest, events in selected:
        report = derive_report(store, run_id)
        errors.extend(
            {**error, "run_id": run_id}
            for error in report.get("errors", [])
            if isinstance(error, dict)
        )
        attempts = _campaign_attempts(store, run_id, report)
        all_attempts.extend(attempts)
        dispatches = [attempt for attempt in attempts if attempt["dispatched"]]
        durations = _duration_summary(attempts)
        boundary_event = _last_event(events, "boundary")
        stop_event = _last_event(events, "stop")
        terminal = _last_terminal_event(events)
        progress = _first_progress(events)
        predecessor = _optional_string(manifest.get("predecessor_run_id"))
        predecessor_by_run[run_id] = predecessor
        stop_by_run[run_id] = stop_event
        progress_by_run[run_id] = progress
        run_records.append(
            {
                "attempt_durations": durations,
                "boundary_outcome": (
                    _optional_string(boundary_event.get("boundary"))
                    if boundary_event is not None
                    else None
                ),
                "campaign_id": _optional_string(manifest.get("campaign_id")),
                "cost_knowledge": _cost_summary(attempts),
                "cost_reporting": report.get("cost_reporting", {}),
                "dispatch_counts": _dispatch_count_summary(dispatches),
                "first_progress_event": progress,
                "predecessor_run_id": predecessor,
                "run_id": run_id,
                "started_at": _manifest_time(manifest.get("started_at")),
                "stop_outcome": (
                    _optional_string(stop_event.get("reason"))
                    if stop_event is not None
                    else None
                ),
                "terminal_outcome": terminal,
            }
        )

    run_ids = [record["run_id"] for record in run_records]
    lineage, chains = _lineage_records(
        tuple(str(run_id) for run_id in run_ids),
        predecessor_by_run,
        available,
        errors,
    )
    recovery = _recovery_intervals(
        tuple(str(run_id) for run_id in run_ids),
        predecessor_by_run,
        stop_by_run,
        progress_by_run,
    )
    stop_reasons = Counter(
        str(event.get("reason"))
        if isinstance(event.get("reason"), str)
        else "unknown"
        for _, _, events in selected
        for event in events
        if event.get("kind") == "stop"
    )
    return {
        "attempt_durations": _duration_summary(all_attempts),
        "campaign_id": campaign_id,
        "cost_knowledge": _cost_summary(all_attempts),
        "dispatch_counts": _dispatch_count_summary(
            [attempt for attempt in all_attempts if attempt["dispatched"]]
        ),
        "errors": errors,
        "lineage": lineage,
        "lineage_chains": chains,
        "recovery_intervals": recovery,
        "run_count": len(run_records),
        "runs": run_records,
        "schema": CAMPAIGN_REPORT_SCHEMA,
        "selection": "all_runs" if all_runs else "campaign",
        "stop_reasons": dict(sorted(stop_reasons.items())),
    }


def _campaign_attempts(
    store: RunStore, run_id: str, report: Mapping[str, object]
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    run_dir = store.run_dir(run_id)
    slices = report.get("slices")
    if not isinstance(slices, list):
        return records
    for slice_record in slices:
        if not isinstance(slice_record, dict):
            continue
        slice_id = _optional_string(slice_record.get("slice_id"))
        attempts = slice_record.get("attempts")
        if slice_id is None or not isinstance(attempts, list):
            continue
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            area = _optional_string(attempt.get("area"))
            attempt_id = _optional_string(attempt.get("attempt_id"))
            if area not in ("primary", "shadow") or attempt_id is None:
                continue
            area_dir = "slices" if area == "primary" else "shadow"
            request = _strict_record_or_empty(
                run_dir / area_dir / slice_id / attempt_id / "request.json"
            )
            role = _optional_string(attempt.get("role")) or "unknown"
            records.append(
                {
                    "adapter": _group_label(attempt.get("adapter")),
                    "cost_knowledge": _group_label(attempt.get("cost_knowledge")),
                    "cost_usd": attempt.get("cost_usd"),
                    "dispatched": attempt.get("dispatched_at") is not None,
                    "duration_seconds": attempt.get("duration_seconds"),
                    "effort": _group_label(request.get("effort")),
                    "model": _group_label(request.get("model")),
                    "role": role,
                    "tokens_in": attempt.get("tokens_in"),
                    "tokens_out": attempt.get("tokens_out"),
                }
            )
    return records


def _strict_record_or_empty(path: Path) -> dict:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_RECORD_BYTES:
            return {}
        value = json.loads(
            path.read_bytes().decode("utf-8"), parse_constant=_reject_constant
        )
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeDecodeError, ValueError):
        return {}


def _group_label(value: object) -> str:
    return value if isinstance(value, str) and value else "unknown"


def _duration_summary(attempts: list[dict[str, object]]) -> dict[str, object]:
    known = [
        value
        for attempt in attempts
        if (value := _duration_value(attempt.get("duration_seconds"))) is not None
    ]
    return {
        "known_count": len(known),
        "known_sum_seconds": math.fsum(float(value) for value in known),
        "unknown_count": len(attempts) - len(known),
    }


def _cost_summary(attempts: list[dict[str, object]]) -> dict[str, object]:
    known_costs = [
        value
        for attempt in attempts
        if (value := _cost_value(attempt.get("cost_usd"))) is not None
    ]
    return {
        "attempts_by_class": _counts(attempts, "cost_knowledge"),
        "attempts_without_numeric_cost": len(attempts) - len(known_costs),
        "known_cost_sum": math.fsum(float(value) for value in known_costs),
        "tokens_in_known_sum": sum(
            int(value)
            for attempt in attempts
            if (value := _usage_value(attempt.get("tokens_in"))) is not None
        ),
        "tokens_out_known_sum": sum(
            int(value)
            for attempt in attempts
            if (value := _usage_value(attempt.get("tokens_out"))) is not None
        ),
    }


def _dispatch_count_summary(
    attempts: list[dict[str, object]],
) -> dict[str, object]:
    triples = Counter(
        (
            str(attempt.get("role", "unknown")),
            str(attempt.get("model", "unknown")),
            str(attempt.get("effort", "unknown")),
        )
        for attempt in attempts
    )
    return {
        "by_effort": _counts(attempts, "effort"),
        "by_model": _counts(attempts, "model"),
        "by_role": _counts(attempts, "role"),
        "by_role_model_effort": [
            {
                "count": count,
                "effort": effort,
                "model": model,
                "role": role,
            }
            for (role, model, effort), count in sorted(triples.items())
        ],
        "total": len(attempts),
    }


def _last_event(events: list[dict], kind: str) -> dict | None:
    return next(
        (event for event in reversed(events) if event.get("kind") == kind), None
    )


def _last_terminal_event(events: list[dict]) -> dict[str, object] | None:
    event = next(
        (
            item
            for item in reversed(events)
            if item.get("kind") in ("boundary", "stop")
        ),
        None,
    )
    if event is None:
        return None
    kind = str(event.get("kind"))
    value = event.get("boundary") if kind == "boundary" else event.get("reason")
    return {
        "kind": kind,
        "t": _time_value(event.get("t")),
        "value": _optional_string(value),
    }


def _first_progress(events: list[dict]) -> dict[str, object] | None:
    for event in events:
        kind = event.get("kind")
        progress = (
            (kind == "collected" and event.get("status") == "completed")
            or (kind == "verification" and event.get("passed") is True)
            or kind in ("verb", "adoption", "pass_boundary", "boundary")
            or (kind == "reconciliation" and event.get("progress") is True)
            or (kind == "tick" and event.get("consumed") is True)
        )
        if progress:
            return {"kind": str(kind), "t": _time_value(event.get("t"))}
    return None


def _manifest_time(value: object) -> int | float | None:
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    return _time_value(value)


def _lineage_records(
    run_ids: tuple[str, ...],
    predecessor_by_run: Mapping[str, str | None],
    available: tuple[str, ...],
    errors: list[dict[str, str]],
) -> tuple[list[dict[str, object]], list[list[str]]]:
    selected = set(run_ids)
    available_set = set(available)
    successors: dict[str, list[str]] = {run_id: [] for run_id in run_ids}
    for run_id in run_ids:
        predecessor = predecessor_by_run.get(run_id)
        if predecessor is None:
            continue
        if predecessor not in available_set:
            errors.append(
                {
                    "code": "lineage_predecessor_missing",
                    "member": "manifest.toml",
                    "run_id": run_id,
                }
            )
        if predecessor in selected:
            successors[predecessor].append(run_id)
    for run_id, children in successors.items():
        if len(children) > 1:
            errors.append(
                {
                    "code": "lineage_multiple_successors",
                    "member": "manifest.toml",
                    "run_id": run_id,
                }
            )

    chains: list[list[str]] = []
    visited: set[str] = set()
    roots = [
        run_id
        for run_id in run_ids
        if predecessor_by_run.get(run_id) not in selected
    ]
    for root in roots:
        pending = [(root, [])]
        while pending:
            current, prefix = pending.pop()
            if current in prefix:
                errors.append(
                    {
                        "code": "lineage_cycle",
                        "member": "manifest.toml",
                        "run_id": current,
                    }
                )
                continue
            path = [*prefix, current]
            visited.add(current)
            children = sorted(successors[current])
            if not children:
                chains.append(path)
            else:
                pending.extend((child, path) for child in reversed(children))
    for run_id in run_ids:
        if run_id not in visited:
            errors.append(
                {
                    "code": "lineage_cycle",
                    "member": "manifest.toml",
                    "run_id": run_id,
                }
            )
            chains.append([run_id])
    lineage = [
        {
            "predecessor_run_id": predecessor_by_run.get(run_id),
            "run_id": run_id,
            "successor_run_ids": sorted(successors[run_id]),
        }
        for run_id in run_ids
    ]
    return lineage, chains


def _recovery_intervals(
    run_ids: tuple[str, ...],
    predecessor_by_run: Mapping[str, str | None],
    stop_by_run: Mapping[str, dict[str, object] | None],
    progress_by_run: Mapping[str, dict[str, object] | None],
) -> list[dict[str, object]]:
    selected = set(run_ids)
    intervals: list[dict[str, object]] = []
    for successor in run_ids:
        predecessor = predecessor_by_run.get(successor)
        if predecessor not in selected:
            continue
        stop = stop_by_run.get(str(predecessor))
        progress = progress_by_run.get(successor)
        stop_t = _time_value(stop.get("t")) if stop is not None else None
        progress_t = (
            _time_value(progress.get("t")) if progress is not None else None
        )
        known = (
            stop_t is not None
            and progress_t is not None
            and progress_t >= stop_t
        )
        intervals.append(
            {
                "duration_seconds": progress_t - stop_t if known else None,
                "first_progress_kind": (
                    _optional_string(progress.get("kind"))
                    if progress is not None
                    else None
                ),
                "predecessor_run_id": predecessor,
                "status": "known" if known else "unknown",
                "stop_reason": (
                    _optional_string(stop.get("reason"))
                    if stop is not None
                    else None
                ),
                "successor_run_id": successor,
            }
        )
    return intervals


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
    cost_knowledge = _cost_knowledge_value(
        result.get("cost_knowledge"), cost
    )
    if "tokens_in" in result and result.get("tokens_in") is not None and tokens_in is None:
        errors.append({"code": "usage_invalid", "member": relative + "/result.json"})
    if "tokens_out" in result and result.get("tokens_out") is not None and tokens_out is None:
        errors.append({"code": "usage_invalid", "member": relative + "/result.json"})
    if "cost_usd" in result and result.get("cost_usd") is not None and cost is None:
        errors.append({"code": "cost_invalid", "member": relative + "/result.json"})
    if "cost_knowledge" in result and cost_knowledge is None:
        errors.append(
            {"code": "cost_knowledge_invalid", "member": relative + "/result.json"}
        )
        cost_knowledge = "unknown"
    provider_duration = _duration_value(result.get("provider_duration_seconds"))
    observed_duration = _duration_value(result.get("observed_duration_seconds"))
    retry_class = _optional_string(result.get("retry_class")) or "not_applicable"
    truncated = (
        result.get("capture_truncated")
        if type(result.get("capture_truncated")) is bool
        else False
    )
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
        "adapter": _optional_string(request.get("adapter")),
        "cost_usd": cost,
        "cost_knowledge": cost_knowledge,
        "dispatched_at": dispatch_t,
        "duration_seconds": duration,
        "observed_duration_seconds": observed_duration,
        "outcome": outcome,
        "provider_duration_seconds": provider_duration,
        "retry_class": retry_class,
        "role": role,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "truncated": truncated,
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
        "attempts_by_cost_knowledge": _counts(attempts, "cost_knowledge"),
        "attempts_with_truncated_capture": sum(
            a["truncated"] is True for a in attempts
        ),
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


def _duration_value(value: object) -> int | float | None:
    if type(value) not in (int, float):
        return None
    numeric = float(value)
    return value if math.isfinite(numeric) and numeric >= 0.0 else None


def _cost_knowledge_value(value: object, cost: object) -> str | None:
    if value is None:
        return "measured" if cost is not None else "unknown"
    if value not in ("measured", "subscription_prepaid", "unknown"):
        return None
    if (value == "measured") != (cost is not None):
        return None
    return str(value)


def _read_manifest(path: Path, errors: list[dict[str, str]]) -> dict:
    try:
        if path.is_symlink() or path.stat().st_size > _MAX_RECORD_BYTES:
            raise OSError
        value = tomllib.loads(path.read_bytes().decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, UnicodeDecodeError, ValueError, tomllib.TOMLDecodeError):
        errors.append({"code": "manifest_invalid", "member": "manifest.toml"})
        return {}


_PROVIDER_CEILING_KEY = re.compile(
    r"external_provider_ceiling_(\d{3})_(provider|amount)"
)


def _reporting_facts(
    manifest: Mapping[str, object], attempts: list[dict[str, object]]
) -> dict[str, object]:
    currency = _optional_string(manifest.get("reporting_currency"))
    slots: dict[str, dict[str, object]] = {}
    for key, value in manifest.items():
        match = _PROVIDER_CEILING_KEY.fullmatch(str(key))
        if match:
            slots.setdefault(match.group(1), {})[match.group(2)] = value
    ceilings = []
    for index in sorted(slots):
        provider = _optional_string(slots[index].get("provider"))
        amount = _cost_value(slots[index].get("amount"))
        if provider is not None and amount is not None:
            ceilings.append({"ceiling": amount, "provider": provider})
    declared_providers = {str(item["provider"]) for item in ceilings}
    relevant = [
        attempt
        for attempt in attempts
        if attempt.get("adapter") in declared_providers
    ]
    auditable = bool(ceilings) and currency == "USD" and bool(relevant) and all(
        attempt.get("cost_knowledge") == "measured" for attempt in relevant
    )
    if not ceilings:
        statement = "No external per-provider ceilings were declared."
    elif auditable:
        statement = "Usage is auditable against the declared USD provider ceilings."
    else:
        statement = (
            "Usage cannot be audited against the declared external provider ceilings: "
            "at least one provider has prepaid or unknown cost, or measured usage is not "
            "available in the reporting currency."
        )
    provider_usage = []
    for ceiling in ceilings:
        provider = str(ceiling["provider"])
        provider_attempts = [
            attempt for attempt in attempts if attempt.get("adapter") == provider
        ]
        provider_usage.append(
            {
                "attempts": len(provider_attempts),
                "ceiling": ceiling["ceiling"],
                "cost_knowledge": _counts(
                    provider_attempts, "cost_knowledge"
                ),
                "measured_cost_known_sum": math.fsum(
                    float(attempt["cost_usd"])
                    for attempt in provider_attempts
                    if attempt.get("cost_knowledge") == "measured"
                    and attempt.get("cost_usd") is not None
                ),
                "provider": provider,
                "tokens_in_known_sum": sum(
                    int(attempt["tokens_in"])
                    for attempt in provider_attempts
                    if attempt.get("tokens_in") is not None
                ),
                "tokens_out_known_sum": sum(
                    int(attempt["tokens_out"])
                    for attempt in provider_attempts
                    if attempt.get("tokens_out") is not None
                ),
            }
        )
    return {
        "audit_statement": statement,
        "declared_external_provider_ceilings": ceilings,
        "provider_usage": provider_usage,
        "reporting_currency": currency,
        "usage_auditable": auditable,
    }


def _reject_constant(_token: str) -> None:
    raise ValueError("non-standard JSON constant")
