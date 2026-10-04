"""Locating rclone, reading its version, and building argv.

**Every builder in this module is a pure function.** Same inputs, same list; no I/O, no clock, no
environment. That is not a style preference — it is what makes R3 hold. The runner computes the
full argv once at plan time, stores it, and replays it at apply time with `--dry-run` removed. If
the timestamped trash path were recomputed at apply, a run crossing midnight would write to a
different path than the one the owner approved. Here it cannot be: the timestamp is an argument,
and the two calls differ only in `dry_run`.

`run()` is therefore the only function here that touches the outside world, and it is
deliberately the dumbest one.

Every flag the project relies on was checked against the installed binary rather than against
documentation for some other release (`rclone help flags`, v1.75.1). See docs/rclone-cheatsheet.md.
"""

import json
import os
import re
import shutil
import subprocess

# A conservative floor, chosen rather than derived: every flag used here (`--max-delete`,
# `--backup-dir`, `--use-json-log`, `--error-on-no-transfer`, `cryptcheck`, `lsjson`) predates it
# by a wide margin. The installed and verified version is 1.75.1.
#
# A version number is a weak check on its own -- a distribution can patch flags out -- so
# `doctor` also confirms each required flag is present in this binary's own `help flags` output.
# The version floor is the cheap first gate; the flag inventory is the real one.
MINIMUM_VERSION = (1, 60, 0)

# Flags this project must never pass, with the reason each one is here. Enforced by
# `banned_flags_in`, which the runner calls on every argv before it is stored or executed, so a
# future edit cannot introduce one by accident. Each reason cites the guardrail that replaces it.
BANNED_FLAGS = {
    "--track-renames": (
        "R13: converts a delete+upload into a server-side move that overwrites the rename target "
        "with no backup, and bypasses --max-delete entirely -- the operation becomes invisible "
        "to the gate"
    ),
    "--immutable": (
        "R14: under `sync` this does not prevent deletion, only modification, so it reads as "
        "protection while breaking every legitimate content change"
    ),
    "--delete-excluded": (
        "deletes the very paths `--exclude` was asked to protect"
    ),
    "--interactive": (
        "blocks on a TTY; the old scripts passed it, and under cron it is a hang, not a prompt"
    ),
    "--ignore-errors": (
        "keeps going after errors; W1 holds the job on any error, so this would convert a hold "
        "into a partial write"
    ),
    "--suffix-keep-extension": (
        "R2: the trash path is timestamped per run precisely because rclone overwrites an "
        "existing suffixed path; changing how suffixes are built breaks that reasoning"
    ),
}

# `--stats` is not banned, but zero is: it suppresses the final stats line the budget gate reads.
STATS_ZERO_REASON = (
    "W1/R1: --stats 0 suppresses the final stats line, which carries the error count and the "
    "delete count the gate depends on"
)

# The flags doctor confirms exist. If one is missing, the guardrail it implements is not
# available on this binary, and running anyway would be worse than refusing.
REQUIRED_FLAGS = (
    "--max-delete",
    "--backup-dir",
    "--dry-run",
    "--use-json-log",
    "--error-on-no-transfer",
    "--stats",
    "--exclude",
    "--include",
    "--config",
    "--immutable",
    "--track-renames",
)

REQUIRED_COMMANDS = (
    "sync", "copy", "check", "cryptcheck", "lsjson", "about", "lsd", "size", "listremotes",
)

_VERSION_RE = re.compile(r"v(\d+)\.(\d+)\.(\d+)")

# Set by the test suite to a fake binary. Honoured before PATH so a test can never reach the
# real rclone even if one is installed.
BINARY_ENV = "CLOUDSYNC_RCLONE"


class RcloneError(Exception):
    """rclone is missing, unusable, or too old for the guardrails this project needs."""


