"""Tests for cloudsync.rclone — the argv builders and the NDJSON reader.

The builders are pure, so every assertion here is an exact-list comparison rather than a mock
capture. That is the point of the module's shape: the argv the runner stores at plan time is
produced by the same function that produces the one it replays at apply time, so exact-argv
assertions are testing the real contract.
"""

import json
import unittest

from cloudsync import rclone
from cloudsync.config import Backup, Pair, Remote

RCLONE = "/fake/rclone"
CONF = "/fake/rclone.conf"


def make_pair(mode="mirror", max_delete=25, trash=None, exclude=(), protect=()):
    return Pair(
        name="documents",
        section="pair:documents",
        local="/mnt/c/edo/Documents",
        remote=Remote("edo-remote", "Documents"),
        mode=mode,
        max_delete=max_delete,
        trash_root=Remote("edo-remote", "_cloudsync-trash") if trash else None,
        approve_deletes="always",
        max_shrink_percent=10,
        min_files=1,
        protect=protect,
        exclude=exclude,
    )


def make_backup():
    return Backup(
        name="documents",
        section="backup:documents",
        local="/mnt/c/edo/Documents",
        remote=Remote("backup-crypt", "Documents"),
    )


class BaseArgvTests(unittest.TestCase):
    def test_every_command_carries_the_config_explicitly(self):
        # Not negotiable: without --config, rclone falls back to ~/.config/rclone, which on this
        # machine is a symlink into a git work tree that pushes to GitHub.
        argvs = [
            rclone.sync_argv(make_pair(), RCLONE, CONF),
            rclone.copy_argv(make_backup(), RCLONE, CONF),
            rclone.lsjson_argv(RCLONE, CONF, "edo-remote:Documents"),
            rclone.check_argv(RCLONE, CONF, "/x", "edo-remote:X"),
            rclone.cryptcheck_argv(RCLONE, CONF, "/x", "backup-crypt:X"),
            rclone.about_argv(RCLONE, CONF, "edo-remote"),
            rclone.lsd_argv(RCLONE, CONF, "edo-remote"),
            rclone.size_argv(RCLONE, CONF, "edo-remote:X"),
            rclone.listremotes_argv(RCLONE, CONF),
        ]

        for argv in argvs:
            self.assertEqual(argv[:3], [RCLONE, "--config", CONF], f"in {argv!r}")


class SyncArgvTests(unittest.TestCase):
    def test_a_mirror_argv_is_exactly_as_expected(self):
        argv = rclone.sync_argv(
            make_pair(max_delete=25, trash=True),
            RCLONE,
            CONF,
            trash_dir="edo-remote:_cloudsync-trash/documents/20261004T190000Z",
            dry_run=False,
        )

        self.assertEqual(
            argv,
            [
                RCLONE, "--config", CONF,
                "sync", "/mnt/c/edo/Documents", "edo-remote:Documents",
                "--stats", "1m", "--use-json-log", "--log-level", "INFO",
                "--error-on-no-transfer",
                "--max-delete", "25",
                "--backup-dir", "edo-remote:_cloudsync-trash/documents/20261004T190000Z",
            ],
        )

    def test_the_local_path_is_the_source_and_never_the_destination(self):
        # L1: the tool is read-only locally by construction. If this ever inverts, the run would
        # write into the local directory.
        argv = rclone.sync_argv(make_pair(trash=True), RCLONE, CONF, trash_dir="r:t", dry_run=True)
        index = argv.index("sync")

        self.assertEqual(argv[index + 1], "/mnt/c/edo/Documents")
        self.assertEqual(argv[index + 2], "edo-remote:Documents")

    def test_the_trash_path_is_taken_as_an_argument_not_derived(self):
        # R3: the same inputs must give the same argv, or apply could write to a different trash
        # path than the plan the owner approved.
        pair = make_pair(trash=True)
        first = rclone.sync_argv(pair, RCLONE, CONF, trash_dir="r:t1", dry_run=True)
        second = rclone.sync_argv(pair, RCLONE, CONF, trash_dir="r:t1", dry_run=True)

        self.assertEqual(first, second)

    def test_dry_run_is_the_only_difference_between_plan_and_apply(self):
        planned = rclone.sync_argv(
            make_pair(trash=True), RCLONE, CONF, trash_dir="r:t1", dry_run=True
        )
        applied = rclone.sync_argv(
            make_pair(trash=True), RCLONE, CONF, trash_dir="r:t1", dry_run=False
        )

        self.assertEqual(planned[:-1], applied)
        self.assertEqual(planned[-1], "--dry-run")

    def test_a_mirror_without_a_trash_carries_no_backup_dir(self):
        argv = rclone.sync_argv(make_pair(trash=False), RCLONE, CONF)

        self.assertNotIn("--backup-dir", argv)
        self.assertIn("--max-delete", argv)

    def test_a_copy_pair_carries_neither_a_budget_nor_a_trash(self):
        # R5: copy cannot delete, so both flags would be meaningless noise.
        argv = rclone.sync_argv(make_pair(mode="copy", max_delete=0), RCLONE, CONF)

        self.assertNotIn("--max-delete", argv)
        self.assertNotIn("--backup-dir", argv)

    def test_exclude_patterns_are_passed_in_order(self):
        argv = rclone.sync_argv(
            make_pair(exclude=(".DS_Store", "Thumbs.db")), RCLONE, CONF
        )

        first = argv.index("--exclude")
        self.assertEqual(
            argv[first:first + 4], ["--exclude", ".DS_Store", "--exclude", "Thumbs.db"]
        )

    def test_a_zero_budget_is_never_built_for_a_mirror_with_a_trash(self):
        # The config layer refuses this combination, so it cannot reach here from a real config.
        # Pinned anyway: R1's whole point is that 0 is not the safe value.
        argv = rclone.sync_argv(make_pair(max_delete=1, trash=True), RCLONE, CONF, trash_dir="r:t")

        self.assertEqual(argv[argv.index("--max-delete") + 1], "1")


