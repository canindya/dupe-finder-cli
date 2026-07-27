#!/usr/bin/env python3
"""
dupe_finder - Find and remove duplicate files across drives.

Strategy (thorough, safe) - the default:
  1. Group candidate files by exact size (cheap - no reads).
  2. Within each size group, group by a partial hash of the first 64 KB.
  3. Within each partial-hash group, group by a full-content hash.
  Files sharing the same full hash are byte-for-byte duplicates.

Strategy (--fast) - a lightweight first pass:
  Group files by (file name, exact size) and read nothing at all. This is
  seconds instead of minutes, but it is a GUESS: two files with the same name
  and size can still have different contents. Use it to survey a drive, then
  confirm anything you intend to delete with a normal (hashing) run.

Deletion always keeps exactly one copy per duplicate group and reports how
much space will be reclaimed *before* anything is removed. Nothing is deleted
without confirmation unless you pass --yes. At the confirmation prompt you can
also step through the groups one by one, or save the plan to a review file and
decide later - declining never throws the scan away.

Examples:
  # Scan every fixed drive, just report duplicates
  python dupe_finder.py

  # Scan specific folders
  python dupe_finder.py -p D:\\Photos -p "E:\\Backup"

  # Quick survey: match on name + size only, no content check
  python dupe_finder.py -p D:\\Photos --fast

  # Ignore tiny files, then interactively delete, keeping the oldest copy
  python dupe_finder.py --min-size 1MB --delete --keep oldest

  # Delete without prompting (careful!), writing an undo log
  python dupe_finder.py --delete --keep shortest-path --yes --log deleted.json

Output detail: a live progress line with percentage and ETA by default,
full per-file detail with -v/--verbose, nothing with -q/--quiet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import string
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

PARTIAL_READ = 64 * 1024   # bytes hashed for the quick partial pass
CHUNK = 1024 * 1024        # streaming chunk size for full hashing


def default_workers() -> int:
    """A sensible thread count for hashing (I/O + hashlib both release the GIL)."""
    return min(16, (os.cpu_count() or 2) * 2)


def default_cache_path() -> str:
    """Per-user location for the persistent full-hash cache."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
            os.path.expanduser("~"), ".cache")
    return os.path.join(base, "dupe_finder", "hashes.json")


class HashCache:
    """Persistent cache of full-content hashes keyed by (path, size, mtime).

    Only full hashes are cached (they dominate cost); partial hashes are cheap
    enough to always recompute. An entry is trusted only while a file's size and
    modification time are unchanged, so edited files are automatically re-hashed.
    Thread-safe for concurrent use during parallel hashing."""

    def __init__(self, path: str | None, enabled: bool = True):
        self.path = path
        self.enabled = enabled and path is not None
        self.data: dict[str, str] = {}
        self.hits = 0
        self.misses = 0
        self._dirty = False
        self._lock = threading.Lock()
        if self.enabled:
            self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data = loaded
        except (OSError, ValueError):
            self.data = {}  # missing or corrupt cache: start fresh

    @staticmethod
    def _key(path: str) -> str | None:
        try:
            st = os.stat(_fs_path(path))
        except OSError:
            return None
        # Key on the plain absolute path so entries stay valid across runs.
        return f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}"

    def get_or_compute(self, path: str) -> str | None:
        """Return the file's full hash, from cache if the entry is still valid,
        otherwise compute and remember it."""
        if not self.enabled:
            return hash_file(path)
        key = self._key(path)
        if key is None:
            return None
        cached = self.data.get(key)
        if cached is not None:
            with self._lock:
                self.hits += 1
            return cached
        h = hash_file(path)
        with self._lock:
            self.misses += 1
            if h is not None:
                self.data[key] = h
                self._dirty = True
        return h

    def save(self) -> None:
        if not (self.enabled and self._dirty):
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)  # atomic
        except OSError as e:
            print(f"! Could not write hash cache: {e}", file=sys.stderr)


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
    raise AssertionError("unreachable: the PB branch always returns")


def _fmt_clock(seconds: float | None) -> str:
    """Format a duration as M:SS or H:MM:SS. Returns '--:--' when unknown."""
    if seconds is None or seconds != seconds or seconds < 0 or seconds > 359_999:
        return "--:--"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


class Progress:
    """A one-line live progress indicator with percentage and ETA.

    Written to stderr so it never pollutes redirected report output. On a TTY it
    rewrites a single line; when redirected it prints an occasional line instead
    (rewriting would produce thousands of junk lines in a log file). Progress is
    measured in bytes when a byte total is known (far more accurate for hashing,
    where file sizes vary by orders of magnitude) and in files otherwise. A total
    of zero means "unknown" - only the running count and elapsed time are shown.
    Thread-safe: workers call advance() concurrently during parallel hashing."""

    TTY_INTERVAL = 0.2      # seconds between redraws on a terminal
    FILE_INTERVAL = 15.0    # seconds between appended lines when redirected

    def __init__(self, label: str, total: int = 0, total_bytes: int = 0,
                 enabled: bool = True):
        self.label = label
        self.total = total
        self.total_bytes = total_bytes
        self.enabled = enabled
        self.stream = sys.stderr
        try:
            self.tty = self.stream.isatty()
        except (AttributeError, ValueError):
            self.tty = False
        self.done_items = 0
        self.done_bytes = 0
        self.start = time.monotonic()
        self._last_draw = self.start  # wait one interval before the first draw
        self._width = 0
        self._lock = threading.Lock()

    def advance(self, nbytes: int = 0, items: int = 1) -> None:
        if not self.enabled:
            return
        with self._lock:
            self.done_items += items
            self.done_bytes += nbytes
            now = time.monotonic()
            interval = self.TTY_INTERVAL if self.tty else self.FILE_INTERVAL
            if now - self._last_draw < interval:
                return
            self._last_draw = now
            self._draw(now)

    def finish(self) -> None:
        """Draw the final state and end the line."""
        if not self.enabled:
            return
        with self._lock:
            self._draw(time.monotonic(), final=True)

    def _draw(self, now: float, final: bool = False) -> None:
        elapsed = now - self.start
        if self.total_bytes:
            frac = min(1.0, self.done_bytes / self.total_bytes)
            detail = f"{human(self.done_bytes)} / {human(self.total_bytes)}"
        elif self.total:
            frac = min(1.0, self.done_items / self.total)
            detail = f"{self.done_items:,} / {self.total:,} files"
        else:
            frac = None
            detail = f"{self.done_items:,} files"

        parts = [f"  {self.label}"]
        if frac is not None:
            parts.append(f"{frac * 100:5.1f}%")
        parts.append(detail)
        if final:
            parts.append(f"done in {_fmt_clock(elapsed)}")
        elif frac and elapsed > 1.0:
            parts.append(f"ETA {_fmt_clock(elapsed / frac - elapsed)}")
        else:
            parts.append(f"{_fmt_clock(elapsed)} elapsed")
        line = " | ".join(parts)

        if self.tty:
            pad = max(0, self._width - len(line))
            self.stream.write("\r" + line + " " * pad + ("\n" if final else ""))
            self._width = 0 if final else len(line)
        else:
            self.stream.write(line + "\n")
        try:
            self.stream.flush()
        except (OSError, ValueError):
            pass


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


