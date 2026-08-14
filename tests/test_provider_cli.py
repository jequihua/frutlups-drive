"""Offline command-binding lanes for the three approved subscription CLIs."""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import _bootstrap  # noqa: F401

from frutlups_drive.contracts import AgentRunRequest, Role
from frutlups_drive.dispatch.provider_cli import (
    APPROVED_SEAT_CATALOG,
    PROVIDER_BINDING_SCHEMA_VERSION,
    ProviderBindingError,
    ProviderCliExecutor,
    build_provider_runtime,
    load_provider_binding,
)
from frutlups_drive.livegate import assess_live_gate
from frutlups_drive.verifier import ProcessOutcome, SubprocessRunner

from _scenario import FakeClock


def gate_declaration(
    timeout=5.0,
    *,
    coder_model="gpt-5.6-sol",
    reviewer_model="kimi-code/k3",
    architect_model="claude-opus-5",
):
    return assess_live_gate(
        {
            "approval_state": "approved",
            "approval_reference": "05_governance/human_owner_notes/099_test.md",
            "coder_adapter": "codex_cli",
            "coder_model": coder_model,
            "reviewer_adapter": "kimi_cli",
            "reviewer_model": reviewer_model,
            "architect_adapter": "claude_cli",
            "architect_model": architect_model,
            "credential_env_names": [
                "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA"
            ],
            "max_total_cost_usd": 10.0,
            "max_call_cost_usd": 2.0,
            "call_timeout_seconds": timeout,
            "rollback_statement": "Delete the disposable fixture.",
            "kill_switch_statement": "Create the declared stop file.",
            "stop_conditions": {
                "cost": "Stop at the cost ceiling.",
                "time": "Stop at the time ceiling.",
                "human": "Stop on owner instruction.",
            },
        }
    ).declaration


class CapturingRunner:
    def __init__(self):
        self.calls = []
        self.stdin_bytes = []

    def run(self, argv, cwd, env, timeout_seconds, stdout_path, stderr_path,
            max_stream_bytes=1_048_576, stdin_bytes=None):
        self.calls.append((tuple(argv), Path(cwd), dict(env), timeout_seconds))
        self.stdin_bytes.append(stdin_bytes)
        Path(stdout_path).write_bytes(b'usage summary: {"tokens":7}\n')
        Path(stderr_path).write_bytes(b"provider progress\n")
        return ProcessOutcome(0.0, 0.1, 0, False)


class FailingRunner:
    def run(self, *args, **kwargs):
        raise OSError("simulated spawn failure")


class ProviderBindingCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.stub = self.root / "stub_provider.py"
        self.stub.write_text(
            "import sys\nprint('usage summary: stub subscription')\n",
            encoding="utf-8",
        )
        self.config = self.root / "config.toml"
        self.write_config("high")
        self.binding = self.root / "provider_binding.toml"
        self.write_binding()

    def write_config(self, effort, *, model_efforts=None):
        efforts = {
            "kimi-code/k3": "high",
            "kimi-code/kimi-for-coding": "high",
            "kimi-code/kimi-for-coding-highspeed": "high",
        }
        efforts.update(model_efforts or {})
        sections = ["[thinking]", f'effort = "{effort}"']
        for model, default in efforts.items():
            supported = '["low"]' if default == "low" else '["low", "high", "max"]'
            sections.extend((
                f'[models."{model}"]',
                f"support_efforts = {supported}",
                f'default_effort = "{default}"',
            ))
        self.config.write_text("\n".join(sections) + "\n", encoding="utf-8")

    def write_binding(self, *, executable=None):
        executable = str(executable or Path(sys.executable))
        def quoted(value):
            return json.dumps(str(value).replace("\\", "/"))
        self.binding.write_text(
            f'schema_version = "{PROVIDER_BINDING_SCHEMA_VERSION}"\n'
            f'kimi_config_path = {quoted(self.config)}\n'
            "[codex_cli]\n"
            f'argv_prefix = [{quoted(executable)}, {quoted(self.stub)}, "codex"]\n'
            "[kimi_cli]\n"
            f'argv_prefix = [{quoted(executable)}, {quoted(self.stub)}, "kimi"]\n'
            "[claude_cli]\n"
            f'argv_prefix = [{quoted(executable)}, {quoted(self.stub)}, "claude"]\n',
            encoding="utf-8",
        )

    def ambient(self):
        values = {
            "USERPROFILE": "profile",
            "HOME": "home",
            "APPDATA": "appdata",
            "LOCALAPPDATA": "localappdata",
            "PATH": "must-not-pass",
        }
        if sys.platform == "win32":
            values["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        return values

    def runtime(self, timeout=5.0, declaration=None):
        return build_provider_runtime(
            load_provider_binding(self.binding),
            declaration or gate_declaration(timeout),
            self.ambient(),
        )

    def request(
        self,
        role,
        prompt="Do the bounded fixture task.",
        *,
        model=None,
        effort=None,
    ):
        prompt_path = self.workspace / f"{role.value}_prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        adapter = {
            Role.ARCHITECT: "claude_cli",
            Role.CODER: "codex_cli",
            Role.REVIEWER: "kimi_cli",
        }[role]
        return AgentRunRequest(
            run_id="run_001",
            attempt_id="attempt_001",
            role=role,
            prompt_path=prompt_path,
            prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            workspace=self.workspace,
            base_revision=None,
            adapter=adapter,
            model=model or {
                "codex_cli": "gpt-5.6-sol",
                "kimi_cli": "kimi-code/k3",
                "claude_cli": "claude-opus-5",
            }[adapter],
            effort=effort or {
                "codex_cli": "medium",
                "kimi_cli": "high",
                "claude_cli": "high",
            }[adapter],
            workspace_access=(
                "workspace_write" if role is Role.CODER else "read_only"
            ),
            expected_artifacts=(),
            max_seconds=5,
            max_cost_usd=2.0,
        )


