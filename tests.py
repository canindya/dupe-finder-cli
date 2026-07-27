#!/usr/bin/env python3
"""Test suite for dupe_finder - stdlib only, no pytest.

Run with:  python tests.py          (or: python -m unittest tests -v)

Every fixture lives in a fresh temporary directory. Nothing here touches real
user data, the real Recycle Bin, or the real hash cache.
"""

from __future__ import annotations

import os
import stat as stat_mod
import sys
import tempfile
import unittest

import dupe_finder as df


# --------------------------------------------------------------------------- #
# Helpers for building fixtures
# --------------------------------------------------------------------------- #
def write(path: str, content: bytes | str) -> str:
    """Create a file (and its parents) with the given content."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(path, mode) as f:
        f.write(content)
    return path


class TempTree(unittest.TestCase):
    """Base class giving each test an empty temporary directory as self.root."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def path(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)

    def scanner(self, **kw) -> df.Scanner:
        kw.setdefault("roots", [self.root])
        return df.Scanner(**kw)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
class TestSizeParsing(unittest.TestCase):
    def test_plain_number(self):
        self.assertEqual(df.parse_size("10"), 10)

    def test_units(self):
        self.assertEqual(df.parse_size("1KB"), 1024)
        self.assertEqual(df.parse_size("1MB"), 1024 ** 2)
        self.assertEqual(df.parse_size("2gb"), 2 * 1024 ** 3)
        self.assertEqual(df.parse_size("1TB"), 1024 ** 4)

    def test_fractional_and_whitespace(self):
        self.assertEqual(df.parse_size(" 1.5MB "), int(1.5 * 1024 ** 2))

    def test_bare_b_suffix(self):
        self.assertEqual(df.parse_size("512B"), 512)


class TestHuman(unittest.TestCase):
    def test_bytes_have_no_decimals(self):
        self.assertEqual(df.human(0), "0 B")
        self.assertEqual(df.human(999), "999 B")

    def test_scales_up(self):
        self.assertEqual(df.human(1024), "1.00 KB")
        self.assertEqual(df.human(1024 ** 2), "1.00 MB")
        self.assertEqual(df.human(1024 ** 3), "1.00 GB")

    def test_petabytes_do_not_overflow_the_units(self):
        # The loop must terminate at PB rather than run off the end of the tuple.
        self.assertTrue(df.human(1024 ** 6).endswith("PB"))
        self.assertTrue(df.human(1024 ** 8).endswith("PB"))


class TestExtensionParsing(unittest.TestCase):
    def test_comma_repeat_and_dot_forms_agree(self):
        self.assertEqual(df.normalize_extensions(["jpg,png"]), {".jpg", ".png"})
        self.assertEqual(df.normalize_extensions([".JPG", "png"]), {".jpg", ".png"})

    def test_blank_tokens_ignored(self):
        self.assertEqual(df.normalize_extensions(["jpg,,  ,png"]), {".jpg", ".png"})

    def test_none(self):
        self.assertEqual(df.normalize_extensions(None), set())


class TestCategories(unittest.TestCase):
    def test_canonical_name(self):
        self.assertIn(".mkv", df.expand_categories(["movies"]))

    def test_alias_resolves(self):
        self.assertEqual(df.expand_categories(["video"]),
                         df.expand_categories(["movies"]))
        self.assertEqual(df.expand_categories(["audio"]),
                         df.expand_categories(["music"]))

    def test_unknown_category_warns_and_yields_nothing(self):
        stderr, sys.stderr = sys.stderr, open(os.devnull, "w")
        try:
            self.assertEqual(df.expand_categories(["nonsense"]), set())
        finally:
            sys.stderr.close()
            sys.stderr = stderr

    def test_every_alias_points_at_a_real_category(self):
        for alias, target in df.CATEGORY_ALIASES.items():
            self.assertIn(target, df.CATEGORIES, f"alias {alias!r} is dangling")


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #
def reference_walk(sc: df.Scanner) -> set[str]:
    """The pre-scandir os.walk implementation, kept here as an oracle.

    Scanner._iter_files() was rewritten to use os.scandir for speed (and to stop
    silently dropping paths over the Windows MAX_PATH limit). This reproduces
    the old logic so the rewrite can be proved equivalent on ordinary trees."""
    found: set[str] = set()
    for root in sc.roots:
        for dirpath, dirnames, filenames in os.walk(
                root, followlinks=sc.follow_symlinks):
            if sc._excluded(dirpath):
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames
                           if not sc._excluded(os.path.join(dirpath, d))]
            for name in filenames:
                if not sc._wanted_type(name):
                    continue
                full = os.path.join(dirpath, name)
                if sc._excluded(full):
                    continue
                try:
                    st = os.stat(full, follow_symlinks=sc.follow_symlinks)
                except OSError:
                    continue
                if not os.path.isfile(full):
                    continue
                if os.path.islink(full) and not sc.follow_symlinks:
                    continue
                size = st.st_size
                if size < sc.min_size:
                    continue
                if sc.max_size is not None and size > sc.max_size:
                    continue
                found.add(full)
    return found


