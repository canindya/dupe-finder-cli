#!/usr/bin/env python3
"""
dupe_finder - Find and remove duplicate files across drives.

Strategy (fast, safe):
  1. Group candidate files by exact size (cheap - no reads).
  2. Within each size group, group by a partial hash of the first 64 KB.
  3. Within each partial-hash group, group by a full-content hash.
  Files sharing the same full hash are byte-for-byte duplicates.

Deletion always keeps exactly one copy per duplicate group and reports how
much space will be reclaimed *before* anything is removed. Nothing is deleted
without confirmation unless you pass --yes.

Examples:
  # Scan every fixed drive, just report duplicates
  python dupe_finder.py

  # Scan specific folders
  python dupe_finder.py -p D:\\Photos -p "E:\\Backup"

  # Ignore tiny files, then interactively delete, keeping the oldest copy
  python dupe_finder.py --min-size 1MB --delete --keep oldest

  # Delete without prompting (careful!), writing an undo log
  python dupe_finder.py --delete --keep shortest-path --yes --log deleted.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import string
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

PARTIAL_READ = 64 * 1024   # bytes hashed for the quick partial pass
CHUNK = 1024 * 1024        # streaming chunk size for full hashing


def default_workers() -> int:
    """A sensible thread count for hashing (I/O + hashlib both release the GIL)."""
    return min(16, (os.cpu_count() or 2) * 2)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def human(n: int) -> str:
    """Human-readable byte size."""
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < step or unit == "PB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:,.2f} {unit}"
        n /= step
    return f"{n} B"


def parse_size(s: str) -> int:
    """Parse a size like '10', '512KB', '1.5MB', '2gb' into bytes."""
    s = s.strip().upper()
    units = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    for suffix in ("KB", "MB", "GB", "TB", "B"):
        if s.endswith(suffix):
            num = s[: -len(suffix)].strip()
            return int(float(num) * units[suffix])
    return int(float(s))


def normalize_extensions(values: list[str] | None) -> set[str]:
    """Turn user-supplied type args into a set of lowercase '.ext' strings.

    Accepts repeated flags and comma-separated lists, with or without a leading
    dot: --type jpg,png  ==  --type .JPG --type png  -> {'.jpg', '.png'}."""
    exts: set[str] = set()
    for value in values or []:
        for token in value.replace(" ", ",").split(","):
            token = token.strip().lower()
            if not token:
                continue
            if not token.startswith("."):
                token = "." + token
            exts.add(token)
    return exts


def windows_drives() -> list[str]:
    """Return the root of every accessible drive on Windows."""
    drives = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.exists(root):
            drives.append(root)
    return drives


def default_roots() -> list[str]:
    if os.name == "nt":
        return windows_drives()
    return ["/"]


def hash_file(path: str, limit: int | None = None) -> str | None:
    """Hash a file's content (blake2b). If *limit*, hash only the first N bytes.
    Returns None on read errors (permission, locked, vanished)."""
    h = hashlib.blake2b()
    try:
        with open(path, "rb", buffering=0) as f:
            if limit is not None:
                h.update(f.read(limit))
            else:
                while chunk := f.read(CHUNK):
                    h.update(chunk)
    except (OSError, PermissionError):
        return None
    return h.hexdigest()


def _recycle_via_ctypes(path: str) -> None:
    """Send a file to the Recycle Bin using the Windows shell API (no deps)."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE = 3
    FOF_ALLOWUNDO = 0x40
    FOF_NOCONFIRMATION = 0x10
    FOF_SILENT = 0x04
    FOF_NOERRORUI = 0x400

    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    # pFrom must be double-null terminated; path must be absolute.
    op.pFrom = os.path.abspath(path) + "\0\0"
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI

    res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if res != 0:
        raise OSError(f"SHFileOperation failed (code {res}) for {path}")


