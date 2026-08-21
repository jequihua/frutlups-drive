"""Three concrete owner-approved subscription CLI bindings.

The binding file is machine-local and ignored.  It declares absolute argv
prefixes for ``codex_cli``, ``kimi_cli``, and ``claude_cli`` plus the Kimi config file
whose effective effort is checked before dispatch.  Provider output remains
transport evidence: stdout and stderr are captured verbatim by the accepted
subprocess executor and are never parsed for loop control, usage, cost, or a
verdict.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from frutlups_drive.contracts import AgentRunRequest, AgentRunResult
from frutlups_drive.dispatch.subprocess_agent import (
    AgentCommandSpec,
    SubprocessAgentExecutor,
    SubprocessAgentFailure,
)
from frutlups_drive.verifier import ProcessRunner

if TYPE_CHECKING:
    from frutlups_drive.livegate import LiveGateDeclaration

PROVIDER_BINDING_SCHEMA_VERSION = "frutlups_drive_provider_binding_v1"
PROVIDER_BINDING_RELATIVE_PATH = "local_state/provider_binding.toml"
APPROVED_PROVIDER_ADAPTERS = ("codex_cli", "kimi_cli", "claude_cli")




@dataclass(frozen=True)
class ProviderSeatCatalogEntry:
    """Verified effort vocabulary and the one fixed project schedule."""

    effort_vocabulary: tuple[str, ...]
    default_effort: str
    corrective_effort: str
    corrective_dispatch_supported: bool = True


_CODEX_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
_CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
APPROVED_SEAT_CATALOG = {
    "codex_cli": {
        "gpt-5.6-sol": ProviderSeatCatalogEntry(
            _CODEX_EFFORTS, "medium", "high"
        ),
        "gpt-5.6-terra": ProviderSeatCatalogEntry(
            _CODEX_EFFORTS, "medium", "medium"
        ),
        "gpt-5.6-luna": ProviderSeatCatalogEntry(
            _CODEX_EFFORTS, "medium", "medium"
        ),
    },
    "kimi_cli": {
        "kimi-code/k3": ProviderSeatCatalogEntry(
            ("low", "high", "max"), "high", "max", False
        ),
        # Minimal verified effort set: pinned default only; full support_efforts unverified.
        "kimi-code/kimi-for-coding": ProviderSeatCatalogEntry(
            ("high",), "high", "high", False
        ),
        # Minimal verified effort set: pinned default only; full support_efforts unverified.
        "kimi-code/kimi-for-coding-highspeed": ProviderSeatCatalogEntry(
            ("high",), "high", "high", False
        ),
    },
    "claude_cli": {
        "claude-opus-5": ProviderSeatCatalogEntry(
            _CLAUDE_EFFORTS, "high", "xhigh"
        )
    },
}
WINDOWS_PROCESS_ENV_NAMES = ("SYSTEMROOT",)
BYTECODE_HYGIENE_ENV = ("PYTHONDONTWRITEBYTECODE", "1")

_MAX_BINDING_BYTES = 65_536
_MAX_CONFIG_BYTES = 1_048_576
_MAX_ARGV_PREFIX_PARTS = 8
_MAX_WINDOWS_COMMAND_LINE_UNITS = 32_767
_SAFE_TEXT = re.compile(r"[^\x00-\x1f\x7f]{1,2048}")


class ProviderBindingError(Exception):
    """Fail-closed local-binding/config refusal with bounded diagnostics."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ProviderLaunch:
    adapter: str
    argv_prefix: tuple[str, ...]
    executable_sha256: str