def find(env=None):
    """Locate the rclone binary. Returns a path, or raises RcloneError."""
    env = os.environ if env is None else env

    override = env.get(BINARY_ENV, "").strip()
    if override:
        if not os.path.isfile(override) or not os.access(override, os.X_OK):
            raise RcloneError(
                f"{BINARY_ENV} is set to {override!r}, which is not an executable file"
            )
        return override

    found = shutil.which("rclone", path=env.get("PATH"))
    if found:
        return found

    for candidate in (
        os.path.expanduser("~/.local/bin/rclone"),
        "/usr/local/bin/rclone",
        "/usr/bin/rclone",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    raise RcloneError(
        f"rclone was not found. Install it, or set {BINARY_ENV} to its path; "
        "`cloudsync doctor` reports which it found."
    )


def parse_version(text):
    """`rclone v1.75.1` -> (1, 75, 1). Returns None if no version is present."""
    match = _VERSION_RE.search(text or "")
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def version(binary, runner=None):
    """Return (tuple, raw_text). Raises RcloneError if the version cannot be read."""
    runner = run if runner is None else runner
    try:
        completed = runner([binary, "version"])
    except OSError as error:
        raise RcloneError(f"could not execute {binary}: {error}") from error
    text = (completed.stdout or "") + (completed.stderr or "")
    parsed = parse_version(text)
    if parsed is None:
        raise RcloneError(f"could not parse a version from {text.strip()[:200]!r}")
    return parsed, text.strip().splitlines()[0] if text.strip() else ""


def banned_flags_in(argv):
    """Return [(flag, reason)] for every banned flag present. Empty list means clean.

    Handles all three spellings rclone accepts: `--flag`, `--flag=value` and `--flag value`.
    """
    found = []
    for index, token in enumerate(argv):
        name = token.split("=", 1)[0]
        if name in BANNED_FLAGS:
            found.append((name, BANNED_FLAGS[name]))
        elif name == "--stats":
            if "=" in token:
                value = token.split("=", 1)[1]
            else:
                value = argv[index + 1] if index + 1 < len(argv) else ""
            if value.strip() in ("0", "0s", "0m"):
                found.append(("--stats 0", STATS_ZERO_REASON))
    return found


def assert_clean(argv):
    """Raise RcloneError if the argv carries a banned flag. Used before storing or running."""
    banned = banned_flags_in(argv)
    if banned:
        reasons = "; ".join(f"{flag} ({reason})" for flag, reason in banned)
        raise RcloneError(f"refusing to run: banned flag(s) present -- {reasons}")
    return argv


def _common(binary, rclone_config):
    return [binary, "--config", rclone_config]


def _logging():
    """Shared logging flags. `--stats` is explicit and never zero (see STATS_ZERO_REASON)."""
    return ["--stats", "1m", "--use-json-log", "--log-level", "INFO", "--error-on-no-transfer"]


def _excludes(patterns):
    argv = []
    for pattern in patterns:
        argv += ["--exclude", pattern]
    return argv


def sync_argv(pair, binary, rclone_config, trash_dir=None, dry_run=False):
    """The argv for one sync pair.

    `trash_dir` is the *already timestamped* backup directory, computed once by the caller at
    plan time and stored. Passing it in rather than deriving it is what keeps apply identical to
    the approved plan (R3).

    A `copy` pair gets neither `--max-delete` nor `--backup-dir`: it cannot delete, so both would
    be meaningless, and the config layer already refuses a copy job that sets a budget.
    """
    argv = _common(binary, rclone_config)
    argv += ["sync", pair.local, str(pair.remote)]
    argv += _logging()

    if pair.mode == "mirror":
        # R1: a finite budget, never 0 -- with --backup-dir a deletion-to-trash still counts as a
        # deletion, and 0 makes the first pending deletion fatal even under --dry-run.
        argv += ["--max-delete", str(pair.max_delete)]
        if trash_dir:
            # R2: a sibling of the destination, timestamped per run.
            argv += ["--backup-dir", trash_dir]

    argv += _excludes(pair.exclude)

    if dry_run:
        argv.append("--dry-run")
    return argv


def copy_argv(job, binary, rclone_config, dry_run=False):
    """The argv for one backup job. Never deletes, never carries a budget."""
    argv = _common(binary, rclone_config)
    argv += ["copy", job.local, str(job.remote)]
    argv += _logging()
    argv += _excludes(getattr(job, "exclude", ()) or ())

    if dry_run:
        argv.append("--dry-run")
    return argv


def lsjson_argv(binary, rclone_config, path, hashes=False):
    """List a path as JSON. Directories are included so a file/directory type change is visible.

    Hashes are off by default: `--hash` makes rclone read every file, which over 12 GB across 9p
    is expensive. Only the paths a plan will touch are hashed (R4).
    """
    argv = _common(binary, rclone_config)
    argv += ["lsjson", path, "--recursive"]
    if hashes:
        argv.append("--hash")
    return argv


def size_argv(binary, rclone_config, path):
    argv = _common(binary, rclone_config)
    return argv + ["size", path, "--json"]


def about_argv(binary, rclone_config, remote_name):
    argv = _common(binary, rclone_config)
    return argv + ["about", f"{remote_name}:", "--json"]


def lsd_argv(binary, rclone_config, remote_name):
    argv = _common(binary, rclone_config)
    return argv + ["lsd", f"{remote_name}:"]


def listremotes_argv(binary, rclone_config):
    argv = _common(binary, rclone_config)
    return argv + ["listremotes"]


def config_dump_argv(binary, rclone_config):
    """`config dump` prints the whole config **including secrets**. Named so it is greppable and
    so any use of it is obviously deliberate; nothing in this project calls it. Remote names and
    types come from `listremotes` plus `config` section headers instead."""
    argv = _common(binary, rclone_config)
    return argv + ["config", "dump"]


def check_argv(binary, rclone_config, local, remote):
    """Verify a sync one way: every source file must exist and match at the destination (R11)."""
    argv = _common(binary, rclone_config)
    return argv + ["check", local, str(remote), "--one-way"]


def cryptcheck_argv(binary, rclone_config, local, remote):
    """Verify an encrypted backup against its plaintext source (R11)."""
    argv = _common(binary, rclone_config)
    return argv + ["cryptcheck", local, str(remote)]


def run(argv, timeout=None, env=None):
    """Execute an argv. The only non-pure function in this module.

    Never raises on a non-zero exit: rclone's exit codes are data (W2), and several of them are
    not failures. Callers map them.
    """
    assert_clean(argv)
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def parse_ndjson(text):
    """Parse `--use-json-log` output.

    The stream is **NDJSON**, one JSON object per line, not one JSON document. It is also
    interleaved with plain, non-JSON lines, so a line that does not parse is skipped rather than
    treated as an error. Returns the objects that did parse.
    """
    records = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def final_stats(records):
    """The last `stats` object in a parsed stream, or None.

    Used **only** as the aggregate budget gate. It is a logging-subsystem detail with no
    compatibility guarantee, so it is never the source of truth for *what* would change -- the
    per-object records are (see `object_events`).
    """
    for record in reversed(records):
        stats = record.get("stats")
        if isinstance(stats, dict):
            return stats
    return None


def object_events(records):
    """Per-object evidence: what rclone decided about each path.

    Entries whose `skipped` names a destructive intent are the ones the gate cares about:
    `move into backup dir`, `delete`, `remove directory`. Kept alongside the rest so the caller
    can tell "nothing happened to this path" from "this path was rewritten".
    """
    events = []
    for record in records:
        skipped = record.get("skipped")
        object_name = record.get("object")
        if not object_name or not isinstance(skipped, str):
            continue
        events.append(
            {
                "object": object_name,
                "object_type": record.get("objectType"),
                "skipped": skipped,
                "size": record.get("size"),
                "level": record.get("level"),
            }
        )
    return events


def is_fatal(records):
    """True if the run ended in a fatal error (W6).

    A fatal run's stats describe a *partial* run, so its plan must be discarded rather than
    gated against or replayed.
    """
    for record in records:
        if str(record.get("fatalError", "")).lower() in ("true", "1"):
            return True
        if record.get("level") == "fatal":
            return True
    return False
