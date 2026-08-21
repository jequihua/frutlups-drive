"""Pure pass-boundary reconciliation for the holistic review oracle.

The bundle contains observations, never findings.  Callers may furnish it to
an independent reviewer, but must not interpret observation content as a
worklist or closure decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from frutlups_drive.policy import INDEX_MODES


INDEX_PATH = "05_governance/reviews/INDEX.md"
MAX_ORACLE_INPUT_BYTES = 16 * 1024 * 1024

_SHA256 = re.compile(r"[0-9a-f]{64}")
_SLICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_PASS_ID = re.compile(r"holistic_pass_[0-9]{3}")
_DRAIN_ANNOTATION = re.compile(
    r"addressed by rework round [1-9][0-9]*; re-confirm or refute against it"
)
_BACKTICK = re.compile(r"`([^`\r\n]+)`")
_RECORD_SLICE = re.compile(
    r"^Slice ID:\s*`(?P<value>[A-Za-z0-9][A-Za-z0-9._-]*)`\s*$",
    re.MULTILINE,
)
_RECORD_VERDICT = re.compile(
    r"^Verdict:\s*`(?P<value>pass|needs_work|blocked|override)`\s*$",
    re.MULTILINE,
)
_RECORD_REPORT = re.compile(
    r"^Review report:\s*`(?P<value>[^`\r\n]+)`\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_RELEASED_VERDICT_LINE = re.compile(
    r"^Verdict:\s*(?P<value>pass|needs_work|blocked|override)\s*"
    r"(?:-|—)\s*next:\s*\S.*$",
    re.MULTILINE,
)

OBSERVATION_CLASSES = frozenset(
    {
        "accepted_index_row_missing_path",
        "artifact_hash_drift",
        "evidence_hash_drift",
        "index_path_not_in_manifest",
        "ledger_row_in_no_ledger_project",
        "manifest_review_artifact_not_indexed",
        "verdict_record_invalid",
        "verdict_record_missing",
        "verdict_report_line_invalid",
        "verdict_review_reference_mismatch",
        "verdict_slice_mismatch",
        "verdict_value_mismatch",
        "verdict_review_report_missing",
        "verdict_self_report_missing",
    }
)


class OracleRefusal(Exception):
    """A required oracle input could not be read safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class _AcceptedRow:
    slice_id: str
    self_report: str | None
    review_report: str | None
    verdict: str
    cited_paths: tuple[str, ...]


@dataclass(frozen=True)
class _LedgerRow:
    slice_id: str | None
    cited_paths: tuple[str, ...]


