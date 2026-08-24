"""Real-frutlups second-pass worklist regression lane (M006-S03).

The driven project is disposable, seats are deterministic local mocks, and
every planning observation and governed artifact write goes through the
installed released frutlups interpreter.  No provider, network, credential,
or paid surface is involved.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

from frutlups_drive.cli import (
    _build_launch_identity,
    _build_supervisor,
    _compile_mock_script,
    main,
)
from frutlups_drive.contracts import (
    AgentRunRequest,
    AgentRunResult,
    ExitCode,
    LoopStep,
    PlanOutcome,
    StopReason,
)
from frutlups_drive.frutlupscli import BINDING_SCHEMA_VERSION, load_launch_binding
from frutlups_drive.planstate import Frontier
from frutlups_drive.policy import load_execution_policy
from frutlups_drive.runstore import RunStore

from test_frutlups_e2e import FIXTURE, FRUTLUPS_PYTHON, _candidate_capability


SELF_REPORT = """# Coder Self-Report

## Intent

Complete the declared holistic rework.

## Files Changed

- alpha_module.py

## Behavior Implemented

The declared finding was corrected.

## Tests Added Or Updated

- existing verification lane

## Verification Run

All green.

## Definition Of Done Audit

The rework prompt is complete.

## Non-Goals Confirmed

No unrelated work was performed.

## Deviations From Prompt

none

## Memory Used

none

## Memory Update Requested

none

## Known Limits / Follow-Up

None.

## Recommended Next Move

Independent review.
"""

PASS_REPORT = """# Review Report

## Findings

None; the fresh rework chain is accepted.

Verdict: pass - next: record the verdict and advance
"""

NEEDS_WORK_REPORT = """# Review Report

## Findings

One material finding requires a fresh corrective round.

Verdict: needs_work - next: correct the finding and re-review
"""

LIVE_SHAPE_LAYOUT = """schema_version: frutlups_layout_config_v0
profile_id: artifact_first_template_v2
prompts:
  coding_prompt_dir: "prompts/for_coding_agent"
  review_prompt_dir: "prompts/for_review_agent"
  coding_template: ""
  review_template: ""
  numbering: "zero-padded sequential"
  section_roles:
    self_report: "required self-report"
  metadata:
    parse_front_matter: true
    milestone_field: "milestone"
    slice_field: "slice"
    title_field: "title"
reports:
  reviews_dir: "05_governance/reviews"
  self_report_suffix: "_self_report.md"
  review_report_suffix: "_review_report.md"
  verdict_record_suffix: "_verdict_record.md"
  self_report_required_headings:
    - "Intent"
    - "Files Changed"
    - "Behavior Implemented"
    - "Tests Added Or Updated"
    - "Verification Run"
    - "Definition Of Done Audit"
    - "Non-Goals Confirmed"
    - "Deviations From Prompt"
    - "Memory Used"
    - "Memory Update Requested"
    - "Known Limits / Follow-Up"
    - "Recommended Next Move"
"""

LIVE_SHAPE_PRIOR_CODING_PATH = (
    "prompts/for_coding_agent/" "014_frutlups_m002_s01_plan_loading.md"
)
LIVE_SHAPE_PRIOR_SELF_REPORT_PATH = (
    "05_governance/reviews/" "m002_s01_plan_loading_self_report.md"
)
LIVE_SHAPE_PRIOR_REVIEW_REPORT_PATH = (
    "05_governance/reviews/" "m002_s01_plan_loading_review_report.md"
)
LIVE_SHAPE_REWORK_SELF_REPORT_PATH = (
    "05_governance/reviews/"
    "m002_s01_plan_loading_rework_001_holistic_pass_001_015_self_report.md"
)

LIVE_SHAPE_CODING_PROMPT = f"""# Coding Prompt 014: M002-S01 plan loading

Workflow metadata:

```yaml
milestone: M002
slice: M002-S01
title: plan loading
role: coder
```

## Task

The original plan-loading slice is already accepted.