class TestScanEquivalence(TempTree):
    """The scandir rewrite must find exactly what the old os.walk found."""

    def build(self) -> None:
        write(self.path("a", "one.txt"), "x" * 100)
        write(self.path("a", "two.jpg"), "y" * 200)
        write(self.path("a", "deep", "three.txt"), "z" * 300)
        write(self.path("a", "deep", "deeper", "four.png"), "w" * 50)
        write(self.path("b", "five.txt"), "v" * 5)
        write(self.path("b", "skipme", "six.txt"), "u" * 100)
        write(self.path("empty_dir_marker", "tiny.txt"), "t")

    def assert_same(self, **kw) -> None:
        sc = self.scanner(**kw)
        expected = reference_walk(sc)
        actual = {p for p, _ in sc._iter_files()}
        self.assertEqual(actual, expected)

    def test_plain(self):
        self.build()
        self.assert_same()

    def test_with_extension_filter(self):
        self.build()
        self.assert_same(extensions={".txt"})

    def test_with_min_size(self):
        self.build()
        self.assert_same(min_size=100)

    def test_with_max_size(self):
        self.build()
        self.assert_same(max_size=150)

    def test_with_substring_exclusion(self):
        self.build()
        self.assert_same(exclude=["skipme"])

    def test_with_prefix_exclusion(self):
        self.build()
        self.assert_same(exclude_prefixes=[self.path("b")])

    def test_sizes_reported_match_the_filesystem(self):
        self.build()
        for full, size in self.scanner()._iter_files():
            self.assertEqual(size, os.path.getsize(full), full)


class TestScannerFiltering(TempTree):
    def test_buckets_by_size_and_drops_unique_sizes(self):
        write(self.path("a.txt"), "1" * 10)
        write(self.path("b.txt"), "2" * 10)   # same size, different content
        write(self.path("c.txt"), "3" * 99)   # unique size
        by_size = self.scanner().collect_by_size()
        self.assertEqual(list(by_size), [10])
        self.assertEqual(len(by_size[10]), 2)

    def test_buckets_by_name_and_size(self):
        write(self.path("x", "same.txt"), "a" * 10)
        write(self.path("y", "same.txt"), "b" * 10)   # same name+size
        write(self.path("y", "other.txt"), "c" * 10)  # same size, other name
        buckets = self.scanner().collect_by_name_size()
        self.assertEqual(list(buckets), [("same.txt", 10)])
        self.assertEqual(len(buckets[("same.txt", 10)]), 2)

    def test_name_matching_is_case_insensitive(self):
        write(self.path("x", "Song.MP3"), "a" * 10)
        write(self.path("y", "song.mp3"), "b" * 10)
        buckets = self.scanner().collect_by_name_size()
        self.assertEqual(len(buckets), 1)

    def test_min_size_excludes_empty_files_by_default(self):
        write(self.path("empty1.txt"), "")
        write(self.path("empty2.txt"), "")
        self.assertEqual(self.scanner().collect_by_size(), {})

    def test_extension_filter_is_case_insensitive(self):
        write(self.path("a.JPG"), "1" * 10)
        write(self.path("b.jpg"), "2" * 10)
        write(self.path("c.txt"), "3" * 10)
        by_size = self.scanner(extensions={".jpg"}).collect_by_size()
        self.assertEqual(sorted(os.path.basename(p) for p in by_size[10]),
                         ["a.JPG", "b.jpg"])

    def test_prefix_exclusion_is_anchored_not_substring(self):
        # The /dev vs ~/dev trap: a prefix must only match at the root.
        write(self.path("dev", "a.txt"), "1" * 10)
        write(self.path("home", "dev", "b.txt"), "2" * 10)
        sc = self.scanner(exclude_prefixes=[self.path("dev")])
        found = {p for p, _ in sc._iter_files()}
        self.assertEqual(found, {self.path("home", "dev", "b.txt")})


