"""Run-store tests: idempotency, append-only events, transitions, encoding,
attempt ownership, strict JSON, Windows identifiers, and synchronized
write-once publication."""

import json
import os
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import _bootstrap  # noqa: F401  (sys.path bootstrap, must precede package imports)

import frutlups_drive.runstore as runstore_module
from frutlups_drive.contracts import AgentRunRequest, AgentRunResult, Role
from frutlups_drive.runstore import (
    TRANSITION_STATES,
    RunStore,
    RunStoreRefusal,
)


def _fail_constant(token):
    raise AssertionError(f"non-standard JSON constant emitted: {token}")


def strict_loads(text):
    """json.loads restricted to standards-conforming JSON (no NaN/Infinity)."""
    return json.loads(text, parse_constant=_fail_constant)

MANIFEST = {
    "policy_hash": "abc123",
    "contract_version": 1,
    "boundary": "milestone_complete",
    "started_at": "2026-08-03T00:00:00Z",
}


def make_request(**overrides):
    base = dict(
        run_id="run-001",
        attempt_id="attempt_001",
        role=Role.CODER,
        prompt_path=Path("prompts/for_coding_agent/001_m001_s01_package_scaffold.md"),
        prompt_sha256="a" * 64,
        workspace=Path("project"),
        base_revision=None,
        adapter="mock",
        model="",
        effort="xhigh",
        workspace_access="workspace_write",
        expected_artifacts=(
            Path("05_governance/reviews/m001/m001_s01_self_report.md"),
        ),
        max_seconds=3600,
        max_cost_usd=None,
    )
    base.update(overrides)
    return AgentRunRequest(**base)


def make_result(**overrides):
    base = dict(
        status="completed",
        event_log_path=Path("provider_events.jsonl"),
        changed_files=(Path("08_pkg/src/frutlups_drive/contracts.py"),),
        produced_artifacts=(
            Path("05_governance/reviews/m001/m001_s01_self_report.md"),
        ),
        exit_reason="finished",
        tokens_in=100,
        tokens_out=200,
        cost_usd=None,
    )
    base.update(overrides)
    return AgentRunResult(**base)


class RunStoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = RunStore(Path(self._tmp.name) / ".frutlups_drive")

    def assert_refused(self, code, callable_, *args, **kwargs):
        with self.assertRaises(RunStoreRefusal) as caught:
            callable_(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class RunCreationTests(RunStoreTestCase):
    def test_create_run_writes_layout_and_manifest(self):
        run_dir = self.store.create_run("run-001", MANIFEST)
        self.assertTrue(run_dir.is_dir())
        manifest_path = run_dir / "manifest.toml"
        parsed = tomllib.loads(manifest_path.read_bytes().decode("utf-8"))
        self.assertEqual(parsed["policy_hash"], "abc123")
        self.assertEqual(parsed["contract_version"], 1)
        self.assertEqual(parsed["boundary"], "milestone_complete")
        self.assertEqual(parsed["started_at"], "2026-08-03T00:00:00Z")

    def test_same_input_retry_is_idempotent(self):
        first = self.store.create_run("run-001", MANIFEST)
        original = (first / "manifest.toml").read_bytes()
        second = self.store.create_run("run-001", MANIFEST)
        self.assertEqual(first, second)
        self.assertEqual((second / "manifest.toml").read_bytes(), original)

    def test_conflicting_manifest_is_refused_and_original_intact(self):
        run_dir = self.store.create_run("run-001", MANIFEST)
        original = (run_dir / "manifest.toml").read_bytes()
        conflicting = dict(MANIFEST, policy_hash="different")
        self.assert_refused(
            "manifest_conflict", self.store.create_run, "run-001", conflicting
        )
        self.assertEqual((run_dir / "manifest.toml").read_bytes(), original)

    def test_manifest_round_trips_types(self):
        manifest = {"a_string": "x", "an_int": 7, "a_float": 1.5, "a_bool": True}
        run_dir = self.store.create_run("run-t", manifest)
        parsed = tomllib.loads((run_dir / "manifest.toml").read_bytes().decode("utf-8"))
        self.assertEqual(parsed, manifest)

    def test_manifest_string_escaping_round_trips(self):
        manifest = {"text": 'quote " backslash \\ newline \n tab \t café'}
        run_dir = self.store.create_run("run-e", manifest)
        parsed = tomllib.loads((run_dir / "manifest.toml").read_bytes().decode("utf-8"))
        self.assertEqual(parsed, manifest)

    def test_manifest_refuses_unsupported_values(self):
        self.assert_refused(
            "manifest_invalid", self.store.create_run, "run-b", {"a_list": [1]}
        )
        self.assert_refused(
            "manifest_invalid",
            self.store.create_run,
            "run-inf",
            {"bad": float("inf")},
        )

    def test_identifiers_are_fenced(self):
        for bad in ("", "..", "a/b", "a\\b", ".hidden", "a b"):
            with self.subTest(run_id=repr(bad)):
                self.assert_refused(
                    "identifier_invalid", self.store.create_run, bad, MANIFEST
                )


class EventJournalTests(RunStoreTestCase):
    def test_append_writes_canonical_lf_terminated_json_lines(self):
        self.store.create_run("run-001", MANIFEST)
        self.store.append_event("run-001", {"kind": "tick", "n": 1})
        self.store.append_event("run-001", {"n": 2, "kind": "dispatch"})
        raw = (self.store.run_dir("run-001") / "events.jsonl").read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"\r", raw)
        lines = raw.decode("utf-8").splitlines()
        self.assertEqual(lines[0], '{"kind":"tick","n":1}')
        self.assertEqual(lines[1], '{"kind":"dispatch","n":2}')
        self.assertEqual(
            [json.loads(line) for line in lines],
            [{"kind": "tick", "n": 1}, {"kind": "dispatch", "n": 2}],
        )

    def test_append_preserves_unicode_without_ascii_escapes(self):
        self.store.create_run("run-001", MANIFEST)
        self.store.append_event("run-001", {"msg": "café"})
        raw = (self.store.run_dir("run-001") / "events.jsonl").read_bytes()
        self.assertIn("café".encode("utf-8"), raw)

    def test_append_to_missing_run_is_refused(self):
        self.assert_refused(
            "run_missing", self.store.append_event, "no-run", {"kind": "tick"}
        )

    def test_unserializable_event_is_refused(self):
        self.store.create_run("run-001", MANIFEST)
        self.assert_refused(
            "event_not_serializable",
            self.store.append_event,
            "run-001",
            {"bad": {1, 2}},
        )
        self.assert_refused(
            "event_not_serializable", self.store.append_event, "run-001", "tick"
        )

    def test_completed_lines_survive_a_refused_append(self):
        self.store.create_run("run-001", MANIFEST)
        self.store.append_event("run-001", {"kind": "tick"})
        before = (self.store.run_dir("run-001") / "events.jsonl").read_bytes()
        self.assert_refused(
            "event_not_serializable",
            self.store.append_event,
            "run-001",
            {"bad": object()},
        )
        after = (self.store.run_dir("run-001") / "events.jsonl").read_bytes()
        self.assertEqual(before, after)


