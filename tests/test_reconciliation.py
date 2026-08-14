"""Phase B N04 guarded reconciliation writer tests."""

import hashlib
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

from frutlups_drive.reconciliation import (
    ReconciliationRefusal,
    ReconciliationWriter,
)

from _scenario import ACTIVE_ROADMAP, ROADMAP_BODY, build_project


ACCEPTED_SECTION = """### M000: Accepted Fixture

Status: completed

Disposition: accepted and closed.

Slices:
- M000-S01: Accepted slice

Implementation package: preserve this history.

Objective:
Preserve the accepted fixture.

Expected artifacts:
- accepted evidence.

Active workspaces:
- package.

Non-goals:
- rewriting history.

Verification/evidence:
- accepted checks.

Review strictness: Level 3.

Likely coding prompt:
Use accepted history.

Done when:
- accepted evidence remains.

"""


class ReconciliationWriterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = build_project(Path(self._tmp.name))
        self.target = self.project / ACTIVE_ROADMAP
        self.before = ROADMAP_BODY.replace(
            "### M001: Fixture Milestone", ACCEPTED_SECTION + "### M001: Fixture Milestone"
        )
        self.target.write_text(self.before, encoding="utf-8", newline="")
        self.proposal = self.project / "proposal.md"
        self.writer = ReconciliationWriter(self.project)

    def write_proposal(self, text):
        self.proposal.write_text(text, encoding="utf-8", newline="")

    def test_allowed_specification_change_is_atomic_and_hash_bound(self):
        after = self.before.replace(
            "Implement the fixture behavior.", "Sharpen the fixture behavior."
        )
        self.write_proposal(after)
        result = self.writer.apply(self.proposal)
        self.assertEqual(result.target, ACTIVE_ROADMAP)
        self.assertEqual(result.slice_id, "M001-S01")
        self.assertEqual(
            result.before_sha256,
            hashlib.sha256(self.before.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            result.after_sha256, hashlib.sha256(after.encode("utf-8")).hexdigest()
        )
        self.assertEqual(self.target.read_text(encoding="utf-8"), after)

    def test_forbidden_proposals_refuse_without_partial_write(self):
        proposals = {
            "destination": self.before.replace(
                "Destination: exercise the fixture.", "Destination: seize control."
            ),
            "ruled_out": self.before.replace(
                "- unrelated mutation.", "- allow unrelated mutation."
            ),
            "status": self.before.replace("Status: active", "Status: completed"),
            "accepted_section": self.before.replace(
                "Preserve the accepted fixture.", "Rewrite the accepted fixture."
            ),
            "smuggled_heading": self.before.replace(
                "Implement the fixture behavior.",
                "Implement the fixture behavior.\n\n## Ruled Out\n\n- permit mutation.",
            ),
            "smuggled_history": self.before.replace(
                "Implement the fixture behavior.",
                "Rewrite accepted milestone M000 and PROJECT_STATE.md.",
            ),
        }
        original = self.target.read_bytes()
        for name, proposal in proposals.items():
            with self.subTest(name=name):
                self.write_proposal(proposal)
                with self.assertRaises(ReconciliationRefusal):
                    self.writer.apply(self.proposal)
                self.assertEqual(self.target.read_bytes(), original)

    def test_guard_free_application_is_causally_unsafe_but_writer_refuses(self):
        forbidden = self.before.replace(
            "Implement the fixture behavior.",
            "Rewrite accepted milestone M000.",
        )
        original = self.target.read_bytes()
        self.target.write_bytes(forbidden.encode("utf-8"))
        self.assertNotEqual(self.target.read_bytes(), original)
        self.target.write_bytes(original)
        self.write_proposal(forbidden)
        with self.assertRaises(ReconciliationRefusal):
            self.writer.apply(self.proposal)
        self.assertEqual(self.target.read_bytes(), original)

    def test_missing_or_ambiguous_active_roadmap_refuses(self):
        self.target.unlink()
        with self.assertRaisesRegex(ReconciliationRefusal, "active_roadmap_ambiguous"):
            self.writer.target_path()
        self.target.write_text(self.before, encoding="utf-8", newline="")
        (self.target.parent / "second_active_roadmap.md").write_text(
            self.before, encoding="utf-8", newline=""
        )
        with self.assertRaisesRegex(ReconciliationRefusal, "active_roadmap_ambiguous"):
            self.writer.target_path()


if __name__ == "__main__":
    unittest.main()