def reconcile_pass_boundary(
    pass_boundary: object,
    project_root: Path | str,
    evidence_root: Path | str,
    *,
    index_mode: str = "human-ledger",
    max_file_bytes: int = MAX_ORACLE_INPUT_BYTES,
) -> dict[str, object]:
    """Return one deterministic reconciliation bundle without writing.

    ``project_root`` resolves the manifest's ``artifacts`` members and
    ``evidence_root`` resolves its ``evidence`` members.  Missing, oversized,
    non-regular, or undecodable required inputs refuse; readable
    contradictions are observations.
    """

    if type(max_file_bytes) is not int or max_file_bytes <= 0:
        raise ValueError("max_file_bytes must be a positive integer")
    if index_mode not in INDEX_MODES:
        raise OracleRefusal(
            "index_mode_invalid", "the declared review-index mode is unsupported"
        )
    record = _checked_boundary(pass_boundary)
    project = _checked_root(project_root, "project_root_unreadable")
    evidence = _checked_root(evidence_root, "evidence_root_unreadable")
    artifact_map = {
        member["path"]: member["sha256"] for member in record["artifacts"]
    }
    evidence_map = {
        member["path"]: member["sha256"] for member in record["evidence"]
    }

    index = _read_named(
        project,
        INDEX_PATH,
        max_file_bytes,
        code="index_unreadable",
        require_manifest=artifact_map,
    )
    try:
        index_text = index.decode("utf-8")
    except UnicodeDecodeError:
        raise OracleRefusal(
            "index_unreadable", "the review index is not valid UTF-8"
        ) from None

    observed_artifacts = _verify_inventory(
        project,
        artifact_map,
        max_file_bytes,
        area="artifact",
    )
    observed_evidence = _verify_inventory(
        evidence,
        evidence_map,
        max_file_bytes,
        area="evidence",
    )
    observations: list[dict[str, object]] = []

    rows, cited = _index_rows(index_text)
    if index_mode == "human-ledger":
        for path, slices in sorted(cited.items()):
            if path not in artifact_map:
                observations.append(
                    _observation(
                        "index_path_not_in_manifest",
                        next(iter(slices)) if len(slices) == 1 else None,
                        [path],
                        artifact_map,
                        observed_artifacts,
                    )
                )

        for path in sorted(artifact_map):
            if _review_side_artifact(path) and path not in cited:
                observations.append(
                    _observation(
                        "manifest_review_artifact_not_indexed",
                        _slice_from_path(path),
                        [path],
                        artifact_map,
                        observed_artifacts,
                    )
                )
    else:
        for row in _ledger_rows(index_text):
            observations.append(
                _observation(
                    "ledger_row_in_no_ledger_project",
                    row.slice_id,
                    list(row.cited_paths) or [INDEX_PATH],
                    artifact_map,
                    observed_artifacts,
                )
            )

    accepted: dict[str, _AcceptedRow] = {}
    for row in rows:
        if row.verdict in ("pass", "override"):
            accepted[row.slice_id] = row
    for slice_id in sorted(accepted):
        _check_verdict_chain(
            accepted[slice_id],
            project,
            artifact_map,
            observed_artifacts,
            max_file_bytes,
            observations,
        )

    for path in sorted(artifact_map):
        if artifact_map[path] != observed_artifacts[path]:
            observations.append(
                _observation(
                    "artifact_hash_drift",
                    _slice_for_path(path, cited),
                    [path],
                    artifact_map,
                    observed_artifacts,
                )
            )
    for path in sorted(evidence_map):
        if evidence_map[path] != observed_evidence[path]:
            observations.append(
                _observation(
                    "evidence_hash_drift",
                    None,
                    [path],
                    evidence_map,
                    observed_evidence,
                )
            )

    draining_rounds: dict[str, int] = {}
    if "events.jsonl" in evidence_map:
        events_data = _read_named(
            evidence, "events.jsonl", max_file_bytes, code="evidence_unreadable"
        )
        primary_events = [
            event
            for _, event in _event_records(events_data)
            if event.get("kind") not in ("shadow_review", "memory_hook")
        ]
        draining_rounds = _draining_rework_rounds(primary_events)
    for observation in observations:
        round_number = draining_rounds.get(str(observation.get("slice_id", "")))
        if round_number is not None:
            observation["annotation"] = (
                f"addressed by rework round {round_number}; "
                "re-confirm or refute against it"
            )

    observations.sort(
        key=lambda item: (
            str(item["class"]),
            str(item["slice_id"] or ""),
            tuple(item["paths"]),
            json.dumps(item["hashes"], sort_keys=True, separators=(",", ":")),
        )
    )
    return {
        "contract_version": 1,
        "run_id": record["run_id"],
        "index_mode": index_mode,
        "pass_boundary_sha256": hashlib.sha256(
            _canonical_json_bytes(record)
        ).hexdigest(),
        "observations": observations,
    }


def valid_oracle_bundle(
    payload: object,
    run_id: str,
    pass_boundary_sha256: str,
    index_mode: str = "human-ledger",
) -> bool:
    """Validate the exact persisted bundle schema without interpreting it."""

    if (
        not isinstance(payload, dict)
        or set(payload)
        != {
            "contract_version",
            "run_id",
            "index_mode",
            "pass_boundary_sha256",
            "observations",
        }
        or payload.get("contract_version") != 1
        or payload.get("run_id") != run_id
        or payload.get("index_mode") != index_mode
        or index_mode not in INDEX_MODES
        or payload.get("pass_boundary_sha256") != pass_boundary_sha256
        or not isinstance(payload.get("observations"), list)
    ):
        return False
    for observation in payload["observations"]:
        keys = set(observation) if isinstance(observation, dict) else set()
        if (
            not isinstance(observation, dict)
            or keys
            not in (
                {"type", "class", "slice_id", "paths", "hashes"},
                {
                    "type",
                    "class",
                    "slice_id",
                    "paths",
                    "hashes",
                    "annotation",
                },
            )
            or observation.get("type") != "oracle_observation"
            or observation.get("class") not in OBSERVATION_CLASSES
            or (
                observation.get("slice_id") is not None
                and (
                    not isinstance(observation.get("slice_id"), str)
                    or not _SLICE_ID.fullmatch(observation["slice_id"])
                )
            )
            or not isinstance(observation.get("paths"), list)
            or not observation["paths"]
            or len(set(observation["paths"])) != len(observation["paths"])
            or any(not _canonical_relative(path) for path in observation["paths"])
            or not isinstance(observation.get("hashes"), list)
            or (
                "annotation" in observation
                and (
                    observation.get("slice_id") is None
                    or not isinstance(observation.get("annotation"), str)
                    or not _DRAIN_ANNOTATION.fullmatch(observation["annotation"])
                )
            )
        ):
            return False
        for pointer in observation["hashes"]:
            if (
                not isinstance(pointer, dict)
                or set(pointer)
                != {"path", "recorded_sha256", "observed_sha256"}
                or pointer.get("path") not in observation["paths"]
                or not _sha256(pointer.get("recorded_sha256"))
                or not _sha256(pointer.get("observed_sha256"))
            ):
                return False
    return True