# --------------------------------------------------------------------------- #
# Duplicate detection
# --------------------------------------------------------------------------- #
class TestFindDuplicates(TempTree):
    def groups_of(self, **kw):
        by_size = self.scanner(**kw).collect_by_size()
        groups = df.find_duplicates(by_size, workers=1, cache=None)
        return sorted(sorted(os.path.basename(p) for p in g) for g in groups)

    def test_identical_content_is_grouped_regardless_of_name(self):
        write(self.path("a.bin"), "same" * 100)
        write(self.path("b.bin"), "same" * 100)
        write(self.path("renamed.bin"), "same" * 100)
        self.assertEqual(self.groups_of(), [["a.bin", "b.bin", "renamed.bin"]])

    def test_same_size_different_content_is_not_grouped(self):
        write(self.path("a.bin"), "A" * 1000)
        write(self.path("b.bin"), "B" * 1000)
        self.assertEqual(self.groups_of(), [])

    def test_unique_size_is_not_grouped(self):
        write(self.path("a.bin"), "A" * 10)
        write(self.path("b.bin"), "A" * 11)
        self.assertEqual(self.groups_of(), [])

    # --- the 64 KB promotion boundary ------------------------------------- #
    # Files at or below PARTIAL_READ are hashed in full by the partial pass, so
    # the full pass reuses that digest instead of reading them again. These
    # tests pin both sides of the boundary and, critically, the case where two
    # large files share their first 64 KB but differ later.

    def test_small_files_below_boundary(self):
        payload = "s" * (df.PARTIAL_READ // 2)
        write(self.path("a.bin"), payload)
        write(self.path("b.bin"), payload)
        self.assertEqual(self.groups_of(), [["a.bin", "b.bin"]])

    def test_files_exactly_at_the_boundary(self):
        payload = "e" * df.PARTIAL_READ
        write(self.path("a.bin"), payload)
        write(self.path("b.bin"), payload)
        self.assertEqual(self.groups_of(), [["a.bin", "b.bin"]])

    def test_files_one_byte_over_the_boundary(self):
        payload = "o" * (df.PARTIAL_READ + 1)
        write(self.path("a.bin"), payload)
        write(self.path("b.bin"), payload)
        self.assertEqual(self.groups_of(), [["a.bin", "b.bin"]])

    def test_shared_prefix_but_different_tail_is_not_grouped(self):
        # Identical first 64 KB, same total size, different content. The full
        # pass must catch this - promoting on the partial hash alone would be
        # a false positive that deletes real data.
        head = "h" * df.PARTIAL_READ
        write(self.path("a.bin"), head + "A" * 5000)
        write(self.path("b.bin"), head + "B" * 5000)
        self.assertEqual(self.groups_of(), [])

    def test_mixed_small_and_large_in_one_scan(self):
        small = "s" * 100
        large = "l" * (df.PARTIAL_READ * 2)
        write(self.path("s1.bin"), small)
        write(self.path("s2.bin"), small)
        write(self.path("l1.bin"), large)
        write(self.path("l2.bin"), large)
        self.assertEqual(self.groups_of(),
                         [["l1.bin", "l2.bin"], ["s1.bin", "s2.bin"]])

    def test_parallel_workers_give_the_same_answer(self):
        for i in range(12):
            payload = f"payload-{i}" * 50
            write(self.path(f"a{i}.bin"), payload)
            write(self.path(f"b{i}.bin"), payload)
        by_size = self.scanner().collect_by_size()
        serial = df.find_duplicates(by_size, workers=1, cache=None)
        parallel = df.find_duplicates(by_size, workers=8, cache=None)
        norm = lambda gs: sorted(sorted(g) for g in gs)
        self.assertEqual(norm(serial), norm(parallel))
        self.assertEqual(len(serial), 12)


class TestFindDuplicatesFast(TempTree):
    def test_matches_on_name_and_size_only(self):
        write(self.path("x", "same.txt"), "A" * 50)
        write(self.path("y", "same.txt"), "B" * 50)   # different content!
        buckets = self.scanner().collect_by_name_size()
        groups = df.find_duplicates_fast(buckets)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)

    def test_misses_renamed_copies(self):
        write(self.path("song.mp3"), "A" * 50)
        write(self.path("song (1).mp3"), "A" * 50)   # identical, renamed
        buckets = self.scanner().collect_by_name_size()
        self.assertEqual(df.find_duplicates_fast(buckets), [])


