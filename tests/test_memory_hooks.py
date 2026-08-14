"""Declaration, llloom-hook, refusal, and non-destruction lanes."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401

from frutlups_drive.memory_hooks import (
    LlloomBinding,
    LlloomMemoryHooks,
    MemoryHookRefusal,
    load_llloom_binding,
    reconcile_memory_mode,
)
from frutlups_drive.cli import _build_memory_hooks, main
from frutlups_drive.contracts import ExitCode
from frutlups_drive.planstate import MemoryMode
from frutlups_drive.policy import SCHEMA_VERSION, load_execution_policy
from frutlups_drive.runstore import RunStore, RunStoreRefusal
from frutlups_drive.verifier import ProcessOutcome, SubprocessRunner

from _scenario import FakeClock


FAKE_LLLOOM_IDENTITY = "llloom-7.8.9"
FAKE_LLLOOM_VERSION = FAKE_LLLOOM_IDENTITY.removeprefix("llloom-")

HEALTHY = {
    "schema": "llloom.liveness.v1",
    "llloom_version": FAKE_LLLOOM_VERSION,
    "status": "healthy",
    "reason": "healthy",
    "root_present": True,
    "workspace_valid": True,
    "usable": True,
    "lock_held": False,
}

REAL_LLLOOM_EXECUTABLE_ENV = "FRUTLUPS_DRIVE_TEST_LLLOOM_EXECUTABLE"


def real_llloom_executable(test_case):
    raw = os.environ.get(REAL_LLLOOM_EXECUTABLE_ENV, "")
    if not raw:
        test_case.skipTest(
            f"{REAL_LLLOOM_EXECUTABLE_ENV} is not bound for this lane"
        )
    executable = Path(raw)
    if not executable.is_absolute() or not executable.is_file():
        test_case.fail(
            f"{REAL_LLLOOM_EXECUTABLE_ENV} must name an absolute file"
        )
    return executable


def tree_identity(root):
    if not root.exists():
        return ("absent",)
    rows = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            rows.append(("directory", relative))
        elif path.is_file():
            rows.append(
                (
                    "file",
                    relative,
                    len(path.read_bytes()),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
        else:
            rows.append(("other", relative))
    return tuple(rows)


class LlloomRunner:
    def __init__(
        self,
        *,
        liveness=None,
        liveness_exit=0,
        query=None,
        submission=None,
        submission_exit=None,
    ):
        self.liveness = HEALTHY if liveness is None else liveness
        self.liveness_exit = liveness_exit
        self.query = query or {
            "question": "bounded",
            "answer": (
                "No authoritative claims, index_only spans, or structure "
                "items matched the question."
            ),
            "citations": [],
            "used_claim_ids": [],
            "used_verbatim_spans": [],
            "used_structure_items": [],
            "ids_only": False,
        }
        self.submission = submission
        self.submission_exit = submission_exit
        self.submitted = set()
        self.calls = []

    def run(
        self,
        argv,
        cwd,
        env,
        timeout_seconds,
        stdout_path,
        stderr_path,
        max_stream_bytes=1_048_576,
    ):
        self.calls.append((tuple(argv), Path(cwd), dict(env), max_stream_bytes))
        if "liveness" in argv:
            payload, exit_code = self.liveness, self.liveness_exit
        elif "query" in argv:
            payload, exit_code = self.query, 0
        elif "submit-update" in argv:
            document = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
            identity = (
                document["submitter_id"]
                + "\0"
                + document["client_proposal_id"]
            ).encode("utf-8")
            proposal_id = "up." + hashlib.sha256(identity).hexdigest()
            if self.submission is not None:
                payload = dict(self.submission)
                if payload.get("proposal_id") == "$computed":
                    payload["proposal_id"] = proposal_id
                exit_code = self.submission_exit
            elif proposal_id in self.submitted:
                payload = submission_result(7)
                payload["proposal_id"] = proposal_id
                exit_code = 7
            else:
                self.submitted.add(proposal_id)
                payload = submission_result(0)
                payload["proposal_id"] = proposal_id
                exit_code = 0
        else:
            raise AssertionError("unexpected llloom surface")
        raw = json.dumps(payload, sort_keys=True).encode("utf-8")
        Path(stdout_path).write_bytes(raw[:max_stream_bytes])
        Path(stderr_path).write_bytes(b"")
        overflow = len(raw) > max_stream_bytes
        return ProcessOutcome(
            1.0,
            1.0,
            exit_code,
            False,
            stdout_overflow=overflow,
            stderr_overflow=False,
        )


def submission_result(exit_code, *, version=FAKE_LLLOOM_VERSION):
    cases = {
        0: ("accepted", "accepted", "$computed", "healthy", "healthy"),
        3: ("refused", "root_absent", None, "absent", "root_absent"),
        4: (
            "refused",
            "inbox_unavailable",
            None,
            "healthy",
            "healthy",
        ),
        5: (
            "refused",
            "malformed_document",
            None,
            "healthy",
            "healthy",
        ),
        6: (
            "refused",
            "document_oversized",
            None,
            "healthy",
            "healthy",
        ),
        7: (
            "refused",
            "duplicate_proposal",
            "$computed",
            "healthy",
            "healthy",
        ),
        8: (
            "refused",
            "unauthorized_action",
            None,
            "healthy",
            "healthy",
        ),
        9: (
            "refused",
            "publication_failed",
            "$computed",
            "healthy",
            "healthy",
        ),
    }
    status, reason, proposal_id, liveness_status, liveness_reason = cases[
        exit_code
    ]
    return {
        "schema": "llloom.update_submission_result.v1",
        "llloom_version": version,
        "status": status,
        "reason": reason,
        "proposal_id": proposal_id,
        "liveness_status": liveness_status,
        "liveness_reason": liveness_reason,
    }


class RaisingRunner:
    def run(self, *args, **kwargs):
        raise OSError("spawn detail must remain private")


class SubmissionTransportRunner(LlloomRunner):
    def __init__(self, kind):
        super().__init__()
        self.kind = kind

    def run(self, argv, *args, **kwargs):
        if "submit-update" not in argv:
            return super().run(argv, *args, **kwargs)
        self.calls.append(
            (
                tuple(argv),
                Path(args[0]),
                dict(args[1]),
                kwargs.get("max_stream_bytes", 1_048_576),
            )
        )
        if self.kind == "spawn":
            raise OSError("submission spawn detail must remain private")
        Path(args[3]).write_bytes(b"")
        Path(args[4]).write_bytes(b"")
        return ProcessOutcome(1.0, 2.0, None, True)


class RecordingSubprocessRunner:
    def __init__(self, clock):
        self._delegate = SubprocessRunner(clock)
        self.calls = []
        self.outcomes = []

    def run(self, *args, **kwargs):
        self.calls.append(tuple(args[0]))
        outcome = self._delegate.run(*args, **kwargs)
        self.outcomes.append(outcome)
        return outcome


class MemoryHookTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "project"
        self.root = self.project / "memory" / "llloom"
        self.root.mkdir(parents=True)
        (self.root / "fixture.txt").write_bytes(b"test-only minimal root\n")
        self.store = RunStore(self.project / ".frutlups_drive")
        self.store.create_run("run_001", {"memory_mode": "llloom"})
        self.mode = MemoryMode("llloom", "memory/llloom")

    def hooks(self, runner, *, binding=True):
        return LlloomMemoryHooks(
            project_root=self.project,
            memory_mode=self.mode,
            binding=(
                LlloomBinding(
                    argv_prefix=(sys.executable,),
                    env=(("PYTHONDONTWRITEBYTECODE", "1"),),
                    tool_identity=FAKE_LLLOOM_IDENTITY,
                    tool_version=FAKE_LLLOOM_VERSION,
                    binding_sha256="a" * 64,
                    executable_sha256="b" * 64,
                )
                if binding
                else None
            ),
            binding_refusal=None if binding else "llloom_binding_missing",
            store=self.store,
            run_id="run_001",
            runner=runner,
            timeout_seconds=5,
        )

    def fingerprint(self):
        return tuple(
            (
                path.relative_to(self.root).as_posix(),
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        )


class BindingAndReconciliationTests(MemoryHookTestCase):
    def policy(self, memory_body=""):
        path = self.project / "frutlups_drive.toml"
        path.write_text(
            f'schema_version = "{SCHEMA_VERSION}"\n{memory_body}',
            encoding="utf-8",
        )
        return load_execution_policy(path).policy

    def write_binding(self, tool_identity=FAKE_LLLOOM_IDENTITY):
        path = self.project / "local_state" / "llloom_binding.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'schema_version = "frutlups_drive_llloom_binding_v1"\n'
            "[launch]\n"
            f'argv_prefix = ["{Path(sys.executable).as_posix()}", "-m", "llloom"]\n'
            f"tool_identity = {json.dumps(tool_identity)}\n"
            "[env]\n",
            encoding="utf-8",
        )
        return path

    def test_exact_binding_loads_with_bytecode_hygiene(self):
        binding = load_llloom_binding(self.write_binding())
        self.assertEqual(binding.argv_prefix[-2:], ("-m", "llloom"))
        self.assertEqual(binding.tool_identity, FAKE_LLLOOM_IDENTITY)
        self.assertEqual(binding.tool_version, FAKE_LLLOOM_VERSION)
        self.assertEqual(
            dict(binding.env)["PYTHONDONTWRITEBYTECODE"], "1"
        )
        self.assertEqual(len(binding.binding_sha256), 64)
        self.assertEqual(
            binding.manifest_facts(),
            {
                "llloom_binding_sha256": binding.binding_sha256,
                "llloom_executable_sha256": binding.executable_sha256,
                "llloom_tool_identity": FAKE_LLLOOM_IDENTITY,
            },
        )

    def test_binding_identity_grammar_is_bounded_and_shape_only(self):
        valid = (
            ("llloom-0.1", "0.1"),
            ("llloom-0.1.2", "0.1.2"),
            ("llloom-12.345.6789.4", "12.345.6789.4"),
        )
        for identity, version in valid:
            with self.subTest(valid=identity):
                binding = load_llloom_binding(self.write_binding(identity))
                self.assertEqual(binding.tool_identity, identity)
                self.assertEqual(binding.tool_version, version)

        invalid = (
            "llloom-0",
            "llloom-01.2",
            "llloom-1.02",
            "llloom-1.2.3.4.5",
            "llloom-1.2rc1",
            "frutlups-1.2",
            "llloom-100000.1",
        )
        for identity in invalid:
            with self.subTest(invalid=identity), self.assertRaises(
                MemoryHookRefusal
            ) as caught:
                load_llloom_binding(self.write_binding(identity))
            self.assertEqual(caught.exception.code, "llloom_tool_identity_invalid")

    def test_missing_and_malformed_bindings_keep_bounded_refusals(self):
        path = self.project / "local_state" / "llloom_binding.toml"
        with self.assertRaises(MemoryHookRefusal) as missing:
            load_llloom_binding(path)
        self.assertEqual(missing.exception.code, "llloom_binding_missing")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not = [valid")
        with self.assertRaises(MemoryHookRefusal) as malformed:
            load_llloom_binding(path)
        self.assertEqual(malformed.exception.code, "llloom_binding_malformed")

    def test_policy_mismatch_refuses_before_hooks(self):
        with self.assertRaises(MemoryHookRefusal) as caught:
            reconcile_memory_mode(
                self.mode,
                self.policy("[memory]\nfollow_project_state = false\n"),
            )
        self.assertEqual(caught.exception.code, "memory_mode_policy_mismatch")

    def test_none_and_lightweight_structurally_skip_binding_lookup(self):
        policy = self.policy()
        for mode in (MemoryMode.none(), MemoryMode("lightweight", None)):
            with self.subTest(mode=mode.mode), patch(
                "frutlups_drive.cli.load_llloom_binding"
            ) as load_binding:
                hooks = _build_memory_hooks(
                    self.project, policy, mode, self.store, "run_001"
                )
                self.assertIsNone(hooks)
                load_binding.assert_not_called()


class ManifestBindingIdentityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        fixture = Path(__file__).parent / "fixtures" / "projects" / "minimal_v3"
        self.project = Path(self._tmp.name) / "project"
        shutil.copytree(fixture, self.project)
        self.binding_path = (
            self.project / "local_state" / "llloom_binding.toml"
        )
        self.binding_path.parent.mkdir(parents=True, exist_ok=True)
        self.write_binding(FAKE_LLLOOM_IDENTITY)

    def write_binding(self, identity):
        self.binding_path.write_text(
            'schema_version = "frutlups_drive_llloom_binding_v1"\n'
            "[launch]\n"
            f'argv_prefix = ["{Path(sys.executable).as_posix()}"]\n'
            f"tool_identity = {json.dumps(identity)}\n"
            "[env]\n",
            encoding="utf-8",
        )

    def test_manifest_records_identity_and_resume_refuses_changed_binding(self):
        admitted = (MemoryMode("llloom", "memory/llloom"), None)
        with patch("frutlups_drive.cli._admit_memory_mode", return_value=admitted):
            code = main(
                ["run", str(self.project), "--until", "slice_complete"]
            )
        self.assertEqual(code, int(ExitCode.OK))
        manifest = tomllib.loads(
            (
                self.project
                / ".frutlups_drive/runs/run_001/manifest.toml"
            ).read_text(encoding="utf-8")
        )
        binding = load_llloom_binding(self.binding_path)
        self.assertEqual(
            {
                key: manifest[key] for key in binding.manifest_facts()
            },
            binding.manifest_facts(),
        )
        self.assertNotIn(str(Path(sys.executable)), manifest.values())

        self.write_binding("llloom-7.8.10")
        with patch("frutlups_drive.cli._admit_memory_mode", return_value=admitted):
            code = main(["resume", str(self.project), "run_001"])
        self.assertEqual(code, int(ExitCode.REFUSED))


class LivenessAndContextTests(MemoryHookTestCase):
    def test_healthy_liveness_is_exact_and_root_is_byte_identical(self):
        runner = LlloomRunner()
        before = self.fingerprint()
        fact = self.hooks(runner).preflight()[0]
        self.assertEqual((fact.status, fact.reason), ("healthy", "healthy"))
        self.assertEqual(self.fingerprint(), before)
        argv, cwd, env, bound = runner.calls[0]
        self.assertEqual(argv[-3:-1], ("--root", str(self.root)))
        self.assertEqual(argv[-1], "liveness")
        self.assertEqual(cwd, self.project)
        self.assertEqual(env, {"PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(bound, 16_384)

    def test_absent_unhealthy_malformed_spawn_and_missing_binding_refuse(self):
        cases = (
            (
                LlloomRunner(
                    liveness={
                        **HEALTHY,
                        "status": "absent",
                        "reason": "root_absent",
                        "root_present": False,
                        "workspace_valid": False,
                        "usable": False,
                        "lock_held": None,
                    },
                    liveness_exit=3,
                ),
                True,
                "root_absent",
            ),
            (
                LlloomRunner(
                    liveness={
                        **HEALTHY,
                        "status": "unhealthy",
                        "reason": "lock_held",
                        "usable": False,
                        "lock_held": True,
                    },
                    liveness_exit=4,
                ),
                True,
                "lock_held",
            ),
            (
                LlloomRunner(
                    liveness={
                        **HEALTHY,
                        "llloom_version": "7.8.10",
                    }
                ),
                True,
                "llloom_identity_mismatch",
            ),
            (
                LlloomRunner(liveness={**HEALTHY, "llloom_version": 789}),
                True,
                "liveness_output_malformed",
            ),
            (LlloomRunner(liveness={"bad": True}), True, "liveness_output_malformed"),
            (RaisingRunner(), True, "llloom_spawn_failed"),
            (LlloomRunner(), False, "llloom_binding_missing"),
        )
        for runner, binding, reason in cases:
            with self.subTest(reason=reason):
                before = self.fingerprint()
                fact = self.hooks(runner, binding=binding).preflight()[0]
                self.assertEqual((fact.status, fact.reason), ("refused", reason))
                self.assertEqual(self.fingerprint(), before)

    def test_file_link_and_escaping_roots_refuse_before_spawn(self):
        runner = LlloomRunner()
        file_root = self.project / "not-memory"
        file_root.write_bytes(b"ordinary file")
        cases = (
            MemoryMode("llloom", "not-memory"),
            MemoryMode("llloom", "../escape"),
        )
        for mode in cases:
            with self.subTest(root=mode.memory_root):
                hooks = LlloomMemoryHooks(
                    project_root=self.project,
                    memory_mode=mode,
                    binding=self.hooks(runner)._binding,
                    binding_refusal=None,
                    store=self.store,
                    run_id="run_001",
                    runner=runner,
                )
                self.assertEqual(hooks.preflight()[0].status, "refused")
        self.assertEqual(runner.calls, [])

    @unittest.skipUnless(
        hasattr(os, "symlink"), "host has no symbolic-link surface"
    )
    def test_link_like_root_refuses_before_spawn(self):
        linked = self.project / "linked-memory"
        try:
            linked.symlink_to(self.root, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"host symbolic-link privilege unavailable: {error}")
        runner = LlloomRunner()
        hooks = LlloomMemoryHooks(
            project_root=self.project,
            memory_mode=MemoryMode("llloom", "linked-memory"),
            binding=self.hooks(runner)._binding,
            binding_refusal=None,
            store=self.store,
            run_id="run_001",
            runner=runner,
        )
        fact = hooks.preflight()[0]
        self.assertEqual(
            (fact.status, fact.reason),
            ("refused", "memory_root_link_like"),
        )
        self.assertEqual(runner.calls, [])

    def test_empty_context_does_not_change_prompt_and_nonempty_is_delimited(self):
        empty = self.hooks(LlloomRunner()).read_context(b"# Task\n\nDo work.\n")
        self.assertEqual(empty.context, b"")
        self.assertEqual(empty.facts[-1].reason, "context_empty")

        runner = LlloomRunner(
            query={
                "question": "bounded",
                "answer": "[claim/demo] bounded fact",
                "citations": [{"claim_id": "demo"}],
                "used_claim_ids": ["claim:demo"],
                "used_verbatim_spans": [],
                "used_structure_items": [],
                "ids_only": False,
            }
        )
        context = self.hooks(runner).read_context(b"# Task\n")
        self.assertIn(b"BEGIN OPTIONAL LLLOOM CONTEXT", context.context)
        self.assertIn(b"NON-AUTHORITATIVE", context.context)
        self.assertIn(b"bounded fact", context.context)
        self.assertEqual(context.facts[-1].reason, "context_available")


class UpdateQueueTests(MemoryHookTestCase):
    def test_write_once_queue_submits_then_retries_as_already_submitted(self):
        runner = LlloomRunner()
        hooks = self.hooks(runner)
        before = self.fingerprint()
        facts = hooks.queue_updates("M005-S01", ("bounded proposal",))
        record = self.store.read_memory_update_queue("run_001", "M005-S01")
        self.assertEqual(record["proposals"], ["bounded proposal"])
        self.assertEqual(
            tuple((fact.hook, fact.status, fact.reason) for fact in facts),
            (
                ("liveness", "healthy", "healthy"),
                (
                    "boundary_update_submission",
                    "healthy",
                    "update_submitted",
                ),
            ),
        )
        self.assertRegex(facts[-1].proposal_id, r"^up\.[0-9a-f]{64}$")
        proposal_path = (
            self.store.run_dir("run_001")
            / "memory_updates"
            / facts[-1].proposal_document
        )
        proposal_before = proposal_path.read_bytes()
        proposal = json.loads(proposal_before.decode("utf-8"))
        self.assertEqual(
            set(proposal),
            {
                "schema",
                "submitter_id",
                "client_proposal_id",
                "requested_action",
                "title",
                "summary",
                "evidence_refs",
            },
        )
        self.assertEqual(proposal["schema"], "llloom.update_proposal.v1")
        self.assertEqual(proposal["submitter_id"], "frutlups-drive")
        self.assertEqual(proposal["requested_action"], "review")
        self.assertEqual(
            proposal["summary"],
            (
                self.store.run_dir("run_001")
                / "memory_updates"
                / "M005-S01.json"
            ).read_text(encoding="utf-8").removesuffix("\n"),
        )
        self.assertEqual(
            proposal["evidence_refs"],
            ["run:run_001", "artifact:memory_updates/M005-S01.json"],
        )
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(runner.calls[-1][0][-2], "submit-update")
        self.assertEqual(Path(runner.calls[-1][0][-1]), proposal_path)
        self.assertFalse(proposal_path.is_relative_to(self.root))

        repeated = hooks.queue_updates("M005-S01", ("bounded proposal",))
        self.assertEqual(
            (repeated[-1].status, repeated[-1].reason),
            ("healthy", "update_already_submitted"),
        )
        self.assertEqual(repeated[-1].proposal_id, facts[-1].proposal_id)
        self.assertEqual(proposal_path.read_bytes(), proposal_before)
        with self.assertRaises(RunStoreRefusal):
            self.store.write_memory_update_queue(
                "run_001",
                "M005-S01",
                {"contract_id": "conflict", "proposals": []},
            )

    def test_every_released_exit_and_identity_mismatch_map_distinctly(self):
        expected = {
            0: ("healthy", "update_submitted"),
            3: ("refused", "update_submission_root_absent"),
            4: ("refused", "update_submission_root_unhealthy"),
            5: ("refused", "update_submission_document_malformed"),
            6: ("refused", "update_submission_document_oversized"),
            7: ("healthy", "update_already_submitted"),
            8: ("refused", "update_submission_unauthorized"),
            9: ("refused", "update_submission_publication_failed"),
        }
        for exit_code, fact_pair in expected.items():
            with self.subTest(exit_code=exit_code):
                runner = LlloomRunner(
                    submission=submission_result(exit_code),
                    submission_exit=exit_code,
                )
                facts = self.hooks(runner).queue_updates(
                    f"M005-S01-E{exit_code}", ("bounded proposal",)
                )
                self.assertEqual(
                    (facts[-1].status, facts[-1].reason), fact_pair
                )
                self.assertEqual(facts[-1].hook, "boundary_update_submission")
                self.assertTrue(facts[-1].evidence.endswith("_stdout.txt"))

        mismatch = LlloomRunner(
            submission=submission_result(0, version="7.8.10"),
            submission_exit=0,
        )
        facts = self.hooks(mismatch).queue_updates(
            "M005-S01-IDENTITY", ("bounded proposal",)
        )
        self.assertEqual(
            (facts[-1].status, facts[-1].reason),
            ("refused", "llloom_identity_mismatch"),
        )

    def test_queue_that_cannot_fit_proposal_summary_refuses_without_submit(self):
        runner = LlloomRunner()
        facts = self.hooks(runner).queue_updates(
            "M005-S01-OVERSIZE", tuple("x" * 1_024 for _ in range(8))
        )
        self.assertEqual(
            (facts[-1].status, facts[-1].reason),
            ("refused", "update_proposal_render_refused"),
        )
        self.assertEqual(
            sum("submit-update" in call[0] for call in runner.calls), 0
        )

    def test_submission_transport_and_malformed_output_refuse_in_hook(self):
        cases = (
            (SubmissionTransportRunner("spawn"), "llloom_spawn_failed"),
            (SubmissionTransportRunner("timeout"), "llloom_timeout"),
            (
                LlloomRunner(
                    submission={"bad": True},
                    submission_exit=0,
                ),
                "update_submission_output_malformed",
            ),
        )
        for index, (runner, reason) in enumerate(cases):
            with self.subTest(reason=reason):
                facts = self.hooks(runner).queue_updates(
                    f"M005-S01-TRANSPORT-{index}", ("bounded proposal",)
                )
                self.assertEqual(
                    (facts[-1].status, facts[-1].reason),
                    ("refused", reason),
                )


class RealLlloomIntegrationTests(unittest.TestCase):
    """Opt-in evidence against the exact governed llloom executable."""

    def setUp(self):
        self.executable = real_llloom_executable(self)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "project"
        self.project.mkdir()
        self.healthy = self.project / "healthy"
        self.absent = self.project / "absent"
        self.unhealthy = self.project / "unhealthy"
        self.pre_inbox = self.project / "pre-inbox"
        self.unhealthy.mkdir()
        for root in (self.healthy, self.pre_inbox):
            initialized = subprocess.run(
                (str(self.executable), "--root", str(root), "init"),
                cwd=self.project,
                env={"PYTHONDONTWRITEBYTECODE": "1"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            self.assertEqual(
                initialized.returncode,
                0,
                initialized.stderr.decode("utf-8", errors="replace"),
            )
        (self.pre_inbox / "state" / "update_proposals").rmdir()
        self.store = RunStore(self.project / ".frutlups_drive")
        self.store.create_run("real_llloom", {"memory_mode": "llloom"})
        repository = Path(__file__).resolve().parents[2]
        self.binding = load_llloom_binding(
            repository / "local_state" / "llloom_binding.toml"
        )
        self.assertEqual(
            Path(self.binding.argv_prefix[0]).resolve(),
            self.executable.resolve(),
        )
        self.assertEqual(self.binding.tool_identity, "llloom-0.1.2")
        self.runners = []

    def hooks(self, root):
        runner = RecordingSubprocessRunner(FakeClock())
        self.runners.append(runner)
        return LlloomMemoryHooks(
            project_root=self.project,
            memory_mode=MemoryMode("llloom", root.name),
            binding=self.binding,
            binding_refusal=None,
            store=self.store,
            run_id="real_llloom",
            runner=runner,
            timeout_seconds=10,
        )

    def captured_stdout(self, fact):
        return json.loads(
            (
                self.store.run_dir("real_llloom")
                / "memory_hooks"
                / fact.evidence
            ).read_text(encoding="utf-8")
        )

    def test_real_liveness_transcripts_use_binding_declared_identity(self):
        cases = (
            (self.healthy, "healthy", "healthy", 0),
            (self.absent, "absent", "root_absent", 3),
            (self.unhealthy, "unhealthy", "layout_invalid", 4),
        )
        for root, status, reason, exit_code in cases:
            with self.subTest(status=status):
                before = tree_identity(root)
                fact = self.hooks(root).preflight()[0]
                result = self.captured_stdout(fact)
                self.assertEqual(result["schema"], "llloom.liveness.v1")
                self.assertEqual(
                    result["llloom_version"], self.binding.tool_version
                )
                self.assertEqual((result["status"], result["reason"]),
                                 (status, reason))
                expected_fact = (
                    ("healthy", "healthy")
                    if status == "healthy"
                    else ("refused", reason)
                )
                self.assertEqual((fact.status, fact.reason), expected_fact)
                self.assertEqual(self.runners[-1].outcomes[-1].exit_code,
                                 exit_code)
                self.assertEqual(tree_identity(root), before)

    def test_real_submission_is_one_file_idempotent_and_pre_inbox_refuses(self):
        hooks = self.hooks(self.healthy)

        before = tree_identity(self.healthy)
        preflight = hooks.preflight()
        self.assertEqual(
            (preflight[0].status, preflight[0].reason),
            ("healthy", "healthy"),
        )
        self.assertEqual(tree_identity(self.healthy), before)

        original = b"# Prompt\n\nOriginal bytes.\n"
        before = tree_identity(self.healthy)
        context = hooks.read_context(original)
        self.assertEqual(context.context, b"")
        self.assertEqual(
            tuple((fact.hook, fact.reason) for fact in context.facts),
            (
                ("liveness", "healthy"),
                ("bounded_context", "context_empty"),
            ),
        )
        self.assertEqual(original, b"# Prompt\n\nOriginal bytes.\n")
        self.assertEqual(tree_identity(self.healthy), before)

        before = tree_identity(self.healthy)
        update = hooks.queue_updates("M005-S01", ("bounded proposal",))
        self.assertEqual(
            tuple((fact.hook, fact.reason) for fact in update),
            (
                ("liveness", "healthy"),
                ("boundary_update_submission", "update_submitted"),
            ),
        )
        self.assertEqual(
            self.store.read_memory_update_queue("real_llloom", "M005-S01")[
                "proposals"
            ],
            ["bounded proposal"],
        )
        after = tree_identity(self.healthy)
        before_rows = {row[1]: row for row in before}
        after_rows = {row[1]: row for row in after}
        added = set(after_rows) - set(before_rows)
        self.assertEqual(
            added,
            {f"state/update_proposals/{update[-1].proposal_id}.json"},
        )
        self.assertEqual(
            {path: after_rows[path] for path in before_rows}, before_rows
        )
        envelope_path = self.healthy / next(iter(added))
        envelope_before = envelope_path.read_bytes()
        envelope = json.loads(envelope_before.decode("utf-8"))
        proposal_path = (
            self.store.run_dir("real_llloom")
            / "memory_updates"
            / update[-1].proposal_document
        )
        self.assertEqual(
            envelope,
            {
                "schema": "llloom.pending_update_proposal.v1",
                "proposal_id": update[-1].proposal_id,
                "status": "pending",
                "document": json.loads(
                    proposal_path.read_text(encoding="utf-8")
                ),
            },
        )
        submitted = self.captured_stdout(update[-1])
        self.assertEqual(
            (submitted["status"], submitted["reason"]),
            ("accepted", "accepted"),
        )
        self.assertEqual(self.runners[-1].outcomes[-1].exit_code, 0)

        before_duplicate = tree_identity(self.healthy)
        duplicate = hooks.queue_updates("M005-S01", ("bounded proposal",))
        self.assertEqual(
            (duplicate[-1].status, duplicate[-1].reason),
            ("healthy", "update_already_submitted"),
        )
        self.assertEqual(duplicate[-1].proposal_id, update[-1].proposal_id)
        self.assertEqual(envelope_path.read_bytes(), envelope_before)
        self.assertEqual(tree_identity(self.healthy), before_duplicate)
        duplicate_result = self.captured_stdout(duplicate[-1])
        self.assertEqual(
            (duplicate_result["status"], duplicate_result["reason"]),
            ("refused", "duplicate_proposal"),
        )
        self.assertEqual(self.runners[-1].outcomes[-1].exit_code, 7)

        legacy_before = tree_identity(self.pre_inbox)
        legacy = self.hooks(self.pre_inbox).queue_updates(
            "M005-S01-LEGACY", ("bounded proposal",)
        )
        self.assertEqual(
            (legacy[-1].status, legacy[-1].reason),
            ("refused", "update_submission_root_unhealthy"),
        )
        legacy_result = self.captured_stdout(legacy[-1])
        self.assertEqual(
            (
                legacy_result["reason"],
                legacy_result["liveness_status"],
                legacy_result["liveness_reason"],
            ),
            ("inbox_unavailable", "healthy", "healthy"),
        )
        self.assertEqual(self.runners[-1].outcomes[-1].exit_code, 4)
        self.assertEqual(tree_identity(self.pre_inbox), legacy_before)

        captures = tuple(
            path.name
            for path in sorted(
                (self.store.run_dir("real_llloom") / "memory_hooks").glob(
                    "*_stdout.txt"
                )
            )
        )
        self.assertEqual(
            captures,
            (
                "001_preflight_liveness_stdout.txt",
                "002_context_liveness_stdout.txt",
                "003_query_stdout.txt",
                "004_update_liveness_stdout.txt",
                "005_submit_update_stdout.txt",
                "006_update_liveness_stdout.txt",
                "007_submit_update_stdout.txt",
                "008_update_liveness_stdout.txt",
                "009_submit_update_stdout.txt",
            ),
        )


if __name__ == "__main__":
    unittest.main()
