# The frutlups-drive Operator's Manual

A human-friendly guide to opening, initializing, and executing a fully
autonomous, milestone-structured development project with template v3,
frutlups, and frutlups-drive.

Current through version 6 (M010) and the 2026-08-31 frutlups adoption:
frutlups-drive is published as 0.6.0 (tag `v0.6.0`), with released
frutlups 0.2.1 as the operational planning baseline, released llloom
0.1.2, and template v3.1.0.
Sections first written at version 1 remain
accurate unless a later-version delta below says otherwise. Where a
step needs a governance decision, the manual says so explicitly:
autonomy in this system is always bounded by something a human ruled.

---

## 1. The three tools, in one breath each

- **Template v3** is the *project shape*: a repository layout where the
  unit of progress is an artifact moving between reviewable states — a
  roadmap of milestones, numbered prompts, reviews, verdicts, and one
  short live state file (`PROJECT_STATE.md`).
- **frutlups** is the *planning interpreter*: a separate CLI that reads
  the roadmap, reviews, and verdicts, and answers exactly one question —
  "what is the next governed step?" — plus four governed artifact verbs
  (declare bounded rework, make a coding prompt, make a review prompt,
  record a verdict). It never executes anything.
- **frutlups-drive** is the *supervisor*: it reads frutlups' answer,
  dispatches real model seats (architect, coder, reviewer) as sandboxed
  subprocesses, verifies results, enforces budgets and fences, journals
  everything, and stops fail-closed the moment anything is off-contract.

The division of labor is strict: frutlups plans, drive executes, humans
rule. Drive owns the sole execution journal; frutlups owns the verbs
that write loop artifacts; you own every commit, every gate, and every
resume after a stop.

## 2. One-time machine setup

Do this once per operator machine, per the exact interpreter and tool
bindings recorded in `05_governance/current/execution_environment.md`:

1. **Three isolated Python environments** (never shared, never on
   PATH): one for frutlups-drive itself, one holding released frutlups
   (installed from its wheel), and — if you use memory — one holding
   released llloom. Drive talks to the other two only through declared
   subprocess bindings; it never imports them.
2. **The provider CLIs**, installed and logged in with subscription
   auth: Codex CLI, Kimi CLI, and Claude Code. Verify each login
   before any run (Codex `login status`; Kimi binding load; Claude
   login surface). Drive never sees or stores credentials — it launches
   the CLIs with a minimal fixed environment and the CLIs use their own
   auth stores.
3. **Local bindings** in the driven project's ignored local-state
   directory (never committed): a provider binding file declaring the
   absolute executable paths and argv prefixes for the three seat CLIs,
   and — with memory on — an llloom binding declaring the released
   llloom executable and its version identity. These are machine-local
   by design; the run manifest pins their hashes so evidence records
   exactly what ran. Write every binding TOML as UTF-8 **without a BOM**.
   A BOM written by a Windows PowerShell encoding default makes the
   binding malformed, and the loader correctly refuses it fail-closed.

## 3. Opening a project (the template)

A driven project is a git repository in template-v3 shape. The minimum
working skeleton, all committed as a pristine baseline before any run:

- `PROJECT_STATE.md` — short, current, the only live state file.
- A `CLAUDE.md` / `README.md` orienting any agent or human who opens it.
- The canonical **active/development roadmap pair** in the experiments
  workspace — see section 4. The active file carries milestone identity
  and status; the development file carries each milestone's `Slices:`
  bullet breakdown. The active file is the guarded runtime-architect
  reconciliation target. Both files must expose the identical
  milestone/slice spine.
- A governance workspace with a reviews directory (reports, verdicts,
  and self-reports land here through governed verbs and seat turns).
- Prompt templates (coding prompt, review prompt, self-report) under
  the prompts workspace — frutlups fills these when generating the
  loop's prompt artifacts.
- The implementation workspace (source and tests the coder seat will
  touch) with its verification command declared in the roadmap.
- The frutlups layout declaration (frutlups.layout.yaml at the project
  root) and the drive policy file (section 5).
- A stay-in-workspace instruction in every seat prompt. The drive's
  dispatch envelope tells every seat, every turn, that confinement covers
  files, processes, and system state. Seats never enumerate or manage host
  processes, launch only declared-verification children and let them finish,
  and make no host-level install, environment, configuration, or settings
  change. A suspected runaway process, busy port, locked file, or other host
  problem is reported in the self-report, never remediated by the seat.

Rules of thumb learned the hard way in v1: commit the baseline before
any dispatch (drive diffs everything against it); remember git cannot
represent empty directories, so seed keep-files where a directory must
exist; and never hand-edit anything under the run store.

