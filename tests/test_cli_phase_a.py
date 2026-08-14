"""M003 Phase A CLI lanes: read-only dry-run identity/gate reporting, seat
identity in the run manifest, and pre-effect refusal of every external
adapter vocabulary value with a byte/member-identical project."""

import shutil
import sys
import tomllib
import unittest

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

from frutlups_drive.contracts import ExitCode
from frutlups_drive.cli import CliRefusal, _build_launch_identity
from frutlups_drive.frutlupscli import BINDING_SCHEMA_VERSION, load_launch_binding
from frutlups_drive.policy import load_execution_policy
from frutlups_drive.runstore import RunStore
from frutlups_drive.supervisor import EXTERNAL_ADAPTERS

from test_cli import FIXTURE_PROJECT, CliTestCase


class PhaseACliTestCase(CliTestCase):
    def setUp(self):
        super().setUp()
        self._fixture_serial = 0

    def copy_fixture(self):
        # Unique target per copy so subTest loops get isolated projects.
        self._fixture_serial += 1
        target = self.tmp / f"driven_project_{self._fixture_serial:02d}"
        shutil.copytree(FIXTURE_PROJECT, target)
        return target

SEAT_POLICY = b"""schema_version = "frutlups_drive_policy_v1"

[roles.architect]
adapter = "mock"

[roles.coder]
adapter = "mock"
model = "model-alpha"

[roles.reviewer]
adapter = "mock"
model = "model-beta"
"""


class DryRunReportTests(PhaseACliTestCase):
    def test_dry_run_reports_identities_alias_provider_and_gate(self):
        project = self.copy_fixture()
        (project / "frutlups_drive.toml").write_bytes(SEAT_POLICY)
        before = self.tree_snapshot(project)
        code, out, err = self.invoke("plan", str(project), "--dry-run")
        self.assertEqual(code, int(ExitCode.OK), err)
        self.assertIn("role architect: adapter=mock model=(empty)", out)
        self.assertIn("role coder: adapter=mock model=model-alpha", out)
        self.assertIn("role reviewer: adapter=mock model=model-beta", out)
        self.assertIn("coder/reviewer exact-seat alias: no", out)
        self.assertIn("planning provider: mock convention", out)
        self.assertIn("live authority: absent", out)
        self.assertEqual(self.tree_snapshot(project), before)
        self.assertFalse((project / ".frutlups_drive").exists())

    def test_dry_run_reports_exact_alias_when_seats_are_identical(self):
        project = self.copy_fixture()
        code, out, _ = self.invoke("plan", str(project), "--dry-run")
        self.assertEqual(code, int(ExitCode.OK))
        # the fixture seats all mock roles with the empty-model convention,
        # so coder and reviewer are exact aliases
        self.assertIn("coder/reviewer exact-seat alias: yes", out)

    def test_dry_run_flags_external_seat_without_model(self):
        project = self.copy_fixture()
        (project / "frutlups_drive.toml").write_bytes(
            b'schema_version = "frutlups_drive_policy_v1"\n'
            b'[roles.coder]\nadapter = "api_call"\n'
        )
        before = self.tree_snapshot(project)
        code, out, _ = self.invoke("plan", str(project), "--dry-run")
        self.assertEqual(code, int(ExitCode.OK))
        self.assertIn(
            "role coder: adapter=api_call model=(empty) "
            "[not executable: external_model_missing]",
            out,
        )
        self.assertIn("live authority: absent", out)
        self.assertEqual(self.tree_snapshot(project), before)

    def test_dry_run_without_policy_reports_unavailable_identities(self):
        project = self.copy_fixture()
        (project / "frutlups_drive.toml").unlink()
        code, out, _ = self.invoke("plan", str(project), "--dry-run")
        self.assertEqual(code, int(ExitCode.OK))
        self.assertIn("role identities: unavailable (policy absent)", out)
        self.assertIn("live authority: absent", out)
        self.assertFalse((project / ".frutlups_drive").exists())


