"""Wall-clock-bounded M004 Phase A/B endurance and recovery family."""

import json
import math
import tempfile
import time
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

from frutlups_drive import killswitch
from frutlups_drive.contracts import StopReason
from frutlups_drive.dispatch.mock import MockAgentAction
from frutlups_drive.runstore import RESOLUTION_MARKER_SUFFIX
from frutlups_drive.telemetry import derive_report
from frutlups_drive.workspace import WorkspaceManager

from _scenario import (
    CODING_PROMPT,
    DEFAULT_VERBS,
    ACTIVE_ROADMAP,
    ROADMAP_BODY,
    REVIEW_PROMPT,
    REVIEW_REPORT,
    SELF_REPORT,
    VERDICT_RECORD,
    Scenario,
    payload,
)
from test_crash_resume import SimulatedCrash, crash_hook


STORE_LIMIT = 4 * 1024 * 1024


def long_scenario_kwargs(slice_count=12, induced=True, shadow=False):
    states = []
    coder = []
    reviewer = []
    shadow_reviewer = []
    review_verbs = []
    verdict_verbs = []
    for index in range(1, slice_count + 1):
        slice_id = f"M100-S{index:02d}"
        self_report = f"05_governance/reviews/m100/{slice_id}_self_report.md"
        review_prompt = f"prompts/for_review_agent/{index:03d}_{slice_id}_review.md"
        review_report = f"05_governance/reviews/m100/{slice_id}_review_report.md"
        verdict_record = f"05_governance/reviews/m100/{slice_id}_verdict.md"
        coding = payload(
            "ready",
            "execute_coding_prompt",
            slice_id=slice_id,
            milestone="M100",
            coding_prompt=CODING_PROMPT,
            self_report=self_report,
        )
        if induced and index == 3:
            states.append(coding)
            coder.append(MockAgentAction(raise_error=True))
        if induced and index == 7:
            states.append(coding)
            coder.append(
                MockAgentAction(status="timeout", exit_reason="induced_timeout")
            )
        states.append(coding)
        coder.append(
            MockAgentAction(
                writes=((self_report, f"# Coder Self-Report {slice_id}\n"),)
            )
        )
        states.extend(
            [
                payload(
                    "ready",
                    "make_review_prompt",
                    slice_id=slice_id,
                    milestone="M100",
                    coding_prompt=CODING_PROMPT,
                    self_report=self_report,
                    review_prompt=review_prompt,
                    actor="orchestrator",
                ),
                payload(
                    "ready",
                    "execute_review_prompt",
                    slice_id=slice_id,
                    milestone="M100",
                    coding_prompt=CODING_PROMPT,
                    self_report=self_report,
                    review_prompt=review_prompt,
                    review_report=review_report,
                    actor="reviewer",
                ),
                payload(
                    "ready",
                    "record_verdict",
                    slice_id=slice_id,
                    milestone="M100",
                    coding_prompt=CODING_PROMPT,
                    self_report=self_report,
                    review_prompt=review_prompt,
                    review_report=review_report,
                    verdict_record=verdict_record,
                    verdict={
                        "value": "pass",
                        "next_move": "advance",
                        "report": review_report,
                    },
                    actor="orchestrator",
                ),
                payload(
                    "ready",
                    "frontier_recorded",
                    slice_id=slice_id,
                    milestone="M100",
                    coding_prompt=CODING_PROMPT,
                    self_report=self_report,
                    review_prompt=review_prompt,
                    review_report=review_report,
                    verdict_record=verdict_record,
                    verdict={
                        "value": "pass",
                        "next_move": "advance",
                        "report": review_report,
                    },
                    actor="human",
                ),
            ]
        )
        reviewer.append(
            MockAgentAction(
                writes=((review_report, f"Verdict: pass for {slice_id}\n"),)
            )
        )
        if shadow:
            shadow_reviewer.append(
                MockAgentAction(
                    writes=(("shadow_report.md", f"shadow {slice_id}\n"),),
                    cost_usd=0.01,
                )
            )
        review_verbs.append((review_prompt, f"# Review {slice_id}\n"))
        verdict_verbs.append((verdict_record, "pass\n"))
    states.append(
        payload(
            "complete",
            None,
            frontier_present=False,
            actor="none",
            completion_evidence={
                "path": "05_governance/reviews/m100/closure.md"
            },
        )
    )
    return {
        "states": states,
        "coder": coder,
        "reviewer": reviewer,
        "verbs": {
            "make-review-prompt": tuple(review_verbs),
            "record-verdict": tuple(verdict_verbs),
        },
        "boundary": "roadmap_complete",
        "policy_body": (
            "[target]\nmax_slices = 50\nmax_passes = 3\n"
            "[autonomy]\nauto_continue_past_frontier_recorded = true\n"
            "[limits]\n"
            "max_coder_attempts_per_slice = 3\n"
            "max_consecutive_provider_failures = 3\n"
            "max_consecutive_no_progress = 4\n"
            "provider_backoff_seconds = [0.1, 0.2, 0.4]\n"
            f"max_run_store_bytes = {STORE_LIMIT}\n"
            "max_retained_runs = 4\n"
            + (
                "[roles.shadow_reviewer]\n"
                "enabled = true\n"
                'adapter = "mock"\n'
                if shadow
                else ""
            )
        ),
        "shadow_reviewer": shadow_reviewer,
    }