class AttemptTests(RunStoreTestCase):
    def setUp(self):
        super().setUp()
        self.store.create_run("run-001", MANIFEST)

    def test_attempts_are_sequential_and_unique(self):
        first = self.store.create_attempt("run-001", "M001-S01")
        second = self.store.create_attempt("run-001", "M001-S01")
        self.assertEqual(first.name, "attempt_001")
        self.assertEqual(second.name, "attempt_002")
        self.assertNotEqual(first, second)

    def test_shadow_attempts_are_separate_and_reports_are_write_once(self):
        primary = self.store.create_attempt("run-001", "M001-S01")
        shadow = self.store.create_shadow_attempt("run-001", "M001-S01")
        self.assertEqual(primary.name, "attempt_001")
        self.assertEqual(shadow.name, "attempt_001")
        self.assertEqual(self.store.list_attempts("run-001", "M001-S01"), (primary,))
        self.assertEqual(
            self.store.list_shadow_attempts("run-001", "M001-S01"), (shadow,)
        )
        report = self.store.publish_shadow_report(shadow, b"evidence\n")
        self.assertEqual(report.read_bytes(), b"evidence\n")
        self.store.publish_shadow_report(shadow, b"evidence\n")
        self.assert_refused(
            "shadow_report_conflict",
            self.store.publish_shadow_report,
            shadow,
            b"different\n",
        )
        self.assert_refused(
            "shadow_attempt_unowned",
            self.store.publish_shadow_report,
            primary,
            b"not-shadow\n",
        )

    def test_existing_attempt_directories_are_never_reused(self):
        slice_dir = (
            self.store.run_dir("run-001") / "slices" / "M001-S01"
        )
        stray = slice_dir / "attempt_007"
        stray.mkdir(parents=True)
        marker = stray / "keep.txt"
        marker.write_bytes(b"do not touch\n")
        created = self.store.create_attempt("run-001", "M001-S01")
        self.assertEqual(created.name, "attempt_008")
        self.assertEqual(marker.read_bytes(), b"do not touch\n")

    def test_attempt_for_missing_run_is_refused(self):
        self.assert_refused(
            "run_missing", self.store.create_attempt, "no-run", "M001-S01"
        )

    def test_slice_identifier_is_fenced(self):
        self.assert_refused(
            "identifier_invalid", self.store.create_attempt, "run-001", "../escape"
        )


class TransitionTests(RunStoreTestCase):
    def setUp(self):
        super().setUp()
        self.store.create_run("run-001", MANIFEST)
        self.attempt = self.store.create_attempt("run-001", "M001-S01")

    def test_lifecycle_order_is_pinned(self):
        self.assertEqual(
            TRANSITION_STATES,
            (
                "planned",
                "started",
                "externally_completed",
                "collected",
                "validated",
                "closed",
            ),
        )

    def test_new_attempt_starts_planned(self):
        self.assertEqual(self.store.read_transition(self.attempt), "planned")

    def test_forward_advance_and_idempotent_retry(self):
        self.store.advance_transition(self.attempt, "started")
        self.assertEqual(self.store.read_transition(self.attempt), "started")
        self.store.advance_transition(self.attempt, "started")
        self.assertEqual(self.store.read_transition(self.attempt), "started")

    def test_externally_completed_recovery_path(self):
        for state in ("started", "externally_completed", "collected"):
            self.store.advance_transition(self.attempt, state)
        self.assertEqual(self.store.read_transition(self.attempt), "collected")

    def test_forward_skip_is_allowed(self):
        self.store.advance_transition(self.attempt, "collected")
        self.assertEqual(self.store.read_transition(self.attempt), "collected")

    def test_regression_is_refused(self):
        self.store.advance_transition(self.attempt, "collected")
        self.assert_refused(
            "transition_regression",
            self.store.advance_transition,
            self.attempt,
            "started",
        )
        self.assertEqual(self.store.read_transition(self.attempt), "collected")

    def test_unknown_state_is_refused(self):
        self.assert_refused(
            "transition_unknown",
            self.store.advance_transition,
            self.attempt,
            "exploded",
        )

    def test_tampered_marker_is_refused_on_read(self):
        (self.attempt / "transition").write_bytes(b"garbage\n")
        self.assert_refused(
            "transition_unknown", self.store.read_transition, self.attempt
        )

    def test_missing_marker_is_refused_on_read(self):
        (self.attempt / "transition").unlink()
        self.assert_refused(
            "transition_missing", self.store.read_transition, self.attempt
        )


