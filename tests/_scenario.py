"""Deterministic in-process scenario harness for the M002 supervisor lanes.

Builds a minimal template-shaped fixture project in a temporary root, wires a
supervisor from scripted planning states, executor actions, verb scripts, and
a fake process runner, and journals ``run_created`` exactly as the CLI does.
Everything is injected; nothing sleeps, spawns real agents, or leaves the
temporary root.
"""

import json
from pathlib import Path

import _bootstrap  # noqa: F401

from frutlups_drive.budget import BudgetCounters
from frutlups_drive.dispatch.mock import MockAgentExecutor
from frutlups_drive.mockverbs import MockVerbWriter
from frutlups_drive.planstate import MockPlanProvider
from frutlups_drive.policy import SCHEMA_VERSION, load_execution_policy
from frutlups_drive.runstore import RunStore
from frutlups_drive.supervisor import Supervisor, mock_plan_offset
from frutlups_drive.verifier import (
    ProcessOutcome,
    VerificationCommand,
    VerificationPlan,
    Verifier,
)
from frutlups_drive.watcher import Watcher
from frutlups_drive.workspace import WorkspaceManager

CODING_PROMPT = "prompts/for_coding_agent/001_m001_s01_fix.md"
SELF_REPORT = "05_governance/reviews/m001/m001_s01_self_report.md"
REVIEW_PROMPT = "prompts/for_review_agent/002_m001_s01_review.md"
REVIEW_REPORT = "05_governance/reviews/m001/m001_s01_round1_review_report.md"
REVIEW_PROMPT_2 = "prompts/for_review_agent/003_m001_s01_round2_review.md"
REVIEW_REPORT_2 = "05_governance/reviews/m001/m001_s01_round2_review_report.md"
VERDICT_RECORD = "05_governance/reviews/m001/m001_s01_verdict_record.md"
ACTIVE_ROADMAP = "03_experiments/active_roadmap_fixture.md"

ROADMAP_BODY = """# Fixture Roadmap

Destination: exercise the fixture.

### M001: Fixture Milestone

Status: active

Disposition: Phase B open.

Slices:
- M001-S01: Fixture slice

Implementation package: exercise the fixture.

Objective:
Implement the fixture behavior.

Expected artifacts:
- fixture evidence.

Active workspaces:
- package.

Non-goals:
- external effects.

Verification/evidence:
- deterministic tests.

Review strictness: Level 3.

Likely coding prompt:
Use the routed coding artifact.

Done when:
- fixture checks pass.

## Ruled Out

- unrelated mutation.
"""

PROMPT_BODY = (
    "# Fix The Fixture\n\n```yaml\nmilestone: M001\nslice: M001-S01\n"
    "role: coder\nstatus: ready\n```\n\nDo the fixture work.\n"
)


def executor_consumed_counts(store, run_id):
    counts = {"architect": 0, "coder": 0, "reviewer": 0,
              "shadow_reviewer": 0}
    for slice_id in store.list_slices(run_id):
        for attempt in store.list_attempts(run_id, slice_id):
            request = store.read_request(attempt)
            result = store.read_result(attempt)
            if (
                request
                and result
                and result.get("exit_reason") != "externally_completed"
                and request.get("role") in counts
            ):
                counts[str(request["role"])] += 1
    for slice_id in store.list_shadow_slices(run_id):
        for attempt in store.list_shadow_attempts(run_id, slice_id):
            result = store.read_result(attempt)
            if result and result.get("exit_reason") != "externally_completed":
                counts["shadow_reviewer"] += 1
    return counts


class FakeClock:
    def __init__(self, start=1000.0):
        self.value = start

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeProcessRunner:
    """Scripted verification process outcomes; deterministic bytes."""

    def __init__(self, clock, exit_codes=None, timed_out=None):
        self._clock = clock
        self.exit_codes = list(exit_codes or [])
        self.timed_out = list(timed_out or [])
        self.calls = 0

    def run(self, argv, cwd, env, timeout_seconds, stdout_path, stderr_path,
            max_stream_bytes=1_048_576):
        index = self.calls
        self.calls += 1
        exit_code = self.exit_codes[index] if index < len(self.exit_codes) else 0
        timed = self.timed_out[index] if index < len(self.timed_out) else False
        started = self._clock.now()
        if timed:
            self._clock.advance(timeout_seconds)
        Path(stdout_path).write_bytes(b"fake verification stdout\n")
        Path(stderr_path).write_bytes(b"")
        return ProcessOutcome(
            started, self._clock.now(), None if timed else exit_code, timed
        )


