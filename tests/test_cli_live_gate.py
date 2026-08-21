"""CLI fail-closed live-gate admission lanes before run-store creation."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401

from frutlups_drive.contracts import ExitCode
from frutlups_drive.dispatch.provider_cli import PROVIDER_BINDING_RELATIVE_PATH
from frutlups_drive.livegate import MAX_LIVE_GATE_FILE_BYTES

from test_cli import FIXTURE_PROJECT, CliTestCase
from test_livegate import gate_markdown


EXTERNAL_POLICY = b'''schema_version = "frutlups_drive_policy_v1"

[roles.architect]
adapter = "claude_cli"
model = "claude-opus-5"

[roles.coder]
adapter = "codex_cli"
model = "gpt-5.6-sol"

[roles.reviewer]
adapter = "kimi_cli"
model = "kimi-code/k3"

[limits]
max_total_cost_usd = 10.0
'''


class LiveGateCliTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.tmp / "external_project"
        shutil.copytree(FIXTURE_PROJECT, self.project)
        (self.project / "frutlups_drive.toml").write_bytes(EXTERNAL_POLICY)
        self.gate = self.tmp / "live_validation_gate.md"

    def invoke_with_gate(self, *args):
        with patch("frutlups_drive.cli.LIVE_GATE_PATH", self.gate):
            return self.invoke(*args)

    def assert_pre_store_refusal(self, expected):
        before = self.tree_snapshot(self.project)
        code, _, err = self.invoke_with_gate(
            "run", str(self.project), "--until", "slice_complete"
        )
        self.assertEqual(code, int(ExitCode.REFUSED))
        self.assertIn(expected, err)
        self.assertEqual(self.tree_snapshot(self.project), before)
        self.assertFalse((self.project / ".frutlups_drive").exists())

    def test_missing_duplicated_oversized_and_unapproved_gate_refuse(self):
        cases = (
            (None, "gate_file_missing"),
            (gate_markdown() + gate_markdown(), "gate_fence_count_invalid"),
            ("x" * (MAX_LIVE_GATE_FILE_BYTES + 1), "gate_file_oversized"),
            (gate_markdown("proposed"), "approval_missing"),
        )
        for content, expected in cases:
            with self.subTest(expected=expected):
                if self.gate.exists():
                    self.gate.unlink()
                if content is not None:
                    self.gate.write_text(content, encoding="utf-8")
                self.assert_pre_store_refusal(expected)

    def test_exact_seat_mismatch_refuses(self):
        self.gate.write_text(
            gate_markdown().replace('reviewer_model = "kimi-code/k3"',
                                    'reviewer_model = "kimi-code/other"'),
            encoding="utf-8",
        )
        self.assert_pre_store_refusal("live_authority_missing")

    def test_architect_seat_mismatch_refuses(self):
        self.gate.write_text(
            gate_markdown().replace(
                'architect_model = "claude-opus-5"',
                'architect_model = "claude-opus-other"',
            ),
            encoding="utf-8",
        )
        self.assert_pre_store_refusal("live_authority_missing")

    def test_corrective_effort_mismatch_on_either_side_refuses(self):
        cases = (
            (
                EXTERNAL_POLICY.replace(
                    b'model = "gpt-5.6-sol"',
                    b'model = "gpt-5.6-sol"\ncorrective_effort = "high"',
                ),
                gate_markdown(),
            ),
            (
                EXTERNAL_POLICY,
                gate_markdown().replace(
                    'coder_model = "gpt-5.6-sol"',
                    'coder_model = "gpt-5.6-sol"\n'
                    'coder_corrective_effort = "high"',
                ),
            ),
        )
        for policy, gate in cases:
            with self.subTest(policy_corrective=b"corrective_effort" in policy):
                (self.project / "frutlups_drive.toml").write_bytes(policy)
                self.gate.write_text(gate, encoding="utf-8")
                self.assert_pre_store_refusal("live_authority_missing")

    def test_ready_gate_then_absent_local_binding_refuses_before_store(self):
        self.gate.write_text(gate_markdown(), encoding="utf-8")
        self.assertFalse(
            (self.project / PROVIDER_BINDING_RELATIVE_PATH).exists()
        )
        self.assert_pre_store_refusal("provider_binding_missing")

    def test_all_mock_run_needs_no_gate_file(self):
        local = self.tmp / "local_project"
        shutil.copytree(FIXTURE_PROJECT, local)
        missing_gate = self.tmp / "does-not-exist.md"
        with patch("frutlups_drive.cli.LIVE_GATE_PATH", missing_gate):
            code, _, err = self.invoke(
                "run", str(local), "--until", "slice_complete"
            )
        self.assertEqual(code, int(ExitCode.OK), err)
        self.assertTrue((local / ".frutlups_drive").is_dir())


if __name__ == "__main__":
    unittest.main()