class RequestResultTests(RunStoreTestCase):
    def setUp(self):
        super().setUp()
        self.store.create_run("run-001", MANIFEST)
        self.attempt = self.store.create_attempt("run-001", "M001-S01")

    def test_request_serializes_canonically(self):
        path = self.store.write_request(self.attempt, make_request())
        payload = json.loads(path.read_bytes().decode("utf-8"))
        self.assertEqual(
            payload,
            {
                "run_id": "run-001",
                "attempt_id": "attempt_001",
                "role": "coder",
                "prompt_path": (
                    "prompts/for_coding_agent/001_m001_s01_package_scaffold.md"
                ),
                "prompt_sha256": "a" * 64,
                "workspace": "project",
                "base_revision": None,
                "adapter": "mock",
                "model": "",
                "effort": "xhigh",
                "workspace_access": "workspace_write",
                "expected_artifacts": [
                    "05_governance/reviews/m001/m001_s01_self_report.md"
                ],
                "max_seconds": 3600,
                "max_cost_usd": None,
            },
        )

    def test_result_serializes_canonically(self):
        path = self.store.write_result(self.attempt, make_result())
        payload = json.loads(path.read_bytes().decode("utf-8"))
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["event_log_path"], "provider_events.jsonl")
        self.assertEqual(payload["tokens_in"], 100)
        self.assertEqual(payload["cost_usd"], None)

    def test_same_input_rewrite_is_idempotent(self):
        first = self.store.write_request(self.attempt, make_request())
        original = first.read_bytes()
        second = self.store.write_request(self.attempt, make_request())
        self.assertEqual(first, second)
        self.assertEqual(second.read_bytes(), original)

    def test_conflicting_request_is_refused_and_original_intact(self):
        path = self.store.write_request(self.attempt, make_request())
        original = path.read_bytes()
        self.assert_refused(
            "request_conflict",
            self.store.write_request,
            self.attempt,
            make_request(model="other-model"),
        )
        self.assertEqual(path.read_bytes(), original)

    def test_conflicting_result_is_refused_and_original_intact(self):
        path = self.store.write_result(self.attempt, make_result())
        original = path.read_bytes()
        self.assert_refused(
            "result_conflict",
            self.store.write_result,
            self.attempt,
            make_result(status="failed"),
        )
        self.assertEqual(path.read_bytes(), original)