def _checked_boundary(payload: object) -> dict:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"contract_version", "run_id", "evidence", "artifacts"}
        or payload.get("contract_version") != 1
        or not isinstance(payload.get("run_id"), str)
        or not _SLICE_ID.fullmatch(payload["run_id"])
    ):
        raise OracleRefusal(
            "pass_boundary_invalid", "the frozen pass-boundary record is malformed"
        )
    for area in ("evidence", "artifacts"):
        members = payload.get(area)
        if not isinstance(members, list):
            raise OracleRefusal(
                "pass_boundary_invalid", f"the {area} inventory is malformed"
            )
        seen: set[str] = set()
        for member in members:
            if (
                not isinstance(member, dict)
                or set(member) != {"path", "sha256"}
                or not _canonical_relative(member.get("path"))
                or not _sha256(member.get("sha256"))
                or member["path"] in seen
            ):
                raise OracleRefusal(
                    "pass_boundary_invalid", f"the {area} inventory is malformed"
                )
            seen.add(member["path"])
    return payload


def _checked_root(root: Path | str, code: str) -> Path:
    try:
        path = Path(root).resolve(strict=True)
        if not path.is_dir():
            raise OSError
    except (OSError, TypeError):
        raise OracleRefusal(code, "the oracle root is unavailable") from None
    return path


def _read_named(
    root: Path,
    relative: str,
    maximum: int,
    *,
    code: str,
    require_manifest: dict[str, str] | None = None,
) -> bytes:
    if require_manifest is not None and relative not in require_manifest:
        raise OracleRefusal(code, f"required input '{relative}' is not frozen")
    try:
        path = (root / Path(relative)).resolve(strict=True)
        path.relative_to(root)
        if path.is_symlink() or not path.is_file():
            raise OSError
        with open(path, "rb") as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise OracleRefusal(code, f"required input '{relative}' exceeds its bound")
        return data
    except OracleRefusal:
        raise
    except (OSError, ValueError):
        raise OracleRefusal(code, f"required input '{relative}' is unreadable") from None


def _verify_inventory(
    root: Path,
    inventory: dict[str, str],
    maximum: int,
    *,
    area: str,
) -> dict[str, str]:
    observed: dict[str, str] = {}
    for relative in sorted(inventory):
        code = f"{area}_unreadable"
        data = _read_named(root, relative, maximum, code=code)
        if area == "evidence" and relative == "events.jsonl":
            digest = _primary_events_digest(data)
        else:
            digest = hashlib.sha256(data).hexdigest()
        observed[relative] = digest
    return observed


def _primary_events_digest(data: bytes) -> str:
    primary: list[bytes] = []
    for line, event in _event_records(data):
        if event.get("kind") not in ("shadow_review", "memory_hook"):
            primary.append(line)
    return hashlib.sha256(b"".join(primary)).hexdigest()


def _event_records(data: bytes) -> list[tuple[bytes, dict[str, object]]]:
    records: list[tuple[bytes, dict[str, object]]] = []
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            raise OracleRefusal(
                "evidence_unreadable", "events.jsonl contains a partial event"
            )
        try:
            event = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise OracleRefusal(
                "evidence_unreadable", "events.jsonl contains invalid JSON"
            ) from None
        if not isinstance(event, dict):
            raise OracleRefusal(
                "evidence_unreadable", "events.jsonl contains a non-object event"
            )
        records.append((line, event))
    return records


