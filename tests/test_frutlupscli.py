"""Offline binding-loader and verb-writer refusal lanes (M003-S02).

Everything here is offline: fake runners implementing the declared
``ProcessRunner`` subset and local stub payloads. The real-frutlups
transaction behavior lives in ``test_frutlups_e2e.py``.
"""

import hashlib
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

from frutlups_drive.frutlupscli import (
    BINDING_SCHEMA_VERSION,
    FrutlupsBindingError,
    FrutlupsCorrectiveRound,
    FrutlupsLaunchIdentity,
    FrutlupsVerbError,
    FrutlupsVerbWriter,
    binding_manifest_facts,
    build_launch_identity,
    load_launch_binding,
)
from frutlups_drive.mockverbs import VerbAuthorityDenied
from frutlups_drive.contracts import LoopStep, PlanOutcome
from frutlups_drive.planstate import ArtifactPaths, Frontier, PlanningState

from test_subprocess_agent import RecordingRunner


def binding_text(argv0: str, *, env_lines: str = "") -> str:
    escaped = argv0.replace("\\", "\\\\")
    return (
        f'schema_version = "{BINDING_SCHEMA_VERSION}"\n'
        "[launch]\n"
        f'argv_prefix = ["{escaped}", "-m", "frutlups"]\n'
        'tool_identity = "frutlups==0.1.0"\n'
        "[env]\n" + env_lines
    )


class BindingLoaderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.binding_path = self.dir / "frutlups_binding.toml"

    def write_binding(self, text: str) -> Path:
        self.binding_path.write_text(text, encoding="utf-8")
        return self.binding_path

    def assert_refused(self, code, text=None):
        if text is not None:
            self.write_binding(text)
        with self.assertRaises(FrutlupsBindingError) as caught:
            load_launch_binding(self.binding_path)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_valid_binding_round_trips_with_identity_hashes(self):
        path = self.write_binding(binding_text(sys.executable))
        binding = load_launch_binding(path)
        self.assertEqual(binding.argv_prefix[0], sys.executable)
        self.assertEqual(binding.argv_prefix[1:], ("-m", "frutlups"))
        self.assertEqual(binding.tool_identity, "frutlups==0.1.0")
        self.assertEqual(
            binding.binding_sha256,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            binding.executable_sha256,
            hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        )
        self.assertEqual(binding.env, (("PYTHONDONTWRITEBYTECODE", "1"),))

    def test_child_environment_forces_bytecode_hygiene(self):
        path = self.write_binding(
            binding_text(
                sys.executable,
                env_lines='PYTHONDONTWRITEBYTECODE = "0"\n',
            )
        )
        binding = load_launch_binding(path)
        self.assertEqual(dict(binding.env)["PYTHONDONTWRITEBYTECODE"], "1")

    def test_missing_binding_refuses(self):
        self.assert_refused("binding_missing")

    def test_malformed_and_unknown_schema_refuse(self):
        self.assert_refused("binding_malformed", "not = [valid\n")
        self.assert_refused(
            "binding_schema_unknown",
            'schema_version = "frutlups_drive_binding_v9"\n[launch]\n'
            f'argv_prefix = ["{sys.executable.replace(chr(92), chr(92) * 2)}"]\n',
        )

    def test_relative_or_missing_executable_refuses(self):
        self.assert_refused(
            "binding_field_invalid", binding_text("python")
        )
        self.assert_refused(
            "binding_executable_missing",
            binding_text(str(self.dir / "no_such_python.exe")),
        )

    def test_secret_shaped_env_values_refuse_without_echo(self):
        refusal = self.assert_refused(
            "binding_secret_shaped",
            binding_text(
                sys.executable,
                env_lines='PROVIDER_API_KEY = "DO-NOT-ECHO-BINDING"\n',
            ),
        )
        self.assertNotIn("DO-NOT-ECHO-BINDING", str(refusal))

    def test_empty_secret_shaped_env_value_is_tolerated(self):
        path = self.write_binding(
            binding_text(sys.executable, env_lines='PROVIDER_API_KEY = ""\n')
        )
        binding = load_launch_binding(path)
        self.assertIn(("PROVIDER_API_KEY", ""), binding.env)

    def test_manifest_facts_contain_hashes_but_no_machine_paths(self):
        path = self.write_binding(binding_text(sys.executable))
        binding = load_launch_binding(path)
        layout = self.dir / "frutlups.layout.yaml"
        layout.write_text("schema_version: frutlups_layout_config_v0\n",
                          encoding="utf-8")
        facts = binding_manifest_facts(
            binding,
            layout_path=layout,
            contract_id="frutlups.planning_frontier",
            contract_version="1",
            package_identity="frutlups==0.1.0",
            policy_hash="0" * 64,
        )
        self.assertEqual(
            facts["frutlups_binding_sha256"], binding.binding_sha256
        )
        self.assertEqual(
            facts["frutlups_layout_sha256"],
            hashlib.sha256(layout.read_bytes()).hexdigest(),
        )
        rendered = repr(facts)
        self.assertNotIn(str(self.dir), rendered)
        self.assertNotIn(sys.executable, rendered)

    def test_binding_is_bounded_and_text_members_are_control_free(self):
        base = binding_text(sys.executable)
        exact = base + ("#" * (65_536 - len(base.encode("utf-8"))))
        self.assertEqual(len(exact.encode("utf-8")), 65_536)
        self.binding_path.write_bytes(exact.encode("utf-8"))
        self.assertEqual(
            load_launch_binding(self.binding_path).tool_identity,
            "frutlups==0.1.0",
        )
        self.binding_path.write_bytes((exact + "#").encode("utf-8"))
        self.assert_refused("binding_oversized")
        for member in (
            binding_text(sys.executable).replace(
                '"-m"', '"\\u0000"'
            ),
            binding_text(sys.executable).replace(
                'tool_identity = "frutlups==0.1.0"',
                'tool_identity = "frutlups\\u00010.1.0"',
            ),
        ):
            with self.subTest(member=member[-80:]):
                self.assert_refused("binding_field_invalid", member)

    def test_tool_identity_uses_the_closed_safe_grammar(self):
        for identity in (
            "",
            "contains spaces",
            "../tool",
            "_leading",
            "api_key=DO-NOT-ECHO",
        ):
            with self.subTest(identity=identity):
                self.assert_refused(
                    "binding_field_invalid",
                    binding_text(sys.executable).replace(
                        "frutlups==0.1.0", identity
                    ),
                )

    def test_missing_layout_refuses_instead_of_recording_empty_identity(self):
        binding = load_launch_binding(
            self.write_binding(binding_text(sys.executable))
        )
        with self.assertRaises(FrutlupsBindingError) as caught:
            binding_manifest_facts(
                binding,
                layout_path=self.dir / "missing.layout.yaml",
                contract_id="frutlups.planning_frontier",
                contract_version="1",
                package_identity="frutlups==0.1.0",
                policy_hash="0" * 64,
            )
        self.assertEqual(caught.exception.code, "layout_missing")
        directory = self.dir / "layout-directory"
        directory.mkdir()
        with self.assertRaises(FrutlupsBindingError) as caught:
            binding_manifest_facts(
                binding,
                layout_path=directory,
                contract_id="frutlups.planning_frontier",
                contract_version="1",
                package_identity="frutlups==0.1.0",
                policy_hash="0" * 64,
            )
        self.assertEqual(caught.exception.code, "layout_missing")

    def test_launch_identity_changes_for_every_execution_authority_member(self):
        executable_a = self.dir / "tool-a.exe"
        executable_b = self.dir / "tool-b.exe"
        executable_a.write_bytes(b"tool bytes v1")
        executable_b.write_bytes(b"tool bytes v1")
        layout = self.dir / "frutlups.layout.yaml"
        layout.write_bytes(b"layout v1\n")

        def identity(executable, *, env="", policy="0" * 64):
            binding = load_launch_binding(
                self.write_binding(binding_text(str(executable), env_lines=env))
            )
            return build_launch_identity(
                binding,
                layout_path=layout,
                contract_id="frutlups.planning_frontier",
                contract_version="1",
                package_identity="frutlups==0.1.0",
                policy_hash=policy,
            )

        baseline = identity(executable_a)
        self.assertNotEqual(identity(executable_b), baseline, "path/argv binding")
        self.assertNotEqual(
            identity(executable_a, env='LANG = "C"\n'), baseline, "environment"
        )
        self.assertNotEqual(identity(executable_a, policy="f" * 64), baseline)
        executable_a.write_bytes(b"tool bytes v2")
        self.assertNotEqual(identity(executable_a), baseline, "executable bytes")
        executable_a.write_bytes(b"tool bytes v1")
        layout.write_bytes(b"layout v2\n")
        self.assertNotEqual(identity(executable_a), baseline, "layout bytes")
        binding = load_launch_binding(
            self.write_binding(binding_text(str(executable_a)))
        )
        changed_package = build_launch_identity(
            binding,
            layout_path=layout,
            contract_id="frutlups.planning_frontier",
            contract_version="1",
            package_identity="frutlups==0.1.1",
            policy_hash="0" * 64,
        )
        self.assertNotEqual(changed_package.package_identity,
                            baseline.package_identity)

    def test_durable_declared_identity_refuses_path_or_credential_text(self):
        executable = self.dir / "tool.exe"
        executable.write_bytes(b"tool")
        binding = load_launch_binding(
            self.write_binding(binding_text(str(executable)))
        )
        layout = self.dir / "frutlups.layout.yaml"
        layout.write_bytes(b"layout\n")
        for package in ("C:/machine/tool", "api_key=DO-NOT-ECHO"):
            with self.subTest(package=package):
                with self.assertRaises(FrutlupsBindingError) as caught:
                    build_launch_identity(
                        binding,
                        layout_path=layout,
                        contract_id="frutlups.planning_frontier",
                        contract_version="1",
                        package_identity=package,
                        policy_hash="0" * 64,
                    )
                self.assertEqual(
                    caught.exception.code, "launch_identity_invalid"
                )
                self.assertNotIn(package, str(caught.exception))

    def test_link_like_layout_refuses_when_host_permits(self):
        executable = self.dir / "tool.exe"
        executable.write_bytes(b"tool")
        binding = load_launch_binding(
            self.write_binding(binding_text(str(executable)))
        )
        real_layout = self.dir / "real.layout.yaml"
        link_layout = self.dir / "link.layout.yaml"
        real_layout.write_bytes(b"layout\n")
        try:
            link_layout.symlink_to(real_layout)
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest("host refuses link creation without elevation (1314)")
            raise
        with self.assertRaises(FrutlupsBindingError) as caught:
            build_launch_identity(
                binding,
                layout_path=link_layout,
                contract_id="frutlups.planning_frontier",
                contract_version="1",
                package_identity="frutlups==0.1.0",
                policy_hash="0" * 64,
            )
        self.assertEqual(caught.exception.code, "layout_missing")