class AttemptOwnershipTests(RunStoreTestCase):
    """F3 regression: request/result/transition operations act only on
    existing canonical attempts of this run store."""

    def setUp(self):
        super().setUp()
        self.store.create_run("run-001", MANIFEST)
        self.attempt = self.store.create_attempt("run-001", "M001-S01")
        self._outside_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._outside_tmp.cleanup)
        self.outside = Path(self._outside_tmp.name)

    def assert_unowned(self, operation, *args):
        with self.assertRaises(RunStoreRefusal) as caught:
            operation(*args)
        self.assertEqual(caught.exception.code, "attempt_unowned")

    def test_outside_directory_write_request_refused_and_untouched(self):
        self.assert_unowned(self.store.write_request, self.outside, make_request())
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_outside_directory_write_result_refused_and_untouched(self):
        self.assert_unowned(self.store.write_result, self.outside, make_result())
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_outside_transition_advance_refused_and_file_preserved(self):
        marker = self.outside / "transition"
        marker.write_bytes(b"planned\n")
        self.assert_unowned(self.store.advance_transition, self.outside, "closed")
        self.assertEqual(marker.read_bytes(), b"planned\n")

    def test_outside_read_transition_refused(self):
        (self.outside / "transition").write_bytes(b"planned\n")
        self.assert_unowned(self.store.read_transition, self.outside)

    def test_sibling_store_attempt_refused_and_untouched(self):
        sibling = RunStore(self.outside / ".frutlups_drive")
        sibling.create_run("run-001", MANIFEST)
        other_attempt = sibling.create_attempt("run-001", "M001-S01")
        before = sorted(entry.name for entry in other_attempt.iterdir())
        self.assert_unowned(
            self.store.write_request, other_attempt, make_request()
        )
        self.assertEqual(
            sorted(entry.name for entry in other_attempt.iterdir()), before
        )

    def test_traversal_aliases_are_refused(self):
        aliases = [
            # resolves back inside the store, but dot-segments never gain
            # authority
            self.attempt / ".." / self.attempt.name,
            self.store.root / "runs" / "run-001" / ".." / ".." / ".."
            / "elsewhere" / "attempt_001",
            Path("..") / "attempt_001",
        ]
        for index, alias in enumerate(aliases):
            with self.subTest(alias=index):
                self.assert_unowned(self.store.read_transition, alias)

    def test_malformed_attempt_names_are_refused(self):
        slice_dir = self.store.run_dir("run-001") / "slices" / "M001-S01"
        for name in ("attempt_7", "attempt_abc", "attempt_001x", "notes"):
            bogus = slice_dir / name
            bogus.mkdir()
            with self.subTest(name=name):
                self.assert_unowned(self.store.read_transition, bogus)

    def test_nonexistent_canonical_attempt_is_refused(self):
        ghost = (
            self.store.run_dir("run-001") / "slices" / "M001-S01" / "attempt_099"
        )
        self.assert_unowned(self.store.read_transition, ghost)

    def test_wrong_depth_store_paths_are_refused(self):
        for path in (
            self.store.root,
            self.store.run_dir("run-001"),
            self.store.run_dir("run-001") / "slices" / "M001-S01",
        ):
            with self.subTest(path=path.name):
                self.assert_unowned(self.store.read_transition, path)

    @unittest.skipUnless(os.sep == "\\", "alternate-separator spelling exists only where os.sep is a backslash")
    def test_alternate_separator_spelling_has_no_authority(self):
        # Round 3: an alternate raw spelling of the owned attempt resolves to
        # the same directory but is not the store-minted spelling.
        alt = str(self.attempt).replace(os.sep, "/")
        self.assert_unowned(self.store.read_transition, alt)

    def test_owned_attempt_operations_still_succeed(self):
        self.store.write_request(self.attempt, make_request())
        self.store.advance_transition(self.attempt, "started")
        self.store.write_result(self.attempt, make_result())
        self.assertEqual(self.store.read_transition(self.attempt), "started")