class BindingLoaderTests(ProviderBindingCase):
    def test_catalog_is_exact_and_excludes_floating_aliases(self):
        self.assertEqual(
            APPROVED_SEAT_CATALOG,
            {
                "codex_cli": {
                    "gpt-5.6-sol": "medium",
                    "gpt-5.6-terra": "medium",
                    "gpt-5.6-luna": "medium",
                },
                "kimi_cli": {
                    "kimi-code/k3": "high",
                    "kimi-code/kimi-for-coding": "high",
                    "kimi-code/kimi-for-coding-highspeed": "high",
                },
                "claude_cli": {"claude-opus-5": "high"},
            },
        )
        self.assertNotIn("gpt-5.6", APPROVED_SEAT_CATALOG["codex_cli"])
        self.assertNotIn("opus", APPROVED_SEAT_CATALOG["claude_cli"])

    def test_valid_binding_hashes_executables_and_resolves_high(self):
        bundle = load_provider_binding(self.binding)
        self.assertEqual(bundle.kimi_effective_effort, "high")
        self.assertRegex(bundle.binding_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(
            Path(bundle.codex.argv_prefix[0]).resolve(), Path(sys.executable).resolve()
        )
        self.assertEqual(
            Path(bundle.claude.argv_prefix[0]).resolve(), Path(sys.executable).resolve()
        )

    def test_low_global_effort_stops_before_dispatch(self):
        self.write_config("low")
        with self.assertRaises(ProviderBindingError) as caught:
            load_provider_binding(self.binding)
        self.assertEqual(caught.exception.code, "kimi_effort_not_high")

    def test_absent_executable_refuses(self):
        self.write_binding(executable=self.root / "absent.exe")
        with self.assertRaises(ProviderBindingError) as caught:
            load_provider_binding(self.binding)
        self.assertEqual(caught.exception.code, "provider_executable_missing")

    def test_second_catalog_models_admit_at_binding_runtime_load(self):
        declaration = gate_declaration(
            coder_model="gpt-5.6-terra",
            reviewer_model="kimi-code/kimi-for-coding",
        )
        runtime = self.runtime(declaration=declaration)
        self.assertEqual(runtime.bundle.kimi_effective_effort, "high")

    def test_catalog_refuses_unapproved_model_adapter_and_alias_at_load(self):
        cases = (
            replace(gate_declaration(), coder_model="gpt-5.6-unknown"),
            replace(
                gate_declaration(),
                coder_adapter="api_call",
                coder_model="gpt-5.6-sol",
            ),
            replace(gate_declaration(), coder_model="gpt-5.6"),
        )
        for declaration in cases:
            with self.subTest(declaration=declaration):
                with self.assertRaises(ProviderBindingError) as caught:
                    self.runtime(declaration=declaration)
                self.assertEqual(caught.exception.code, "provider_seat_mismatch")

    def test_selected_kimi_catalog_entry_must_resolve_to_its_pinned_effort(self):
        self.write_config(
            "high",
            model_efforts={"kimi-code/kimi-for-coding": "low"},
        )
        with self.assertRaises(ProviderBindingError) as caught:
            self.runtime(
                declaration=gate_declaration(
                    reviewer_model="kimi-code/kimi-for-coding"
                )
            )
        self.assertEqual(caught.exception.code, "kimi_effort_not_high")

    def test_selected_kimi_catalog_entry_must_exist_in_local_config(self):
        self.write_config("high")
        text = self.config.read_text(encoding="utf-8")
        start = text.index('[models."kimi-code/kimi-for-coding"]')
        end = text.index(
            '[models."kimi-code/kimi-for-coding-highspeed"]', start
        )
        self.config.write_text(text[:start] + text[end:], encoding="utf-8")
        with self.assertRaises(ProviderBindingError) as caught:
            self.runtime(
                declaration=gate_declaration(
                    reviewer_model="kimi-code/kimi-for-coding"
                )
            )
        self.assertEqual(caught.exception.code, "kimi_effort_not_high")

    def test_configured_entry_without_effort_metadata_is_skipped_not_fatal(self):
        # R2-F1: real host configs list approved models with only
        # provider/capability keys and no effort metadata; such entries
        # must not fail binding load, and selecting one must still
        # refuse fail-closed.
        self.write_config("high")
        text = self.config.read_text(encoding="utf-8")
        start = text.index('[models."kimi-code/kimi-for-coding"]')
        end = text.index(
            '[models."kimi-code/kimi-for-coding-highspeed"]', start
        )
        hostlike = (
            '[models."kimi-code/kimi-for-coding"]\n'
            'provider = "managed:kimi-code"\n'
            'model = "kimi-for-coding"\n'
            'display_name = "K2.7 Coding"\n'
        )
        self.config.write_text(
            text[:start] + hostlike + text[end:], encoding="utf-8"
        )
        runtime = self.runtime()
        resolved = dict(runtime.bundle.kimi_effective_efforts)
        self.assertEqual(resolved.get("kimi-code/k3"), "high")
        self.assertNotIn("kimi-code/kimi-for-coding", resolved)
        with self.assertRaises(ProviderBindingError) as caught:
            self.runtime(
                declaration=gate_declaration(
                    reviewer_model="kimi-code/kimi-for-coding"
                )
            )
        self.assertEqual(caught.exception.code, "kimi_effort_not_high")

    def test_child_environment_is_gate_names_plus_startup_and_hygiene(self):
        names = [name for name, _ in self.runtime().child_env]
        expected = ["USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA"]
        if sys.platform == "win32":
            expected.append("SYSTEMROOT")
        expected.append("PYTHONDONTWRITEBYTECODE")
        self.assertEqual(names, expected)
        self.assertEqual(dict(self.runtime().child_env)["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertNotIn("PATH", names)


class CommandConstructionTests(ProviderBindingCase):
    def execute(self, role):
        runner = CapturingRunner()
        executor = ProviderCliExecutor(
            self.runtime(),
            {
                Role.ARCHITECT: "claude_cli",
                Role.CODER: "codex_cli",
                Role.REVIEWER: "kimi_cli",
            }[role],
            runner,
            self.root / "captures",
        )
        result = executor.execute(self.request(role))
        return runner.calls[0], result, runner

    def test_codex_command_pins_one_shot_model_effort_and_sandbox(self):
        call, result, runner = self.execute(Role.CODER)
        argv, _, env, timeout = call
        self.assertEqual(
            argv[3:],
            (
                "exec",
                "--model",
                "gpt-5.6-sol",
                "-c",
                "model_reasoning_effort=medium",
                "-c",
                "service_tier=default",
                "--sandbox",
                "workspace-write",
                "--ephemeral",
                "--skip-git-repo-check",
                "--json",
                "-",
            ),
        )
        self.assertNotIn("Do the bounded fixture task.", argv)
        self.assertNotIn("--ignore-user-config", argv)
        self.assertEqual(runner.stdin_bytes, [b"Do the bounded fixture task."])
        self.assertEqual(timeout, 5.0)
        self.assertNotIn("PATH", env)
        self.assertEqual(result.cost_usd, 0.0)

    def test_kimi_command_pins_model_and_has_no_effort_flag(self):
        call, result, runner = self.execute(Role.REVIEWER)
        argv, _, env, _ = call
        skills_dir = Path(argv[argv.index("--skills-dir") + 1])
        self.assertTrue(skills_dir.is_dir())
        self.assertIn("--prompt", argv)
        self.assertIn("kimi-code/k3", argv)
        self.assertIn("stream-json", argv)
        self.assertNotIn("--effort", argv)
        self.assertEqual(runner.stdin_bytes, [None])
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(result.cost_usd, 0.0)

    def test_second_catalog_models_dispatch_with_their_pinned_efforts(self):
        cases = (
            (Role.CODER, "codex_cli", "gpt-5.6-terra", "medium"),
            (
                Role.REVIEWER,
                "kimi_cli",
                "kimi-code/kimi-for-coding",
                "high",
            ),
        )
        for role, adapter, model, effort in cases:
            with self.subTest(adapter=adapter, model=model):
                runner = CapturingRunner()
                executor = ProviderCliExecutor(
                    self.runtime(),
                    adapter,
                    runner,
                    self.root / f"alternate-{adapter}",
                )
                result = executor.execute(
                    self.request(role, model=model, effort=effort)
                )
                self.assertEqual(result.status, "completed")
                self.assertIn(model, runner.calls[0][0])

    def test_dispatch_refuses_unapproved_model_adapter_and_alias(self):
        cases = (
            replace(self.request(Role.CODER), model="gpt-5.6-unknown"),
            replace(self.request(Role.CODER), adapter="api_call"),
            replace(self.request(Role.CODER), model="gpt-5.6"),
        )
        for index, request in enumerate(cases):
            with self.subTest(adapter=request.adapter, model=request.model):
                runner = CapturingRunner()
                executor = ProviderCliExecutor(
                    self.runtime(),
                    "codex_cli",
                    runner,
                    self.root / f"refusal-{index}",
                )
                with self.assertRaises(ProviderBindingError) as caught:
                    executor.execute(request)
                self.assertEqual(caught.exception.code, "provider_seat_mismatch")
                self.assertEqual(runner.calls, [])

    def test_claude_command_pins_one_shot_model_effort_and_write_surface(self):
        call, result, runner = self.execute(Role.ARCHITECT)
        argv, _, env, timeout = call
        self.assertEqual(
            argv[3:],
            (
                "-p",
                "--model",
                "claude-opus-5",
                "--effort",
                "high",
                "--permission-mode",
                "acceptEdits",
                "--tools",
                "Write",
                "--output-format",
                "stream-json",
                "--verbose",
                "--safe-mode",
                "--strict-mcp-config",
                "--mcp-config",
                "{}",
                "--no-session-persistence",
            ),
        )
        self.assertNotIn("Do the bounded fixture task.", argv)
        self.assertEqual(runner.stdin_bytes, [b"Do the bounded fixture task."])
        self.assertEqual(timeout, 5.0)
        self.assertNotIn("PATH", env)
        self.assertEqual(result.cost_usd, 0.0)

    def test_prompt_response_and_usage_text_are_captured_verbatim(self):
        _, result, _ = self.execute(Role.CODER)
        event_lines = [
            json.loads(line)
            for line in Path(result.event_log_path).read_text(encoding="utf-8").splitlines()
        ]
        capture_root = Path(result.event_log_path).parent
        self.assertEqual((capture_root / "prompt.md").read_text(encoding="utf-8"),
                         "Do the bounded fixture task.")
        stdout_name = event_lines[1]["stdout_capture"]
        self.assertEqual(
            (capture_root / stdout_name).read_bytes(),
            b'usage summary: {"tokens":7}\n',
        )
        self.assertEqual(event_lines[0]["effort"], "medium")

    def test_timeout_uses_accepted_process_tree_kill(self):
        self.stub.write_text(
            "import threading\nprint('usage summary: before timeout', flush=True)\n"
            "threading.Event().wait(60)\n",
            encoding="utf-8",
        )
        self.write_binding()
        runtime = self.runtime(timeout=0.2)
        executor = ProviderCliExecutor(
            runtime,
            "codex_cli",
            SubprocessRunner(FakeClock()),
            self.root / "timeout-captures",
        )
        result = executor.execute(self.request(Role.CODER))
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.exit_reason, "agent_timeout")
        self.assertEqual(result.cost_usd, 0.0)

    def test_captured_runner_failure_returns_subscription_cost_fact(self):
        executor = ProviderCliExecutor(
            self.runtime(),
            "codex_cli",
            FailingRunner(),
            self.root / "failure-captures",
        )
        result = executor.execute(self.request(Role.CODER))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.exit_reason, "agent_runner_failure")
        self.assertEqual(result.cost_usd, 0.0)
        observations = [
            json.loads(line)
            for line in Path(result.event_log_path).read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        self.assertEqual(observations[-1]["kind"], "runner_failure")

    def test_large_prompts_pipe_exact_bytes_to_stdin_capable_stubs(self):
        prompt = "x" * 50_000
        self.stub.write_text(
            "import sys\n"
            "data = sys.stdin.buffer.read()\n"
            "print(len(data))\n",
            encoding="utf-8",
        )
        self.write_binding()
        for role in (Role.CODER, Role.ARCHITECT):
            with self.subTest(role=role.value):
                executor = ProviderCliExecutor(
                    self.runtime(),
                    {
                        Role.CODER: "codex_cli",
                        Role.ARCHITECT: "claude_cli",
                    }[role],
                    SubprocessRunner(FakeClock()),
                    self.root / f"large-{role.value}",
                )
                result = executor.execute(self.request(role, prompt))
                self.assertEqual(result.status, "completed")
                stdout = Path(result.event_log_path).with_name(
                    Path(result.event_log_path).name.replace(
                        "_events.jsonl", "_stdout.txt"
                    )
                )
                self.assertEqual(stdout.read_bytes(), b"50000\r\n" if os.name == "nt" else b"50000\n")

    def test_large_kimi_prompt_refuses_stably_before_spawn(self):
        runner = CapturingRunner()
        executor = ProviderCliExecutor(
            self.runtime(), "kimi_cli", runner, self.root / "large-kimi"
        )
        with self.assertRaises(ProviderBindingError) as caught:
            executor.execute(self.request(Role.REVIEWER, "x" * 50_000))
        self.assertEqual(caught.exception.code, "provider_prompt_argv_oversized")
        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