@dataclass(frozen=True)
class ProviderLaunchBundle:
    codex: ProviderLaunch
    kimi: ProviderLaunch
    claude: ProviderLaunch
    kimi_config_path: Path
    binding_sha256: str
    kimi_config_sha256: str
    kimi_effective_effort: str
    kimi_effective_efforts: tuple[tuple[str, str], ...]

    def launch_for(self, adapter: str) -> ProviderLaunch:
        if adapter == "codex_cli":
            return self.codex
        if adapter == "kimi_cli":
            return self.kimi
        if adapter == "claude_cli":
            return self.claude
        raise ProviderBindingError(
            "provider_adapter_unapproved",
            "the configured adapter has no M003-S03 provider binding",
        )

    def manifest_facts(self) -> dict[str, str]:
        return {
            "provider_binding_sha256": self.binding_sha256,
            "codex_executable_sha256": self.codex.executable_sha256,
            "kimi_executable_sha256": self.kimi.executable_sha256,
            "claude_executable_sha256": self.claude.executable_sha256,
            "kimi_config_sha256": self.kimi_config_sha256,
            "kimi_effective_effort": self.kimi_effective_effort,
        }


@dataclass(frozen=True)
class ProviderRuntimeBindings:
    bundle: ProviderLaunchBundle
    child_env: tuple[tuple[str, str], ...]
    timeout_seconds: float
    max_call_cost_usd: float

    def manifest_facts(self) -> dict[str, str]:
        return self.bundle.manifest_facts()


def _read_bounded(path: Path, maximum: int, *, missing: str, oversized: str) -> bytes:
    try:
        with path.open("rb") as stream:
            raw = stream.read(maximum + 1)
    except OSError:
        raise ProviderBindingError(missing, "the local provider input is unavailable") from None
    if len(raw) > maximum:
        raise ProviderBindingError(oversized, "the local provider input exceeds its size bound")
    return raw


def _decode_toml(raw: bytes, *, code: str) -> dict:
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise ProviderBindingError(code, "the local provider input is not valid TOML") from None
    if not isinstance(document, dict):
        raise ProviderBindingError(code, "the local provider input must be a TOML table")
    return document


def _load_launch(adapter: str, table: object) -> ProviderLaunch:
    if not isinstance(table, dict) or set(table) != {"argv_prefix"}:
        raise ProviderBindingError(
            "provider_binding_field_invalid",
            "each provider binding must declare only argv_prefix",
        )
    raw_prefix = table.get("argv_prefix")
    if (
        not isinstance(raw_prefix, list)
        or not 1 <= len(raw_prefix) <= _MAX_ARGV_PREFIX_PARTS
        or not all(isinstance(part, str) and _SAFE_TEXT.fullmatch(part) for part in raw_prefix)
    ):
        raise ProviderBindingError(
            "provider_binding_field_invalid",
            "provider argv_prefix must be a small non-empty string list",
        )
    executable = Path(raw_prefix[0])
    if not executable.is_absolute():
        raise ProviderBindingError(
            "provider_binding_field_invalid",
            "provider argv_prefix[0] must be an absolute executable path",
        )
    if not executable.is_file():
        raise ProviderBindingError(
            "provider_executable_missing",
            "a declared provider executable is absent",
        )
    try:
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    except OSError:
        raise ProviderBindingError(
            "provider_executable_missing",
            "a declared provider executable could not be read",
        ) from None
    return ProviderLaunch(adapter, tuple(raw_prefix), digest)


def _effective_kimi_effort(config: dict, model_name: str) -> str:
    models = config.get("models")
    if not isinstance(models, dict):
        raise ProviderBindingError(
            "kimi_effort_unresolved", "the Kimi model table is unavailable"
        )
    model = models.get(model_name)
    if not isinstance(model, dict):
        raise ProviderBindingError(
            "kimi_effort_unresolved", "an approved Kimi model is not configured"
        )
    supported = model.get("support_efforts")
    default = model.get("default_effort")
    if (
        not isinstance(supported, list)
        or not all(isinstance(item, str) for item in supported)
        or not isinstance(default, str)
        or default not in supported
    ):
        raise ProviderBindingError(
            "kimi_effort_unresolved", "the Kimi effort metadata is invalid"
        )
    thinking = config.get("thinking", {})
    if not isinstance(thinking, dict):
        raise ProviderBindingError(
            "kimi_effort_unresolved", "the Kimi thinking configuration is invalid"
        )
    configured = thinking.get("effort", default)
    if not isinstance(configured, str):
        raise ProviderBindingError(
            "kimi_effort_unresolved", "the Kimi effort setting is invalid"
        )
    return configured if configured in supported else default


