# dupe_finder

A single-file Python utility (stdlib only, no dependencies) that finds duplicate
files across your drives, tells you **how much space you'd reclaim before
deleting anything**, and helps you delete redundant copies while always keeping
exactly one.

## How it detects duplicates

Files are compared by *content*, not name. To stay fast on large drives it works
in three cheap-to-expensive stages, and only advances files that survive each:

1. **Group by size** — files with a unique size can't have a duplicate.
2. **Partial hash** — hash the first 64 KB of each same-size file.
3. **Full hash** — hash the entire file only when the partial hashes collide.

Only files with an identical full-content hash are reported as duplicates, so
same-size-but-different files are never falsely matched. Both hash passes run in
parallel across worker threads (tunable with `--workers`).

## Install

It's a single self-contained file — you can just run it:

```powershell
python dupe_finder.py --help
```

Or install it as a proper command (`dupe-finder`) with pip or pipx:

```powershell
pipx install .                 # from a checkout
pip install .                  # into the current environment
pip install .[recycle]         # also pulls in send2trash for Trash support
```

Then:

```powershell
dupe-finder --type pdf --dry-run
```

No third-party dependencies are required. `send2trash` is optional (see Recycle
Bin notes below). Requires Python 3.9+.

## Usage

```powershell
# Scan every drive and just report duplicates (deletes nothing)
python dupe_finder.py

# Scan specific folders
python dupe_finder.py -p D:\Photos -p "E:\Backup"

# Ignore files under 1 MB
python dupe_finder.py --min-size 1MB

# Only look at specific file types (comma-separated or repeated; dot optional)
python dupe_finder.py --type jpg,png,gif
python dupe_finder.py -p D:\Music --ext .mp3 --ext .flac

# Report, then delete redundant copies keeping the OLDEST one, prompt to confirm
python dupe_finder.py --delete --keep oldest

# Decide group-by-group interactively
python dupe_finder.py --delete --interactive

# Delete without a final prompt, writing a JSON record of what was removed
python dupe_finder.py --delete --keep shortest-path --yes --log deleted.json

# Send duplicates to the Recycle Bin instead of deleting permanently
python dupe_finder.py --recycle --keep oldest

# Preview the deletion plan (counts + space) without removing anything
python dupe_finder.py --type pdf --dry-run

# Prefer keeping copies on a specific drive/folder; fall back to --keep otherwise
python dupe_finder.py --type pdf --recycle --prefer L: --keep oldest

# Repeat runs reuse cached hashes (unchanged files aren't re-hashed)
python dupe_finder.py --type pdf              # first run populates the cache
python dupe_finder.py --type pdf              # second run is much faster
python dupe_finder.py --type pdf --no-cache   # force a full re-hash
```

## Deletion safety

- Nothing is ever deleted in plain report mode (the default).
- Even with `--delete`, you get a **summary of space to reclaim and a
  confirmation prompt** first (unless you pass `--yes`).
- `--dry-run` shows exactly what would be removed (and how much space) without
  touching any files.
- Every group always keeps one copy — you choose which with `--keep`, and can
  bias the choice toward a location with `--prefer`.
- `--recycle` sends removed files to the **Recycle Bin** (recoverable) instead
  of deleting them permanently.
- `--log FILE` writes a JSON record of every removed file and the copy it was
  kept against.

> **Recycle Bin caveat:** the bin has a per-drive size cap. Files larger than
> the available quota are **permanently deleted** rather than recycled (Windows
> behavior). When recycling many large files, don't rely on everything being
> recoverable.

## Options

