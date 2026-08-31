# frutlups-drive

`frutlups-drive` supervises artifact-first autonomous coding loops. Released
frutlups determines the next governed planning step; frutlups-drive dispatches
that work to configured model seats, independently verifies the results,
enforces budgets and stop conditions, and records an append-only execution
journal. Humans retain authority over gates, commits, publication, and every
resume after a governed stop.

The 0.1.x series is an early, deliberately narrow release. Its contracts and
fail-closed behavior have been exercised in governed campaigns, but operators
should expect explicit setup and should inspect run evidence before widening
autonomy.

## Requirements

- Python 3.11 or newer;
- a released frutlups installation, exposed through an explicit local binding;
- the provider CLIs required by the model seats in your policy, installed and
  authenticated separately.

llloom is optional and needs its own released installation and binding when a
project enables memory.

## Install

From a source checkout:

```console
python -m pip install .
```

From a built wheel:

```console
python -m pip install ./frutlups_drive-0.1.0-py3-none-any.whl
```

Confirm the installed version with `python -m frutlups_drive --version`.

## Commands

```console
python -m frutlups_drive plan   <project> [--dry-run]
python -m frutlups_drive run    <project> --until slice_complete|roadmap_complete
python -m frutlups_drive resume <project> <run_id> [--until slice_complete|roadmap_complete]
python -m frutlups_drive stop   <project>
python -m frutlups_drive report <project> <run_id> [--json]
```

`plan` and `report` are read-only. `run`, `resume`, and `stop` require the
project's policy and applicable local authority declarations.

## frutlups 0.2 seam consumer qualification

The checkout-local `frutlups_drive.seam_consumer` surface provides the typed
`FrutlupsSeamConsumer`, response admission, and corrective-proposal builder for
the frozen frutlups 0.2 seam. After binding `$projectPython` and `$seamPython`,
run its complete offline qualification lane from the repository root. In a
development checkout, use:

```powershell
& $projectPython 08_pkg/scripts/verify_frutlups_seam_consumer.py --seam-python $seamPython
```

In a released front checkout, use:

```powershell
& $projectPython scripts/verify_frutlups_seam_consumer.py --seam-python $seamPython
```

The command pins the producer identity and fixture manifest, runs all 22
consumer tests plus the two-test 73-row live replay, and emits one
`frutlups-seam-consumer-proof/v1` summary. It dispatches no provider seat and
writes only inside test temporary roots. This qualified surface is parallel to
the operational loop: planning and status observation run on released
frutlups 0.2.1 (adopted 2026-08-31; the observation contract is key-identical
with the prior 0.2.0 and 0.1.8 baselines).

## Minimal quickstart

1. Prepare a committed driven project with the released frutlups two-roadmap
   contract: the active roadmap owns milestone inventory and status, while its
   development-roadmap sibling owns each milestone's `Slices:` breakdown. Keep
   their milestone and slice identities aligned; a single combined roadmap does
   not provide the planning frontier.
2. Add the project policy, explicit frutlups and provider bindings, and any live
   execution gate required by the seats you selected.
3. Inspect the resolved identities and authority without dispatching work:

   ```console
   python -m frutlups_drive plan <project> --dry-run
   ```

4. Run the read-only plan, then begin with one governed slice:

   ```console
   python -m frutlups_drive plan <project>
   python -m frutlups_drive run <project> --until slice_complete
   ```

## Governed oracle exclusions

The pass-boundary oracle keeps its 16 MiB per-file content bound and does not
infer exclusions from `.gitignore`. A project with intentional large local
outputs may declare one committed, project-local manifest in its policy:

```toml
oracle_exclusion_manifest = "05_governance/oracle_exclusions.json"
```

The referenced UTF-8 JSON file has this exact version-1 shape:

```json
{
  "contract_version": 1,
  "exact_paths": ["reports/large-local-result.bin"],
  "top_level_prefixes": ["build/"]
}
```

Exact paths name files. Prefixes name top-level directories and must end in
`/`. The manifest itself is frozen as an ordinary artifact. Excluded files are
never silent: exact files receive individual `type: "excluded"` boundary
members, while each excluded top-level tree receives one aggregate excluded
member; both carry streamed byte-size and SHA-256 evidence. Missing,
malformed, link-like, self-excluding, or over-bound manifests refuse before
`pass_boundary.json` is written. With no declaration, boundary and oracle
behavior remain unchanged. An oversize refusal lists the offending paths and
sizes plus candidate top-level prefixes; the drive never auto-excludes them.

## Dispatch environment, ceilings, and capture evidence

Optional M009 dispatch controls fail closed when present and leave prior
behavior unchanged when omitted. `runtime_environment_bindings` is an array of
approved non-secret name/literal pairs; external live gates must declare the
same pairs. Their names and value SHA-256 hashes enter the run manifest, while
credential variables remain names-only and are never hashed.

```toml
runtime_environment_bindings = [
  {name = "JAVA_TOOL_OPTIONS", value = "-Djava.io.tmpdir=.tmp"},
]

# Default off. A live gate must declare the same values.
architect_corrective_turn_enabled = true
max_architect_corrective_turns_per_run = 1

[dispatch]
role_call_ceiling_seconds = {coder = 2400, reviewer = 1200}
slice_call_ceiling_overrides = [
  {slice_id = "M009-S03", ceiling_seconds = 7200},
]
scientific_subprocess_budget_seconds = 5400
capture_truncation_disposition = "invalidate" # or "tolerate"

[reporting]
currency = "EUR"
external_provider_ceilings = [
  {provider = "codex_cli", ceiling = 100},
]
```

Slice ceilings override role ceilings; otherwise the existing global call
ceiling applies. The scientific subprocess budget is separate from model-call
and artifact-watch time. External currency ceilings are recorded and reported
only—no enforcement path consumes them.

The architect corrective turn is limited to typed prompt-contract,
prompt-adoption, and rework-declaration-mapping stops. Each selected stop gets
at most one architect dispatch and one staged JSON proposal. The drive applies
only the exact failing prompt or rework-declaration path after structural and
released-frutlups dry-run checks; it refuses sidecars, governance-adjacent
targets, and accepted history. A refused proposal remains run-store evidence
and the original governed stop remains in force. This surface is disabled
unless policy and live gate declare identical enablement and per-run cap facts.

The stream admission ceiling remains 1,048,576 bytes per stream. On overflow,
the attempt stores a `capture_spool/summary.json` plus fixed 65,536-byte maximum
head and tail files for stdout and stderr. The summary records per-stream total
bytes and newline-delimited event counts and the journal marks
`truncated: true`. `invalidate` retains the fail-closed attempt disposition;
`tolerate` may continue only after a clean process exit.

Read [the operator's manual](docs/operators_manual.md) before authorizing live
model seats. It covers the roadmap pair, policy, bindings, live gate, budgets,
monitoring, stops, and recovery in full.
