"""The state directory: last-good manifests, saved plans, run records, and the lock.

Everything this tool writes locally lives under `state_dir`, and nothing else does (L3). That is
asserted by `paths_are_inside_state_dir` and tested, because "the tool only writes under state/"
is a safety claim, and a claim without a check is a hope.

The directory holds file paths, argv, and possibly a credential fragment inside an rclone error
string, which is why it is gitignored *and* listed in the secret guard's REQUIRED_IGNORE_PATTERNS
(scripts/secret_guard.py) — the rule that keeps it out of the repository cannot be deleted
without failing the boundary scan.

Manifests are deliberately small: a count, a byte total, the newest mtime, and a digest of the
sorted paths. The full file list of a 12 GB tree is tens of thousands of entries and none of the
guards need it — W3 compares counts, and R4's freshness check re-runs the dry run rather than
consulting a stored list.
"""

import errno
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

STATE_VERSION = 1

# Subdirectories, created on first use.
PLANS = "plans"
RUNS = "runs"
LOGS = "logs"
LOCK = "lock"
STATE_FILE = "state.json"
LOG_FILE = "cloudsync.log"


class StateError(Exception):
    """The state directory is unusable, or the lock is held."""


class Locked(StateError):
    """Another run holds the lock. Distinct type so the CLI can exit 3 rather than 1."""


def utc_now():
    return datetime.now(timezone.utc)


def timestamp(moment=None):
    """A filesystem- and rclone-safe UTC stamp: 20261004T190000Z.

    Second resolution, and unique per run rather than per day: rclone *overwrites* an existing
    suffixed path, so a day-granular trash name would collide on a same-day rerun and destroy the
    earlier copy it was meant to preserve (R2).
    """
    return (moment or utc_now()).strftime("%Y%m%dT%H%M%SZ")


def isoformat(moment=None):
    return (moment or utc_now()).isoformat()


def digest_paths(paths):
    """A stable digest of a sorted path list. Order-independent, cheap to compare."""
    hasher = hashlib.sha256()
    for path in sorted(paths):
        hasher.update(path.encode("utf-8", "surrogateescape"))
        hasher.update(b"\0")
    return hasher.hexdigest()


@dataclass
class Paths:
    """Every path this tool may write, derived from one root."""

    root: str

    def __post_init__(self):
        self.root = os.path.abspath(os.path.expanduser(self.root))

    @property
    def plans(self):
        return os.path.join(self.root, PLANS)

    @property
    def runs(self):
        return os.path.join(self.root, RUNS)

    @property
    def logs(self):
        return os.path.join(self.root, LOGS)

    @property
    def lock(self):
        return os.path.join(self.root, LOCK)

    @property
    def state_file(self):
        return os.path.join(self.root, STATE_FILE)

    @property
    def log_file(self):
        return os.path.join(self.logs, LOG_FILE)

    def all_dirs(self):
        return [self.root, self.plans, self.runs, self.logs]

    def contains(self, path):
        """True if `path` resolves inside the state root. The L3 assertion, in one place."""
        resolved = os.path.realpath(os.path.abspath(path))
        root = os.path.realpath(self.root)
        return resolved == root or resolved.startswith(root.rstrip(os.sep) + os.sep)


def paths_are_inside_state_dir(paths, candidates):
    """Return the candidates that escape the state directory. Empty means all clear."""
    return [path for path in candidates if not paths.contains(path)]


def ensure(paths):
    """Create the state directory tree. Raises StateError if it cannot."""
    for directory in paths.all_dirs():
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as error:
            raise StateError(f"cannot create {directory}: {error}") from error
    if not os.access(paths.root, os.W_OK):
        raise StateError(f"state directory is not writable: {paths.root}")
    return paths


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as error:
        raise StateError(f"cannot read {path}: {error}") from error


def _write_json(path, payload):
    """Write via a temporary file and rename, so an interrupted write cannot truncate state."""
    temporary = path + ".partial"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except OSError as error:
        raise StateError(f"cannot write {path}: {error}") from error
    return path


# --------------------------------------------------------------------------------------------
# state.json -- the last known-good manifest per job
# --------------------------------------------------------------------------------------------


def load_state(paths):
    payload = _read_json(paths.state_file, {})
    if not isinstance(payload, dict):
        raise StateError(f"{paths.state_file} is not a JSON object")
    payload.setdefault("version", STATE_VERSION)
    payload.setdefault("jobs", {})
    payload.setdefault("runs", [])
    return payload


def save_state(paths, state):
    return _write_json(paths.state_file, state)


def last_good(state, job):
    """The stored manifest for a job, or None if it has never completed a verified run."""
    entry = state.get("jobs", {}).get(job)
    return entry if isinstance(entry, dict) else None


def record_good(state, job, manifest, moment=None):
    """Record a manifest as known-good. Called only after verification passes."""
    state.setdefault("jobs", {})[job] = {
        "manifest": manifest,
        "recorded_at": isoformat(moment),
    }
    return state


