"""Tests for sync_gdrive.py.

These pin the behaviour that must survive the Phase 4 refactors: the pairing validation, the
shape of the rclone invocation, and the exit status. ``log_message`` is patched everywhere so
the suite never writes a log file into the repository.

Only ``subprocess.run`` is patched (not the whole ``subprocess`` module), so
``subprocess.CalledProcessError`` inside the module under test is still the real class.
"""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import sync_gdrive  # noqa: E402


class PairingTests(unittest.TestCase):
    def test_the_directory_lists_are_the_same_length(self):
        self.assertEqual(len(sync_gdrive.LOCAL_DIRS), len(sync_gdrive.REMOTE_DIRS))

    def test_mismatched_counts_exit(self):
        with mock.patch.object(sync_gdrive, "LOCAL_DIRS", ["/tmp"]), \
             mock.patch.object(sync_gdrive, "REMOTE_DIRS", []), \
             mock.patch.object(sync_gdrive, "log_message"):
            with self.assertRaises(SystemExit):
                sync_gdrive.check_local_directories()

    def test_a_missing_directory_exits(self):
        with mock.patch.object(sync_gdrive.os.path, "isdir", return_value=False), \
             mock.patch.object(sync_gdrive, "log_message"):
            with self.assertRaises(SystemExit):
                sync_gdrive.check_local_directories()


class RcloneCheckTests(unittest.TestCase):
    def test_a_missing_rclone_exits(self):
        with mock.patch.object(sync_gdrive.subprocess, "run", side_effect=FileNotFoundError()), \
             mock.patch.object(sync_gdrive, "log_message"):
            with self.assertRaises(SystemExit):
                sync_gdrive.check_rclone_installed()

    def test_a_broken_rclone_exits(self):
        error = subprocess.CalledProcessError(returncode=1, cmd=["rclone", "version"])
        with mock.patch.object(sync_gdrive.subprocess, "run", side_effect=error), \
             mock.patch.object(sync_gdrive, "log_message"):
            with self.assertRaises(SystemExit):
                sync_gdrive.check_rclone_installed()


class SyncInvocationTests(unittest.TestCase):
    def _run(self, behaviour):
        """Run sync_directories with subprocess.run replaced by ``behaviour(command)``."""
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return behaviour(command)

        with mock.patch.object(sync_gdrive.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(sync_gdrive, "log_message"):
            result = sync_gdrive.sync_directories()
        return calls, result

    @staticmethod
    def _success(command):
        return mock.Mock(returncode=0, stdout=b"", stderr=b"")

    @staticmethod
    def _failure(command):
        raise subprocess.CalledProcessError(returncode=1, cmd=command, stderr=b"boom")

    def test_one_rclone_sync_per_directory_pair(self):
        calls, _ = self._run(self._success)
        self.assertEqual(len(calls), len(sync_gdrive.LOCAL_DIRS))

    def test_each_call_is_an_interactive_sync_of_local_to_remote(self):
        calls, _ = self._run(self._success)
        for call, (local, remote) in zip(calls, zip(sync_gdrive.LOCAL_DIRS, sync_gdrive.REMOTE_DIRS)):
            self.assertEqual(call[:3], ["rclone", "sync", "--interactive"])
            self.assertEqual(call[3], local)
            self.assertEqual(call[4], remote)

    def test_a_clean_run_returns_zero(self):
        _, result = self._run(self._success)
        self.assertEqual(result, 0)

    def test_a_failed_pair_returns_non_zero(self):
        # Before the refactor this returned None after printing "Sync completed successfully".
        _, result = self._run(self._failure)
        self.assertEqual(result, 1)

    def test_a_failed_pair_does_not_stop_the_remaining_pairs(self):
        calls, _ = self._run(self._failure)
        self.assertEqual(len(calls), len(sync_gdrive.LOCAL_DIRS))


class LogMessageTests(unittest.TestCase):
    def test_the_log_file_lives_next_to_the_script(self):
        self.assertTrue(os.path.isabs(sync_gdrive.LOG_FILE))
        self.assertEqual(
            os.path.dirname(sync_gdrive.LOG_FILE),
            os.path.dirname(os.path.abspath(sync_gdrive.__file__)),
        )

    def test_log_message_appends_to_the_configured_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "log.txt")
            with mock.patch.object(sync_gdrive, "LOG_FILE", target), \
                 contextlib.redirect_stdout(io.StringIO()):
                sync_gdrive.log_message("first")
                sync_gdrive.log_message("second")
            with open(target) as fh:
                self.assertEqual(fh.read(), "first\nsecond\n")


class MainTests(unittest.TestCase):
    def test_main_returns_the_sync_status(self):
        with mock.patch.object(sync_gdrive, "check_rclone_installed"), \
             mock.patch.object(sync_gdrive, "check_local_directories"), \
             mock.patch.object(sync_gdrive, "sync_directories", return_value=1):
            self.assertEqual(sync_gdrive.main(), 1)


if __name__ == "__main__":
    unittest.main()