def send_to_recycle(path: str) -> None:
    """Move *path* to the Recycle Bin / Trash.

    Prefers the ``send2trash`` package if installed (best cross-platform
    behavior); otherwise falls back to the native Windows shell API. Raises
    OSError / RuntimeError if the file cannot be recycled."""
    try:
        from send2trash import send2trash  # type: ignore
        send2trash(os.path.abspath(path))
        return
    except ImportError:
        pass
    if os.name == "nt":
        _recycle_via_ctypes(path)
        return
    raise RuntimeError(
        "Recycle Bin support on this platform requires the 'send2trash' "
        "package. Install it with: pip install send2trash"
    )


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #
@dataclass
class Scanner:
    roots: list[str]
    min_size: int = 1
    max_size: int | None = None
    follow_symlinks: bool = False
    exclude: list[str] = field(default_factory=list)
    extensions: set[str] = field(default_factory=set)  # e.g. {".jpg", ".png"}
    verbose: bool = False

    def _excluded(self, path: str) -> bool:
        low = path.lower()
        return any(x.lower() in low for x in self.exclude)

    def _wanted_type(self, name: str) -> bool:
        if not self.extensions:
            return True
        return os.path.splitext(name)[1].lower() in self.extensions

    def collect_by_size(self) -> dict[int, list[str]]:
        """Walk all roots, bucketing files by size."""
        by_size: dict[int, list[str]] = defaultdict(list)
        seen_files = 0
        for root in self.roots:
            for dirpath, dirnames, filenames in os.walk(root, followlinks=self.follow_symlinks):
                if self._excluded(dirpath):
                    dirnames[:] = []
                    continue
                # prune excluded subdirs in-place so os.walk skips them
                dirnames[:] = [d for d in dirnames
                               if not self._excluded(os.path.join(dirpath, d))]
                for name in filenames:
                    if not self._wanted_type(name):
                        continue
                    full = os.path.join(dirpath, name)
                    if self._excluded(full):
                        continue
                    try:
                        st = os.stat(full, follow_symlinks=self.follow_symlinks)
                    except (OSError, PermissionError):
                        continue
                    if not os.path.isfile(full):
                        continue
                    if os.path.islink(full) and not self.follow_symlinks:
                        continue
                    size = st.st_size
                    if size < self.min_size:
                        continue
                    if self.max_size is not None and size > self.max_size:
                        continue
                    by_size[size].append(full)
                    seen_files += 1
                    if self.verbose and seen_files % 5000 == 0:
                        print(f"  ...scanned {seen_files:,} files", file=sys.stderr)
        # keep only sizes with >1 file — a unique size cannot have a duplicate
        return {s: paths for s, paths in by_size.items() if len(paths) > 1}


def _hash_many(paths: list[str], limit: int | None, workers: int,
               label: str, verbose: bool) -> dict[str, str]:
    """Hash *paths* (optionally only the first *limit* bytes) concurrently.

    Returns {path: hash}, silently dropping unreadable files. Falls back to a
    plain sequential loop when workers <= 1 (useful for spinning disks, where
    parallel reads can thrash the head)."""
    result: dict[str, str] = {}
    done = 0
    total = len(paths)

    def _tick() -> None:
        nonlocal done
        done += 1
        if verbose and done % 2000 == 0:
            print(f"  ...{label} {done:,}/{total:,}", file=sys.stderr)

    if workers <= 1:
        for p in paths:
            h = hash_file(p, limit=limit)
            if h is not None:
                result[p] = h
            _tick()
        return result

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(hash_file, p, limit): p for p in paths}
        for fut in as_completed(futures):
            h = fut.result()
            if h is not None:
                result[futures[fut]] = h
            _tick()
    return result


def find_duplicates(by_size: dict[int, list[str]], workers: int = 1,
                    verbose: bool = False) -> list[list[str]]:
    """Refine size groups into confirmed duplicate groups.

    Two content passes, each hashing in parallel: a cheap partial hash (first
    64 KB) to weed out obvious non-matches, then a full-content hash to confirm.
    Hashes are only ever compared within the same size, so distinct files that
    happen to share a partial hash are never falsely merged."""
    size_of = {p: size for size, paths in by_size.items() for p in paths}
    candidates = list(size_of)

    # Pass 1: partial hash everything, bucket by (size, partial-hash).
    if verbose:
        print(f"  hashing {len(candidates):,} candidates (partial pass, "
              f"{workers} worker(s))", file=sys.stderr)
    partial = _hash_many(candidates, PARTIAL_READ, workers, "partial-hashed", verbose)
    partial_buckets: dict[tuple[int, str], list[str]] = defaultdict(list)
    for p, ph in partial.items():
        partial_buckets[(size_of[p], ph)].append(p)

    survivors = [p for grp in partial_buckets.values() if len(grp) > 1 for p in grp]

    # Pass 2: full hash only survivors, bucket by (size, full-hash).
    if verbose:
        print(f"  confirming {len(survivors):,} candidates (full pass)",
              file=sys.stderr)
    full = _hash_many(survivors, None, workers, "full-hashed", verbose)
    full_buckets: dict[tuple[int, str], list[str]] = defaultdict(list)
    for p, fh in full.items():
        full_buckets[(size_of[p], fh)].append(p)

    return [grp for grp in full_buckets.values() if len(grp) > 1]


