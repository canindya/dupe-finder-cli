# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A single-file, **stdlib-only** Python CLI (`dupe_finder.py`) that finds duplicate
files by content across drives/folders, reports reclaimable space, and removes
redundant copies (keeping exactly one per group) either permanently or to the
Recycle Bin. Target platform is Windows, but the core logic is cross-platform.

There are no third-party dependencies. `send2trash` is used **only if already
installed**; otherwise `--recycle` falls back to the native Windows shell API via
`ctypes`. Do not add required dependencies without a strong reason — keeping it
zero-install is a design goal.

## Layout

- `dupe_finder.py` — the entire tool (CLI, scanning, hashing, deletion).
- `pyproject.toml` — packaging; single-module (`py-modules = ["dupe_finder"]`),
  console script `dupe-finder = "dupe_finder:run"`, optional `[recycle]` extra
  (`send2trash`). Build with `python -m build`; test install with `pipx install .`.
- `LICENSE` — MIT.
- `README.md` — user-facing docs. Keep options/examples in sync when you change
  the CLI.
- `recycle_log.json` / `pdf_duplicates_report.txt` — generated artifacts from
  real runs; not source (git-ignored). Safe to ignore/regenerate.

## Architecture (all in `dupe_finder.py`)

Duplicate detection is a three-stage funnel, cheap → expensive, so large drives
stay fast:

1. `Scanner.collect_by_size()` — walk roots, bucket files by exact size; discard
   unique sizes (a unique size cannot have a duplicate). Applies `--type`,
   `--exclude`, size filters, symlink policy here.
2. `find_duplicates()` — a **partial hash** pass (first 64 KB, `PARTIAL_READ`)
   buckets candidates by `(size, partial_hash)`, then a **full-content hash**
   pass confirms survivors, bucketing by `(size, full_hash)`. Hashes are only
   ever compared within the same size (the bucket key includes size), so a
   partial-hash collision between different-sized files can't cause a false
   merge. Both passes run through `_hash_many()`, which uses a
   `ThreadPoolExecutor` (`--workers`, default `default_workers()`), or a plain
   loop when `workers <= 1`.
3. `report()` / `delete_duplicates()` — present results and, if requested, build
   a deletion plan and execute it.

Hashing uses `blake2b` and streams in `CHUNK`-sized reads. Threads help because
both file I/O and `hashlib` release the GIL. `hash_file()` returns `None` on read
errors (permission/locked/vanished) and those files are skipped — never let one
unreadable file abort a scan.

Keeper selection: `choose_keeper(group, strategy, prefer)`. `--prefer` narrows
candidates to paths matching a substring (first matching token wins); `--keep`
breaks ties. Groups with no preferred match fall back to the whole group.

Deletion: `_do_delete()` does the actual removal — `os.remove()` normally, or
`send_to_recycle()` when `--recycle`. `send_to_recycle()` prefers `send2trash`,
else `_recycle_via_ctypes()` (SHFileOperationW with `FOF_ALLOWUNDO`).

## Conventions & gotchas

- **Console encoding:** Windows consoles default to cp1252 and crash on
  filenames with characters they can't encode. `main()` calls
  `sys.stdout/stderr.reconfigure(errors="replace")`. Keep this — printing paths
  is otherwise a real crash risk. Prefer ASCII in program output (avoid em
  dashes / `·` etc. in `print()` strings and the module docstring, which is
  reused as the `--help` epilog).
- **Safety first:** report mode never deletes. Deletion always keeps one copy
  per group, prints a plan with space-to-reclaim, and confirms (unless `--yes`).
  Preserve this contract. `--dry-run` must never remove anything.
- **Recycle Bin cap:** files larger than the bin's per-drive quota are
  *permanently* deleted, not recycled. This is documented; keep it documented if
  behavior changes.
- **Long scans:** scanning all drives can take minutes and touch tens of
  thousands of files. When running for real, use a background run and redirect
  output to a file. Note stdout is block-buffered when redirected — partial
  totals won't appear until the process exits.

## Running & testing

- Python: invoke with `python dupe_finder.py ...` (Python 3.9+; 3.13 is present).
- No test suite. To verify changes, create a temp dir with known duplicates
  (identical content in different files), plus a unique file and a
  same-size-but-different pair, and confirm only the true duplicates are grouped.
  Test deletion against throwaway fixtures — **never** against real user data.
- Clean up any temp fixtures (and Recycle Bin test entries) after verifying.

## When changing the CLI

Update, in lockstep: the `argparse` definition in `build_parser()`, the wiring in
`main()`, the function signatures it calls (`delete_duplicates`, `choose_keeper`,
`Scanner`), the module docstring examples, and `README.md` (usage + options
table).