# Friendly file-type categories, so users can say "movies" instead of listing
# a dozen extensions. Keys are the canonical names; CATEGORY_ALIASES map common
# synonyms onto them.
CATEGORIES: dict[str, list[str]] = {
    "movies": [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
               ".mpg", ".mpeg", ".3gp", ".ts", ".m2ts", ".vob", ".ogv", ".rm",
               ".rmvb", ".divx", ".asf", ".mts"],
    "music": [".mp3", ".flac", ".wav", ".aac", ".ogg", ".m4a", ".wma", ".aiff",
              ".aif", ".alac", ".opus", ".ape", ".mid", ".midi", ".amr"],
    "photos": [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".heic",
               ".heif", ".webp", ".svg", ".ico", ".raw", ".cr2", ".cr3", ".nef",
               ".arw", ".dng", ".orf", ".rw2", ".raf", ".psd"],
    "documents": [".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                  ".odt", ".ods", ".odp", ".rtf", ".txt", ".md", ".csv", ".tsv",
                  ".pages", ".key", ".numbers"],
    "ebooks": [".epub", ".mobi", ".azw", ".azw3", ".pdf", ".djvu", ".fb2",
               ".cbz", ".cbr", ".lit"],
    "archives": [".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz",
                 ".tbz2", ".iso", ".cab", ".arj", ".lz", ".lzma", ".z", ".zst"],
    "code": [".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp", ".cc",
             ".h", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt",
             ".scala", ".sh", ".html", ".css", ".json", ".xml", ".yaml", ".yml",
             ".sql", ".r", ".m", ".pl"],
}

CATEGORY_ALIASES: dict[str, str] = {
    "movie": "movies", "video": "movies", "videos": "movies", "film": "movies",
    "films": "movies",
    "audio": "music", "song": "music", "songs": "music", "sound": "music",
    "sounds": "music",
    "photo": "photos", "image": "photos", "images": "photos", "picture": "photos",
    "pictures": "photos", "pics": "photos", "pic": "photos",
    "document": "documents", "docs": "documents", "doc": "documents",
    "office": "documents",
    "ebook": "ebooks", "book": "ebooks", "books": "ebooks",
    "archive": "archives", "compressed": "archives", "zips": "archives",
    "source": "code", "sourcecode": "code", "src": "code",
}


def expand_categories(values: list[str] | None) -> set[str]:
    """Turn category names (with aliases, comma-separated or repeated) into a set
    of '.ext' strings. Unknown names print a warning listing valid categories."""
    exts: set[str] = set()
    for value in values or []:
        for token in value.replace(" ", ",").split(","):
            name = token.strip().lower()
            if not name:
                continue
            name = CATEGORY_ALIASES.get(name, name)
            if name in CATEGORIES:
                exts.update(CATEGORIES[name])
            else:
                print(f"! Unknown category '{token.strip()}'. Valid: "
                      f"{', '.join(sorted(CATEGORIES))}", file=sys.stderr)
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


def default_exclusions() -> tuple[list[str], list[str]]:
    """System locations to skip when scanning whole drives / the filesystem root.

    Returns ``(substrings, prefixes)``:
      * substrings match anywhere in a path (used on Windows, where the system
        drive letter varies),
      * prefixes match a path only at the filesystem root (anchored), so a user
        folder like ``/home/me/dev`` is never mistaken for ``/dev``.
    Only meaningful for a whole-root scan; skipped when explicit paths are given.
    """
    if os.name == "nt":
        return ([
            "\\Windows\\", "\\$Recycle.Bin", "\\System Volume Information",
            "\\ProgramData\\", "\\AppData\\",
        ], [])

    if sys.platform == "darwin":
        prefixes = [
            "/System", "/Library", "/private", "/Volumes", "/Applications",
            "/usr", "/bin", "/sbin", "/opt", "/cores", "/dev", "/.Spotlight-V100",
            "/.Trashes", "/.fseventsd",
        ]
        prefixes.append(os.path.join(os.path.expanduser("~"), "Library"))
        return ([], prefixes)

    # Linux / other POSIX — pseudo-filesystems first, then common system trees.
    return ([], [
        "/proc", "/sys", "/dev", "/run", "/boot", "/lost+found",
        "/usr", "/bin", "/sbin", "/lib", "/lib64", "/opt", "/snap",
        "/var", "/tmp",
    ])