Two more, learned in the first paired live campaign: **reconcile every
shipped scaffold artifact against the policy you declare** — the
canonical instance is the reviews INDEX: current template pins ship it
header-only, but if you project from an older pin, delete the
template's `M000` placeholder row before declaring the no-ledger index
mode, or the oracle's row tripwire fires at every pass boundary and
the anomalous row's milestone key can end up in a holistic finding
list. And **re-verify tool identities at init, never trust them**: the
three isolated environments, the BOM-free bindings, and the provider
CLI logins.

For an ordinary production or pilot project, `no-ledger` means exactly
that: launch with the reviews INDEX header-only and do not plant test
rows. A deliberate oracle canary belongs only in an explicitly ruled,
disposable qualification campaign and must follow the same-run
reopenability procedure in section 4.

## 4. Writing the roadmap (milestones made of slices)

Released frutlups uses two plain-Markdown roadmap files as one strict,
parseable contract. The **active roadmap** carries the milestone inventory
and statuses. The sibling **development roadmap** carries the `Slices:`
bullet breakdown that lets frutlups derive the slice frontier. Do not collapse
the pair into one file: released frutlups (0.1.4 through 0.2.1) does not derive
a planning frontier from that single-file shape. Since frutlups
0.1.3, generated coding and review prompts are project-derived: the
frontier milestone's authored Objective, Non-goals, and Verification
fields flow into the prompt, identities are project-neutral, and the
coder review-prompt permission follows the layout policy.

Across that pair, each milestone is a heading with labeled fields:

- **Status** (active/completed) and **Disposition** — protected: only
  humans (or the guarded reconciliation path) change these.
- **Slices** — the milestone's numbered units of work (M001-S01, …).
  This top-level list is protected structure.
- The **specification fields** — Implementation package, Objective,
  Expected artifacts, Active workspaces, Non-goals,
  Verification/evidence, Review strictness, Likely coding prompt, Done
  when, Opening gates. These are the *editable surface*: the only
  region the runtime architect's reconciliation may change, one
  milestone at a time, under the N04 guarded writer.

A milestone is "conformed by various slices": frutlups walks the
active milestone slice by slice — for each slice it asks for a coding
prompt, a coder turn, verification, a review prompt, a reviewer turn,
and a verdict; repair rounds happen inside caps. When planning reports
the frontier recorded, the drive journals the slice complete and
continues (or stops, per your boundary). When every slice has a passing
verdict, released frutlups automatically advances the frontier across
subsequent planned milestones as verdicts record; no human milestone-status
edit is needed at those boundaries. At roadmap completion the drive freezes
the pass boundary and runs holistic reviewer passes until two consecutive
clean passes close the run.

When a holistic pass reports findings, drive keeps that exact bounded
slice worklist in its journal. Since drive 0.4.0 the finding ids are
first validated against the run's own accepted-slice journal history:
the valid subset proceeds in reported order, every unmappable id is
journaled as a typed `holistic_finding_unmappable` fact (never
silently dropped), and a worklist with findings but no reopenable
slice id stops governed as `holistic_findings_unmappable` with the
ids preserved in the escalation for you to adjudicate. At the
otherwise terminal `complete` / `no_frontier` observation, drive
invokes released frutlups' `declare-rework` verb with the journaled
`holistic_pass_NNN` identity and exactly the still-missing slices. The
transaction is the same governed shape as the other writes: dry run,
contained target validation under
`05_governance/rework_declarations/`, append-only real write, effect
fence, and fresh status. Drive never reads or parses the declaration
file. Frutlups canonicalizes multiple slices to roadmap order and gives
each one a disjoint coding/review prompt window.

The reopened slice then follows the ordinary loop: fresh coding prompt,
coder self-report and verification, fresh review prompt, independent
review, and a passing `record-verdict` transaction. Only that accepted
fresh chain drains the slice from the worklist; an attempted rework or
historical verdict does not. A `needs_work` verdict produces another
fresh corrective prompt and review inside the existing caps.

### Qualification canaries must be reopenable in the same run

The oracle detects evidence; it does not make a finding routable. A
roadmap-valid slice id is therefore not enough for a deliberate holistic
canary. Drive can reopen that id only after **the current run** has accepted
the slice into its own journal history. A verdict accepted while preparing
the fixture, or by a predecessor or earlier run, is historical evidence and
does not satisfy this rule.

Use this procedure whenever an explicitly authorized qualification campaign
plants a no-ledger canary such as one controlled INDEX data row:

1. Use a fresh disposable fixture. Keep ordinary production and pilot
   projects canary-free and their no-ledger INDEX header-only.
2. Attribute the canary to an exact slice that is **pending at launch**. Do
   not pre-accept that slice during fixture preparation and do not target a
   slice accepted by an earlier run.
3. Start the run at or before that slice, and set the run boundary and slice
   budget so the same run must implement and accept it before the holistic
   pass boundary.
4. Before launch, verify all four facts: planning still reports the intended
   frontier; the oracle emits exactly the intended canary observation with
   the exact target id; the target is absent from pre-run accepted history;
   and the target lies inside this run's planned slice range. Planning plus
   oracle validation alone is insufficient because neither proves
   same-run reopenability.
5. At the non-clean holistic pass, require the finding to carry that exact
   slice id. The normal rework loop must remove the planted condition,
   re-accept the slice, and then produce the configured clean passes.

If the canary target is absent from the current run's accepted history, the
resulting `holistic_findings_unmappable` stop is correct fail-closed behavior.
Do not edit a verdict, the accepted history, or the run store, and do not
treat this setup error as an ordinary resume. Preserve the stopped run as
evidence, correct the campaign design, and—under fresh owner authority—launch
a fresh fixture/run in which the target begins pending.

Two review-file conventions, enforced end to end since frutlups 0.1.8
and the matching template pin, exist because a live campaign died
without them: **a review report file carries exactly one `## Verdict`
section** — released frutlups refuses a multi-verdict file
("refusing to resolve an ambiguous verdict") instead of silently
taking the first — and **corrective rounds are round-qualified**:
generated corrective prompts declare `_round_{NNN}` zero-padded paths
(a literal unpadded `_round2` is misread as round 1), seats write
exactly the declared paths, and the artifact watch honestly refuses
anything else. The
outside-the-worklist guard still stops any unexpected frontier. If the
process crashes after declaration, resume reconciles the durable pending
witness or observes the already-ready worklist and continues without a
second declaration. A new non-clean holistic pass uses its new pass
number and therefore a new append-only declaration.

Version 5 makes that lifecycle explicit across runs. Every terminal
non-repair coder round records a typed `ladder_event`: `product_finding`
counts toward the same-invariant round-three stop; `transport`,
`environment`, `path_contract`, and `operator_kill_switch` are visible but
excluded from product recurrence. If immutable owner-note authority changes,
the stop is `fresh_run_required`, not an ordinary resume. Its escalation gives
one exact `run ... --predecessor-run <stopped_run>` command. The successor
manifest and `run_created` event record the predecessor automatically and
inherit its campaign id when the new launch does not declare one. Ordinary
`resume` still continues the same run and therefore creates no fictitious
cross-run edge.

If a slice is deliberately under-specified, planning reports
needs-specification and the **architect seat** gets exactly one guarded
turn to propose a completed roadmap — the N04 writer publishes it only
if every protected field is byte-identical and only specification text
changed. (v1 lesson, now baked into the generated prompt: the current
structure is authoritative even where it looks odd; proposals that
"fix" layout are refused.)

## 5. Declaring the policy — seats, budgets, boundaries

The drive policy lives at the driven project root in
frutlups_drive.toml. A current-shape policy (the v1 closure campaign's
real declarations plus the version 3-5 additions), annotated:

```toml
schema_version = "frutlups_drive_policy_v1"
index_mode = "no-ledger"       # or "human-ledger" (the default when absent):
                               # in no-ledger, NOBODY keeps the reviews INDEX -
                               # it stays at its shipped header-only state, the
                               # manifest is the routing truth, and the oracle
                               # treats any data row that appears as a tamper
                               # tripwire. Declare no-ledger for fully
                               # autonomous projects, at project init.
campaign_id = "v5-evaluation" # optional; run --campaign-id may declare it
oracle_exclusion_manifest = "05_governance/oracle_exclusions.json" # optional, committed
runtime_environment_bindings = [
  {name = "JAVA_TOOL_OPTIONS", value = "-Djava.io.tmpdir=.tmp"}
]                              # non-secret literals only; gate must match
architect_corrective_turn_enabled = false # default-off; gate must match
max_architect_corrective_turns_per_run = 1

[target]
stop_at = "roadmap_complete"   # or "slice_complete" for one-slice runs
max_slices = 6                 # hard cap per run
max_passes = 3                 # holistic pass budget

[roles.architect]
adapter = "claude_cli"         # which CLI drives this seat
model = "claude-opus-5"        # exact pinned identifier, never an alias
workspace_access = "workspace_write"
corrective_effort = "xhigh"    # optional: effort for corrective rounds, ONE
                               # rung above the seat default, from the
                               # fail-closed per-provider catalog fixed at
                               # init (top tiers stay excluded)

[roles.coder]
adapter = "codex_cli"
model = "gpt-5.6-sol"
workspace_access = "workspace_write"
corrective_effort = "high"     # coder default is medium; correctives run high

[roles.reviewer]
adapter = "kimi_cli"
model = "kimi-code/k3"
workspace_access = "read_only" # reviewers publish reports, not code
                               # no corrective_effort: this CLI cannot switch
                               # effort per dispatch, so the seat declares
                               # nothing - fail-closed, never a crash

[roles.shadow_reviewer]
enabled = false                # off in v1 closure; proven inert

[autonomy]
auto_continue_past_frontier_recorded = true  # roll slice to slice
pass_boundary = "two_clean"    # completion needs 2 clean holistic passes

[limits]
max_coder_attempts_per_slice = 3
max_report_repairs = 2
max_reconciliations_without_progress = 2
max_total_cost_usd = 15.0
max_wall_clock_minutes = 360
max_consecutive_provider_failures = 3
max_consecutive_no_progress = 3
provider_backoff_seconds = [1.0, 2.0, 4.0]
watch_poll_seconds = 0.25
max_run_store_bytes = 67108864
max_retained_runs = 25

[git]
worktree_per_slice = false

[frutlups]
provider = "frutlups_cli"
timeout_seconds = 120

[dispatch]
role_call_ceiling_seconds = {coder = 900, reviewer = 600}
slice_call_ceiling_overrides = [
  {slice_id = "M009-S05", ceiling_seconds = 1200}
]
scientific_subprocess_budget_seconds = 1800
capture_truncation_disposition = "invalidate" # or explicit "tolerate"

[reporting]
currency = "EUR"
external_provider_ceilings = [
  {provider = "codex_cli", ceiling = 100.0}
]
```

The version 5 declarations are opt-in. With no campaign declaration, the
manifest and start event keep their historical shape. A campaign may instead
be supplied by `run --campaign-id`; conflicting policy and launch values
refuse. The exclusion manifest is strict bounded JSON with exactly
`contract_version`, `exact_paths`, and `top_level_prefixes`. Excluded files or
top-level trees remain named, sized, and hashed in the frozen pass boundary,
but the oracle does not read their content. Nothing is inferred from
`.gitignore`; malformed, link-like, self-excluding, protected, oversized, or
non-canonical declarations stop before the boundary freezes. Pre-freeze size
and member-limit refusals name the offending paths and candidate declarations.

Runtime bindings are non-secret name/literal pairs. The manifest retains only
the name and literal SHA-256, never the literal or a credential value/hash.
Model-call ceilings resolve slice, then role, then the existing global limit;
the scientific-subprocess budget is independent. Policy and live gate must
declare identical runtime bindings and model-call ceilings. Capture remains
bounded at 1 MiB per stream: overflow writes a bounded `capture_spool` summary
plus fixed 65,536-byte head/tail members. `invalidate` stops the attempt;
`tolerate` may continue only after a real zero exit. Reporting currency and
provider ceilings are facts, not enforcement. Measured, subscription-prepaid,
and unknown cost knowledge stay distinct; prepaid and unknown attempts carry
`cost_usd = null`, never a misleading zero.

**Per-milestone operation** is a policy choice, not a special mode: set
`stop_at = "slice_complete"` to get one governed slice per run (you
commit and inspect between slices), or `roadmap_complete` with
`two_clean` to let a whole roadmap of milestones run to a fully
reviewed completion in one authorized session. Milestone boundaries in
a multi-milestone roadmap are natural commit points for *you* — drive
never commits.

## 6. Choosing which model sits in which seat

Seats are per-project and per-campaign. To choose:

1. **Pick an adapter per role** in the policy file. v1 ships three
   approved external adapters, each pinned to one exact model:
   `claude_cli` → claude-opus-5, `codex_cli` → gpt-5.6-sol,
   `kimi_cli` → kimi-code/k3 (efforts high/medium/high respectively).
   The pinning is deliberate v1 conservatism — an unapproved
   adapter/model pair fails closed at dispatch. `manual` and `mock`
   local adapters exist for human-in-the-loop and offline runs.
2. **Respect the seat rules**: coder and reviewer must differ, and the
   architect must differ from both — the gate refuses identical seats.
3. **Re-declare the live gate** (section 7) with the same three seats,
   byte-for-byte. Policy and gate must agree exactly or nothing
   dispatches.
