"""Tests for cloudsync.config — one case per refusal rule.

The refusal rules *are* the safety property, so each one gets a test that would fail if the
rule were dropped. The rules that exist for non-obvious reasons (the `max_delete`/`trash_root`
interaction, the sibling-trash requirement) are grouped and carry a comment saying what rclone
does that makes them necessary.

Local paths must exist to validate, so every case builds real directories under a temporary
directory. Nothing here touches the network, the remote, or the repository.
"""

import os
import tempfile
import unittest

from cloudsync import config as config_module
from cloudsync.config import ConfigError


class ConfigTestCase(unittest.TestCase):
    """Builds a config file from a template, substituting {tmp} with a scratch directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = os.path.realpath(self._tmp.name)
        # Two real source directories, so the common cases have somewhere to point.
        self.documents = os.path.join(self.tmp, "Documents")
        self.books = os.path.join(self.tmp, "Books")
        for path in (self.documents, self.books):
            os.makedirs(path, exist_ok=True)
        self.conf_dir = os.path.join(self.tmp, "conf")
        os.makedirs(self.conf_dir, exist_ok=True)
        self.rclone_config = os.path.join(self.conf_dir, "rclone.conf")
        with open(self.rclone_config, "w", encoding="utf-8") as handle:
            handle.write("[edo-remote]\ntype = drive\n")

    def write(self, body):
        path = os.path.join(self.conf_dir, "cloudsync.ini")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body.replace("{tmp}", self.tmp).replace("{rclone}", self.rclone_config))
        return path

    def load(self, body):
        return config_module.load(self.write(body))

    def refuse(self, body):
        """Assert the config is refused; return the list of problems."""
        with self.assertRaises(ConfigError) as caught:
            self.load(body)
        return caught.exception.problems

    def assertRefused(self, body, fragment):
        problems = self.refuse(body)
        joined = "\n".join(problems)
        self.assertIn(fragment, joined, f"expected {fragment!r} in:\n{joined}")
        return problems


GENERAL = """
[general]
rclone_config = {rclone}
"""

MINIMAL_PAIR = """
[pair:documents]
local = {tmp}/Documents
remote = edo-remote:Documents
"""


class HappyPathTests(ConfigTestCase):
    def test_a_minimal_config_loads_with_the_documented_defaults(self):
        loaded = self.load(GENERAL + MINIMAL_PAIR)

        self.assertEqual(len(loaded.pairs), 1)
        pair = loaded.pairs[0]
        self.assertEqual(pair.name, "documents")
        self.assertEqual(pair.local, self.documents)
        self.assertEqual(str(pair.remote), "edo-remote:Documents")
        self.assertEqual(pair.mode, config_module.MIRROR)
        self.assertEqual(pair.max_delete, config_module.DEFAULT_MAX_DELETE)
        self.assertEqual(pair.approve_deletes, config_module.APPROVE_ALWAYS)
        self.assertEqual(pair.max_shrink_percent, config_module.DEFAULT_MAX_SHRINK_PERCENT)
        self.assertEqual(pair.min_files, config_module.DEFAULT_MIN_FILES)
        self.assertIsNone(pair.trash_root)
        self.assertTrue(pair.can_delete)
        self.assertTrue(pair.any_delete_needs_approval)

    def test_state_dir_is_resolved_against_the_config_file_not_the_working_directory(self):
        # A cron job starts in an arbitrary directory; if this were cwd-relative, two runs
        # could disagree about where the lock and the run history live.
        loaded = self.load(GENERAL + MINIMAL_PAIR)

        self.assertEqual(loaded.state_dir, os.path.join(self.conf_dir, "state"))

    def test_an_absolute_state_dir_is_left_alone(self):
        loaded = self.load(
            "[general]\nrclone_config = {rclone}\nstate_dir = /var/tmp/cloudsync-state\n"
            + MINIMAL_PAIR
        )

        self.assertEqual(loaded.state_dir, "/var/tmp/cloudsync-state")

    def test_remote_names_is_the_allowlist(self):
        loaded = self.load(
            GENERAL
            + MINIMAL_PAIR
            + """
[pair:books]
local = {tmp}/Books
remote = edo-remote:Books
trash_root = edo-remote:_cloudsync-trash