class FakeVerbRunner:
    """Scripted ProcessRunner-subset fake: writes scripted stdout payloads."""

    def __init__(self, stdouts, exit_codes=None):
        self.stdouts = list(stdouts)
        self.exit_codes = list(exit_codes or [])
        self.calls = 0

    def run(self, argv, cwd, env, timeout_seconds, stdout_path, stderr_path,
            max_stream_bytes=1_048_576):
        from frutlups_drive.verifier import ProcessOutcome

        index = self.calls
        self.calls += 1
        payload = self.stdouts[index] if index < len(self.stdouts) else "{}"
        Path(stdout_path).write_bytes(payload.encode("utf-8"))
        Path(stderr_path).write_bytes(b"")
        code = self.exit_codes[index] if index < len(self.exit_codes) else 0
        return ProcessOutcome(0.0, 0.0, code, False)


class VerbWriterRefusalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.project = self.dir / "project"
        (self.project / "prompts" / "for_coding_agent").mkdir(parents=True)
        binding_path = self.dir / "binding.toml"
        binding_path.write_text(binding_text(sys.executable), encoding="utf-8")
        self.binding = load_launch_binding(binding_path)
        self.identity = FrutlupsLaunchIdentity(
            policy_hash="0" * 64,
            binding_sha256=self.binding.binding_sha256,
            executable_sha256=self.binding.executable_sha256,
            tool_identity=self.binding.tool_identity,
            layout_sha256="1" * 64,
            contract_id="frutlups.planning_frontier",
            contract_version="1",
            package_identity="frutlups==0.1.0",
        )

    def writer(self, runner, **kwargs):
        from frutlups_drive.workspace import WorkspaceManager

        manager = WorkspaceManager(
            self.project, self.project / ".frutlups_drive"
        )
        snapshot = kwargs.pop(
            "snapshot", lambda: manager.transaction_snapshot(self.project)
        )
        intent_path = kwargs.pop(
            "intent_path", self.dir / "store" / "pending_verb.json"
        )
        identity_reader = kwargs.pop(
            "identity_reader", lambda: self.identity
        )
        return FrutlupsVerbWriter(
            project_root=self.project,
            binding=self.binding,
            runner=runner,
            capture_root=self.dir / "captures",
            store_root=self.project / ".frutlups_drive",
            timeout_seconds=30.0,
            snapshot=snapshot,
            launch_identity=self.identity,
            identity_reader=identity_reader,
            intent_path=intent_path,
            **kwargs,
        )

    def dry_payload(self, target="prompts/for_coding_agent/003_x.md",
                    valid=True):
        import json

        return json.dumps(
            {
                "errors": [] if valid else ["typed refusal"],
                "valid": valid,
                "preview": {"target_path": target, "would_write": True},
            }
        )

    @staticmethod
    def progressed_state():
        return PlanningState(
            outcome=PlanOutcome.READY,
            step=LoopStep.EXECUTE_CODING_PROMPT,
            actor=None,
            gate_state=None,
            frontier=Frontier("M001", "M001-S01", "slice", 1),
            artifacts=ArtifactPaths(None, None, None, None, None),
            verdict=None,
            blocked=None,
            completion_evidence=None,
            diagnostics=(),
            next_command=None,
        )

    def crash_after_validation(
        self,
        target="prompts/for_coding_agent/003_x.md",
        verb="make-coding-prompt",
    ):
        def hook(stage, verb):
            if stage == "validated":
                raise RuntimeError("simulated crash after durable validation")

        writer = self.writer(
            FakeVerbRunner([self.dry_payload(target)]),
            transaction_hook=hook,
            status_reader=self.progressed_state,
        )
        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            writer.invoke(verb, None)
        return writer

    def test_unsanctioned_verb_refuses(self):
        writer = self.writer(FakeVerbRunner([]))
        with self.assertRaises(FrutlupsVerbError) as caught:
            writer.invoke("orchestrator-run", None)
        self.assertEqual(caught.exception.code, "verb_not_allowed")

    def test_nonzero_dry_run_refuses(self):
        writer = self.writer(FakeVerbRunner(["{}"], exit_codes=[3]))
        with self.assertRaises(FrutlupsVerbError) as caught:
            writer.invoke("make-coding-prompt", None)
        self.assertEqual(caught.exception.code, "verb_exit_nonzero")

    def test_nonzero_dry_run_cannot_hide_a_workspace_effect(self):
        class FailingDryRunner(FakeVerbRunner):
            def run(runner, argv, cwd, env, timeout_seconds, stdout_path,
                    stderr_path, max_stream_bytes=1_048_576):
                outcome = super().run(
                    argv, cwd, env, timeout_seconds, stdout_path, stderr_path,
                    max_stream_bytes,
                )
                (self.project / "PROJECT_STATE.md").write_bytes(b"extra\n")
                return outcome

        writer = self.writer(FailingDryRunner(["{}"], exit_codes=[7]))
        with self.assertRaises(FrutlupsVerbError) as caught:
            writer.invoke("make-coding-prompt", None)
        self.assertEqual(caught.exception.code, "verb_dry_run_effect")

    def test_typed_dry_run_refusal_fails_closed(self):
        writer = self.writer(FakeVerbRunner([self.dry_payload(valid=False)]))
        with self.assertRaises(FrutlupsVerbError) as caught:
            writer.invoke("make-coding-prompt", None)
        self.assertEqual(caught.exception.code, "verb_dry_run_refused")

    def test_escaping_dry_run_target_is_authority_denied(self):
        writer = self.writer(
            FakeVerbRunner([self.dry_payload(target="../outside.md")])
        )
        with self.assertRaises(FrutlupsVerbError) as caught:
            writer.invoke("make-coding-prompt", None)
        self.assertEqual(caught.exception.code, "verb_target_invalid")

    def test_governance_target_is_authority_denied_before_write(self):
        runner = FakeVerbRunner(
            [self.dry_payload(target="PROJECT_STATE.md")]
        )
        writer = self.writer(runner)
        with self.assertRaises(VerbAuthorityDenied):
            writer.invoke("make-coding-prompt", None)
        self.assertEqual(runner.calls, 1, "the real write never ran")

    def test_target_mismatch_with_declared_path_refuses(self):
        writer = self.writer(FakeVerbRunner([self.dry_payload()]))
        with self.assertRaises(FrutlupsVerbError) as caught:
            writer.invoke(
                "make-coding-prompt", "prompts/for_coding_agent/999_y.md"
            )
        self.assertEqual(caught.exception.code, "verb_target_mismatch")

    def test_preexisting_target_refuses_before_write(self):
        target = self.project / "prompts/for_coding_agent/003_x.md"
        target.write_bytes(b"already here\n")
        runner = FakeVerbRunner([self.dry_payload()])
        writer = self.writer(runner)
        with self.assertRaises(FrutlupsVerbError) as caught:
            writer.invoke("make-coding-prompt", None)
        self.assertEqual(caught.exception.code, "verb_target_preexists")
        self.assertEqual(target.read_bytes(), b"already here\n")
        self.assertEqual(runner.calls, 1)

    def test_record_verdict_requires_validated_report_reference(self):
        writer = self.writer(FakeVerbRunner([]))
        for bad in (None, "", "../escape_review_report.md"):
            with self.subTest(report=repr(bad)):
                with self.assertRaises(FrutlupsVerbError) as caught:
                    writer.invoke("record-verdict", None, review_report=bad)
                self.assertEqual(
                    caught.exception.code, "verb_report_reference_invalid"
                )

    def test_recode_next_action_raises_corrective_round_without_write(self):
        import json

        payload = json.dumps(
            {
                "errors": [],
                "valid": True,
                "target_path": "05_governance/reviews/x_verdict_record.md",
                "next_action": {"kind": "recode_same_slice"},
            }
        )
        runner = FakeVerbRunner([payload])
        writer = self.writer(runner)
        with self.assertRaises(FrutlupsCorrectiveRound):
            writer.invoke(
                "record-verdict",
                None,
                review_report="05_governance/reviews/x_review_report.md",
            )
        self.assertEqual(runner.calls, 1, "only the dry run may execute")

    def test_unknown_next_action_kind_fails_closed(self):
        import json

        payload = json.dumps(
            {
                "errors": [],
                "valid": True,
                "target_path": "05_governance/reviews/x_verdict_record.md",
                "next_action": {"kind": "teleport"},
            }
        )
        writer = self.writer(FakeVerbRunner([payload]))
        with self.assertRaises(FrutlupsVerbError) as caught:
            writer.invoke(
                "record-verdict",
                None,
                review_report="05_governance/reviews/x_review_report.md",
            )
        self.assertEqual(caught.exception.code, "verb_next_action_unknown")

    def test_write_facts_must_prove_one_fresh_write(self):
        import json

        target = "prompts/for_coding_agent/003_x.md"
        real = json.dumps(
            {
                "errors": [],
                "valid": True,
                "preview": {"target_path": target},
                "write_result": {"wrote": False, "overwrote": False},
            }
        )
        writer = self.writer(FakeVerbRunner([self.dry_payload(), real]))
        with self.assertRaises(FrutlupsVerbError) as caught:
            writer.invoke("make-coding-prompt", None)
        self.assertEqual(caught.exception.code, "verb_write_facts_invalid")

    def test_transport_failure_is_owned(self):
        writer = self.writer(RecordingRunner(error=OSError("spawn refused")))
        with self.assertRaises(FrutlupsVerbError) as caught:
            writer.invoke("make-coding-prompt", None)
        self.assertEqual(caught.exception.code, "verb_transport_failed")

    def test_intent_marker_survives_the_write_until_cleared(self):
        # The write-ahead intent is staged before the real write and outlives
        # invoke(): only the supervisor clears it after the journal event
        # exists, so a crash between effect and journal stays reconcilable.
        import json

        target = "prompts/for_coding_agent/003_x.md"
        real = json.dumps(
            {
                "errors": [],
                "valid": True,
                "preview": {"target_path": target},
                "write_result": {"wrote": True, "overwrote": False},
            }
        )

        class WritingRunner(FakeVerbRunner):
            def __init__(self, stdouts, project):
                super().__init__(stdouts)
                self.project = project

            def run(self, argv, cwd, env, timeout_seconds, stdout_path,
                    stderr_path, max_stream_bytes=1_048_576):
                outcome = super().run(argv, cwd, env, timeout_seconds,
                                      stdout_path, stderr_path,
                                      max_stream_bytes)
                if "--dry-run" not in argv:
                    written = self.project / target
                    written.parent.mkdir(parents=True, exist_ok=True)
                    written.write_bytes(b"prompt body\n")
                return outcome

        intent_path = self.dir / "store" / "pending_verb.json"
        writer = self.writer(
            WritingRunner([self.dry_payload(), real], self.project),
            intent_path=intent_path,
        )
        produced = writer.invoke("make-coding-prompt", None)
        self.assertEqual(produced, self.project / target)
        self.assertTrue(intent_path.is_file(), "intent must outlive invoke()")
        staged = json.loads(intent_path.read_text(encoding="utf-8"))
        self.assertEqual(staged["schema_version"], 1)
        self.assertEqual(staged["verb"], "make-coding-prompt")
        self.assertEqual(staged["target"], target)
        self.assertFalse(staged["target_preexisted"])
        self.assertIn("workspace_before", staged)
        self.assertIn("launch_identity", staged)
        writer.clear_intent()
        self.assertFalse(intent_path.exists())
        writer.clear_intent()  # idempotent

    def test_write_accepts_target_with_new_ordinary_ancestor_directory(self):
        target = "prompts/for_review_agent/002_x.md"
        real = json.dumps(
            {
                "errors": [],
                "valid": True,
                "preview": {"target_path": target},
                "write_result": {"wrote": True, "overwrote": False},
            }
        )

        class WritingRunner(FakeVerbRunner):
            def run(runner, argv, cwd, env, timeout_seconds, stdout_path,
                    stderr_path, max_stream_bytes=1_048_576):
                outcome = super().run(
                    argv, cwd, env, timeout_seconds, stdout_path, stderr_path,
                    max_stream_bytes,
                )
                if "--dry-run" not in argv:
                    written = self.project / target
                    written.parent.mkdir(parents=True)
                    written.write_bytes(b"review prompt\n")
                return outcome

        writer = self.writer(
            WritingRunner([self.dry_payload(target), real])
        )
        produced = writer.invoke("make-review-prompt", None)
        self.assertEqual(produced, self.project / target)
        self.assertTrue(produced.is_file())

    def test_legacy_target_exists_intent_cannot_self_authorize_recovery(self):
        """The reviewed supervisor accepted this without any fresh check."""

        from frutlups_drive.supervisor import Supervisor

        target = "prompts/for_coding_agent/003_x.md"
        target_path = self.project / target
        target_path.write_bytes(b"prompt\n")
        (self.project / "PROJECT_STATE.md").write_bytes(b"extra effect\n")
        run_dir = self.dir / "run_001"
        run_dir.mkdir()
        intent = run_dir / "pending_verb.json"
        intent.write_text(
            json.dumps({"verb": "make-coding-prompt", "target": target}),
            encoding="utf-8",
        )

        class Store:
            def run_dir(self, run_id):
                return run_dir

            def read_events(self, run_id):
                return ()

        class Probe:
            _store = Store()
            _run_id = "run_001"
            _project_root = self.project
            _verb_writer = self.writer(FakeVerbRunner([]), intent_path=intent)
            journal = []

            def _journal(probe, kind, **fields):
                probe.journal.append((kind, fields))

            def _stop(probe, reason, detail, **fields):
                return (reason, detail)

        probe = Probe()
        stopped = Supervisor._reconcile_pending_verb(probe)
        self.assertIsNotNone(stopped)
        self.assertFalse(
            any(kind == "verb" for kind, _ in probe.journal),
            "recovery must not publish a verb fact",
        )
        self.assertTrue(intent.is_file(), "refused evidence stays preserved")

    def test_no_effect_pending_transaction_is_cleared_for_unchanged_retry(self):
        writer = self.crash_after_validation()
        intent = self.dir / "store" / "pending_verb.json"
        before = writer._workspace_snapshot()
        recovered = writer.reconcile_pending()
        self.assertIsNone(recovered)
        self.assertEqual(writer._workspace_snapshot(), before)
        self.assertFalse(intent.exists())

    def test_recovery_rejects_target_plus_extra_effect_and_preserves_witness(self):
        writer = self.crash_after_validation()
        target = self.project / "prompts/for_coding_agent/003_x.md"
        target.write_bytes(b"prompt\n")
        extra = self.project / "PROJECT_STATE.md"
        extra.write_bytes(b"unauthorized\n")
        before = extra.read_bytes()
        with self.assertRaises(FrutlupsVerbError) as caught:
            writer.reconcile_pending()
        self.assertEqual(caught.exception.code, "verb_extra_effect")
        self.assertEqual(extra.read_bytes(), before)
        self.assertTrue((self.dir / "store/pending_verb.json").is_file())

    def test_recovery_accepts_target_with_new_ordinary_ancestor_directory(self):
        target = "prompts/for_review_agent/002_x.md"
        writer = self.crash_after_validation(target, "make-review-prompt")
        target_path = self.project / target
        target_path.parent.mkdir(parents=True)
        target_path.write_bytes(b"review prompt\n")
        recovered = writer.reconcile_pending()
        self.assertEqual(recovered, ("make-review-prompt", target_path))
        self.assertTrue((self.dir / "store/pending_verb.json").is_file())

    def test_recovery_rejects_deletion_accompanying_target(self):
        preserved = self.project / "preserved.txt"
        preserved.write_bytes(b"before\n")
        writer = self.crash_after_validation()
        target = self.project / "prompts/for_coding_agent/003_x.md"
        target.write_bytes(b"prompt\n")
        preserved.unlink()
        with self.assertRaises(FrutlupsVerbError) as caught:
            writer.reconcile_pending()
        self.assertEqual(caught.exception.code, "verb_extra_effect")

    def test_recovery_rejects_modification_accompanying_target(self):
        preserved = self.project / "preserved.txt"
        preserved.write_bytes(b"before\n")
        writer = self.crash_after_validation()
        target = self.project / "prompts/for_coding_agent/003_x.md"
        target.write_bytes(b"prompt\n")
        preserved.write_bytes(b"after\n")
        with self.assertRaises(FrutlupsVerbError) as caught:
            writer.reconcile_pending()
        self.assertEqual(caught.exception.code, "verb_extra_effect")

    def test_recovery_rejects_target_absent_plus_unrelated_change(self):
        writer = self.crash_after_validation()
        extra = self.project / "PROJECT_STATE.md"
        extra.write_bytes(b"unauthorized\n")
        with self.assertRaises(FrutlupsVerbError) as caught:
            writer.reconcile_pending()
        self.assertEqual(caught.exception.code, "verb_extra_effect")
        self.assertTrue((self.dir / "store/pending_verb.json").is_file())

    def test_recovery_rejects_target_absent_plus_empty_directory_effect(self):
        writer = self.crash_after_validation()
        (self.project / "unrelated-empty-directory").mkdir()
        with self.assertRaises(FrutlupsVerbError) as caught:
            writer.reconcile_pending()
        self.assertEqual(caught.exception.code, "verb_extra_effect")

    def test_recovery_rejects_a_nonancestor_directory_accompanying_target(self):
        writer = self.crash_after_validation()
        target = self.project / "prompts/for_coding_agent/003_x.md"
        target.write_bytes(b"prompt\n")
        (self.project / "unrelated-empty-directory").mkdir()
        with self.assertRaises(FrutlupsVerbError) as caught:
            writer.reconcile_pending()
        self.assertEqual(caught.exception.code, "verb_extra_effect")

    def test_recovery_rejects_link_like_ancestor_manifest_entry(self):
        target = "prompts/for_review_agent/002_x.md"
        self.crash_after_validation(target, "make-review-prompt")
        target_path = self.project / target
        target_path.parent.mkdir(parents=True)
        target_path.write_bytes(b"review prompt\n")

        from frutlups_drive.workspace import WorkspaceManager

        manager = WorkspaceManager(
            self.project, self.project / ".frutlups_drive"
        )
        after = manager.transaction_snapshot(self.project)
        after["prompts/for_review_agent"] = hashlib.sha256(
            b"link-like-directory\0outside"
        ).hexdigest()
        recovering = self.writer(
            FakeVerbRunner([]), snapshot=lambda: dict(after)
        )
        with self.assertRaises(FrutlupsVerbError) as caught:
            recovering.reconcile_pending()
        self.assertEqual(caught.exception.code, "verb_extra_effect")

    def test_normal_completion_rejects_target_plus_extra_before_fresh_status(self):
        target = "prompts/for_coding_agent/003_x.md"
        real = json.dumps(
            {
                "errors": [],
                "valid": True,
                "preview": {"target_path": target},
                "write_result": {"wrote": True, "overwrote": False},
            }
        )

        class ExtraWritingRunner(FakeVerbRunner):
            def run(runner, argv, cwd, env, timeout_seconds, stdout_path,
                    stderr_path, max_stream_bytes=1_048_576):
                outcome = super().run(
                    argv, cwd, env, timeout_seconds, stdout_path, stderr_path,
                    max_stream_bytes,
                )
                if "--dry-run" not in argv:
                    (self.project / target).write_bytes(b"prompt\n")
                    (self.project / "PROJECT_STATE.md").write_bytes(b"extra\n")
                return outcome

        status_calls = []
        writer = self.writer(
            ExtraWritingRunner([self.dry_payload(), real]),
            status_reader=lambda: status_calls.append(True),
        )
        with self.assertRaises(FrutlupsVerbError) as caught:
            writer.invoke("make-coding-prompt", None)
        self.assertEqual(caught.exception.code, "verb_extra_effect")
        self.assertEqual(status_calls, [])
        self.assertTrue((self.dir / "store/pending_verb.json").is_file())

    def test_nonzero_real_invocation_still_fences_its_extra_effect(self):
        class FailingExtraRunner(FakeVerbRunner):
            def run(runner, argv, cwd, env, timeout_seconds, stdout_path,
                    stderr_path, max_stream_bytes=1_048_576):
                outcome = super().run(
                    argv, cwd, env, timeout_seconds, stdout_path, stderr_path,
                    max_stream_bytes,
                )
                if "--dry-run" not in argv:
                    (self.project / "PROJECT_STATE.md").write_bytes(b"extra\n")
                return outcome

        writer = self.writer(
            FailingExtraRunner(
                [self.dry_payload(), "{}"], exit_codes=[0, 7]
            )
        )
        with self.assertRaises(FrutlupsVerbError) as caught:
            writer.invoke("make-coding-prompt", None)
        self.assertEqual(caught.exception.code, "verb_extra_effect")
        self.assertTrue((self.dir / "store/pending_verb.json").is_file())

    def test_changed_identity_blocks_real_write_after_authorized_dry_run(self):
        changed = replace(self.identity, binding_sha256="e" * 64)
        calls = {"identity": 0}

        def current_identity():
            calls["identity"] += 1
            return self.identity if calls["identity"] <= 4 else changed

        runner = FakeVerbRunner([self.dry_payload()])
        writer = self.writer(runner, identity_reader=current_identity)
        with self.assertRaises(FrutlupsVerbError) as caught:
            writer.invoke("make-coding-prompt", None)
        self.assertEqual(caught.exception.code, "verb_identity_changed")
        self.assertEqual(runner.calls, 1, "the real subprocess was not launched")

    def test_recovery_rejects_nonordinary_target(self):
        writer = self.crash_after_validation()
        target = self.project / "prompts/for_coding_agent/003_x.md"
        target.mkdir()
        with self.assertRaises(FrutlupsVerbError) as caught:
            writer.reconcile_pending()
        self.assertEqual(caught.exception.code, "verb_effect_invalid")

    def test_recovery_rejects_changed_launch_identity_before_fresh_status(self):
        writer = self.crash_after_validation()
        target = self.project / "prompts/for_coding_agent/003_x.md"
        target.write_bytes(b"prompt\n")
        changed = replace(self.identity, executable_sha256="f" * 64)
        status_calls = []
        recovering = self.writer(
            FakeVerbRunner([]),
            identity_reader=lambda: changed,
            status_reader=lambda: status_calls.append(True),
        )
        with self.assertRaises(FrutlupsVerbError) as caught:
            recovering.reconcile_pending()
        self.assertEqual(caught.exception.code, "verb_identity_changed")
        self.assertEqual(status_calls, [])

    def test_no_effect_recovery_rechecks_identity_before_clearing_witness(self):
        self.crash_after_validation()
        changed = replace(self.identity, layout_sha256="d" * 64)
        calls = {"count": 0}

        def current_identity():
            calls["count"] += 1
            return self.identity if calls["count"] == 1 else changed

        recovering = self.writer(
            FakeVerbRunner([]), identity_reader=current_identity
        )
        with self.assertRaises(FrutlupsVerbError) as caught:
            recovering.reconcile_pending()
        self.assertEqual(caught.exception.code, "verb_identity_changed")
        self.assertTrue((self.dir / "store/pending_verb.json").is_file())

    def test_recovery_rejects_canonical_forged_or_identity_incomplete_witness(self):
        writer = self.crash_after_validation()
        intent = self.dir / "store/pending_verb.json"
        original = json.loads(intent.read_text(encoding="utf-8"))
        variants = []
        unknown = json.loads(json.dumps(original))
        unknown["forged"] = True
        variants.append(unknown)
        incomplete = json.loads(json.dumps(original))
        del incomplete["launch_identity"]["frutlups_layout_sha256"]
        variants.append(incomplete)
        for payload in variants:
            with self.subTest(keys=sorted(payload)):
                intent.write_text(
                    json.dumps(
                        payload, separators=(",", ":"), sort_keys=True
                    ) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaises(FrutlupsVerbError) as caught:
                    writer.reconcile_pending()
                self.assertEqual(caught.exception.code, "verb_intent_invalid")
                self.assertTrue(intent.is_file())

    def test_completed_recovery_reuses_fence_and_fresh_status_without_spawn(self):
        writer = self.crash_after_validation()
        target = self.project / "prompts/for_coding_agent/003_x.md"
        target.write_bytes(b"prompt\n")
        runner = FakeVerbRunner([])
        status_calls = []
        recovering = self.writer(
            runner,
            status_reader=lambda: (
                status_calls.append(True) or self.progressed_state()
            ),
        )
        recovered = recovering.reconcile_pending()
        self.assertEqual(recovered, ("make-coding-prompt", target))
        self.assertEqual(runner.calls, 0)
        self.assertEqual(status_calls, [True])
        self.assertTrue((self.dir / "store/pending_verb.json").is_file())

    def test_invalid_fresh_state_cannot_certify_a_completed_recovery(self):
        self.crash_after_validation()
        target = self.project / "prompts/for_coding_agent/003_x.md"
        target.write_bytes(b"prompt\n")
        invalid = replace(
            self.progressed_state(), outcome=PlanOutcome.INVALID, step=None
        )
        recovering = self.writer(
            FakeVerbRunner([]), status_reader=lambda: invalid
        )
        with self.assertRaises(FrutlupsVerbError) as caught:
            recovering.reconcile_pending()
        self.assertEqual(caught.exception.code, "verb_post_state_invalid")
        self.assertTrue((self.dir / "store/pending_verb.json").is_file())


if __name__ == "__main__":
    unittest.main()