def payload(
    outcome="ready",
    step="execute_coding_prompt",
    *,
    slice_id="M001-S01",
    milestone="M001",
    round_=1,
    coding_prompt=CODING_PROMPT,
    self_report=SELF_REPORT,
    review_prompt=None,
    review_report=None,
    verdict_record=None,
    verdict=None,
    blocked=None,
    completion_evidence=None,
    diagnostics=(),
    actor="coder",
    frontier_present=True,
):
    body = {
        "contract": "frutlups_planning_state",
        "version": 1,
        "outcome": outcome,
        "step": step,
        "actor": actor,
        "gate_state": "open",
        "frontier": {
            "milestone_id": milestone,
            "slice_id": slice_id,
            "slice_title": "Fixture slice",
            "round": round_,
        }
        if frontier_present
        else None,
        "artifacts": {
            "coding_prompt": coding_prompt,
            "self_report": self_report,
            "review_prompt": review_prompt,
            "review_report": review_report,
            "verdict_record": verdict_record,
        },
        "verdict": verdict,
        "blocked": blocked,
        "completion_evidence": completion_evidence,
        "diagnostics": list(diagnostics),
        "next_command": "python -m frutlups status . --json",
    }
    return json.dumps(body).encode("utf-8")


def build_project(root: Path) -> Path:
    project = Path(root) / "project"
    (project / "prompts/for_coding_agent").mkdir(parents=True)
    (project / "prompts/for_review_agent").mkdir(parents=True)
    (project / "05_governance/reviews/m001").mkdir(parents=True)
    (project / "03_experiments").mkdir(parents=True)
    (project / "PROJECT_STATE.md").write_bytes(
        b"# Project State\n\nStatus: fixture\n"
    )
    (project / "03_experiments/roadmap.md").write_bytes(b"# Roadmap\n")
    (project / ACTIVE_ROADMAP).write_bytes(ROADMAP_BODY.encode("utf-8"))
    (project / CODING_PROMPT).write_bytes(PROMPT_BODY.encode("utf-8"))
    (project / "05_governance/reviews/INDEX.md").write_text(
        "# Review Index\n\n"
        "| Milestone | Slice | Round | Self-Report | Review Prompt | "
        "Review Report | Verdict | Commit |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n",
        encoding="utf-8",
    )
    return project


