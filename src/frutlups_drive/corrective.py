"""Guarded single-artifact application for architect corrective proposals."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from frutlups_drive.contracts import CorrectiveFailureClass


CORRECTIVE_PROPOSAL_NAME = "architect_corrective_proposal.json"
MAX_CORRECTIVE_PROPOSAL_BYTES = 1_048_576
_REWORK_PASS_ID = re.compile(r"holistic_pass_(?P<number>[0-9]{3})")
_BOUNDED_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class CorrectiveProposalRefusal(Exception):
    """One bounded proposal-validation/application refusal."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CorrectiveProposal:
    target: str
    content: bytes
    proposal_sha256: str


@dataclass(frozen=True)
class CorrectiveApplication:
    target: str
    before_sha256: str | None
    after_sha256: str


class CorrectiveProposalWriter:
    """Validate and atomically apply exactly one declared artifact.

    The staging proposal is evidence, never a write manifest.  The only
    project mutation is the byte string bound to its exact expected target.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        accepted_target: Callable[[str], bool],
        transaction_authority: Callable[
            [CorrectiveFailureClass, str, Mapping[str, object]], None
        ]
        | None = None,
    ) -> None:
        self._root = Path(project_root).resolve()
        self._accepted_target = accepted_target
        self._transaction_authority = transaction_authority

    def validate(
        self,
        proposal_path: Path,
        *,
        expected_target: str,
        failure_class: CorrectiveFailureClass,
    ) -> CorrectiveProposal:
        raw = self._bounded_read(proposal_path)
        payload = self._strict_json(raw)
        if not isinstance(payload, dict) or set(payload) != {
            "contract_version",
            "target",
            "content",
        }:
            raise CorrectiveProposalRefusal("proposal_shape_invalid")
        if payload.get("contract_version") != 1:
            raise CorrectiveProposalRefusal("proposal_contract_invalid")
        target = payload.get("target")
        content = payload.get("content")
        if not isinstance(target, str) or not self._canonical_relative(target):
            raise CorrectiveProposalRefusal("proposal_target_invalid")
        if target != expected_target:
            raise CorrectiveProposalRefusal("proposal_target_out_of_bounds")
        if not isinstance(content, str) or "\x00" in content:
            raise CorrectiveProposalRefusal("proposal_content_invalid")
        content_bytes = content.encode("utf-8")
        if not content_bytes or len(content_bytes) > MAX_CORRECTIVE_PROPOSAL_BYTES:
            raise CorrectiveProposalRefusal("proposal_content_invalid")
        authority_payload: Mapping[str, object]
        if failure_class is CorrectiveFailureClass.REWORK_DECLARATION_MAPPING:
            authority_payload = self._validate_rework(content)
            if not target.startswith("05_governance/rework_declarations/"):
                raise CorrectiveProposalRefusal("proposal_target_out_of_bounds")
        else:
            self._validate_prompt(content)
            authority_payload = {}
            if not target.startswith(
                ("prompts/for_coding_agent/", "prompts/for_review_agent/")
            ):
                raise CorrectiveProposalRefusal("proposal_target_out_of_bounds")
        if target.endswith(".slices.yaml"):
            raise CorrectiveProposalRefusal("proposal_target_out_of_bounds")
        if self._transaction_authority is not None:
            try:
                self._transaction_authority(
                    failure_class, target, authority_payload
                )
            except CorrectiveProposalRefusal:
                raise
            except Exception:
                raise CorrectiveProposalRefusal(
                    "proposal_transaction_refused"
                ) from None
        return CorrectiveProposal(
            target=target,
            content=content_bytes,
            proposal_sha256=hashlib.sha256(raw).hexdigest(),
        )

    def apply(self, proposal: CorrectiveProposal) -> CorrectiveApplication:
        target = self._ordinary_target(proposal.target)
        if self._accepted_target(proposal.target):
            raise CorrectiveProposalRefusal("proposal_target_accepted")
        before: bytes | None = None
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise CorrectiveProposalRefusal("proposal_target_invalid")
            try:
                before = target.read_bytes()
            except OSError:
                raise CorrectiveProposalRefusal("proposal_target_invalid") from None
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(proposal.content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, target)
        except OSError:
            raise CorrectiveProposalRefusal("proposal_application_failed") from None
        finally:
            temporary_path.unlink(missing_ok=True)
        return CorrectiveApplication(
            target=proposal.target,
            before_sha256=(
                hashlib.sha256(before).hexdigest() if before is not None else None
            ),
            after_sha256=hashlib.sha256(proposal.content).hexdigest(),
        )

    @staticmethod
    def _strict_json(raw: bytes) -> object:
        def reject_constant(value: str) -> None:
            raise ValueError(value)

        try:
            return json.loads(
                raw.decode("utf-8"), parse_constant=reject_constant
            )
        except (UnicodeDecodeError, ValueError, TypeError):
            raise CorrectiveProposalRefusal("proposal_shape_invalid") from None

    @staticmethod
    def _bounded_read(path: Path) -> bytes:
        try:
            with Path(path).open("rb") as stream:
                raw = stream.read(MAX_CORRECTIVE_PROPOSAL_BYTES + 1)
        except OSError:
            raise CorrectiveProposalRefusal("proposal_unreadable") from None
        if not raw or len(raw) > MAX_CORRECTIVE_PROPOSAL_BYTES:
            raise CorrectiveProposalRefusal("proposal_oversized")
        return raw

    @staticmethod
    def _canonical_relative(value: str) -> bool:
        if not value or chr(92) in value:
            return False
        path = PurePosixPath(value)
        return (
            not path.is_absolute()
            and path.as_posix() == value
            and all(part not in ("", ".", "..") for part in path.parts)
        )

    @staticmethod
    def _validate_prompt(content: str) -> None:
        lines = content.splitlines()
        if (
            not content.endswith("\n")
            or not lines
            or not lines[0].startswith("# ")
        ):
            raise CorrectiveProposalRefusal("proposal_prompt_shape_invalid")

    @staticmethod
    def _validate_rework(content: str) -> Mapping[str, object]:
        try:
            payload = json.loads(content)
        except (ValueError, TypeError):
            raise CorrectiveProposalRefusal("proposal_rework_shape_invalid") from None
        required = {
            "contract_id",
            "contract_version",
            "declaration_sequence",
            "pass_id",
            "baseline_prompt_sequence",
            "slice_ids",
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise CorrectiveProposalRefusal("proposal_rework_shape_invalid")
        sequence = payload.get("declaration_sequence")
        baseline = payload.get("baseline_prompt_sequence")
        pass_id = payload.get("pass_id")
        slice_ids = payload.get("slice_ids")
        if (
            payload.get("contract_id") != "frutlups.rework_declaration"
            or payload.get("contract_version") != "1"
            or type(sequence) is not int
            or sequence < 1
            or type(baseline) is not int
            or baseline < 0
            or not isinstance(pass_id, str)
            or _REWORK_PASS_ID.fullmatch(pass_id) is None
            or not isinstance(slice_ids, list)
            or not 1 <= len(slice_ids) <= 64
            or any(
                not isinstance(item, str) or not _BOUNDED_ID.fullmatch(item)
                for item in slice_ids
            )
            or len(set(slice_ids)) != len(slice_ids)
        ):
            raise CorrectiveProposalRefusal("proposal_rework_shape_invalid")
        return payload

    def _ordinary_target(self, relative: str) -> Path:
        target = self._root.joinpath(*PurePosixPath(relative).parts)
        try:
            target.parent.resolve().relative_to(self._root)
        except (OSError, ValueError):
            raise CorrectiveProposalRefusal("proposal_target_invalid") from None
        current = self._root
        for part in PurePosixPath(relative).parts[:-1]:
            current = current / part
            if current.is_symlink() or not current.is_dir():
                raise CorrectiveProposalRefusal("proposal_target_invalid")
        return target
