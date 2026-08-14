# Package Tests

Status: active — this is the product test lane for the `frutlups_drive`
package in `../src`.

Run it from the repository root:

    python -m unittest discover -s 08_pkg/tests

The lane is deterministic `unittest` only: temporary directories, injected
clocks and payloads, no network, and no machine-specific paths. `_bootstrap.py`
prepends `08_pkg/src` to `sys.path`, so the same command works with or without
the editable install; `_scenario.py` is the shared supervisor scenario
harness. Planning-state conformance fixtures live under `fixtures/planstate/`
and the template-shaped CLI fixture project under
`fixtures/projects/minimal_v3/`. Test families cover contracts, parsers,
run-store safety, runtime primitives, supervisor scenarios, verifier
evidence, workspace fences, adversarial refusals, crash/resume, and the CLI.

M003 Phase A adds offline provider-neutral families: role-seat identity
propagation (`test_role_identity.py`), the generic bounded subprocess agent
executor (`test_subprocess_agent.py`), the `FrutlupsPlanProvider`
(`test_frutlups_provider.py`), the no-I/O live-gate assessment
(`test_livegate.py`), and the dry-run/refusal CLI surface
(`test_cli_phase_a.py`). Subprocess lanes launch only deterministic local
helper processes through the interpreter running the tests;
`stub_frutlups_status.py` is the reviewer-visible stub CLI that emits a
released-shaped planning wrapper — it is not frutlups, and those lanes make
no real-frutlups compatibility claim, contact no provider, and read no
credential.

M003 Phase B (M003-S02) adds the released-contract families. Strict
released-wrapper parsing is covered by `test_released_contract.py` over the
captured wrapper in `fixtures/frontier/` plus curated released-shape members
(the five outcomes, nine steps, and the malformed/contract/version/
combination/path/constant/size/privacy refusals), served through
`stub_frutlups_status.py` for the transport lanes. `test_frutlupscli.py` covers the machine-local
launch binding loader and the offline verb-writer refusal lanes with fake
runners. `test_frutlups_e2e.py` exercises the actual installed frutlups CLI
candidate through the governed tool interpreter against the committed
two-slice fixture project `fixtures/projects/frutlups_e2e/` — clean pass,
`needs_work` repair, blocked/override stops, all three governed verbs, the
crash/resume matrix, and dogfood-adjacent privacy checks. That lane skips
honestly, recording the observed host fact, when the governed tool
environment is absent or still holds the uncorrected released baseline; the
"real" component is the local frutlups artifact tool only — no provider
command, credential, network, or cost.

The reusable template's own behavior is covered by the top-level `tests/`
suite (`python -m unittest discover -s tests`), which never imports this
package; the import boundary is enforced in both directions by
`test_import_boundaries.py`.