# --------------------------------------------------------------------------- #
# Reporting & deletion
# --------------------------------------------------------------------------- #
def group_wasted(size: int, count: int) -> int:
    """Space reclaimable in a group of *count* identical files: all but one."""
    return size * (count - 1)


def report(groups: list[list[str]]) -> int:
    """Print duplicate groups and return total reclaimable bytes."""
    if not groups:
        print("\nNo duplicate files found.")
        return 0

    total_waste = 0
    total_dupes = 0
    # Largest reclaimable groups first.
    groups_sorted = sorted(
        groups, key=lambda g: os.path.getsize(g[0]) * (len(g) - 1), reverse=True
    )
    for i, g in enumerate(groups_sorted, 1):
        try:
            size = os.path.getsize(g[0])
        except OSError:
            continue
        waste = group_wasted(size, len(g))
        total_waste += waste
        total_dupes += len(g) - 1
        print(f"\n[{i}] {len(g)} copies | {human(size)} each | "
              f"reclaimable {human(waste)}")
        for p in g:
            print(f"      {p}")

    print("\n" + "=" * 60)
    print(f"Duplicate groups : {len(groups):,}")
    print(f"Redundant files  : {total_dupes:,}")
    print(f"Reclaimable space: {human(total_waste)}")
    print("=" * 60)
    return total_waste


KEEP_STRATEGIES = ("oldest", "newest", "shortest-path", "longest-path", "first")


def choose_keeper(group: list[str], strategy: str,
                  prefer: list[str] | None = None) -> str:
    """Pick which file in a duplicate group to KEEP.

    If *prefer* is given, restrict the candidates to files whose path contains
    the first prefer-substring that matches any file in the group (e.g. keep the
    copy on drive 'L:'). The *strategy* then breaks ties among those candidates.
    Groups with no preferred copy fall back to the whole group."""
    candidates = group
    if prefer:
        for token in prefer:
            matched = [p for p in group if token.lower() in p.lower()]
            if matched:
                candidates = matched
                break

    if strategy == "first":
        return candidates[0]
    if strategy == "shortest-path":
        return min(candidates, key=len)
    if strategy == "longest-path":
        return max(candidates, key=len)
    # time-based
    def mtime(p: str) -> float:
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0
    if strategy == "oldest":
        return min(candidates, key=mtime)
    if strategy == "newest":
        return max(candidates, key=mtime)
    return candidates[0]


