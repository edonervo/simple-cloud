"""Building a plan: dry run, then read what it would do.

This is the read-only half of the tool. It runs rclone with `--dry-run` and writes nothing
anywhere except a plan file under `state/plans/`. The apply path lives in `runner.py` and is not
reachable from here.

Two decisions shape the whole module:

**The plan is evidence, not inference.** What a run would do is read from the per-object NDJSON
records — `{object, objectType, skipped}` — not from the aggregate stats counters. The stats
object is a logging-subsystem detail with no compatibility guarantee; it is used only as the
aggregate veto (W1: did anything error?). The distinction matters because the gate has to
enumerate *which* paths would be deleted to check them against `protect` and against the budget,
and a counter cannot be enumerated.

**The argv is computed once, here, and stored verbatim.** Including the timestamped trash path.
The runner replays it with only `--dry-run` removed, so a run crossing midnight cannot write to a
different path than the one that was approved (R3).
"""

import json
import os
from dataclasses import dataclass, field
from typing import Optional

from . import guards, rclone
from . import state as state_module
from .config import COPY, Backup, Pair

# Exit codes, mapped explicitly rather than tested for zero (W2). rclone's own table, with the
# one entry that is most often got wrong called out: 9 means "no files transferred", which is a
# clean no-op, not a failure.
EXIT_OK = 0
EXIT_NO_TRANSFER = 9
EXIT_TEMPORARY = 5

# 1 syntax/usage -- 3 directory not found -- 4 file not found -- 6 something else -- 7 fatal --
# 8 transfer limit -- 10 max transfer. All are holds for this project: none of them mean "the
# run did less than you asked and that is fine".
HOLD_CODES = (1, 3, 4, 6, 7, 8, 10)


def classify_exit(code):
    """Map an rclone exit code to (ok, note). `ok` means the run may be considered successful."""
    if code == EXIT_OK:
        return True, "ok"
    if code == EXIT_NO_TRANSFER:
        # W2: treating this as failure would raise a false alarm on every idempotent run.
        return True, "no files transferred"
    if code == EXIT_TEMPORARY:
        return False, "temporary error; the job should be retried"
    if code in HOLD_CODES:
        return False, f"rclone exited {code}"
    return False, f"unrecognised rclone exit code {code}"