[backup:documents]
local = {tmp}/Documents
remote = backup-crypt:Documents
"""
        )

        self.assertEqual(loaded.remote_names, ("backup-crypt", "edo-remote"))

    def test_a_backup_job_is_always_a_copy_and_can_never_delete(self):
        loaded = self.load(
            GENERAL
            + MINIMAL_PAIR
            + """
[backup:documents]
local = {tmp}/Documents
remote = backup-crypt:Documents
"""
        )

        backup = loaded.backups[0]
        self.assertEqual(backup.mode, config_module.COPY)
        self.assertFalse(backup.can_delete)

    def test_lists_are_split_and_trimmed(self):
        loaded = self.load(
            GENERAL
            + MINIMAL_PAIR
            + "protect = *.pdf, *.docx\n"
            + "exclude = .DS_Store,Thumbs.db\n"
        )

        self.assertEqual(loaded.pairs[0].protect, ("*.pdf", "*.docx"))
        self.assertEqual(loaded.pairs[0].exclude, (".DS_Store", "Thumbs.db"))

    def test_a_comment_and_a_percent_sign_survive_parsing(self):
        # interpolation=None: a bare % in a path must not raise from deep inside configparser.
        loaded = self.load(
            GENERAL + MINIMAL_PAIR + "protect = 100%*.pdf\n"
        )

        self.assertEqual(loaded.pairs[0].protect, ("100%*.pdf",))


class LocalPathTests(ConfigTestCase):
    def test_a_relative_local_is_refused(self):
        self.assertRefused(
            GENERAL + "[pair:p]\nlocal = Documents\nremote = edo-remote:Documents\n",
            "must be absolute",
        )

    def test_a_local_containing_dotdot_is_refused(self):
        self.assertRefused(
            GENERAL + "[pair:p]\nlocal = {tmp}/Documents/../Books\nremote = edo-remote:B\n",
            "must not contain '..'",
        )

    def test_a_missing_local_is_refused(self):
        self.assertRefused(
            GENERAL + "[pair:p]\nlocal = {tmp}/Nope\nremote = edo-remote:Nope\n",
            "does not exist",
        )

    def test_a_local_that_is_a_file_is_refused(self):
        target = os.path.join(self.tmp, "a-file")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("x")

        self.assertRefused(
            GENERAL + f"[pair:p]\nlocal = {target}\nremote = edo-remote:X\n",
            "not a directory",
        )

    def test_the_filesystem_root_is_refused(self):
        self.assertRefused(
            GENERAL + "[pair:p]\nlocal = /\nremote = edo-remote:X\n",
            "filesystem root",
        )

    def test_the_home_directory_itself_is_refused(self):
        self.assertRefused(
            GENERAL + "[pair:p]\nlocal = ~\nremote = edo-remote:X\n",
            "home directory itself",
        )

    def test_a_symlinked_local_is_refused(self):
        # A symlink can be repointed without this config changing, which would silently move
        # the sync source out from under an approved plan.
        link = os.path.join(self.tmp, "link")
        os.symlink(self.documents, link)

        self.assertRefused(
            GENERAL + f"[pair:p]\nlocal = {link}\nremote = edo-remote:X\n",
            "is a symlink",
        )

    def test_a_tilde_local_is_expanded_before_the_existence_check(self):
        # The error must name the *expanded* path, which is what proves `~` was resolved
        # rather than treated as a relative directory called "~".
        problems = self.refuse(
            GENERAL + "[pair:p]\nlocal = ~/cloudsync-absent-fixture\nremote = edo-remote:X\n"
        )

        self.assertIn(os.path.expanduser("~/cloudsync-absent-fixture"), "\n".join(problems))


class RemoteTests(ConfigTestCase):
    def test_a_remote_without_a_path_is_refused(self):
        # A job may never address a remote root: `rclone sync x edo-remote:` would mirror into
        # the whole drive.
        self.assertRefused(
            GENERAL + "[pair:p]\nlocal = {tmp}/Documents\nremote = edo-remote:\n",
            "non-empty path",
        )

    def test_a_remote_without_a_colon_is_refused(self):
        self.assertRefused(
            GENERAL + "[pair:p]\nlocal = {tmp}/Documents\nremote = edo-remote\n",
            "must be",
        )

    def test_a_remote_name_is_not_validated_here_and_that_is_deliberate(self):
        # Pinned so the limitation is a decision rather than an accident. The allowlist is
        # derived from the `remote` fields themselves, so checking one against it can never
        # fail -- whatever name is typed joins the set. Only preflight, which reads rclone's
        # own config, can tell whether a remote exists. Adding a check here would be a guard
        # that cannot fire.
        loaded = self.load(
            GENERAL + "[pair:p]\nlocal = {tmp}/Documents\nremote = not-configured-anywhere:X\n"
        )

        self.assertEqual(str(loaded.pairs[0].remote), "not-configured-anywhere:X")

    def test_a_remote_path_containing_dotdot_is_refused(self):
        self.assertRefused(
            GENERAL + "[pair:p]\nlocal = {tmp}/Documents\nremote = edo-remote:a/../b\n",
            "must not contain '..'",
        )

    def test_an_undeclared_trash_remote_is_refused(self):
        self.assertRefused(
            GENERAL
            + MINIMAL_PAIR
            + "trash_root = other-remote:_cloudsync-trash\n",
            "not declared anywhere",
        )


class TrashAndBudgetTests(ConfigTestCase):
    """The two rules that exist because rclone's behaviour is counter-intuitive."""

    def test_max_delete_zero_with_a_trash_is_refused(self):
        # Verified against rclone v1.75.1: with --backup-dir, a deletion-to-trash still counts
        # as a deletion, so --max-delete 0 makes the first pending deletion fatal -- under
        # --dry-run too, which hands the gate a truncated plan instead of a full one.
        problems = self.assertRefused(
            GENERAL + MINIMAL_PAIR + "max_delete = 0\ntrash_root = edo-remote:_cloudsync-trash\n",
            "max_delete must be >= 1 when trash_root is set",
        )
        self.assertIn("dry run fails too", "\n".join(problems))

    def test_a_negative_max_delete_is_refused(self):
        self.assertRefused(GENERAL + MINIMAL_PAIR + "max_delete = -1\n", "must be >= 0")

    def test_a_non_integer_max_delete_is_refused(self):
        self.assertRefused(GENERAL + MINIMAL_PAIR + "max_delete = lots\n", "must be an integer")

    def test_a_trash_inside_the_destination_is_refused(self):
        # A nested trash is re-synced into itself: the next run reads it as destination-only
        # files and moves the trash into the trash.
        self.assertRefused(
            GENERAL
            + MINIMAL_PAIR
            + "trash_root = edo-remote:Documents/_cloudsync-trash\n",
            "is inside the destination",
        )

    def test_a_sibling_trash_is_accepted(self):
        loaded = self.load(
            GENERAL + MINIMAL_PAIR + "trash_root = edo-remote:_cloudsync-trash\n"
        )

        self.assertEqual(str(loaded.pairs[0].trash_root), "edo-remote:_cloudsync-trash")

    def test_a_trash_on_another_declared_remote_is_not_an_overlap(self):
        # The inside-the-destination check compares paths within one remote, so a trash held on
        # a different remote cannot be nested in this job's destination at all.
        loaded = self.load(
            GENERAL
            + MINIMAL_PAIR
            + "trash_root = trash-remote:Documents\n"
            + "[pair:books]\nlocal = {tmp}/Books\nremote = trash-remote:Books\n"
        )

        self.assertEqual(str(loaded.pairs[0].trash_root), "trash-remote:Documents")

    def test_a_trash_on_a_remote_no_job_uses_is_refused(self):
        self.assertRefused(
            GENERAL + MINIMAL_PAIR + "trash_root = trash-remote:Documents\n",
            "not declared anywhere",
        )

    def test_copy_mode_may_not_carry_a_delete_budget(self):
        self.assertRefused(
            GENERAL + MINIMAL_PAIR + "mode = copy\nmax_delete = 5\n",
            "mode = copy cannot carry max_delete",
        )

    def test_a_copy_job_that_omits_max_delete_defaults_to_zero(self):
        # The mirror default must not leak into a copy job, or every copy pair would be
        # refused for carrying a budget it never asked for.
        loaded = self.load(GENERAL + MINIMAL_PAIR + "mode = copy\n")

        self.assertEqual(loaded.pairs[0].max_delete, 0)
        self.assertFalse(loaded.pairs[0].can_delete)

    def test_a_trash_below_the_budget_is_accepted(self):
        loaded = self.load(
            GENERAL + MINIMAL_PAIR + "max_delete = 1\ntrash_root = edo-remote:_cloudsync-trash\n"
        )

        self.assertEqual(loaded.pairs[0].max_delete, 1)