def _fs_path(path: str) -> str:
    """Return a form of *path* usable past the Windows 260-char MAX_PATH limit.

    The ``\\\\?\\`` prefix tells Win32 to skip path parsing, which lifts the
    limit. It needs a fully-qualified backslash path; UNC shares take the
    ``\\\\?\\UNC\\`` form. No-op on POSIX and on already-prefixed paths.

    The scan finds these files (os.scandir has no such limit) but open(),
    os.stat() and os.remove() all fail on them without this, so every I/O
    boundary funnels through here. Only ever pass the result to the OS - the
    plain path stays the file's identity everywhere else, including cache keys
    that must survive across runs, and anything printed to the user."""
    if os.name != "nt" or path.startswith("\\\\?\\"):
        return path
    full = os.path.abspath(path)
    if full.startswith("\\\\"):                      # \\server\share\...
        return "\\\\?\\UNC\\" + full.lstrip("\\")
    return "\\\\?\\" + full


def _safe_size(path: str) -> int:
    """Size of *path*, or 0 if it cannot be read (vanished, denied)."""
    try:
        return os.path.getsize(_fs_path(path))
    except OSError:
        return 0


def hash_file(path: str, limit: int | None = None) -> str | None:
    """Hash a file's content (blake2b). If *limit*, hash only the first N bytes.
    Returns None on read errors (permission, locked, vanished)."""
    h = hashlib.blake2b()
    try:
        with open(_fs_path(path), "rb", buffering=0) as f:
            if limit is not None:
                h.update(f.read(limit))
            else:
                while chunk := f.read(CHUNK):
                    h.update(chunk)
    except OSError:  # PermissionError is an OSError subclass
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


def _unique_trash_name(files_dir: str, info_dir: str, name: str) -> str:
    """A base name free in BOTH <files_dir>/<name> and <info_dir>/<name>.trashinfo."""
    stem, ext = os.path.splitext(name)
    candidate = name
    i = 1
    while (os.path.lexists(os.path.join(files_dir, candidate))
           or os.path.lexists(os.path.join(info_dir, candidate + ".trashinfo"))):
        candidate = f"{stem}.{i}{ext}"
        i += 1
    return candidate


def _unique_dest(directory: str, name: str) -> str:
    """Return a filename in *directory* not already taken (bare name)."""
    stem, ext = os.path.splitext(name)
    candidate = name
    i = 1
    while os.path.lexists(os.path.join(directory, candidate)):
        candidate = f"{stem}.{i}{ext}"
        i += 1
    return candidate


def _trash_into(path: str, trash_dir: str) -> None:
    """FreeDesktop-style: move *path* into <trash_dir>/files and record a
    .trashinfo in <trash_dir>/info so it's restorable. Cross-platform-safe core
    (no OS-only calls) so it can be unit-tested anywhere."""
    import datetime
    import shutil
    import urllib.parse

    src = os.path.abspath(path)
    files_dir = os.path.join(trash_dir, "files")
    info_dir = os.path.join(trash_dir, "info")
    os.makedirs(files_dir, exist_ok=True)
    os.makedirs(info_dir, exist_ok=True)

    dest_name = _unique_trash_name(files_dir, info_dir, os.path.basename(src))

    when = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    info_path = os.path.join(info_dir, dest_name + ".trashinfo")
    with open(info_path, "w", encoding="utf-8") as f:
        f.write("[Trash Info]\n"
                f"Path={urllib.parse.quote(src)}\n"
                f"DeletionDate={when}\n")

    dest = os.path.join(files_dir, dest_name)
    try:
        os.rename(src, dest)               # fast path (same filesystem)
    except OSError:
        try:
            shutil.move(src, dest)          # cross-device
        except Exception:
            os.remove(info_path)            # don't leave orphan metadata
            raise


