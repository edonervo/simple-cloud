"""Tests for cloudsync.state — the state directory, manifests, plans, runs, and the lock."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from cloudsync import state as state_module


class StateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)
        self.paths = state_module.Paths(os.path.join(self.root, "state"))


class PathsTests(StateTestCase):
    def test_all_dirs_are_under_the_root(self):
        for directory in self.paths.all_dirs():
            self.assertTrue(
                self.paths.contains(directory), f"{directory} escapes the state dir"
            )

    def test_contains_accepts_an_inner_file(self):
        self.assertTrue(self.paths.contains(os.path.join(self.paths.plans, "p.json")))
        self.assertTrue(self.paths.contains(self.paths.log_file))

    def test_contains_rejects_a_sibling_and_a_parent(self):
        self.assertFalse(self.paths.contains(os.path.join(self.root, "elsewhere")))
        self.assertFalse(self.paths.contains(self.root))
        self.assertFalse(self.paths.contains("/etc/passwd"))

    def test_paths_are_inside_state_dir_reports_the_escapers(self):
        # L3: the only local writes are under state/. Stated as a check so "the tool only writes
        # under state/" is a claim with a test, not a hope.
        candidates = [self.paths.lock, os.path.join(self.root, "nope"), self.paths.state_file]

        escaped = state_module.paths_are_inside_state_dir(self.paths, candidates)

        self.assertEqual(escaped, [os.path.join(self.root, "nope")])

    def test_ensure_creates_the_tree_and_is_idempotent(self):
        state_module.ensure(self.paths)
        state_module.ensure(self.paths)

        for directory in self.paths.all_dirs():
            self.assertTrue(os.path.isdir(directory), directory)


class TimestampTests(unittest.TestCase):
    def test_a_timestamp_is_filesystem_and_rclone_safe(self):
        moment = datetime(2026, 10, 4, 19, 30, 5, tzinfo=timezone.utc)

        self.assertEqual(state_module.timestamp(moment), "20261004T193005Z")

    def test_two_runs_in_the_same_second_are_the_same_name_and_the_next_is_not(self):
        # R2 depends on this: rclone overwrites an existing suffixed path, so a day-granular
        # trash name would collide on a same-day rerun and destroy the earlier copy.
        first = datetime(2026, 10, 4, 19, 30, 5, tzinfo=timezone.utc)
        second = first + timedelta(seconds=1)

        self.assertNotEqual(state_module.timestamp(first), state_module.timestamp(second))


class DigestTests(unittest.TestCase):
    def test_the_digest_ignores_order(self):
        self.assertEqual(
            state_module.digest_paths(["b", "a"]), state_module.digest_paths(["a", "b"])
        )

    def test_the_digest_changes_with_content(self):
        self.assertNotEqual(
            state_module.digest_paths(["a", "b"]), state_module.digest_paths(["a", "c"])
        )

    def test_a_prefix_does_not_collide(self):
        # The NUL separator is what stops ["ab"] and ["a","b"] digesting alike.
        self.assertNotEqual(
            state_module.digest_paths(["ab"]), state_module.digest_paths(["a", "b"])
        )


class ManifestTests(unittest.TestCase):
    def test_a_manifest_counts_files_bytes_and_the_newest_mtime(self):
        files = [
            {"Path": "a", "Size": 10, "ModTime": "2026-01-01T00:00:00Z"},
            {"Path": "b", "Size": 5, "ModTime": "2026-06-01T00:00:00Z"},
        ]

        manifest = state_module.manifest_for(files)

        self.assertEqual(manifest["file_count"], 2)
        self.assertEqual(manifest["bytes"], 15)
        self.assertEqual(manifest["newest_mtime"], "2026-06-01T00:00:00Z")

    def test_an_empty_manifest_is_all_zeroes(self):
        manifest = state_module.manifest_for([])

        self.assertEqual(manifest["file_count"], 0)
        self.assertEqual(manifest["bytes"], 0)
        self.assertIsNone(manifest["newest_mtime"])

    def test_a_missing_size_is_treated_as_zero(self):
        manifest = state_module.manifest_for([{"Path": "a"}])

        self.assertEqual(manifest["bytes"], 0)


class StateFileTests(StateTestCase):
    def test_load_of_an_absent_state_file_returns_defaults(self):
        state = state_module.load_state(self.paths)

        self.assertEqual(state["jobs"], {})
        self.assertEqual(state["runs"], [])
        self.assertEqual(state["version"], state_module.STATE_VERSION)

    def test_record_and_read_a_manifest(self):
        state = {}
        state_module.record_good(state, "documents", {"file_count": 3}, moment=datetime(
            2026, 10, 4, tzinfo=timezone.utc
        ))

        entry = state_module.last_good(state, "documents")

        self.assertEqual(entry["manifest"]["file_count"], 3)
        self.assertIn("2026-10-04", entry["recorded_at"])

    def test_last_good_is_none_for_an_unknown_job(self):
        self.assertIsNone(state_module.last_good({}, "nope"))

    def test_save_and_load_round_trip(self):
        state_module.ensure(self.paths)
        state = state_module.record_good({}, "documents", {"file_count": 7})
        state_module.save_state(self.paths, state)

        reloaded = state_module.load_state(self.paths)

        self.assertEqual(state_module.last_good(reloaded, "documents")["manifest"]["file_count"], 7)

    def test_a_write_leaves_no_partial_file_behind(self):
        state_module.ensure(self.paths)
        state_module.save_state(self.paths, {"jobs": {}})

        leftovers = [name for name in os.listdir(self.paths.root) if name.endswith(".partial")]
        self.assertEqual(leftovers, [])

    def test_an_unreadable_state_file_raises_rather_than_being_ignored(self):
        state_module.ensure(self.paths)
        with open(self.paths.state_file, "w", encoding="utf-8") as handle:
            handle.write("{not json")

        with self.assertRaises(state_module.StateError):
            state_module.load_state(self.paths)


class PlanTests(StateTestCase):
    def test_a_plan_round_trips(self):
        state_module.ensure(self.paths)
        plan = {"job": "documents", "argv": ["rclone", "sync"]}

        path = state_module.write_plan(self.paths, plan)
        loaded = state_module.read_plan(path)

        self.assertEqual(loaded, plan)

    def test_a_plan_filename_carries_the_job_and_the_time(self):
        path = state_module.plan_path(
            self.paths, "documents", datetime(2026, 10, 4, 19, 30, 5, tzinfo=timezone.utc)
        )

        self.assertTrue(path.endswith("documents-20261004T193005Z.json"), path)

    def test_plans_are_listed_newest_first(self):
        state_module.ensure(self.paths)
        for second in (5, 6, 7):
            state_module.write_plan(
                self.paths,
                {"job": "documents", "argv": []},
                moment=datetime(2026, 10, 4, 19, 30, second, tzinfo=timezone.utc),
            )

        names = [entry["name"] for entry in state_module.list_plans(self.paths)]

        self.assertEqual(names[0], "documents-20261004T193007Z.json")

    def test_listing_plans_in_an_empty_directory_is_empty(self):
        self.assertEqual(state_module.list_plans(self.paths), [])

    def test_an_unrelated_json_file_is_skipped_not_fatal(self):
        state_module.ensure(self.paths)
        with open(os.path.join(self.paths.plans, "notes.json"), "w", encoding="utf-8") as handle:
            handle.write('{"unrelated": true}')

        self.assertEqual(state_module.list_plans(self.paths), [])

    def test_reading_a_missing_plan_raises(self):
        with self.assertRaises(state_module.StateError):
            state_module.read_plan(os.path.join(self.paths.plans, "absent.json"))

    def test_a_file_this_tool_did_not_write_is_refused(self):
        state_module.ensure(self.paths)
        path = os.path.join(self.paths.plans, "other.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"job": "x"}')

        with self.assertRaises(state_module.StateError):
            state_module.read_plan(path)

    def test_find_plan_for_job_returns_the_newest(self):
        state_module.ensure(self.paths)
        state_module.write_plan(
            self.paths, {"job": "documents", "argv": ["first"]},
            moment=datetime(2026, 10, 4, 19, 30, 5, tzinfo=timezone.utc),
        )
        state_module.write_plan(
            self.paths, {"job": "documents", "argv": ["second"]},
            moment=datetime(2026, 10, 4, 19, 30, 9, tzinfo=timezone.utc),
        )

        found = state_module.find_plan_for_job(self.paths, "documents")

        self.assertEqual(found["argv"], ["second"])

    def test_find_plan_for_an_unknown_job_is_none(self):
        self.assertIsNone(state_module.find_plan_for_job(self.paths, "nope"))


class LockTests(StateTestCase):
    def test_acquire_then_release(self):
        lock = state_module.Lock(self.paths)

        lock.acquire()
        self.assertTrue(os.path.exists(self.paths.lock))
        lock.release()
        self.assertFalse(os.path.exists(self.paths.lock))

    def test_a_second_acquire_is_refused_while_held(self):
        # L5: two runs must not interleave.
        first = state_module.Lock(self.paths)
        first.acquire()
        self.addCleanup(first.release)

        with self.assertRaises(state_module.Locked):
            state_module.Lock(self.paths).acquire()

    def test_the_error_names_the_holder(self):
        first = state_module.Lock(self.paths)
        first.acquire()
        self.addCleanup(first.release)

        with self.assertRaises(state_module.Locked) as caught:
            state_module.Lock(self.paths).acquire()

        self.assertIn("pid", str(caught.exception))

    def test_the_lock_can_be_reacquired_after_release(self):
        with state_module.Lock(self.paths):
            pass

        second = state_module.Lock(self.paths)
        second.acquire()
        second.release()

    def test_the_context_manager_releases_on_an_exception(self):
        with self.assertRaises(ValueError):
            with state_module.Lock(self.paths):
                raise ValueError("boom")

        self.assertFalse(os.path.exists(self.paths.lock))

    def test_locked_is_a_state_error(self):
        # The CLI catches StateError for exit 1 and Locked for exit 3, so the relationship
        # matters: Locked must be the more specific type.
        self.assertTrue(issubclass(state_module.Locked, state_module.StateError))

    def test_release_without_acquire_is_a_no_op(self):
        self.assertFalse(state_module.Lock(self.paths).release())


class LogTests(StateTestCase):
    def test_append_and_tail(self):
        state_module.append_log(self.paths, "first")
        state_module.append_log(self.paths, "second")

        lines = state_module.tail_log(self.paths, count=10)

        self.assertEqual(len(lines), 2)
        self.assertIn("first", lines[0])

    def test_tail_limits_to_the_requested_count(self):
        for index in range(5):
            state_module.append_log(self.paths, f"line {index}")

        lines = state_module.tail_log(self.paths, count=2)

        self.assertEqual(len(lines), 2)
        self.assertIn("line 4", lines[-1])

    def test_tail_of_an_absent_log_is_empty(self):
        self.assertEqual(state_module.tail_log(self.paths), [])

    def test_a_log_failure_does_not_raise(self):
        # A log write must never be able to fail a run.
        paths = state_module.Paths("/proc/definitely/not/writable")

        self.assertFalse(state_module.append_log(paths, "x"))


class RunRecordTests(StateTestCase):
    def test_a_run_record_is_written_with_a_timestamped_name(self):
        state_module.ensure(self.paths)

        path = state_module.write_run(
            self.paths, "documents", {"outcome": "held"},
            moment=datetime(2026, 10, 4, 19, 30, 5, tzinfo=timezone.utc),
        )

        self.assertTrue(os.path.basename(path).startswith("documents-20261004T193005Z"))
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["outcome"], "held")

    def test_run_records_are_capped(self):
        state = {}
        for index in range(10):
            state_module.record_run(state, {"index": index}, limit=3)

        self.assertEqual([entry["index"] for entry in state["runs"]], [7, 8, 9])


class StalenessTests(unittest.TestCase):
    now = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)

    def test_no_runs_at_all_is_stale(self):
        stale, description = state_module.is_stale({}, 35, moment=self.now)

        self.assertTrue(stale)
        self.assertIn("no run", description)

    def test_a_recent_run_is_not_stale(self):
        state = {"runs": [{"finished_at": (self.now - timedelta(days=2)).isoformat()}]}

        stale, _ = state_module.is_stale(state, 35, moment=self.now)

        self.assertFalse(stale)

    def test_a_run_beyond_the_cadence_is_stale(self):
        state = {"runs": [{"finished_at": (self.now - timedelta(days=40)).isoformat()}]}

        stale, description = state_module.is_stale(state, 35, moment=self.now)

        self.assertTrue(stale)
        self.assertIn("40", description)

    def test_the_newest_run_decides(self):
        state = {
            "runs": [
                {"finished_at": (self.now - timedelta(days=100)).isoformat()},
                {"finished_at": (self.now - timedelta(days=1)).isoformat()},
            ]
        }

        stale, _ = state_module.is_stale(state, 35, moment=self.now)

        self.assertFalse(stale)

    def test_a_naive_timestamp_is_read_as_utc(self):
        naive = (self.now - timedelta(days=1)).replace(tzinfo=None).isoformat()
        state = {"runs": [{"finished_at": naive}]}

        stale, _ = state_module.is_stale(state, 35, moment=self.now)

        self.assertFalse(stale)

    def test_an_unreadable_timestamp_is_stale_rather_than_silent(self):
        state = {"runs": [{"finished_at": "not a date"}]}

        stale, description = state_module.is_stale(state, 35, moment=self.now)

        self.assertTrue(stale)
        self.assertIn("unreadable", description)

    def test_a_run_record_without_a_finish_time_is_stale(self):
        stale, _ = state_module.is_stale({"runs": [{"outcome": "held"}]}, 35, moment=self.now)

        self.assertTrue(stale)


if __name__ == "__main__":
    unittest.main()