class ExactAttemptSpellingTests(RunStoreTestCase):
    """Round 3 (R2-F1) causal regressions: only the exact canonical spelling
    minted by this store has authority. Alias inputs are assembled as raw
    strings by concatenation, never passed through Path or another
    normalizer, so the observable spelling reaches the boundary intact."""

    def setUp(self):
        super().setUp()
        self.store.create_run("run-001", MANIFEST)
        self.attempt = self.store.create_attempt("run-001", "M001-S01")

    def raw_dot_spelling(self):
        return os.sep.join(
            [
                str(self.store.root),
                "runs",
                "run-001",
                "slices",
                "M001-S01",
                ".",
                "attempt_001",
            ]
        )

    def raw_dotdot_spelling(self):
        return os.sep.join(
            [
                str(self.store.root),
                "runs",
                "run-001",
                "slices",
                "M001-S01",
                "attempt_001",
                "..",
                "attempt_001",
            ]
        )

    def assert_unowned(self, operation, *args):
        with self.assertRaises(RunStoreRefusal) as caught:
            operation(*args)
        self.assertEqual(caught.exception.code, "attempt_unowned")

    def test_raw_dot_spelling_refused_for_write_request(self):
        self.assert_unowned(
            self.store.write_request, self.raw_dot_spelling(), make_request()
        )
        self.assertFalse((self.attempt / "request.json").exists())

    def test_raw_dot_spelling_refused_for_write_result(self):
        self.assert_unowned(
            self.store.write_result, self.raw_dot_spelling(), make_result()
        )
        self.assertFalse((self.attempt / "result.json").exists())

    def test_raw_dot_spelling_refused_for_read_transition(self):
        self.assert_unowned(self.store.read_transition, self.raw_dot_spelling())

    def test_raw_dot_spelling_refused_for_advance_transition(self):
        self.assert_unowned(
            self.store.advance_transition, self.raw_dot_spelling(), "closed"
        )
        self.assertEqual(
            (self.attempt / "transition").read_bytes(), b"planned\n"
        )

    def test_raw_dotdot_spelling_refused(self):
        self.assert_unowned(
            self.store.read_transition, self.raw_dotdot_spelling()
        )

    @unittest.skipUnless(os.name == "nt", "drive-letter alias applies to Windows paths")
    def test_drive_case_alias_spelling_has_no_authority(self):
        minted = str(self.attempt)
        self.assertEqual(minted[1], ":", "expected a drive-qualified temp path")
        swapped = minted[0].swapcase() + minted[1:]
        self.assertNotEqual(swapped, minted)
        self.assert_unowned(self.store.read_transition, swapped)

    def test_link_alias_spelling_has_no_authority_when_links_permitted(self):
        alias = (
            self.store.run_dir("run-001") / "slices" / "M001-S01" / "attempt_777"
        )
        try:
            os.symlink(str(self.attempt), str(alias), target_is_directory=True)
        except OSError as error:
            code = getattr(error, "winerror", None) or error.errno
            self.skipTest(
                f"host refuses link creation without elevation (error {code})"
            )
        raw_alias = os.sep.join(
            [
                str(self.store.root),
                "runs",
                "run-001",
                "slices",
                "M001-S01",
                "attempt_777",
            ]
        )
        self.assert_unowned(self.store.read_transition, raw_alias)
        self.assert_unowned(self.store.write_request, raw_alias, make_request())
        self.assertFalse((self.attempt / "request.json").exists())

    def test_non_string_path_representations_are_refused(self):
        for bad in (b"attempt-bytes", 123, None):
            with self.subTest(value=repr(bad)):
                self.assert_unowned(self.store.read_transition, bad)

    def test_minted_spelling_remains_accepted(self):
        self.assertEqual(self.store.read_transition(self.attempt), "planned")
        self.store.write_request(self.attempt, make_request())
        self.store.advance_transition(self.attempt, "started")
        self.assertEqual(self.store.read_transition(self.attempt), "started")