4. Consult `06_infra/provider_model_registry.json` and
   `06_infra/provider_reference.md` for the verified CLI invocation
   surfaces, effort semantics, and model catalog before changing
   anything.

Widening the approved model set (say, a different Kimi or GPT variant)
is a small, gate-guarded product change — a natural early version 2
item, not a config edit.

## 7. The live gate — your authorization to spend

Nothing external dispatches without a **ready** committed gate:
`06_infra/live_validation_gate.md` declares, in fenced TOML, the
approval state, an approval reference (the owner note that ruled it),
all three seats, dollar and time limits, credential environment names,
rollback and kill-switch statements, and stop conditions. Since the
version 3 corrective-effort schedule, the gate also declares the
corrective efforts, and they must match the policy's declarations
byte-identically (the extended gate equality). The drive
assesses it at launch; any unknown field, missing approval, seat
mismatch with the policy, or malformed value refuses with
`live_authority_missing` and nothing is created, spawned, or spent.

Version 5 extends that equality again: approved non-secret runtime bindings,
role/slice model-call ceilings, architect-corrective enablement and cap, and
reporting currency/provider ceilings must match their policy halves exactly.
The scientific-subprocess budget has no gate half because it can only narrow
offline verification. These declarations add no provider dispatch to `plan`
or `report`.

The gate location is resolved from the drive package: three package
parents above the `frutlups_drive.cli` module, then
`06_infra/live_validation_gate.md`. In a source checkout this is the
repository root; for a wheel installed in a virtual environment it is
the environment root. Before `run`, stage the approved gate declaration
at that resolved location. If the file is absent, admission refuses
`live_authority_missing (gate_file_missing)` before a run store is
created or anything is spawned or spent.

Treat the gate as the contract it is: one owner ruling, one campaign
scope. Staging means copying that approved declaration byte-for-byte,
not writing a deployment-specific replacement. Never re-declare it just
because the deployment form or install root changed. When seats, limits,
or scope change, obtain a new owner ruling and approve a new declaration.
(Every v1 campaign consumed its authority exactly this way — notes 017
through 021.)

## 8. Memory (optional, llloom)

If the driven project declares memory mode llloom (read through the
released frutlups memory-mode contract — drive never guesses):

- Initialize a memory root inside the project with released llloom's
  init, and declare the llloom binding in local state.
- During runs, drive performs **observer-only** hooks: liveness
  preflights, bounded context reads appended to prompts (never into
  routing or control flow), and boundary update submissions through
  llloom's released submit-update verb — at each recorded frontier and
  at the completion boundary. Drive itself never writes a byte into
  the memory root; all proposals flow through the released inbox.
- Every hook is refusal-isolated: a sick memory system produces
  journaled refusal facts, never a changed loop decision. Mode-none and
  empty-llloom runs are proven transition-identical.

For a POPULATED root (the recall path, proven live in the AL-002
paired campaign):