class CopyArgvTests(unittest.TestCase):
    def test_a_backup_argv_is_exactly_as_expected(self):
        argv = rclone.copy_argv(make_backup(), RCLONE, CONF)

        self.assertEqual(
            argv,
            [
                RCLONE, "--config", CONF,
                "copy", "/mnt/c/edo/Documents", "backup-crypt:Documents",
                "--stats", "1m", "--use-json-log", "--log-level", "INFO",
                "--error-on-no-transfer",
            ],
        )

    def test_a_backup_can_never_be_built_with_a_delete_flag(self):
        for dry_run in (True, False):
            argv = rclone.copy_argv(make_backup(), RCLONE, CONF, dry_run=dry_run)
            for flag in ("--max-delete", "--backup-dir", "--delete-excluded"):
                self.assertNotIn(flag, argv)


class BannedFlagTests(unittest.TestCase):
    def test_no_builder_emits_a_banned_flag(self):
        built = [
            rclone.sync_argv(make_pair(trash=True), RCLONE, CONF, trash_dir="r:t", dry_run=True),
            rclone.sync_argv(make_pair(trash=True), RCLONE, CONF, trash_dir="r:t"),
            rclone.copy_argv(make_backup(), RCLONE, CONF),
            rclone.check_argv(RCLONE, CONF, "/x", "r:x"),
            rclone.cryptcheck_argv(RCLONE, CONF, "/x", "r:x"),
        ]

        for argv in built:
            self.assertEqual(rclone.banned_flags_in(argv), [], f"in {argv!r}")

    def test_each_banned_flag_is_detected(self):
        for flag in rclone.BANNED_FLAGS:
            with self.subTest(flag=flag):
                self.assertTrue(rclone.banned_flags_in(["rclone", "sync", flag]))

    def test_a_banned_flag_is_detected_in_equals_form(self):
        found = rclone.banned_flags_in(["rclone", "sync", "--track-renames=true"])

        self.assertEqual(found[0][0], "--track-renames")

    def test_stats_zero_is_detected_in_both_spellings(self):
        for argv in (["rclone", "sync", "--stats", "0"], ["rclone", "sync", "--stats=0"]):
            with self.subTest(argv=argv):
                found = rclone.banned_flags_in(argv)
                self.assertEqual([name for name, _ in found], ["--stats 0"])

    def test_a_non_zero_stats_interval_is_allowed(self):
        self.assertEqual(rclone.banned_flags_in(["rclone", "sync", "--stats", "1m"]), [])

    def test_every_banned_flag_carries_a_reason(self):
        for flag, reason in rclone.BANNED_FLAGS.items():
            with self.subTest(flag=flag):
                self.assertTrue(reason.strip())
                self.assertGreater(len(reason), 30)

    def test_assert_clean_raises_naming_the_flag(self):
        with self.assertRaises(rclone.RcloneError) as caught:
            rclone.assert_clean(["rclone", "sync", "--immutable"])

        self.assertIn("--immutable", str(caught.exception))
        self.assertIn("banned flag", str(caught.exception))

    def test_assert_clean_passes_a_clean_argv_through(self):
        argv = ["rclone", "sync", "--max-delete", "5"]

        self.assertEqual(rclone.assert_clean(argv), argv)