def load_provider_binding(path: Path | str) -> ProviderLaunchBundle:
    """Load both exact provider argv prefixes and resolve Kimi effort."""

    binding_path = Path(path)
    raw = _read_bounded(
        binding_path,
        _MAX_BINDING_BYTES,
        missing="provider_binding_missing",
        oversized="provider_binding_oversized",
    )
    document = _decode_toml(raw, code="provider_binding_malformed")
    if document.get("schema_version") != PROVIDER_BINDING_SCHEMA_VERSION:
        raise ProviderBindingError(
            "provider_binding_schema_unknown",
            "the provider binding schema_version is not implemented",
        )
    if set(document) != {
        "schema_version",
        "codex_cli",
        "kimi_cli",
        "claude_cli",
        "kimi_config_path",
    }:
        raise ProviderBindingError(
            "provider_binding_field_invalid",
            "the provider binding has missing or unknown fields",
        )
    config_value = document.get("kimi_config_path")
    if not isinstance(config_value, str) or not _SAFE_TEXT.fullmatch(config_value):
        raise ProviderBindingError(
            "provider_binding_field_invalid", "kimi_config_path must be a string"
        )
    config_path = Path(config_value)
    if not config_path.is_absolute():
        raise ProviderBindingError(
            "provider_binding_field_invalid", "kimi_config_path must be absolute"
        )
    config_raw = _read_bounded(
        config_path,
        _MAX_CONFIG_BYTES,
        missing="kimi_config_missing",
        oversized="kimi_config_oversized",
    )
    config = _decode_toml(config_raw, code="kimi_config_malformed")
    models = config.get("models")
    if not isinstance(models, dict):
        raise ProviderBindingError(
            "kimi_effort_unresolved", "the Kimi model table is unavailable"
        )
    # R2-F1 (campaign-2 admission): a configured-but-unselected approved
    # model that declares no effort metadata at all is skipped, never
    # fatal — real host configs carry such entries. An entry that
    # declares any effort metadata still validates strictly, and the
    # runtime seat check below fails closed when a *selected* model did
    # not resolve here.
    kimi_effective_efforts = tuple(
        (model, _effective_kimi_effort(config, model))
        for model in APPROVED_SEAT_CATALOG["kimi_cli"]
        if model in models
        and isinstance(models[model], dict)
        and (
            "support_efforts" in models[model]
            or "default_effort" in models[model]
        )
    )
    if not kimi_effective_efforts:
        raise ProviderBindingError(
            "kimi_effort_unresolved",
            "no approved Kimi catalog entry is configured",
        )
    for model, effective in kimi_effective_efforts:
        if effective != APPROVED_SEAT_CATALOG["kimi_cli"][model].default_effort:
            raise ProviderBindingError(
                "kimi_effort_not_high",
                "an approved Kimi catalog entry does not resolve to pinned effort high",
            )
    return ProviderLaunchBundle(
        codex=_load_launch("codex_cli", document.get("codex_cli")),
        kimi=_load_launch("kimi_cli", document.get("kimi_cli")),
        claude=_load_launch("claude_cli", document.get("claude_cli")),
        kimi_config_path=config_path,
        binding_sha256=hashlib.sha256(raw).hexdigest(),
        kimi_config_sha256=hashlib.sha256(config_raw).hexdigest(),
        kimi_effective_effort=dict(kimi_effective_efforts).get(
            "kimi-code/k3", kimi_effective_efforts[0][1]
        ),
        kimi_effective_efforts=kimi_effective_efforts,
    )


def _catalog_effort(adapter: str, model: str) -> str:
    return _catalog_entry(adapter, model).default_effort