# --------------------------------------------------------------------------- #
# Keeper selection
# --------------------------------------------------------------------------- #
class TestChooseKeeper(TempTree):
    def setUp(self):
        super().setUp()
        # 'old' is both the oldest and the shortest path; 'new' is the newest
        # and the longest, so every strategy has an unambiguous answer. The
        # marker segments are long enough not to collide with the random
        # characters in the temporary directory name.
        self.old = write(self.path("keepdir", "old.txt"), "x")
        self.new = write(self.path("aa", "bb", "cc", "nested", "new.txt"), "x")
        os.utime(self.old, (1_000_000, 1_000_000))
        os.utime(self.new, (2_000_000, 2_000_000))
        self.group = [self.old, self.new]
        self.assertLess(len(self.old), len(self.new))

    def test_oldest_and_newest(self):
        self.assertEqual(df.choose_keeper(self.group, "oldest"), self.old)
        self.assertEqual(df.choose_keeper(self.group, "newest"), self.new)

    def test_path_length_strategies(self):
        self.assertEqual(df.choose_keeper(self.group, "shortest-path"), self.old)
        self.assertEqual(df.choose_keeper(self.group, "longest-path"), self.new)

    def test_first(self):
        self.assertEqual(df.choose_keeper(self.group, "first"), self.old)

    def test_prefer_wins_over_strategy(self):
        # 'new' is not the oldest, but --prefer should still select it.
        keeper = df.choose_keeper(self.group, "oldest", prefer=["nested"])
        self.assertEqual(keeper, self.new)

    def test_prefer_falls_back_when_nothing_matches(self):
        keeper = df.choose_keeper(self.group, "oldest", prefer=["nomatchhere"])
        self.assertEqual(keeper, self.old)

    def test_earlier_prefer_tokens_win(self):
        keeper = df.choose_keeper(self.group, "oldest",
                                  prefer=["nested", "keepdir"])
        self.assertEqual(keeper, self.new)

    def test_every_documented_strategy_returns_a_member(self):
        for strategy in df.KEEP_STRATEGIES:
            self.assertIn(df.choose_keeper(self.group, strategy), self.group)


# --------------------------------------------------------------------------- #
# Hash cache
# --------------------------------------------------------------------------- #
class TestHashCache(TempTree):
    def test_hit_on_unchanged_file(self):
        target = write(self.path("a.bin"), "content")
        cache = df.HashCache(self.path("cache.json"))
        first = cache.get_or_compute(target)
        second = cache.get_or_compute(target)
        self.assertEqual(first, second)
        self.assertEqual(cache.hits, 1)
        self.assertEqual(cache.misses, 1)

    def test_miss_after_content_and_mtime_change(self):
        target = write(self.path("a.bin"), "before")
        cache = df.HashCache(self.path("cache.json"))
        first = cache.get_or_compute(target)
        write(target, "after-and-longer")
        os.utime(target, (3_000_000, 3_000_000))
        second = cache.get_or_compute(target)
        self.assertNotEqual(first, second)
        self.assertEqual(cache.misses, 2)

    def test_survives_a_save_and_reload(self):
        target = write(self.path("a.bin"), "content")
        path = self.path("cache.json")
        cache = df.HashCache(path)
        expected = cache.get_or_compute(target)
        cache.save()
        reloaded = df.HashCache(path)
        self.assertEqual(reloaded.get_or_compute(target), expected)
        self.assertEqual(reloaded.hits, 1)

    def test_corrupt_cache_file_starts_fresh(self):
        path = write(self.path("cache.json"), "{not json at all")
        cache = df.HashCache(path)
        self.assertEqual(cache.data, {})

    def test_disabled_cache_still_hashes(self):
        target = write(self.path("a.bin"), "content")
        cache = df.HashCache(None, enabled=False)
        self.assertEqual(cache.get_or_compute(target), df.hash_file(target))

    def test_unreadable_path_returns_none(self):
        cache = df.HashCache(self.path("cache.json"))
        self.assertIsNone(cache.get_or_compute(self.path("does-not-exist")))