def _draining_rework_rounds(
    events: list[dict[str, object]],
) -> dict[str, int]:
    """Return only journal-proven, unambiguous post-declaration drains."""

    declarations: dict[str, list[int]] = {}
    for index, event in enumerate(events):
        if event.get("kind") != "verb" or event.get("verb") != "declare-rework":
            continue
        slices = event.get("slices")
        if not isinstance(slices, list):
            continue
        for slice_id in slices:
            if isinstance(slice_id, str):
                declarations.setdefault(slice_id, []).append(index)

    drained: dict[str, int] = {}
    for slice_id, positions in declarations.items():
        if len(positions) != 1 or not _SLICE_ID.fullmatch(slice_id):
            continue
        declaration = events[positions[0]]
        slices = declaration.get("slices")
        if (
            not isinstance(slices, list)
            or not slices
            or any(
                not isinstance(item, str) or not _SLICE_ID.fullmatch(item)
                for item in slices
            )
            or len(slices) != len(set(slices))
            or not isinstance(declaration.get("pass_id"), str)
            or not _PASS_ID.fullmatch(declaration["pass_id"])
        ):
            continue
        round_number = _strict_drain_round(
            events, positions[0] + 1, slice_id
        )
        if round_number is not None:
            drained[slice_id] = round_number
    return drained


def _strict_drain_round(
    events: list[dict[str, object]], start: int, slice_id: str
) -> int | None:
    verdicts = [
        index
        for index in range(start, len(events))
        if events[index].get("kind") == "verb"
        and events[index].get("verb") == "record-verdict"
        and events[index].get("slice") == slice_id
        and _canonical_relative(events[index].get("artifact"))
    ]
    if len(verdicts) != 1:
        return None
    verdict_index = verdicts[0]

    coder_dispatches = [
        index
        for index in range(start, verdict_index)
        if events[index].get("kind") == "dispatch"
        and events[index].get("role") == "coder"
        and events[index].get("slice") == slice_id
        and events[index].get("repair") is False
        and isinstance(events[index].get("attempt"), str)
        and bool(events[index].get("attempt"))
        and _canonical_relative(events[index].get("prompt_source"))
    ]
    if not coder_dispatches:
        return None
    coder_index = coder_dispatches[-1]
    if any(
        event.get("kind") == "dispatch"
        and event.get("role") == "coder"
        and event.get("slice") == slice_id
        for event in events[coder_index + 1 : verdict_index]
    ):
        return None
    coder = events[coder_index]
    coder_attempt = coder["attempt"]
    coding_source = coder["prompt_source"]
    coding_prompts = [
        index
        for index in range(start, coder_index)
        if events[index].get("kind") == "verb"
        and events[index].get("verb") == "make-coding-prompt"
        and events[index].get("slice") == slice_id
        and events[index].get("artifact") == coding_source
    ]
    if len(coding_prompts) != 1:
        return None

    coder_collections = [
        index
        for index in range(coder_index + 1, verdict_index)
        if events[index].get("kind") == "collected"
        and events[index].get("role") == "coder"
        and events[index].get("slice") == slice_id
    ]
    if len(coder_collections) != 1:
        return None
    coder_collected = coder_collections[0]
    if (
        events[coder_collected].get("attempt") != coder_attempt
        or events[coder_collected].get("status") != "completed"
    ):
        return None
    verifications = [
        index
        for index in range(coder_collected + 1, verdict_index)
        if events[index].get("kind") == "verification"
        and events[index].get("slice") == slice_id
    ]
    if len(verifications) != 1:
        return None
    verified = verifications[0]
    if (
        events[verified].get("attempt") != coder_attempt
        or events[verified].get("passed") is not True
    ):
        return None

    reviewer_dispatches = [
        index
        for index in range(verified + 1, verdict_index)
        if events[index].get("kind") == "dispatch"
        and events[index].get("role") == "reviewer"
        and events[index].get("slice") == slice_id
    ]
    if len(reviewer_dispatches) != 1:
        return None
    reviewer_index = reviewer_dispatches[0]
    reviewer = events[reviewer_index]
    if (
        reviewer.get("repair") is not False
        or not isinstance(reviewer.get("attempt"), str)
        or not reviewer.get("attempt")
        or not _canonical_relative(reviewer.get("prompt_source"))
    ):
        return None
    reviewer_attempt = reviewer["attempt"]
    review_source = reviewer["prompt_source"]
    review_prompts = [
        index
        for index in range(verified + 1, reviewer_index)
        if events[index].get("kind") == "verb"
        and events[index].get("verb") == "make-review-prompt"
        and events[index].get("slice") == slice_id
        and events[index].get("artifact") == review_source
    ]
    if len(review_prompts) != 1:
        return None
    reviewer_collections = [
        index
        for index in range(reviewer_index + 1, verdict_index)
        if events[index].get("kind") == "collected"
        and events[index].get("role") == "reviewer"
        and events[index].get("slice") == slice_id
    ]
    if len(reviewer_collections) != 1:
        return None
    reviewer_collected = reviewer_collections[0]
    if (
        events[reviewer_collected].get("attempt") != reviewer_attempt
        or events[reviewer_collected].get("status") != "completed"
    ):
        return None

    prior_coder_collections = sum(
        event.get("kind") == "collected"
        and event.get("role") == "coder"
        and event.get("slice") == slice_id
        for event in events[start:coder_index]
    )
    return prior_coder_collections + 1