def _mount_point(path: str) -> str:
    path = os.path.abspath(path)
    while not os.path.ismount(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def _trash_freedesktop(path: str) -> None:
    """Linux/BSD Trash per the FreeDesktop spec: home trash when on the same
    filesystem, else the top-directory trash (<mount>/.Trash-$uid)."""
    src = os.path.abspath(path)
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    home_trash = os.path.join(xdg, "Trash")

    same_fs = False
    try:
        anchor = xdg if os.path.exists(xdg) else os.path.expanduser("~")
        same_fs = os.stat(src).st_dev == os.stat(anchor).st_dev
    except OSError:
        same_fs = True  # best effort: use home trash

    if same_fs:
        _trash_into(src, home_trash)
        return
    # File lives on another mount — trash on that mount so the move is a rename.
    mount = _mount_point(src)
    uid = os.getuid()  # POSIX-only; this branch only runs on POSIX
    _trash_into(src, os.path.join(mount, f".Trash-{uid}"))


def _trash_macos(path: str) -> None:
    """macOS: move into the user's Trash (~/.Trash). Recoverable via Finder."""
    import shutil
    src = os.path.abspath(path)
    trash = os.path.join(os.path.expanduser("~"), ".Trash")
    os.makedirs(trash, exist_ok=True)
    dest_name = _unique_dest(trash, os.path.basename(src))
    dest = os.path.join(trash, dest_name)
    try:
        os.rename(src, dest)
    except OSError:
        shutil.move(src, dest)  # file on another volume


def send_to_recycle(path: str) -> None:
    """Move *path* to the Recycle Bin / Trash — natively on every platform.

    Order of preference: the ``send2trash`` package if it happens to be
    installed (most battle-tested), then the OS-native implementation
    (Windows shell API, macOS ~/.Trash, FreeDesktop trash on Linux). No
    third-party dependency is required on any platform. Raises OSError on
    failure."""
    try:
        from send2trash import send2trash  # type: ignore
        send2trash(os.path.abspath(path))
        return
    except ImportError:
        pass
    if os.name == "nt":
        _recycle_via_ctypes(path)
    elif sys.platform == "darwin":
        _trash_macos(path)
    else:
        _trash_freedesktop(path)


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #
@dataclass
class Scanner:
    roots: list[str]
    min_size: int = 1
    max_size: int | None = None
    follow_symlinks: bool = False
    exclude: list[str] = field(default_factory=list)          # substring match
    exclude_prefixes: list[str] = field(default_factory=list)  # anchored at root
    extensions: set[str] = field(default_factory=set)  # e.g. {".jpg", ".png"}
    verbose: bool = False        # per-file detail lines
    show_progress: bool = False  # live one-line counter

    def __post_init__(self) -> None:
        # Lowercase the patterns once. _excluded() runs for every directory and
        # every file, so folding them per call costs millions of str.lower()
        # calls on a large scan.
        self._excl_low = tuple(x.lower() for x in self.exclude)
        self._prefix_low = tuple(p.lower().rstrip("/\\")
                                 for p in self.exclude_prefixes)

    def _excluded(self, path: str) -> bool:
        low = path.lower()
        for x in self._excl_low:
            if x in low:
                return True
        for p in self._prefix_low:
            if low == p or low.startswith(p + "/") or low.startswith(p + "\\"):
                return True
        return False

    def _wanted_type(self, name: str) -> bool:
        if not self.extensions:
            return True
        return os.path.splitext(name)[1].lower() in self.extensions

    def _iter_files(self):
        """Yield (path, size) for every file passing the type/size/exclude
        filters. Shared by both bucketing strategies so they can never drift.

        Uses os.scandir rather than os.walk: the directory enumeration already
        carries each entry's type and size, so DirEntry.stat() answers for free
        on Windows instead of costing a syscall. The previous os.walk version
        spent three calls per file (os.stat + os.path.isfile + os.path.islink);
        dropping to one measured ~8.5x faster on a 350k-file tree. It also
        stopped silently discarding paths longer than MAX_PATH (260 chars),
        which os.stat cannot open but scandir reports correctly.

        Do not reintroduce isfile()/islink() here - entry.is_file() and
        entry.is_dir() with an explicit follow_symlinks express the same policy
        without re-resolving the path."""
        # The walk has no cheap way to know its total up front, so progress here
        # is an indeterminate running count rather than a percentage.
        prog = Progress("scanning ", enabled=self.show_progress)
        follow = self.follow_symlinks
        seen_files = 0

        for root in self.roots:
            if self._excluded(root):
                continue
            stack = [root]
            while stack:
                current = stack.pop()
                try:
                    with os.scandir(current) as it:
                        # Sorted so results are reproducible: enumeration order
                        # is filesystem-dependent and would otherwise decide
                        # --keep first / --prefer tie-breaks by luck.
                        entries = sorted(it, key=lambda e: e.name)
                except OSError:
                    continue  # unreadable directory: skip, never abort the scan

                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=follow):
                            if not self._excluded(entry.path):
                                stack.append(entry.path)
                            continue
                        # is_file(follow_symlinks=False) is False for symlinks,
                        # which is exactly the old policy of skipping them
                        # unless --follow-symlinks was given.
                        if not entry.is_file(follow_symlinks=follow):
                            continue
                        if not self._wanted_type(entry.name):
                            continue
                        if self._excluded(entry.path):
                            continue
                        size = entry.stat(follow_symlinks=follow).st_size
                    except OSError:
                        continue  # vanished or permission denied mid-walk

                    if size < self.min_size:
                        continue
                    if self.max_size is not None and size > self.max_size:
                        continue

                    yield entry.path, size
                    seen_files += 1
                    prog.advance()
                    if self.verbose and seen_files % 5000 == 0:
                        print(f"  ...scanned {seen_files:,} files", file=sys.stderr)
        prog.finish()

    def collect_by_size(self) -> dict[int, list[str]]:
        """Walk all roots, bucketing files by size."""
        by_size: dict[int, list[str]] = defaultdict(list)
        for full, size in self._iter_files():
            by_size[size].append(full)
        # keep only sizes with >1 file — a unique size cannot have a duplicate
        return {s: paths for s, paths in by_size.items() if len(paths) > 1}

    def collect_by_name_size(self) -> dict[tuple[str, int], list[str]]:
        """Walk all roots, bucketing files by (lowercased name, size).

        This is the whole of --fast mode's matching: no file is ever opened.
        Names are casefolded because Windows filesystems are case-insensitive
        and copies frequently differ only in case."""
        buckets: dict[tuple[str, int], list[str]] = defaultdict(list)
        for full, size in self._iter_files():
            buckets[(os.path.basename(full).casefold(), size)].append(full)
        return {k: paths for k, paths in buckets.items() if len(paths) > 1}


def _hash_many(paths: list[str], hash_fn, workers: int, label: str,
               verbose: bool, progress: "Progress | None" = None,
               sizes: dict[str, int] | None = None) -> dict[str, str]:
    """Apply *hash_fn(path) -> str|None* to every path concurrently.

    Returns {path: hash}, silently dropping paths that hash to None (unreadable
    files). Falls back to a plain sequential loop when workers <= 1 (useful for
    spinning disks, where parallel reads can thrash the head). When *progress*
    is given it is advanced per file, by *sizes[path]* bytes if known so the
    percentage tracks work actually done rather than files touched."""
    result: dict[str, str] = {}
    done = 0
    total = len(paths)

    def _tick(path: str) -> None:
        nonlocal done
        done += 1
        if progress is not None:
            progress.advance(sizes.get(path, 0) if sizes else 0)
        if verbose and done % 2000 == 0:
            print(f"  ...{label} {done:,}/{total:,}", file=sys.stderr)

    if workers <= 1:
        for p in paths:
            h = hash_fn(p)
            if h is not None:
                result[p] = h
            _tick(p)
        return result

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(hash_fn, p): p for p in paths}
        for fut in as_completed(futures):
            h = fut.result()
            if h is not None:
                result[futures[fut]] = h
            _tick(futures[fut])
    return result


