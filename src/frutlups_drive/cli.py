"""``frutlups-drive`` CLI: ``run``, ``resume``, ``stop``, ``plan`` (§6, §9).

Exit codes are frozen: 0 clean boundary/success, 1 internal error, 2 refusal
before action, 10 stopped with an escalation artifact. ``plan`` and ``report``
are read-only; ``report`` derives only from an existing run store, while each
mutating verb requires the project-local policy. Local mock runs use the
project-local mock-script convention. External execution is limited to the
two M003-S03 CLI seats and is admitted only through the committed live gate
and an ignored explicit launch binding.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from frutlups_drive import __version__, killswitch
from frutlups_drive.contracts import ExitCode, Role
from frutlups_drive.dispatch.manual import ManualAgentExecutor
from frutlups_drive.dispatch.mock import MockAgentAction, MockAgentExecutor
from frutlups_drive.dispatch.provider_cli import (
    PROVIDER_BINDING_RELATIVE_PATH,
    ProviderBindingError,
    ProviderCliExecutor,
    ProviderRuntimeBindings,
    build_provider_runtime,
    load_provider_binding,
    provider_effort_schedules,
)
from frutlups_drive.frutlupscli import (
    PENDING_VERB_FILENAME,
    FrutlupsBindingError,
    FrutlupsLaunchBinding,
    FrutlupsLaunchIdentity,
    FrutlupsVerbWriter,
    build_launch_identity,
    load_launch_binding,
)
from frutlups_drive.mockverbs import ORCHESTRATOR_VERBS, MockVerbWriter
from frutlups_drive.livegate import (
    LoadedLiveGate,
    LiveGateLoadError,
    load_live_gate,
)
from frutlups_drive.memory_hooks import (
    LLLOOM_BINDING_RELATIVE,
    LlloomBinding,
    LlloomMemoryHooks,
    MemoryHookRefusal,
    load_llloom_binding,
    reconcile_memory_mode,
)
from frutlups_drive.planstate import (
    FrutlupsPlanProvider,
    MemoryMode,
    MockPlanProvider,
    PlanProviderUnavailable,
    PlanningStateRefusal,
)
from frutlups_drive.policy import PolicyRefusal, load_execution_policy
from frutlups_drive.runstore import RunStore, RunStoreRefusal
from frutlups_drive.supervisor import (
    BOUNDARIES,
    EXTERNAL_ADAPTERS,
    LOCAL_ADAPTERS,
    Supervisor,
    TickResult,
    exact_seat_alias,
    mock_plan_offset,
    policy_seat,
    seat_executable_issue,
    shadow_policy_seat,
)
from frutlups_drive.telemetry import (
    TelemetryRefusal,
    derive_campaign_report,
    derive_report,
    render_json,
    render_text,
)
from frutlups_drive.verifier import (
    SubprocessRunner,
    VerificationCommand,
    VerificationPlan,
    Verifier,
)
from frutlups_drive.watcher import Watcher
from frutlups_drive.workspace import WorkspaceManager

MOCK_CONVENTION_DIR = ".frutlups_drive_mock"
POLICY_FILENAME = "frutlups_drive.toml"
LIVE_GATE_PATH = (
    Path(__file__).resolve().parents[3] / "06_infra" / "live_validation_gate.md"
)
_CAMPAIGN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class CliRefusal(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class _SystemClock:
    def now(self) -> float:
        return time.time()


@dataclass(frozen=True)
class _RunAuthority:
    policy: object
    live_gate: LoadedLiveGate | None = None
    providers: ProviderRuntimeBindings | None = None

    def manifest_facts(self) -> dict[str, str]:
        if self.live_gate is None or self.providers is None:
            return {}
        return {
            "live_gate_sha256": self.live_gate.source_sha256,
            **self.providers.manifest_facts(),
        }


@dataclass(frozen=True)
class _LlloomBindingState:
    binding: LlloomBinding | None
    refusal: str | None

    def manifest_facts(self) -> dict[str, str]:
        return self.binding.manifest_facts() if self.binding is not None else {}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if args == ["--version"]:
        print(f"frutlups-drive {__version__}")
        return int(ExitCode.OK)
    parser = _parser()
    try:
        namespace = parser.parse_args(args)
    except SystemExit as exit_request:
        return int(ExitCode.OK) if exit_request.code == 0 else int(ExitCode.REFUSED)
    try:
        return _dispatch(namespace)
    except (
        CliRefusal,
        PolicyRefusal,
        PlanningStateRefusal,
        FrutlupsBindingError,
        LiveGateLoadError,
        ProviderBindingError,
        MemoryHookRefusal,
        TelemetryRefusal,
    ) as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return int(ExitCode.REFUSED)
    except RunStoreRefusal as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return int(ExitCode.REFUSED)
    except Exception as error:  # bounded: no traceback on the owned surface
        print(f"internal error: {type(error).__name__}", file=sys.stderr)
        return int(ExitCode.INTERNAL_ERROR)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="frutlups-drive",
        description="Execution runtime for artifact-first agentic coding loops.",
    )
    parser.add_argument(
        "--version", action="version", version=f"frutlups-drive {__version__}"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="read-only next-action report")
    plan.add_argument("project", type=Path)
    plan.add_argument("--dry-run", action="store_true")

    run = commands.add_parser("run", help="start a run until a boundary")
    run.add_argument("project", type=Path)
    run.add_argument("--until", required=True, choices=BOUNDARIES)
    run.add_argument("--campaign-id")
    run.add_argument("--predecessor-run")

    resume = commands.add_parser("resume", help="reconcile and continue a run")
    resume.add_argument("project", type=Path)
    resume.add_argument("run_id")
    resume.add_argument("--until", choices=BOUNDARIES)

    stop = commands.add_parser("stop", help="create the STOP sentinel")
    stop.add_argument("project", type=Path)

    report = commands.add_parser("report", help="derive run or campaign telemetry")
    report.add_argument("project", type=Path)
    report.add_argument("run_id", nargs="?")
    report.add_argument("--campaign")
    report.add_argument("--all", action="store_true", dest="all_runs")
    report.add_argument("--json", action="store_true")
    return parser


def _dispatch(namespace: argparse.Namespace) -> int:
    project = Path(namespace.project)
    if namespace.command in ("run", "resume"):
        project = project.resolve()
    if not project.is_dir():
        raise CliRefusal("project_missing", "project directory does not exist")
    if namespace.command == "stop":
        # R1-F2: the mandatory policy boundary precedes any STOP mutation.
        # Stopping an already configured manual/future run is a safe control
        # action, so only schema acceptance is required — not mock adapters.
        load_execution_policy(project / POLICY_FILENAME)
        sentinel = killswitch.request_stop(project / ".frutlups_drive")
        print(f"stop requested: {sentinel.name}")
        return int(ExitCode.OK)
    if namespace.command == "plan":
        return _plan(project, dry_run=bool(namespace.dry_run))
    if namespace.command == "report":
        return _report(
            project,
            namespace.run_id,
            bool(namespace.json),
            campaign_id=namespace.campaign,
            all_runs=bool(namespace.all_runs),
        )
    if namespace.command == "run":
        return _run(
            project,
            namespace.until,
            campaign_id=namespace.campaign_id,
            predecessor_run_id=namespace.predecessor_run,
        )
    if namespace.command == "resume":
        return _resume(project, namespace.run_id, namespace.until)
    raise CliRefusal("unknown_command", "command is not part of this milestone")


def _report(
    project: Path,
    run_id: str | None,
    as_json: bool,
    *,
    campaign_id: str | None,
    all_runs: bool,
) -> int:
    selections = sum((run_id is not None, campaign_id is not None, all_runs))
    if selections != 1:
        raise CliRefusal(
            "report_selection_invalid",
            "select exactly one run id, --campaign CAMPAIGN_ID, or --all",
        )
    store = RunStore(project / ".frutlups_drive")
    if run_id is not None:
        report = derive_report(store, run_id)
    else:
        report = derive_campaign_report(
            store, campaign_id=campaign_id, all_runs=all_runs
        )
    sys.stdout.write(render_json(report) if as_json else render_text(report))
    return int(ExitCode.OK)


def _plan(project: Path, dry_run: bool = False) -> int:
    loaded = None
    try:
        loaded = load_execution_policy(project / POLICY_FILENAME)
        external_seats = _policy_external_seats(loaded.policy)
        executable_external_seats = all(
            seat_executable_issue(policy_seat(loaded.policy, Role(role)))
            is None
            for role in external_seats
            if role != "shadow_reviewer"
        )
        if "shadow_reviewer" in external_seats:
            executable_external_seats = (
                executable_external_seats
                and seat_executable_issue(shadow_policy_seat(loaded.policy))
                is None
            )
        if external_seats and executable_external_seats:
            # Plan admission validates the same declared non-secret runtime
            # bindings as run admission, without creating a store or loading
            # credential values.
            _authorized_gate(loaded.policy)
        policy_line = (
            f"policy: present (stop_at={loaded.policy.target.stop_at}, "
            f"coder adapter={loaded.policy.coder.adapter})"
        )
    except PolicyRefusal as refusal:
        if refusal.code == "policy_file_missing":
            policy_line = "policy: absent (required for run/resume/stop only)"
        else:
            raise
    print(policy_line)
    if dry_run:
        _report_dry_run(project, loaded)

    if loaded is not None and loaded.policy.frutlups.provider == "frutlups_cli":
        state = _read_released_plan(project, loaded.policy)
        print(
            "planning check: released frutlups status read "
            "(strict planning_frontier + loop_resume; no provider dispatch)"
        )
        _print_next_action(state)
        return int(ExitCode.OK)

    script_dir = project / MOCK_CONVENTION_DIR
    if not (script_dir / "script.json").is_file():
        print("planning check: mock script unavailable")
        print("next action: unavailable (no mock planning sequence)")
        return int(ExitCode.OK)
    compiled = _compile_mock_script(script_dir)
    print("planning check: mock script .frutlups_drive_mock/script.json")
    position = _latest_tick_count(project)
    if position >= len(compiled.planstate_payloads):
        print("next action: none (planning sequence exhausted)")
        return int(ExitCode.OK)
    state = MockPlanProvider(
        compiled.planstate_payloads[position:]
    ).read_planning_state()
    _print_next_action(state)
    return int(ExitCode.OK)


def _read_released_plan(project: Path, policy):
    policy_bytes = _confirmed_policy_bytes(project, policy)
    binding = _required_binding(project, policy)
    if binding is None:
        raise CliRefusal(
            "planning_provider_missing",
            "the live-configured project has no released-frutlups binding",
        )
    # Plan validates the same executable, layout, package, policy, and finite
    # environment identity as run admission. Captures live only in a disposable
    # system temporary directory, so the driven project and its run store remain
    # byte-read-only.
    _build_launch_identity(project, policy, binding, policy_bytes)
    with tempfile.TemporaryDirectory(prefix="frutlups-drive-plan-") as tmp:
        _, provider = _admit_memory_mode(
            project,
            policy,
            binding,
            "plan",
            capture_root=Path(tmp) / "status",
        )
        assert provider is not None
        try:
            return provider.read_planning_state()
        except PlanProviderUnavailable as refusal:
            raise CliRefusal(refusal.code, refusal.message) from None


def _print_next_action(state) -> None:
    step = state.step.value if state.step else "null"
    if state.frontier is None:
        frontier = "null"
    else:
        frontier = f"{state.frontier.milestone_id}/{state.frontier.slice_id}"
    print(
        f"next action: outcome={state.outcome.value} step={step} "
        f"frontier={frontier}"
    )


def _report_dry_run(project: Path, loaded) -> int | None:
    """Read-only identity/gate report for ``plan --dry-run``.

    Reports exact configured role seats, the coder/reviewer exact-seat alias
    fact, the planning-provider mode, and the absent Phase C live authority.
    Creates no run store, capture, subprocess, or project artifact.
    """
    if loaded is None:
        print("role identities: unavailable (policy absent)")
    else:
        for role in (Role.ARCHITECT, Role.CODER, Role.REVIEWER):
            seat = policy_seat(loaded.policy, role)
            issue = seat_executable_issue(seat)
            note = f" [not executable: {issue}]" if issue else ""
            model = seat.model if seat.model else "(empty)"
            print(
                f"role {role.value}: adapter={seat.adapter} "
                f"model={model}{note}"
            )
        shadow = shadow_policy_seat(loaded.policy)
        shadow_issue = seat_executable_issue(shadow)
        shadow_note = f" [not executable: {shadow_issue}]" if shadow_issue else ""
        shadow_model = shadow.model if shadow.model else "(empty)"
        print(
            "role shadow_reviewer: enabled="
            f"{str(loaded.policy.shadow_reviewer.enabled).lower()} "
            f"adapter={shadow.adapter} model={shadow_model}{shadow_note}"
        )
        coder = policy_seat(loaded.policy, Role.CODER)
        reviewer = policy_seat(loaded.policy, Role.REVIEWER)
        alias = exact_seat_alias(coder, reviewer)
        print(
            "coder/reviewer exact-seat alias: "
            + ("yes (identical adapter and model)" if alias
               else "no (exact-seat identities differ; this is not a "
               "model-family independence claim)")
        )
    script_present = (project / MOCK_CONVENTION_DIR / "script.json").is_file()
    if loaded is not None and loaded.policy.frutlups.provider == "frutlups_cli":
        binding_present = (
            project / loaded.policy.frutlups.binding_path
        ).is_file()
        print(
            "planning provider: frutlups_cli (released "
            f"{loaded.policy.frutlups.contract_id} v"
            f"{loaded.policy.frutlups.contract_version}; local binding "
            + ("present" if binding_present else "missing")
            + ")"
        )
    else:
        print(
            "planning provider: "
            + ("mock convention" if script_present
               else "none (mock convention absent)")
        )
    live_ready = False
    if loaded is not None and _policy_external_seats(loaded.policy):
        try:
            _authorized_gate(loaded.policy)
            live_ready = True
        except CliRefusal:
            pass
    if live_ready:
        print("live authority: ready (committed gate exactly matches external seats)")
    else:
        print("live authority: absent (external adapters refuse before any effect)")
    return None


def _run(
    project: Path,
    boundary: str,
    *,
    campaign_id: str | None = None,
    predecessor_run_id: str | None = None,
) -> int:
    authority = _required_run_authority(project)
    policy = authority.policy
    # R2-F2/R3-F1: the complete mock configuration is compiled into one
    # immutable snapshot — script, planstate payloads, content bytes,
    # actions, verb writes, and the verification plan — before the run
    # store, a manifest, or a journal event exists. Refusal leaves the
    # project byte/member-identical, and the supervisor executes exactly
    # this compiled object with no second input read.
    compiled = _compile_mock_script(project / MOCK_CONVENTION_DIR)
    # M003-S02: the machine-local launch binding is loaded and validated
    # before any store, manifest, journal, subprocess, or agent effect when
    # the committed policy selects the released frutlups CLI provider.
    policy_bytes = _confirmed_policy_bytes(project, policy)
    binding = _required_binding(project, policy)
    launch_identity = (
        _build_launch_identity(project, policy, binding, policy_bytes)
        if binding is not None
        else None
    )
    store = RunStore(project / ".frutlups_drive")
    run_id = store.next_run_id()
    campaign_id, predecessor_run_id = _resolve_run_lineage(
        store,
        policy_campaign_id=policy.campaign_id,
        launch_campaign_id=campaign_id,
        requested_predecessor_run_id=predecessor_run_id,
    )
    memory_mode, admitted_provider = _admit_memory_mode(
        project, policy, binding, run_id
    )
    llloom_binding = _resolve_llloom_binding(project, memory_mode)
    # Seat identity is recorded durably at run creation: exact configured
    # adapter/model per role plus the explicit coder/reviewer exact-seat
    # comparison (never a model-family inference).
    seats = {
        role.value: policy_seat(policy, role)
        for role in (Role.ARCHITECT, Role.CODER, Role.REVIEWER)
    }
    manifest = {
        "boundary": boundary,
        "contract_version": 1,
        "policy_hash": hashlib.sha256(policy_bytes).hexdigest(),
        "started_at": f"{time.time():.3f}",
        "coder_reviewer_exact_alias": exact_seat_alias(
            seats["coder"], seats["reviewer"]
        ),
    }
    if campaign_id is not None:
        manifest["campaign_id"] = campaign_id
    if predecessor_run_id is not None:
        manifest["predecessor_run_id"] = predecessor_run_id
    for role_name, seat in seats.items():
        manifest[f"{role_name}_adapter"] = seat.adapter
        manifest[f"{role_name}_model"] = seat.model
    shadow = shadow_policy_seat(policy)
    manifest["shadow_reviewer_enabled"] = policy.shadow_reviewer.enabled
    manifest["shadow_reviewer_adapter"] = shadow.adapter
    manifest["shadow_reviewer_model"] = shadow.model
    if policy.reporting.currency is not None:
        manifest["reporting_currency"] = policy.reporting.currency
        for index, (provider, ceiling) in enumerate(
            policy.reporting.external_provider_ceilings, 1
        ):
            manifest[f"external_provider_ceiling_{index:03d}_provider"] = provider
            manifest[f"external_provider_ceiling_{index:03d}_amount"] = ceiling
    if authority.providers is not None:
        declaration = authority.live_gate.assessment.declaration
        if declaration is None:
            raise CliRefusal(
                "live_authority_missing",
                "the admitted live gate has no declaration",
            )
        for role_name, schedule in provider_effort_schedules(declaration).items():
            manifest[f"{role_name}_effort"] = schedule[0]
            manifest[f"{role_name}_corrective_effort"] = schedule[1]
        manifest.update(authority.manifest_facts())
    if launch_identity is not None:
        manifest.update(launch_identity.manifest_facts())
    manifest.update(memory_mode.manifest_facts())
    manifest.update(llloom_binding.manifest_facts())
    store.create_run(run_id, manifest)
    run_created: dict[str, object] = {
        "kind": "run_created",
        "t": time.time(),
        "boundary": boundary,
    }
    if campaign_id is not None:
        run_created["campaign_id"] = campaign_id
    if predecessor_run_id is not None:
        run_created["predecessor_run_id"] = predecessor_run_id
    store.append_event(run_id, run_created)
    memory_hooks = _build_memory_hooks(
        project,
        policy,
        memory_mode,
        store,
        run_id,
        binding_state=llloom_binding,
    )
    supervisor = _build_supervisor(
        project,
        store,
        run_id,
        policy,
        boundary,
        compiled,
        binding=binding,
        launch_identity=launch_identity,
        authority=authority,
        admitted_provider=admitted_provider,
        memory_hooks=memory_hooks,
    )
    supervisor.memory_preflight()
    print(f"run started: {run_id}")
    return _finish(supervisor.run_until())


def _required_binding(project: Path, policy) -> "FrutlupsLaunchBinding | None":
    if policy.frutlups.provider != "frutlups_cli":
        return None
    binding_path = _declared_project_member(
        project,
        policy.frutlups.binding_path,
        required_prefix="local_state/",
        code="binding_path_invalid",
    )
    return load_launch_binding(binding_path)


def _admit_memory_mode(
    project: Path,
    policy,
    binding: "FrutlupsLaunchBinding | None",
    run_id: str,
    *,
    capture_root: Path | None = None,
) -> tuple[MemoryMode, "FrutlupsPlanProvider | None"]:
    """Observe and reconcile declaration authority before run creation."""

    if binding is None:
        mode = MemoryMode.none()
        reconcile_memory_mode(mode, policy)
        return mode, None
    clock = _SystemClock()
    provider = FrutlupsPlanProvider(
        argv=tuple(binding.argv_prefix) + ("status", ".", "--json"),
        cwd=project,
        capture_root=capture_root
        or (project / "local_state" / "frutlups_transport" / run_id / "status"),
        timeout_seconds=float(policy.frutlups.timeout_seconds or 120),
        runner=SubprocessRunner(clock),
        env=binding.env,
    )
    try:
        mode = provider.read_memory_mode()
    except PlanProviderUnavailable as refusal:
        raise CliRefusal(refusal.code, refusal.message) from None
    reconcile_memory_mode(mode, policy)
    provider.bind_memory_mode(mode)
    return mode, provider


def _resolve_run_lineage(
    store: RunStore,
    *,
    policy_campaign_id: str | None,
    launch_campaign_id: str | None,
    requested_predecessor_run_id: str | None,
) -> tuple[str | None, str | None]:
    """Resolve opt-in campaign identity and one honest predecessor edge.

    A fresh-run-required stop prepares an exact command carrying the explicit
    ``--predecessor-run`` edge. Ordinary launches stay unlinked, and ordinary
    ``resume`` continues the same run rather than inventing a second lifecycle.
    """

    campaign = _checked_campaign_id(launch_campaign_id)
    if policy_campaign_id is not None:
        if campaign is not None and campaign != policy_campaign_id:
            raise CliRefusal(
                "campaign_identity_conflict",
                "the launch and policy declare different campaign ids",
            )
        campaign = policy_campaign_id

    predecessor = requested_predecessor_run_id
    if predecessor is not None:
        try:
            exists = store.run_exists(predecessor)
        except RunStoreRefusal as refusal:
            raise CliRefusal("predecessor_run_invalid", refusal.message) from None
        if not exists:
            raise CliRefusal(
                "predecessor_run_missing",
                "the declared predecessor run does not exist in this project",
            )
    if predecessor is not None:
        predecessor_campaign = _manifest_campaign(store, predecessor)
        if (
            predecessor_campaign is not None
            and campaign is not None
            and predecessor_campaign != campaign
        ):
            raise CliRefusal(
                "campaign_lineage_mismatch",
                "the predecessor run belongs to a different campaign",
            )
        campaign = campaign or predecessor_campaign
    return campaign, predecessor


def _checked_campaign_id(value: str | None) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not _CAMPAIGN_ID.fullmatch(value):
        raise CliRefusal(
            "campaign_id_invalid",
            "campaign id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}",
        )
    return value


def _manifest_campaign(store: RunStore, run_id: str) -> str | None:
    value = _run_manifest(store, run_id).get("campaign_id")
    return value if isinstance(value, str) and _CAMPAIGN_ID.fullmatch(value) else None


def _run_manifest(store: RunStore, run_id: str) -> dict:
    try:
        return tomllib.loads(
            (store.run_dir(run_id) / "manifest.toml").read_bytes().decode("utf-8")
        )
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise CliRefusal(
            "manifest_invalid",
            f"run '{run_id}' has no readable strict TOML manifest",
        ) from None


def _build_memory_hooks(
    project: Path,
    policy,
    mode: MemoryMode,
    store: RunStore,
    run_id: str,
    *,
    binding_state: _LlloomBindingState | None = None,
) -> LlloomMemoryHooks | None:
    # Structural short circuit: neither binding lookup nor llloom code is
    # reachable for declaration modes none/lightweight.
    if mode.mode != "llloom":
        return None
    binding_state = binding_state or _resolve_llloom_binding(project, mode)
    return LlloomMemoryHooks(
        project_root=project,
        memory_mode=mode,
        binding=binding_state.binding,
        binding_refusal=binding_state.refusal,
        store=store,
        run_id=run_id,
        runner=SubprocessRunner(_SystemClock()),
        timeout_seconds=min(float(policy.frutlups.timeout_seconds or 120), 30.0),
    )


def _resolve_llloom_binding(
    project: Path,
    mode: MemoryMode,
) -> _LlloomBindingState:
    if mode.mode != "llloom":
        return _LlloomBindingState(None, None)
    try:
        binding_path = _declared_project_member(
            project,
            LLLOOM_BINDING_RELATIVE,
            required_prefix="local_state/",
            code="llloom_binding_path_invalid",
        )
        return _LlloomBindingState(load_llloom_binding(binding_path), None)
    except (CliRefusal, MemoryHookRefusal) as error:
        return _LlloomBindingState(None, error.code)


def _declared_project_member(
    project: Path,
    relative: str,
    *,
    required_prefix: str | None,
    code: str,
) -> Path:
    if type(relative) is not str or (
        required_prefix is not None and not relative.startswith(required_prefix)
    ):
        raise CliRefusal(code, "the declared local tool path is outside authority")
    candidate = project.joinpath(*PurePosixPath(relative).parts)
    cursor = project
    for part in PurePosixPath(relative).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise CliRefusal(
                code, "the declared local tool path uses a link-like alias"
            )
    try:
        candidate.resolve(strict=False).relative_to(project.resolve())
    except (OSError, ValueError):
        raise CliRefusal(
            code, "the declared local tool path escapes the project root"
        ) from None
    return candidate


def _build_launch_identity(
    project: Path,
    policy,
    binding: FrutlupsLaunchBinding,
    policy_bytes: bytes,
) -> FrutlupsLaunchIdentity:
    layout = _declared_project_member(
        project,
        policy.frutlups.layout_config,
        required_prefix=None,
        code="layout_path_invalid",
    )
    declared_package = policy.frutlups.package_identity
    if declared_package and declared_package != binding.tool_identity:
        raise CliRefusal(
            "package_identity_mismatch",
            "the declared frutlups package identity does not exactly match "
            "the proven tool identity; nothing was created or spawned",
        )
    return build_launch_identity(
        binding,
        layout_path=layout,
        contract_id=policy.frutlups.contract_id,
        contract_version=policy.frutlups.contract_version,
        package_identity=declared_package or binding.tool_identity,
        policy_hash=hashlib.sha256(policy_bytes).hexdigest(),
    )


def _current_launch_identity(project: Path) -> FrutlupsLaunchIdentity:
    policy_path = project / POLICY_FILENAME
    before = policy_path.read_bytes()
    loaded = load_execution_policy(policy_path)
    after = policy_path.read_bytes()
    if before != after:
        raise CliRefusal(
            "policy_identity_changed",
            "the execution policy changed while its launch identity was read",
        )
    binding = _required_binding(project, loaded.policy)
    if binding is None:
        raise FrutlupsBindingError(
            "launch_identity_changed",
            "the released frutlups provider is no longer selected",
        )
    return _build_launch_identity(
        project,
        loaded.policy,
        binding,
        before,
    )


def _confirmed_policy_bytes(project: Path, expected_policy) -> bytes:
    policy_path = project / POLICY_FILENAME
    before = policy_path.read_bytes()
    confirmed = load_execution_policy(policy_path).policy
    after = policy_path.read_bytes()
    if before != after or confirmed != expected_policy:
        raise CliRefusal(
            "policy_identity_changed",
            "the execution policy changed while its launch identity was read",
        )
    return before


def _resume(project: Path, run_id: str, boundary: str | None) -> int:
    authority = _required_run_authority(project)
    policy = authority.policy
    policy_bytes = _confirmed_policy_bytes(project, policy)
    binding = _required_binding(project, policy)
    launch_identity = (
        _build_launch_identity(project, policy, binding, policy_bytes)
        if binding is not None
        else None
    )
    store = RunStore(project / ".frutlups_drive")
    if not store.run_exists(run_id):
        raise CliRefusal("run_missing", "run id does not exist in this project")
    manifest = tomllib.loads(
        (store.run_dir(run_id) / "manifest.toml").read_bytes().decode("utf-8")
    )
    if boundary is None:
        boundary = str(manifest.get("boundary", "milestone_complete"))
    if authority.providers is not None:
        recorded_provider_facts = {
            key: manifest.get(key) for key in authority.manifest_facts()
        }
        if recorded_provider_facts != authority.manifest_facts():
            raise CliRefusal(
                "provider_authority_changed",
                "the live gate, provider executables, or Kimi effort config "
                "no longer matches the run manifest",
            )
    elif "live_gate_sha256" in manifest:
        raise CliRefusal(
            "provider_authority_changed",
            "the run manifest requires live provider authority",
        )
    # A changed binding or tool identity between runs is contradictory
    # state: the manifest snapshot must still match the local binding.
    if launch_identity is not None:
        recorded = {
            key: manifest.get(key)
            for key in launch_identity.manifest_facts()
        }
        if recorded != launch_identity.manifest_facts():
            raise CliRefusal(
                "binding_identity_changed",
                "the current policy, binding, executable, layout, or declared "
                "tool identity no longer matches the run manifest",
            )
    elif any(
        key in manifest
        for key in FrutlupsLaunchIdentity.manifest_names() - {"policy_hash"}
    ):
        raise CliRefusal(
            "binding_identity_changed",
            "the run manifest requires the released frutlups launch identity "
            "but the current policy no longer selects it",
        )
    memory_mode, admitted_provider = _admit_memory_mode(
        project, policy, binding, run_id
    )
    recorded_memory = {
        key: manifest.get(key) for key in memory_mode.manifest_facts()
    }
    if recorded_memory != memory_mode.manifest_facts():
        raise CliRefusal(
            "memory_mode_changed",
            "the current declared memory mode no longer matches the run manifest",
        )
    llloom_binding = _resolve_llloom_binding(project, memory_mode)
    current_llloom_facts = llloom_binding.manifest_facts()
    recorded_llloom_facts = {
        key: manifest.get(key) for key in LlloomBinding.manifest_names()
    }
    if current_llloom_facts:
        if recorded_llloom_facts != current_llloom_facts:
            raise CliRefusal(
                "llloom_binding_identity_changed",
                "the llloom binding, executable, or declared tool identity "
                "no longer matches the run manifest",
            )
    elif any(key in manifest for key in LlloomBinding.manifest_names()):
        raise CliRefusal(
            "llloom_binding_identity_changed",
            "the run manifest requires a llloom binding identity that is "
            "not currently admitted",
        )
    # R3-F1: resume independently compiles one snapshot before supervisor
    # construction and uses that exact object throughout the invocation.
    compiled = _compile_mock_script(project / MOCK_CONVENTION_DIR)
    memory_hooks = _build_memory_hooks(
        project,
        policy,
        memory_mode,
        store,
        run_id,
        binding_state=llloom_binding,
    )
    supervisor = _build_supervisor(
        project,
        store,
        run_id,
        policy,
        boundary,
        compiled,
        binding=binding,
        launch_identity=launch_identity,
        authority=authority,
        admitted_provider=admitted_provider,
        memory_hooks=memory_hooks,
    )
    supervisor.memory_preflight()
    stop = supervisor.resume()
    if stop is not None:
        return _finish(stop)
    return _finish(supervisor.run_until())


def _finish(result: TickResult) -> int:
    if result.kind == "boundary":
        print(f"boundary reached: {result.detail}")
        return int(ExitCode.OK)
    if result.kind == "stopped":
        escalation = result.escalation_path.name if result.escalation_path else ""
        print(
            f"stopped: {result.stop_reason.value if result.stop_reason else ''} "
            f"(escalation {escalation})"
        )
        return int(ExitCode.STOPPED_WITH_ESCALATION)
    print(f"refused: {result.detail}", file=sys.stderr)
    return int(ExitCode.REFUSED)


def _policy_external_seats(policy) -> dict[str, tuple[str, str, str | None]]:
    seats = {}
    for role in (Role.ARCHITECT, Role.CODER, Role.REVIEWER):
        seat = policy_seat(policy, role)
        if seat.adapter in EXTERNAL_ADAPTERS:
            section = getattr(policy, role.value)
            seats[role.value] = (
                seat.adapter,
                seat.model,
                section.corrective_effort,
            )
    shadow = shadow_policy_seat(policy)
    if policy.shadow_reviewer.enabled and shadow.adapter in EXTERNAL_ADAPTERS:
        seats["shadow_reviewer"] = (shadow.adapter, shadow.model, None)
    return seats


def _authorized_gate(policy) -> LoadedLiveGate:
    try:
        loaded = load_live_gate(LIVE_GATE_PATH)
    except LiveGateLoadError as refusal:
        raise CliRefusal(
            "live_authority_missing",
            f"the committed live gate refused ({refusal.code}); nothing was "
            "created, spawned, or spent",
        ) from None
    assessment = loaded.assessment
    declaration = assessment.declaration
    if not assessment.ready or declaration is None:
        codes = sorted({issue.code for issue in assessment.issues})
        summary = ",".join(codes) if codes else "not_ready"
        raise CliRefusal(
            "live_authority_missing",
            f"the committed live gate is not ready ({summary}); nothing was "
            "created, spawned, or spent",
        )
    gate_seats = {
        "architect": (
            declaration.architect_adapter,
            declaration.architect_model,
            declaration.architect_corrective_effort,
        ),
        "coder": (
            declaration.coder_adapter,
            declaration.coder_model,
            declaration.coder_corrective_effort,
        ),
        "reviewer": (
            declaration.reviewer_adapter,
            declaration.reviewer_model,
            declaration.reviewer_corrective_effort,
        ),
    }
    if _policy_external_seats(policy) != gate_seats:
        raise CliRefusal(
            "live_authority_missing",
            "the committed gate seats and corrective efforts do not exactly "
            "equal the policy's external declarations; nothing was created, "
            "spawned, or spent",
        )
    if (
        tuple(policy.runtime_environment_bindings)
        != declaration.runtime_environment_bindings
    ):
        raise CliRefusal(
            "runtime_environment_binding_missing",
            "the policy and live gate do not declare the same approved non-secret runtime environment bindings; nothing was created, spawned, or spent",
        )
    if (
        tuple(policy.dispatch.role_call_ceiling_seconds)
        != declaration.role_call_ceiling_seconds
        or tuple(policy.dispatch.slice_call_ceiling_overrides)
        != declaration.slice_call_ceiling_overrides
    ):
        raise CliRefusal(
            "dispatch_ceiling_authority_mismatch",
            "the policy and live gate do not declare the same role and slice call ceilings; nothing was created, spawned, or spent",
        )
    if (
        policy.architect_corrective_turn_enabled
        != declaration.architect_corrective_turn_enabled
        or policy.max_architect_corrective_turns_per_run
        != declaration.max_architect_corrective_turns_per_run
    ):
        raise CliRefusal(
            "architect_corrective_turn_authority_mismatch",
            "the policy and live gate do not declare the same architect corrective-turn enablement and per-run cap; nothing was created, spawned, or spent",
        )
    if (
        policy.reporting.currency != declaration.reporting_currency
        or tuple(policy.reporting.external_provider_ceilings)
        != declaration.external_provider_ceilings
    ):
        raise CliRefusal(
            "reporting_authority_mismatch",
            "the policy and live gate do not declare the same reporting currency and external provider ceilings; nothing was created, spawned, or spent",
        )
    return loaded


def _required_run_authority(project: Path) -> _RunAuthority:
    loaded = load_execution_policy(project / POLICY_FILENAME)
    policy = loaded.policy
    adapters = {
        policy.architect.adapter,
        policy.coder.adapter,
        policy.reviewer.adapter,
    }
    if policy.shadow_reviewer.enabled:
        adapters.add(policy.shadow_reviewer.adapter)
    if (
        policy.architect.adapter in LOCAL_ADAPTERS
        and policy.coder.adapter == "mock"
        and policy.reviewer.adapter == "mock"
        and (
            not policy.shadow_reviewer.enabled
            or policy.shadow_reviewer.adapter in LOCAL_ADAPTERS
        )
    ):
        return _RunAuthority(policy)
    if any(adapter in EXTERNAL_ADAPTERS for adapter in adapters):
        for role in (Role.ARCHITECT, Role.CODER, Role.REVIEWER):
            issue = seat_executable_issue(policy_seat(policy, role))
            if issue is not None:
                raise CliRefusal(
                    "live_authority_missing",
                    f"an external seat is not executable ({issue}); nothing "
                    "was created, spawned, or spent",
                )
        if policy.shadow_reviewer.enabled:
            issue = seat_executable_issue(shadow_policy_seat(policy))
            if issue is not None:
                raise CliRefusal(
                    "live_authority_missing",
                    f"an external seat is not executable ({issue}); nothing "
                    "was created, spawned, or spent",
                )
        gate = _authorized_gate(policy)
        declaration = gate.assessment.declaration
        assert declaration is not None
        binding_path = _declared_project_member(
            project,
            PROVIDER_BINDING_RELATIVE_PATH,
            required_prefix="local_state/",
            code="provider_binding_path_invalid",
        )
        bundle = load_provider_binding(binding_path)
        providers = build_provider_runtime(bundle, declaration)
        return _RunAuthority(policy, gate, providers)
    raise CliRefusal(
        "adapter_unavailable",
        "the configured adapter set is not available in this milestone",
    )


def _assert_live_authority_current(project: Path, authority: _RunAuthority) -> None:
    if authority.live_gate is None or authority.providers is None:
        return
    current_gate = _authorized_gate(authority.policy)
    if current_gate.source_sha256 != authority.live_gate.source_sha256:
        raise CliRefusal(
            "live_authority_changed",
            "the committed live gate changed after run admission",
        )
    binding_path = _declared_project_member(
        project,
        PROVIDER_BINDING_RELATIVE_PATH,
        required_prefix="local_state/",
        code="provider_binding_path_invalid",
    )
    declaration = current_gate.assessment.declaration
    assert declaration is not None
    current = build_provider_runtime(load_provider_binding(binding_path), declaration)
    if current.manifest_facts() != authority.providers.manifest_facts():
        raise CliRefusal(
            "provider_authority_changed",
            "the provider binding or Kimi effort config changed after run admission",
        )


def _reject_json_constant(token: str) -> None:
    raise ValueError("non-finite JSON constants are not part of the schema")


def _load_script(script_dir: Path) -> dict:
    path = script_dir / "script.json"
    if not path.is_file():
        raise CliRefusal(
            "mock_script_missing",
            "the mock sequence convention requires "
            f"{MOCK_CONVENTION_DIR}/script.json",
        )
    try:
        # Strict constants (R2-F2): NaN/Infinity/-Infinity are refusals, not
        # parsed values that could later evade a numeric comparison.
        return json.loads(
            path.read_bytes().decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except ValueError:
        raise CliRefusal(
            "mock_script_invalid", "the mock sequence script is not valid JSON"
        ) from None


@dataclass(frozen=True)
class _CompiledMockScript:
    """One immutable per-invocation snapshot of the mock convention (R3-F1).

    Compilation reads ``script.json`` and every referenced planstate and
    content member exactly once, validates the complete structure current
    mock execution consumes, and captures only tuples, bytes, and frozen
    objects. Execution never re-reads the convention directory within the
    invocation, so the authority decision and the executed configuration
    are the same object."""

    planstate_payloads: tuple[bytes, ...]
    architect_actions: tuple[MockAgentAction, ...]
    coder_actions: tuple[MockAgentAction, ...]
    reviewer_actions: tuple[MockAgentAction, ...]
    shadow_reviewer_actions: tuple[MockAgentAction, ...]
    verb_scripts: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    verification_plan: VerificationPlan

    def actions_for(self, role: str) -> tuple[MockAgentAction, ...]:
        return {
            "architect": self.architect_actions,
            "coder": self.coder_actions,
            "reviewer": self.reviewer_actions,
            "shadow_reviewer": self.shadow_reviewer_actions,
        }[role]


def _read_member(script_dir: Path, name: object) -> bytes:
    if not isinstance(name, str):
        raise CliRefusal(
            "mock_script_invalid",
            "a referenced mock member name must be a string",
        )
    try:
        return (script_dir / name).read_bytes()
    except OSError:
        raise CliRefusal(
            "mock_script_invalid",
            "a member referenced by the mock script could not be read",
        ) from None


def _read_text_member(script_dir: Path, name: object) -> str:
    try:
        return _read_member(script_dir, name).decode("utf-8")
    except UnicodeDecodeError:
        raise CliRefusal(
            "mock_script_invalid",
            "a member referenced by the mock script is not valid UTF-8",
        ) from None


def _compiled_writes(
    script_dir: Path, entry: dict
) -> tuple[tuple[str, str], ...]:
    writes = entry.get("writes", [])
    if not isinstance(writes, list):
        raise CliRefusal(
            "mock_script_invalid", "an action's writes must be a list"
        )
    captured = []
    for write in writes:
        if not isinstance(write, dict) or not isinstance(
            write.get("path"), str
        ):
            raise CliRefusal(
                "mock_script_invalid",
                "a write needs string path and content_file members",
            )
        captured.append(
            (write["path"], _read_text_member(script_dir, write.get("content_file")))
        )
    return tuple(captured)


def _compiled_command(entry: object) -> VerificationCommand:
    if not isinstance(entry, dict):
        raise CliRefusal(
            "mock_script_invalid",
            "a verification command must be a JSON object",
        )
    argv_entry = entry.get("argv", [])
    if not isinstance(argv_entry, list):
        raise CliRefusal(
            "mock_script_invalid", "a verification argv must be a list"
        )
    argv = tuple(
        sys.executable if part == "{python}" else str(part)
        for part in argv_entry
    )
    command_kwargs = {}
    if "max_stream_bytes" in entry:
        declared = entry["max_stream_bytes"]
        if type(declared) is not int:
            raise CliRefusal(
                "verification_capture_invalid",
                "max_stream_bytes must be a plain integer",
            )
        command_kwargs["max_stream_bytes"] = declared
    # R4-F1: the raw declared timeout passes through the shared verifier
    # declaration boundary inside VerificationCommand; its one stable
    # validation error maps to the existing owned exit-2 refusal without
    # echoing the value and without any caller-local numeric special case.
    try:
        return VerificationCommand(
            argv=argv,
            cwd=str(entry.get("cwd", ".")),
            timeout_seconds=entry.get("timeout_seconds", 120.0),
            **command_kwargs,
        )
    except ValueError as invalid:
        raise CliRefusal(
            "verification_capture_invalid", str(invalid)
        ) from None


def _compile_mock_script(script_dir: Path) -> _CompiledMockScript:
    """The one compile boundary (R3-F1): a single strict ``script.json``
    read, complete structural validation, and eager capture of every
    referenced member into an immutable snapshot — all before any run
    mutation. Diagnostics stay bounded and never echo invalid values."""
    script = _load_script(script_dir)
    if not isinstance(script, dict):
        raise CliRefusal(
            "mock_script_invalid", "the mock script must be a JSON object"
        )

    planstate = script.get("planstate", [])
    if not isinstance(planstate, list):
        raise CliRefusal(
            "mock_script_invalid", "planstate must be a list of member names"
        )
    payloads = tuple(_read_member(script_dir, name) for name in planstate)

    executors = script.get("executors", {})
    if not isinstance(executors, dict):
        raise CliRefusal(
            "mock_script_invalid", "executors must be a JSON object"
        )
    actions: dict[str, tuple[MockAgentAction, ...]] = {}
    for role in ("architect", "coder", "reviewer", "shadow_reviewer"):
        entries = executors.get(role, [])
        if not isinstance(entries, list):
            raise CliRefusal(
                "mock_script_invalid",
                "an executor script must be a list of actions",
            )
        role_actions = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise CliRefusal(
                    "mock_script_invalid",
                    "an executor action must be a JSON object",
                )
            try:
                role_actions.append(
                    MockAgentAction(
                        writes=_compiled_writes(script_dir, entry),
                        status=str(entry.get("status", "completed")),
                        exit_reason=str(
                            entry.get("exit_reason", "mock_completed")
                        ),
                        cost_usd=entry.get("cost_usd"),
                        tokens_in=entry.get("tokens_in"),
                        tokens_out=entry.get("tokens_out"),
                        provider_duration_seconds=entry.get(
                            "provider_duration_seconds"
                        ),
                        observed_duration_seconds=entry.get(
                            "observed_duration_seconds"
                        ),
                        retry_class=str(
                            entry.get("retry_class", "not_applicable")
                        ),
                        cost_knowledge=entry.get("cost_knowledge"),
                        capture_truncated=entry.get(
                            "capture_truncated", False
                        ),
                    )
                )
            except ValueError:
                raise CliRefusal(
                    "mock_cost_invalid",
                    "a scripted mock cost must be a finite non-negative "
                    "number",
                ) from None
        actions[role] = tuple(role_actions)

    verbs = script.get("verbs", {})
    if not isinstance(verbs, dict):
        raise CliRefusal("mock_script_invalid", "verbs must be a JSON object")
    verb_scripts = []
    for verb, entries in verbs.items():
        if not isinstance(entries, list):
            raise CliRefusal(
                "mock_script_invalid",
                "a verb script must be a list of artifacts",
            )
        compiled_entries = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(
                entry.get("path"), str
            ):
                raise CliRefusal(
                    "mock_script_invalid",
                    "a verb artifact needs string path and content_file "
                    "members",
                )
            compiled_entries.append(
                (
                    entry["path"],
                    _read_text_member(script_dir, entry.get("content_file")),
                )
            )
        verb_scripts.append((str(verb), tuple(compiled_entries)))

    verification = script.get("verification", {})
    if not isinstance(verification, dict):
        raise CliRefusal(
            "mock_script_invalid", "verification must be a JSON object"
        )
    command_entries = verification.get("commands", [])
    if not isinstance(command_entries, list):
        raise CliRefusal(
            "mock_script_invalid", "verification commands must be a list"
        )
    declared = verification.get("declared_regenerated", [])
    if not isinstance(declared, list) or not all(
        isinstance(path, str) for path in declared
    ):
        raise CliRefusal(
            "mock_script_invalid",
            "declared_regenerated must be a list of paths",
        )
    plan = VerificationPlan(
        commands=tuple(_compiled_command(entry) for entry in command_entries),
        declared_regenerated=tuple(declared),
    )
    return _CompiledMockScript(
        planstate_payloads=payloads,
        architect_actions=actions["architect"],
        coder_actions=actions["coder"],
        reviewer_actions=actions["reviewer"],
        shadow_reviewer_actions=actions["shadow_reviewer"],
        verb_scripts=tuple(verb_scripts),
        verification_plan=plan,
    )


def _latest_tick_count(project: Path) -> int:
    store = RunStore(project / ".frutlups_drive")
    runs_dir = store.root / "runs"
    if not runs_dir.is_dir():
        return 0
    run_ids = sorted(entry.name for entry in runs_dir.iterdir() if entry.is_dir())
    if not run_ids:
        return 0
    events = store.read_events(run_ids[-1])
    return sum(
        1
        for event in events
        if event.get("kind") == "tick" and event.get("consumed")
    )


def _build_supervisor(
    project: Path,
    store: RunStore,
    run_id: str,
    policy,
    boundary: str,
    compiled: _CompiledMockScript,
    binding: "FrutlupsLaunchBinding | None" = None,
    launch_identity: "FrutlupsLaunchIdentity | None" = None,
    authority: _RunAuthority | None = None,
    admitted_provider: "FrutlupsPlanProvider | None" = None,
    memory_hooks: "LlloomMemoryHooks | None" = None,
) -> Supervisor:
    # R3-F1: this constructor consumes only the already-compiled snapshot;
    # it performs no script, planstate, content, or verification re-read.
    # Consumption offsets still derive from durable run events/results and
    # index only into the compiled tuples.
    events = store.read_events(run_id)
    plan_offset = mock_plan_offset(list(events))
    clock = _SystemClock()
    transport_root = project / "local_state" / "frutlups_transport" / run_id
    if binding is not None:
        # Released frutlups planning transport: exact declared argv, finite
        # declared environment, project cwd, bounded ignored capture root.
        provider = admitted_provider or FrutlupsPlanProvider(
            argv=tuple(binding.argv_prefix) + ("status", ".", "--json"),
            cwd=project,
            capture_root=transport_root / "status",
            timeout_seconds=float(policy.frutlups.timeout_seconds or 120),
            runner=SubprocessRunner(clock),
            env=binding.env,
        )
    else:
        provider = MockPlanProvider(
            compiled.planstate_payloads[plan_offset:]
        )

    verb_counts = {verb: 0 for verb in ORCHESTRATOR_VERBS}
    for event in events:
        if event.get("kind") == "verb" and event.get("verb") in verb_counts:
            verb_counts[str(event.get("verb"))] += 1
    # Executor scripts advance only for attempts whose result came from an
    # actual executor run; externally completed attempts never re-pay.
    role_counts = {
        "architect": 0,
        "coder": 0,
        "reviewer": 0,
        "shadow_reviewer": 0,
    }
    for slice_id in store.list_slices(run_id):
        for attempt in store.list_attempts(run_id, slice_id):
            request = store.read_request(attempt)
            result = store.read_result(attempt)
            if (
                request
                and result
                and result.get("exit_reason") != "externally_completed"
                and request.get("role") in role_counts
            ):
                role_counts[str(request["role"])] += 1
    for slice_id in store.list_shadow_slices(run_id):
        for attempt in store.list_shadow_attempts(run_id, slice_id):
            result = store.read_result(attempt)
            if result and result.get("exit_reason") != "externally_completed":
                role_counts["shadow_reviewer"] += 1

    if binding is not None:
        if launch_identity is None:
            raise CliRefusal(
                "launch_identity_missing",
                "released frutlups execution requires a complete launch identity",
            )
        workspace_manager = WorkspaceManager(project, store.root)
        verb_writer = FrutlupsVerbWriter(
            project_root=project,
            binding=binding,
            runner=SubprocessRunner(clock),
            capture_root=transport_root / "verbs",
            store_root=store.root,
            timeout_seconds=float(policy.frutlups.timeout_seconds or 120),
            max_stream_bytes=min(
                policy.frutlups.max_stream_bytes or 1_048_576, 1_048_576
            ),
            status_reader=provider.read_planning_state,
            snapshot=lambda: workspace_manager.transaction_snapshot(project),
            launch_identity=launch_identity,
            identity_reader=lambda: _current_launch_identity(project),
            intent_path=store.run_dir(run_id) / PENDING_VERB_FILENAME,
        )
    else:
        verb_writer = MockVerbWriter(
            project,
            dict(compiled.verb_scripts),
            consumed=verb_counts,
            store_root=store.root,
        )

    authority = authority or _RunAuthority(policy)
    log_dir = store.run_dir(run_id) / "adapter_logs"
    executors = {}
    for role in ("architect", "coder", "reviewer"):
        seat = policy_seat(policy, Role(role))
        if seat.adapter in EXTERNAL_ADAPTERS:
            if authority.providers is None:
                raise CliRefusal(
                    "live_authority_missing",
                    "an external executor was requested without live authority",
                )
            executors[role] = ProviderCliExecutor(
                authority.providers,
                seat.adapter,
                SubprocessRunner(clock),
                log_dir,
                policy.dispatch.capture_truncation_disposition,
            )
        elif seat.adapter == "manual":
            executors[role] = ManualAgentExecutor(
                Watcher(clock, time.sleep),
                sys.stdout,
                log_dir,
                stop_requested=lambda: killswitch.stop_requested(store.root),
                poll_seconds=policy.limits.watch_poll_seconds,
            )
        else:
            executors[role] = MockAgentExecutor(
                compiled.actions_for(role),
                log_dir,
                consumed=role_counts[role],
                store_root=store.root,
            )

    if policy.shadow_reviewer.enabled:
        seat = shadow_policy_seat(policy)
        shadow_log_dir = store.run_dir(run_id) / "shadow" / "_adapter_logs"
        if seat.adapter in EXTERNAL_ADAPTERS:
            raise CliRefusal(
                "live_authority_missing",
                "an external shadow reviewer has no Phase C live authority",
            )
        if seat.adapter == "manual":
            executors["shadow_reviewer"] = ManualAgentExecutor(
                Watcher(clock, time.sleep),
                sys.stdout,
                shadow_log_dir,
                stop_requested=lambda: killswitch.stop_requested(store.root),
                poll_seconds=policy.limits.watch_poll_seconds,
            )
        else:
            executors["shadow_reviewer"] = MockAgentExecutor(
                compiled.actions_for("shadow_reviewer"),
                shadow_log_dir,
                consumed=role_counts["shadow_reviewer"],
                store_root=store.root,
            )

    plan = compiled.verification_plan
    declaration = (
        authority.live_gate.assessment.declaration
        if authority.live_gate is not None
        else None
    )
    return Supervisor(
        project_root=project,
        store=store,
        run_id=run_id,
        policy=policy,
        boundary=boundary,
        plan_provider=provider,
        executors=executors,
        verb_writer=verb_writer,
        verifier=Verifier(store, SubprocessRunner(clock), clock),
        verification_plan=plan,
        watcher=Watcher(clock, time.sleep),
        workspace=WorkspaceManager(project, store.root),
        clock=clock,
        watch_timeout_seconds=(
            declaration.call_timeout_seconds if declaration is not None else 300.0
        ),
        role_efforts=(
            provider_effort_schedules(declaration)
            if authority.providers is not None and declaration is not None
            else {}
        ),
        max_call_cost_usd=(
            declaration.max_call_cost_usd if declaration is not None else None
        ),
        max_total_cost_usd=(
            declaration.max_total_cost_usd if declaration is not None else None
        ),
        external_dispatch_guard=(
            (lambda: _assert_live_authority_current(project, authority))
            if authority.providers is not None
            else None
        ),
        memory_hooks=memory_hooks,
    )
