"""Supervisor: one bound action per tick over fresh planning state (§5, §9).

One ``tick()``: kill switch, budgets, fresh planning-state read, exactly one
action from the binding tables, journal, typed result. No cached planning
belief crosses a side effect; everything a fresh process needs lives in the
run store and repository artifacts. ``run_until()`` loops ticks;
``resume()`` reconciles ``started`` attempts (external completion, stale
prompt hashes, abandonment) before continuing.

The scripted plan provider's replay position is the count of completed
``tick`` journal events, so a mid-tick crash re-serves the same state exactly
as a real recomputation would.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from frutlups_drive import killswitch, ladder
from frutlups_drive.budget import BudgetCounters, BudgetGate, Clock
from frutlups_drive.contracts import (
    AgentRunRequest,
    AgentRunResult,
    LoopStep,
    PlanOutcome,
    Role,
    StopReason,
)
from frutlups_drive.dispatch.base import (
    AgentExecutor,
    CostAuthorizationExceeded,
    WorkspaceAuthorityDenied,
)
from frutlups_drive.escalate import write_escalation
from frutlups_drive.frutlupscli import (
    FrutlupsCorrectiveRound,
    FrutlupsVerbError,
    RecoveredVerb,
)
from frutlups_drive.mockverbs import (
    MockVerbWriter,
    VerbAuthorityDenied,
    VerbScriptExhausted,
)
from frutlups_drive.memory_hooks import LlloomMemoryHooks, MemoryHookFact
from frutlups_drive.oracle import (
    OracleRefusal,
    reconcile_pass_boundary,
    rework_protected_artifact,
    valid_oracle_bundle,
)
from frutlups_drive.planstate import (
    MockScriptExhausted,
    PlanningState,
    PlanningStateRefusal,
    PlanProvider,
    PlanProviderUnavailable,
)
from frutlups_drive.policy import _ADAPTER_VALUES, ExecutionPolicy
from frutlups_drive.reconciliation import (
    ReconciliationRefusal,
    ReconciliationWriter,
)
from frutlups_drive.runstore import (
    TRANSITION_STATES,
    RunStore,
    RunStoreRefusal,
)
from frutlups_drive.verifier import (
    VerificationPlan,
    Verifier,
    validate_evidence,
    validate_evidence_envelope,
)
from frutlups_drive.watcher import Watcher
from frutlups_drive.workspace import (
    WorkspaceLease,
    WorkspaceManager,
    check_fences,
)

BOUNDARIES = (
    "slice_complete",
    "milestone_complete",
    "roadmap_complete",
    "pass_complete",
)

EVENT_KINDS = (
    "run_created",
    "resume",
    "plan_consumed",
    "reconciled_attempt",
    "plan_read",
    "refusal",
    "verb",
    "dispatch",
    "collected",
    "attempt_abandoned",
    "watch_timeout",
    "backoff",
    "run_store_control",
    "adoption",
    "verification",
    "fence",
    "reconciliation",
    "pass_boundary",
    "pass_oracle",
    "holistic_review",
    "shadow_review",
    "memory_hook",
    "slice_complete",
    "continue_past_frontier",
    "boundary",
    "stop",
    "tick",
)

_STEP_VERBS = {
    LoopStep.MAKE_CODING_PROMPT: "make-coding-prompt",
    LoopStep.MAKE_REVIEW_PROMPT: "make-review-prompt",
    LoopStep.RECORD_VERDICT: "record-verdict",
}
_VERB_ARTIFACT_FIELDS = {
    "make-coding-prompt": "coding_prompt",
    "make-review-prompt": "review_prompt",
    "record-verdict": "verdict_record",
}

# Role seats are provider-neutral facts read from the one policy snapshot.
# ``manual`` and ``mock`` are the local adapter classes whose frozen policy
# convention allows an empty model; every other policy adapter value is
# external-class and needs a non-blank model before it could ever become
# executable. No seat logic may infer a model family from string spelling,
# adapter name, case, or marketing alias — only exact equality is computed.
LOCAL_ADAPTERS = ("manual", "mock")
EXTERNAL_ADAPTERS = tuple(
    adapter for adapter in _ADAPTER_VALUES if adapter not in LOCAL_ADAPTERS
)

_RECONCILIATION_PROPOSAL = "roadmap_proposal.md"
_HOLISTIC_REVIEW = "holistic_review.json"
_SHADOW_REVIEW = "shadow_report.md"
_OWNER_NOTES_RELATIVE = Path("05_governance/human_owner_notes")
_MAX_OWNER_NOTES = 1_000
_MAX_BOUNDARY_MEMBERS = 20_000
_SLICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_MAX_SEAT_CONDUCT_BLOCK_BYTES = 768
_MAX_REWORK_ENVELOPE_BYTES = 4_096
_STOP_JOURNAL_ATTEMPTED = "_frutlups_stop_journal_attempted"
_REFUSAL_ROUTING_ATTEMPTED = "_frutlups_refusal_routing_attempted"
_SEAT_CONDUCT_BLOCK = (
    b"\n\n## Seat Conduct Boundary\n\n"
    b"Work only inside your assigned workspace. This confinement covers files, "
    b"processes, and system state. Never enumerate the host process table to "
    b"remediate a problem. Never kill, stop, signal, or otherwise manage any "
    b"process, including one you believe you spawned or believe is runaway. "
    b"Launch only the child processes required by your declared verification "
    b"commands, and let them finish. Make no host-level changes: do not install "
    b"software, mutate the environment or configuration, or change system "
    b"settings. If anything outside the workspace appears wrong, including a "
    b"suspected runaway process, busy port, or locked file, end your turn and "
    b"report the observation in your self-report instead of acting on it.\n"
)


@dataclass(frozen=True)
class RoleSeat:
    role: Role
    adapter: str
    model: str


def policy_seat(policy: ExecutionPolicy, role: Role) -> RoleSeat:
    section = {
        Role.ARCHITECT: policy.architect,
        Role.CODER: policy.coder,
        Role.REVIEWER: policy.reviewer,
    }[role]
    return RoleSeat(role=role, adapter=section.adapter, model=section.model)


def shadow_policy_seat(policy: ExecutionPolicy) -> RoleSeat:
    section = policy.shadow_reviewer
    return RoleSeat(role=Role.REVIEWER, adapter=section.adapter, model=section.model)


def seat_executable_issue(seat: RoleSeat) -> str | None:
    """One bounded executability fact: external-class seats need a model.

    Local adapters (``manual``/``mock``) keep their frozen empty-model
    convention. This returns a stable code, never a refusal message with
    machine or policy content.
    """
    if seat.adapter in LOCAL_ADAPTERS:
        return None
    if not seat.model.strip():
        return "external_model_missing"
    return None


def exact_seat_alias(first: RoleSeat, second: RoleSeat) -> bool:
    """Exact-seat identity: byte-equal adapter and model strings only.

    A ``True`` result proves the two roles are configured onto the same
    exact seat. A ``False`` result proves only exact-seat non-aliasing; it
    is never a model-family independence claim (that remains a Phase C
    owner fact).
    """
    return (first.adapter, first.model) == (second.adapter, second.model)


def mock_plan_offset(events: tuple[dict, ...] | list[dict]) -> int:
    """Reconstruct mock payload consumption including Phase B inner reads.

    Ordinary actions are represented by consumed ticks. A durable Phase B
    control fact can outlive a crash before its tick, and the reconciliation
    coherence check consumes one additional planning observation.
    """
    ticks = [
        event
        for event in events
        if event.get("kind") == "tick" and event.get("consumed")
    ]
    controls = sum(event.get("kind") == "plan_consumed" for event in events)
    paired_controls = sum(
        event.get("detail") in ("reconciliation", "pass_boundary")
        for event in ticks
    )
    inner_reads = sum(
        event.get("kind") == "plan_read"
        and event.get("context") == "reconciliation"
        for event in events
    )
    return len(ticks) + max(0, controls - paired_controls) + inner_reads


@dataclass(frozen=True)
class TickResult:
    kind: str  # "acted" | "boundary" | "stopped" | "refused"
    detail: str
    stop_reason: StopReason | None = None
    escalation_path: Path | None = None


class Supervisor:
    def __init__(
        self,
        *,
        project_root: Path,
        store: RunStore,
        run_id: str,
        policy: ExecutionPolicy,
        boundary: str,
        plan_provider: PlanProvider,
        executors: Mapping[str, AgentExecutor],
        verb_writer: MockVerbWriter,
        verifier: Verifier,
        verification_plan: VerificationPlan,
        watcher: Watcher,
        workspace: WorkspaceManager,
        clock: Clock,
        watch_timeout_seconds: float = 300.0,
        round4_authority: Callable[[], bool] | None = None,
        role_efforts: Mapping[str, str | tuple[str, str]] | None = None,
        max_call_cost_usd: float | None = None,
        max_total_cost_usd: float | None = None,
        external_dispatch_guard: Callable[[], None] | None = None,
        memory_hooks: LlloomMemoryHooks | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if boundary not in BOUNDARIES:
            raise ValueError(f"unknown boundary: {boundary}")
        self._project_root = Path(project_root)
        self._store = store
        self._run_id = run_id
        self._policy = policy
        self._boundary = boundary
        self._plan_provider = plan_provider
        self._executors = dict(executors)
        self._verb_writer = verb_writer
        self._verifier = verifier
        self._verification_plan = verification_plan
        self._watcher = watcher
        self._workspace = workspace
        self._clock = clock
        self._watch_timeout = watch_timeout_seconds
        self._round4_authority = round4_authority or (lambda: False)
        self._role_efforts: dict[str, tuple[str, str]] = {}
        for role_name, declared in (role_efforts or {}).items():
            if isinstance(declared, str):
                self._role_efforts[role_name] = (declared, declared)
            elif (
                isinstance(declared, (tuple, list))
                and len(declared) == 2
                and all(isinstance(item, str) for item in declared)
            ):
                self._role_efforts[role_name] = (declared[0], declared[1])
            else:
                raise ValueError("role effort schedule must contain two strings")
        self._max_call_cost_usd = max_call_cost_usd
        self._max_total_cost_usd = max_total_cost_usd
        self._external_dispatch_guard = external_dispatch_guard
        self._memory_hooks = memory_hooks
        self._sleep = sleep or time.sleep
        self._reconciliation_writer = ReconciliationWriter(self._project_root)
        self._events = list(store.read_events(run_id))
        self._counters = BudgetCounters.from_events(self._events)
        self._gate = BudgetGate(policy, clock)
        # The real frutlups writer needs the fresh state's review-report
        # reference for record-verdict; the accepted mock writer does not.
        # Signature inspection keeps both writer contracts unchanged.
        self._verb_supports_report = (
            "review_report" in inspect.signature(verb_writer.invoke).parameters
        )
        self._verb_supports_context = (
            "slice_id" in inspect.signature(verb_writer.invoke).parameters
        )
        self._verb_supports_rework = all(
            name in inspect.signature(verb_writer.invoke).parameters
            for name in ("pass_id", "rework_slices")
        )
        self._owner_snapshot_error: str | None = None
        try:
            if self._store.read_owner_notes_snapshot(self._run_id) is None:
                self._store.write_owner_notes_snapshot(
                    self._run_id, self._owner_notes_snapshot()
                )
        except (OSError, RunStoreRefusal, ValueError):
            self._owner_snapshot_error = "owner_notes_snapshot_invalid"

    # ------------------------------------------------------------------ loop

    def run_until(self) -> TickResult:
        for _ in range(10_000):
            try:
                result = self.tick()
            except RunStoreRefusal as refusal:
                # ``tick`` owns the ordinary boundary. This second guard
                # keeps the loop safe if a substituted/overridden tick leaks
                # the typed refusal, while never retrying a dead stop journal.
                result = self._run_store_refusal_stop(refusal)
            if result.kind != "acted":
                return result
        raise RuntimeError("supervisor exceeded the tick safety cap")

    def memory_preflight(self) -> None:
        """Record the optional hook preflight without gating runner work."""

        if self._memory_hooks is None:
            return
        try:
            self._journal_memory_facts(self._memory_hooks.preflight())
        except Exception:
            self._journal(
                "memory_hook",
                hook="liveness",
                status="refused",
                reason="memory_hook_internal_refusal",
                evidence="",
            )

    def tick(self) -> TickResult:
        self._consumed_read = False
        try:
            return self._tick_and_record()
        except RunStoreRefusal as refusal:
            return self._run_store_refusal_stop(refusal)

    def _tick_and_record(self) -> TickResult:
        result = self._tick_inner()
        if result.kind in ("acted", "boundary"):
            control = self._enforce_run_store()
            if isinstance(control, TickResult):
                result = control
        # A state is consumed only when its bound action completed; stops and
        # refusals re-serve the same state on resume, exactly as a real
        # recomputation from unchanged artifacts would.
        progress = self._tick_advanced(result)
        consumed = (
            self._consumed_read
            and result.kind in ("acted", "boundary")
            and progress
        )
        self._journal(
            "tick",
            result=result.kind,
            detail=result.detail,
            consumed=consumed,
            progress=progress,
        )
        return result

    def _run_store_refusal_stop(
        self, refusal: RunStoreRefusal
    ) -> TickResult:
        if getattr(refusal, _STOP_JOURNAL_ATTEMPTED, False) or getattr(
            refusal, _REFUSAL_ROUTING_ATTEMPTED, False
        ):
            raise refusal
        try:
            return self._stop(
                StopReason.INVALID_STATE,
                f"run-store refusal {refusal.code}: {refusal.message}",
                decision=(
                    f"Inspect run-store refusal '{refusal.code}' "
                    f"({refusal.message}) and its attempt/journal evidence. "
                    "Resume only after the store and project evidence are "
                    "internally consistent."
                ),
            )
        except RunStoreRefusal as routed:
            # One routing attempt is the bound even when escalation storage,
            # rather than append_event itself, is the dead store surface.
            setattr(routed, _REFUSAL_ROUTING_ATTEMPTED, True)
            raise

    def _tick_inner(self) -> TickResult:
        if killswitch.stop_requested(self._store.root):
            return self._stop(
                StopReason.KILL_SWITCH,
                "stop sentinel present",
                decision="Remove the STOP sentinel to allow resumption.",
            )
        owner_change = self._owner_note_change()
        if owner_change is not None:
            return self._stop(
                StopReason.OWNER_NOTE,
                f"owner-note admission snapshot changed: {owner_change}",
                decision=(
                    f"Route owner-note file '{owner_change}' for human review; "
                    "its content was not interpreted."
                ),
            )
        exceeded = self._gate.check_global(self._counters)
        if exceeded is not None:
            reason, meter = exceeded
            return self._stop(reason, f"budget:{meter}")
        control = self._enforce_run_store()
        if isinstance(control, TickResult):
            return control
        if self._counters.consecutive_provider_failures:
            schedule = self._policy.limits.provider_backoff_seconds
            streak = self._counters.consecutive_provider_failures
            delay = schedule[min(streak - 1, len(schedule) - 1)]
            self._sleep(delay)
            self._journal(
                "backoff", failure_streak=streak, seconds=delay
            )
            if killswitch.stop_requested(self._store.root):
                return self._stop(
                    StopReason.KILL_SWITCH,
                    "stop sentinel present during provider backoff",
                    decision="Remove the STOP sentinel to allow resumption.",
                )
            exceeded = self._gate.check_global(self._counters)
            if exceeded is not None:
                reason, meter = exceeded
                return self._stop(reason, f"budget:{meter}")
        if self._max_total_cost_usd is not None:
            maximum = self._max_total_cost_usd
            spent = self._counters.total_cost_usd
            if (maximum > 0.0 and spent >= maximum) or spent > maximum:
                return self._stop(
                    StopReason.BUDGET_EXHAUSTED, "budget:live_gate_total_cost"
                )

        try:
            state = self._plan_provider.read_planning_state()
        except PlanningStateRefusal as refusal:
            self._consumed_read = True
            self._journal("refusal", code=refusal.code)
            return TickResult("refused", refusal.code)
        except MockScriptExhausted:
            self._journal("refusal", code="plan_script_exhausted")
            return TickResult("refused", "plan_script_exhausted")
        except PlanProviderUnavailable as unavailable:
            # A provider transport failure is a refusal before any bound
            # action: no attempt, executor call, verification, or project
            # mutation may follow it in this tick.
            self._journal("refusal", code=unavailable.code)
            return TickResult("refused", unavailable.code)
        self._consumed_read = True

        self._journal(
            "plan_read",
            outcome=state.outcome.value,
            step=state.step.value if state.step else None,
        )

        if state.outcome is PlanOutcome.INVALID:
            return self._stop(
                StopReason.INVALID_STATE, "invalid planning state", state=state
            )
        if state.outcome is PlanOutcome.BLOCKED:
            reason = (
                StopReason.OVERRIDE_REQUIRED
                if state.verdict is not None and state.verdict.value == "override"
                else StopReason.BLOCKED_VERDICT
            )
            blocked = state.blocked
            decision = (
                f"Blocked: cited '{blocked.citation}', owner '{blocked.owner}'."
                if blocked
                else "Blocked without reference."
            )
            return self._stop(reason, "blocked planning state", state=state,
                              decision=decision)
        if state.outcome is PlanOutcome.COMPLETE:
            if self._policy.autonomy.pass_boundary == "two_clean":
                return self._complete_pass(state)
            self._journal("boundary", boundary=self._boundary, cause="complete")
            return TickResult("boundary", "complete")
        if state.outcome is PlanOutcome.NEEDS_SPECIFICATION:
            return self._reconcile(state)

        step = state.step
        slice_id = state.frontier.slice_id if state.frontier else ""
        worklist_stop = self._check_active_worklist(state)
        if worklist_stop is not None:
            return worklist_stop
        if step is LoopStep.NO_FRONTIER:
            return self._stop(
                StopReason.INVALID_STATE,
                "no_frontier is never interpreted",
                state=state,
            )
        if step is LoopStep.FRONTIER_RECORDED:
            if self._frontier_unchanged(slice_id):
                return TickResult("acted", "frontier_unchanged")
            self._queue_memory_updates(slice_id)
            self._journal(
                "slice_complete",
                slice=slice_id,
                milestone=state.frontier.milestone_id if state.frontier else "",
            )
            if self._boundary == "slice_complete":
                self._journal("boundary", boundary=self._boundary, cause="frontier_recorded")
                return TickResult("boundary", "slice_complete")
            if not self._policy.autonomy.auto_continue_past_frontier_recorded:
                return self._stop(
                    StopReason.HUMAN_GATE,
                    "frontier recorded; auto-continue disabled",
                    state=state,
                    slice_id=slice_id,
                )
            self._journal("continue_past_frontier", slice=slice_id)
            return TickResult("acted", "continue_past_frontier")
        if step in _STEP_VERBS:
            if step is LoopStep.MAKE_REVIEW_PROMPT and self._corrective_prompt_pending(
                slice_id, state.artifacts.coding_prompt
            ):
                # Drive-owned corrective round: a governed corrective coding
                # prompt was written and no coder attempt followed it. The
                # producer interprets artifacts only — the stale self-report
                # satisfies its existence checks, so its typed step would
                # send stale work to review. Execution facts are drive's
                # durable journal authority, so the coder runs first and the
                # review prompt follows a fresh observation.
                return self._corrective_coding(state, slice_id)
            if (
                step is LoopStep.RECORD_VERDICT
                and not self._review_fresh_after_last_coding(slice_id)
            ):
                # Same authority for the report side: after a corrective
                # coder collection, a review report written before that
                # collection is stale slice-round evidence, not a recordable
                # verdict. The reviewer re-executes against the fresh round;
                # authored historical reports (no drive coder collection)
                # are untouched and record normally.
                return self._execute_review(state, slice_id)
            return self._invoke_verb(_STEP_VERBS[step], state)
        if step is LoopStep.EXECUTE_CODING_PROMPT:
            return self._execute_coding(state, slice_id)
        if step in (LoopStep.FIX_SELF_REPORT, LoopStep.FIX_REVIEW_REPORT):
            return self._repair(state, slice_id, step)
        if step is LoopStep.EXECUTE_REVIEW_PROMPT:
            if self._corrective_prompt_pending(
                slice_id, state.artifacts.coding_prompt
            ):
                return self._corrective_coding(state, slice_id)
            return self._execute_review(state, slice_id)
        return self._stop(
            StopReason.INVALID_STATE, "unbound planning step", state=state
        )

    # --------------------------------------------------------------- actions

    _DECLARED_FROM_STATE = object()

    def _invoke_verb(
        self,
        verb: str,
        state: PlanningState,
        declared: object = _DECLARED_FROM_STATE,
        *,
        pass_id: str | None = None,
        rework_slices: tuple[str, ...] = (),
    ) -> TickResult:
        slice_id = state.frontier.slice_id if state.frontier else ""
        if slice_id and self._has_any_coder_evidence(slice_id):
            gate = self._ensure_verified_coder_attempt(slice_id)
            if gate is not None:
                return gate
        if declared is self._DECLARED_FROM_STATE:
            declared = getattr(state.artifacts, _VERB_ARTIFACT_FIELDS[verb])
            if (
                verb == "make-coding-prompt"
                and state.step is LoopStep.MAKE_CODING_PROMPT
                and self._has_any_coder_evidence(slice_id)
            ):
                # Released rework planning may route a needs_work chain
                # directly back to make_coding_prompt while retaining the
                # prior prompt as linked context. The typed write step owns a
                # fresh target; the historical prompt path is not that target.
                declared = None
        if verb == "declare-rework" and not self._verb_supports_rework:
            return self._stop(
                StopReason.INVALID_STATE,
                "planning completed before the second-pass worklist",
                state=state,
                slice_id=rework_slices[0] if rework_slices else "",
            )
        try:
            kwargs: dict[str, object] = {}
            if self._verb_supports_context:
                kwargs["slice_id"] = slice_id or None
            if verb == "record-verdict" and self._verb_supports_report:
                kwargs["review_report"] = state.artifacts.review_report
            if verb == "declare-rework":
                kwargs["pass_id"] = pass_id
                kwargs["rework_slices"] = rework_slices
            artifact = self._verb_writer.invoke(verb, declared, **kwargs)
        except FrutlupsCorrectiveRound:
            # Released acceptance semantics: a non-pass report is never
            # recorded. frutlups's own typed recode_same_slice next-action
            # routes one corrective coding-prompt transaction instead (the
            # fresh corrective prompt has no declared path yet); the
            # run-store dispatch count remains the ladder authority for the
            # rounds this creates.
            return self._invoke_verb("make-coding-prompt", state, declared=None)
        except FrutlupsVerbError as failed:
            # A governed verb transaction failure is fail-closed provider
            # state with preserved evidence; nothing is retried in-tick.
            self._journal("refusal", code=failed.code)
            return self._stop(
                StopReason.PROVIDER_FAILURE,
                f"governed verb transaction failed: {failed.code}",
                state=state,
            )
        except VerbScriptExhausted:
            return self._stop(
                StopReason.INVALID_STATE,
                f"orchestrator verb refused: {verb}",
                state=state,
            )
        except VerbAuthorityDenied as denied:
            self._journal(
                "fence",
                attempt="",
                violations=[
                    {"code": v.code, "path": v.path} for v in denied.violations
                ],
            )
            return self._stop(
                StopReason.PATH_VIOLATION,
                f"verb destination refused before mutation: {verb}",
                state=state,
            )
        relative = artifact.relative_to(self._project_root).as_posix()
        journal_fields: dict[str, object] = {
            "verb": verb,
            "artifact": relative,
            "slice": slice_id,
        }
        if verb == "declare-rework":
            journal_fields["pass_id"] = pass_id
            journal_fields["slices"] = list(rework_slices)
        self._journal("verb", **journal_fields)
        mark_journaled = getattr(self._verb_writer, "mark_journaled", None)
        if mark_journaled is not None:
            mark_journaled(verb)
        clear_intent = getattr(self._verb_writer, "clear_intent", None)
        if clear_intent is not None:
            clear_intent()
        return TickResult("acted", f"verb:{verb}")

    def _execute_coding(self, state: PlanningState, slice_id: str) -> TickResult:
        pending = self._pending_coder_attempt(slice_id)
        if pending is not None:
            return self._finish_coder_attempt(pending, slice_id)
        prompt_rel = state.artifacts.coding_prompt
        report_rel = state.artifacts.self_report
        if not prompt_rel or not report_rel:
            return self._stop(
                StopReason.INVALID_STATE,
                "execute_coding_prompt without prompt/self-report references",
                state=state,
                slice_id=slice_id,
            )
        frontier_round = state.frontier.round if state.frontier else 1
        if self._satisfied_coder_attempt(slice_id, frontier_round, prompt_rel):
            return TickResult("acted", "coder_attempt_already_satisfied")

        stop = ladder.check_ladder(
            frontier_round,
            self._counters.lifecycle_coder_collected_for(slice_id),
            self._round4_authority(),
        )
        if stop is not None:
            return self._stop(stop, "ladder cap", state=state, slice_id=slice_id)
        exceeded = self._gate.check_coder_dispatch(self._counters, slice_id)
        if exceeded is not None:
            return self._stop(
                exceeded[0], f"budget:{exceeded[1]}", state=state, slice_id=slice_id
            )

        return self._dispatch_and_collect(
            state,
            slice_id,
            role=Role.CODER,
            access=self._policy.coder.workspace_access,
            prompt_rel=prompt_rel,
            expected_rel=(report_rel,),
            repair=False,
            verify_after=True,
        )

    def _corrective_prompt_pending(
        self, slice_id: str, prompt_rel: str | None
    ) -> bool:
        """True when the current coding prompt is a governed write this run
        performed and no completed coder collection for the slice follows it
        in the journal. Prompts authored outside this run never qualify."""

        if not prompt_rel:
            return False
        prompt_index: int | None = None
        for index, event in enumerate(self._events):
            if (
                event.get("kind") == "verb"
                and event.get("verb") == "make-coding-prompt"
                and event.get("artifact") == prompt_rel
            ):
                prompt_index = index
        if prompt_index is None:
            return False
        return not any(
            event.get("kind") == "collected"
            and event.get("slice") == slice_id
            and event.get("role") == "coder"
            and event.get("status") == "completed"
            for event in self._events[prompt_index:]
        )

    def _review_fresh_after_last_coding(self, slice_id: str) -> bool:
        """True when the newest reviewer collection for the slice follows the
        newest coder collection (or when this run never collected a coder
        attempt for the slice, i.e. purely authored artifact state)."""

        last_coder: int | None = None
        last_reviewer: int | None = None
        for index, event in enumerate(self._events):
            if (
                event.get("kind") == "collected"
                and event.get("slice") == slice_id
                and event.get("status") == "completed"
            ):
                if event.get("role") == "coder":
                    last_coder = index
                elif event.get("role") == "reviewer":
                    last_reviewer = index
        if last_coder is None:
            return True
        return last_reviewer is not None and last_reviewer > last_coder

    def _corrective_coding(self, state: PlanningState, slice_id: str) -> TickResult:
        """Dispatch the coder for an unexecuted corrective coding prompt.

        Same lifecycle as :meth:`_execute_coding` minus the satisfied-attempt
        short-circuit — the corrective round is exactly the case where a
        verified earlier attempt exists and must not suppress the new round.
        The mechanical ladder and budget gates still decide whether another
        round is allowed.
        """

        pending = self._pending_coder_attempt(slice_id)
        if pending is not None:
            return self._finish_coder_attempt(pending, slice_id)
        stop = ladder.check_ladder(
            state.frontier.round if state.frontier else 1,
            self._counters.lifecycle_coder_collected_for(slice_id),
            self._round4_authority(),
        )
        if stop is not None:
            return self._stop(stop, "ladder cap", state=state, slice_id=slice_id)
        exceeded = self._gate.check_coder_dispatch(self._counters, slice_id)
        if exceeded is not None:
            return self._stop(
                exceeded[0], f"budget:{exceeded[1]}", state=state, slice_id=slice_id
            )
        prompt_rel = state.artifacts.coding_prompt
        report_rel = state.artifacts.self_report
        if not prompt_rel or not report_rel:
            return self._stop(
                StopReason.INVALID_STATE,
                "corrective round without prompt/self-report references",
                state=state,
                slice_id=slice_id,
            )
        return self._dispatch_and_collect(
            state,
            slice_id,
            role=Role.CODER,
            access=self._policy.coder.workspace_access,
            prompt_rel=prompt_rel,
            expected_rel=(report_rel,),
            repair=False,
            verify_after=True,
        )

    def _repair(
        self, state: PlanningState, slice_id: str, step: LoopStep
    ) -> TickResult:
        exceeded = self._gate.check_repair_dispatch(self._counters, slice_id)
        if exceeded is not None:
            return self._stop(
                exceeded[0], f"budget:{exceeded[1]}", state=state, slice_id=slice_id
            )
        if step is LoopStep.FIX_SELF_REPORT:
            role, access = Role.CODER, self._policy.coder.workspace_access
            prompt_rel = state.artifacts.coding_prompt
            target_rel = state.artifacts.self_report
        else:
            role, access = Role.REVIEWER, self._policy.reviewer.workspace_access
            prompt_rel = state.artifacts.review_prompt
            target_rel = state.artifacts.review_report
        if not prompt_rel or not target_rel:
            return self._stop(
                StopReason.INVALID_STATE,
                "repair step without prompt/report references",
                state=state,
                slice_id=slice_id,
            )
        diagnostics = "\n".join(
            f"- {d.severity} {d.code}: {d.message}" for d in state.diagnostics
        )
        repair_prompt = (
            self._read_project_file(prompt_rel)
            + "\n\n## Repair Diagnostics (verbatim)\n\n"
            + diagnostics
            + "\n"
        )
        return self._dispatch_and_collect(
            state,
            slice_id,
            role=role,
            access="workspace_write",
            prompt_rel=prompt_rel,
            expected_rel=(target_rel,),
            repair=True,
            verify_after=False,
            repair_prompt=repair_prompt.encode("utf-8"),
        )

    def _execute_review(self, state: PlanningState, slice_id: str) -> TickResult:
        gate = self._ensure_verified_coder_attempt(slice_id)
        if gate is not None:
            return gate
        prompt_rel = state.artifacts.review_prompt
        report_rel = state.artifacts.review_report
        if not prompt_rel or not report_rel:
            return self._stop(
                StopReason.INVALID_STATE,
                "execute_review_prompt without prompt/report references",
                state=state,
                slice_id=slice_id,
            )
        primary = (
            self._completed_primary_review_attempt(
                slice_id, prompt_rel, report_rel
            )
            if self._policy.shadow_reviewer.enabled
            else None
        )
        if primary is None:
            result = self._dispatch_and_collect(
                state,
                slice_id,
                role=Role.REVIEWER,
                access=self._policy.reviewer.workspace_access,
                prompt_rel=prompt_rel,
                expected_rel=(report_rel,),
                repair=False,
                verify_after=False,
            )
            if result.kind != "acted" or result.detail != "reviewer_attempt_completed":
                return result
            primary = self._completed_primary_review_attempt(
                slice_id, prompt_rel, report_rel
            )
            if primary is None:
                return self._stop(
                    StopReason.INVALID_STATE,
                    "completed primary review has no durable attempt",
                    state=state,
                    slice_id=slice_id,
                )
        self._capture_shadow_review(slice_id, prompt_rel, primary)
        return TickResult("acted", "reviewer_attempt_completed")

    def _completed_primary_review_attempt(
        self, slice_id: str, prompt_rel: str, report_rel: str
    ) -> Path | None:
        prompt = self._project_root / prompt_rel
        if not prompt.is_file():
            return None
        prompt_sha256 = hashlib.sha256(prompt.read_bytes()).hexdigest()
        last_coder_event = max(
            (
                index
                for index, event in enumerate(self._events)
                if event.get("kind") == "collected"
                and event.get("slice") == slice_id
                and event.get("role") == "coder"
            ),
            default=-1,
        )
        for attempt in reversed(self._store.list_attempts(self._run_id, slice_id)):
            request = self._store.read_request(attempt) or {}
            result = self._store.read_result(attempt) or {}
            if (
                request.get("role") != "reviewer"
                or not self._request_matches_prompt_source(
                    slice_id, attempt.name, request, prompt_sha256
                )
                or request.get("expected_artifacts") != [report_rel]
                or result.get("status") != "completed"
                or self._store.read_transition(attempt) != "closed"
            ):
                continue
            collected_index = max(
                (
                    index
                    for index, event in enumerate(self._events)
                    if event.get("kind") == "collected"
                    and event.get("slice") == slice_id
                    and event.get("attempt") == attempt.name
                    and event.get("role") == "reviewer"
                ),
                default=-1,
            )
            if collected_index > last_coder_event:
                return attempt
        return None

    def _capture_shadow_review(
        self, slice_id: str, prompt_rel: str, primary: Path
    ) -> None:
        if not self._policy.shadow_reviewer.enabled:
            return
        if any(
            event.get("kind") == "shadow_review"
            and event.get("slice") == slice_id
            and event.get("primary_attempt") == primary.name
            for event in self._events
        ):
            return
        attempts = self._store.list_shadow_attempts(self._run_id, slice_id)
        if len(attempts) >= self._policy.limits.max_shadow_attempts_per_slice:
            return
        seat = shadow_policy_seat(self._policy)
        if seat.adapter in EXTERNAL_ADAPTERS:
            return

        prompt_bytes = (self._project_root / prompt_rel).read_bytes()
        shadow_prompt = (
            b"# Shadow Review (Evidence Only)\n\n"
            b"Review the same slice evidence as the primary reviewer. Your "
            b"output is captured only and cannot affect acceptance, routing, "
            b"budgets, planning, or closure. Produce shadow_report.md in the "
            b"assigned workspace.\n\n"
            + prompt_bytes
        )
        attempt = self._store.create_shadow_attempt(self._run_id, slice_id)
        prompt_path = self._store.write_attempt_prompt(
            attempt, "shadow_prompt.md", shadow_prompt
        )
        prompt_path = self._prompt_with_memory(attempt, prompt_path)
        staging = attempt / "staged_output"
        staging.mkdir()
        lease = WorkspaceLease(staging, None, False)
        request = self._build_request(
            attempt,
            Role.REVIEWER,
            "read_only",
            prompt_path,
            (_SHADOW_REVIEW,),
            lease,
            seat=seat,
            effort="",
        )
        self._store.write_request(attempt, request)
        dispatched_at = self._clock.now()
        self._advance_transition(attempt, "started")
        status = "failed"
        result: AgentRunResult | None = None
        try:
            result = self._executors["shadow_reviewer"].execute(request)
            status = result.status
            log_path = Path(result.event_log_path)
            if log_path.is_file():
                self._store.write_provider_events(attempt, log_path.read_bytes())
            self._store.write_result(attempt, result)
            self._advance_transition(attempt, "collected")
            if result.status == "completed":
                output = staging / _SHADOW_REVIEW
                try:
                    entries = tuple(staging.iterdir())
                    with open(output, "rb") as stream:
                        report = stream.read(
                            self._policy.limits.max_run_store_bytes + 1
                        )
                    if (
                        len(entries) != 1
                        or entries[0] != output
                        or output.is_symlink()
                        or not output.is_file()
                        or len(report) > self._policy.limits.max_run_store_bytes
                    ):
                        raise OSError
                    self._store.publish_shadow_report(attempt, report)
                except (OSError, RunStoreRefusal):
                    status = "capture_refused"
        except WorkspaceAuthorityDenied:
            status = "authority_refused"
        except CostAuthorizationExceeded:
            status = "cost_refused"
        except Exception:
            status = "executor_error"
        completed_at = self._clock.now()
        self._journal(
            "shadow_review",
            attempt=attempt.name,
            primary_attempt=primary.name,
            slice=slice_id,
            role="shadow_reviewer",
            adapter=seat.adapter,
            model=seat.model,
            effort=request.effort,
            dispatched=True,
            dispatched_at=dispatched_at,
            completed_at=completed_at,
            status=status,
            tokens_in=result.tokens_in if result is not None else None,
            tokens_out=result.tokens_out if result is not None else None,
            cost_usd=result.cost_usd if result is not None else None,
        )
        self._advance_transition(attempt, "validated")
        self._advance_transition(attempt, "closed")

    def _reconcile(self, state: PlanningState) -> TickResult:
        try:
            slice_id = self._reconciliation_writer.target_slice_id()
        except ReconciliationRefusal as refusal:
            return self._stop(
                StopReason.PATH_VIOLATION,
                f"reconciliation target refused: {refusal.code}",
                state=state,
            )
        exceeded = self._gate.check_reconciliation(self._counters, slice_id)
        if exceeded is not None:
            return self._stop(
                exceeded[0], f"budget:{exceeded[1]}", state=state, slice_id=slice_id
            )
        diagnostics = "\n".join(
            f"- {d.severity} {d.code}: {d.message}" for d in state.diagnostics
        )
        try:
            roadmap = self._reconciliation_writer.target_path().read_bytes().decode(
                "utf-8"
            )
        except (OSError, UnicodeDecodeError, ReconciliationRefusal):
            return self._stop(
                StopReason.PATH_VIOLATION,
                "reconciliation target became unreadable",
                state=state,
                slice_id=slice_id,
            )
        base_prompt = (
            "# Architect Reconciliation Turn\n\nPlanning state requires "
            "specification. Produce exactly one complete proposed active "
            "roadmap as roadmap_proposal.md in the assigned staging "
            "workspace. Stay inside the assigned staging workspace; create "
            "only roadmap_proposal.md and do not read or write any other "
            "path. Preserve the roadmap prefix, the Ruled Out region, every "
            "milestone heading and status, every completed milestone, and "
            "the top-level Slices field. The current roadmap structure is "
            "authoritative even where it appears anomalous. Complete the one "
            "existing untitled slice line at its exact current location. "
            "Reproduce every other line byte-identically, including empty or "
            "unusually placed fields. Never relocate, normalize, or repair "
            "structure; a proposal that fixes layout will be refused. In the "
            "one active milestone, edit "
            "only specification text within Implementation package, "
            "Objective, Expected artifacts, Active workspaces, Non-goals, "
            "Verification/evidence, Review strictness, Likely coding prompt, "
            "Done when, or Opening gates. Complete the one existing untitled "
            "slice line in place; do not add, remove, reorder, or rename "
            "slice or milestone identities. Avoid governance-history terms "
            "in changed specification lines. Emit UTF-8 text with LF line "
            "endings and one final newline.\n\n## Diagnostics (verbatim)\n\n"
            + diagnostics
            + "\n\n## Current Active Roadmap (verbatim)\n\n"
            + roadmap
        ).encode("utf-8")
        prompt = base_prompt
        for dispatch_index in range(2):
            staged = self._dispatch_staged_output(
                state,
                slice_id,
                role=Role.ARCHITECT,
                prompt=prompt,
                prompt_name="reconciliation_prompt.md",
                output_name=_RECONCILIATION_PROPOSAL,
            )
            if isinstance(staged, TickResult):
                return staged
            attempt, proposal = staged
            try:
                applied = self._reconciliation_writer.apply(proposal)
            except ReconciliationRefusal as refusal:
                self._journal(
                    "fence",
                    attempt=attempt.name,
                    violations=[
                        {"code": refusal.code, "path": "active_roadmap"}
                    ],
                )
                if refusal.code == "proposal_has_no_change":
                    self._journal(
                        "reconciliation",
                        attempt=attempt.name,
                        slice=slice_id,
                        progress=False,
                    )
                    self._advance_transition(attempt, "closed")
                    return self._stop(
                        StopReason.NO_PROGRESS,
                        f"reconciliation proposal refused: {refusal.code}",
                        state=state,
                        slice_id=slice_id,
                        attempt_id=attempt.name,
                    )
                self._advance_transition(attempt, "closed")
                headroom = self._gate.check_reconciliation(
                    self._counters, slice_id
                )
                if dispatch_index == 0 and headroom is None:
                    feedback = (
                        "\n## Guard Refusal Feedback\n\n"
                        f"The previous proposal was refused by the N04 writer "
                        f"with guard code `{refusal.code}`. Retry once under "
                        "the unchanged guard contract.\n\n"
                        "- Editable fields: Implementation package, Objective, "
                        "Expected artifacts, Active workspaces, Non-goals, "
                        "Verification/evidence, Review strictness, Likely "
                        "coding prompt, Done when, and Opening gates.\n"
                        "- Protected regions: the roadmap prefix, the Ruled Out "
                        "region, every completed milestone, and every field "
                        "outside that editable set must remain byte-identical.\n"
                        "- In-place rule: complete the existing untitled slice "
                        "line at its exact location; do not add, remove, "
                        "relocate, reorder, rename, normalize, or repair "
                        "roadmap structure.\n"
                    ).encode("utf-8")
                    prompt = base_prompt + feedback
                    continue
                return self._stop(
                    StopReason.PATH_VIOLATION,
                    f"reconciliation proposal refused: {refusal.code}",
                    state=state,
                    slice_id=slice_id,
                    attempt_id=attempt.name,
                )
            self._journal(
                "plan_consumed", action="reconciliation"
            )
            self._journal(
                "reconciliation",
                attempt=attempt.name,
                slice=applied.slice_id,
                target=applied.target,
                before_sha256=applied.before_sha256,
                after_sha256=applied.after_sha256,
                progress=True,
            )
            self._advance_transition(attempt, "validated")
            self._advance_transition(attempt, "closed")
            return self._reconciliation_post_read(
                state, slice_id, attempt.name
            )
        raise AssertionError("bounded reconciliation retry loop exhausted")

    def _dispatch_staged_output(
        self,
        state: PlanningState,
        slice_id: str,
        *,
        role: Role,
        prompt: bytes,
        prompt_name: str,
        output_name: str,
    ) -> tuple[Path, Path] | TickResult:
        seat = policy_seat(self._policy, role)
        if seat.adapter in EXTERNAL_ADAPTERS:
            if self._external_dispatch_guard is None:
                self._journal("refusal", code="live_authority_missing")
                return TickResult("refused", "live_authority_missing")
            try:
                self._external_dispatch_guard()
            except Exception:
                self._journal("refusal", code="live_authority_changed")
                return TickResult("refused", "live_authority_changed")
        attempt = self._store.create_attempt(self._run_id, slice_id)
        prompt_path = self._store.write_attempt_prompt(
            attempt, prompt_name, prompt
        )
        prompt_path = self._prompt_with_memory(attempt, prompt_path)
        staging = attempt / "staged_output"
        staging.mkdir()
        lease = WorkspaceLease(staging, None, False)
        request = self._build_request(
            attempt,
            role,
            "workspace_write",
            prompt_path,
            (output_name,),
            lease,
            effort=self._effort_for_dispatch(
                role,
                slice_id,
                repair=False,
                frontier_round=state.frontier.round if state.frontier else 1,
            ),
        )
        self._store.write_request(attempt, request)
        self._journal(
            "dispatch",
            role=role.value,
            slice=slice_id,
            attempt=attempt.name,
            repair=False,
            adapter=request.adapter,
            model=request.model,
            effort=request.effort,
        )
        self._advance_transition(attempt, "started")
        try:
            result = self._execute(request, attempt)
        except WorkspaceAuthorityDenied as denied:
            return self._authority_stop(denied, state, slice_id, attempt)
        except CostAuthorizationExceeded:
            self._advance_transition(attempt, "closed")
            return self._stop(
                StopReason.BUDGET_EXHAUSTED,
                "budget:cost_authorization",
                state=state,
                slice_id=slice_id,
                attempt_id=attempt.name,
            )
        if result is None:
            return TickResult("acted", "provider_failure")
        self._collect(attempt, result, role.value, slice_id)
        if result.status != "completed":
            self._advance_transition(attempt, "closed")
            return TickResult("acted", f"attempt_{result.status}")
        watch = self._watcher.wait_for(
            [staging / output_name],
            self._watch_timeout,
            poll_seconds=self._policy.limits.watch_poll_seconds,
            stop_requested=lambda: killswitch.stop_requested(self._store.root),
        )
        if not watch.ok:
            self._advance_transition(attempt, "closed")
            if watch.stop_requested:
                return self._stop(
                    StopReason.KILL_SWITCH,
                    "stop sentinel observed during staged artifact watch",
                    state=state,
                    slice_id=slice_id,
                    attempt_id=attempt.name,
                )
            self._journal("watch_timeout", attempt=attempt.name)
            return TickResult("acted", "watch_timeout")
        output = staging / output_name
        try:
            entries = tuple(staging.iterdir())
        except OSError:
            entries = ()
        if (
            len(entries) != 1
            or entries[0] != output
            or output.is_symlink()
            or not output.is_file()
        ):
            self._advance_transition(attempt, "closed")
            return self._stop(
                StopReason.PATH_VIOLATION,
                "staged output shape violated its single-file authority",
                state=state,
                slice_id=slice_id,
                attempt_id=attempt.name,
            )
        digest = _bounded_file_sha256(
            output, self._policy.limits.max_run_store_bytes
        )
        self._store.write_diff_manifest(
            attempt,
            {
                "changes": [
                    {"path": output_name, "kind": "created", "sha256": digest}
                ]
            },
        )
        return attempt, output

    def _reconciliation_post_read(
        self, prior: PlanningState, slice_id: str, attempt_id: str
    ) -> TickResult:
        try:
            state = self._plan_provider.read_planning_state()
        except PlanningStateRefusal as refusal:
            self._journal("refusal", code=refusal.code)
            return self._stop(
                StopReason.INVALID_STATE,
                f"post-reconciliation state refused: {refusal.code}",
                state=prior,
                slice_id=slice_id,
                attempt_id=attempt_id,
            )
        except (MockScriptExhausted, PlanProviderUnavailable):
            return self._stop(
                StopReason.PROVIDER_FAILURE,
                "post-reconciliation planning state unavailable",
                state=prior,
                slice_id=slice_id,
                attempt_id=attempt_id,
            )
        self._journal(
            "plan_read",
            outcome=state.outcome.value,
            step=state.step.value if state.step else None,
            context="reconciliation",
        )
        if state.outcome in (PlanOutcome.READY, PlanOutcome.COMPLETE):
            return TickResult("acted", "reconciliation")
        if state.outcome is PlanOutcome.BLOCKED:
            return self._stop(
                StopReason.BLOCKED_VERDICT,
                "post-reconciliation planning state is blocked",
                state=state,
                slice_id=slice_id,
                attempt_id=attempt_id,
            )
        reason = (
            StopReason.NO_PROGRESS
            if state.outcome is PlanOutcome.NEEDS_SPECIFICATION
            else StopReason.INVALID_STATE
        )
        return self._stop(
            reason,
            "post-reconciliation planning state is not coherent",
            state=state,
            slice_id=slice_id,
            attempt_id=attempt_id,
        )

    def _complete_pass(self, state: PlanningState) -> TickResult:
        missing = self._missing_worklist_slices()
        if missing:
            active = self._active_worklist()
            if active is None:
                return self._stop(
                    StopReason.INVALID_STATE,
                    "the second-pass worklist identity is unavailable",
                    state=state,
                    slice_id=missing[0],
                )
            _, pass_number, _ = active
            return self._invoke_verb(
                "declare-rework",
                state,
                declared=None,
                pass_id=f"holistic_pass_{pass_number:03d}",
                rework_slices=missing,
            )
        frozen = self._ensure_pass_boundary(state)
        if isinstance(frozen, TickResult):
            return frozen
        if frozen:
            return TickResult("acted", "pass_boundary")
        return self._run_holistic_review(state)

    def _ensure_pass_boundary(self, state: PlanningState) -> bool | TickResult:
        boundary_events = [
            event for event in self._events if event.get("kind") == "pass_boundary"
        ]
        try:
            record = self._store.read_pass_boundary(self._run_id)
        except (OSError, UnicodeDecodeError, ValueError, RunStoreRefusal):
            return self._stop(
                StopReason.INVALID_STATE,
                "pass-boundary evidence is unreadable",
                state=state,
            )
        created = record is None
        if record is None:
            try:
                evidence = _tree_inventory(
                    self._store.run_dir(self._run_id),
                    excluded=frozenset(
                        {"pass_boundary.json", "pass_oracle.json"}
                    ),
                    max_members=_MAX_BOUNDARY_MEMBERS,
                    max_file_bytes=self._policy.limits.max_run_store_bytes,
                )
                artifacts = tuple(
                    {"path": path, "sha256": digest}
                    for path, digest in sorted(
                        self._workspace.snapshot(self._project_root).items()
                    )
                )
                if len(artifacts) > _MAX_BOUNDARY_MEMBERS:
                    raise ValueError("artifact inventory exceeds its bound")
                record = {
                    "contract_version": 1,
                    "run_id": self._run_id,
                    "evidence": list(evidence),
                    "artifacts": list(artifacts),
                }
                path = self._store.write_pass_boundary(self._run_id, record)
            except (OSError, RunStoreRefusal, ValueError):
                return self._stop(
                    StopReason.RUN_STORE_FULL,
                    "pass-boundary evidence could not be frozen safely",
                    state=state,
                )
        else:
            path = self._store.run_dir(self._run_id) / "pass_boundary.json"
        if not _valid_pass_boundary_record(record, self._run_id):
            return self._stop(
                StopReason.INVALID_STATE,
                "pass-boundary evidence record is malformed",
                state=state,
            )
        try:
            digest = _bounded_file_sha256(
                path, self._policy.limits.max_run_store_bytes
            )
        except (OSError, ValueError):
            return self._stop(
                StopReason.INVALID_STATE,
                "pass-boundary evidence is missing or over-bound",
                state=state,
            )
        if len(boundary_events) > 1 or (
            boundary_events
            and boundary_events[0].get("evidence_sha256") != digest
        ):
            return self._stop(
                StopReason.INVALID_STATE,
                "pass-boundary evidence does not match its durable fact",
                state=state,
            )
        oracle_path = self._store.run_dir(self._run_id) / "pass_oracle.json"
        try:
            oracle_record = self._store.read_pass_oracle(self._run_id)
        except RunStoreRefusal:
            return self._stop(
                StopReason.INVALID_STATE,
                "pass oracle is unreadable or over-bound",
                state=state,
            )
        if oracle_record is None:
            if boundary_events:
                return self._stop(
                    StopReason.INVALID_STATE,
                    "pass oracle is missing after the durable pass boundary",
                    state=state,
                )
            try:
                oracle_record = reconcile_pass_boundary(
                    record,
                    self._project_root,
                    self._store.run_dir(self._run_id),
                    index_mode=self._policy.index_mode,
                )
                oracle_path = self._store.write_pass_oracle(
                    self._run_id, oracle_record
                )
            except OracleRefusal:
                return self._stop(
                    StopReason.INVALID_STATE,
                    "pass oracle could not be computed from readable frozen inputs",
                    state=state,
                )
            except (OSError, RunStoreRefusal, ValueError):
                return self._stop(
                    StopReason.RUN_STORE_FULL,
                    "pass oracle could not be persisted safely",
                    state=state,
                )
        if not valid_oracle_bundle(
            oracle_record,
            self._run_id,
            digest,
            self._policy.index_mode,
        ):
            return self._stop(
                StopReason.INVALID_STATE,
                "pass oracle identity or schema is malformed",
                state=state,
            )
        try:
            oracle_digest = _bounded_file_sha256(
                oracle_path, self._policy.limits.max_run_store_bytes
            )
        except (OSError, ValueError):
            return self._stop(
                StopReason.INVALID_STATE,
                "pass oracle is missing or over-bound",
                state=state,
            )
        oracle_events = [
            event for event in self._events if event.get("kind") == "pass_oracle"
        ]
        if len(oracle_events) > 1 or (
            oracle_events
            and (
                oracle_events[0].get("oracle_sha256") != oracle_digest
                or oracle_events[0].get("pass_boundary_sha256") != digest
                or oracle_events[0].get("artifact") != "pass_oracle.json"
                or oracle_events[0].get("contract_version") != 1
                or oracle_events[0].get("run_id") != self._run_id
                or oracle_events[0].get("observations")
                != len(oracle_record["observations"])
            )
        ):
            return self._stop(
                StopReason.INVALID_STATE,
                "pass oracle does not match its durable fact",
                state=state,
            )
        if not boundary_events:
            controls = sum(
                1
                for event in self._events
                if event.get("kind") == "plan_consumed"
                and event.get("action") == "pass_boundary"
            )
            if controls == 0:
                self._journal("plan_consumed", action="pass_boundary")
            self._journal(
                "pass_boundary",
                evidence_sha256=digest,
                evidence_members=len(record.get("evidence", ())),
                artifact_members=len(record.get("artifacts", ())),
            )
        if not oracle_events:
            self._journal(
                "pass_oracle",
                artifact="pass_oracle.json",
                contract_version=1,
                run_id=self._run_id,
                pass_boundary_sha256=digest,
                oracle_sha256=oracle_digest,
                observations=len(oracle_record["observations"]),
            )
        return created

    def _run_holistic_review(self, state: PlanningState) -> TickResult:
        if (
            self._policy.reviewer.adapter != "mock"
            and self._policy.reviewer.adapter not in EXTERNAL_ADAPTERS
        ):
            self._journal("refusal", code="holistic_reviewer_not_mock")
            return TickResult("refused", "holistic_reviewer_not_mock")
        pass_number = 1 + sum(
            1 for event in self._events if event.get("kind") == "holistic_review"
        )
        if pass_number > self._policy.target.max_passes:
            return self._stop(
                StopReason.BUDGET_EXHAUSTED,
                "budget:passes",
                state=state,
            )
        record = self._store.read_pass_review(self._run_id, pass_number)
        attempt: Path | None = None
        if record is None:
            boundary_path = self._store.run_dir(self._run_id) / "pass_boundary.json"
            oracle_path = self._store.run_dir(self._run_id) / "pass_oracle.json"
            boundary_hash = _bounded_file_sha256(
                boundary_path, self._policy.limits.max_run_store_bytes
            )
            oracle_hash = _bounded_file_sha256(
                oracle_path, self._policy.limits.max_run_store_bytes
            )
            try:
                boundary_path.read_text(encoding="utf-8")
                oracle_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return self._stop(
                    StopReason.INVALID_STATE,
                    "frozen pass-boundary evidence or oracle is unreadable",
                    state=state,
                )
            # R2-F2: reference the frozen manifest by location and hash
            # instead of embedding it verbatim — embedded manifests scale
            # with the roadmap and broke argv-only reviewer surfaces at 13
            # slices. The relative coordinate from any attempt staging
            # workspace to the run directory is a structural constant of
            # the store layout, keeping prompt bytes deterministic across
            # hosts and roots; the hash keeps the reviewed bytes exact.
            if self._policy.index_mode == "no-ledger":
                index_protocol = (
                    "Declared reviews INDEX mode: no-ledger. The ledger is "
                    "kept by nobody and the frozen manifest is routing truth; "
                    "unindexed-artifact complaints are absent by contract, "
                    "and any ledger_row_in_no_ledger_project observation is "
                    "high-priority. "
                )
            else:
                index_protocol = (
                    "Declared reviews INDEX mode: human-ledger. The INDEX is "
                    "an authored ledger and both index-to-manifest and "
                    "manifest-to-index checks remain live. "
                )
            prompt = (
                "# Holistic Pass Review\n\nReview the frozen first-pass "
                "evidence manifest: the file named pass_boundary.json in "
                "this run's store directory, reachable from your working "
                "directory (the attempt staging workspace inside the run "
                "store) at ../../../../pass_boundary.json. Its SHA-256 is "
                + boundary_hash
                + ". Read that manifest file first; it inventories every "
                "artifact and evidence member of the completed first pass. "
                "Verify the reviewed bytes match the hash before trusting "
                "them. Then read the reconciliation bundle named "
                "pass_oracle.json at ../../../../pass_oracle.json. Its "
                "SHA-256 is "
                + oracle_hash
                + ". Verify the bundle bytes match that hash before trusting "
                "them. "
                + index_protocol
                + "An observation annotation is a pointer requiring "
                "confirmation against primary sources, never a pre-judgment. "
                "For each oracle observation, confirm or refute it "
                "against the primary sources and list the implicated slice "
                "only when the observation is confirmed. Oracle observations "
                "are evidence, not findings or worklist authority. "
                "holistic_review.json remains the sole worklist authority. After "
                "checking every observation, attack beyond the bundle with "
                "spot-checks of judgment claims that mechanical reconciliation "
                "cannot judge. Produce exactly holistic_review.json with one JSON "
                "object whose findings member is a list of unique slice "
                "identifiers. Use an empty list for a clean pass. Write "
                "only into the assigned staging workspace; create only "
                "holistic_review.json. Reading the manifest, the bundle, and the "
                "project's reviewed artifacts is expected; write nothing "
                "outside the project.\n"
            ).encode("utf-8")
            staged = self._dispatch_staged_output(
                state,
                f"holistic_pass_{pass_number:03d}",
                role=Role.REVIEWER,
                prompt=prompt,
                prompt_name="holistic_prompt.md",
                output_name=_HOLISTIC_REVIEW,
            )
            if isinstance(staged, TickResult):
                return staged
            attempt, output = staged
            try:
                with open(output, "rb") as stream:
                    data = stream.read(64 * 1024 + 1)
                if len(data) > 64 * 1024:
                    raise ValueError("review output exceeds its bound")
                record = json.loads(data.decode("utf-8"))
                findings = _checked_findings(
                    record, self._policy.target.max_slices
                )
                record = {
                    "contract_version": 1,
                    "pass_number": pass_number,
                    "findings": findings,
                }
                self._store.write_pass_review(self._run_id, pass_number, record)
            except (OSError, UnicodeDecodeError, ValueError, RunStoreRefusal):
                self._advance_transition(attempt, "closed")
                return self._stop(
                    StopReason.INVALID_STATE,
                    "holistic review output is not a bounded slice worklist",
                    state=state,
                    attempt_id=attempt.name,
                )
        try:
            findings = _checked_findings(record, self._policy.target.max_slices)
            if (
                not isinstance(record, dict)
                or record.get("contract_version") != 1
                or record.get("pass_number") != pass_number
            ):
                raise ValueError("stored holistic review identity is invalid")
        except ValueError:
            return self._stop(
                StopReason.INVALID_STATE,
                "stored holistic review is not a bounded slice worklist",
                state=state,
            )
        clean = not findings
        self._journal(
            "holistic_review",
            pass_number=pass_number,
            findings=findings,
            clean=clean,
            attempt=attempt.name if attempt is not None else "",
            slice=f"holistic_pass_{pass_number:03d}",
        )
        if attempt is not None:
            self._advance_transition(attempt, "validated")
            self._advance_transition(attempt, "closed")
        if findings:
            return TickResult("acted", "second_pass_worklist")
        consecutive = 0
        for event in reversed(self._events):
            if event.get("kind") != "holistic_review":
                continue
            if event.get("clean") is True:
                consecutive += 1
            else:
                break
        if consecutive >= 2:
            # R7-F1: single-slice and final-slice completions never pass the
            # frontier-recorded step, so the completion boundary is a
            # submission moment of its own. The stable boundary identifier
            # keeps the write-once queue evidence idempotent across resume.
            self._queue_memory_updates("roadmap_complete")
            self._journal("boundary", boundary=self._boundary, cause="two_clean")
            return TickResult("boundary", "complete")
        return TickResult("acted", "clean_pass")

    def _active_worklist(self) -> tuple[int, int, tuple[str, ...]] | None:
        for index in range(len(self._events) - 1, -1, -1):
            event = self._events[index]
            if event.get("kind") != "holistic_review":
                continue
            findings = event.get("findings")
            pass_number = event.get("pass_number")
            if (
                isinstance(findings, list)
                and findings
                and type(pass_number) is int
                and pass_number > 0
            ):
                return index, pass_number, tuple(str(item) for item in findings)
            return None
        return None

    def _missing_worklist_slices(self) -> tuple[str, ...]:
        active = self._active_worklist()
        if active is None:
            return ()
        index, _, findings = active
        completed = {
            str(event.get("slice"))
            for event in self._events[index + 1 :]
            if event.get("kind") == "slice_complete"
            or (
                event.get("kind") == "verb"
                and event.get("verb") == "record-verdict"
                and isinstance(event.get("slice"), str)
                and bool(event.get("slice"))
            )
        }
        return tuple(item for item in findings if item not in completed)

    def _check_active_worklist(self, state: PlanningState) -> TickResult | None:
        active = self._active_worklist()
        if active is None:
            return None
        _, _, findings = active
        slice_id = state.frontier.slice_id if state.frontier else ""
        if slice_id not in findings:
            return self._stop(
                StopReason.INVALID_STATE,
                "planning frontier is outside the active second-pass worklist",
                state=state,
                slice_id=slice_id,
            )
        return None

    def _owner_notes_snapshot(self) -> dict[str, object]:
        directory = self._project_root / _OWNER_NOTES_RELATIVE
        if not directory.exists():
            files: dict[str, str] = {}
        else:
            if directory.is_symlink() or _is_junction(directory) or not directory.is_dir():
                raise ValueError("owner notes directory is not regular")
            entries = tuple(sorted(directory.iterdir(), key=lambda item: item.name))
            if len(entries) > _MAX_OWNER_NOTES:
                raise ValueError("owner notes directory exceeds its bound")
            files = {}
            for entry in entries:
                if (
                    entry.is_symlink()
                    or _is_junction(entry)
                    or not entry.is_file()
                    or not entry.name
                    or any(ord(char) < 0x20 for char in entry.name)
                ):
                    raise ValueError("owner note entry is not a regular file")
                files[entry.name] = _bounded_file_sha256(
                    entry, self._policy.limits.max_run_store_bytes
                )
        return {"contract_version": 1, "files": files}

    def _owner_note_change(self) -> str | None:
        if self._owner_snapshot_error is not None:
            return self._owner_snapshot_error
        try:
            admission = self._store.read_owner_notes_snapshot(self._run_id)
            current = self._owner_notes_snapshot()
        except (OSError, RunStoreRefusal, ValueError):
            return "owner_notes_invalid"
        if (
            not isinstance(admission, dict)
            or admission.get("contract_version") != 1
            or not isinstance(admission.get("files"), dict)
        ):
            return "owner_notes_snapshot_invalid"
        before = admission["files"]
        after = current["files"]
        if not all(_sha256_text(value) for value in before.values()):
            return "owner_notes_snapshot_invalid"
        changed = sorted(
            name for name in set(before) | set(after) if before.get(name) != after.get(name)
        )
        return changed[0] if changed else None

    # ------------------------------------------------------------ dispatches

    def _dispatch_and_collect(
        self,
        state: PlanningState,
        slice_id: str,
        *,
        role: Role,
        access: str,
        prompt_rel: str,
        expected_rel: tuple[str, ...],
        repair: bool,
        verify_after: bool,
        repair_prompt: bytes | None = None,
    ) -> TickResult:
        seat = policy_seat(self._policy, role)
        if seat.adapter in EXTERNAL_ADAPTERS:
            if self._external_dispatch_guard is None:
                self._journal("refusal", code="live_authority_missing")
                return TickResult("refused", "live_authority_missing")
            try:
                self._external_dispatch_guard()
            except Exception:
                self._journal("refusal", code="live_authority_changed")
                return TickResult("refused", "live_authority_changed")
        lease = self._workspace.lease(
            self._run_id, slice_id, self._policy.git.worktree_per_slice
        )
        before = self._workspace.snapshot(lease.root)
        attempt = self._store.create_attempt(self._run_id, slice_id)
        rework_snapshot: dict | None = None
        rework_envelope = b""

        prompt_source_sha256: str | None = None
        if repair_prompt is not None:
            prompt_path = self._store.write_attempt_prompt(
                attempt, "repair_prompt.md", repair_prompt
            )
        else:
            prompt_path = self._project_root / prompt_rel
            if not prompt_path.is_file():
                self._advance_transition(attempt, "closed")
                return self._stop(
                    StopReason.INVALID_STATE,
                    "referenced prompt artifact does not exist",
                    state=state,
                    slice_id=slice_id,
                    attempt_id=attempt.name,
                )
            prompt_source_sha256 = hashlib.sha256(
                prompt_path.read_bytes()
            ).hexdigest()
        if role is Role.CODER and not repair and self._declared_rework(slice_id):
            prepared = self._prepare_rework_turn(
                state,
                slice_id,
                attempt,
                lease,
                expected_rel,
            )
            if isinstance(prepared, TickResult):
                return prepared
            rework_snapshot, rework_envelope = prepared
        prompt_path = self._prompt_with_memory(
            attempt, prompt_path, envelope=rework_envelope
        )
        request = self._build_request(
            attempt,
            role,
            access,
            prompt_path,
            expected_rel,
            lease,
            effort=self._effort_for_dispatch(
                role,
                slice_id,
                repair=repair,
                frontier_round=state.frontier.round if state.frontier else 1,
            ),
        )
        self._store.write_request(attempt, request)
        dispatch_fields: dict[str, object] = {
            "role": role.value,
            "slice": slice_id,
            "attempt": attempt.name,
            "repair": repair,
            "adapter": request.adapter,
            "model": request.model,
            "effort": request.effort,
        }
        if prompt_source_sha256 is not None:
            dispatch_fields["prompt_source"] = prompt_rel
            dispatch_fields["prompt_source_sha256"] = prompt_source_sha256
        self._journal("dispatch", **dispatch_fields)
        self._advance_transition(attempt, "started")

        try:
            result = self._execute(request, attempt)
        except WorkspaceAuthorityDenied as denied:
            return self._authority_stop(denied, state, slice_id, attempt)
        except CostAuthorizationExceeded:
            self._advance_transition(attempt, "closed")
            return self._stop(
                StopReason.BUDGET_EXHAUSTED,
                "budget:cost_authorization",
                state=state,
                slice_id=slice_id,
                attempt_id=attempt.name,
            )
        if result is None:
            return TickResult("acted", "provider_failure")

        if result.status != "completed":
            self._collect(attempt, result, role.value, slice_id)
            violations = self._rework_snapshot_violations(
                lease.root, rework_snapshot
            )
            if violations:
                return self._rework_path_stop(
                    state, slice_id, attempt, violations
                )
            self._advance_transition(attempt, "closed")
            return TickResult("acted", f"attempt_{result.status}")

        # Transport completion is a durable collection boundary even when
        # the expected artifact never appears.  Journal the adapter's bounded
        # usage/cost fact before the watch so a timeout cannot hide spend.
        self._collect(attempt, result, role.value, slice_id)
        watch = self._watcher.wait_for(
            [lease.root / rel for rel in expected_rel],
            self._watch_timeout,
            poll_seconds=self._policy.limits.watch_poll_seconds,
            stop_requested=lambda: killswitch.stop_requested(self._store.root),
            protected_changed=(
                lambda: bool(
                    self._rework_snapshot_violations(
                        lease.root, rework_snapshot
                    )
                )
                if rework_snapshot is not None
                else None
            ),
        )
        if not watch.ok:
            if watch.stop_requested:
                self._advance_transition(attempt, "closed")
                return self._stop(
                    StopReason.KILL_SWITCH,
                    "stop sentinel observed during artifact watch",
                    state=state,
                    slice_id=slice_id,
                    attempt_id=attempt.name,
                    decision="Remove the STOP sentinel to allow resumption.",
                )
            if watch.protected_change:
                violations = self._rework_snapshot_violations(
                    lease.root, rework_snapshot
                )
                return self._rework_path_stop(
                    state, slice_id, attempt, violations
                )
            self._journal("watch_timeout", attempt=attempt.name)
            self._advance_transition(attempt, "closed")
            return TickResult("acted", "watch_timeout")

        violations = self._rework_snapshot_violations(
            lease.root, rework_snapshot
        )
        if violations:
            return self._rework_path_stop(
                state, slice_id, attempt, violations
            )

        after = self._workspace.snapshot(lease.root)
        self._store.write_diff_manifest(
            attempt, self._workspace.diff_manifest_payload(lease, before, after)
        )
        violations = check_fences(
            lease,
            workspace_access=access,
            expected_artifacts=tuple(Path(rel) for rel in expected_rel),
            reported_paths=tuple(result.changed_files)
            + tuple(result.produced_artifacts),
            before=before,
            after=after,
            store_root=self._store.root,
        )
        if violations:
            self._journal(
                "fence",
                attempt=attempt.name,
                violations=[
                    {"code": v.code, "path": v.path} for v in violations
                ],
            )
            self._advance_transition(attempt, "closed")
            return self._stop(
                StopReason.PATH_VIOLATION,
                "attempt violated its workspace authority fence",
                state=state,
                slice_id=slice_id,
                attempt_id=attempt.name,
            )

        if verify_after:
            verified = self._run_verification(attempt, lease, slice_id)
            if isinstance(verified, TickResult):
                return verified
        self._advance_transition(attempt, "validated")
        self._advance_transition(attempt, "closed")
        return TickResult("acted", f"{role.value}_attempt_completed")

    def _declared_rework(self, slice_id: str) -> bool:
        """Replay whether the slice is inside a journaled rework lifecycle."""

        for event in self._events:
            slices = event.get("slices")
            if (
                event.get("kind") == "verb"
                and event.get("verb") == "declare-rework"
                and isinstance(slices, list)
                and slice_id in slices
            ):
                return True
        return False

    def _prepare_rework_turn(
        self,
        state: PlanningState,
        slice_id: str,
        attempt: Path,
        lease: WorkspaceLease,
        expected_rel: tuple[str, ...],
    ) -> tuple[dict, bytes] | TickResult:
        """Authenticate the frozen boundary and snapshot protected members."""

        try:
            record = self._store.read_pass_boundary(self._run_id)
        except (OSError, UnicodeDecodeError, ValueError, RunStoreRefusal):
            record = None
        if not _valid_pass_boundary_record(record, self._run_id):
            self._advance_transition(attempt, "closed")
            return self._stop(
                StopReason.INVALID_STATE,
                "rework requires a readable frozen pass-boundary manifest",
                state=state,
                slice_id=slice_id,
                attempt_id=attempt.name,
            )

        boundary_path = self._store.run_dir(self._run_id) / "pass_boundary.json"
        try:
            boundary_sha256 = _bounded_file_sha256(
                boundary_path, self._policy.limits.max_run_store_bytes
            )
        except (OSError, ValueError):
            self._advance_transition(attempt, "closed")
            return self._stop(
                StopReason.INVALID_STATE,
                "rework pass-boundary manifest is missing or over-bound",
                state=state,
                slice_id=slice_id,
                attempt_id=attempt.name,
            )
        boundary_events = [
            event for event in self._events if event.get("kind") == "pass_boundary"
        ]
        if (
            len(boundary_events) != 1
            or boundary_events[0].get("evidence_sha256") != boundary_sha256
            or boundary_events[0].get("evidence_members")
            != len(record["evidence"])
            or boundary_events[0].get("artifact_members")
            != len(record["artifacts"])
        ):
            self._advance_transition(attempt, "closed")
            return self._stop(
                StopReason.INVALID_STATE,
                "rework pass-boundary manifest does not match its durable fact",
                state=state,
                slice_id=slice_id,
                attempt_id=attempt.name,
            )

        frozen: dict[str, bytes] = {}
        frozen_total = 0
        try:
            for member in record["artifacts"]:
                relative = member["path"]
                if not rework_protected_artifact(relative):
                    continue
                data = _bounded_workspace_file(
                    lease.root,
                    relative,
                    self._policy.limits.max_run_store_bytes,
                )
                if hashlib.sha256(data).hexdigest() != member["sha256"]:
                    raise ValueError("protected member differs from frozen hash")
                frozen_total += len(data)
                if frozen_total > self._policy.limits.max_run_store_bytes:
                    raise RunStoreRefusal(
                        "accepted_snapshot_store_full",
                        "protected accepted artifacts exceed the run-store bound",
                    )
                frozen[relative] = data
            envelope = _rework_envelope(expected_rel)
            snapshot = self._store.write_accepted_snapshot(
                attempt,
                frozen,
                pass_boundary_sha256=boundary_sha256,
                max_total_bytes=self._policy.limits.max_run_store_bytes,
            )
        except RunStoreRefusal as refused:
            self._advance_transition(attempt, "closed")
            reason = (
                StopReason.RUN_STORE_FULL
                if refused.code == "accepted_snapshot_store_full"
                else StopReason.INVALID_STATE
            )
            return self._stop(
                reason,
                "rework accepted-artifact snapshot could not be persisted safely",
                state=state,
                slice_id=slice_id,
                attempt_id=attempt.name,
            )
        except (OSError, ValueError):
            self._advance_transition(attempt, "closed")
            return self._stop(
                StopReason.INVALID_STATE,
                "a protected accepted artifact is missing, unreadable, or stale",
                state=state,
                slice_id=slice_id,
                attempt_id=attempt.name,
            )
        return snapshot, envelope

    def _rework_snapshot_violations(
        self, workspace: Path, snapshot: dict | None
    ) -> list[dict[str, str]]:
        if snapshot is None:
            return []
        violations: list[dict[str, str]] = []
        for member in snapshot.get("members", ()):
            try:
                data = _bounded_workspace_file(
                    workspace,
                    member["path"],
                    self._policy.limits.max_run_store_bytes,
                )
                observed = hashlib.sha256(data).hexdigest()
            except (KeyError, OSError, TypeError, ValueError):
                observed = "unavailable"
            if observed != member.get("sha256"):
                violations.append(
                    {
                        "code": "accepted_artifact_changed",
                        "path": str(member.get("path", "")),
                        "expected_sha256": str(member.get("sha256", "")),
                        "observed_sha256": observed,
                        "snapshot": str(member.get("snapshot", "")),
                    }
                )
        return violations

    def _rework_path_stop(
        self,
        state: PlanningState | None,
        slice_id: str,
        attempt: Path,
        violations: list[dict[str, str]],
    ) -> TickResult:
        if not violations:
            violations = [
                {
                    "code": "accepted_artifact_changed",
                    "path": "unavailable",
                    "expected_sha256": "unavailable",
                    "observed_sha256": "unavailable",
                    "snapshot": "accepted_snapshot/inventory.json",
                }
            ]
        self._journal(
            "fence", attempt=attempt.name, violations=violations
        )
        if self._store.read_transition(attempt) != "closed":
            self._advance_transition(attempt, "closed")
        members = "\n".join(
            "- {path}: expected SHA-256 {expected}; observed {observed}; "
            "snapshot {snapshot}".format(
                path=item["path"],
                expected=item["expected_sha256"],
                observed=item["observed_sha256"],
                snapshot=item["snapshot"],
            )
            for item in violations
        )
        return self._stop(
            StopReason.PATH_VIOLATION,
            "rework turn changed a protected accepted artifact",
            state=state,
            slice_id=slice_id,
            attempt_id=attempt.name,
            decision=(
                "Protected accepted-artifact changes require disclosed human "
                "adjudication. The drive does not restore files automatically. "
                "Compare the workspace with the byte-exact attempt snapshots, "
                "decide whether restoration is authorized, and record that "
                "decision before resuming. Before any authorized byte-exact "
                "filing, follow the 'Governed filing protocol (stopped runs "
                "only)' section of 09_ops/operators_manual.md, record the "
                "intervention, and only then resume.\n" + members
            ),
        )

    def _build_request(
        self,
        attempt: Path,
        role: Role,
        access: str,
        prompt_path: Path,
        expected_rel: tuple[str, ...],
        lease,
        seat: RoleSeat | None = None,
        effort: str = "",
    ) -> AgentRunRequest:
        prompt_bytes = Path(prompt_path).read_bytes()
        # The request carries the selected role's exact configured seat
        # facts; no role receives an empty or coder-global placeholder.
        seat = seat or policy_seat(self._policy, role)
        # R1-F5: each request is authorized for no more than the remaining
        # total, so a dispatch can never reopen spent authorization.
        remaining_cost = self._gate.remaining_cost(self._counters)
        if self._max_total_cost_usd is not None:
            remaining_cost = min(
                remaining_cost,
                max(0.0, self._max_total_cost_usd - self._counters.total_cost_usd),
            )
        if self._max_call_cost_usd is not None:
            remaining_cost = min(remaining_cost, self._max_call_cost_usd)
        return AgentRunRequest(
            run_id=self._run_id,
            attempt_id=attempt.name,
            role=role,
            prompt_path=Path(prompt_path),
            prompt_sha256=hashlib.sha256(prompt_bytes).hexdigest(),
            workspace=lease.root,
            base_revision=lease.base_revision,
            adapter=seat.adapter,
            model=seat.model,
            effort=effort,
            workspace_access=access
            if access in ("read_only", "workspace_write")
            else "read_only",
            expected_artifacts=tuple(Path(rel) for rel in expected_rel),
            max_seconds=max(1, int(self._watch_timeout)),
            max_cost_usd=remaining_cost,
        )

    def _effort_for_dispatch(
        self,
        role: Role,
        slice_id: str,
        *,
        repair: bool,
        frontier_round: int = 1,
    ) -> str:
        """Select the fixed launch-time effort for this lifecycle round."""

        default, corrective = self._role_efforts.get(role.value, ("", ""))
        if role is Role.ARCHITECT:
            return default
        collected = self._counters.lifecycle_coder_collected_for(slice_id)
        replay_round = (
            collected + 1
            if role is Role.CODER and not repair
            else max(1, collected)
        )
        round_number = max(frontier_round, replay_round)
        return corrective if round_number > 1 else default

    def _execute(self, request: AgentRunRequest, attempt: Path) -> AgentRunResult | None:
        executor = self._executors[request.role.value]
        try:
            return executor.execute(request)
        except (WorkspaceAuthorityDenied, CostAuthorizationExceeded):
            raise
        except Exception:
            self._journal("attempt_abandoned", attempt=attempt.name,
                          cause="executor_error")
            self._advance_transition(attempt, "closed")
            return None

    def _authority_stop(
        self,
        denied: WorkspaceAuthorityDenied,
        state: PlanningState | None,
        slice_id: str,
        attempt: Path,
    ) -> TickResult:
        self._journal(
            "fence",
            attempt=attempt.name,
            violations=[
                {"code": v.code, "path": v.path} for v in denied.violations
            ],
        )
        self._advance_transition(attempt, "closed")
        return self._stop(
            StopReason.PATH_VIOLATION,
            "pre-effect authority refusal",
            state=state,
            slice_id=slice_id,
            attempt_id=attempt.name,
        )

    def _collect(
        self, attempt: Path, result: AgentRunResult, role: str, slice_id: str
    ) -> None:
        if self._store.read_result(attempt) is None:
            log_path = Path(result.event_log_path)
            if log_path.is_file():
                self._store.write_provider_events(attempt, log_path.read_bytes())
            self._store.write_result(attempt, result)
        self._advance_transition(attempt, "collected")
        if not self._has_collected_event(slice_id, attempt.name):
            self._journal(
                "collected",
                attempt=attempt.name,
                role=role,
                slice=slice_id,
                status=result.status,
                cost_usd=result.cost_usd,
            )

    # -------------------------------------------------------- verification

    def _run_verification(
        self, attempt: Path, lease, slice_id: str
    ) -> TickResult | None:
        recorded = self._verification_event(slice_id, attempt.name)
        if recorded is not None:
            return None if recorded else self._verification_failed_result(attempt)
        evidence_dir = attempt / "verification"
        if evidence_dir.exists():
            self._journal(
                "fence",
                attempt=attempt.name,
                violations=[{"code": "fake_evidence", "path": "verification"}],
            )
            return self._stop(
                StopReason.PATH_VIOLATION,
                "verification evidence existed before the verifier ran",
                slice_id=slice_id,
                attempt_id=attempt.name,
            )
        try:
            outcome = self._verifier.verify(
                attempt,
                self._verification_plan,
                lease.root,
                base_revision=lease.base_revision,
                final_revision_reader=lambda: self._workspace.revision(lease.root),
            )
        except RunStoreRefusal:
            return self._stop(
                StopReason.PATH_VIOLATION,
                "verification evidence could not be published safely",
                slice_id=slice_id,
                attempt_id=attempt.name,
            )
        self._journal(
            "verification",
            attempt=attempt.name,
            slice=slice_id,
            passed=outcome.passed,
            evidence_sha256=outcome.evidence_sha256,
        )
        if not outcome.passed:
            return self._verification_failed_result(attempt)
        return None

    def _verification_failed_result(self, attempt: Path) -> TickResult:
        self._advance_transition(attempt, "closed")
        return TickResult("acted", "verification_failed")

    def _ensure_verified_coder_attempt(self, slice_id: str) -> TickResult | None:
        attempts = self._store.list_attempts(self._run_id, slice_id)
        candidates = [
            a
            for a in attempts
            if self._is_coder_evidence_attempt(a)
        ]
        if not candidates:
            adopted = self._adopt_prior_coder_attempt(slice_id)
            if isinstance(adopted, TickResult):
                return adopted
            if adopted is None:
                return self._stop(
                    StopReason.VERIFICATION_MISSING,
                    "no completed, verified coder attempt exists for this slice",
                    slice_id=slice_id,
                )
            latest = adopted
        else:
            latest = candidates[-1]
        event = self._verification_event_record(slice_id, latest.name)
        if event is not None and bool(event.get("passed")):
            # R1-F3: the journal boolean is routing metadata only; the durable
            # evidence must be present, intact, and bound to this attempt.
            violation = validate_evidence(
                self._store, latest, event.get("evidence_sha256")
            )
            if violation is None:
                if self._store.read_transition(latest) == "validated":
                    self._advance_transition(latest, "closed")
                return None
            return self._stop(
                StopReason.VERIFICATION_MISSING,
                f"evidence revalidation refused: {violation}",
                slice_id=slice_id,
                attempt_id=latest.name,
            )
        if event is not None:
            return self._stop(
                StopReason.VERIFICATION_MISSING,
                "latest coder attempt failed verification",
                slice_id=slice_id,
                attempt_id=latest.name,
            )
        lease = self._workspace.lease(
            self._run_id, slice_id, self._policy.git.worktree_per_slice
        )
        verified = self._run_verification(latest, lease, slice_id)
        if isinstance(verified, TickResult):
            if verified.kind == "stopped":
                return verified
            return self._stop(
                StopReason.VERIFICATION_MISSING,
                "latest coder attempt failed verification",
                slice_id=slice_id,
                attempt_id=latest.name,
            )
        self._advance_transition(latest, "validated")
        self._advance_transition(latest, "closed")
        return None

    def _is_coder_evidence_attempt(self, attempt: Path) -> bool:
        adoption = self._store.read_adoption(attempt)
        if adoption is not None:
            return adoption.get("contract_version") == 1
        return (
            (self._store.read_request(attempt) or {}).get("role") == "coder"
            and (self._store.read_result(attempt) or {}).get("status")
            == "completed"
        )

    def _has_any_coder_evidence(self, slice_id: str) -> bool:
        for run_id in reversed(self._store.list_runs()):
            for attempt in reversed(self._store.list_attempts(run_id, slice_id)):
                if self._is_coder_evidence_attempt(attempt):
                    return True
        return False

    def _adopt_prior_coder_attempt(
        self, slice_id: str
    ) -> Path | TickResult | None:
        prior: tuple[str, Path] | None = None
        for run_id in reversed(self._store.list_runs()):
            if run_id == self._run_id:
                continue
            for attempt in reversed(self._store.list_attempts(run_id, slice_id)):
                request = self._store.read_request(attempt) or {}
                result = self._store.read_result(attempt) or {}
                if (
                    request.get("role") == "coder"
                    and result.get("status") == "completed"
                ):
                    prior = (run_id, attempt)
                    break
            if prior is not None:
                break
        if prior is None:
            return None

        prior_run, prior_attempt = prior
        checked = self._check_adoption_candidate(
            prior_run, slice_id, prior_attempt
        )
        if isinstance(checked, str):
            return self._stop(
                StopReason.VERIFICATION_MISSING,
                f"prior coder evidence adoption refused: {checked}",
                slice_id=slice_id,
                attempt_id=prior_attempt.name,
            )
        hashes = checked
        attempt = self._store.create_attempt(self._run_id, slice_id)
        adoption = {
            "contract_version": 1,
            "prior_run_id": prior_run,
            "prior_slice_id": slice_id,
            "prior_attempt_id": prior_attempt.name,
            "evidence_sha256": hashes,
        }
        self._store.write_adoption(attempt, adoption)
        self._journal(
            "adoption",
            attempt=attempt.name,
            slice=slice_id,
            prior_run=prior_run,
            prior_attempt=prior_attempt.name,
            evidence_sha256=hashes,
        )
        self._advance_transition(attempt, "collected")
        return attempt

    def _check_adoption_candidate(
        self, prior_run: str, slice_id: str, attempt: Path
    ) -> dict[str, str] | str:
        required = ("request.json", "result.json", "diff_manifest.json")
        members: list[tuple[str, Path]] = [
            (name, attempt / name) for name in required
        ]
        verification_dir = attempt / "verification"
        try:
            if not verification_dir.is_dir() or verification_dir.is_symlink():
                return "prior_verification_missing"
            verification_members = tuple(sorted(verification_dir.iterdir()))
        except OSError:
            return "prior_verification_missing"
        if not verification_members:
            return "prior_verification_missing"
        for path in verification_members:
            if path.is_symlink() or not path.is_file():
                return "prior_evidence_member_invalid"
            members.append((f"verification/{path.name}", path))
        hashes: dict[str, str] = {}
        try:
            for name, path in members:
                if path.is_symlink() or not path.is_file():
                    return "prior_evidence_member_missing"
                hashes[name] = _bounded_file_sha256(
                    path, self._policy.limits.max_run_store_bytes
                )
        except (OSError, ValueError):
            return "prior_evidence_hash_refused"

        prior_events = self._store.read_events(prior_run)
        verifications = [
            event
            for event in prior_events
            if event.get("kind") == "verification"
            and event.get("slice") == slice_id
            and event.get("attempt") == attempt.name
        ]
        verification = verifications[0] if len(verifications) == 1 else None
        if (
            verification is None
            or hashes.get("verification/evidence.toml")
            != verification.get("evidence_sha256")
        ):
            return "prior_evidence_journal_mismatch"
        violation = validate_evidence_envelope(
            self._store, attempt, verification.get("evidence_sha256")
        )
        if violation is not None:
            return f"prior_{violation}"

        request = self._store.read_request(attempt) or {}
        prompt_path = _contained_project_path(
            self._project_root, request.get("prompt_path")
        )
        if prompt_path is None or not prompt_path.is_file():
            return "prior_prompt_missing"
        try:
            prompt_sha256 = _bounded_file_sha256(
                prompt_path, self._policy.limits.max_run_store_bytes
            )
        except (OSError, ValueError):
            return "prior_prompt_hash_refused"
        if prompt_sha256 != request.get("prompt_sha256"):
            return "prior_prompt_hash_mismatch"

        try:
            diff = json.loads((attempt / "diff_manifest.json").read_bytes())
        except (OSError, ValueError):
            return "prior_diff_malformed"
        if not isinstance(diff, dict) or not isinstance(diff.get("changes"), list):
            return "prior_diff_malformed"
        changes: dict[str, tuple[str, str | None]] = {}
        for item in diff["changes"]:
            if not isinstance(item, dict):
                return "prior_diff_malformed"
            path = item.get("path")
            kind = item.get("kind")
            digest = item.get("sha256")
            if (
                not _canonical_relative(path)
                or kind not in ("created", "modified", "deleted")
                or (kind == "deleted" and digest is not None)
                or (kind != "deleted" and not _sha256_text(digest))
                or path in changes
            ):
                return "prior_diff_malformed"
            changes[path] = (kind, digest)

        lease = self._workspace.lease(
            self._run_id, slice_id, self._policy.git.worktree_per_slice
        )
        current = self._workspace.snapshot(lease.root)
        for path, (kind, digest) in changes.items():
            if kind == "deleted":
                if path in current:
                    return "workspace_state_changed"
            elif current.get(path) != digest:
                return "workspace_state_changed"
        expected = request.get("expected_artifacts")
        if not isinstance(expected, list) or not expected:
            return "prior_expected_artifacts_missing"
        for path in expected:
            if (
                not _canonical_relative(path)
                or path not in changes
                or changes[path][0] == "deleted"
                or current.get(path) != changes[path][1]
            ):
                return "prior_expected_artifact_mismatch"
        return hashes

    # ----------------------------------------------------------- recovery

    def resume(self) -> TickResult | None:
        self._journal("resume", run=self._run_id)
        stop = self._reconcile_pending_verb()
        if stop is not None:
            return stop
        self._reconcile_shadow_attempts()
        for slice_id in self._store.list_slices(self._run_id):
            for attempt in self._store.list_attempts(self._run_id, slice_id):
                transition = self._store.read_transition(attempt)
                if transition != "closed":
                    try:
                        snapshot = self._store.read_accepted_snapshot(
                            attempt,
                            max_total_bytes=self._policy.limits.max_run_store_bytes,
                        )
                    except RunStoreRefusal:
                        self._advance_transition(attempt, "closed")
                        return self._stop(
                            StopReason.INVALID_STATE,
                            "rework accepted-artifact snapshot is unreadable",
                            slice_id=slice_id,
                            attempt_id=attempt.name,
                        )
                    if snapshot is not None:
                        boundary_path = (
                            self._store.run_dir(self._run_id)
                            / "pass_boundary.json"
                        )
                        try:
                            boundary_sha256 = _bounded_file_sha256(
                                boundary_path,
                                self._policy.limits.max_run_store_bytes,
                            )
                        except (OSError, ValueError):
                            boundary_sha256 = ""
                        if boundary_sha256 != snapshot.get(
                            "pass_boundary_sha256"
                        ):
                            self._advance_transition(attempt, "closed")
                            return self._stop(
                                StopReason.INVALID_STATE,
                                "rework snapshot no longer matches its frozen boundary",
                                slice_id=slice_id,
                                attempt_id=attempt.name,
                            )
                        request = self._store.read_request(attempt) or {}
                        workspace_value = request.get("workspace")
                        workspace = (
                            Path(workspace_value)
                            if isinstance(workspace_value, str)
                            and workspace_value
                            else self._workspace.lease(
                                self._run_id,
                                slice_id,
                                self._policy.git.worktree_per_slice,
                            ).root
                        )
                        violations = self._rework_snapshot_violations(
                            workspace, snapshot
                        )
                        if violations:
                            return self._rework_path_stop(
                                None, slice_id, attempt, violations
                            )
                if transition == "planned":
                    self._journal(
                        "attempt_abandoned", attempt=attempt.name, cause="planned_at_crash"
                    )
                    self._advance_transition(attempt, "closed")
                elif transition in ("started", "externally_completed"):
                    stop = self._reconcile_started(attempt, transition)
                    if stop is not None:
                        return stop
                elif transition in ("collected", "validated"):
                    # A crash between the durable collected transition and
                    # the journal append loses the payment fact; the durable
                    # request/result reconstruct it exactly once.
                    self._reconcile_collected_journal(attempt)
        return None

    def _reconcile_shadow_attempts(self) -> None:
        """Close shadow evidence without routing any primary control path."""
        for slice_id in self._store.list_shadow_slices(self._run_id):
            for attempt in self._store.list_shadow_attempts(self._run_id, slice_id):
                matching = [
                    event
                    for event in self._events
                    if event.get("kind") == "shadow_review"
                    and event.get("slice") == slice_id
                    and event.get("attempt") == attempt.name
                ]
                if not matching:
                    result = self._store.read_result(attempt) or {}
                    now = self._clock.now()
                    self._journal(
                        "shadow_review",
                        attempt=attempt.name,
                        primary_attempt="",
                        slice=slice_id,
                        role="shadow_reviewer",
                        adapter=str((self._store.read_request(attempt) or {}).get("adapter", "")),
                        model=str((self._store.read_request(attempt) or {}).get("model", "")),
                        dispatched=True,
                        dispatched_at=None,
                        completed_at=now,
                        status="interrupted",
                        tokens_in=result.get("tokens_in"),
                        tokens_out=result.get("tokens_out"),
                        cost_usd=result.get("cost_usd"),
                    )
                if self._store.read_transition(attempt) != "closed":
                    self._advance_transition(attempt, "validated")
                    self._advance_transition(attempt, "closed")

    def _reconcile_collected_journal(self, attempt: Path) -> None:
        request = self._store.read_request(attempt) or {}
        result = self._store.read_result(attempt) or {}
        if not request or not result:
            return
        slice_id = attempt.parent.name
        if not self._has_collected_event(slice_id, attempt.name):
            self._journal(
                "collected",
                attempt=attempt.name,
                role=str(request.get("role", "")),
                slice=slice_id,
                status=str(result.get("status", "")),
                cost_usd=result.get("cost_usd"),
            )
        completed_control = any(
            event.get("attempt") == attempt.name
            and event.get("slice") == slice_id
            and event.get("kind") in ("reconciliation", "holistic_review")
            for event in self._events
        )
        if completed_control:
            self._advance_transition(attempt, "validated")
            self._advance_transition(attempt, "closed")

    def _reconcile_pending_verb(self) -> TickResult | None:
        """Delegate crash recovery to the normal verb transaction authority."""

        reconcile = getattr(self._verb_writer, "reconcile_pending", None)
        if reconcile is None:
            return None
        try:
            recovered = reconcile()
        except FrutlupsVerbError as failed:
            self._journal("refusal", code=failed.code)
            return self._stop(
                StopReason.INVALID_STATE,
                f"pending governed verb refused: {failed.code}",
            )
        except VerbAuthorityDenied as denied:
            self._journal(
                "fence",
                attempt="",
                violations=[
                    {"code": item.code, "path": item.path}
                    for item in denied.violations
                ],
            )
            return self._stop(
                StopReason.PATH_VIOLATION,
                "pending governed verb target is outside its authority",
            )
        if recovered is None:
            return None
        if not isinstance(recovered, RecoveredVerb):
            return self._stop(
                StopReason.INVALID_STATE,
                "pending governed verb recovery returned an invalid witness",
            )
        verb = recovered.verb
        target = recovered.artifact.relative_to(self._project_root).as_posix()
        matching = [
            event
            for event in self._store.read_events(self._run_id)
            if event.get("kind") == "verb"
            and event.get("verb") == verb
            and event.get("artifact") == target
        ]
        if len(matching) > 1:
            return self._stop(
                StopReason.INVALID_STATE,
                "pending governed verb has duplicate durable journal facts",
            )
        if not matching:
            journal_fields: dict[str, object] = {
                "verb": verb,
                "artifact": target,
                "slice": recovered.slice_id or "",
            }
            if verb == "declare-rework":
                journal_fields["pass_id"] = recovered.pass_id
                journal_fields["slices"] = list(recovered.rework_slices)
            self._journal("verb", **journal_fields)
        mark_journaled = getattr(self._verb_writer, "mark_journaled", None)
        if mark_journaled is not None:
            mark_journaled(verb)
        clear_intent = getattr(self._verb_writer, "clear_intent", None)
        if clear_intent is not None:
            clear_intent()
        return None

    def _reconcile_started(
        self, attempt: Path, transition: str
    ) -> TickResult | None:
        request = self._store.read_request(attempt)
        if request is None:
            self._journal(
                "attempt_abandoned", attempt=attempt.name, cause="request_missing"
            )
            self._advance_transition(attempt, "closed")
            return None
        workspace = Path(str(request.get("workspace")))
        expected = [str(p) for p in request.get("expected_artifacts", [])]
        artifacts_present = bool(expected) and all(
            (workspace / rel).is_file() for rel in expected
        )
        if transition == "started" and not artifacts_present:
            self._journal(
                "attempt_abandoned", attempt=attempt.name, cause="no_external_artifacts"
            )
            self._advance_transition(attempt, "closed")
            return None

        prompt_path = Path(str(request.get("prompt_path")))
        if not prompt_path.is_absolute():
            prompt_path = self._project_root / prompt_path
        current_sha = (
            hashlib.sha256(prompt_path.read_bytes()).hexdigest()
            if prompt_path.is_file()
            else ""
        )
        if current_sha != request.get("prompt_sha256"):
            return self._stop(
                StopReason.INVALID_STATE,
                "stale prompt hash: external output cannot bind to a newer prompt",
                slice_id=attempt.parent.name,
                attempt_id=attempt.name,
            )
        source = self._prompt_source_for_attempt(
            attempt.parent.name, attempt.name
        )
        if source is not None:
            source_path = _contained_project_path(self._project_root, source[0])
            source_sha = (
                hashlib.sha256(source_path.read_bytes()).hexdigest()
                if source_path is not None and source_path.is_file()
                else ""
            )
            if source_sha != source[1]:
                return self._stop(
                    StopReason.INVALID_STATE,
                    "stale prompt hash: external output cannot bind to a newer prompt",
                    slice_id=attempt.parent.name,
                    attempt_id=attempt.name,
                )
        if transition == "started":
            self._advance_transition(attempt, "externally_completed")
        if self._store.read_result(attempt) is None:
            synthesized = AgentRunResult(
                status="completed",
                event_log_path=Path("external"),
                changed_files=(),
                produced_artifacts=tuple(Path(rel) for rel in expected),
                exit_reason="externally_completed",
                tokens_in=None,
                tokens_out=None,
                cost_usd=None,
            )
            self._store.write_result(attempt, synthesized)
        self._advance_transition(attempt, "collected")
        if not self._has_collected_event(attempt.parent.name, attempt.name):
            self._journal(
                "collected",
                attempt=attempt.name,
                role=str(request.get("role", "")),
                slice=attempt.parent.name,
                status="completed",
                cost_usd=None,
            )
        self._journal(
            "reconciled_attempt",
            attempt=attempt.name,
            disposition="externally_completed",
        )
        return None

    def _pending_coder_attempt(self, slice_id: str) -> Path | None:
        for attempt in reversed(self._store.list_attempts(self._run_id, slice_id)):
            transition = self._store.read_transition(attempt)
            if transition in ("collected", "validated"):
                request = self._store.read_request(attempt) or {}
                result = self._store.read_result(attempt) or {}
                if (
                    self._store.read_adoption(attempt) is not None
                    or (
                        request.get("role") == "coder"
                        and result.get("status") == "completed"
                    )
                ):
                    return attempt
        return None

    def _finish_coder_attempt(self, attempt: Path, slice_id: str) -> TickResult:
        lease = self._workspace.lease(
            self._run_id, slice_id, self._policy.git.worktree_per_slice
        )
        verified = self._run_verification(attempt, lease, slice_id)
        if isinstance(verified, TickResult):
            return verified
        self._advance_transition(attempt, "validated")
        self._advance_transition(attempt, "closed")
        return TickResult("acted", "coder_attempt_completed")

    def _satisfied_coder_attempt(
        self, slice_id: str, frontier_round: int, prompt_rel: str
    ) -> bool:
        # A completed tick is replayed after a crash only when the same state
        # is re-served; a corrective round carries an incremented
        # frontier.round, so the count of verified closed attempts decides.
        prompt = self._project_root / prompt_rel
        if not prompt.is_file():
            return False
        try:
            prompt_sha256 = hashlib.sha256(prompt.read_bytes()).hexdigest()
        except OSError:
            return False
        verified = 0
        for attempt in self._store.list_attempts(self._run_id, slice_id):
            if self._store.read_transition(attempt) == "closed":
                request = self._store.read_request(attempt) or {}
                if (
                    self._is_coder_evidence_attempt(attempt)
                    and self._request_matches_prompt_source(
                        slice_id, attempt.name, request, prompt_sha256
                    )
                    and self._verification_event(slice_id, attempt.name) is True
                ):
                    verified += 1
        return verified >= max(1, frontier_round)

    def _prompt_source_for_attempt(
        self, slice_id: str, attempt_name: str
    ) -> tuple[str, str] | None:
        for event in reversed(self._events):
            if (
                event.get("kind") == "dispatch"
                and event.get("slice") == slice_id
                and event.get("attempt") == attempt_name
                and isinstance(event.get("prompt_source"), str)
                and _sha256_text(event.get("prompt_source_sha256"))
            ):
                return (
                    str(event["prompt_source"]),
                    str(event["prompt_source_sha256"]),
                )
        return None

    def _request_matches_prompt_source(
        self,
        slice_id: str,
        attempt_name: str,
        request: Mapping[str, object],
        prompt_source_sha256: str,
    ) -> bool:
        source = self._prompt_source_for_attempt(slice_id, attempt_name)
        if source is not None:
            return source[1] == prompt_source_sha256
        # Compatibility with attempts journaled before dispatch-envelope
        # composition, whose request hash was the source prompt hash.
        return request.get("prompt_sha256") == prompt_source_sha256

    # ------------------------------------------------------------- helpers

    def _verification_event(self, slice_id: str, attempt_name: str) -> bool | None:
        event = self._verification_event_record(slice_id, attempt_name)
        return None if event is None else bool(event.get("passed"))

    def _verification_event_record(
        self, slice_id: str, attempt_name: str
    ) -> dict | None:
        # Attempt names restart per slice (each slice has its own
        # attempt_001), so a journal match must carry both identifiers or a
        # later slice would inherit an earlier slice's verification outcome.
        for event in reversed(self._events):
            if (
                event.get("kind") == "verification"
                and event.get("attempt") == attempt_name
                and event.get("slice") == slice_id
            ):
                return event
        return None

    def _has_collected_event(self, slice_id: str, attempt_name: str) -> bool:
        return any(
            event.get("kind") == "collected"
            and event.get("attempt") == attempt_name
            and event.get("slice") == slice_id
            for event in self._events
        )

    def _enforce_run_store(self) -> TickResult | None:
        try:
            outcome = self._store.enforce_limits(
                self._run_id,
                max_total_bytes=self._policy.limits.max_run_store_bytes,
                max_retained_runs=self._policy.limits.max_retained_runs,
            )
        except RunStoreRefusal as refusal:
            return self._stop(
                StopReason.RUN_STORE_FULL,
                f"run-store control refused: {refusal.code}",
            )
        if outcome.deleted_runs:
            self._journal(
                "run_store_control",
                before_total_bytes=outcome.before_total_bytes,
                total_bytes=outcome.total_bytes,
                per_run_bytes=dict(outcome.per_run_bytes),
                deleted_runs=list(outcome.deleted_runs),
            )
        return None

    @staticmethod
    def _tick_advanced(result: TickResult) -> bool:
        if result.kind == "boundary":
            return True
        if result.kind != "acted":
            return False
        if result.detail in (
            "frontier_unchanged",
            "provider_failure",
            "watch_timeout",
            "verification_failed",
        ):
            return False
        return not result.detail.startswith("attempt_")

    def _frontier_unchanged(self, slice_id: str) -> bool:
        """Return whether the latest completion-relevant fact is this slice.

        A fresh record-verdict permits one frontier completion.  With no later
        acceptance fact, observing the same completed slice again cannot
        advance the frontier and must consume the no-progress budget instead
        of creating another slice-complete fact.
        """

        for event in reversed(self._events):
            if event.get("kind") == "slice_complete":
                return event.get("slice") == slice_id
            if (
                event.get("kind") == "verb"
                and event.get("verb") == "record-verdict"
            ):
                return False
        return False

    def _read_project_file(self, relative: str) -> str:
        path = self._project_root / relative
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def _prompt_with_memory(
        self, attempt: Path, prompt_path: Path, *, envelope: bytes = b""
    ) -> Path:
        """Project optional context and fixed conduct into attempt evidence."""

        prompt = prompt_path.read_bytes()
        context = b""
        if self._memory_hooks is not None:
            try:
                result = self._memory_hooks.read_context(prompt)
                self._journal_memory_facts(result.facts)
                context = result.context
            except Exception:
                self._journal(
                    "memory_hook",
                    hook="bounded_context",
                    status="refused",
                    reason="memory_context_internal_refusal",
                    evidence="",
                )
        return self._store.write_attempt_prompt(
            attempt,
            "memory_prompt.md",
            _SEAT_CONDUCT_BLOCK + prompt + envelope + context,
        )

    def _queue_memory_updates(self, slice_id: str) -> None:
        if self._memory_hooks is None:
            return
        try:
            self._journal_memory_facts(
                self._memory_hooks.queue_updates(slice_id, ())
            )
        except Exception:
            self._journal(
                "memory_hook",
                hook="boundary_update_submission",
                status="refused",
                reason="memory_update_internal_refusal",
                evidence="",
            )

    def _journal_memory_facts(
        self, facts: tuple[MemoryHookFact, ...]
    ) -> None:
        for fact in facts:
            fields = {
                "hook": fact.hook,
                "status": fact.status,
                "reason": fact.reason,
                "evidence": fact.evidence,
            }
            if fact.proposal_id:
                fields["proposal_id"] = fact.proposal_id
            if fact.proposal_document:
                fields["proposal_document"] = fact.proposal_document
            if fact.queue_evidence:
                fields["queue_evidence"] = fact.queue_evidence
            self._journal("memory_hook", **fields)

    def _journal(self, kind: str, **fields: object) -> None:
        event: dict[str, object] = {"kind": kind, "t": self._clock.now()}
        event.update(fields)
        self._store.append_event(self._run_id, event)
        self._events.append(event)
        self._counters.apply(event)

    def _advance_transition(self, attempt: Path, state: str) -> None:
        """Advance one attempt step without ever requesting a regression."""

        try:
            target_index = TRANSITION_STATES.index(state)
        except ValueError:
            # Preserve the run store as the authority for unknown-state
            # refusal instead of turning a caller bug into a generic error.
            self._store.advance_transition(attempt, state)
            return
        current = self._store.read_transition(attempt)
        if TRANSITION_STATES.index(current) >= target_index:
            return
        self._store.advance_transition(attempt, state)

    def _stop(
        self,
        reason: StopReason,
        detail: str,
        state: PlanningState | None = None,
        slice_id: str = "",
        attempt_id: str = "",
        decision: str = "",
    ) -> TickResult:
        if state is not None and not slice_id and state.frontier:
            slice_id = state.frontier.slice_id
        snapshot = "planning state unavailable at stop"
        if state is not None:
            codes = ",".join(d.code for d in state.diagnostics) or "none"
            snapshot = (
                f"outcome={state.outcome.value}; "
                f"step={state.step.value if state.step else 'null'}; "
                f"diagnostic_codes={codes}"
            )
        attempts_summary = self._attempts_summary(slice_id)
        decision_required = decision or (
            f"Resolve the '{reason.value}' stop condition ({detail})."
        )
        safe_options = (
            "Inspect the run store journal and attempt records; adjust "
            "policy limits or the driven project's artifacts; then resume "
            "only when the governing prompt or owner authority permits it."
        )
        if reason is StopReason.LADDER_ROUND3:
            attempts_summary += "\n\n" + self._ladder_round3_chain(
                state, slice_id
            )
            decision_required = (
                "Before any resume, the architect reassessment must classify "
                "the blocking findings and record exactly one exit:\n"
                "(a) product-plane code defects — exit to a corrective round; "
                "round 4 still requires the existing human authorization;\n"
                "(b) demands not traceable to the coding prompt's Task, "
                "Non-Goals, Verification, or Definition Of Done — exit to "
                "envelope-expansion change control, never another corrective "
                "round; or\n"
                "(c) evidence/documentation-plane issues — exit to a narrow "
                "documentation round, a P3 disposition accompanying pass, "
                "or a waiver/accepted-limitation record.\n"
                "The classification and selected exit must be recorded before "
                "any resume."
            )
            safe_options = (
                "Use only the recorded three-exit reassessment fork. The "
                "runner does not parse reports, compute invariant recurrence, "
                "or authorize a fourth round."
            )
        escalation = write_escalation(
            self._store,
            self._run_id,
            reason=reason,
            slice_id=slice_id,
            attempt_id=attempt_id,
            planning_snapshot=snapshot,
            attempts_summary=attempts_summary,
            decision_required=decision_required,
            safe_options=safe_options,
            actions_not_taken=(
                "No commit, artifact acceptance, governance edit, redispatch, "
                "or retry was performed after the stop condition fired."
            ),
            resume_command=f"python -m frutlups_drive resume . {self._run_id}",
        )
        try:
            self._journal(
                "stop",
                reason=reason.value,
                detail=detail,
                escalation=escalation.name,
                slice=slice_id,
                attempt=attempt_id,
            )
        except RunStoreRefusal as refusal:
            # The tick boundary must not recursively retry the very journal
            # write that proves the governed stop. One honest attempt is the
            # limit when the append-only store itself is unavailable.
            setattr(refusal, _STOP_JOURNAL_ATTEMPTED, True)
            raise
        return TickResult("stopped", detail, reason, escalation)

    def _ladder_round3_chain(
        self, state: PlanningState | None, slice_id: str
    ) -> str:
        """Render the active lifecycle from journal and planning state only."""

        start = 0
        for index, event in enumerate(self._events):
            slices = event.get("slices")
            if (
                event.get("kind") == "verb"
                and event.get("verb") == "declare-rework"
                and isinstance(slices, list)
                and slice_id in slices
            ):
                start = index + 1

        rounds: dict[int, list[str]] = {}
        collected_coder_rounds = 0
        for event in self._events[start:]:
            if event.get("slice") != slice_id:
                continue
            if event.get("kind") == "collected" and event.get("role") == "coder":
                collected_coder_rounds += 1
                continue
            if event.get("kind") != "dispatch":
                continue
            role = str(event.get("role", "unknown"))
            repair = bool(event.get("repair"))
            active_round = (
                collected_coder_rounds + 1
                if role == "coder" and not repair
                else max(1, collected_coder_rounds)
            )
            identity = (
                f"{event.get('adapter', 'unknown')}/"
                f"{event.get('model', 'unknown')} "
                f"effort={event.get('effort', 'unavailable')}"
            )
            prompt = event.get("prompt_source", "not journaled")
            rounds.setdefault(active_round, []).append(
                f"  - {role} dispatch {event.get('attempt', 'unknown')}: "
                f"{identity}; repair={str(repair).lower()}; prompt={prompt}"
            )

        artifact_line = "unavailable"
        verdict_line = "verdict unavailable"
        if state is not None:
            artifacts = state.artifacts
            artifact_line = "; ".join(
                f"{name}={getattr(artifacts, name) or 'null'}"
                for name in (
                    "coding_prompt",
                    "self_report",
                    "review_prompt",
                    "review_report",
                    "verdict_record",
                )
            )
            if state.verdict is not None:
                verdict_line = (
                    f"verdict.value={state.verdict.value}; "
                    f"verdict.next_move={state.verdict.next_move}; "
                    f"verdict.report={state.verdict.report}"
                )

        lines = ["### Active Lifecycle Chain"]
        if not rounds:
            lines.append("- no active-lifecycle dispatches journaled")
        for number, dispatches in sorted(rounds.items()):
            lines.append(f"- round {number}")
            lines.extend(dispatches)
            lines.append(f"  - planning-state artifact paths: {artifact_line}")
        lines.append(f"- planning-state verdict tokens: {verdict_line}")
        return "\n".join(lines)

    def _attempts_summary(self, slice_id: str) -> str:
        if not slice_id:
            return "no frontier slice at stop"
        try:
            attempts = self._store.list_attempts(self._run_id, slice_id)
        except RunStoreRefusal:
            return "no attempts recorded"
        if not attempts:
            return "no attempts recorded"
        lines = []
        for attempt in attempts:
            transition = self._store.read_transition(attempt)
            lines.append(f"- {attempt.name}: {transition}")
        return "\n".join(lines)


def _bounded_file_sha256(path: Path, cap: int) -> str:
    with open(path, "rb") as stream:
        data = stream.read(cap + 1)
    if len(data) > cap:
        raise ValueError("file exceeds the run-store evidence bound")
    return hashlib.sha256(data).hexdigest()


def _bounded_workspace_file(root: Path, relative: str, cap: int) -> bytes:
    if not _canonical_relative(relative):
        raise ValueError("workspace artifact path is not canonical")
    target = Path(root) / relative
    if target.is_symlink() or _is_junction(target):
        raise ValueError("workspace artifact is link-like")
    try:
        resolved_root = Path(root).resolve(strict=True)
        resolved = target.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError):
        raise ValueError("workspace artifact is outside the workspace") from None
    if not resolved.is_file():
        raise ValueError("workspace artifact is not an ordinary file")
    with open(resolved, "rb") as stream:
        data = stream.read(cap + 1)
    if len(data) > cap:
        raise ValueError("workspace artifact exceeds the run-store evidence bound")
    return data


def _rework_envelope(expected_rel: tuple[str, ...]) -> bytes:
    if not expected_rel or any(not _canonical_relative(path) for path in expected_rel):
        raise ValueError("rework declared outputs are missing or non-canonical")
    outputs = b"".join(
        b"- `" + path.encode("utf-8") + b"`\n" for path in expected_rel
    )
    envelope = (
        b"\n\n## Rework Turn Write Authority\n\n"
        b"The only report or evidence outputs authorized for this turn are:\n"
        + outputs
        + b"Legitimate product and code changes required by the governed prompt "
        b"remain authorized. Never create, modify, delete, or rename an accepted "
        b"artifact, including any accepted coding prompt, review prompt, "
        b"self-report, review report, or verdict record. A protected accepted "
        b"artifact write stops the run.\n"
    )
    if len(envelope) > _MAX_REWORK_ENVELOPE_BYTES:
        raise ValueError("rework dispatch envelope exceeds its byte bound")
    return envelope


def _sha256_text(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(char in "0123456789abcdef" for char in value)


def _canonical_relative(value: object) -> bool:
    if not isinstance(value, str) or not value or chr(92) in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and path.as_posix() == value
        and all(part not in ("", ".", "..") for part in path.parts)
    )


def _contained_project_path(project_root: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        if not _canonical_relative(value):
            return None
        candidate = project_root / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(project_root.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def _checked_findings(payload: object, maximum: int) -> list[str]:
    if not isinstance(payload, dict):
        raise ValueError("holistic review must be an object")
    allowed = {"findings"} if "contract_version" not in payload else {
        "contract_version",
        "pass_number",
        "findings",
    }
    if set(payload) != allowed or (
        "contract_version" in payload and payload.get("contract_version") != 1
    ):
        raise ValueError("holistic review fields are not exact")
    if "pass_number" in payload and (
        type(payload.get("pass_number")) is not int
        or payload["pass_number"] <= 0
    ):
        raise ValueError("holistic pass number is invalid")
    findings = payload.get("findings")
    if not isinstance(findings, list) or len(findings) > maximum:
        raise ValueError("holistic findings exceed their bound")
    if any(
        not isinstance(item, str) or not _SLICE_ID.fullmatch(item)
        for item in findings
    ) or len(set(findings)) != len(findings):
        raise ValueError("holistic findings are not unique slice identifiers")
    return list(findings)


def _valid_pass_boundary_record(payload: object, run_id: str) -> bool:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"contract_version", "run_id", "evidence", "artifacts"}
        or payload.get("contract_version") != 1
        or payload.get("run_id") != run_id
    ):
        return False
    for key in ("evidence", "artifacts"):
        members = payload.get(key)
        if not isinstance(members, list) or len(members) > _MAX_BOUNDARY_MEMBERS:
            return False
        paths: set[str] = set()
        for member in members:
            if (
                not isinstance(member, dict)
                or set(member) != {"path", "sha256"}
                or not _canonical_relative(member.get("path"))
                or not _sha256_text(member.get("sha256"))
                or member["path"] in paths
            ):
                return False
            paths.add(member["path"])
    return True


def _tree_inventory(
    root: Path,
    *,
    excluded: frozenset[str],
    max_members: int,
    max_file_bytes: int,
) -> tuple[dict[str, str], ...]:
    records: list[dict[str, str]] = []
    root = Path(root)
    for current, directories, filenames in os.walk(root):
        current_path = Path(current)
        for name in tuple(directories):
            path = current_path / name
            relative_directory = path.relative_to(root).as_posix()
            if relative_directory in ("shadow", "memory_hooks", "memory_updates"):
                # Optional observer evidence is retained for humans but is
                # outside the frozen primary evidence that can drive closure.
                directories.remove(name)
                continue
            if path.is_symlink() or _is_junction(path):
                raise ValueError("evidence inventory contains a link-like directory")
        for name in filenames:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if relative in excluded:
                continue
            if path.is_symlink() or _is_junction(path) or not path.is_file():
                raise ValueError("evidence inventory contains a non-regular file")
            records.append(
                {
                    "path": relative,
                    "sha256": (
                        _primary_events_sha256(path, max_file_bytes)
                        if relative == "events.jsonl"
                        else _bounded_file_sha256(path, max_file_bytes)
                    ),
                }
            )
            if len(records) > max_members:
                raise ValueError("evidence inventory exceeds its member bound")
    records.sort(key=lambda item: item["path"])
    return tuple(records)


def _primary_events_sha256(path: Path, cap: int) -> str:
    """Hash exact journal bytes after removing optional observer facts."""
    with open(path, "rb") as stream:
        data = stream.read(cap + 1)
    if len(data) > cap:
        raise ValueError("journal exceeds its evidence bound")
    primary: list[bytes] = []
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            raise ValueError("journal contains a partial event")
        try:
            event = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("journal contains an invalid event") from None
        if not isinstance(event, dict):
            raise ValueError("journal contains an invalid event")
        if event.get("kind") not in ("shadow_review", "memory_hook"):
            primary.append(line)
    return hashlib.sha256(b"".join(primary)).hexdigest()


def _is_junction(path: Path) -> bool:
    checker = getattr(os.path, "isjunction", None)
    return bool(checker is not None and checker(path))
