"""Tests for cloudsync.cli — the exit-code contract, --json, and the phase-1 no-write invariant.

Every test injects a `FakeRclone`, so no test can reach the real binary, the network, or a
remote. `env` is pointed at an executable that is never actually run, because the runner is
faked — `rclone.find` only checks that the path is executable.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

from cloudsync import cli, rclone
from cloudsync import state as state_module
from test.fake_rclone import FakeRclone, entries

BINARY = sys.executable  # any executable; the runner is faked, so it is never invoked.

FULL_HELP = " ".join(rclone.REQUIRED_FLAGS) + " " + " ".join(rclone.REQUIRED_COMMANDS)


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = os.path.realpath(self._tmp.name)

        self.documents = os.path.join(self.tmp, "Documents")
        self.books = os.path.join(self.tmp, "Books")
        for path in (self.documents, self.books):
            os.makedirs(path, exist_ok=True)

        self.rclone_config = os.path.join(self.tmp, "rclone.conf")
        with open(self.rclone_config, "w", encoding="utf-8") as handle:
            handle.write("[edo-remote]\ntype = drive\n")

        self.config_path = os.path.join(self.tmp, "cloudsync.ini")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write(self.config_body())

    def config_body(self, extra="", rclone_config=None):
        return (
            "[general]\n"
            f"rclone_config = {rclone_config or self.rclone_config}\n"
            "state_dir = state\n"
            "\n"
            "[pair:documents]\n"
            f"local = {self.tmp}/Documents\n"
            "remote = edo-remote:Documents\n"
            "max_delete = 25\n"
            "trash_root = edo-remote:_cloudsync-trash\n"
            "\n"
            "[pair:books]\n"
            f"local = {self.tmp}/Books\n"
            "remote = edo-remote:Books\n"
            "max_delete = 25\n"
            "trash_root = edo-remote:_cloudsync-trash\n"
            f"{extra}"
        )

    def write_config(self, name, **kwargs):
        """Write a variant config file and return its path.

        Used by the tests that need the config to be wrong in one specific way — most often a
        `rclone_config` that does not exist, which the shared fixture cannot express because its
        own rclone config is real.
        """
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.config_body(**kwargs))
        return path

    def run_cli(self, argv, fake=None):
        """Run main(), returning (exit_code, stdout, stderr).

        `--config` is always injected unless the test supplied one of its own. Without this a
        test would silently fall through to the repository's own cloudsync.ini and write plan
        files into the real state directory — which is exactly what happened the first time this
        suite was written, and is why the injection is here rather than left to each test.
        """
        fake = FakeRclone() if fake is None else fake
        if "--config" not in argv:
            argv = ["--config", self.config_path] + list(argv)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(
                argv,
                runner=fake,
                env={rclone.BINARY_ENV: BINARY},
            )
        return code, out.getvalue(), err.getvalue()

    @property
    def state_dir(self):
        return os.path.join(self.tmp, "state")


class UsageTests(CliTestCase):
    def test_no_command_prints_help_and_exits_usage(self):
        code, out, _ = self.run_cli([])

        self.assertEqual(code, cli.STATUS_USAGE)
        self.assertIn("usage:", out)

    def test_a_missing_config_exits_usage(self):
        code, _, err = self.run_cli(["--config", os.path.join(self.tmp, "absent.ini"), "status"])

        self.assertEqual(code, cli.STATUS_USAGE)
        self.assertIn("configuration error", err)

    def test_an_invalid_config_exits_usage_with_every_problem(self):
        path = os.path.join(self.tmp, "bad.ini")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("[general]\n[pair:p]\nlocal = relative\nremote = nowhere:\n")

        code, _, err = self.run_cli(["--config", path, "status"])

        self.assertEqual(code, cli.STATUS_USAGE)
        self.assertIn("rclone_config is required", err)
        self.assertIn("must be absolute", err)

    def test_an_unknown_job_exits_usage(self):
        code, _, err = self.run_cli(["plan", "nope"], fake=FakeRclone())

        self.assertEqual(code, cli.STATUS_USAGE)
        self.assertIn("unknown job", err)


class StatusTests(CliTestCase):
    def test_status_reports_every_job(self):
        fake = FakeRclone()
        code, out, _ = self.run_cli(["status"], fake=fake)

        self.assertEqual(code, cli.STATUS_OK)
        self.assertIn("documents", out)
        self.assertIn("books", out)
        self.assertIn("mirror", out)

    def test_status_says_the_apply_path_does_not_exist_yet(self):
        _, out, _ = self.run_cli(["status"], fake=FakeRclone())

        self.assertIn("no apply path", out)

    def test_status_json_is_one_object_with_the_expected_keys(self):
        code, out, _ = self.run_cli(["status", "--json"], fake=FakeRclone())

        payload = json.loads(out)
        self.assertEqual(code, cli.STATUS_OK)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(len(payload["jobs"]), 2)
        self.assertEqual(payload["remotes"], ["edo-remote"])
        self.assertTrue(payload["stale"])

    def test_status_makes_no_rclone_calls_at_all(self):
        # status is a read of local files and config: it must not depend on the network.
        fake = FakeRclone()
        self.run_cli(["status"], fake=fake)

        self.assertEqual(fake.calls, [])

    def test_status_shows_a_recorded_manifest(self):
        state_module.ensure(state_module.Paths(self.state_dir))
        paths = state_module.Paths(self.state_dir)
        state_module.save_state(
            paths,
            state_module.record_good({}, "documents", {"file_count": 42}),
        )

        _, out, _ = self.run_cli(["status"], fake=FakeRclone())

        self.assertIn("42", out)


class DoctorTests(CliTestCase):
    def test_a_healthy_environment_passes(self):
        fake = FakeRclone(help_flags=FULL_HELP, remotes="edo-remote:\n")
        code, out, _ = self.run_cli(["doctor"], fake=fake)

        self.assertEqual(code, cli.STATUS_OK, out)
        self.assertIn("0 failure(s)", out)

    def missing_rclone_config(self):
        """A config file that is valid apart from pointing at an rclone config that is not there.

        The shared fixture creates a real `rclone.conf` — its other checks need one — so a test
        about a *missing* rclone config has to ask for this variant explicitly. Pointing at the
        fixture's own file instead made the check pass, and the test then asserted on whichever
        unrelated check happened to fail first.
        """
        return self.write_config(
            "missing-rclone.ini", rclone_config=os.path.join(self.tmp, "absent-rclone.conf")
        )

    def test_a_missing_rclone_config_fails(self):
        fake = FakeRclone(help_flags=FULL_HELP, remotes="edo-remote:\n")
        code, out, _ = self.run_cli(
            ["--config", self.missing_rclone_config(), "doctor"], fake=fake
        )

        self.assertEqual(code, cli.STATUS_HELD)
        self.assertIn("rclone config file", out)

    def test_a_missing_rclone_config_is_reported_in_json(self):
        fake = FakeRclone(help_flags=FULL_HELP)
        _, out, _ = self.run_cli(
            ["--config", self.missing_rclone_config(), "doctor", "--json"], fake=fake
        )

        payload = json.loads(out)
        names = [check["name"] for check in payload["checks"] if not check["ok"]]
        self.assertIn("rclone config file", names)

    def test_an_undeclared_remote_fails_the_doctor(self):
        fake = FakeRclone(help_flags=FULL_HELP, remotes="something-else:\n")
        code, out, _ = self.run_cli(["doctor"], fake=fake)

        self.assertEqual(code, cli.STATUS_HELD)
        self.assertIn("edo-remote", out)

    def test_a_missing_guardrail_flag_fails_the_doctor(self):
        # A guardrail that silently does not exist on this binary is worse than no guardrail.
        fake = FakeRclone(help_flags="sync copy", remotes="edo-remote:\n")
        code, out, _ = self.run_cli(["doctor"], fake=fake)

        self.assertEqual(code, cli.STATUS_HELD)
        self.assertIn("rclone capabilities", out)

    def test_an_outdated_version_fails_the_doctor(self):
        fake = FakeRclone(help_flags=FULL_HELP, version="rclone v1.40.0\n", remotes="edo-remote:\n")
        code, out, _ = self.run_cli(["doctor"], fake=fake)

        self.assertEqual(code, cli.STATUS_HELD)
        self.assertIn("older than", out)

    def test_a_stale_schedule_warns_without_failing(self):
        fake = FakeRclone(help_flags=FULL_HELP, remotes="edo-remote:\n")
        code, out, _ = self.run_cli(["doctor"], fake=fake)

        self.assertEqual(code, cli.STATUS_OK)
        self.assertIn("warn", out)
        self.assertIn("no run has ever been recorded", out)

    def test_a_backup_written_to_a_plain_sync_destination_fails(self):
        # The check that stops an "encrypted" backup from landing unencrypted in the same place
        # as the plain sync. Phase 1 declares no [backup:*] jobs, so without a test of its own
        # this branch would never run until Phase 3 -- i.e. exactly when it matters most.
        extra = (
            "\n[backup:documents]\n"
            f"local = {self.documents}\n"
            "remote = edo-remote:Documents\n"
        )
        path = self.write_config("with-backup.ini", extra=extra)
        fake = FakeRclone(help_flags=FULL_HELP, remotes="edo-remote:\n")

        code, out, _ = self.run_cli(["--config", path, "doctor"], fake=fake)

        self.assertEqual(code, cli.STATUS_HELD)
        self.assertIn("backup encryption", out)
        self.assertIn("edo-remote", out)
        self.assertIn("unencrypted", out)


class PlanTests(CliTestCase):
    def clean_fake(self, **kwargs):
        return FakeRclone(
            source=entries("a.txt", "b.txt"),
            destination=entries(*[f"d{index}" for index in range(100)]),
            help_flags=FULL_HELP,
            remotes="edo-remote:\n",
            **kwargs
        )

    def test_a_clean_plan_exits_ok(self):
        code, out, _ = self.run_cli(["plan"], fake=self.clean_fake())

        self.assertEqual(code, cli.STATUS_OK, out)

    def test_a_plan_never_runs_a_write(self):
        # The phase-1 invariant, asserted on the calls the tool actually made: every sync argv
        # carries --dry-run, so nothing could have been written to a remote.
        fake = self.clean_fake()
        self.run_cli(["plan"], fake=fake)

        self.assertEqual(fake.wrote_anything(), [])

    def test_a_plan_runs_the_dry_run_with_a_backup_dir(self):
        fake = self.clean_fake()
        self.run_cli(["plan", "documents"], fake=fake)

        argvs = fake.argvs_for("sync")
        self.assertEqual(len(argvs), 1)
        self.assertIn("--dry-run", argvs[0])
        self.assertIn("--backup-dir", argvs[0])
        self.assertIn("--max-delete", argvs[0])

    def test_every_sync_argv_carries_the_explicit_config(self):
        fake = self.clean_fake()
        self.run_cli(["plan"], fake=fake)

        for argv in fake.argvs_for("sync"):
            self.assertEqual(argv[1:3], ["--config", self.rclone_config])

    def test_a_planned_deletion_holds_and_exits_one(self):
        fake = FakeRclone(
            source=entries("a.txt"),
            destination=entries(*[f"d{index}" for index in range(100)]),
            deletions=("a.txt",),
            help_flags=FULL_HELP,
        )
        code, out, _ = self.run_cli(["plan"], fake=fake)

        self.assertEqual(code, cli.STATUS_HELD)
        self.assertIn("R12", out)
        self.assertIn("a.txt", out)

    def test_an_empty_source_holds(self):
        fake = FakeRclone(source=[], destination=entries("d0"), help_flags=FULL_HELP)
        code, out, _ = self.run_cli(["plan", "documents"], fake=fake)

        self.assertEqual(code, cli.STATUS_HELD)
        self.assertIn("W4", out)

    def test_a_fatal_dry_run_holds_and_says_the_plan_is_partial(self):
        fake = self.clean_fake(fatal=True)
        code, out, _ = self.run_cli(["plan", "documents"], fake=fake)

        self.assertEqual(code, cli.STATUS_HELD)
        self.assertIn("W6", out)
        self.assertIn("partial", out.lower())

    def test_exit_nine_is_read_as_success_not_failure(self):
        # W2: rclone exits 9 when nothing was transferred. Treating that as a failure would
        # raise a false alarm on every idempotent run.
        fake = self.clean_fake(exit_code=9)
        code, out, _ = self.run_cli(["plan"], fake=fake)

        self.assertEqual(code, cli.STATUS_OK, out)

    def test_a_temporary_error_holds(self):
        fake = self.clean_fake(exit_code=5)
        code, out, _ = self.run_cli(["plan", "documents"], fake=fake)

        self.assertEqual(code, cli.STATUS_HELD)
        self.assertIn("retried", out)

    def test_a_plan_is_saved_under_the_state_directory(self):
        self.run_cli(["plan", "documents"], fake=self.clean_fake())

        plans = state_module.list_plans(state_module.Paths(self.state_dir))
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["job"], "documents")

    def test_nothing_is_saved_when_the_plan_is_not_saved(self):
        self.run_cli(["plan", "--no-save"], fake=self.clean_fake())

        self.assertEqual(state_module.list_plans(state_module.Paths(self.state_dir)), [])

    def test_the_saved_plan_stores_the_argv_verbatim_for_replay(self):
        # R3: the apply phase replays this argv with only --dry-run removed, so the stored one
        # must not carry it.
        fake = self.clean_fake()
        self.run_cli(["plan", "documents"], fake=fake)

        saved = state_module.read_plan(
            state_module.list_plans(state_module.Paths(self.state_dir))[0]["path"]
        )
        self.assertNotIn("--dry-run", saved["argv"])
        self.assertIn("--dry-run", saved["dry_argv"])
        self.assertIn("--backup-dir", saved["argv"])

    def test_plan_json_carries_the_findings(self):
        fake = FakeRclone(
            source=entries("a.txt"),
            destination=entries(*[f"d{index}" for index in range(100)]),
            deletions=("a.txt",),
            help_flags=FULL_HELP,
        )
        capi, out, _ = self.run_cli(["plan", "--json"], fake=fake)

        payload = json.loads(out)
        self.assertEqual(capi, cli.STATUS_HELD)
        self.assertEqual(payload["status"], "held")
        rules = [finding["rule"] for finding in payload["plans"][0]["findings"]]
        self.assertIn("R12", rules)

    def test_a_named_job_plans_only_that_job(self):
        fake = self.clean_fake()
        self.run_cli(["plan", "books"], fake=fake)

        saved = state_module.list_plans(state_module.Paths(self.state_dir))
        self.assertEqual([entry["job"] for entry in saved], ["books"])

    def test_an_unreadable_source_is_a_hold_not_a_crash(self):
        fake = FakeRclone(source_missing=True, help_flags=FULL_HELP)
        code, out, _ = self.run_cli(["plan", "documents"], fake=fake)

        self.assertEqual(code, cli.STATUS_HELD)
        self.assertIn("W1", out)


class LockTests(CliTestCase):
    def test_a_held_lock_exits_three(self):
        paths = state_module.Paths(self.state_dir)
        holder = state_module.Lock(paths)
        holder.acquire()
        self.addCleanup(holder.release)

        code, _, err = self.run_cli(["plan"], fake=FakeRclone())

        self.assertEqual(code, cli.STATUS_LOCKED)
        self.assertIn("another run holds the lock", err)

    def test_the_lock_is_released_after_a_run(self):
        self.run_cli(["plan"], fake=FakeRclone(source=entries("a.txt")))

        self.assertFalse(os.path.exists(state_module.Paths(self.state_dir).lock))

    def test_the_lock_is_released_even_when_a_job_holds(self):
        fake = FakeRclone(source=[], destination=entries("d"))
        self.run_cli(["plan", "documents"], fake=fake)

        self.assertFalse(os.path.exists(state_module.Paths(self.state_dir).lock))


class RcloneFailureTests(CliTestCase):
    def test_a_missing_binary_exits_held_with_a_clear_message(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(
                ["plan"],
                runner=FakeRclone(),
                env={rclone.BINARY_ENV: os.path.join(self.tmp, "absent")},
            )

        self.assertEqual(code, cli.STATUS_HELD)
        self.assertIn("rclone error", err.getvalue())

    def test_interrupt_exits_held_and_says_nothing_was_applied(self):
        err = io.StringIO()

        class Interrupting(FakeRclone):
            def __call__(self, argv):
                raise KeyboardInterrupt()

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = cli.main(
                ["plan"], runner=Interrupting(), env={rclone.BINARY_ENV: BINARY}
            )

        self.assertEqual(code, cli.STATUS_HELD)
        self.assertIn("nothing was applied", err.getvalue())


if __name__ == "__main__":
    unittest.main()