def delete_duplicates(groups: list[list[str]], strategy: str,
                      assume_yes: bool, log_path: str | None,
                      interactive: bool, recycle: bool = False,
                      prefer: list[str] | None = None,
                      dry_run: bool = False) -> None:
    """Delete redundant copies, keeping one per group. Reports savings first."""
    # Build the deletion plan.
    plan: list[tuple[str, list[str]]] = []  # (keeper, [to_delete])
    planned_bytes = 0
    fallback_groups = 0   # groups with no preferred copy
    for g in groups:
        keeper = choose_keeper(g, strategy, prefer)
        if prefer and not any(t.lower() in keeper.lower() for t in prefer):
            fallback_groups += 1
        victims = [p for p in g if p != keeper]
        if not victims:
            continue
        plan.append((keeper, victims))
        try:
            planned_bytes += os.path.getsize(keeper) * len(victims)
        except OSError:
            pass

    if not plan:
        print("Nothing to delete.")
        return

    if prefer:
        print(f"\nPreferring to keep copies matching: {', '.join(prefer)}")
        print(f"  Groups kept on a preferred copy : {len(plan) - fallback_groups:,}")
        print(f"  Groups with no preferred copy (fell back to '{strategy}'): "
              f"{fallback_groups:,}")

    if dry_run:
        files_to_go = sum(len(v) for _, v in plan)
        print(f"\n[DRY RUN] No files will be removed.")
        print(f"  Groups affected : {len(plan):,}")
        print(f"  Files that would be removed : {files_to_go:,}")
        print(f"  Space that would be reclaimed: {human(planned_bytes)}")
        return

    action = "recycle" if recycle else "delete"
    print(f"\nDeletion plan (keep strategy: {strategy}):")
    print(f"  Groups affected : {len(plan):,}")
    print(f"  Files to {action}: {sum(len(v) for _, v in plan):,}")
    print(f"  Space to reclaim: {human(planned_bytes)}"
          + ("  (files go to the Recycle Bin)" if recycle else ""))

    if interactive:
        _interactive_delete(plan, log_path, recycle)
        return

    if not assume_yes:
        verb = "recycling" if recycle else "deletion"
        ans = input(f"\nProceed with {verb}? Type 'yes' to confirm: ").strip().lower()
        if ans != "yes":
            print("Aborted. No files removed.")
            return

    deleted, freed, errors = _do_delete(
        [(v, keeper) for keeper, vics in plan for v in vics], recycle
    )
    _finish(deleted, freed, errors, log_path, recycle)


def _interactive_delete(plan: list[tuple[str, list[str]]], log_path: str | None,
                        recycle: bool = False) -> None:
    to_delete: list[tuple[str, str]] = []  # (victim, keeper)
    for keeper, victims in plan:
        try:
            size = os.path.getsize(keeper)
        except OSError:
            size = 0
        print("\n" + "-" * 60)
        print(f"KEEP:   {keeper}")
        for v in victims:
            print(f"delete: {v}")
        print(f"reclaims {human(size * len(victims))}")
        verb = "recycle" if recycle else "delete"
        ans = input(f"[y] {verb} these  [n] skip  [q] quit: ").strip().lower()
        if ans == "q":
            break
        if ans == "y":
            to_delete.extend((v, keeper) for v in victims)
    if not to_delete:
        print("No files selected. Nothing removed.")
        return
    deleted, freed, errors = _do_delete(to_delete, recycle)
    _finish(deleted, freed, errors, log_path, recycle)


def _do_delete(items: list[tuple[str, str]], recycle: bool = False
               ) -> tuple[list[dict], int, list[str]]:
    deleted: list[dict] = []
    freed = 0
    errors: list[str] = []
    for victim, keeper in items:
        try:
            size = os.path.getsize(victim)
            if recycle:
                send_to_recycle(victim)
            else:
                os.remove(victim)
            freed += size
            deleted.append({"deleted": victim, "kept": keeper, "bytes": size,
                            "recycled": recycle})
        except (OSError, RuntimeError) as e:
            errors.append(f"{victim}: {e}")
    return deleted, freed, errors