class OverlapTests(ConfigTestCase):
    def test_two_pairs_may_not_share_a_local(self):
        self.assertRefused(
            GENERAL
            + MINIMAL_PAIR
            + "[pair:again]\nlocal = {tmp}/Documents\nremote = edo-remote:Again\n",
            "overlaps",
        )

    def test_a_nested_local_is_refused(self):
        nested = os.path.join(self.documents, "inner")
        os.makedirs(nested, exist_ok=True)

        self.assertRefused(
            GENERAL
            + MINIMAL_PAIR
            + "[pair:inner]\nlocal = {tmp}/Documents/inner\nremote = edo-remote:Inner\n",
            "jobs must not nest",
        )

    def test_sibling_locals_are_accepted(self):
        loaded = self.load(
            GENERAL
            + MINIMAL_PAIR
            + "[pair:books]\nlocal = {tmp}/Books\nremote = edo-remote:Books\n"
        )

        self.assertEqual(len(loaded.pairs), 2)


class EnumFieldTests(ConfigTestCase):
    def test_an_unknown_mode_is_refused(self):
        self.assertRefused(GENERAL + MINIMAL_PAIR + "mode = bisync\n", "mode must be one of")

    def test_an_unknown_approve_policy_is_refused(self):
        self.assertRefused(
            GENERAL + MINIMAL_PAIR + "approve_deletes = never\n",
            "approve_deletes must be one of",
        )

    def test_an_unknown_on_warning_is_refused(self):
        self.assertRefused(
            "[general]\nrclone_config = {rclone}\non_warning = ignore\n" + MINIMAL_PAIR,
            "on_warning must be one of",
        )

    def test_the_permissive_approve_policy_must_be_typed_out(self):
        loaded = self.load(GENERAL + MINIMAL_PAIR + "approve_deletes = over_budget\n")

        self.assertEqual(loaded.pairs[0].approve_deletes, config_module.APPROVE_OVER_BUDGET)
        self.assertFalse(loaded.pairs[0].any_delete_needs_approval)