## Required Self-Report

Write the self-report at `{LIVE_SHAPE_PRIOR_SELF_REPORT_PATH}`.
"""

LIVE_SHAPE_REVIEW_PROMPT = f"""# Review Prompt 014: M002-S01 plan loading

Workflow metadata:

```yaml
milestone: M002
slice: M002-S01
title: plan loading
role: reviewer
round: 1
```

## Review Checks

Reviewed coding prompt: `{LIVE_SHAPE_PRIOR_CODING_PATH}`
"""

LIVE_SHAPE_REWORK_CODING_PATH = (
    "prompts/for_coding_agent/"
    "015_frutlups_m002_s01_plan_loading_rework_001_holistic_pass_001_015.md"
)
LIVE_SHAPE_REWORK_REVIEW_PATH = (
    "prompts/for_review_agent/"
    "015_review_frutlups_m002_s01_plan_loading_rework_001_"
    "holistic_pass_001_015.md"
)
LIVE_SHAPE_REWORK_REVIEW_REPORT_PATH = (
    "05_governance/reviews/"
    "m002_s01_plan_loading_rework_001_holistic_pass_001_015_review_report.md"
)

LIVE_SHAPE_REWORK_CODING_PROMPT = f"""# Coding Prompt 015: M002-S01 plan loading rework

Workflow metadata:

```yaml
milestone: M002
slice: M002-S01
title: plan loading
role: coder
```

## Task

Re-audit the previously accepted slice for holistic pass 001.

## Required Self-Report

Write the self-report at `{LIVE_SHAPE_REWORK_SELF_REPORT_PATH}`.
"""

LIVE_SHAPE_REWORK_REVIEW_PROMPT = f"""# Review Prompt 015: M002-S01 plan loading rework

Workflow metadata:

```yaml
milestone: M002
slice: M002-S01
title: plan loading
role: reviewer
round: 1
```

## Review Checks

Reviewed coding prompt: `{LIVE_SHAPE_REWORK_CODING_PATH}`
Coder self-report: `{LIVE_SHAPE_REWORK_SELF_REPORT_PATH}`

## Definition Of Done

- Write the review report at `{LIVE_SHAPE_REWORK_REVIEW_REPORT_PATH}`.
"""

# The authority-free live shape exactly as committed before the 0.1.5
# correction: no review-output declaration in any recognized form.  Corrected
# frutlups intentionally fails closed on it; the drive must never accept it.
LIVE_SHAPE_REWORK_REVIEW_PROMPT_UNDECLARED = f"""# Review Prompt 015: M002-S01 plan loading rework

Workflow metadata:

```yaml
milestone: M002
slice: M002-S01
title: plan loading
role: reviewer
round: 1
```

## Review Checks

Reviewed coding prompt: `{LIVE_SHAPE_REWORK_CODING_PATH}`
Coder self-report: `{LIVE_SHAPE_REWORK_SELF_REPORT_PATH}`
"""

LIVE_SHAPE_VERDICT_RECORD = f"""# Verdict Record: M002-S01

## Source

Review report: `{LIVE_SHAPE_PRIOR_REVIEW_REPORT_PATH}`

## Slice

Slice ID: `M002-S01`
Title: plan loading
Milestone: `M002`

## Parsed Verdict

Verdict: `pass`

## Next Action