class ManifestSeatIdentityTests(PhaseACliTestCase):
    def test_run_manifest_records_exact_seats_and_alias_fact(self):
        project = self.copy_fixture()
        (project / "frutlups_drive.toml").write_bytes(SEAT_POLICY)
        code, _, err = self.invoke(
            "run", str(project), "--until", "slice_complete"
        )
        self.assertEqual(code, int(ExitCode.OK), err)
        manifest = tomllib.loads(
            (project / ".frutlups_drive/runs/run_001/manifest.toml")
            .read_bytes()
            .decode("utf-8")
        )
        self.assertEqual(manifest["architect_adapter"], "mock")
        self.assertEqual(manifest["architect_model"], "")
        self.assertEqual(manifest["coder_adapter"], "mock")
        self.assertEqual(manifest["coder_model"], "model-alpha")
        self.assertEqual(manifest["reviewer_adapter"], "mock")
        self.assertEqual(manifest["reviewer_model"], "model-beta")
        self.assertIs(manifest["coder_reviewer_exact_alias"], False)

    def test_request_records_carry_the_manifest_seats(self):
        project = self.copy_fixture()
        (project / "frutlups_drive.toml").write_bytes(SEAT_POLICY)
        code, _, err = self.invoke(
            "run", str(project), "--until", "slice_complete"
        )
        self.assertEqual(code, int(ExitCode.OK), err)
        import json

        attempts_root = (
            project / ".frutlups_drive/runs/run_001/slices/M001-S01"
        )
        models = {}
        for attempt in sorted(attempts_root.iterdir()):
            request_path = attempt / "request.json"
            if request_path.is_file():
                record = json.loads(request_path.read_bytes().decode("utf-8"))
                models[record["role"]] = (record["adapter"], record["model"])
        self.assertEqual(models["coder"], ("mock", "model-alpha"))
        self.assertEqual(models["reviewer"], ("mock", "model-beta"))