class VersionTests(unittest.TestCase):
    def test_a_version_line_is_parsed(self):
        self.assertEqual(rclone.parse_version("rclone v1.75.1\n- os/version: ubuntu"), (1, 75, 1))

    def test_an_unparseable_version_returns_none(self):
        self.assertIsNone(rclone.parse_version("no version here"))

    def test_find_honours_the_override(self):
        found = rclone.find(env={rclone.BINARY_ENV: "/bin/sh"})

        self.assertEqual(found, "/bin/sh")

    def test_find_refuses_an_override_that_is_not_executable(self):
        with self.assertRaises(rclone.RcloneError) as caught:
            rclone.find(env={rclone.BINARY_ENV: "/nonexistent/rclone"})

        self.assertIn(rclone.BINARY_ENV, str(caught.exception))


class NdjsonTests(unittest.TestCase):
    def test_plain_lines_are_skipped_and_objects_are_kept(self):
        text = "\n".join(
            [
                "2026/10/04 19:00:00 INFO  : Starting sync",
                '{"level":"notice","msg":"a.txt: Deleted","object":"a.txt","skipped":"delete"}',
                "not json at all",
                '{"level":"info","msg":"b.txt: Copied","object":"b.txt"}',
            ]
        )

        records = rclone.parse_ndjson(text)

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["object"], "a.txt")

    def test_a_truncated_final_line_does_not_raise(self):
        text = '{"level":"info"}\n{"level":"in'

        self.assertEqual(len(rclone.parse_ndjson(text)), 1)

    def test_empty_input_is_empty_output(self):
        self.assertEqual(rclone.parse_ndjson(""), [])
        self.assertEqual(rclone.parse_ndjson(None), [])

    def test_final_stats_returns_the_last_stats_object(self):
        text = "\n".join(
            [
                '{"level":"info","stats":{"deletes":1,"errors":0}}',
                '{"level":"info","stats":{"deletes":3,"errors":0}}',
            ]
        )

        stats = rclone.final_stats(rclone.parse_ndjson(text))

        self.assertEqual(stats["deletes"], 3)

    def test_final_stats_is_none_when_absent(self):
        self.assertIsNone(rclone.final_stats(rclone.parse_ndjson('{"level":"info"}')))

    def test_object_events_collect_the_destructive_intents(self):
        text = "\n".join(
            [
                json.dumps({
                    "level": "notice", "object": "old.pdf", "objectType": "file",
                    "skipped": "move into backup dir", "size": 10,
                }),
                json.dumps({
                    "level": "notice", "object": "trash", "objectType": "directory",
                    "skipped": "remove directory",
                }),
                json.dumps({"level": "info", "object": "new.pdf", "objectType": "file"}),
            ]
        )

        events = rclone.object_events(rclone.parse_ndjson(text))

        self.assertEqual([event["object"] for event in events], ["old.pdf", "trash"])
        self.assertEqual(events[0]["skipped"], "move into backup dir")
        self.assertEqual(events[0]["size"], 10)

    def test_is_fatal_reads_the_flag_and_the_level(self):
        self.assertTrue(rclone.is_fatal(rclone.parse_ndjson('{"level":"info","fatalError":true}')))
        self.assertTrue(rclone.is_fatal(rclone.parse_ndjson('{"level":"fatal","msg":"boom"}')))
        self.assertFalse(rclone.is_fatal(rclone.parse_ndjson('{"level":"info","msg":"fine"}')))


class MinimumVersionTests(unittest.TestCase):
    def test_the_floor_is_a_three_part_tuple(self):
        self.assertEqual(len(rclone.MINIMUM_VERSION), 3)
        self.assertTrue(all(isinstance(part, int) for part in rclone.MINIMUM_VERSION))

    def test_the_installed_version_clears_the_floor(self):
        # Guards against a floor edited above the version this project was verified against.
        self.assertGreaterEqual((1, 75, 1), rclone.MINIMUM_VERSION)

    def test_required_flags_and_commands_are_declared(self):
        # doctor checks these against the binary's own help; a missing guardrail must be found
        # before a run, not during one.
        self.assertIn("--max-delete", rclone.REQUIRED_FLAGS)
        self.assertIn("cryptcheck", rclone.REQUIRED_COMMANDS)
        self.assertIn("--immutable", rclone.REQUIRED_FLAGS)


if __name__ == "__main__":
    unittest.main()