def find_duplicates(by_size: dict[int, list[str]], workers: int = 1,
                    cache: "HashCache | None" = None,
                    verbose: bool = False,
                    show_progress: bool = False) -> list[list[str]]:
    """Refine size groups into confirmed duplicate groups.

    Two content passes, each hashing in parallel: a cheap partial hash (first
    64 KB) to weed out obvious non-matches, then a full-content hash to confirm.
    Hashes are only ever compared within the same size, so distinct files that
    happen to share a partial hash are never falsely merged. The full pass reads
    from *cache* (keyed by path+size+mtime) when provided, so unchanged files
    are not re-hashed on repeat runs."""
    size_of = {p: size for size, paths in by_size.items() for p in paths}
    candidates = list(size_of)

    # Pass 1: partial hash everything (always fresh — cheap), bucket by
    # (size, partial-hash).
    if verbose:
        print(f"  hashing {len(candidates):,} candidates (partial pass, "
              f"{workers} worker(s))", file=sys.stderr)
    # The partial pass reads a fixed 64 KB per file, so files are the honest
    # unit of work here; the full pass below is byte-bound instead.
    prog = Progress("quick scan", total=len(candidates), enabled=show_progress)
    partial = _hash_many(candidates, lambda p: hash_file(p, PARTIAL_READ),
                         workers, "partial-hashed", verbose, prog)
    prog.finish()
    partial_buckets: dict[tuple[int, str], list[str]] = defaultdict(list)
    for p, ph in partial.items():
        partial_buckets[(size_of[p], ph)].append(p)

    survivors = [p for grp in partial_buckets.values() if len(grp) > 1 for p in grp]

    # Pass 2: full hash only survivors (cache-backed), bucket by (size, full-hash).
    #
    # A file at or below PARTIAL_READ was already read end-to-end by the partial
    # pass, so its partial digest IS its full digest - the same bytes through
    # the same blake2b. Reuse it rather than reading the file a second time.
    # This is safe because a bucket key carries the size: every member of a
    # bucket has one size, so a bucket is either entirely promoted or entirely
    # freshly hashed, and the two kinds are never compared against each other.
    promoted = {p: partial[p] for p in survivors if size_of[p] <= PARTIAL_READ}
    to_hash = [p for p in survivors if size_of[p] > PARTIAL_READ]

    if verbose:
        print(f"  confirming {len(to_hash):,} candidates (full pass); "
              f"{len(promoted):,} small file(s) already hashed in full",
              file=sys.stderr)

    full: dict[str, str] = dict(promoted)
    if to_hash:
        full_fn = (cache.get_or_compute if cache is not None
                   else (lambda p: hash_file(p)))
        prog = Progress("full scan ", total=len(to_hash), enabled=show_progress,
                        total_bytes=sum(size_of[p] for p in to_hash))
        full.update(_hash_many(to_hash, full_fn, workers, "full-hashed",
                               verbose, prog, size_of))
        prog.finish()

    full_buckets: dict[tuple[int, str], list[str]] = defaultdict(list)
    for p, fh in full.items():
        full_buckets[(size_of[p], fh)].append(p)

    return [grp for grp in full_buckets.values() if len(grp) > 1]


def find_duplicates_fast(by_name_size: dict[tuple[str, int], list[str]]
                         ) -> list[list[str]]:
    """--fast mode: treat same-name + same-size files as duplicates.

    No file is opened, so this is orders of magnitude quicker than hashing --
    and strictly a heuristic. Same name + same size is strong evidence for
    copies made by a file manager or a backup tool, but it is NOT proof: two
    different edits of report.docx can easily land on the same byte count.
    Callers must label these groups as unverified (see report())."""
    return [sorted(paths) for paths in by_name_size.values() if len(paths) > 1]


FAST_WARNING = (
    "!! FAST MODE: files were matched on NAME + SIZE only. Their contents were\n"
    "!! never read, so these groups are LIKELY duplicates, not confirmed ones.\n"
    "!! Re-run without --fast to verify by content before deleting anything."
)

# Short form for the deletion plan, where the full banner has just been printed
# by report() a few lines above.
FAST_UNVERIFIED_NOTE = (
    "!! These matches are UNVERIFIED (--fast: name + size only, contents never "
    "read)."
)


# --------------------------------------------------------------------------- #
# Reporting & deletion
# --------------------------------------------------------------------------- #
def group_wasted(size: int, count: int) -> int:
    """Space reclaimable in a group of *count* identical files: all but one."""
    return size * (count - 1)


TOP_GROUPS = 20  # groups listed in non-verbose mode; -v lists them all


def report(groups: list[list[str]], verified: bool = True,
           limit: int | None = None) -> int:
    """Print duplicate groups and return total reclaimable bytes.

    *limit* caps how many groups are listed (largest first); the totals always
    cover every group. Pass None to list them all (-v). *verified* is False for
    --fast results, which are labelled as unconfirmed."""
    if not groups:
        print("\nNo duplicate files found.")
        return 0

    # Size each group exactly once, tolerating files that vanished between the
    # scan and now, then rank by reclaimable space. Sizing inside the sort key
    # would stat twice per group and let an OSError escape from the sort.
    ranked: list[tuple[int, int, list[str]]] = []
    for g in groups:
        size = _safe_size(g[0])
        if not size:
            continue  # keeper vanished or unreadable; nothing useful to report
        ranked.append((group_wasted(size, len(g)), size, g))
    ranked.sort(key=lambda r: r[0], reverse=True)

    if not ranked:
        print("\nNo duplicate files found.")
        return 0

    total_waste = 0
    total_dupes = 0
    heading = "copies" if verified else "likely copies"
    listed = 0
    for i, (waste, size, g) in enumerate(ranked, 1):
        total_waste += waste
        total_dupes += len(g) - 1
        if limit is not None and listed >= limit:
            continue  # keep totalling, just stop printing
        listed += 1
        print(f"\n[{i}] {len(g)} {heading} | {human(size)} each | "
              f"reclaimable {human(waste)}")
        for p in g:
            print(f"      {p}")

    if limit is not None and len(ranked) > listed:
        print(f"\n...and {len(ranked) - listed:,} more group(s). "
              "Use -v to list them all.")

    print("\n" + "=" * 60)
    label = "Duplicate groups " if verified else "Possible dupe groups"
    print(f"{label}: {len(ranked):,}")
    print(f"Redundant files  : {total_dupes:,}")
    print(f"Reclaimable space: {human(total_waste)}"
          + ("" if verified else "  (if the matches are real)"))
    print("=" * 60)
    if not verified:
        print(FAST_WARNING)
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
            return os.path.getmtime(_fs_path(p))
        except OSError:
            return 0.0
    if strategy == "oldest":
        return min(candidates, key=mtime)
    if strategy == "newest":
        return max(candidates, key=mtime)
    return candidates[0]