class TestFsPath(unittest.TestCase):
    """The \\\\?\\ escape hatch used to get past Windows MAX_PATH."""

    def test_noop_on_posix(self):
        if os.name == "nt":
            self.skipTest("windows-specific behaviour")
        self.assertEqual(df._fs_path("/tmp/a.txt"), "/tmp/a.txt")

    def test_prefixes_on_windows(self):
        if os.name != "nt":
            self.skipTest("windows-only")
        out = df._fs_path(r"C:\some\file.txt")
        self.assertEqual(out, "\\\\?\\C:\\some\\file.txt")

    def test_already_prefixed_is_untouched(self):
        if os.name != "nt":
            self.skipTest("windows-only")
        already = "\\\\?\\C:\\some\\file.txt"
        self.assertEqual(df._fs_path(already), already)

    def test_unc_paths_use_the_unc_form(self):
        if os.name != "nt":
            self.skipTest("windows-only")
        self.assertEqual(df._fs_path(r"\\server\share\f.txt"),
                         "\\\\?\\UNC\\server\\share\\f.txt")

    def test_ordinary_files_still_hash_through_it(self):
        with tempfile.TemporaryDirectory() as d:
            target = write(os.path.join(d, "a.bin"), "content")
            self.assertIsNotNone(df.hash_file(target))
            self.assertEqual(df._safe_size(target), 7)


class TestSafeSize(TempTree):
    def test_returns_size(self):
        self.assertEqual(df._safe_size(write(self.path("a.bin"), "x" * 42)), 42)

    def test_missing_file_is_zero_not_an_exception(self):
        self.assertEqual(df._safe_size(self.path("nope.bin")), 0)


class TestHashFile(TempTree):
    def test_partial_equals_full_for_small_files(self):
        # This identity is what licenses the <=PARTIAL_READ promotion.
        target = write(self.path("a.bin"), "x" * 1000)
        self.assertEqual(df.hash_file(target, df.PARTIAL_READ),
                         df.hash_file(target))

    def test_missing_file_returns_none(self):
        self.assertIsNone(df.hash_file(self.path("nope.bin")))


# --------------------------------------------------------------------------- #
# Review file
# --------------------------------------------------------------------------- #
class TestWriteReview(TempTree):
    def make_plan(self):
        keeper = write(self.path("keep", "movie.mkv"), "z" * 400)
        victim = write(self.path("dupe", "movie.mkv"), "z" * 400)
        return [(keeper, [victim])], keeper, victim

    def test_lists_keeper_and_victims(self):
        plan, keeper, victim = self.make_plan()
        out = self.path("review.txt")
        written = df.write_review(plan, out, "oldest", False, True, explicit=True)
        self.assertEqual(written, out)
        body = open(out, encoding="utf-8").read()
        self.assertIn(keeper, body)
        self.assertIn(victim, body)
        self.assertIn("KEEP", body)
        self.assertIn("remove", body)

    def test_explicit_path_is_overwritten(self):
        plan, _, _ = self.make_plan()
        out = write(self.path("review.txt"), "stale")
        df.write_review(plan, out, "oldest", False, True, explicit=True)
        self.assertNotIn("stale", open(out, encoding="utf-8").read())

    def test_implicit_path_never_clobbers(self):
        plan, _, _ = self.make_plan()
        out = write(self.path("review.txt"), "precious")
        written = df.write_review(plan, out, "oldest", False, True, explicit=False)
        self.assertNotEqual(written, out)
        self.assertEqual(open(out, encoding="utf-8").read(), "precious")

    def test_fast_mode_review_carries_the_warning(self):
        plan, _, _ = self.make_plan()
        out = self.path("review.txt")
        df.write_review(plan, out, "oldest", False, verified=False, explicit=True)
        self.assertIn("FAST MODE", open(out, encoding="utf-8").read())

    def test_recycle_wording(self):
        plan, _, _ = self.make_plan()
        out = self.path("review.txt")
        df.write_review(plan, out, "oldest", True, True, explicit=True)
        self.assertIn("RECYCLE", open(out, encoding="utf-8").read())