Kind: `milestone_complete`
Next slice: none
Message: slice M002-S01 accepted; milestone M002 appears complete
"""


class _SimulatedDeclarationCrash(Exception):
    pass


class _PlanningSequence:
    def __init__(self, *states):
        self._states = list(states)

    def read_planning_state(self):
        return self._states.pop(0)


class _RecordingPlanProvider:
    def __init__(self, provider):
        self._provider = provider
        self.states = []

    def read_planning_state(self):
        state = self._provider.read_planning_state()
        self.states.append(state)
        return state


class _SecondPassExecutor:
    """Request-shaped mock seat; artifact paths come only from the request."""

    def __init__(
        self,
        store: RunStore,
        run_id: str,
        findings: tuple[str, ...],
        *,
        needs_work_first: bool = False,
        second_findings: tuple[str, ...] = (),
    ):
        self._store = store
        self._run_id = run_id
        self._worklists = (findings, second_findings)
        self._needs_work_first = needs_work_first

    def execute(self, request: AgentRunRequest) -> AgentRunResult:
        expected = tuple(Path(path) for path in request.expected_artifacts)
        if len(expected) != 1:
            raise AssertionError("the regression seat expects one governed output")
        relative = expected[0]
        if relative.name == "holistic_review.json":
            completed = sum(
                event.get("kind") == "holistic_review"
                for event in self._store.read_events(self._run_id)
            )
            findings = (
                self._worklists[completed]
                if completed < len(self._worklists)
                else ()
            )
            content = json.dumps({"findings": list(findings)})
        elif request.role.value == "coder":
            content = SELF_REPORT
        else:
            prior_reviews = sum(
                event.get("kind") == "collected"
                and event.get("role") == "reviewer"
                and not str(event.get("slice", "")).startswith("holistic_pass_")
                for event in self._store.read_events(self._run_id)
            )
            content = (
                NEEDS_WORK_REPORT
                if self._needs_work_first and prior_reviews == 0
                else PASS_REPORT
            )

        target = Path(request.workspace) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        log_root = self._store.run_dir(self._run_id) / "adapter_logs"
        log_root.mkdir(parents=True, exist_ok=True)
        identity = hashlib.sha256(
            f"{request.workspace}\0{request.attempt_id}".encode("utf-8")
        ).hexdigest()[:16]
        log_path = log_root / f"second_pass_{identity}.jsonl"
        log_path.write_text('{"event":"mock dispatch"}\n', encoding="utf-8")
        return AgentRunResult(
            status="completed",
            event_log_path=log_path,
            changed_files=(relative,),
            produced_artifacts=expected,
            exit_reason="mock_completed",
            tokens_in=None,
            tokens_out=None,
            cost_usd=0.0,
        )


class SecondPassRealPlanningTests(unittest.TestCase):
    def setUp(self):
        reason = _candidate_capability()
        if reason:
            self.skipTest(reason)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _project(self, name: str, *, prepare: bool = True) -> Path:
        project = self.root / name
        shutil.copytree(FIXTURE, project)
        binding = project / "local_state/frutlups_binding.toml"
        binding.parent.mkdir(parents=True)
        escaped = str(FRUTLUPS_PYTHON).replace(chr(92), chr(92) * 2)
        binding.write_text(
            f'schema_version = "{BINDING_SCHEMA_VERSION}"\n'
            "[launch]\n"
            f'argv_prefix = ["{escaped}", "-m", "frutlups"]\n'
            'tool_identity = "frutlups==0.1.4"\n'
            "[env]\n",
            encoding="utf-8",
        )
        if prepare:
            prepared = main(["run", str(project), "--until", "milestone_complete"])
            self.assertEqual(
                prepared,
                int(ExitCode.OK),
                RunStore(project / ".frutlups_drive").read_events("run_001"),
            )
            shutil.rmtree(project / ".frutlups_drive")
        policy = project / "frutlups_drive.toml"
        policy.write_text(
            policy.read_text(encoding="utf-8")
            + "\n[target]\nmax_passes = 4\nmax_slices = 10\n"
            + "\n[autonomy]\npass_boundary = \"two_clean\"\n"
            + "auto_continue_past_frontier_recorded = true\n",
            encoding="utf-8",
        )
        return project

    def _live_shape_project(self) -> Path:
        project = self._project("live_shape", prepare=False)
        (project / "03_experiments/active_roadmap_fixture.md").write_text(
            "# Fixture Active Roadmap\n\n"
            "### M002: Live Shape Milestone\n\nStatus: active\n",
            encoding="utf-8",
        )
        (project / "03_experiments/development_roadmap_fixture.md").write_text(
            "# Fixture Development Roadmap\n\n"
            "### M002: Live Shape Milestone\n\nStatus: active\n\n"
            "Slices:\n\n- M002-S01: plan loading\n",
            encoding="utf-8",
        )
        (project / "frutlups.layout.yaml").write_text(
            LIVE_SHAPE_LAYOUT, encoding="utf-8"
        )
        for relative in (
            "prompts/for_coding_agent",
            "prompts/for_review_agent",
            "05_governance/reviews",
        ):
            shutil.rmtree(project / relative)
            (project / relative).mkdir(parents=True)
        (project / "05_governance/reviews/INDEX.md").write_text(
            "# Review Index\n\n"
            "| Milestone | Slice | Round | Self-Report | Review Prompt | "
            "Review Report | Verdict | Commit |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- |\n",
            encoding="utf-8",
        )
        (project / LIVE_SHAPE_PRIOR_CODING_PATH).write_text(
            LIVE_SHAPE_CODING_PROMPT, encoding="utf-8"
        )
        (project / (
            "prompts/for_review_agent/" "014_review_frutlups_m002_s01_plan_loading.md"
        )).write_text(
            LIVE_SHAPE_REVIEW_PROMPT, encoding="utf-8"
        )
        (project / LIVE_SHAPE_PRIOR_SELF_REPORT_PATH).write_text(
            SELF_REPORT, encoding="utf-8"
        )
        (project / LIVE_SHAPE_PRIOR_REVIEW_REPORT_PATH).write_text(
            PASS_REPORT, encoding="utf-8"
        )
        (project / (
            "05_governance/reviews/" "m002_s01_plan_loading_verdict_record.md"
        )).write_text(
            LIVE_SHAPE_VERDICT_RECORD, encoding="utf-8"
        )
        return project

    def _live_shape_supervisor(
        self, review_prompt: str = LIVE_SHAPE_REWORK_REVIEW_PROMPT
    ):
        project = self._live_shape_project()
        supervisor, store = self._supervisor(
            project,
            ("M002-S01",),
            accepted_slices=("M002-S01",),
        )

        # The tiny fixture has no historical v2 prompt template.  Materialize
        # the frozen campaign's metadata after the real governed write so all
        # planning decisions still come from released frutlups behavior.
        def materialize_live_metadata(stage: str, verb: str) -> None:
            if stage != "written":
                return
            if verb == "make-coding-prompt":
                (project / LIVE_SHAPE_REWORK_CODING_PATH).write_text(
                    LIVE_SHAPE_REWORK_CODING_PROMPT, encoding="utf-8"
                )
            elif verb == "make-review-prompt":
                (project / LIVE_SHAPE_REWORK_REVIEW_PATH).write_text(
                    review_prompt, encoding="utf-8"
                )

        supervisor._verb_writer._hook = materialize_live_metadata
        return supervisor, store

    def _advance_live_shape_to_fresh_verdict(
        self, supervisor, store: RunStore
    ) -> tuple[dict, ...]:
        for _ in range(20):
            result = supervisor.tick()
            self.assertEqual(result.kind, "acted", store.read_events("run_001"))
            events = store.read_events("run_001")
            if any(
                event.get("kind") == "verb"
                and event.get("verb") == "record-verdict"
                and event.get("slice") == "M002-S01"
                and event.get("fixture_prior_acceptance") is not True
                for event in events
            ):
                return events
        self.fail("the fresh accepted chain did not record its verdict")

    def _supervisor(
        self,
        project: Path,
        findings: tuple[str, ...],
        *,
        needs_work_first: bool = False,
        second_findings: tuple[str, ...] = (),
        accepted_slices: tuple[str, ...] = (
            "M001-S01",
            "M001-S02",
            "M001-S03",
        ),
    ):
        policy_path = project / "frutlups_drive.toml"
        policy = load_execution_policy(policy_path).policy
        compiled = _compile_mock_script(project / ".frutlups_drive_mock")
        binding = load_launch_binding(project / "local_state/frutlups_binding.toml")
        identity = _build_launch_identity(
            project, policy, binding, policy_path.read_bytes()
        )
        store = RunStore(project / ".frutlups_drive")
        if not store.run_exists("run_001"):
            store.create_run(
                "run_001",
                {
                    "boundary": "roadmap_complete",
                    "contract_version": 1,
                    **identity.manifest_facts(),
                },
            )
            store.append_event(
                "run_001",
                {
                    "kind": "run_created",
                    "t": time.time(),
                    "boundary": "roadmap_complete",
                },
            )
            for slice_id in accepted_slices:
                store.append_event(
                    "run_001",
                    {
                        "kind": "verb",
                        "t": time.time(),
                        "verb": "record-verdict",
                        "artifact": (
                            "05_governance/reviews/"
                            f"{slice_id.lower()}_accepted_verdict.md"
                        ),
                        "slice": slice_id,
                        "fixture_prior_acceptance": True,
                    },
                )
        supervisor = _build_supervisor(
            project,
            store,
            "run_001",
            policy,
            "roadmap_complete",
            compiled,
            binding=binding,
            launch_identity=identity,
        )
        executor = _SecondPassExecutor(
            store,
            "run_001",
            findings,
            needs_work_first=needs_work_first,
            second_findings=second_findings,
        )
        supervisor._executors["coder"] = executor
        supervisor._executors["reviewer"] = executor
        return supervisor, store

    def test_mixed_finding_ids_repair_valid_subset_and_reach_two_clean(self):
        project = self._project("mixed_ids")
        supervisor, store = self._supervisor(
            project, ("M000", "M001-S02", "M999")
        )

        result = supervisor.run_until()

        self.assertEqual((result.kind, result.detail), ("boundary", "complete"))
        events = store.read_events("run_001")
        declaration = [
            event
            for event in events
            if event.get("kind") == "verb"
            and event.get("verb") == "declare-rework"
        ]
        self.assertEqual(len(declaration), 1)
        self.assertEqual(declaration[0]["slices"], ["M001-S02"])
        unmappable = [
            event
            for event in events
            if event.get("kind") == "holistic_finding_unmappable"
        ]
        self.assertEqual(
            [event["finding_id"] for event in unmappable], ["M000", "M999"]
        )

    def test_all_invalid_finding_ids_stop_before_real_verb_transaction(self):
        project = self._project("all_invalid_ids")
        supervisor, store = self._supervisor(project, ("M000", "M999"))
        original_invoke = supervisor._verb_writer.invoke
        calls = []

        def recording_invoke(*args, **kwargs):
            calls.append((args, kwargs))
            return original_invoke(*args, **kwargs)

        supervisor._verb_writer.invoke = recording_invoke

        result = supervisor.run_until()

        self.assertEqual(result.stop_reason, StopReason.HOLISTIC_FINDINGS_UNMAPPABLE)
        self.assertEqual(calls, [])
        self.assertFalse(
            (store.run_dir("run_001") / "pending_verb.json").exists()
        )

    def test_duplicate_valid_finding_id_declares_once(self):
        project = self._project("duplicate_ids")
        supervisor, store = self._supervisor(
            project, ("M001-S02", "M001-S02")
        )

        result = supervisor.run_until()

        self.assertEqual((result.kind, result.detail), ("boundary", "complete"))
        declaration = [
            event
            for event in store.read_events("run_001")
            if event.get("kind") == "verb"
            and event.get("verb") == "declare-rework"
        ]
        self.assertEqual(len(declaration), 1)
        self.assertEqual(declaration[0]["slices"], ["M001-S02"])

    def test_single_slice_rework_reaches_two_clean(self):
        project = self._project("single")
        supervisor, store = self._supervisor(project, ("M001-S02",))
        result = supervisor.run_until()
        self.assertEqual(
            (result.kind, result.detail),
            ("boundary", "complete"),
            store.read_events("run_001"),
        )
        events = store.read_events("run_001")
        self.assertEqual(
            [event["clean"] for event in events if event["kind"] == "holistic_review"],
            [False, True, True],
        )
        self.assertEqual(
            [event["verb"] for event in events if event["kind"] == "verb"].count(
                "declare-rework"
            ),
            1,
        )
        self.assertFalse(
            any(event["kind"] == "slice_complete" for event in events),
            "released planning clears rework through accepted verdicts",
        )
        self.assertEqual(supervisor._missing_worklist_slices(), ())

    def test_live_shape_prior_acceptance_and_sequence_015_clear_declaration(self):
        supervisor, store = self._live_shape_supervisor()
        events = self._advance_live_shape_to_fresh_verdict(supervisor, store)
        coding = [
            event["artifact"]
            for event in events
            if event.get("kind") == "verb"
            and event.get("verb") == "make-coding-prompt"
        ]
        review = [
            event["artifact"]
            for event in events
            if event.get("kind") == "verb"
            and event.get("verb") == "make-review-prompt"
        ]
        self.assertEqual(
            coding,
            [LIVE_SHAPE_REWORK_CODING_PATH],
        )
        self.assertEqual(
            review,
            [LIVE_SHAPE_REWORK_REVIEW_PATH],
        )

        state = supervisor._plan_provider.read_planning_state()
        self.assertEqual(
            (state.outcome.value, state.step.value if state.step else None),
            ("complete", "no_frontier"),
            "the fresh accepted chain must clear the declaration at roadmap end",
        )

    def test_live_shape_declaration_free_review_prompt_fails_closed(self):
        supervisor, store = self._live_shape_supervisor(
            review_prompt=LIVE_SHAPE_REWORK_REVIEW_PROMPT_UNDECLARED
        )
        result = None
        for _ in range(20):
            result = supervisor.tick()
            if result.kind != "acted":
                break
        events = store.read_events("run_001")
        # Released 0.1.5 fails closed on the authority-free prompt the moment
        # the governed make-review-prompt write lands: planning goes typed
        # invalid, so the drive's post-state certification refuses the write
        # and stops governed with preserved evidence.
        self.assertEqual(
            (result.kind, result.stop_reason.value if result.stop_reason else None),
            ("stopped", "provider_failure"),
            events,
        )
        self.assertEqual(
            result.detail,
            "governed verb transaction failed: verb_post_state_invalid",
        )

        state = supervisor._plan_provider.read_planning_state()
        self.assertEqual(
            (state.outcome.value, state.step.value if state.step else None),
            ("invalid", "no_frontier"),
        )
        self.assertIn(
            "invalid rework evidence state: rework_review_output_declaration_unrecognized",
            [diagnostic.message for diagnostic in state.diagnostics],
        )
        # The authority-free prompt never clears the fresh chain's
        # declaration: no completion, no slice completion, no verdict.
        self.assertNotEqual(state.outcome.value, "complete")
        self.assertFalse(
            any(event["kind"] == "slice_complete" for event in events)
        )
        self.assertFalse(
            any(
                event.get("kind") == "verb" and event.get("verb") == "record-verdict"
                and event.get("fixture_prior_acceptance") is not True
                for event in events
            )
        )

    def test_stuck_live_shape_frontier_stops_on_no_progress_budget(self):
        supervisor, store = self._live_shape_supervisor()
        events = self._advance_live_shape_to_fresh_verdict(supervisor, store)
        self.assertEqual(
            [
                event["artifact"]
                for event in events
                if event.get("kind") == "verb"
                and event.get("verb") == "make-coding-prompt"
            ],
            [LIVE_SHAPE_REWORK_CODING_PATH],
        )
        self.assertEqual(
            [
                event["artifact"]
                for event in events
                if event.get("kind") == "verb"
                and event.get("verb") == "make-review-prompt"
            ],
            [LIVE_SHAPE_REWORK_REVIEW_PATH],
        )
        # Released 0.1.5 clears the declared live shape, so the stuck source
        # is scripted deterministically instead of coming from released
        # planning behavior: one identical ready/frontier_recorded state for
        # the completed slice on every subsequent read.
        cleared = supervisor._plan_provider.read_planning_state()
        self.assertEqual(
            (cleared.outcome.value, cleared.step.value if cleared.step else None),
            ("complete", "no_frontier"),
            "released planning clears the declared live shape after the fresh verdict",
        )
        stuck = replace(
            cleared,
            outcome=PlanOutcome.READY,
            step=LoopStep.FRONTIER_RECORDED,
            frontier=Frontier(
                milestone_id="M002",
                slice_id="M002-S01",
                slice_title="plan loading",
                round=1,
            ),
            completion_evidence=None,
        )
        supervisor._plan_provider = _PlanningSequence(*([stuck] * 10))

        result = supervisor.run_until()

        self.assertEqual(
            (result.kind, result.stop_reason.value if result.stop_reason else None),
            ("stopped", "no_progress"),
        )
        events = store.read_events("run_001")
        self.assertEqual(
            [event["slice"] for event in events if event["kind"] == "slice_complete"],
            ["M002-S01"],
        )
        stalled = [
            event
            for event in events
            if event.get("kind") == "tick"
            and event.get("detail") == "frontier_unchanged"
        ]
        self.assertEqual(
            len(stalled),
            supervisor._policy.limits.max_consecutive_no_progress,
        )
        self.assertTrue(
            all(
                event.get("progress") is False
                and event.get("consumed") is False
                for event in stalled
            )
        )
        stop = [event for event in events if event["kind"] == "stop"][-1]
        self.assertEqual(
            (stop["reason"], stop["detail"]),
            ("no_progress", "budget:consecutive_loop_iterations"),
        )

    def test_normal_multi_slice_roll_keeps_distinct_completions(self):
        project = self._project("normal_roll", prepare=False)
        supervisor, store = self._supervisor(project, ())
        first = supervisor._plan_provider.read_planning_state()
        self.assertIsNotNone(first.frontier)
        first_recorded = replace(first, step=LoopStep.FRONTIER_RECORDED)
        second_recorded = replace(
            first_recorded,
            frontier=replace(first.frontier, slice_id="M001-S03"),
        )
        supervisor._plan_provider = _PlanningSequence(first_recorded, second_recorded)

        self.assertEqual(supervisor.tick().detail, "continue_past_frontier")
        self.assertEqual(supervisor.tick().detail, "continue_past_frontier")

        events = store.read_events("run_001")

        completions = [
            event["slice"] for event in events if event["kind"] == "slice_complete"
        ]
        self.assertEqual(completions, ["M001-S02", "M001-S03"])
        self.assertFalse(
            any(
                event.get("kind") == "tick"
                and event.get("detail") == "frontier_unchanged"
                for event in events
            )
        )

    def test_multi_slice_rework_uses_canonical_order_and_disjoint_windows(self):
        project = self._project("multi")
        supervisor, store = self._supervisor(
            project, ("M001-S03", "M001-S02")
        )
        result = supervisor.run_until()
        self.assertEqual((result.kind, result.detail), ("boundary", "complete"))
        events = store.read_events("run_001")
        declaration = [
            event
            for event in events
            if event.get("kind") == "verb"
            and event.get("verb") == "declare-rework"
        ]
        self.assertEqual(len(declaration), 1)
        self.assertEqual(declaration[0]["slices"], ["M001-S03", "M001-S02"])
        coding = [
            event
            for event in events
            if event.get("kind") == "verb"
            and event.get("verb") == "make-coding-prompt"
        ]
        review = [
            event
            for event in events
            if event.get("kind") == "verb"
            and event.get("verb") == "make-review-prompt"
        ]
        self.assertEqual([event["slice"] for event in coding], ["M001-S02", "M001-S03"])
        self.assertEqual(
            [Path(event["artifact"]).name.split("_", 1)[0] for event in coding],
            ["007", "009"],
        )
        self.assertEqual(
            [Path(event["artifact"]).name.split("_", 1)[0] for event in review],
            ["008", "010"],
        )

    def test_needs_work_rework_retries_before_acceptance(self):
        project = self._project("retry")
        supervisor, store = self._supervisor(
            project, ("M001-S02",), needs_work_first=True
        )
        planning = _RecordingPlanProvider(supervisor._plan_provider)
        supervisor._plan_provider = planning
        result = supervisor.run_until()
        self.assertEqual(
            (result.kind, result.detail),
            ("boundary", "complete"),
            store.read_events("run_001"),
        )
        events = store.read_events("run_001")
        verbs = [
            event["verb"]
            for event in events
            if event["kind"] == "verb"
            and event.get("fixture_prior_acceptance") is not True
        ]
        self.assertEqual(verbs.count("declare-rework"), 1)
        self.assertEqual(verbs.count("make-coding-prompt"), 2)
        self.assertEqual(verbs.count("make-review-prompt"), 2)
        self.assertEqual(verbs.count("record-verdict"), 1)
        self.assertEqual(supervisor._missing_worklist_slices(), ())
        corrective_frontier_rounds = [
            state.frontier.round
            for state in planning.states
            if state.frontier is not None
            and state.frontier.slice_id == "M001-S02"
            and state.step in (LoopStep.MAKE_REVIEW_PROMPT, LoopStep.EXECUTE_REVIEW_PROMPT)
            and state.artifacts.coding_prompt is not None
        ]
        self.assertTrue(corrective_frontier_rounds)
        self.assertEqual(
            set(corrective_frontier_rounds),
            {1},
            "released frutlups must not carry an accepted lifecycle's round forward",
        )

    def test_consecutive_nonclean_passes_get_distinct_declarations(self):
        project = self._project("consecutive")
        supervisor, store = self._supervisor(
            project,
            ("M001-S02",),
            second_findings=("M001-S03",),
        )
        result = supervisor.run_until()
        self.assertEqual((result.kind, result.detail), ("boundary", "complete"))
        events = store.read_events("run_001")
        declarations = [
            event
            for event in events
            if event.get("kind") == "verb"
            and event.get("verb") == "declare-rework"
        ]
        self.assertEqual(
            [event["pass_id"] for event in declarations],
            ["holistic_pass_001", "holistic_pass_002"],
        )
        self.assertEqual(
            [event["slices"] for event in declarations],
            [["M001-S02"], ["M001-S03"]],
        )
        self.assertEqual(
            [event["clean"] for event in events if event["kind"] == "holistic_review"],
            [False, False, True, True],
        )

    def test_crash_resume_across_declaration_write_is_idempotent(self):
        project = self._project("resume")
        supervisor, _ = self._supervisor(project, ("M001-S02",))

        def crash_after_write(stage: str, verb: str) -> None:
            if stage == "written" and verb == "declare-rework":
                raise _SimulatedDeclarationCrash(stage)

        supervisor._verb_writer._hook = crash_after_write
        with self.assertRaises(_SimulatedDeclarationCrash):
            supervisor.run_until()

        resumed, store = self._supervisor(project, ("M001-S02",))
        self.assertIsNone(resumed.resume())
        result = resumed.run_until()
        self.assertEqual((result.kind, result.detail), ("boundary", "complete"))
        events = store.read_events("run_001")
        declarations = [
            event
            for event in events
            if event.get("kind") == "verb"
            and event.get("verb") == "declare-rework"
        ]
        self.assertEqual(len(declarations), 1)
        self.assertEqual(declarations[0]["pass_id"], "holistic_pass_001")
        self.assertEqual(declarations[0]["slices"], ["M001-S02"])
        self.assertFalse(
            (store.run_dir("run_001") / "pending_verb.json").exists()
        )


if __name__ == "__main__":
    unittest.main()