def _catalog_entry(adapter: str, model: str) -> ProviderSeatCatalogEntry:
    try:
        return APPROVED_SEAT_CATALOG[adapter][model]
    except KeyError:
        raise ProviderBindingError(
            "provider_seat_mismatch",
            "the adapter/model pair is outside the approved provider catalog",
        ) from None


def catalog_effort_schedule(
    adapter: str,
    model: str,
    corrective_effort: str | None = None,
) -> tuple[str, str]:
    """Resolve one declared schedule or raise a stable fail-closed refusal.

    Omission means no escalation. A present value must be in the verified
    vocabulary, match either the default or the catalog's ruled corrective
    position, remain within one rung, and be expressible per dispatch.
    Values are never normalized or clamped.
    """

    entry = _catalog_entry(adapter, model)
    corrective = (
        entry.default_effort if corrective_effort is None else corrective_effort
    )
    if type(corrective) is not str or corrective not in entry.effort_vocabulary:
        raise ProviderBindingError(
            "provider_effort_unknown",
            "the declared corrective effort is outside the model's verified vocabulary",
        )
    default_index = entry.effort_vocabulary.index(entry.default_effort)
    corrective_index = entry.effort_vocabulary.index(corrective)
    allowed_index = entry.effort_vocabulary.index(entry.corrective_effort)
    if corrective not in (entry.default_effort, entry.corrective_effort):
        if corrective_index == allowed_index + 1:
            raise ProviderBindingError(
                "provider_corrective_effort_not_allowed",
                "the declared corrective effort is excluded from the catalog position",
            )
        if corrective_index > default_index + 1:
            raise ProviderBindingError(
                "provider_corrective_effort_too_high",
                "the declared corrective effort is more than one verified rung above default",
            )
        raise ProviderBindingError(
            "provider_corrective_effort_not_allowed",
            "the declared corrective effort is not the catalog's allowed position",
        )
    if corrective_index > default_index + 1:
        raise ProviderBindingError(
            "provider_corrective_effort_too_high",
            "the declared corrective effort is more than one verified rung above default",
        )
    if corrective != entry.default_effort and not entry.corrective_dispatch_supported:
        raise ProviderBindingError(
            "provider_corrective_effort_unsupported",
            "the provider CLI cannot express the declared corrective effort per dispatch",
        )
    return entry.default_effort, corrective


def build_provider_runtime(
    bundle: ProviderLaunchBundle,
    declaration: LiveGateDeclaration,
    ambient: Mapping[str, str] | None = None,
) -> ProviderRuntimeBindings:
    """Bind the gate's finite environment names to current ambient values.

    On Windows, ``SYSTEMROOT`` is the sole platform extra name. Prompt 025's host
    precheck and the stub lane prove it is required for the Kimi executable's
    process initialization. Every provider child also receives the fixed
    bytecode-hygiene variable; no ambient ``PATH`` is inherited.
    """

    declared_seats = (
        (
            declaration.coder_adapter,
            declaration.coder_model,
            declaration.coder_corrective_effort,
        ),
        (
            declaration.reviewer_adapter,
            declaration.reviewer_model,
            declaration.reviewer_corrective_effort,
        ),
        (
            declaration.architect_adapter,
            declaration.architect_model,
            declaration.architect_corrective_effort,
        ),
    )
    kimi_effective = dict(bundle.kimi_effective_efforts)
    for adapter, model, corrective_effort in declared_seats:
        pinned_effort, _ = catalog_effort_schedule(
            adapter, model, corrective_effort
        )
        if adapter == "kimi_cli" and kimi_effective.get(model) != pinned_effort:
            raise ProviderBindingError(
                "kimi_effort_not_high",
                "the selected Kimi catalog entry does not resolve to pinned effort high",
            )

    source = os.environ if ambient is None else ambient
    names = list(declaration.credential_env_names)
    if sys.platform == "win32":
        names.extend(WINDOWS_PROCESS_ENV_NAMES)
    names.append(BYTECODE_HYGIENE_ENV[0])
    if len(names) != len(set(names)):
        raise ProviderBindingError(
            "provider_env_invalid", "the restricted child environment has duplicate names"
        )
    pairs = []
    for name in names:
        value = (
            BYTECODE_HYGIENE_ENV[1]
            if name == BYTECODE_HYGIENE_ENV[0]
            else source.get(name, "")
        )
        if not isinstance(value, str):
            raise ProviderBindingError(
                "provider_env_invalid", "an ambient child environment value is invalid"
            )
        if name in WINDOWS_PROCESS_ENV_NAMES and not value:
            raise ProviderBindingError(
                "provider_env_invalid", "a required Windows process variable is absent"
            )
        pairs.append((name, value))
    return ProviderRuntimeBindings(
        bundle=bundle,
        child_env=tuple(pairs),
        timeout_seconds=declaration.call_timeout_seconds,
        max_call_cost_usd=declaration.max_call_cost_usd,
    )