class ReleasedLaunchIdentityResumeTests(PhaseACliTestCase):
    def test_nonempty_package_identity_must_match_before_store(self):
        project = self.copy_fixture()
        policy_path = project / "frutlups_drive.toml"
        policy_path.write_text(
            policy_path.read_text(encoding="utf-8")
            + '\n[frutlups]\nprovider = "frutlups_cli"\n'
              'package_identity = "frutlups==0.1.0"\n',
            encoding="utf-8",
        )
        (project / "frutlups.layout.yaml").write_bytes(b"layout-v1\n")
        local = project / "local_state"
        local.mkdir()
        executable = local / "bound-python.exe"
        shutil.copyfile(sys.executable, executable)
        escaped = str(executable).replace("\\", "\\\\")
        binding_path = local / "frutlups_binding.toml"
        binding_path.write_text(
            f'schema_version = "{BINDING_SCHEMA_VERSION}"\n'
            '[launch]\n'
            f'argv_prefix = ["{escaped}", "-m", "frutlups"]\n'
            'tool_identity = "frutlups==0.1.1"\n'
            '[env]\n',
            encoding="utf-8",
        )
        policy = load_execution_policy(policy_path).policy
        binding = load_launch_binding(binding_path)
        before = self.tree_snapshot(project)
        with self.assertRaises(CliRefusal) as caught:
            _build_launch_identity(
                project, policy, binding, policy_path.read_bytes()
            )
        self.assertEqual(caught.exception.code, "package_identity_mismatch")
        self.assertEqual(self.tree_snapshot(project), before)
        self.assertFalse((project / ".frutlups_drive").exists())

    def prepared_run(self):
        project = self.copy_fixture()
        policy_path = project / "frutlups_drive.toml"
        policy_path.write_text(
            policy_path.read_text(encoding="utf-8")
            + '\n[frutlups]\nprovider = "frutlups_cli"\n'
              'package_identity = "frutlups==0.1.0"\n',
            encoding="utf-8",
        )
        (project / "frutlups.layout.yaml").write_bytes(b"layout-v1\n")
        local = project / "local_state"
        local.mkdir()
        executable = local / "bound-python.exe"
        shutil.copyfile(sys.executable, executable)
        escaped = str(executable).replace("\\", "\\\\")
        binding_path = local / "frutlups_binding.toml"
        binding_path.write_text(
            f'schema_version = "{BINDING_SCHEMA_VERSION}"\n'
            '[launch]\n'
            f'argv_prefix = ["{escaped}", "-m", "frutlups"]\n'
            'tool_identity = "frutlups==0.1.0"\n'
            '[env]\n',
            encoding="utf-8",
        )
        policy = load_execution_policy(policy_path).policy
        binding = load_launch_binding(binding_path)
        identity = _build_launch_identity(
            project, policy, binding, policy_path.read_bytes()
        )
        store = RunStore(project / ".frutlups_drive")
        store.create_run(
            "run_001",
            {
                "boundary": "slice_complete",
                "contract_version": 1,
                **identity.manifest_facts(),
            },
        )
        return project, policy_path, binding_path, executable

    def test_every_launch_authority_change_refuses_resume_without_mutation(self):
        def binding_edit(path, old, new):
            path.write_text(
                path.read_text(encoding="utf-8").replace(old, new),
                encoding="utf-8",
            )

        def executable_path_edit(project, policy, binding, executable):
            alternate = executable.with_name("alternate-python.exe")
            shutil.copyfile(executable, alternate)
            binding_edit(
                binding,
                str(executable).replace("\\", "\\\\"),
                str(alternate).replace("\\", "\\\\"),
            )

        mutations = (
            lambda p, policy, binding, executable: binding_edit(
                binding, '"-m", "frutlups"', '"-X", "utf8", "-m", "frutlups"'
            ),
            lambda p, policy, binding, executable: binding.write_text(
                binding.read_text(encoding="utf-8") + 'LANG = "C"\n',
                encoding="utf-8",
            ),
            lambda p, policy, binding, executable: executable.write_bytes(
                executable.read_bytes() + b"changed"
            ),
            executable_path_edit,
            lambda p, policy, binding, executable: (
                (p / "frutlups.layout.yaml").write_bytes(b"layout-v2\n")
            ),
            lambda p, policy, binding, executable: binding_edit(
                policy, "frutlups==0.1.0", "frutlups==0.1.1"
            ),
            lambda p, policy, binding, executable: binding_edit(
                policy, 'provider = "frutlups_cli"', 'provider = "mock"'
            ),
            lambda p, policy, binding, executable: (
                p / "frutlups.layout.yaml"
            ).unlink(),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                project, policy, binding, executable = self.prepared_run()
                mutate(project, policy, binding, executable)
                before = self.tree_snapshot(project)
                code, _, _ = self.invoke("resume", str(project), "run_001")
                self.assertEqual(code, int(ExitCode.REFUSED))
                self.assertEqual(self.tree_snapshot(project), before)


class ExternalAdapterRefusalTests(PhaseACliTestCase):
    def external_policy(self, adapter, role="coder"):
        return (
            b'schema_version = "frutlups_drive_policy_v1"\n'
            + f'[roles.{role}]\nadapter = "{adapter}"\n'.encode("utf-8")
        )

    def test_every_external_adapter_value_refuses_run_before_mutation(self):
        for adapter in EXTERNAL_ADAPTERS:
            with self.subTest(adapter=adapter):
                project = self.copy_fixture()
                (project / "frutlups_drive.toml").write_bytes(
                    self.external_policy(adapter)
                )
                before = self.tree_snapshot(project)
                code, _, err = self.invoke(
                    "run", str(project), "--until", "slice_complete"
                )
                self.assertEqual(code, int(ExitCode.REFUSED))
                self.assertIn("live_authority_missing", err)
                self.assertEqual(self.tree_snapshot(project), before)
                self.assertFalse((project / ".frutlups_drive").exists())

    def test_every_external_adapter_value_refuses_resume_before_mutation(self):
        for adapter in EXTERNAL_ADAPTERS:
            with self.subTest(adapter=adapter):
                project = self.copy_fixture()
                (project / "frutlups_drive.toml").write_bytes(
                    self.external_policy(adapter, role="reviewer")
                )
                before = self.tree_snapshot(project)
                code, _, err = self.invoke("resume", str(project), "run_001")
                self.assertEqual(code, int(ExitCode.REFUSED))
                self.assertIn("live_authority_missing", err)
                self.assertEqual(self.tree_snapshot(project), before)
                self.assertFalse((project / ".frutlups_drive").exists())

    def test_external_mixed_with_manual_still_lacks_live_authority(self):
        project = self.copy_fixture()
        (project / "frutlups_drive.toml").write_bytes(
            b'schema_version = "frutlups_drive_policy_v1"\n'
            b'[roles.coder]\nadapter = "manual"\n'
            b'[roles.reviewer]\nadapter = "api_call"\n'
        )
        before = self.tree_snapshot(project)
        code, _, err = self.invoke(
            "run", str(project), "--until", "slice_complete"
        )
        self.assertEqual(code, int(ExitCode.REFUSED))
        self.assertIn("live_authority_missing", err)
        self.assertEqual(self.tree_snapshot(project), before)

    def test_manual_only_configuration_keeps_its_stable_refusal(self):
        project = self.copy_fixture()
        (project / "frutlups_drive.toml").write_bytes(
            b'schema_version = "frutlups_drive_policy_v1"\n'
            b'[roles.architect]\nadapter = "manual"\n'
            b'[roles.coder]\nadapter = "manual"\n'
            b'[roles.reviewer]\nadapter = "manual"\n'
        )
        code, _, err = self.invoke(
            "run", str(project), "--until", "slice_complete"
        )
        self.assertEqual(code, int(ExitCode.REFUSED))
        self.assertIn("adapter_unavailable", err)
        self.assertFalse((project / ".frutlups_drive").exists())

    def test_no_flag_accepts_an_executable_provider_or_authority(self):
        # The Phase A CLI grammar exposes no executable, provider,
        # credential, network, approval, or cost-override option.
        for forbidden in (
            ("run", ".", "--until", "slice_complete", "--provider", "x"),
            ("run", ".", "--until", "slice_complete", "--executable", "x"),
            ("run", ".", "--until", "slice_complete", "--api-key", "x"),
            ("run", ".", "--until", "slice_complete", "--live", "yes"),
            ("run", ".", "--until", "slice_complete", "--max-cost", "9"),
            ("plan", ".", "--credential", "x"),
        ):
            with self.subTest(argv=forbidden):
                code, _, _ = self.invoke(*forbidden)
                self.assertEqual(code, int(ExitCode.REFUSED))


if __name__ == "__main__":
    unittest.main()