def _finish(deleted: list[dict], freed: int, errors: list[str],
            log_path: str | None, recycle: bool = False) -> None:
    verb = "Recycled" if recycle else "Deleted"
    print(f"\n{verb} {len(deleted):,} files | reclaimed {human(freed)}"
          + ("  (recoverable from Recycle Bin)" if recycle else ""))
    if errors:
        action = "recycled" if recycle else "deleted"
        print(f"! {len(errors)} file(s) could not be {action}:")
        for e in errors[:20]:
            print(f"    {e}")
        if len(errors) > 20:
            print(f"    ...and {len(errors) - 20} more")
    if log_path and deleted:
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(deleted, f, indent=2)
        print(f"Undo log written to {log_path}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dupe_finder",
        description="Find and remove duplicate files across drives.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-p", "--path", action="append", dest="paths", metavar="DIR",
                   help="Directory to scan (repeatable). Default: all drives.")
    p.add_argument("--min-size", default="1", metavar="SIZE",
                   help="Ignore files smaller than this (e.g. 1MB). Default 1 byte.")
    p.add_argument("--max-size", default=None, metavar="SIZE",
                   help="Ignore files larger than this.")
    p.add_argument("-t", "--type", "--ext", action="append", default=[],
                   dest="types", metavar="EXT",
                   help="Only scan these file extensions, e.g. --type jpg,png "
                        "(repeatable; leading dot optional). Default: all files.")
    p.add_argument("--exclude", action="append", default=[], metavar="SUBSTR",
                   help="Skip paths containing this substring (repeatable).")
    p.add_argument("--follow-symlinks", action="store_true",
                   help="Follow symbolic links (off by default).")
    p.add_argument("--delete", action="store_true",
                   help="Enter deletion mode after reporting.")
    p.add_argument("--recycle", action="store_true",
                   help="Send removed files to the Recycle Bin instead of "
                        "deleting permanently (implies --delete).")
    p.add_argument("--keep", choices=KEEP_STRATEGIES, default="oldest",
                   help="Which copy to keep per group. Default: oldest.")
    p.add_argument("--prefer", action="append", default=[], metavar="SUBSTR",
                   help="Prefer keeping copies whose path contains this "
                        "substring, e.g. --prefer L: (repeatable; earlier "
                        "values win). Groups with no match fall back to --keep.")
    p.add_argument("--dry-run", action="store_true",
                   help="Show the deletion plan (counts and space) without "
                        "removing anything.")
    p.add_argument("--interactive", action="store_true",
                   help="Confirm each group individually before deleting.")
    p.add_argument("--yes", action="store_true",
                   help="Skip the final confirmation prompt (dangerous).")
    p.add_argument("--log", default=None, metavar="FILE",
                   help="Write a JSON log of deleted files.")
    p.add_argument("--workers", type=int, default=default_workers(), metavar="N",
                   help=f"Parallel hashing threads (default: {default_workers()}). "
                        "Use 1 for spinning disks where parallel reads thrash.")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="Suppress progress output.")
    return p


def main(argv: list[str] | None = None) -> int:
    # Windows consoles often use a legacy codepage (cp1252) that can't encode
    # every character in a filename. Never let printing a path crash the run.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)

    roots = args.paths if args.paths else default_roots()
    for r in roots:
        if not os.path.exists(r):
            print(f"! Path does not exist, skipping: {r}", file=sys.stderr)
    roots = [r for r in roots if os.path.exists(r)]
    if not roots:
        print("No valid paths to scan.", file=sys.stderr)
        return 2

    # Sensible default exclusions on Windows to avoid churning system files.
    default_excludes = []
    if os.name == "nt" and not args.paths:
        default_excludes = [
            "\\Windows\\", "\\$Recycle.Bin", "\\System Volume Information",
            "\\ProgramData\\", "\\AppData\\",
        ]
    excludes = default_excludes + args.exclude

    extensions = normalize_extensions(args.types)

    verbose = not args.quiet
    print(f"Scanning: {', '.join(roots)}")
    if extensions:
        print(f"File types: {', '.join(sorted(extensions))}")
    if excludes:
        print(f"Excluding paths containing: {', '.join(excludes)}")

    scanner = Scanner(
        roots=roots,
        min_size=parse_size(args.min_size),
        max_size=parse_size(args.max_size) if args.max_size else None,
        follow_symlinks=args.follow_symlinks,
        exclude=excludes,
        extensions=extensions,
        verbose=verbose,
    )

    by_size = scanner.collect_by_size()
    if verbose:
        print(f"Size groups with potential dupes: {len(by_size):,}", file=sys.stderr)

    groups = find_duplicates(by_size, workers=max(1, args.workers), verbose=verbose)
    total_waste = report(groups)

    do_delete = args.delete or args.recycle or args.dry_run
    if do_delete and groups and total_waste > 0:
        delete_duplicates(
            groups,
            strategy=args.keep,
            assume_yes=args.yes,
            log_path=args.log,
            interactive=args.interactive,
            recycle=args.recycle,
            prefer=args.prefer,
            dry_run=args.dry_run,
        )
    elif do_delete:
        print("Nothing to delete.")

    return 0


def run() -> int:
    """Console-script entry point (see pyproject.toml). Handles Ctrl-C cleanly."""
    try:
        return main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(run())