class RelativeRootStoreTests(unittest.TestCase):
    """Round 3 guard: store-minted spellings from a relative root stay usable,
    which also proves each operation validates once — with a relative root the
    resolved absolute spelling differs from the minted one, so any internal
    re-entry of the public boundary with a transformed path would refuse."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        original_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.addCleanup(os.chdir, original_cwd)
        self.store = RunStore(Path(".frutlups_drive"))
        self.store.create_run("run-001", MANIFEST)
        self.attempt = self.store.create_attempt("run-001", "M001-S01")

    def test_minted_relative_spelling_is_not_absolute(self):
        self.assertFalse(self.attempt.is_absolute())

    def test_all_operations_accept_minted_relative_spelling(self):
        self.store.write_request(self.attempt, make_request())
        self.assertEqual(self.store.read_transition(self.attempt), "planned")
        self.store.advance_transition(self.attempt, "started")
        self.store.write_result(self.attempt, make_result())
        self.assertEqual(self.store.read_transition(self.attempt), "started")

    def test_advance_succeeds_without_transformed_path_reentry(self):
        self.store.advance_transition(self.attempt, "collected")
        self.assertEqual(
            (self.attempt / "transition").read_bytes(), b"collected\n"
        )

    def test_resolved_absolute_spelling_of_relative_attempt_is_refused(self):
        absolute = os.path.abspath(str(self.attempt))
        with self.assertRaises(RunStoreRefusal) as caught:
            self.store.read_transition(absolute)
        self.assertEqual(caught.exception.code, "attempt_unowned")


class StrictJsonTests(RunStoreTestCase):
    """F4 regression: non-finite floats never reach a journal or record."""

    def setUp(self):
        super().setUp()
        self.store.create_run("run-001", MANIFEST)
        self.attempt = self.store.create_attempt("run-001", "M001-S01")

    def test_nonfinite_events_refused_and_journal_preserved(self):
        self.store.append_event("run-001", {"kind": "tick", "cost": 1.5})
        journal = self.store.run_dir("run-001") / "events.jsonl"
        before = journal.read_bytes()
        for bad in (float("nan"), float("inf"), float("-inf")):
            for event in ({"cost": bad}, {"nested": {"values": [bad]}}):
                with self.subTest(value=repr(bad), shape=next(iter(event))):
                    self.assert_refused(
                        "event_not_serializable",
                        self.store.append_event,
                        "run-001",
                        event,
                    )
                    self.assertEqual(journal.read_bytes(), before)
        for line in before.decode("utf-8").splitlines():
            strict_loads(line)

    def test_nonfinite_request_refused_before_publication(self):
        self.assert_refused(
            "request_not_serializable",
            self.store.write_request,
            self.attempt,
            make_request(max_cost_usd=float("nan")),
        )
        self.assertFalse((self.attempt / "request.json").exists())

    def test_nonfinite_result_refused_and_prior_record_preserved(self):
        path = self.store.write_result(self.attempt, make_result())
        before = path.read_bytes()
        self.assert_refused(
            "result_not_serializable",
            self.store.write_result,
            self.attempt,
            make_result(cost_usd=float("-inf")),
        )
        self.assertEqual(path.read_bytes(), before)
        strict_loads(before.decode("utf-8"))

    def test_finite_costs_still_serialize_strictly(self):
        path = self.store.write_request(
            self.attempt, make_request(max_cost_usd=2.5)
        )
        payload = strict_loads(path.read_bytes().decode("utf-8"))
        self.assertEqual(payload["max_cost_usd"], 2.5)


class WindowsIdentifierTests(RunStoreTestCase):
    """F5 regression: identifiers are distinct usable Windows path components."""

    def test_reserved_and_alias_run_identifiers_are_refused(self):
        for bad in (
            "run.",
            "run..",
            "trailing.dot.",
            "CON",
            "con",
            "NUL",
            "nul.toml",
            "PRN",
            "AUX",
            "AUX.tar.gz",
            "com1",
            "COM9.log",
            "lpt3",
            "LPT1.txt",
        ):
            with self.subTest(identifier=repr(bad)):
                self.assert_refused(
                    "identifier_invalid", self.store.create_run, bad, MANIFEST
                )

    def test_reserved_slice_identifiers_are_refused(self):
        self.store.create_run("run-001", MANIFEST)
        for bad in ("con", "NUL.json", "slice."):
            with self.subTest(identifier=repr(bad)):
                self.assert_refused(
                    "identifier_invalid",
                    self.store.create_attempt,
                    "run-001",
                    bad,
                )

    def test_windows_safe_identifiers_remain_accepted(self):
        for good in (
            "run-001",
            "M001-S01",
            "common",
            "console",
            "nullable",
            "com10",
            "aux2",
            "CONTRACT",
            "r.2",
        ):
            with self.subTest(identifier=good):
                self.store.create_run(good, MANIFEST)


class ConcurrentPublicationTests(RunStoreTestCase):
    """F5 regression: synchronized two-party write-once publication.

    A barrier inside a patched `_read_if_exists` guarantees both parties pass
    the absent-destination check before either publishes, so the production
    collision branch is reached causally, not probabilistically.
    """

    def _race(self, gate_filename, call_a, call_b):
        barrier = threading.Barrier(2)
        original = runstore_module._read_if_exists

        def gated(path):
            value = original(path)
            if path.name == gate_filename and value is None:
                barrier.wait(timeout=30)
            return value

        outcomes = [None, None]

        def runner(index, call):
            try:
                call()
                outcomes[index] = "ok"
            except RunStoreRefusal as refusal:
                outcomes[index] = refusal.code
            except BaseException as unexpected:  # raw platform errors must not escape
                outcomes[index] = f"raw:{type(unexpected).__name__}"

        with mock.patch.object(runstore_module, "_read_if_exists", gated):
            threads = [
                threading.Thread(target=runner, args=(0, call_a)),
                threading.Thread(target=runner, args=(1, call_b)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        return outcomes

    def assert_no_temp_residue(self):
        self.assertEqual(list(self.store.root.rglob("*.tmp")), [])

    def test_same_content_publication_converges_idempotently(self):
        outcomes = self._race(
            "manifest.toml",
            lambda: self.store.create_run("run-001", MANIFEST),
            lambda: self.store.create_run("run-001", MANIFEST),
        )
        self.assertEqual(outcomes, ["ok", "ok"])
        raw = (self.store.run_dir("run-001") / "manifest.toml").read_bytes()
        self.assertEqual(
            tomllib.loads(raw.decode("utf-8"))["policy_hash"], "abc123"
        )
        self.assert_no_temp_residue()

    def test_conflicting_publication_keeps_one_winner_and_owned_loser(self):
        variant = dict(MANIFEST, policy_hash="different")
        outcomes = self._race(
            "manifest.toml",
            lambda: self.store.create_run("run-001", MANIFEST),
            lambda: self.store.create_run("run-001", variant),
        )
        self.assertEqual(sorted(outcomes), ["manifest_conflict", "ok"])
        raw = (self.store.run_dir("run-001") / "manifest.toml").read_bytes()
        parsed = tomllib.loads(raw.decode("utf-8"))
        self.assertIn(parsed["policy_hash"], {"abc123", "different"})
        self.assertEqual(sorted(parsed), sorted(MANIFEST))
        self.assert_no_temp_residue()

    def test_conflicting_request_race_uses_request_conflict(self):
        self.store.create_run("run-001", MANIFEST)
        attempt = self.store.create_attempt("run-001", "M001-S01")
        outcomes = self._race(
            "request.json",
            lambda: self.store.write_request(attempt, make_request()),
            lambda: self.store.write_request(
                attempt, make_request(model="other-model")
            ),
        )
        self.assertEqual(sorted(outcomes), ["ok", "request_conflict"])
        payload = json.loads(
            (attempt / "request.json").read_bytes().decode("utf-8")
        )
        self.assertIn(payload["model"], {"", "other-model"})
        self.assert_no_temp_residue()


class EncodingAndAtomicityTests(RunStoreTestCase):
    def test_all_store_text_is_utf8_lf_and_no_temp_residue(self):
        self.store.create_run("run-001", dict(MANIFEST, note="café"))
        self.store.append_event("run-001", {"kind": "tick", "msg": "café"})
        attempt = self.store.create_attempt("run-001", "M001-S01")
        self.store.write_request(attempt, make_request())
        self.store.advance_transition(attempt, "started")
        self.store.write_result(attempt, make_result())
        self.store.advance_transition(attempt, "collected")
        files = [p for p in self.store.root.rglob("*") if p.is_file()]
        self.assertGreaterEqual(len(files), 5)
        for file_path in files:
            with self.subTest(file=file_path.name):
                raw = file_path.read_bytes()
                self.assertNotIn(b"\r", raw)
                self.assertTrue(raw.endswith(b"\n"))
                raw.decode("utf-8")
                self.assertFalse(file_path.name.endswith(".tmp"))


if __name__ == "__main__":
    unittest.main()