DEFAULT_REVIEW = "duplicates_review.txt"


def write_review(plan: list[tuple[str, list[str]]], path: str | None,
                 strategy: str, recycle: bool, verified: bool,
                 explicit: bool = False) -> str | None:
    """Write a human-readable review of the deletion plan and return its path.

    Called whenever the user declines the confirmation, so a long scan is never
    thrown away: the exact plan - which copy would be kept and which removed -
    is on disk to read at leisure. When *path* was not chosen by the user
    (*explicit* False) an unused filename is picked rather than clobbering an
    existing file."""
    target = path or DEFAULT_REVIEW
    if not explicit and os.path.lexists(target):
        directory = os.path.dirname(target)
        base = _unique_dest(directory or ".", os.path.basename(target))
        target = os.path.join(directory, base) if directory else base
    action = "RECYCLE" if recycle else "DELETE"
    total_files = sum(len(v) for _, v in plan)
    total_bytes = 0

    try:
        with open(target, "w", encoding="utf-8", errors="replace") as f:
            f.write("Duplicate review - generated by dupe_finder\n")
            f.write(f"Keep strategy   : {strategy}\n")
            f.write(f"Pending action  : {action} the lines marked 'remove'\n")
            if not verified:
                f.write("\n" + FAST_WARNING + "\n")
            f.write("\nNothing has been removed. Re-run the same command and\n"
                    "confirm at the prompt to carry this plan out (cached\n"
                    "hashes make the second run much faster), or use\n"
                    "--interactive to work through it group by group.\n")
            f.write("\n" + "=" * 70 + "\n")

            # Biggest wins first, so a reader can stop part-way and still have
            # seen everything that matters.
            ordered = sorted(
                plan,
                key=lambda kv: _safe_size(kv[0]) * len(kv[1]), reverse=True)
            for i, (keeper, victims) in enumerate(ordered, 1):
                size = _safe_size(keeper)
                waste = size * len(victims)
                total_bytes += waste
                f.write(f"\n[{i}] {len(victims) + 1} copies | {human(size)} each"
                        f" | reclaimable {human(waste)}\n")
                f.write(f"    KEEP    {keeper}\n")
                for v in victims:
                    f.write(f"    remove  {v}\n")

            f.write("\n" + "=" * 70 + "\n")
            done_word = "recycled" if recycle else "deleted"
            f.write(f"Groups affected              : {len(plan):,}\n")
            f.write(f"Files that would be {done_word:<9s}: {total_files:,}\n")
            f.write(f"Space that would be reclaimed: {human(total_bytes)}\n")
    except OSError as e:
        print(f"! Could not write review file: {e}", file=sys.stderr)
        return None
    return target