class GeneralSectionTests(ConfigTestCase):
    def test_a_missing_rclone_config_is_refused(self):
        # Deliberately required: falling back to rclone's default would resolve into
        # ~/.config/rclone, which on this machine is a symlink into a git repo that pushes to
        # GitHub.
        self.assertRefused(
            "[general]\n" + MINIMAL_PAIR,
            "rclone_config is required",
        )

    def test_a_missing_general_section_is_refused(self):
        self.assertRefused(MINIMAL_PAIR, "missing the [general] section")

    def test_a_config_with_no_jobs_is_refused(self):
        self.assertRefused(GENERAL, "nothing to do")

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            config_module.load(os.path.join(self.tmp, "absent.ini"))

        self.assertIn("config file not found", str(caught.exception))

    def test_a_malformed_file_is_refused(self):
        path = os.path.join(self.conf_dir, "broken.ini")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("this is not ini at all\n")

        with self.assertRaises(ConfigError) as caught:
            config_module.load(path)

        self.assertIn("could not be read as INI", str(caught.exception))


class ProblemCollectionTests(ConfigTestCase):
    def test_every_problem_is_reported_at_once(self):
        # Three mistakes should report three, not send the owner round the loop three times.
        problems = self.refuse(
            "[general]\n"
            "[pair:p]\nlocal = relative\nremote = nowhere:\nmode = sideways\n"
        )

        joined = "\n".join(problems)
        self.assertGreaterEqual(len(problems), 4)
        self.assertIn("rclone_config is required", joined)
        self.assertIn("must be absolute", joined)
        self.assertIn("mode must be one of", joined)
        self.assertIn("non-empty path", joined)

    def test_a_single_problem_reads_as_one_sentence(self):
        with self.assertRaises(ConfigError) as caught:
            self.load("[general]\n" + MINIMAL_PAIR)

        self.assertNotIn("problems:", str(caught.exception))

    def test_several_problems_are_numbered(self):
        with self.assertRaises(ConfigError) as caught:
            self.load("[pair:p]\nlocal = relative\nremote = nowhere:\n")

        self.assertIn("problems:", str(caught.exception))
        self.assertIn("\n  - ", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