@dataclass
class Plan:
    """What a run would do, and the exact argv that would do it."""

    job: str
    kind: str  # "sync" | "backup"
    local: str
    remote: str
    mode: str
    argv: list[str]
    dry_argv: list[str]
    created_at: str
    deletes: list[str] = field(default_factory=list)
    transfers: int = 0
    errors: int = 0
    fatal: bool = False
    exit_code: int = 0
    trash_dir: Optional[str] = None
    source_manifest: dict = field(default_factory=dict)
    destination_count: int = 0
    findings: list[guards.Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def eligible(self):
        """True when nothing holds the plan. Eligible is not the same as safe: R12 still asks."""
        return not self.findings

    def to_dict(self):
        return {
            "job": self.job,
            "kind": self.kind,
            "local": self.local,
            "remote": self.remote,
            "mode": self.mode,
            "argv": list(self.argv),
            "dry_argv": list(self.dry_argv),
            "created_at": self.created_at,
            "deletes": sorted(self.deletes),
            "transfers": self.transfers,
            "errors": self.errors,
            "fatal": self.fatal,
            "exit_code": self.exit_code,
            "trash_dir": self.trash_dir,
            "source_manifest": self.source_manifest,
            "destination_count": self.destination_count,
            "findings": [
                {"rule": finding.rule, "message": finding.message} for finding in self.findings
            ],
            "notes": list(self.notes),
        }


def trash_dir_for(pair, moment=None):
    """The timestamped sibling trash path for a run.

    A sibling of the destination, never inside it: nested, the next run reads the trash as
    destination-only files and moves the trash into the trash (R2).
    """
    if pair.trash_root is None:
        return None
    return f"{pair.trash_root}/{pair.name}/{state_module.timestamp(moment)}"


def list_files(runner, binary, config_path, path, hashes=False, missing_is_empty=False):
    """List a path as JSON records. Returns (records, note); records is None on failure.

    `missing_is_empty` distinguishes the two directions, and the distinction matters:

    * **Destination** (True) -- absent is normal on a first run, and rclone reports it as an error
      on some backends. Treating it as empty is what lets a first sync proceed.
    * **Source** (False) -- absent is the exact failure W4 exists for. The config layer already
      confirmed the directory exists, so a "not found" here means it moved, unmounted, or lost
      its permissions since. Reporting that as an empty source would let the run read the entire
      destination as disposable, which is the mistake the guardrails are for.
    """
    argv = rclone.lsjson_argv(binary, config_path, path, hashes=hashes)
    completed = runner(argv)
    if completed.returncode != 0:
        text = (completed.stderr or "") + (completed.stdout or "")
        lowered = text.lower()
        if missing_is_empty and ("not found" in lowered or "no such file" in lowered):
            return [], text
        return None, text

    try:
        payload = json.loads(completed.stdout or "[]")
    except ValueError as error:
        return None, f"could not parse lsjson output: {error}"
    if not isinstance(payload, list):
        return None, "lsjson did not return a list"
    return payload, ""


def files_only(entries):
    return [entry for entry in entries if not entry.get("IsDir")]


def build_sync_plan(pair, config, binary, runner=None, moment=None, last_manifest=None):
    """Plan one sync pair. Runs `--dry-run`; writes nothing to any remote."""
    runner = rclone.run if runner is None else runner

    trash = trash_dir_for(pair, moment)
    argv = rclone.sync_argv(pair, binary, config.rclone_config, trash_dir=trash, dry_run=False)
    dry_argv = rclone.sync_argv(pair, binary, config.rclone_config, trash_dir=trash, dry_run=True)
    rclone.assert_clean(argv)

    created = state_module.isoformat(moment)
    plan = Plan(
        job=pair.name,
        kind="sync",
        local=pair.local,
        remote=str(pair.remote),
        mode=pair.mode,
        argv=argv,
        dry_argv=dry_argv,
        created_at=created,
        trash_dir=trash,
    )

    source_entries, note = list_files(runner, binary, config.rclone_config, pair.local)
    if source_entries is None:
        plan.errors = 1
        plan.notes.append(f"could not list the source: {note.strip()[:400]}")
        plan.findings = guards.evaluate(
            _evidence(pair, plan, source=(), last_manifest=last_manifest)
        )
        return plan

    source_files = files_only(source_entries)
    plan.source_manifest = state_module.manifest_for(source_files, moment)

    destination_entries, note = list_files(
        runner, binary, config.rclone_config, str(pair.remote), missing_is_empty=True
    )
    if destination_entries is None:
        # An unreadable destination is a hold, not an empty destination: assuming zero objects
        # would silently disable the mass-delete guard (R16).
        plan.errors = 1
        plan.notes.append(f"could not list the destination: {note.strip()[:400]}")
        plan.findings = guards.evaluate(
            _evidence(pair, plan, source=source_files, last_manifest=last_manifest)
        )
        return plan
    plan.destination_count = len(destination_entries)

    completed = runner(dry_argv)
    plan.exit_code = completed.returncode

    stream = (completed.stdout or "") + (completed.stderr or "")
    records = rclone.parse_ndjson(stream)
    plan.fatal = rclone.is_fatal(records)

    stats = rclone.final_stats(records)
    if stats is not None:
        plan.errors = _as_int(stats.get("errors"))
        plan.transfers = _as_int(stats.get("transfers"))

    # The enumerated evidence, not the counters.
    destructive = {"move into backup dir", "delete", "remove directory"}
    deletes = []
    for event in rclone.object_events(records):
        if event["skipped"] in destructive:
            deletes.append(event["object"])
    plan.deletes = sorted(set(deletes))

    ok, note = classify_exit(completed.returncode)
    if not ok:
        plan.notes.append(note)
        plan.errors = max(plan.errors, 1)
    elif note == "no files transferred":
        plan.notes.append("nothing to transfer")

    if plan.fatal:
        plan.notes.append("the dry run aborted; its plan is partial and will not be applied")

    plan.findings = guards.evaluate(
        _evidence(pair, plan, source=source_files, last_manifest=last_manifest)
    )
    return plan


def build_backup_plan(backup, config, binary, runner=None, moment=None, last_manifest=None):
    """Plan one backup job. `copy`, so there is nothing to delete and no trash."""
    runner = rclone.run if runner is None else runner

    argv = rclone.copy_argv(backup, binary, config.rclone_config, dry_run=False)
    dry_argv = rclone.copy_argv(backup, binary, config.rclone_config, dry_run=True)
    rclone.assert_clean(argv)

    plan = Plan(
        job=backup.name,
        kind="backup",
        local=backup.local,
        remote=str(backup.remote),
        mode=COPY,
        argv=argv,
        dry_argv=dry_argv,
        created_at=state_module.isoformat(moment),
    )

    source_entries, note = list_files(runner, binary, config.rclone_config, backup.local)
    if source_entries is None:
        plan.errors = 1
        plan.notes.append(f"could not list the source: {note.strip()[:400]}")
        plan.findings = guards.evaluate(
            _evidence(backup, plan, source=(), last_manifest=last_manifest)
        )
        return plan

    source_files = files_only(source_entries)
    plan.source_manifest = state_module.manifest_for(source_files, moment)

    completed = runner(dry_argv)
    plan.exit_code = completed.returncode
    stream = (completed.stdout or "") + (completed.stderr or "")
    records = rclone.parse_ndjson(stream)
    plan.fatal = rclone.is_fatal(records)

    stats = rclone.final_stats(records)
    if stats is not None:
        plan.errors = _as_int(stats.get("errors"))
        plan.transfers = _as_int(stats.get("transfers"))

    ok, note = classify_exit(completed.returncode)
    if not ok:
        plan.notes.append(note)
        plan.errors = max(plan.errors, 1)

    plan.findings = guards.evaluate(
        _evidence(backup, plan, source=source_files, last_manifest=last_manifest)
    )
    return plan


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _evidence(job, plan, source, last_manifest):
    """Assemble guard evidence from a job and its plan.

    `job` is a Pair or a Backup; both carry the fields the guards read.
    """
    return guards.evidence_for(
        job,
        job=plan.job,
        local=plan.local,
        remote=plan.remote,
        mode=plan.mode,
        deletes=tuple(plan.deletes),
        transfers=plan.transfers,
        errors=plan.errors,
        fatal=plan.fatal,
        source_files=tuple(entry.get("Path", "") for entry in source),
        destination_count=plan.destination_count,
        last_source_count=(last_manifest or {}).get("file_count"),
    )


def select_jobs(config, names):
    """Resolve requested job names to jobs, or raise with the unknown ones."""
    everything = list(config.pairs) + list(config.backups)
    if not names:
        return everything
    by_name = {job.name: job for job in everything}
    unknown = [name for name in names if name not in by_name]
    if unknown:
        known = ", ".join(sorted(by_name)) or "none"
        raise KeyError(f"unknown job(s): {', '.join(unknown)}. Known: {known}")
    return [by_name[name] for name in names]


def sync_pairs(jobs):
    return [job for job in jobs if isinstance(job, Pair)]


def backups_only(jobs):
    return [job for job in jobs if isinstance(job, Backup)]


def summarise(plan) -> tuple[str, list[str]]:
    """A one-line headline and detail lines for a human."""
    if plan.fatal:
        headline = f"HOLD  {plan.job}: the dry run aborted, so no plan can be trusted"
    elif plan.findings:
        headline = f"HOLD  {plan.job}: {len(plan.findings)} guard(s) fired"
    elif not plan.transfers and not plan.deletes:
        headline = f"OK    {plan.job}: nothing to do"
    else:
        headline = (
            f"OK    {plan.job}: {plan.transfers} transfer(s), {len(plan.deletes)} deletion(s)"
        )

    details = []
    for finding in plan.findings:
        details.append(f"  - {finding}")
    for note in plan.notes:
        details.append(f"  . {note}")
    for deleted in plan.deletes[:20]:
        details.append(f"    delete {deleted}")
    if len(plan.deletes) > 20:
        details.append(f"    ... and {len(plan.deletes) - 20} more")
    return headline, details


def local_paths_in(plans):
    """Every local path a plan touches, for the L1 assertion: the tool reads these, never writes."""
    return [plan.local for plan in plans]


def assert_local_is_never_a_destination(plans):
    """L1, checked on real plans rather than only in the argv unit tests.

    For sync and copy, rclone takes source then destination. If a local path ever appeared in the
    destination position, the run would write into the local directory.
    """
    problems = []
    for plan in plans:
        argv = plan.argv
        if plan.kind == "sync" and "sync" in argv:
            index = argv.index("sync")
        elif plan.kind == "backup" and "copy" in argv:
            index = argv.index("copy")
        else:
            continue
        if index + 2 >= len(argv):
            problems.append(f"{plan.job}: argv is too short to contain a source and a destination")
            continue
        source, destination = argv[index + 1], argv[index + 2]
        if not os.path.isabs(source) and ":" not in source:
            problems.append(f"{plan.job}: source {source!r} is neither a local path nor a remote")
        if destination == plan.local:
            problems.append(f"{plan.job}: the local path is the destination")
        if ":" not in destination:
            problems.append(
                f"{plan.job}: destination {destination!r} is not a remote, so rclone would "
                "write locally"
            )
    return problems