def _confirm_action(plan: list[tuple[str, list[str]]], phrase: str, verb: str,
                    action: str, strategy: str, recycle: bool, verified: bool,
                    review_path: str | None, review_explicit: bool) -> str:
    """Ask what to do with the plan. Returns 'go', 'interactive' or 'abort'.

    Declining used to end the run outright, discarding a scan that may have
    taken many minutes. Instead this offers a way forward: act now, step
    through the groups one by one, or save the plan and read it first. An
    unrecognised answer re-prompts rather than silently aborting - but it never
    falls through to deleting."""
    total_files = sum(len(v) for _, v in plan)
    saved: str | None = None   # reuse a review saved earlier in this loop
    while True:
        print(f"\nProceed with {verb}?")
        print(f"  {phrase:<18s} - {action} all {total_files:,} file(s) above")
        print(f"  {'i':<18s} - review and decide group by group")
        print(f"  {'s':<18s} - save a review file, then ask again")
        print(f"  {'n':<18s} - abort (a review file is saved first)")
        try:
            ans = input("Choice: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()  # end the prompt line
            ans = "n"

        if ans == phrase:
            return "go"
        if ans == "i":
            return "interactive"
        if ans in ("n", "no", "q", "quit", "abort", ""):
            if saved is None:
                saved = write_review(plan, review_path, strategy, recycle,
                                     verified, review_explicit)
            print("\nAborted. No files removed.")
            if saved:
                print(f"Review of the full plan written to: {saved}")
                print("Re-run the same command when you've decided - cached "
                      "hashes make it much faster.")
            return "abort"
        if ans == "s":
            if saved is None:
                saved = write_review(plan, review_path, strategy, recycle,
                                     verified, review_explicit)
            print(f"\nReview written to: {saved}" if saved else "")
            continue
        print(f"! Unrecognised choice '{ans}'. Nothing has been removed.")


def delete_duplicates(groups: list[list[str]], strategy: str,
                      assume_yes: bool, log_path: str | None,
                      interactive: bool, recycle: bool = False,
                      prefer: list[str] | None = None,
                      dry_run: bool = False, verified: bool = True,
                      review_path: str | None = None,
                      review_explicit: bool = False,
                      review_only: bool = False) -> None:
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
        planned_bytes += _safe_size(keeper) * len(victims)

    if not plan:
        print("Nothing to delete.")
        return

    if prefer:
        print(f"\nPreferring to keep copies matching: {', '.join(prefer)}")
        print(f"  Groups kept on a preferred copy : {len(plan) - fallback_groups:,}")
        print(f"  Groups with no preferred copy (fell back to '{strategy}'): "
              f"{fallback_groups:,}")

    # Both --dry-run and a standalone --review build the full plan, write it
    # out and stop: no prompt, nothing removed.
    if dry_run or review_only:
        files_to_go = sum(len(v) for _, v in plan)
        print("\n[DRY RUN] No files will be removed." if dry_run
              else "\nReview only. No files will be removed.")
        print(f"  Groups affected : {len(plan):,}")
        print(f"  Files that would be removed : {files_to_go:,}")
        print(f"  Space that would be reclaimed: {human(planned_bytes)}")
        if not verified:
            print(f"\n{FAST_UNVERIFIED_NOTE}")
        saved = write_review(plan, review_path, strategy, recycle, verified,
                             review_explicit)
        if saved:
            print(f"  Full plan written to: {saved}")
        return

    action = "recycle" if recycle else "delete"
    print(f"\nDeletion plan (keep strategy: {strategy}):")
    print(f"  Groups affected : {len(plan):,}")
    print(f"  Files to {action}: {sum(len(v) for _, v in plan):,}")
    print(f"  Space to reclaim: {human(planned_bytes)}"
          + ("  (files go to the Recycle Bin)" if recycle else ""))
    if not verified:
        print(f"\n{FAST_UNVERIFIED_NOTE}")
        if not recycle:
            print("!! Deleting them permanently is IRREVERSIBLE. Consider "
                  "--recycle.")

    review_kw = dict(strategy=strategy, verified=verified,
                     review_path=review_path, review_explicit=review_explicit)

    if interactive:
        _interactive_delete(plan, log_path, recycle, **review_kw)
        return

    if not assume_yes:
        verb = "recycling" if recycle else "deletion"
        # Unverified matches demand a deliberate phrase, not a reflexive 'yes'.
        phrase = "yes" if verified else "delete unverified"
        choice = _confirm_action(plan, phrase, verb, action, strategy, recycle,
                                 verified, review_path, review_explicit)
        if choice == "abort":
            return
        if choice == "interactive":
            _interactive_delete(plan, log_path, recycle, **review_kw)
            return

    deleted, freed, errors = _do_delete(
        [(v, keeper) for keeper, vics in plan for v in vics], recycle
    )
    _finish(deleted, freed, errors, log_path, recycle)


def _interactive_delete(plan: list[tuple[str, list[str]]], log_path: str | None,
                        recycle: bool = False, *, strategy: str = "",
                        verified: bool = True, review_path: str | None = None,
                        review_explicit: bool = False) -> None:
    """Step through the plan one group at a time.

    Like the confirm menu, this must never leave a completed scan with nothing
    to show for it: 's' saves the plan mid-review, and stopping without acting
    writes a review of whatever was left undecided."""
    to_delete: list[tuple[str, str]] = []  # (victim, keeper)
    verb = "recycle" if recycle else "delete"
    total = len(plan)
    saved: str | None = None  # reuse one review file across repeated saves
    i = 0

    def _save(groups: list[tuple[str, list[str]]], what: str) -> str | None:
        """Write a review once and report it. Later calls reuse that file rather
        than spawning duplicates.1.txt, duplicates.2.txt, ..."""
        nonlocal saved
        if saved is not None:
            print(f"Review already written to: {saved}")
            return saved
        saved = write_review(groups, review_path, strategy, recycle,
                             verified, review_explicit)
        if saved:
            print(f"Review of {what} written to: {saved}")
        return saved

    while i < total:
        keeper, victims = plan[i]
        size = _safe_size(keeper)
        print("\n" + "-" * 60)
        print(f"[{i + 1}/{total}]")
        print(f"KEEP:   {keeper}")
        for v in victims:
            print(f"{verb}: {v}")
        print(f"reclaims {human(size * len(victims))}")
        try:
            ans = input(f"[y] {verb} these  [n] skip  "
                        f"[a] {verb} these and ALL remaining  "
                        f"[s] save review  [q] stop: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            ans = "q"

        if ans == "s":
            # Save the whole plan and re-show this group; don't advance.
            _save(plan, f"all {total:,} group(s)")
            continue
        if ans == "q":
            # Leave a record of what was never decided on.
            remaining = plan[i:]
            print("Stopped.")
            if remaining:
                _save(remaining, f"the {len(remaining):,} undecided group(s)")
            break
        if ans == "a":
            # Accept this group and every one left, without more prompting.
            for k, vs in plan[i:]:
                to_delete.extend((v, k) for v in vs)
            break
        if ans == "y":
            to_delete.extend((v, keeper) for v in victims)
        i += 1

    if not to_delete:
        print("No files selected. Nothing removed.")
        if saved is None:  # a 'q' or 's' earlier already reported one
            _save(plan, "the full plan")
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
            size = _safe_size(victim)
            if recycle:
                send_to_recycle(victim)
            else:
                os.remove(_fs_path(victim))
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
    p.add_argument("-t", "--type", action="append", default=[],
                   dest="types", metavar="EXT",
                   help="Only scan these file extensions, e.g. --type jpg,png "
                        "(repeatable; leading dot optional). Default: all files.")
    p.add_argument("-c", "--category", action="append", default=[],
                   dest="categories", metavar="NAME",
                   help="Only scan a file-type category: "
                        f"{', '.join(sorted(CATEGORIES))} (with synonyms like "
                        "'video', 'audio', 'images'). Repeatable/comma-separated; "
                        "combines with --type.")
    p.add_argument("--fast", action="store_true", dest="fast",
                   help="Lightweight first pass: match files on NAME + SIZE "
                        "only, without reading their contents. Much faster, but "
                        "results are unverified guesses - confirm with a normal "
                        "run before deleting.")
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
    p.add_argument("--review", default=None, metavar="FILE",
                   help="Write a human-readable review of the plan (which copy "
                        "is kept, which are removed) to FILE. On its own this "
                        "is a read-only mode: nothing is deleted and you are "
                        "never prompted. A review is also written automatically "
                        "on --dry-run, and whenever you decline or stop a "
                        f"deletion, so a long scan is never wasted (default "
                        f"location: ./{DEFAULT_REVIEW}).")
    p.add_argument("--workers", type=int, default=default_workers(), metavar="N",
                   help=f"Parallel hashing threads (default: {default_workers()}). "
                        "Use 1 for spinning disks where parallel reads thrash.")
    p.add_argument("--cache", default=None, metavar="FILE",
                   help="Persistent hash cache file, so unchanged files are not "
                        "re-hashed on repeat runs. Pass 'none' to disable "
                        "caching for this run (for a file actually named none, "
                        f"use ./none). Default: {default_cache_path()}")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Show full detail: every duplicate group and per-file "
                        "scanning/hashing lines. Default is a single progress "
                        "line with a percentage and ETA.")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="Suppress all progress output (report only).")
    return p


