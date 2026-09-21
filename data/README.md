# AiiDA Error Inspector — data files

Persistent state for the TUI. These are plain JSON so you can read, edit, diff
and share them; keeping them in version control gives you a history of how a
campaign's failures were classified.

The directory is chosen in this order: `--data-dir`, `$AIIDA_ERROR_INSPECTOR_DATA`,
this `data/` directory when running from a source checkout, then the platform
user-data directory.

## `tags.json`

Which workchains carry which tags.

```json
{
  "version": 2,
  "tags": {
    "SCF convergence issue": [8378, 9270, 20266],
    "exit 305": [8378]
  }
}
```

A PK may appear under several tags — a calculation can fail for more than one
reason. Two older layouts are still read automatically: a bare
`{tag: [pks]}` object, and the original `{"pk": "tag"}` mapping.

## `patterns.json`

The rules that assign tags. Three kinds:

```jsonc
{
  "SCF convergence issue": {
    "kind": "substring",              // default; may be omitted
    "filename": "aiida.out",
    "pattern": "convergence NOT achieved"
  },
  "QE routine error": {
    "kind": "regex",
    "filename": "aiida.out",
    "pattern": "Error in routine\\s+(\\w+)",
    "case_sensitive": true
  },
  "exit 305": {
    "kind": "exit_code",
    "exit_code": 305                  // reads no files at all
  }
}
```

Entries with no `kind` are read as `substring`, so files written by earlier
versions keep working unchanged.

## `scanned.json`

The scan cache: which classifiers each workchain has already been tested
against.

```json
{"8378": ["a1b2c3d4e5f60718", "0f1e2d3c4b5a6978"]}
```

This records *misses* as well as matches, so adding a new pattern only reads
files for that pattern, and re-running a scan with no changes is nearly free.
Delete an entry to force that workchain to be re-examined.

Supersedes `categorized.json`, which stored only matched PKs and therefore
re-read every unmatched workchain on every scan. An existing `categorized.json`
is migrated automatically on first run and then left alone.

## `settings.json`

`preview_lines` (how many lines of an output file to show) and `max_calcjobs`
(how many failing CalcJobs per workchain a scan inspects).

## `qe_patterns.json`

Search presets offered by `p` in the file viewer. Editable.

## `export_*.txt` / `.csv` / `.json`

Written by `e`. Includes the **unclassified** PKs as well as the tagged ones —
that is the set that still needs attention.