def provider_efforts() -> dict[str, str]:
    return {
        "architect": _catalog_effort("claude_cli", "claude-opus-5"),
        "coder": _catalog_effort("codex_cli", "gpt-5.6-sol"),
        "reviewer": _catalog_effort("kimi_cli", "kimi-code/k3"),
    }


def provider_effort_schedules(
    declaration: LiveGateDeclaration,
) -> dict[str, tuple[str, str]]:
    """Resolve the gate-fixed default/corrective schedule for each role."""

    return {
        role: catalog_effort_schedule(
            getattr(declaration, f"{role}_adapter"),
            getattr(declaration, f"{role}_model"),
            getattr(declaration, f"{role}_corrective_effort"),
        )
        for role in ("architect", "coder", "reviewer")
    }


def _provider_argv(
    launch: ProviderLaunch,
    request: AgentRunRequest,
    prompt: str,
    isolation_root: Path,
) -> tuple[str, ...]:
    default_effort, selected_effort = catalog_effort_schedule(
        launch.adapter, request.model, request.effort
    )
    if request.effort not in (default_effort, selected_effort):
        raise ProviderBindingError(
            "provider_seat_mismatch", "the request does not match the approved provider seat"
        )
    if launch.adapter == "codex_cli":
        sandbox = "workspace-write" if request.workspace_access == "workspace_write" else "read-only"
        return launch.argv_prefix + (
            "exec",
            "--model",
            request.model,
            "-c",
            f"model_reasoning_effort={request.effort}",
            "-c",
            "service_tier=default",
            "--sandbox",
            sandbox,
            "--ephemeral",
            "--skip-git-repo-check",
            "--json",
            "-",
        )
    if launch.adapter == "kimi_cli":
        skills_dir = isolation_root / "empty_skills"
        skills_dir.mkdir(parents=True)
        argv = launch.argv_prefix + (
            "--skills-dir",
            str(skills_dir),
            "--model",
            request.model,
            "--prompt",
            prompt,
            "--output-format",
            "stream-json",
        )
        command_line = subprocess.list2cmdline(argv)
        command_line_units = len(command_line.encode("utf-16-le")) // 2 + 1
        if command_line_units > _MAX_WINDOWS_COMMAND_LINE_UNITS:
            raise ProviderBindingError(
                "provider_prompt_argv_oversized",
                "the argv-only provider cannot receive this prompt within the Windows command-line bound",
            )
        return argv
    return launch.argv_prefix + (
        "-p",
        "--model",
        request.model,
        "--effort",
        request.effort,
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
    )


class _PromptStdinRunner:
    """Bind one exact captured prompt as stdin without changing argv evidence."""

    def __init__(self, runner: ProcessRunner, prompt_bytes: bytes) -> None:
        self._runner = runner
        self._prompt_bytes = prompt_bytes

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
        return self._runner.run(
            argv,
            cwd,
            env,
            timeout_seconds,
            stdout_path,
            stderr_path,
            max_stream_bytes=max_stream_bytes,
            stdin_bytes=self._prompt_bytes,
        )