def manifest_for(files, moment=None):
    """Build the small manifest the guards need from a list of file records."""
    sizes = [entry.get("Size") or 0 for entry in files]
    newest = None
    for entry in files:
        modified = entry.get("ModTime")
        if isinstance(modified, str) and (newest is None or modified > newest):
            newest = modified
    return {
        "file_count": len(files),
        "bytes": sum(sizes),
        "newest_mtime": newest,
        "paths_digest": digest_paths([entry.get("Path", "") for entry in files]),
    }


# --------------------------------------------------------------------------------------------
# plans/
# --------------------------------------------------------------------------------------------


def plan_path(paths, job, moment=None):
    return os.path.join(paths.plans, f"{job}-{timestamp(moment)}.json")


def write_plan(paths, plan, moment=None):
    """Save a plan. The moment is passed in rather than read from the clock, so a caller that
    already stamped the plan uses the same instant for the filename and its contents."""
    return _write_json(plan_path(paths, plan["job"], moment), plan)


def read_plan(path):
    payload = _read_json(path, None)
    if payload is None:
        raise StateError(f"plan not found: {path}")
    if not isinstance(payload, dict) or "argv" not in payload:
        raise StateError(f"{path} is not a plan this tool wrote")
    return payload


def list_plans(paths):
    """Saved plans, newest first. Unreadable files are skipped rather than aborting a listing."""
    try:
        names = os.listdir(paths.plans)
    except FileNotFoundError:
        return []
    plans = []
    for name in names:
        if not name.endswith(".json"):
            continue
        full = os.path.join(paths.plans, name)
        try:
            payload = read_plan(full)
        except StateError:
            continue
        plans.append({"path": full, "job": payload.get("job"), "name": name})
    return sorted(plans, key=lambda entry: entry["name"], reverse=True)


# --------------------------------------------------------------------------------------------
# runs/ and logs/
# --------------------------------------------------------------------------------------------


def write_run(paths, job, record, moment=None):
    path = os.path.join(paths.runs, f"{job}-{timestamp(moment)}.json")
    return _write_json(path, record)


def append_log(paths, line):
    """Append one line to the human log. Never raises: a log failure must not fail a run."""
    try:
        ensure(paths)
        with open(paths.log_file, "a", encoding="utf-8") as handle:
            handle.write(f"{isoformat()} {line}\n")
    except (OSError, StateError):
        return False
    return True


def tail_log(paths, count=20):
    try:
        with open(paths.log_file, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except (OSError, StateError):
        return []
    return lines[-count:]


# --------------------------------------------------------------------------------------------
# The lock (L5)
# --------------------------------------------------------------------------------------------


class Lock:
    """An exclusive lock file, so two runs cannot interleave (L5).

    O_CREAT|O_EXCL, so acquiring is atomic. The holder's pid and start time are written into the
    file purely so a human reading it can tell whether the owner is still alive — this code never
    reclaims a lock on its own, because guessing wrong about a live run is exactly the failure the
    lock exists to prevent.
    """

    def __init__(self, paths: Paths):
        self.paths = paths
        self.path = paths.lock
        self.acquired = False

    def acquire(self, moment=None):
        ensure(self.paths)
        try:
            handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError as error:
            if error.errno == errno.EEXIST:
                raise Locked(
                    f"another run holds the lock at {self.path} ({self._describe()}). "
                    "If no run is active, remove that file."
                ) from error
            raise StateError(f"cannot create {self.path}: {error}") from error

        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"pid": os.getpid(), "started_at": isoformat(moment)}) + "\n")
        self.acquired = True
        return self

    def _describe(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return "contents unreadable"
        pid = payload.get("pid")
        return f"pid {pid} since {payload.get('started_at')}"

    def release(self):
        if not self.acquired:
            return False
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise StateError(f"cannot remove {self.path}: {error}") from error
        self.acquired = False
        return True

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return False


def is_stale(state, cadence_days, moment=None):
    """True if the last recorded run is older than the cadence, or there is none.

    Returns (stale, description). This is what `doctor` reports, so a schedule that has quietly
    stopped is visible rather than silent.
    """
    runs = state.get("runs") or []
    if not runs:
        return True, "no run has ever been recorded"

    last = None
    for entry in runs:
        finished = entry.get("finished_at")
        if isinstance(finished, str) and (last is None or finished > last):
            last = finished
    if last is None:
        return True, "no completed run has been recorded"

    try:
        finished_at = datetime.fromisoformat(last)
    except ValueError:
        return True, f"the last run's timestamp is unreadable: {last!r}"

    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=timezone.utc)
    age_days = ((moment or utc_now()) - finished_at).total_seconds() / 86400.0
    if age_days > cadence_days:
        return True, (
            f"the last run finished {age_days:.1f} days ago, beyond the {cadence_days}-day cadence"
        )
    return False, f"the last run finished {age_days:.1f} days ago"


def record_run(state, record, limit=200):
    """Append a run record, keeping the most recent `limit` entries."""
    runs: list[dict] = list(state.setdefault("runs", []))
    runs.append(record)
    state["runs"] = runs[-limit:]
    return state


def find_plan_for_job(paths, job) -> Optional[dict]:
    """The most recent saved plan for a job, or None."""
    for entry in list_plans(paths):
        if entry["job"] == job:
            return read_plan(entry["path"])
    return None
