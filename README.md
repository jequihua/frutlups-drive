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

Read [the operator's manual](docs/operators_manual.md) before authorizing live
model seats. It covers the roadmap pair, policy, bindings, live gate, budgets,
monitoring, stops, and recovery in full.
