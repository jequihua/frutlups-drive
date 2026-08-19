"""Seat-conduct dispatch-envelope coverage for every prompt kind."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import _bootstrap  # noqa: F401

from frutlups_drive.dispatch.mock import MockAgentAction
from frutlups_drive.supervisor import (
    _MAX_SEAT_CONDUCT_BLOCK_BYTES,
    _SEAT_CONDUCT_BLOCK,
)

from _scenario import (
    CODING_PROMPT,
    DEFAULT_VERBS,
    PROMPT_BODY,
    REVIEW_REPORT,
    ROADMAP_BODY,
    SELF_REPORT,
    Scenario,
    clean_pass_states,
    payload,
)


CODER_WRITES_REPORT = MockAgentAction(
    writes=((SELF_REPORT, "# Coder Self-Report\n\nIntent:\ndone\n"),)
)
REVIEWER_WRITES_REPORT = MockAgentAction(
    writes=((REVIEW_REPORT, "Verdict: pass — next: record the verdict\n"),)
)


def _complete_state():
    return payload(
        "complete",
        None,
        actor="none",
        frontier_present=False,
        completion_evidence={"path": "05_governance/completion.md"},
    )


def _request_prompt(scenario, attempt):
    request = scenario.store.read_request(attempt)
    prompt_path = Path(request["prompt_path"])
    if not prompt_path.is_absolute():
        prompt_path = scenario.project / prompt_path
    return request, prompt_path


class SeatConductPromptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def assert_journaled_conduct(self, scenario, attempt):
        request, prompt_path = _request_prompt(scenario, attempt)
        prompt = prompt_path.read_bytes()
        self.assertTrue(prompt_path.is_relative_to(attempt))
        self.assertEqual(prompt.count(_SEAT_CONDUCT_BLOCK), 1)
        self.assertEqual(prompt[: len(_SEAT_CONDUCT_BLOCK)], _SEAT_CONDUCT_BLOCK)
        self.assertEqual(
            request["prompt_sha256"], hashlib.sha256(prompt).hexdigest()
        )
        return prompt[: len(_SEAT_CONDUCT_BLOCK)]

    def test_every_dispatch_kind_journals_the_identical_conduct_block(self):
        ordinary = Scenario(
            self.root / "ordinary",
            states=clean_pass_states(),
            coder=[CODER_WRITES_REPORT],
            reviewer=[REVIEWER_WRITES_REPORT],
            shadow_reviewer=[
                MockAgentAction(writes=(("shadow_report.md", "shadow\n"),))
            ],
            verbs=DEFAULT_VERBS,
            policy_body=(
                "[roles.shadow_reviewer]\n"
                "enabled = true\n"
                'adapter = "mock"\n'
            ),
        )
        for _ in range(3):
            ordinary.supervisor.tick()
        ordinary_attempts = ordinary.store.list_attempts(
            "run_001", "M001-S01"
        )
        prompts = {
            "coding": self.assert_journaled_conduct(
                ordinary, ordinary_attempts[0]
            ),
            "review": self.assert_journaled_conduct(
                ordinary, ordinary_attempts[1]
            ),
            "shadow": self.assert_journaled_conduct(
                ordinary,
                ordinary.store.list_shadow_attempts(
                    "run_001", "M001-S01"
                )[0],
            ),
        }

        repair = Scenario(
            self.root / "repair",
            states=[
                payload(
                    "ready",
                    "fix_self_report",
                    diagnostics=(
                        {
                            "severity": "error",
                            "code": "report_incomplete",
                            "message": "repair the report",
                        },
                    ),
                )
            ],
            coder=[MockAgentAction(writes=((SELF_REPORT, "repaired\n"),))],
        )
        repair.supervisor.tick()
        prompts["repair"] = self.assert_journaled_conduct(
            repair,
            repair.store.list_attempts("run_001", "M001-S01")[0],
        )

        proposal = ROADMAP_BODY.replace(
            "Implement the fixture behavior.",
            "Sharpen the fixture behavior.",
        )
        reconciliation = Scenario(
            self.root / "reconciliation",
            states=[
                payload(
                    "needs_specification",
                    None,
                    actor="architect",
                    frontier_present=False,
                ),
                payload("ready", "frontier_recorded"),
            ],
            architect=[
                MockAgentAction(writes=(("roadmap_proposal.md", proposal),))
            ],
        )
        reconciliation.supervisor.tick()
        reconciliation_slice = reconciliation.store.list_slices("run_001")[0]
        prompts["reconciliation"] = self.assert_journaled_conduct(
            reconciliation,
            reconciliation.store.list_attempts(
                "run_001", reconciliation_slice
            )[0],
        )

        holistic = Scenario(
            self.root / "holistic",
            states=[_complete_state(), _complete_state()],
            reviewer=[
                MockAgentAction(
                    writes=(("holistic_review.json", json.dumps({"findings": []})),)
                )
            ],
            boundary="roadmap_complete",
            policy_body=(
                "[target]\nmax_passes = 3\nmax_slices = 10\n"
                "[roles.reviewer]\nadapter = \"mock\"\n"
                "[autonomy]\npass_boundary = \"two_clean\"\n"
            ),
        )
        holistic.supervisor.tick()
        holistic.supervisor.tick()
        prompts["holistic"] = self.assert_journaled_conduct(
            holistic,
            holistic.store.list_attempts(
                "run_001", "holistic_pass_001"
            )[0],
        )

        for kind, block in prompts.items():
            with self.subTest(kind=kind):
                self.assertEqual(block, _SEAT_CONDUCT_BLOCK)

    def test_memory_context_has_fixed_conduct_then_prompt_then_context_order(self):
        context = b"\n\n## Memory Context\n\nBounded context.\n"
        hooks = SimpleNamespace(seen=[])

        def read_context(prompt):
            hooks.seen.append(prompt)
            return SimpleNamespace(context=context, facts=())

        hooks.read_context = read_context
        scenario = Scenario(
            self.root / "memory",
            states=[payload("ready", "execute_coding_prompt")],
            coder=[CODER_WRITES_REPORT],
            memory_hooks_factory=lambda *_args: hooks,
        )
        scenario.supervisor.tick()
        attempt = scenario.store.list_attempts("run_001", "M001-S01")[0]
        request, prompt_path = _request_prompt(scenario, attempt)
        expected = _SEAT_CONDUCT_BLOCK + PROMPT_BODY.encode("utf-8") + context
        self.assertEqual(hooks.seen, [PROMPT_BODY.encode("utf-8")])
        self.assertEqual(prompt_path.read_bytes(), expected)
        self.assertEqual(
            request["prompt_sha256"], hashlib.sha256(expected).hexdigest()
        )

    def test_conduct_block_stays_within_its_byte_bound(self):
        self.assertLessEqual(
            len(_SEAT_CONDUCT_BLOCK), _MAX_SEAT_CONDUCT_BLOCK_BYTES
        )
        self.assertEqual(_SEAT_CONDUCT_BLOCK.decode("utf-8").encode("utf-8"),
                         _SEAT_CONDUCT_BLOCK)


if __name__ == "__main__":
    unittest.main()