class ProviderCliExecutor:
    """Concrete adapter executor over the accepted subprocess mechanism."""

    def __init__(
        self,
        runtime: ProviderRuntimeBindings,
        adapter: str,
        runner: ProcessRunner,
        log_root: Path,
    ) -> None:
        self._runtime = runtime
        self._launch = runtime.bundle.launch_for(adapter)
        self._runner = runner
        self._log_root = Path(log_root)

    def execute(self, request: AgentRunRequest) -> AgentRunResult:
        if request.adapter != self._launch.adapter:
            raise ProviderBindingError(
                "provider_seat_mismatch", "the executor received a different adapter"
            )
        if not Path(request.workspace).is_dir():
            raise SubprocessAgentFailure(
                "workspace_missing",
                "the request workspace is not an existing directory; "
                "nothing was captured or spawned",
            )
        authorized = request.max_cost_usd
        if (
            type(authorized) not in (int, float)
            or not math.isfinite(float(authorized))
            or float(authorized) < 0.0
            or float(authorized) > self._runtime.max_call_cost_usd
        ):
            raise ProviderBindingError(
                "provider_cost_authority_invalid",
                "the request exceeds the gate's per-call cost authority",
            )
        try:
            prompt_bytes = Path(request.prompt_path).read_bytes()
            prompt = prompt_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            raise ProviderBindingError(
                "provider_prompt_invalid", "the exact provider prompt is unavailable"
            ) from None
        if hashlib.sha256(prompt_bytes).hexdigest() != request.prompt_sha256:
            raise ProviderBindingError(
                "provider_prompt_changed", "the provider prompt hash changed before dispatch"
            )
        scope = hashlib.sha256(
            (
                request.role.value
                + "\0"
                + request.attempt_id
                + "\0"
                + str(request.prompt_path)
            ).encode("utf-8")
        ).hexdigest()[:16]
        capture_root = self._log_root / request.role.value / scope
        prompt_capture = capture_root / "prompt.md"
        if prompt_capture.exists():
            raise SubprocessAgentFailure(
                "capture_conflict", "an exact prompt capture already exists"
            )
        capture_root.mkdir(parents=True, exist_ok=True)
        prompt_capture.write_bytes(prompt_bytes)
        argv = _provider_argv(
            self._launch,
            request,
            prompt,
            capture_root / "isolated_cli",
        )
        spec = AgentCommandSpec(
            argv=argv,
            command_id=self._launch.adapter.replace("_", "-"),
            env=self._runtime.child_env,
            timeout_seconds=self._runtime.timeout_seconds,
            prompt_capture_name=prompt_capture.name,
        )
        runner = (
            _PromptStdinRunner(self._runner, prompt_bytes)
            if self._launch.adapter in ("codex_cli", "claude_cli")
            else self._runner
        )
        try:
            result = SubprocessAgentExecutor(
                spec, runner, capture_root
            ).execute(request)
        except SubprocessAgentFailure:
            # ``runner_failure`` is written to the bounded event log before
            # the generic executor raises.  Preserve that captured spawn/run
            # failure as a normal closed subscription result so the
            # supervisor journals the contractual zero-dollar fact.  Refuse
            # pre-spawn failures that produced no durable observation.
            event_log = (
                capture_root
                / f"{request.run_id}_{request.attempt_id}_events.jsonl"
            )
            if not event_log.is_file():
                raise
            result = AgentRunResult(
                status="failed",
                event_log_path=event_log,
                changed_files=(),
                produced_artifacts=(),
                exit_reason="agent_runner_failure",
                tokens_in=None,
                tokens_out=None,
                cost_usd=None,
            )
        # All approved seats are subscription CLIs.  The raw CLI usage text
        # remains verbatim in stdout/stderr captures; no transport text is
        # interpreted into token, cost, verdict, or control facts.
        return replace(result, cost_usd=0.0)