# --------------------------------------------------------------------------- #
# Trash core (must stay OS-independent so it is testable everywhere)
# --------------------------------------------------------------------------- #
class TestTrashInto(TempTree):
    def test_moves_file_and_writes_restorable_metadata(self):
        victim = write(self.path("gone.txt"), "bye")
        trash = self.path("Trash")
        df._trash_into(victim, trash)

        self.assertFalse(os.path.exists(victim))
        self.assertTrue(os.path.exists(os.path.join(trash, "files", "gone.txt")))
        info = os.path.join(trash, "info", "gone.txt.trashinfo")
        self.assertTrue(os.path.exists(info))
        body = open(info, encoding="utf-8").read()
        self.assertIn("[Trash Info]", body)
        self.assertIn("DeletionDate=", body)

    def test_name_collisions_do_not_overwrite(self):
        trash = self.path("Trash")
        for i in range(3):
            df._trash_into(write(self.path("sub", str(i), "same.txt"), f"v{i}"),
                           trash)
        files = sorted(os.listdir(os.path.join(trash, "files")))
        self.assertEqual(len(files), 3, files)
        self.assertEqual(len(set(files)), 3)


class TestUniqueDest(TempTree):
    def test_returns_the_name_when_free(self):
        self.assertEqual(df._unique_dest(self.root, "a.txt"), "a.txt")

    def test_numbers_around_existing_files(self):
        write(self.path("a.txt"), "x")
        write(self.path("a.1.txt"), "x")
        self.assertEqual(df._unique_dest(self.root, "a.txt"), "a.2.txt")


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
class TestReport(TempTree):
    def capture(self, groups, **kw):
        import io
        buf, sys.stdout = io.StringIO(), None
        sys.stdout = buf
        try:
            total = df.report(groups, **kw)
        finally:
            sys.stdout = sys.__stdout__
        return total, buf.getvalue()

    def test_totals_cover_every_group_even_when_listing_is_capped(self):
        groups = []
        for i in range(5):
            a = write(self.path(f"a{i}.bin"), "x" * 100)
            b = write(self.path(f"b{i}.bin"), "x" * 100)
            groups.append([a, b])
        total, out = self.capture(groups, limit=2)
        self.assertEqual(total, 5 * 100)          # all five counted
        self.assertIn("more group(s)", out)       # but only two listed

    def test_vanished_file_does_not_crash_the_sort(self):
        # A file can disappear between scanning and reporting; the ranking must
        # tolerate that rather than raising out of the sort key.
        a = write(self.path("a.bin"), "x" * 100)
        b = write(self.path("b.bin"), "x" * 100)
        c = write(self.path("c.bin"), "y" * 200)
        d = write(self.path("d.bin"), "y" * 200)
        os.remove(c)
        os.remove(d)
        total, out = self.capture([[a, b], [c, d]])
        self.assertEqual(total, 100)

    def test_unverified_results_are_labelled(self):
        a = write(self.path("a.bin"), "x" * 100)
        b = write(self.path("b.bin"), "x" * 100)
        _, out = self.capture([[a, b]], verified=False)
        self.assertIn("likely copies", out)
        self.assertIn("FAST MODE", out)

    def test_empty_input(self):
        total, out = self.capture([])
        self.assertEqual(total, 0)
        self.assertIn("No duplicate files found", out)


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #
class TestRemovedOptions(unittest.TestCase):
    def test_each_removed_spelling_is_caught(self):
        for old, new in df.REMOVED_OPTIONS.items():
            msg = df.check_removed_options([old, "value"])
            self.assertIsNotNone(msg, old)
            self.assertIn(new, msg)

    def test_equals_form_is_caught(self):
        self.assertIsNotNone(df.check_removed_options(["--ext=jpg"]))

    def test_current_options_pass_through(self):
        self.assertIsNone(df.check_removed_options(
            ["--type", "jpg", "--category", "movies", "--fast",
             "--cache", "none", "-p", "D:\\x"]))

    def test_replacements_are_real_options(self):
        parser = df.build_parser()
        known = set()
        for action in parser._actions:
            known.update(action.option_strings)
        for old, new in df.REMOVED_OPTIONS.items():
            self.assertNotIn(old, known, f"{old} should be gone")
            self.assertIn(new.split()[0], known, f"{new} should exist")


class TestCacheNone(TempTree):
    def test_none_disables_caching(self):
        # main() maps --cache none to a None path; HashCache treats that as off.
        cache = df.HashCache(None)
        self.assertFalse(cache.enabled)
        target = write(self.path("a.bin"), "content")
        self.assertEqual(cache.get_or_compute(target), df.hash_file(target))
        self.assertEqual(cache.hits, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
