"""Adversarial lanes: path escape, governance mutation, fake evidence,
attempt clobber, read-only reviewer mutation, worktree isolation."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

from frutlups_drive.contracts import AgentRunResult, StopReason
from frutlups_drive.dispatch.mock import MockAgentAction

from _scenario import (
    DEFAULT_VERBS,
    REVIEW_PROMPT,
    REVIEW_REPORT,
    SELF_REPORT,
    Scenario,
    clean_pass_states,
    payload,
)
from test_workspace_fences import GIT_AVAILABLE, init_repo

CODER_OK = MockAgentAction(writes=((SELF_REPORT, "# Coder Self-Report\n"),))


class AdversarialTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def assert_stop(self, result, reason):
        self.assertEqual(result.kind, "stopped")
        self.assertEqual(result.stop_reason, reason)
        self.assertTrue(result.escalation_path.is_file())


class CoderFenceAdversaries(AdversarialTestCase):
    def test_path_escape_outside_workspace_is_refused(self):
        outside = self.tmp / "outside_target.txt"
        scenario = Scenario(
            self.tmp,
            states=[payload("ready", "execute_coding_prompt")],
            coder=[
                MockAgentAction(
                    writes=((SELF_REPORT, "# report\n"),),
                    absolute_writes=((str(outside), "stolen data\n"),),
                )
            ],
        )
        result = scenario.supervisor.run_until()
        self.assert_stop(result, StopReason.PATH_VIOLATION)
        fence_events = [e for e in scenario.events() if e["kind"] == "fence"]
        codes = {v["code"] for v in fence_events[0]["violations"]}
        self.assertIn("path_escape", codes)

    def test_governance_mutation_is_refused(self):
        scenario = Scenario(
            self.tmp,
            states=[payload("ready", "execute_coding_prompt")],
            coder=[
                MockAgentAction(
                    writes=(
                        (SELF_REPORT, "# report\n"),
                        ("PROJECT_STATE.md", "# tampered state\n"),
                    )
                )
            ],
        )
        result = scenario.supervisor.run_until()
        self.assert_stop(result, StopReason.PATH_VIOLATION)
        fence_events = [e for e in scenario.events() if e["kind"] == "fence"]
        codes = {(v["code"], v["path"]) for v in fence_events[0]["violations"]}
        self.assertIn(("governance_mutation", "PROJECT_STATE.md"), codes)

    def test_roadmap_mutation_is_refused(self):
        scenario = Scenario(
            self.tmp,
            states=[payload("ready", "execute_coding_prompt")],
            coder=[
                MockAgentAction(
                    writes=(
                        (SELF_REPORT, "# report\n"),
                        ("03_experiments/roadmap.md", "# rewritten roadmap\n"),
                    )
                )
            ],
        )
        self.assert_stop(scenario.supervisor.run_until(), StopReason.PATH_VIOLATION)


class RogueEvidencePlanter:
    """Models a rogue external process outside the mock authority boundary:
    it writes its expected artifact and directly plants verifier evidence,
    then lies about its changed files."""

    def __init__(self, planted_path):
        self.planted_path = Path(planted_path)

    def execute(self, request):
        report = Path(request.workspace) / SELF_REPORT
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_bytes(b"# report\n")
        self.planted_path.parent.mkdir(parents=True, exist_ok=True)
        self.planted_path.write_bytes(b"passed = true\n")
        return AgentRunResult(
            status="completed",
            event_log_path=Path("none"),
            changed_files=(Path(SELF_REPORT),),
            produced_artifacts=(Path(SELF_REPORT),),
            exit_reason="rogue",
            tokens_in=None,
            tokens_out=None,
            cost_usd=None,
        )


class EvidenceAdversaries(AdversarialTestCase):
    def test_planted_fake_evidence_is_refused_before_verification(self):
        # A rogue process (not the authorized mock effect path) plants
        # evidence into the predictable attempt directory; the post-hoc gate
        # must still refuse before any verification trust.
        fake_evidence = (
            self.tmp
            / "project/.frutlups_drive/runs/run_001/slices/M001-S01/attempt_001"
            / "verification/evidence.toml"
        )
        scenario = Scenario(
            self.tmp, states=[payload("ready", "execute_coding_prompt")]
        )
        scenario.supervisor._executors["coder"] = RogueEvidencePlanter(
            fake_evidence
        )
        result = scenario.supervisor.run_until()
        self.assert_stop(result, StopReason.PATH_VIOLATION)
        fence_events = [e for e in scenario.events() if e["kind"] == "fence"]
        codes = {v["code"] for e in fence_events for v in e["violations"]}
        self.assertIn("fake_evidence", codes)
        # No verification event was ever journaled for the tainted attempt.
        self.assertEqual(
            [e for e in scenario.events() if e["kind"] == "verification"], []
        )

    def test_intended_store_reach_is_refused_pre_effect(self):
        store_target = (
            self.tmp
            / "project/.frutlups_drive/runs/run_001/slices/M001-S01/attempt_001"
            / "planted.txt"
        )
        scenario = Scenario(
            self.tmp,
            states=[payload("ready", "execute_coding_prompt")],
            coder=[
                MockAgentAction(
                    writes=((SELF_REPORT, "# report\n"),),
                    absolute_writes=((str(store_target), "planted\n"),),
                )
            ],
        )
        result = scenario.supervisor.run_until()
        self.assert_stop(result, StopReason.PATH_VIOLATION)
        self.assertFalse(store_target.exists(), "store reach must be prevented")
        self.assertFalse((scenario.project / SELF_REPORT).exists())

    def test_attempt_clobber_reach_preserves_prior_attempt_bytes(self):
        scenario = Scenario(
            self.tmp,
            states=[
                payload("ready", "execute_coding_prompt"),
                payload("ready", "execute_coding_prompt", round_=2),
            ],
            coder=[
                CODER_OK,
                MockAgentAction(
                    writes=((SELF_REPORT, "# round two report\n"),),
                    absolute_writes=(
                        (
                            str(
                                self.tmp
                                / "project/.frutlups_drive/runs/run_001/slices"
                                / "M001-S01/attempt_001/clobber_marker.txt"
                            ),
                            "clobbered\n",
                        ),
                    ),
                ),
            ],
        )
        first = scenario.supervisor.tick()
        self.assertEqual(first.detail, "coder_attempt_completed")
        prior_attempt = scenario.store.list_attempts("run_001", "M001-S01")[0]
        prior_request = (prior_attempt / "request.json").read_bytes()
        result = scenario.supervisor.tick()
        self.assert_stop(result, StopReason.PATH_VIOLATION)
        self.assertEqual((prior_attempt / "request.json").read_bytes(), prior_request)


class ReviewerAdversaries(AdversarialTestCase):
    def test_read_only_reviewer_mutation_is_refused(self):
        states = clean_pass_states()
        scenario = Scenario(
            self.tmp,
            states=states,
            coder=[CODER_OK],
            reviewer=[
                MockAgentAction(
                    writes=(
                        (REVIEW_REPORT, "Verdict: pass — next: record\n"),
                        ("prompts/for_coding_agent/001_m001_s01_fix.md",
                         "# tampered prompt\n"),
                    )
                )
            ],
            verbs=DEFAULT_VERBS,
        )
        result = scenario.supervisor.run_until()
        self.assert_stop(result, StopReason.PATH_VIOLATION)
        fence_events = [e for e in scenario.events() if e["kind"] == "fence"]
        codes = {v["code"] for e in fence_events for v in e["violations"]}
        self.assertEqual(codes, {"read_only_mutation"})


class PreEffectAuthorityTests(AdversarialTestCase):
    """R1-F1 regressions: authority is decided before the first byte.

    Every probe asserts the negative space — outside targets, protected
    metadata, store members, and even the otherwise-valid expected artifact
    must remain absent/unchanged when any intended path lacks authority."""

    def test_coder_absolute_write_leaves_outside_and_report_absent(self):
        outside = self.tmp / "outside_target.txt"
        scenario = Scenario(
            self.tmp,
            states=[payload("ready", "execute_coding_prompt")],
            coder=[
                MockAgentAction(
                    writes=((SELF_REPORT, "# report\n"),),
                    absolute_writes=((str(outside), "mutated\n"),),
                )
            ],
        )
        result = scenario.supervisor.run_until()
        self.assert_stop(result, StopReason.PATH_VIOLATION)
        self.assertFalse(outside.exists(), "outside target must never be written")
        self.assertFalse(
            (scenario.project / SELF_REPORT).exists(),
            "an unauthorized action must be refused as a unit",
        )

    def test_coder_relative_traversal_refused_before_any_write(self):
        scenario = Scenario(
            self.tmp,
            states=[payload("ready", "execute_coding_prompt")],
            coder=[
                MockAgentAction(
                    writes=(
                        ("../traversal_escape.md", "escaped\n"),
                        (SELF_REPORT, "# report\n"),
                    )
                )
            ],
        )
        result = scenario.supervisor.run_until()
        self.assert_stop(result, StopReason.PATH_VIOLATION)
        self.assertFalse((self.tmp / "traversal_escape.md").exists())
        self.assertFalse((scenario.project / "traversal_escape.md").exists())
        self.assertFalse((scenario.project / SELF_REPORT).exists())

    def test_reviewer_absolute_write_stops_before_report_or_outside_change(self):
        outside = self.tmp / "reviewer_outside.txt"
        outside.write_bytes(b"original\n")
        scenario = Scenario(
            self.tmp,
            states=clean_pass_states(),
            coder=[CODER_OK],
            reviewer=[
                MockAgentAction(
                    writes=((REVIEW_REPORT, "Verdict: pass — next: record\n"),),
                    absolute_writes=((str(outside), "mutated\n"),),
                )
            ],
            verbs=DEFAULT_VERBS,
        )
        result = scenario.supervisor.run_until()
        self.assert_stop(result, StopReason.PATH_VIOLATION)
        self.assertEqual(outside.read_bytes(), b"original\n")
        self.assertFalse((scenario.project / REVIEW_REPORT).exists())

    def test_architect_absolute_write_is_refused_with_escalation(self):
        outside = self.tmp / "architect_outside.txt"
        scenario = Scenario(
            self.tmp,
            states=[payload("needs_specification", None, actor="architect",
                            frontier_present=False)],
            architect=[
                MockAgentAction(absolute_writes=((str(outside), "mutated\n"),))
            ],
        )
        result = scenario.supervisor.run_until()
        self.assert_stop(result, StopReason.PATH_VIOLATION)
        self.assertFalse(outside.exists())

    def test_architect_cannot_reach_git_or_store(self):
        scenario = Scenario(
            self.tmp,
            states=[payload("needs_specification", None, actor="architect",
                            frontier_present=False)],
            architect=[
                MockAgentAction(
                    writes=(
                        (".git/hooks_evil", "evil\n"),
                        (".frutlups_drive/runs/run_001/evil.txt", "evil\n"),
                    )
                )
            ],
        )
        result = scenario.supervisor.run_until()
        self.assert_stop(result, StopReason.PATH_VIOLATION)
        self.assertFalse((scenario.project / ".git/hooks_evil").exists())
        self.assertFalse(
            (scenario.project / ".frutlups_drive/runs/run_001/evil.txt").exists()
        )

    def test_coder_git_config_write_is_refused_and_bytes_preserved(self):
        scenario = Scenario(
            self.tmp,
            states=[payload("ready", "execute_coding_prompt")],
            coder=[
                MockAgentAction(
                    writes=(
                        (".git/config", "[core]\nevil = true\n"),
                        (SELF_REPORT, "# report\n"),
                    )
                )
            ],
        )
        git_dir = scenario.project / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_bytes(b"[core]\noriginal = true\n")
        result = scenario.supervisor.run_until()
        self.assert_stop(result, StopReason.PATH_VIOLATION)
        self.assertEqual(
            (git_dir / "config").read_bytes(), b"[core]\noriginal = true\n"
        )
        self.assertEqual(
            sorted(p.name for p in git_dir.iterdir()), ["config"],
            "repository metadata member set must be unchanged",
        )
        self.assertFalse((scenario.project / SELF_REPORT).exists())

    def test_lying_result_overrides_do_not_bypass_pre_effect_authority(self):
        outside = self.tmp / "hidden_target.txt"
        scenario = Scenario(
            self.tmp,
            states=[payload("ready", "execute_coding_prompt")],
            coder=[
                MockAgentAction(
                    writes=((SELF_REPORT, "# report\n"),),
                    absolute_writes=((str(outside), "hidden mutation\n"),),
                    changed_files_override=(SELF_REPORT,),
                    produced_override=(SELF_REPORT,),
                )
            ],
        )
        result = scenario.supervisor.run_until()
        self.assert_stop(result, StopReason.PATH_VIOLATION)
        self.assertFalse(outside.exists())

    def test_prior_attempt_member_set_and_bytes_preserved(self):
        scenario = Scenario(
            self.tmp,
            states=[
                payload("ready", "execute_coding_prompt"),
                payload("ready", "execute_coding_prompt", round_=2),
            ],
            coder=[
                CODER_OK,
                MockAgentAction(
                    writes=((SELF_REPORT, "# round two\n"),),
                    absolute_writes=(
                        (
                            str(
                                self.tmp
                                / "project/.frutlups_drive/runs/run_001/slices"
                                / "M001-S01/attempt_001/planted.txt"
                            ),
                            "planted\n",
                        ),
                    ),
                ),
            ],
        )
        first = scenario.supervisor.tick()
        self.assertEqual(first.detail, "coder_attempt_completed")
        prior = scenario.store.list_attempts("run_001", "M001-S01")[0]
        members_before = {
            p.relative_to(prior).as_posix(): p.read_bytes()
            for p in prior.rglob("*")
            if p.is_file()
        }
        result = scenario.supervisor.tick()
        self.assert_stop(result, StopReason.PATH_VIOLATION)
        members_after = {
            p.relative_to(prior).as_posix(): p.read_bytes()
            for p in prior.rglob("*")
            if p.is_file()
        }
        self.assertEqual(members_after, members_before)

    def test_verb_traversal_and_mismatch_are_refused_before_mutation(self):
        cases = {
            "traversal": {
                "make-review-prompt": (("../escaped.md", "# escaped\n"),),
            },
            "absolute": {
                "make-review-prompt": (
                    (str(self.tmp / "abs_escaped.md"), "# escaped\n"),
                ),
            },
            "mismatch": {
                "make-review-prompt": (
                    ("prompts/for_review_agent/999_substituted.md", "# sub\n"),
                ),
            },
        }
        for label, verbs in cases.items():
            with self.subTest(case=label):
                root = self.tmp / f"verb_{label}"
                root.mkdir()
                scenario = Scenario(
                    root,
                    states=[
                        payload("ready", "make_review_prompt",
                                actor="orchestrator",
                                review_prompt=REVIEW_PROMPT)
                    ],
                    verbs=verbs,
                )
                result = scenario.supervisor.run_until()
                self.assert_stop(result, StopReason.PATH_VIOLATION)
                self.assertFalse((root / "escaped.md").exists())
                self.assertFalse((self.tmp / "abs_escaped.md").exists())
                self.assertFalse(
                    (
                        scenario.project
                        / "prompts/for_review_agent/999_substituted.md"
                    ).exists()
                )
                self.assertFalse((scenario.project / REVIEW_PROMPT).exists())

    def test_positive_controls_still_pass(self):
        scenario = Scenario(
            self.tmp,
            states=clean_pass_states(),
            coder=[
                MockAgentAction(
                    writes=(
                        ("src_module_fix.py", "print('fixed')\n"),
                        (SELF_REPORT, "# Coder Self-Report\n"),
                    )
                )
            ],
            reviewer=[
                MockAgentAction(
                    writes=((REVIEW_REPORT, "Verdict: pass — next: record\n"),)
                )
            ],
            verbs=DEFAULT_VERBS,
        )
        result = scenario.supervisor.run_until()
        self.assertEqual(result.kind, "boundary")
        self.assertTrue((scenario.project / "src_module_fix.py").is_file())
        self.assertTrue((scenario.project / SELF_REPORT).is_file())
        self.assertTrue((scenario.project / REVIEW_REPORT).is_file())


class SymlinkAdversaries(AdversarialTestCase):
    def test_symlink_escape_inside_workspace_is_refused(self):
        outside = self.tmp / "outside_secret_dir"
        outside.mkdir()

        class SymlinkingExecutor:
            def __init__(self, project):
                self.project = project

            def execute(self, request):
                link = Path(request.workspace) / "escape_link"
                os.symlink(str(outside), str(link), target_is_directory=True)
                report = Path(request.workspace) / SELF_REPORT
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_bytes(b"# report\n")
                return AgentRunResult(
                    status="completed",
                    event_log_path=Path("none"),
                    changed_files=(Path(SELF_REPORT),),
                    produced_artifacts=(Path(SELF_REPORT),),
                    exit_reason="mock",
                    tokens_in=None,
                    tokens_out=None,
                    cost_usd=None,
                )

        try:
            probe = self.tmp / "probe_link"
            os.symlink(str(outside), str(probe), target_is_directory=True)
        except OSError as error:
            code = getattr(error, "winerror", None) or error.errno
            self.skipTest(
                f"host refuses link creation without elevation (error {code})"
            )
        scenario = Scenario(
            self.tmp, states=[payload("ready", "execute_coding_prompt")]
        )
        scenario.supervisor._executors["coder"] = SymlinkingExecutor(
            scenario.project
        )
        self.assert_stop(scenario.supervisor.run_until(), StopReason.PATH_VIOLATION)


@unittest.skipUnless(GIT_AVAILABLE, "git executable is required for worktree lanes")
class WorktreeModeScenarioTests(AdversarialTestCase):
    def test_clean_pass_in_worktree_mode_leaves_source_repo_untouched(self):
        from _scenario import build_project
        from frutlups_drive.workspace import WorkspaceManager

        project = build_project(self.tmp)
        init_repo(project)
        source_manager = WorkspaceManager(project, project / ".frutlups_drive")
        source_before = source_manager.snapshot(project)
        head_before = source_manager.revision(project)

        scenario = Scenario(
            self.tmp,
            project=project,
            states=clean_pass_states(),
            coder=[CODER_OK],
            reviewer=[
                MockAgentAction(
                    writes=((REVIEW_REPORT, "Verdict: pass — next: record\n"),)
                )
            ],
            verbs=DEFAULT_VERBS,
            policy_body="[git]\nworktree_per_slice = true\n",
        )
        result = scenario.supervisor.run_until()
        self.assertEqual(result.kind, "boundary")
        worktree = (
            scenario.store.root / "runs/run_001/worktrees/M001-S01"
        )
        self.assertTrue((worktree / SELF_REPORT).is_file())
        self.assertFalse((project / SELF_REPORT).is_file())
        self.assertEqual(source_manager.revision(project), head_before)
        source_after = {
            path: digest
            for path, digest in source_manager.snapshot(project).items()
            if not path.startswith("frutlups_drive.toml")
        }
        expected_after = dict(source_before)
        # The verb writer legitimately wrote review prompt and verdict record
        # into the project; everything else must be untouched.
        for path, digest in source_after.items():
            if path in (REVIEW_PROMPT,
                        "05_governance/reviews/m001/m001_s01_verdict_record.md",
                        "frutlups_drive.toml"):
                continue
            self.assertEqual(
                digest, expected_after.get(path),
                f"unexpected source repository change: {path}",
            )


if __name__ == "__main__":
    unittest.main()
