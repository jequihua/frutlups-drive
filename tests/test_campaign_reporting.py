"""M009-S05 campaign identity, lineage, and aggregate reporting proofs."""

import contextlib
import io
import json
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

from frutlups_drive.cli import main
from frutlups_drive.contracts import AgentRunRequest, AgentRunResult, ExitCode, Role
from frutlups_drive.runstore import RunStore
from frutlups_drive.telemetry import derive_campaign_report


FIXTURE_PROJECT = (
    Path(__file__).resolve().parent / "fixtures" / "projects" / "minimal_v3"
)


class CampaignReportingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = RunStore(self.root / ".frutlups_drive")

    def seed_run(self, run_id, *, campaign=None, predecessor=None, events=()):
        manifest = {"contract_version": 1, "started_at": str(events[0]["t"])}
        if campaign is not None:
            manifest["campaign_id"] = campaign
        if predecessor is not None:
            manifest["predecessor_run_id"] = predecessor
        self.store.create_run(run_id, manifest)
        for event in events:
            self.store.append_event(run_id, event)

    def attempt(
        self,
        run_id,
        *,
        role,
        model,
        effort,
        cost_knowledge,
        dispatched_at,
        completed_at,
    ):
        attempt = self.store.create_attempt(run_id, "M009-S05")
        self.store.write_request(
            attempt,
            AgentRunRequest(
                run_id=run_id,
                attempt_id=attempt.name,
                role=role,
                prompt_path=Path("prompt.md"),
                prompt_sha256="a" * 64,
                workspace=Path("workspace"),
                base_revision=None,
                adapter="codex_cli" if role is Role.CODER else "kimi_cli",
                model=model,
                effort=effort,
                workspace_access=(
                    "workspace_write" if role is Role.CODER else "read_only"
                ),
                expected_artifacts=(Path("report.md"),),
                max_seconds=60,
                max_cost_usd=1.0,
            ),
        )
        self.store.write_result(
            attempt,
            AgentRunResult(
                status="completed",
                event_log_path=Path("events.jsonl"),
                changed_files=(),
                produced_artifacts=(Path("report.md"),),
                exit_reason="completed",
                tokens_in=100,
                tokens_out=20,
                cost_usd=None,
                cost_knowledge=cost_knowledge,
            ),
        )
        self.store.advance_transition(attempt, "closed")
        self.store.append_event(
            run_id,
            {
                "kind": "dispatch",
                "t": dispatched_at,
                "slice": "M009-S05",
                "attempt": attempt.name,
                "role": role.value,
                "repair": False,
            },
        )
        self.store.append_event(
            run_id,
            {
                "kind": "collected",
                "t": completed_at,
                "slice": "M009-S05",
                "attempt": attempt.name,
                "role": role.value,
                "status": "completed",
                "cost_usd": None,
            },
        )

    def fixture(self):
        self.seed_run(
            "run_001",
            campaign="v5-eval",
            events=({"kind": "run_created", "t": 100},),
        )
        self.attempt(
            "run_001",
            role=Role.CODER,
            model="gpt-5.6-sol",
            effort="high",
            cost_knowledge="subscription_prepaid",
            dispatched_at=101,
            completed_at=106,
        )
        self.store.append_event(
            "run_001",
            {
                "kind": "stop",
                "t": 110,
                "reason": "fresh_run_required",
                "detail": "owner note changed",
                "escalation": "fresh.md",
            },
        )
        self.seed_run(
            "run_002",
            campaign="v5-eval",
            predecessor="run_001",
            events=({"kind": "run_created", "t": 120},),
        )
        self.attempt(
            "run_002",
            role=Role.REVIEWER,
            model="kimi-code/k3",
            effort="high",
            cost_knowledge="unknown",
            dispatched_at=122,
            completed_at=126,
        )
        self.store.append_event(
            "run_002",
            {"kind": "boundary", "t": 130, "boundary": "roadmap_complete"},
        )
        self.seed_run(
            "run_003",
            campaign="other",
            events=({"kind": "run_created", "t": 140},),
        )

    def test_multi_run_campaign_story_is_complete_and_error_free(self):
        self.fixture()
        report = derive_campaign_report(self.store, campaign_id="v5-eval")
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["run_count"], 2)
        self.assertEqual(report["lineage_chains"], [["run_001", "run_002"]])
        self.assertEqual(report["stop_reasons"], {"fresh_run_required": 1})
        self.assertEqual(
            report["attempt_durations"],
            {"known_count": 2, "known_sum_seconds": 9.0, "unknown_count": 0},
        )
        self.assertEqual(
            report["dispatch_counts"]["by_role"], {"coder": 1, "reviewer": 1}
        )
        self.assertEqual(
            report["dispatch_counts"]["by_model"],
            {"gpt-5.6-sol": 1, "kimi-code/k3": 1},
        )
        self.assertEqual(
            report["dispatch_counts"]["by_effort"], {"high": 2}
        )
        self.assertEqual(
            report["cost_knowledge"],
            {
                "attempts_by_class": {
                    "subscription_prepaid": 1,
                    "unknown": 1,
                },
                "attempts_without_numeric_cost": 2,
                "known_cost_sum": 0.0,
                "tokens_in_known_sum": 200,
                "tokens_out_known_sum": 40,
            },
        )
        self.assertEqual(
            report["recovery_intervals"],
            [
                {
                    "duration_seconds": 16,
                    "first_progress_kind": "collected",
                    "predecessor_run_id": "run_001",
                    "status": "known",
                    "stop_reason": "fresh_run_required",
                    "successor_run_id": "run_002",
                }
            ],
        )
        self.assertEqual(report["runs"][0]["stop_outcome"], "fresh_run_required")
        self.assertEqual(report["runs"][1]["boundary_outcome"], "roadmap_complete")

    def test_campaign_and_all_run_cli_selectors_are_read_only(self):
        self.fixture()
        before = {
            path.relative_to(self.store.root).as_posix(): path.read_bytes()
            for path in self.store.root.rglob("*")
            if path.is_file()
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(
                ["report", str(self.root), "--campaign", "v5-eval", "--json"]
            )
        self.assertEqual(code, int(ExitCode.OK))
        self.assertEqual(json.loads(output.getvalue())["run_count"], 2)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["report", str(self.root), "--all", "--json"])
        self.assertEqual(code, int(ExitCode.OK))
        self.assertEqual(json.loads(output.getvalue())["run_count"], 3)
        after = {
            path.relative_to(self.store.root).as_posix(): path.read_bytes()
            for path in self.store.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_manifest_identity_is_opt_in_and_fresh_lineage_is_automatic(self):
        project = self.root / "project"
        shutil.copytree(FIXTURE_PROJECT, project)
        store = RunStore(project / ".frutlups_drive")
        store.create_run(
            "run_000",
            {"contract_version": 1, "campaign_id": "v5-eval"},
        )
        store.append_event(
            "run_000",
            {"kind": "stop", "t": 1, "reason": "fresh_run_required"},
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(
                [
                    "run",
                    str(project),
                    "--until",
                    "slice_complete",
                    "--predecessor-run",
                    "run_000",
                ]
            )
        self.assertEqual(code, int(ExitCode.OK))
        manifest = tomllib.loads(
            (store.run_dir("run_001") / "manifest.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["campaign_id"], "v5-eval")
        self.assertEqual(manifest["predecessor_run_id"], "run_000")
        created = store.read_events("run_001")[0]
        self.assertEqual(created["campaign_id"], "v5-eval")
        self.assertEqual(created["predecessor_run_id"], "run_000")

        plain = self.root / "plain"
        shutil.copytree(FIXTURE_PROJECT, plain)
        plain_store = RunStore(plain / ".frutlups_drive")
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["run", str(plain), "--until", "slice_complete"])
        self.assertEqual(code, int(ExitCode.OK))
        raw = (plain_store.run_dir("run_001") / "manifest.toml").read_bytes()
        self.assertNotIn(b"campaign_id", raw)
        self.assertNotIn(b"predecessor_run_id", raw)
        self.assertEqual(
            set(plain_store.read_events("run_001")[0]),
            {"kind", "t", "boundary"},
        )

        launched = self.root / "launch_campaign"
        shutil.copytree(FIXTURE_PROJECT, launched)
        launched_store = RunStore(launched / ".frutlups_drive")
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(
                [
                    "run",
                    str(launched),
                    "--until",
                    "slice_complete",
                    "--campaign-id",
                    "launch.v5",
                ]
            )
        self.assertEqual(code, int(ExitCode.OK))
        launched_manifest = tomllib.loads(
            (launched_store.run_dir("run_001") / "manifest.toml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(launched_manifest["campaign_id"], "launch.v5")
        self.assertNotIn("predecessor_run_id", launched_manifest)


if __name__ == "__main__":
    unittest.main()