def _index_rows(text: str) -> tuple[list[_AcceptedRow], dict[str, set[str]]]:
    rows: list[_AcceptedRow] = []
    citations: dict[str, set[str]] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if (
            len(cells) != 8
            or cells[1].lower() == "slice"
            or not _SLICE_ID.fullmatch(cells[1])
        ):
            continue
        slice_id = cells[1]
        row_paths: list[str] = []
        for match in _BACKTICK.finditer(line):
            path = match.group(1)
            if _artifact_path(path):
                citations.setdefault(path, set()).add(slice_id)
                if path not in row_paths:
                    row_paths.append(path)
        verdict = cells[6].lower()
        rows.append(
            _AcceptedRow(
                slice_id=slice_id,
                self_report=_first_path(cells[3]),
                review_report=_first_path(cells[5]),
                verdict=verdict,
                cited_paths=tuple(row_paths),
            )
        )
    return rows, citations


def _ledger_rows(text: str) -> list[_LedgerRow]:
    """Return every non-template Markdown table row, valid or malformed."""

    rows: list[_LedgerRow] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if (
            (len(cells) > 1 and cells[1].lower() == "slice")
            or (
                cells
                and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)
            )
        ):
            continue
        cited_paths = tuple(
            dict.fromkeys(
                match.group(1)
                for match in _BACKTICK.finditer(line)
                if _artifact_path(match.group(1))
            )
        )
        slice_id = (
            cells[1]
            if len(cells) > 1 and _SLICE_ID.fullmatch(cells[1])
            else None
        )
        rows.append(_LedgerRow(slice_id=slice_id, cited_paths=cited_paths))
    return rows


def _first_path(cell: str) -> str | None:
    for match in _BACKTICK.finditer(cell):
        if _artifact_path(match.group(1)):
            return match.group(1)
    return None


def _artifact_path(value: object) -> bool:
    if not _canonical_relative(value):
        return False
    path = PurePosixPath(value)
    return bool(path.suffix) and not any(char.isspace() for char in value)


def _review_side_artifact(path: str) -> bool:
    return (
        path.startswith("05_governance/reviews/")
        and path.endswith(("_self_report.md", "_review_report.md"))
    ) or (
        path.startswith("prompts/for_review_agent/") and path.endswith(".md")
    )


def rework_protected_artifact(path: str) -> bool:
    """Return whether a frozen artifact is protected during coder rework.

    This deliberately leaves the holistic oracle's narrower review-side
    predicate unchanged.  The rework fence additionally protects verdict
    records and governed coding prompts while excluding product/code paths.
    """

    if not _canonical_relative(path):
        return False
    return _review_side_artifact(path) or (
        path.startswith("05_governance/reviews/")
        and path.endswith("_verdict_record.md")
    ) or (
        path.startswith("prompts/for_coding_agent/") and path.endswith(".md")
    )


