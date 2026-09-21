# AiiDA Error Inspector

A terminal UI for triaging failed AiiDA workchains.

High-throughput campaigns produce failures in bulk, and AiiDA tells you *that* a
workchain failed while the actual reason sits several levels down — in the
output file of a calculation the workchain called, possibly indirectly. This
tool closes that gap: browse the failure tree, read one representative output,
then turn that single observation into a group-wide classification that persists
between sessions.

```
 PK  Type/Formula      State      Exit  Tag          │ Call graph · 4 called, 4 failed
 6   PwRelaxWorkChain  excepted   -                  │ ▼ PwRelaxWorkChain<1> Finished [401] 1:while_(…)
 1   PwRelaxWorkChain  finished   401   SCF conver…  │ └─ ▼ PwBaseWorkChain<2> | relax Finished [410]
 7   PwRelaxWorkChain  finished   0     -            │    ├─ PwCalculation<3> | iteration_01 Fin [305]
                                                     │    ├─ PwCalculation<4> | iteration_02 Fin [305]
                                                     │    ├─ PwCalculation<5> | iteration_03 Excepted
│    └─ inputs (3)
│         structure:  StructureData<9>  Ca2N
│         parameters: Dict<11>
│         kpoints:    KpointsData<12>
```

## Install

```bash
pip install -e ".[dev]"
```

Requires a working AiiDA profile (`aiida-core >= 2.0`).

## Run

```bash
aiida-error-inspector                    # browse every core group
aiida-error-inspector "my-group"         # open a group by label
aiida-error-inspector 123                # ...or by PK / UUID

aiida-error-inspector --data-dir ~/tags  # where tags and rules are kept
aiida-error-inspector --profile other    # a non-default AiiDA profile
aiida-error-inspector --debug            # verbose log in the data directory
```

## How triage works

1. Open a group. Failures sort to the top, colour-coded: red for
   excepted/killed, yellow for a non-zero exit code.
2. Press `w` for the **workflow tree** on the right — the same shape as
   `verdi process status`, following whatever the cursor is on. `W` focuses it;
   `Enter` on any node opens it, so you can jump straight to the calculation
   that actually failed.
3. Each process carries collapsed `inputs`/`outputs` branches with its full
   provenance — the structure that went in, the parameters, the relaxed
   structure that came out. `Enter` on a **StructureData** shows formula, cell,
   volume, density, sites and extras; `Enter` on a `Dict` shows its contents.
   `D` hides the data nodes if you want just the call graph.
4. Press `d` on a row for a **failure summary** — exit status and message, the
   exception traceback, the failing call chain, and the tail of the scheduler
   error — without drilling down at all.
5. If you need the raw file, press `a` to walk down to the calculation and open
   any retrieved file.
6. Press `t` to turn what you found into a rule, `E` to classify by exit code,
   then `u` to re-scan as new failures arrive.
7. Press `S` for the campaign breakdown: how many are classified, what is
   killing the rest.

### Classification rules

Three kinds, all stored in `data/patterns.json`:

| Kind | Matches | Reads files? |
|---|---|---|
| `substring` | plain text in a named file (default) | yes |
| `regex` | a regular expression in a named file | yes |
| `exit_code` | the failing calculation's exit status | **no** |

Exit-code rules need no file access at all, so `E` → `Ctrl+A` can classify an
entire group at query speed — including workchains that never produced output.

A workchain can carry several tags; a failure often has more than one symptom.

### The scan cache

`data/scanned.json` records which rules each workchain has already been tested
against — misses included. Adding a fourth pattern therefore reads files only
for that pattern, and re-running an unchanged scan costs almost nothing.

## Keys

Press `?` in the app for the full list, grouped by context. The footer only
advertises keys that do something where you are.

| Key | Action |
|---|---|
| `a` / `Enter` | Select — drill down |
| `b` / `Backspace` | Back |
| `/` | Filter rows, or search inside a file |
| `T` | Cycle tag filter: all → tagged → untagged |
| `w` / `W` | Toggle / focus the workflow tree panel |
| `D` | Show or hide data nodes in the tree |
| `v` | Inspect the data node under the cursor (structure, Dict, ...) |
| `d` | Failure summary for the row under the cursor |
| `t` | Create a rule from the open file |
| `E` | Tag by exit code (`Ctrl+A` inside: auto-tag every code) |
| `u` | Re-scan with every saved rule |
| `x` | Remove all tags from a row |
| `i` | Tag inspector — counts and the rule behind each tag |
| `S` | Statistics for the group |
| `e` | Export (txt + csv + json, including the unclassified set) |
| `n` `N` `L` `F` | Next / previous / last match; toggle filtered view |
| `p` | Search presets |
| `f` | Open the current file in `$PAGER` |
| `m` / `l` | More / fewer preview lines |
| `Escape` | Dismiss a panel, clear a search, or cancel a running scan |
| `?` / `q` | Help / quit |

## Layout

```
aiida_error_inspector/
├── main.py            CLI entry point, logging, profile loading
├── app.py             the Textual UI
├── traversal.py       walking the call graph and data provenance
├── datainfo.py        human-readable summaries of data nodes
├── classify.py        the rules (substring / regex / exit code)
├── scan.py            the scan engine — no AiiDA or Textual imports
├── node_inspector.py  streaming access to repository files
├── storage.py         atomic JSON persistence
└── queries.py         group and node queries
tests/                 pytest; DB tests use a throwaway sqlite profile
```

`classify.py`, `scan.py` and `storage.py` import neither AiiDA nor Textual,
which is what lets most of the suite run without a profile.

## Tests

```bash
pytest                  # everything
pytest -m "not db"      # skip the ones needing a temporary profile
```

DB-marked tests spin up a temporary `core.sqlite_dos` profile, so no PostgreSQL
is required.