# Spellings dropped in 2.0, mapped to what replaced them. argparse would only
# say "unrecognized arguments", which leaves the user guessing; these give the
# answer instead.
REMOVED_OPTIONS: dict[str, str] = {
    "--ext": "--type",
    "--kind": "--category",
    "--quick": "--fast",
    "--name-size": "--fast",
    "--no-cache": "--cache none",
}


def check_removed_options(argv: list[str]) -> str | None:
    """Return an error message if *argv* uses an option removed in 2.0."""
    for arg in argv:
        name = arg.split("=", 1)[0]  # handle --ext=jpg as well as --ext jpg
        if name in REMOVED_OPTIONS:
            return (f"{name} was removed in dupe_finder 2.0. "
                    f"Use {REMOVED_OPTIONS[name]} instead.")
    return None


def main(argv: list[str] | None = None) -> int:
    # Windows consoles often use a legacy codepage (cp1252) that can't encode
    # every character in a filename. Never let printing a path crash the run.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    problem = check_removed_options(sys.argv[1:] if argv is None else argv)
    if problem:
        print(f"! {problem}", file=sys.stderr)
        return 2

    args = build_parser().parse_args(argv)

    requested = args.paths if args.paths else default_roots()
    roots = []
    for r in requested:
        if os.path.exists(r):
            roots.append(r)
        else:
            print(f"! Path does not exist, skipping: {r}", file=sys.stderr)
    if not roots:
        print("No valid paths to scan.", file=sys.stderr)
        return 2

    # Skip system locations when scanning whole drives / the filesystem root,
    # unless the user chose explicit paths. Platform-aware (see default_exclusions).
    default_subs, default_prefixes = ([], [])
    if not args.paths:
        default_subs, default_prefixes = default_exclusions()
    excludes = default_subs + args.exclude
    exclude_prefixes = default_prefixes

    extensions = normalize_extensions(args.types) | expand_categories(args.categories)

    # Three levels of output: -q (silent), default (one live progress line),
    # -v (per-file detail plus every duplicate group). -q wins if both given.
    verbose = args.verbose and not args.quiet
    show_progress = not args.quiet and not args.verbose

    print(f"Scanning: {', '.join(roots)}"
          + ("   [FAST: name + size only]" if args.fast else ""))
    if args.categories:
        print(f"Categories: {', '.join(args.categories)}")
    if extensions:
        print(f"File types: {', '.join(sorted(extensions))}")
    if excludes:
        print(f"Excluding paths containing: {', '.join(excludes)}")
    if exclude_prefixes:
        print(f"Excluding system locations: {', '.join(exclude_prefixes)}")

    # stdout is block-buffered when redirected; flush so the header above can't
    # land after the stderr progress lines in a log file.
    sys.stdout.flush()

    scanner = Scanner(
        roots=roots,
        min_size=parse_size(args.min_size),
        max_size=parse_size(args.max_size) if args.max_size else None,
        follow_symlinks=args.follow_symlinks,
        exclude=excludes,
        exclude_prefixes=exclude_prefixes,
        extensions=extensions,
        verbose=verbose,
        show_progress=show_progress,
    )

    if args.fast:
        # No hashing at all: one walk, bucket by (name, size), done.
        by_name_size = scanner.collect_by_name_size()
        if verbose:
            print(f"Name+size groups with >1 file: {len(by_name_size):,}",
                  file=sys.stderr)
        groups = find_duplicates_fast(by_name_size)
    else:
        by_size = scanner.collect_by_size()
        if verbose:
            print(f"Size groups with potential dupes: {len(by_size):,}",
                  file=sys.stderr)

        # --cache none disables caching; HashCache treats a None path as
        # disabled already, so there is no separate flag to carry around.
        caching_off = args.cache is not None and args.cache.lower() == "none"
        cache = HashCache(None if caching_off
                          else (args.cache or default_cache_path()))
        groups = find_duplicates(by_size, workers=max(1, args.workers),
                                 cache=cache, verbose=verbose,
                                 show_progress=show_progress)
        cache.save()
        if verbose and cache.enabled and (cache.hits or cache.misses):
            print(f"Hash cache: {cache.hits:,} hit(s), {cache.misses:,} miss(es)",
                  file=sys.stderr)

    total_waste = report(groups, verified=not args.fast,
                         limit=None if verbose else TOP_GROUPS)

    do_delete = args.delete or args.recycle or args.dry_run
    # --review on its own is a read-only mode: build the plan, write it out,
    # delete nothing, never prompt.
    review_only = args.review is not None and not do_delete
    if (do_delete or review_only) and groups and total_waste > 0:
        delete_duplicates(
            groups,
            strategy=args.keep,
            assume_yes=args.yes,
            log_path=args.log,
            interactive=args.interactive,
            recycle=args.recycle,
            prefer=args.prefer,
            dry_run=args.dry_run,
            verified=not args.fast,
            review_path=args.review,
            review_explicit=args.review is not None,
            review_only=review_only,
        )
    elif review_only:
        print("No duplicates to review.")
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