| Option | Description |
|---|---|
| `-p, --path DIR` | Directory to scan (repeatable). Default: all drives. |
| `-t, --type, --ext EXT` | Only scan these extensions, e.g. `jpg,png` (repeatable, leading dot optional, case-insensitive). Default: all files. |
| `--min-size SIZE` | Ignore files smaller than this (e.g. `1MB`). Default `1`. |
| `--max-size SIZE` | Ignore files larger than this. |
| `--exclude SUBSTR` | Skip any path containing this substring (repeatable). |
| `--follow-symlinks` | Follow symbolic links (off by default). |
| `--delete` | Enter deletion mode after reporting. |
| `--recycle` | Send removed files to the Recycle Bin instead of deleting permanently (implies `--delete`). |
| `--keep STRATEGY` | Which copy to keep: `oldest` (default), `newest`, `shortest-path`, `longest-path`, `first`. |
| `--prefer SUBSTR` | Prefer keeping copies whose path contains this substring, e.g. `--prefer L:` (repeatable; earlier values win). Groups with no match fall back to `--keep`. |
| `--dry-run` | Show the deletion plan (counts and space) without removing anything. |
| `--interactive` | Confirm each duplicate group individually. |
| `--yes` | Skip the final confirmation prompt. |
| `--log FILE` | Write a JSON log of deleted files. |
| `--workers N` | Parallel hashing threads (default: based on CPU count). Use `1` for spinning disks where parallel reads thrash. |
| `--cache FILE` | Persistent hash-cache file (default: per-user cache dir). Unchanged files aren't re-hashed on repeat runs. |
| `--no-cache` | Disable the persistent hash cache for this run. |
| `-q, --quiet` | Suppress progress output. |

## Notes

- When scanning whole drives / the filesystem root (no `-p`), system locations
  are skipped by default, tailored per OS. Pass explicit `-p` paths to scan them.
  - **Windows:** `\Windows\`, `\$Recycle.Bin`, `\System Volume Information`,
    `\ProgramData\`, `\AppData\`.
  - **macOS:** `/System`, `/Library`, `/private`, `/Volumes`, `/Applications`,
    `/usr`, `~/Library`, and friends.
  - **Linux:** pseudo-filesystems (`/proc`, `/sys`, `/dev`, `/run`) plus common
    system trees (`/usr`, `/var`, `/boot`, `/snap`, `/tmp`, …).
  - Unix defaults are **anchored at the root**, so a user folder like
    `~/dev` or `~/var` is never mistaken for `/dev` or `/var`.
- Scanning entire drives can take a while and touch many files; start with a
  single folder or a `--min-size` filter to get a feel for it.
- Hashing is multi-threaded by default. On **SSDs/NVMe** this is a big speedup;
  on a single **spinning HDD**, parallel reads can thrash the head — pass
  `--workers 1` there.
- `--recycle` works **natively on every platform, no dependency required**:
  the Windows shell API via `ctypes`, `~/.Trash` on macOS, and the FreeDesktop
  trash spec on Linux (home trash, or the mount's `.Trash-$uid` for other
  drives, with restorable `.trashinfo` metadata). If the
  [`send2trash`](https://pypi.org/project/send2trash/) package happens to be
  installed it's used in preference (most battle-tested), but it's optional.
- Repeat scans are fast: full-content hashes are cached per user, keyed by
  path + size + mtime, so unchanged files are never re-hashed. Control it with
  `--cache FILE` / `--no-cache`.
- Output is written with `errors="replace"`, so filenames containing characters
  the console can't display won't crash the run (they show a `?` placeholder).
- Requires Python 3.9+.

## Releasing (maintainers)

Releases are published to PyPI automatically by
`.github/workflows/release.yml` using **PyPI Trusted Publishing** (OIDC) — no
API tokens are stored. One-time setup:

1. On PyPI, add a *trusted publisher* for the project `dupe-finder-cli`:
   owner `canindya`, repo `dupe-finder-cli`, workflow `release.yml`,
   environment `pypi`. (Do the same on TestPyPI with environment `testpypi` if
   you want to dry-run.)
2. In the GitHub repo settings, create environments named `pypi` (and
   optionally `testpypi`).

Then, to cut a release:

1. Bump `version` in `pyproject.toml` and commit.
2. Publish a GitHub Release (tag e.g. `v1.0.1`) — the workflow builds the sdist
   and wheel and publishes to PyPI.

To test the pipeline first, use **Actions → Release to PyPI → Run workflow** and
pick `testpypi`.

## Recommended workflow

1. **Report** — run with a `--type`/`-p` filter to see duplicates and total
   reclaimable space.
2. **Preview** — add `--dry-run` (plus `--prefer`/`--keep`) to confirm the exact
   plan and where deletions would land.
3. **Act** — swap `--dry-run` for `--recycle` (or `--delete`) and add `--log` to
   keep a record.