class Scenario:
    def __init__(
        self,
        tmp_root: Path,
        *,
        states,
        coder=(),
        reviewer=(),
        shadow_reviewer=(),
        architect=(),
        verbs=None,
        policy_body="",
        boundary="slice_complete",
        verifier_exit_codes=None,
        verifier_timed_out=None,
        watch_timeout=5.0,
        transition_hook=None,
        event_hook=None,
        round4_authority=None,
        project=None,
        run_id="run_001",
        sleep_hook=None,
        memory_hooks_factory=None,
        role_efforts=None,
    ):
        self.root = Path(tmp_root)
        self.project = project if project is not None else build_project(self.root)
        policy_path = self.project / "frutlups_drive.toml"
        if not policy_path.is_file():
            policy_path.write_bytes(
                (f'schema_version = "{SCHEMA_VERSION}"\n' + policy_body).encode(
                    "utf-8"
                )
            )
        self.policy = load_execution_policy(policy_path).policy
        self.clock = FakeClock()
        self.store = RunStore(
            self.project / ".frutlups_drive",
            transition_hook=transition_hook,
            event_hook=event_hook,
        )
        self.run_id = run_id
        if not self.store.run_exists(self.run_id):
            self.store.create_run(
                self.run_id, {"boundary": boundary, "contract_version": 1}
            )
            self.store.append_event(
                self.run_id,
                {"kind": "run_created", "t": self.clock.now(), "boundary": boundary},
            )
        self.runner = FakeProcessRunner(
            self.clock,
            exit_codes=verifier_exit_codes,
            timed_out=verifier_timed_out,
        )
        events = self.store.read_events(self.run_id)
        plan_offset = mock_plan_offset(list(events))
        # Executor scripts advance only for attempts whose result was produced
        # by an actual executor run (durable in the store); crashed-before-
        # execution dispatches and externally completed attempts re-serve or
        # skip their scripted action deterministically.
        role_counts = executor_consumed_counts(self.store, self.run_id)
        verb_counts = {}
        for event in events:
            if event.get("kind") == "verb":
                verb_counts[str(event["verb"])] = (
                    verb_counts.get(str(event["verb"]), 0) + 1
                )
        log_dir = self.store.run_dir(self.run_id) / "adapter_logs"
        def sleep(seconds):
            self.clock.advance(seconds)
            if sleep_hook is not None:
                sleep_hook(self)

        memory_hooks = (
            memory_hooks_factory(
                self.project, self.store, self.run_id, self.clock
            )
            if memory_hooks_factory is not None
            else None
        )

        self.supervisor = Supervisor(
            project_root=self.project,
            store=self.store,
            run_id=self.run_id,
            policy=self.policy,
            boundary=boundary,
            plan_provider=MockPlanProvider(
                list(states)[plan_offset:]
            ),
            executors={
                "coder": MockAgentExecutor(
                    list(coder), log_dir, consumed=role_counts["coder"],
                    store_root=self.store.root,
                ),
                "reviewer": MockAgentExecutor(
                    list(reviewer), log_dir, consumed=role_counts["reviewer"],
                    store_root=self.store.root,
                ),
                "architect": MockAgentExecutor(
                    list(architect), log_dir, consumed=role_counts["architect"],
                    store_root=self.store.root,
                ),
                "shadow_reviewer": MockAgentExecutor(
                    list(shadow_reviewer),
                    self.store.run_dir(self.run_id) / "shadow" / "_adapter_logs",
                    consumed=role_counts["shadow_reviewer"],
                    store_root=self.store.root,
                ),
            },
            verb_writer=MockVerbWriter(
                self.project, verbs or {}, consumed=verb_counts or None,
                store_root=self.store.root,
            ),
            verifier=Verifier(self.store, self.runner, self.clock),
            verification_plan=VerificationPlan(
                commands=(VerificationCommand(argv=("fake-validate",)),)
            ),
            watcher=Watcher(self.clock, sleep),
            workspace=WorkspaceManager(self.project, self.store.root),
            clock=self.clock,
            watch_timeout_seconds=watch_timeout,
            round4_authority=round4_authority,
            role_efforts=role_efforts,
            memory_hooks=memory_hooks,
            sleep=sleep,
        )

    def events(self):
        return self.store.read_events(self.run_id)

    def event_kinds(self):
        return [event["kind"] for event in self.events()]

    def counters(self):
        return BudgetCounters.from_events(self.events())


DEFAULT_VERBS = {
    "make-review-prompt": (
        (REVIEW_PROMPT, "# Review M001-S01\n\n```yaml\nround: 1\n```\n"),
    ),
    "record-verdict": (
        (VERDICT_RECORD, "# Verdict Record\n\npass\n"),
    ),
}


def clean_pass_states():
    return [
        payload("ready", "execute_coding_prompt"),
        payload("ready", "make_review_prompt", actor="orchestrator",
                review_prompt=REVIEW_PROMPT),
        payload(
            "ready",
            "execute_review_prompt",
            actor="reviewer",
            review_prompt=REVIEW_PROMPT,
            review_report=REVIEW_REPORT,
        ),
        payload(
            "ready",
            "record_verdict",
            actor="orchestrator",
            review_prompt=REVIEW_PROMPT,
            review_report=REVIEW_REPORT,
            verdict_record=VERDICT_RECORD,
            verdict={
                "value": "pass",
                "next_move": "advance",
                "report": REVIEW_REPORT,
            },
        ),
        payload(
            "ready",
            "frontier_recorded",
            actor="human",
            review_prompt=REVIEW_PROMPT,
            review_report=REVIEW_REPORT,
            verdict_record=VERDICT_RECORD,
            verdict={
                "value": "pass",
                "next_move": "advance",
                "report": REVIEW_REPORT,
            },
        ),
    ]
