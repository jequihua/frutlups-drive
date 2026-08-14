# The frutlups-drive Operator's Manual (v1)

A human-friendly guide to opening, initializing, and executing a fully
autonomous, milestone-structured development project with template v3,
frutlups, and frutlups-drive — as they exist in closed version 1.

Everything here describes shipped behavior proven by the v1 closure
campaigns. Where a step needs a governance decision, the manual says so
explicitly: autonomy in this system is always bounded by something a
human ruled.

---

## 1. The three tools, in one breath each

- **Template v3** is the *project shape*: a repository layout where the
  unit of progress is an artifact moving between reviewable states — a
  roadmap of milestones, numbered prompts, reviews, verdicts, and one
  short live state file (`PROJECT_STATE.md`).
- **frutlups** is the *planning interpreter*: a separate CLI that reads
  the roadmap, reviews, and verdicts, and answers exactly one question —
  "what is the next governed step?" — plus three governed artifact verbs
  (make a coding prompt, make a review prompt, record a verdict). It
  never executes anything.
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
  reconciliation target.
- A governance workspace with a reviews directory (reports, verdicts,
  and self-reports land here through governed verbs and seat turns).
- Prompt templates (coding prompt, review prompt, self-report) under
  the prompts workspace — frutlups fills these when generating the
  loop's prompt artifacts.
- The implementation workspace (source and tests the coder seat will
  touch) with its verification command declared in the roadmap.
- The frutlups layout declaration (frutlups.layout.yaml at the project
  root) and the drive policy file (section 5).
- A stay-in-workspace instruction inside every generated prompt surface
  — the templates carry it so every seat is told, every turn, to touch
  nothing outside the project.

Rules of thumb learned the hard way in v1: commit the baseline before
any dispatch (drive diffs everything against it); remember git cannot
represent empty directories, so seed keep-files where a directory must
exist; and never hand-edit anything under the run store.

## 4. Writing the roadmap (milestones made of slices)

Released frutlups uses two plain-Markdown roadmap files as one strict,
parseable contract. The **active roadmap** carries the milestone inventory
and statuses. The sibling **development roadmap** carries the `Slices:`
bullet breakdown that lets frutlups derive the slice frontier. Do not collapse
the pair into one file: released frutlups (0.1.2 and 0.1.3 alike) does not
derive a planning frontier from that single-file shape. Since frutlups
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

If a slice is deliberately under-specified, planning reports
needs-specification and the **architect seat** gets exactly one guarded
turn to propose a completed roadmap — the N04 writer publishes it only
if every protected field is byte-identical and only specification text
changed. (v1 lesson, now baked into the generated prompt: the current
structure is authoritative even where it looks odd; proposals that
"fix" layout are refused.)

## 5. Declaring the policy — seats, budgets, boundaries

The drive policy lives at the driven project root in
frutlups_drive.toml. The v1 closure campaign's real policy, annotated:

```toml
schema_version = "frutlups_drive_policy_v1"

[target]
stop_at = "roadmap_complete"   # or "slice_complete" for one-slice runs
max_slices = 6                 # hard cap per run
max_passes = 3                 # holistic pass budget

[roles.architect]
adapter = "claude_cli"         # which CLI drives this seat
model = "claude-opus-5"        # exact pinned identifier, never an alias
workspace_access = "workspace_write"

[roles.coder]
adapter = "codex_cli"
model = "gpt-5.6-sol"
workspace_access = "workspace_write"

[roles.reviewer]
adapter = "kimi_cli"
model = "kimi-code/k3"
workspace_access = "read_only" # reviewers publish reports, not code

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
```

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
rollback and kill-switch statements, and stop conditions. The drive
assesses it at launch; any unknown field, missing approval, seat
mismatch with the policy, or malformed value refuses with
`live_authority_missing` and nothing is created, spawned, or spent.

Treat the gate as the contract it is: one ruling, one campaign scope.
When seats, limits, or scope change, write a new owner note and
re-declare. (Every v1 campaign consumed its authority exactly this
way — notes 017 through 021.)

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

## 9. Running

All commands run with the drive environment's interpreter, from
anywhere, against the driven project's path (always pass it absolute
or let the CLI resolve it — it does, since v1's final fix):

```
python -m frutlups_drive plan   <project> [--dry-run]
python -m frutlups_drive run    <project> --until slice_complete|roadmap_complete
python -m frutlups_drive resume <project> <run_id> [--until ...]
python -m frutlups_drive stop   <project>
python -m frutlups_drive report <project> <run_id> [--json]
```

A sensible first session on a fresh project:

1. `plan` — read-only; confirms frutlups parses your roadmap and shows
   the next governed step. Fix the roadmap until this looks right.
2. A **mock rehearsal** if you want one: point the policy at mock seats
   and run offline; the loop shape exercises without any spend.
3. `run <project> --until slice_complete` — the first real slice, then
   look at everything (section 10) before granting more.
4. Then `run ... --until roadmap_complete` for full autonomous
   milestone execution under your gate's budgets.

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
- **Telemetry** — `report <project> <run_id>` renders reconciled
  counters (dispatches, outcomes, memory facts, verbs, verdicts,
  durations) as text or `--json`; its error list must be empty.
- **Money and time** — every attempt journals one cost/usage fact.
  Subscription CLIs journal a contractual $0.00; the raw captures
  retain any provider-side estimates. Keep your own append-only cost
  log per campaign (`05_governance/cost_log.md` is the v1 pattern) and
  record both forms.
- **The driven repo itself** — `git status` in the project shows
  exactly what the loop produced: implementations, self-reports,
  generated prompts, review reports, verdict records, roadmap edits.
  Anything else appearing there is a finding.

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

**Golden rules**: never edit the run store or a capture; never
sanitize a transcript; never retry past a cap; never let anything but
the governed verbs write loop artifacts; commit at boundaries you
choose, not mid-run; and treat every governed stop as evidence first,
inconvenience second — all three v1 stops turned out to be the system
catching something real.

## 11. Talking to the architect

Two different "architects" exist, on purpose:

- **The runtime architect seat** (Opus 5 in v1) is not a chat. It gets
  single guarded reconciliation turns during runs — one prompt, one
  proposal, published only through the N04 writer's guards. You
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