def _check_verdict_chain(
    row: _AcceptedRow,
    project: Path,
    manifest: dict[str, str],
    observed: dict[str, str],
    maximum: int,
    observations: list[dict[str, object]],
) -> None:
    paths = [path for path in (row.self_report, row.review_report) if path]
    if row.self_report is None or row.review_report is None:
        observations.append(
            _observation(
                "accepted_index_row_missing_path",
                row.slice_id,
                paths or [INDEX_PATH],
                manifest,
                observed,
            )
        )
    if row.self_report is not None and row.self_report not in manifest:
        observations.append(
            _observation(
                "verdict_self_report_missing",
                row.slice_id,
                [row.self_report],
                manifest,
                observed,
            )
        )
    if row.review_report is None:
        return
    if row.review_report not in manifest:
        observations.append(
            _observation(
                "verdict_review_report_missing",
                row.slice_id,
                [row.review_report],
                manifest,
                observed,
            )
        )
        return

    verdict_path = row.review_report.removesuffix("_review_report.md")
    if verdict_path == row.review_report:
        observations.append(
            _observation(
                "verdict_record_missing",
                row.slice_id,
                [row.review_report],
                manifest,
                observed,
            )
        )
        return
    verdict_path += "_verdict_record.md"
    if verdict_path not in manifest:
        observations.append(
            _observation(
                "verdict_record_missing",
                row.slice_id,
                [verdict_path],
                manifest,
                observed,
            )
        )
        return

    verdict_text = _read_utf8(project, verdict_path, maximum, "artifact_unreadable")
    record_slice = _one_match(_RECORD_SLICE, verdict_text)
    record_verdict = _one_match(_RECORD_VERDICT, verdict_text)
    record_report = _one_match(_RECORD_REPORT, verdict_text)
    if None in (record_slice, record_verdict, record_report):
        observations.append(
            _observation(
                "verdict_record_invalid",
                row.slice_id,
                [verdict_path],
                manifest,
                observed,
            )
        )
        return
    if record_slice != row.slice_id:
        observations.append(
            _observation(
                "verdict_slice_mismatch",
                row.slice_id,
                [verdict_path],
                manifest,
                observed,
            )
        )
    if record_report != row.review_report:
        observations.append(
            _observation(
                "verdict_review_reference_mismatch",
                row.slice_id,
                [verdict_path, row.review_report],
                manifest,
                observed,
            )
        )

    report_text = _read_utf8(
        project, row.review_report, maximum, "artifact_unreadable"
    )
    report_matches = list(_RELEASED_VERDICT_LINE.finditer(report_text))
    if len(report_matches) != 1:
        observations.append(
            _observation(
                "verdict_report_line_invalid",
                row.slice_id,
                [row.review_report],
                manifest,
                observed,
            )
        )
        return
    report_verdict = report_matches[0].group("value")
    if len({row.verdict, record_verdict, report_verdict}) != 1:
        observations.append(
            _observation(
                "verdict_value_mismatch",
                row.slice_id,
                [verdict_path, row.review_report],
                manifest,
                observed,
            )
        )


def _read_utf8(root: Path, relative: str, maximum: int, code: str) -> str:
    data = _read_named(root, relative, maximum, code=code)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise OracleRefusal(code, f"required input '{relative}' is not UTF-8") from None


def _one_match(pattern: re.Pattern[str], text: str) -> str | None:
    matches = list(pattern.finditer(text))
    return matches[0].group("value") if len(matches) == 1 else None


def _observation(
    observation_class: str,
    slice_id: str | None,
    paths: list[str],
    recorded: dict[str, str],
    observed: dict[str, str],
) -> dict[str, object]:
    unique_paths = list(dict.fromkeys(paths))
    hashes = [
        {
            "path": path,
            "recorded_sha256": recorded[path],
            "observed_sha256": observed[path],
        }
        for path in unique_paths
        if path in recorded and path in observed
    ]
    return {
        "type": "oracle_observation",
        "class": observation_class,
        "slice_id": slice_id,
        "paths": unique_paths,
        "hashes": hashes,
    }


def _slice_for_path(path: str, cited: dict[str, set[str]]) -> str | None:
    slices = cited.get(path, set())
    if len(slices) == 1:
        return next(iter(slices))
    return _slice_from_path(path)


def _slice_from_path(path: str) -> str | None:
    match = re.search(r"(?<![a-z0-9])m(\d{3})_s(\d{2})(?![a-z0-9])", path, re.I)
    return f"M{match.group(1)}-S{match.group(2)}" if match else None


def _canonical_relative(value: object) -> bool:
    if not isinstance(value, str) or not value or chr(92) in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and path.as_posix() == value
        and all(part not in ("", ".", "..") for part in path.parts)
    )


def _sha256(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256.fullmatch(value))


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