def durable_outcome(scenario, result):
    events = scenario.events()
    collected = [
        (event.get("slice"), event.get("attempt"))
        for event in events
        if event["kind"] == "collected"
    ]
    return {
        "final": (result.kind, result.detail),
        "project": WorkspaceManager(
            scenario.project, scenario.store.root
        ).snapshot(scenario.project),
        "slices": scenario.counters().slices_completed,
        "passing_verifications": sum(
            1
            for event in events
            if event["kind"] == "verification" and event["passed"]
        ),
        "unique_collection": len(collected) == len(set(collected)),
    }


class EnduranceScenarioTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_long_multi_slice_run_recovers_failures_inside_store_policy(self):
        started = time.monotonic()
        scenario = Scenario(self.root, **long_scenario_kwargs(shadow=True))
        result = scenario.supervisor.run_until()
        elapsed = time.monotonic() - started
        self.assertEqual((result.kind, result.detail), ("boundary", "complete"))
        self.assertEqual(scenario.counters().slices_completed, 12)
        self.assertLess(elapsed, 30.0, "endurance lane is seconds-bounded")
        backoffs = [e for e in scenario.events() if e["kind"] == "backoff"]
        self.assertEqual([e["seconds"] for e in backoffs], [0.1, 0.1])
        self.assertEqual(scenario.store.run_size_bytes("run_001") < STORE_LIMIT, True)
        collected = [
            (e.get("slice"), e.get("attempt"))
            for e in scenario.events()
            if e["kind"] == "collected"
        ]
        self.assertEqual(len(collected), len(set(collected)))
        self.assertEqual(
            sum(e["kind"] == "shadow_review" for e in scenario.events()), 12
        )
        report = derive_report(scenario.store, "run_001")
        raw_attempts = []
        for slice_id in scenario.store.list_slices("run_001"):
            raw_attempts.extend(scenario.store.list_attempts("run_001", slice_id))
            raw_attempts.extend(
                scenario.store.list_shadow_attempts("run_001", slice_id)
            )
        raw_results = [
            scenario.store.read_result(attempt) or {} for attempt in raw_attempts
        ]
        self.assertEqual(report["summary"]["attempts"], len(raw_attempts))
        self.assertEqual(
            report["summary"]["tokens_in_known_sum"],
            sum(
                result["tokens_in"]
                for result in raw_results
                if result.get("tokens_in") is not None
            ),
        )
        self.assertEqual(
            report["summary"]["cost_usd_known_sum"],
            math.fsum(
                result["cost_usd"]
                for result in raw_results
                if result.get("cost_usd") is not None
            ),
        )

    def test_multi_slice_crash_matrix_resumes_to_uninterrupted_result(self):
        baseline_root = self.root / "baseline"
        baseline_root.mkdir()
        baseline = Scenario(
            baseline_root, **long_scenario_kwargs(slice_count=4, induced=False)
        )
        baseline_result = durable_outcome(
            baseline, baseline.supervisor.run_until()
        )
        self.assertEqual(baseline_result["final"], ("boundary", "complete"))

        for transition in ("planned", "started", "collected", "validated", "closed"):
            with self.subTest(transition=transition):
                root = self.root / f"crash_{transition}"
                root.mkdir()
                crashed = Scenario(
                    root,
                    transition_hook=crash_hook(transition),
                    **long_scenario_kwargs(slice_count=4, induced=False),
                )
                with self.assertRaises(SimulatedCrash):
                    crashed.supervisor.run_until()
                resumed = Scenario(
                    root,
                    project=crashed.project,
                    **long_scenario_kwargs(slice_count=4, induced=False),
                )
                stop = resumed.supervisor.resume()
                result = stop or resumed.supervisor.run_until()
                observed = durable_outcome(resumed, result)
                for key in ("final", "project", "slices", "passing_verifications"):
                    self.assertEqual(observed[key], baseline_result[key], key)
                self.assertTrue(observed["unique_collection"])
                self.assertLess(
                    resumed.store.run_size_bytes("run_001"), STORE_LIMIT
                )

    def test_control_stop_matrix_backoff_no_progress_rotation_and_watch(self):
        failure_root = self.root / "provider"
        failure_root.mkdir()
        provider = Scenario(
            failure_root,
            states=[payload(), payload()],
            coder=[
                MockAgentAction(raise_error=True),
                MockAgentAction(raise_error=True),
            ],
            policy_body=(
                "[limits]\nmax_consecutive_provider_failures = 2\n"
                "provider_backoff_seconds = [0.1]\n"
            ),
        )
        self.assertEqual(provider.supervisor.tick().detail, "provider_failure")
        self.assertEqual(provider.supervisor.tick().detail, "provider_failure")
        self.assertEqual(
            provider.supervisor.tick().stop_reason, StopReason.PROVIDER_FAILURE
        )
        self.assertEqual(
            [e["seconds"] for e in provider.events() if e["kind"] == "backoff"],
            [0.1],
        )

        progress_root = self.root / "progress"
        progress_root.mkdir()
        report = "05_governance/reviews/m001/m001_s01_self_report.md"
        no_progress = Scenario(
            progress_root,
            states=[payload(), payload()],
            coder=[
                MockAgentAction(writes=((report, "# report\n"),)),
                MockAgentAction(writes=((report, "# report\n"),)),
            ],
            verifier_exit_codes=[1, 1],
            policy_body="[limits]\nmax_consecutive_no_progress = 2\n",
        )
        no_progress.supervisor.tick()
        no_progress.supervisor.tick()
        self.assertEqual(
            no_progress.supervisor.tick().stop_reason, StopReason.NO_PROGRESS
        )

        rotation_root = self.root / "rotation"
        rotation_root.mkdir()
        old = Scenario(
            rotation_root,
            run_id="run_001",
            states=[
                payload(
                    "blocked",
                    None,
                    actor="human",
                    blocked={"citation": "05_governance/decision_log.md", "owner": "owner"},
                )
            ],
            policy_body="[limits]\nmax_retained_runs = 1\n",
        )
        old_stop = old.supervisor.tick()
        self.assertEqual(old_stop.stop_reason, StopReason.BLOCKED_VERDICT)
        active = Scenario(
            rotation_root,
            project=old.project,
            run_id="run_002",
            states=[payload()],
        )
        self.assertEqual(
            active.supervisor.tick().stop_reason, StopReason.RUN_STORE_FULL
        )
        self.assertTrue(active.store.run_dir("run_001").is_dir())
        self.assertTrue(active.store.run_dir("run_002").is_dir())
        old_stop.escalation_path.with_name(
            old_stop.escalation_path.name + RESOLUTION_MARKER_SUFFIX
        ).write_bytes(b"")
        released = active.store.enforce_limits(
            "run_002", max_total_bytes=STORE_LIMIT, max_retained_runs=1
        )
        self.assertEqual(released.deleted_runs, ("run_001",))

        watch_root = self.root / "watch"
        watch_root.mkdir()
        stop_once = {"done": False}

        def stop_during_sleep(scenario):
            if not stop_once["done"]:
                stop_once["done"] = True
                killswitch.request_stop(scenario.store.root)

        watched = Scenario(
            watch_root,
            states=[payload()],
            coder=[MockAgentAction(writes=(), produced_override=())],
            sleep_hook=stop_during_sleep,
            policy_body="[limits]\nwatch_poll_seconds = 0.2\n",
        )
        self.assertEqual(
            watched.supervisor.tick().stop_reason, StopReason.KILL_SWITCH
        )
        self.assertEqual(watched.clock.now(), 1000.2)

        owner_root = self.root / "owner_note"
        owner_root.mkdir()
        owner = Scenario(owner_root, states=[payload()])
        notes = owner.project / "05_governance/human_owner_notes"
        notes.mkdir(parents=True, exist_ok=True)
        (notes / "new_note.md").write_bytes(b"uninterpreted owner prose\n")
        self.assertEqual(
            owner.supervisor.tick().stop_reason, StopReason.OWNER_NOTE
        )

    def test_phase_b_reconciliation_freeze_worklist_and_two_clean_endures(self):
        started = time.monotonic()
        complete = payload(
            "complete",
            None,
            actor="none",
            frontier_present=False,
            completion_evidence={"path": "05_governance/completion.md"},
        )
        proposed = ROADMAP_BODY.replace(
            "Implement the fixture behavior.", "Sharpen the fixture behavior."
        )
        states = [
            payload(
                "needs_specification",
                None,
                actor="architect",
                frontier_present=False,
            ),
            complete,
            complete,
            complete,
            payload("ready", "execute_coding_prompt", round_=2),
            payload("ready", "frontier_recorded", round_=2),
            complete,
            complete,
        ]
        phase_b_policy = (
            "[target]\nmax_passes = 3\nmax_slices = 10\n"
            "[roles.reviewer]\nadapter = \"mock\"\n"
            "[autonomy]\npass_boundary = \"two_clean\"\n"
            "auto_continue_past_frontier_recorded = true\n"
        )
        scenario = Scenario(
            self.root / "phase_b",
            states=states,
            architect=[
                MockAgentAction(writes=(("roadmap_proposal.md", proposed),))
            ],
            coder=[MockAgentAction(writes=((SELF_REPORT, "# second pass\n"),))],
            reviewer=[
                MockAgentAction(
                    writes=((
                        "holistic_review.json",
                        json.dumps({"findings": ["M001-S01"]}),
                    ),)
                ),
                MockAgentAction(
                    writes=(("holistic_review.json", '{"findings": []}'),)
                ),
                MockAgentAction(
                    writes=(("holistic_review.json", '{"findings": []}'),)
                ),
            ],
            boundary="roadmap_complete",
            policy_body=phase_b_policy,
        )
        result = scenario.supervisor.run_until()
        self.assertEqual((result.kind, result.detail), ("boundary", "complete"))
        self.assertEqual(
            (scenario.project / ACTIVE_ROADMAP).read_text(encoding="utf-8"),
            proposed,
        )
        self.assertEqual(
            [e["clean"] for e in scenario.events() if e["kind"] == "holistic_review"],
            [False, True, True],
        )
        self.assertEqual(
            sum(e["kind"] == "pass_boundary" for e in scenario.events()), 1
        )
        self.assertLess(time.monotonic() - started, 10.0)

        forbidden_root = self.root / "phase_b_forbidden"
        forbidden_root.mkdir()
        forbidden = ROADMAP_BODY.replace(
            "Implement the fixture behavior.",
            "Rewrite accepted history and PROJECT_STATE.md.",
        )
        refused = Scenario(
            forbidden_root,
            states=[
                payload(
                    "needs_specification",
                    None,
                    actor="architect",
                    frontier_present=False,
                )
            ],
            architect=[
                MockAgentAction(writes=(("roadmap_proposal.md", forbidden),)),
                MockAgentAction(writes=(("roadmap_proposal.md", forbidden),)),
            ],
        )
        original = (refused.project / ACTIVE_ROADMAP).read_bytes()
        self.assertEqual(
            refused.supervisor.tick().stop_reason, StopReason.PATH_VIOLATION
        )
        self.assertEqual(
            sum(
                e["kind"] == "dispatch" and e["role"] == "architect"
                for e in refused.events()
            ),
            2,
        )
        self.assertEqual((refused.project / ACTIVE_ROADMAP).read_bytes(), original)

    def test_phase_b_new_journal_boundaries_resume_without_duplicate_effects(self):
        complete = payload(
            "complete",
            None,
            actor="none",
            frontier_present=False,
            completion_evidence={"path": "05_governance/completion.md"},
        )

        for boundary in ("reconciliation", "pass_boundary"):
            with self.subTest(boundary=boundary):
                root = self.root / f"phase_b_crash_{boundary}"
                seen = {"done": False}

                def event_hook(kind, _run_id, target=boundary):
                    if kind == target and not seen["done"]:
                        seen["done"] = True
                        raise SimulatedCrash(target)

                if boundary == "reconciliation":
                    proposal = ROADMAP_BODY.replace(
                        "Implement the fixture behavior.",
                        "Sharpen the fixture behavior.",
                    )
                    states = [
                        payload(
                            "needs_specification",
                            None,
                            actor="architect",
                            frontier_present=False,
                        ),
                        payload(),
                    ]
                    kwargs = {
                        "architect": [
                            MockAgentAction(
                                writes=(("roadmap_proposal.md", proposal),)
                            )
                        ],
                        "coder": [
                            MockAgentAction(writes=((SELF_REPORT, "# resumed\n"),))
                        ],
                    }
                else:
                    states = [complete, complete]
                    kwargs = {
                        "reviewer": [
                            MockAgentAction(
                                writes=(("holistic_review.json", '{"findings": []}'),)
                            )
                        ],
                        "policy_body": (
                            "[roles.reviewer]\nadapter = \"mock\"\n"
                            "[autonomy]\npass_boundary = \"two_clean\"\n"
                        ),
                    }
                crashed = Scenario(
                    root, states=states, event_hook=event_hook, **kwargs
                )
                with self.assertRaises(SimulatedCrash):
                    crashed.supervisor.tick()
                frozen = (
                    crashed.store.run_dir("run_001") / "pass_boundary.json"
                )
                frozen_bytes = frozen.read_bytes() if frozen.is_file() else None
                resumed = Scenario(
                    root,
                    project=crashed.project,
                    states=states,
                    **kwargs,
                )
                self.assertIsNone(resumed.supervisor.resume())
                outcome = resumed.supervisor.tick()
                self.assertEqual(outcome.kind, "acted")
                self.assertEqual(
                    sum(e["kind"] == boundary for e in resumed.events()), 1
                )
                if frozen_bytes is not None:
                    self.assertEqual(frozen.read_bytes(), frozen_bytes)

    def test_native_handle_owner_endures_mixed_close_outcomes(self):
        import frutlups_drive.verifier as verifier_module

        calls = []
        owners = []
        for index in range(192):
            def close(handle, mode=index % 3):
                calls.append(handle)
                if mode == 1:
                    return False
                if mode == 2:
                    raise RuntimeError("synthetic")
                return True

            owners.append(
                verifier_module._NativeHandleOwner.acquire(
                    "endurance_handle", lambda value=index + 1: value, close
                )
            )
        first = [owner.finalize() for owner in owners]
        second = [owner.finalize() for owner in owners]
        self.assertEqual(first, second)
        self.assertEqual(len(calls), len(owners))
        self.assertEqual(sum(outcome.closed for outcome in first), 64)
        self.assertEqual(
            sum(outcome.failure is not None for outcome in first), 128
        )

    def test_adoption_recovery_survives_every_current_attempt_boundary(self):
        for transition in ("planned", "collected", "validated", "closed"):
            with self.subTest(transition=transition):
                root = self.root / f"adoption_{transition}"
                root.mkdir()
                prior = Scenario(
                    root,
                    run_id="run_002",
                    states=[payload()],
                    coder=[
                        MockAgentAction(
                            writes=((SELF_REPORT, "# preserved coder work\n"),)
                        )
                    ],
                    verifier_exit_codes=[1],
                )
                self.assertEqual(
                    prior.supervisor.tick().detail, "verification_failed"
                )
                prior_attempt = prior.store.list_attempts(
                    "run_002", "M001-S01"
                )[0]
                prior_bytes = {
                    path.relative_to(prior_attempt).as_posix(): path.read_bytes()
                    for path in prior_attempt.rglob("*")
                    if path.is_file()
                }
                states = [
                    payload(
                        "ready",
                        "make_review_prompt",
                        review_prompt=REVIEW_PROMPT,
                        actor="orchestrator",
                    ),
                    payload(
                        "ready",
                        "execute_review_prompt",
                        review_prompt=REVIEW_PROMPT,
                        review_report=REVIEW_REPORT,
                        actor="reviewer",
                    ),
                    payload(
                        "ready",
                        "record_verdict",
                        review_prompt=REVIEW_PROMPT,
                        review_report=REVIEW_REPORT,
                        verdict_record=VERDICT_RECORD,
                        verdict={
                            "value": "pass",
                            "next_move": "advance",
                            "report": REVIEW_REPORT,
                        },
                        actor="orchestrator",
                    ),
                    payload(
                        "ready",
                        "frontier_recorded",
                        review_prompt=REVIEW_PROMPT,
                        review_report=REVIEW_REPORT,
                        verdict_record=VERDICT_RECORD,
                        verdict={
                            "value": "pass",
                            "next_move": "advance",
                            "report": REVIEW_REPORT,
                        },
                        actor="human",
                    ),
                ]
                crashed = Scenario(
                    root,
                    project=prior.project,
                    run_id="run_003",
                    states=states,
                    reviewer=[
                        MockAgentAction(
                            writes=((REVIEW_REPORT, "Verdict: pass\n"),)
                        )
                    ],
                    verbs=DEFAULT_VERBS,
                    transition_hook=crash_hook(transition),
                )
                with self.assertRaises(SimulatedCrash):
                    crashed.supervisor.run_until()
                resumed = Scenario(
                    root,
                    project=prior.project,
                    run_id="run_003",
                    states=states,
                    reviewer=[
                        MockAgentAction(
                            writes=((REVIEW_REPORT, "Verdict: pass\n"),)
                        )
                    ],
                    verbs=DEFAULT_VERBS,
                )
                stop = resumed.supervisor.resume()
                result = stop or resumed.supervisor.run_until()
                self.assertEqual(
                    (result.kind, result.detail), ("boundary", "slice_complete")
                )
                observed_prior = {
                    path.relative_to(prior_attempt).as_posix(): path.read_bytes()
                    for path in prior_attempt.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(observed_prior, prior_bytes)
                self.assertEqual(
                    len([e for e in resumed.events() if e["kind"] == "adoption"]),
                    1,
                )
                self.assertEqual(
                    [
                        e
                        for e in resumed.events()
                        if e["kind"] == "collected" and e.get("role") == "coder"
                    ],
                    [],
                )


if __name__ == "__main__":
    unittest.main()