- Author the root before launch through llloom's own released verbs
  from a seed manifest of locator-anchored claims. Two traps learned
  live: pages need their commentary pairs (a page without them crashes
  `seed apply` at render and leaves a dead-owner lock — recover with
  llloom's own `unlock --dead-owner`, never by hand-editing the root),
  and excerpt equality is byte-strict.
- Boundary update submissions are proposals into the llloom review
  queue; nothing is ever applied to the root by a machine — applying
  updates remains a human or architect decision.
- Set expectations honestly: measured live against a memoryless twin,
  populated memory bought judgment quality (an earlier, sharper
  holistic catch), not per-slice speed, and well-behaved seats verify
  recalled facts against the repository before acting on them.

## 9. Running

All commands run with the drive environment's interpreter, from
anywhere, against the driven project's path (always pass it absolute
or let the CLI resolve it — it does, since v1's final fix):

```
python -m frutlups_drive plan   <project> [--dry-run]
python -m frutlups_drive run    <project> --until slice_complete|roadmap_complete [--campaign-id ID] [--predecessor-run RUN]
python -m frutlups_drive resume <project> <run_id> [--until ...]
python -m frutlups_drive stop   <project>
python -m frutlups_drive report <project> <run_id> [--json]
python -m frutlups_drive report <project> --campaign <campaign_id> [--json]
python -m frutlups_drive report <project> --all [--json]
```

Every `run` and `resume` requires the project-local
`.frutlups_drive_mock/script.json`, including runs whose seats are live.
The name is historical: in a live project the script supplies
the immutable per-invocation verification plan, while its `planstate`,
`verbs`, and `executors` entries remain empty. If the file is absent,
admission refuses `mock_script_missing` before any run effect.

A minimal live-mode script uses the drive interpreter for the product
test lane, the project root as its working directory, and a bounded
timeout:

```json
{
  "planstate": [],
  "verbs": {},
  "executors": {},
  "verification": {
    "commands": [
      {
        "argv": ["{python}", "-m", "unittest", "discover", "-s", "08_pkg/tests"],
        "cwd": ".",
        "timeout_seconds": 120
      }
    ],
    "declared_regenerated": []
  }
}
```

The script is mandatory; only the mock rehearsal is optional.

A sensible first session on a fresh project:

1. `plan` — read-only and explicit about its evidence. In a
   `frutlups_cli` project it executes the same strict released-frutlups
   `status` observation used by `run`, then reports outcome, frontier, and
   step and says that it checked `planning_frontier` plus `loop_resume`.
   Captures are disposable and outside the project; no provider seat is
   dispatched. In a mock-configured project it instead says that it planned
   against `.frutlups_drive_mock/script.json`. Fix the roadmap or script until
   the stated observation looks right.
2. If this is an explicitly ruled qualification campaign with a deliberate
   canary, complete section 4's same-run reopenability checks before any live
   dispatch. Otherwise confirm that a no-ledger reviews INDEX is header-only.
3. A **mock rehearsal** if you want one: point the policy at mock seats
   and run offline; the loop shape exercises without any spend.
4. `run <project> --until slice_complete` — the first real slice, then
   look at everything (section 10) before granting more.
5. Then `run ... --until roadmap_complete` for full autonomous
   milestone execution under your gate's budgets.

Use one stable campaign id for related launches. A policy declaration or
`run --campaign-id` records it in the manifest and `run_created` event. The
fresh command printed by `fresh_run_required` names `--predecessor-run`; if
you execute it without a new campaign declaration, the successor inherits the
predecessor's campaign. Do not attach an unrelated run merely to make a report
look continuous.

The run either reaches its boundary (`complete` after two clean
holistic passes, or `slice_complete`) or **stops governed** — with a
stop reason, a complete escalation document, and untouched evidence.

## 10. Monitoring — what to watch and how

Everything observable lives in the run store, inside the project's
`.frutlups_drive` directory (ignored, never committed):

- **The journal** — the run's events.jsonl file, one JSON event per
  line: dispatches, collections, verifications, verbs, memory facts,
  backoffs, boundaries, stops. Tail it live; it is the single source
  of execution truth, and it is append-only.
- **Escalations** — the run's escalations directory: when a run stops,
  one Markdown document tells you the stop reason, the planning
  snapshot, the attempts summary, safe options, and the exact resume
  command. Read this first, always.
- **Captures** — the run's adapter-logs directory, per role: every seat
  turn keeps a four-file record (prompt, stdout, stderr, agent events).
  This is your transcript audit surface — v1 practice is to actually
  read them, especially any turn that touched something unexpected.
- **Telemetry** — `report <project> <run_id>` renders one run;
  `--campaign <id>` aggregates the matching lifecycles and `--all`
  explicitly aggregates every retained run. Campaign output includes each
  run's boundary/stop, lineage chains, dispatch counts by role/model/effort,
  attempt-duration sums, stop taxonomy, stop-to-successor recovery time, and
  honest cost-knowledge facts. Historical runs without the optional fields
  remain unlinked/uncampaigned, never invented. Text and `--json` carry the
  same data; the error list must be empty on a healthy store.
- **Money and time** — every attempt journals one cost/usage fact.
  Measured usage carries a numeric cost; subscription CLIs carry
  `subscription_prepaid` plus null cost; unavailable facts carry `unknown`
  plus null cost. Declared external ceilings are reported beside tokens and
  measured totals, with an explicit statement when prepaid, unknown, or
  currency-mismatched usage makes comparison unauditable. Keep your own
  append-only cost log per campaign (`05_governance/cost_log.md` is the v1
  pattern) when provider-side invoices are authoritative.
- **The driven repo itself** — `git status` in the project shows
  exactly what the loop produced: implementations, self-reports,
  generated prompts, review reports, verdict records, roadmap edits.
  Anything else appearing there is a finding.

**Launch practice for long autonomous runs** (the campaign pattern):
launch `run` detached from any interactive session that might kill it,
and supervise by tailing the journal — never by attaching to the
process.

**Journal surfaces added in versions 3-4**, so you recognize them live:

- `watch_timeout` + an effort-escalated redispatch: a seat that
  "completes" without delivering its declared artifacts is honestly
  waited out for the full watch ceiling, journaled, and re-dispatched
  once at the corrective effort — self-recovery, not a stop. Every
  corrective dispatch journals its `effort`.
- A `ladder_round3` escalation now carries the mandatory three-exit
  reassessment fork (defect plane / prompt-contract plane /
  evidence-documentation plane) — classify before any resume.
- `holistic_finding_unmappable` events and the
  `holistic_findings_unmappable` stop (section 4): unroutable holistic
  finding ids, journaled and escalated instead of poisoning the rework
  declaration.
- The no-ledger pass-boundary oracle bundle is QUIET: zero observations is
  the healthy baseline for every ordinary project. The observation class you
  will most likely ever see is the `ledger_row_in_no_ledger_project` tripwire
  — a data row in an INDEX nobody should be keeping. The only planned
  exception is an explicitly ruled qualification canary prepared under
  section 4; it must be attributed to a slice accepted by that same run so
  the finding can enter governed rework.

**Operator surfaces added in version 5**:

- S01's `ladder_event` records the closed failure class, recurrence key, and
  whether it counted. `fresh_run_required` names the controlling-note hash,
  predecessor, and exact fresh launch; ordinary resume is refused.
- S02's exclusion members are visibly typed in `pass_boundary.json`.
  `path_violation` escalations list sorted `(code, path)` facts plus the exact
  expected artifacts or allowed prefixes; they do not broaden the fence.
- S03 dispatch events name the effective model-call ceiling and its source;
  verification timeouts name the independent scientific ceiling and measured
  duration. On stream overflow, read `capture_spool/summary.json` and its
  bounded head/tail members rather than assuming the missing middle. Cost
  knowledge and auditability are explicit as described above.
- S04's corrective architect turn is default-off. Only `prompt_contract`,
  `prompt_adoption`, and declaration-backed `rework_declaration_mapping`
  failures may trigger one; product findings and verification/seat failures
  may not. The architect sees bounded drive-held facts and may emit only
  `architect_corrective_proposal.json` in staging. The drive can apply complete
  content only to the exact failing coding/review prompt or rework declaration
  after structural, accepted-history, sidecar, path-family, and released-
  frutlups dry-run target checks. It cannot touch roadmaps, registers, reviews,
  verdicts, or accepted history. Read the `architect_corrective_turn` phases,
  proposal hash/evidence path, before/after hashes or refusal, and the matching
  escalation section; a refused proposal preserves the original stop.
- S05 puts `campaign_id` and optional `predecessor_run_id` in manifests and
  start events only when known, then exposes the aggregate campaign report and
  truthful plan observation described in sections 9-10.

**Version 6 delta (M010, the frutlups 0.2 seam consumer)**: the drive
ships a typed consumer for released frutlups' three subprocess seam
verbs (`drive-payload`, `drive-frontier`, `corrective-publish`) as
`frutlups_drive.seam_consumer`, with a one-command offline
qualification proof (`scripts/verify_frutlups_seam_consumer.py
--seam-python <producer interpreter>` in a released checkout;
`08_pkg/scripts/...` in development). This surface is PARALLEL to the
operational loop — nothing about running, monitoring, or stopping
changes for an operator — and it dispatches no provider seat. Since
2026-08-31 the operational planning baseline itself is released
frutlups 0.2.1 (the observation contract is key-identical with
0.2.0 and 0.1.8, so every procedure in this manual is unchanged; the governed
interpreter binding in the execution-environment record is the
authority).

**The kill switch**: `stop <project>` writes a STOP sentinel; the
supervisor halts at the next safe point (including mid-backoff) with a
governed stop. Removing the sentinel is a human decision.

**When it stops**: read the escalation, adjudicate the cause (the v1
reviews are worked examples of exactly this), fix what a human should
fix — never inside the run store — and only then `resume` under
explicit authority. A resume consumes the durable pending witness,
resets only the failure streaks (a human adjudicated; every monotone
budget still counts), and continues the same run. Resumes are precious:
v1 practice is one per ruling.

### Governed filing protocol (stopped runs only)

Use this protocol only when a run is already STOPPED, its recovery requires
placing an existing seat-authored artifact at the path declared by the
governing prompt or planning state, and a human ruling authorizes that exact
filing. The two precedent classes are (1) filing a blocked review report aside
under its round-qualified name before the next review round, as authorized by
owner note 031 and disclosed in the second-pass campaign ledger, and (2)
filing a genuine rework self-report from the wrong canonical path to its
prompt-declared disjoint-window path, as authorized by owner note 034 and
disclosed in the rerun ledger with an identical SHA-256 before and after.
See `05_governance/human_owner_notes/031_2026-08-17_campaign_stop2_seat_visible_interpreter_guidance.md`,
`03_experiments/second_pass_acceptance_campaign_record.md` (ledger L5),
`05_governance/human_owner_notes/034_2026-08-19_campaign_rerun_stop1_file_and_resume_ruled.md`,
and `03_experiments/m006_s03_acceptance_rerun_campaign_record.md`
(ledger L3).

It does **not** authorize editing content, merging or synthesizing an artifact,
touching accepted history, or filing while the run is live. Never perform it
during an active watch or anywhere between dispatch and collection. The
note-031 recovery preserved both blocked reports byte-exact under distinct
round-qualified names; the note-034 recovery disclosed that the accepted
round-1 self-report had already been overwritten and unrecoverable, rather
than treating filing as authority to repair that lost history.

After the stop and ruling:

1. Record the authorizing ruling and identify the exact source and declared
   destination paths. Refuse the operation if the destination is accepted
   history or already contains different bytes.
2. Read the source without changing it and compute its SHA-256. Do not edit,
   normalize, concatenate, or regenerate any byte.
3. Copy the bytes to the destination — never move-and-edit — and preserve the
   source bytes where the seat wrote them or in the attempt's byte-exact
   snapshot.
4. Compute the destination SHA-256 and compare it with the source hash. A
   mismatch is not a filing; leave the run stopped and seek a new ruling.
5. In the campaign's intervention ledger, record the source path, destination
   path, identical SHA-256, filing timestamp, authorizing ruling, and the fact
   that a human performed the copy. Resume only after this entry exists.

The disclosure-plus-hash trail is the authorship boundary: it proves that the
operator placed already-authored seat bytes instead of authoring replacement
content. An operator who omits that record has performed the unauthorized
artifact write identified by F9/F15, not a governed recovery.

Mechanism assessment — **DEFERRED candidate `governed-refile`**: a future
human-invoked command would need to enforce the stopped-run copy, dual-hash,
source-preservation, and journal-evidence contract above, but the existing
`adoption` event honestly means incorporation of verified coder-attempt
evidence from a prior run, not a workspace filing, and this slice forbids the
new event kind required to journal re-filing without overloading that meaning.

**Golden rules**: never edit the run store or a capture; never
sanitize a transcript; never retry past a cap; never let anything but
the governed verbs write loop artifacts; commit at boundaries you
choose, not mid-run; and treat every governed stop as evidence first,
inconvenience second — all three v1 stops turned out to be the system
catching something real.

## 11. Talking to the architect

Two different "architects" exist, on purpose:

- **The runtime architect seat** (Opus 5 in v1) is not a chat. It gets
  single guarded reconciliation turns and, only when the version 5 gate opts
  in, the narrow corrective turn described in section 10 — one prompt, one
  staged proposal, published only through the applicable guarded writer. You
  influence it through the roadmap you write and the guidance baked
  into the generated prompt; you audit it through its captures.
- **The project architect is this conversation** — the development-side
  role that writes roadmaps and prompts, executes reviews, adjudicates
  stops, records owner notes, and keeps scope honest. It is a logical
  role, deliberately not tied to any provider.

To keep chatting with the architect across sessions: open a session in
the drive repository and let the standing orientation do its work —
`CLAUDE.md` defines the read order and rules, `PROJECT_STATE.md` says
exactly where things stand, `prompts/INDEX.md` and
`05_governance/reviews/INDEX.md` index the whole history, and the
architect initialization prompt under the initialization directory
re-establishes the role from scratch. The architect's persistent memory
directory carries session-spanning lessons. State your ruling or
question in plain language — the working pattern of all of v1 was
exactly that: you launch coding prompts and report outcomes; the
architect reviews, adjudicates, records, and hands you the next
one-line decision.

## 12. The loop on one page (operator's view)

```
you: write roadmap ── commit baseline ── declare policy ── rule the gate
                                                              │
drive: run ──► frutlups: next step? ──► seat turn ──► verify ──► review
  ▲                │                       (captured)             │
  │                ▼                                              ▼
  │   needs_specification ──► architect seat ──► N04 guarded publish
  │                                                               │
  └── verdicts ── slices complete ── pass freeze ── two clean ── COMPLETE
                                                                  │
any off-contract event ──► governed stop + escalation ──► you adjudicate
                                        │                        │
                                        └──────── resume ◄───────┘
```

Costs are bounded by the gate, evidence is complete by construction,
and nothing irreversible happens without a human ruling. That is v1's
whole design — use it exactly as lazily as that allows.
