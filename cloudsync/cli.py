"""The command line: argument parsing, dispatch, and the exit-code contract.

Phase 1 ships three commands — `status`, `doctor` and `plan` — and none of them can write to a
remote. That is the point: there is no apply path in this phase at all, so the tool cannot lose
data even when misused. `runner.py` adds the writing half in Phase 2.

Exit codes are a documented contract, not an accident, and they match the convention already in
`scripts/secret_guard.py`:

| Code | Meaning |
|---|---|
| `0` | ok |
| `1` | held, or a run failed |
| `2` | usage or configuration error |
| `3` | another run holds the lock |

Every command accepts `--json`. Commands are registered by an `add_*_parser(sub)` function that
owns its own flags, so a later phase can contribute a subcommand without touching this file's
dispatch, and each command is testable by calling `main([...])` directly — no subprocess.
"""

import argparse
import os
import sys
from typing import Optional

from . import config as config_module
from . import doctor as doctor_module
from . import plan as plan_module
from . import rclone, report
from . import state as state_module

STATUS_OK = 0
STATUS_HELD = 1
STATUS_USAGE = 2
STATUS_LOCKED = 3

# Resolved relative to the package, so the tool works from any working directory -- which matters
# because a scheduled job starts in an arbitrary one.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(REPO_ROOT, "cloudsync.ini")

# How old the last recorded run may be before `doctor` calls the schedule stale. Monthly cadence
# plus a few days of slack, so a run that is merely late is not reported as broken.
CADENCE_DAYS = 35

PLAN_ONLY_NOTE = (
    "Phase 1 has no apply path: `plan` runs rclone with --dry-run and writes nothing to any "
    "remote. Applying is added in Phase 2."
)


def _add_globals(parser, subcommand=False):
    """Add `--config` and `--json` so both `cloudsync --json status` and `cloudsync status --json`
    work.

    The subcommand copies use `default=SUPPRESS`, which means "do not set this attribute at all
    if the flag is absent". Without it a subparser would overwrite a value already given before
    the subcommand with its own default -- the classic argparse parents pitfall, and one that
    would silently redirect the tool at the wrong config file.
    """
    parser.add_argument(
        "--config",
        default=argparse.SUPPRESS if subcommand else DEFAULT_CONFIG,
        help="path to cloudsync.ini",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS if subcommand else False,
        help="emit one JSON object",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cloudsync",
        description="Configurable directory sync and encrypted backup, with deletion guardrails.",
    )
    _add_globals(parser)

    sub = parser.add_subparsers(dest="command")
    add_status_parser(sub)
    add_doctor_parser(sub)
    add_plan_parser(sub)
    return parser


# --------------------------------------------------------------------------------------------
# Subcommand registration. Each owns its own flags.
# --------------------------------------------------------------------------------------------


def add_status_parser(sub):
    parser = sub.add_parser("status", help="config, jobs and run history — read-only")
    _add_globals(parser, subcommand=True)
    parser.set_defaults(func=cmd_status)
    return parser


def add_doctor_parser(sub):
    parser = sub.add_parser("doctor", help="environment preflight — read-only")
    _add_globals(parser, subcommand=True)
    parser.set_defaults(func=cmd_doctor)
    return parser


def add_plan_parser(sub):
    parser = sub.add_parser(
        "plan", help="dry run and gate the named jobs; writes nothing to any remote"
    )
    _add_globals(parser, subcommand=True)
    parser.add_argument("jobs", nargs="*", help="job names (default: every job)")
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="do not save the plan under state/plans/",
    )
    parser.set_defaults(func=cmd_plan)
    return parser


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------


def cmd_status(args, runner=None, env=None, moment=None):
    config = config_module.load(args.config)
    paths = state_module.Paths(config.state_dir)
    state = state_module.load_state(paths) if os.path.isfile(paths.state_file) else {}

    jobs = []
    rows = []
    for pair in config.pairs:
        entry = state_module.last_good(state, pair.name) or {}
        manifest = entry.get("manifest") or {}
        jobs.append(
            {
                "name": pair.name,
                "kind": "sync",
                "local": pair.local,
                "remote": str(pair.remote),
                "mode": pair.mode,
                "max_delete": pair.max_delete,
                "trash_root": str(pair.trash_root) if pair.trash_root else None,
                "approve_deletes": pair.approve_deletes,
                "last_file_count": manifest.get("file_count"),
                "last_recorded_at": entry.get("recorded_at"),
            }
        )
        rows.append(
            [
                pair.name,
                "sync",
                pair.mode,
                str(manifest.get("file_count", "-")),
                str(entry.get("recorded_at", "never"))[:19],
            ]
        )

    backup_rows = []
    for backup in config.backups:
        jobs.append(
            {
                "name": backup.name,
                "kind": "backup",
                "local": backup.local,
                "remote": str(backup.remote),
                "mode": "copy",
            }
        )
        backup_rows.append([backup.name, "backup", "copy", "-", "-"])

    stale, staleness = state_module.is_stale(state, CADENCE_DAYS, moment=moment)
    plans = state_module.list_plans(paths)

    payload = {
        "status": report.STATUS_OK,
        "config": args.config,
        "state_dir": paths.root,
        "on_warning": config.on_warning,
        "rclone_config": config.rclone_config,
        "remotes": list(config.remote_names),
        "jobs": jobs,
        "runs_recorded": len(state.get("runs") or []),
        "plans_available": [entry["name"] for entry in plans],
        "stale": stale,
        "staleness": staleness,
    }

    lines = [
        f"config      {args.config}",
        f"state       {paths.root}",
        f"rclone cfg  {config.rclone_config}",
        f"remotes     {', '.join(config.remote_names) or 'none'}",
        f"on warning  {config.on_warning}",
        "",
    ]
    if rows or backup_rows:
        lines.append("jobs")
        lines += report.table(
            rows + backup_rows, headers=["name", "kind", "mode", "files", "last good"]
        )
    else:
        lines.append("jobs        none")
    lines.append("")
    lines.append(f"schedule    {staleness}")
    lines.append(f"plans       {', '.join(entry['name'] for entry in plans) or 'none'}")
    lines.append("")
    lines.append(PLAN_ONLY_NOTE)
    report.emit(payload, lines, as_json=args.json)
    return STATUS_OK


def cmd_doctor(args, runner=None, env=None, moment=None):
    config = config_module.load(args.config)
    paths = state_module.Paths(config.state_dir)
    state = state_module.load_state(paths) if os.path.isfile(paths.state_file) else {}

    checks, _binary = doctor_module.run_checks(
        config, paths, state, state_module, env=env, runner=runner, moment=moment,
        cadence_days=CADENCE_DAYS,
    )
    failures = doctor_module.failed(checks)
    warnings = doctor_module.warned(checks)

    payload = {
        "status": report.STATUS_HELD if failures else report.STATUS_OK,
        "checks": [
            {
                "name": check.name,
                "ok": check.ok,
                "fatal": check.fatal,
                "detail": check.detail,
                "remedy": check.remedy,
            }
            for check in checks
        ],
        "failures": len(failures),
        "warnings": len(warnings),
    }

    lines = doctor_module.format_checks(checks)
    lines.append("")
    lines.append(f"doctor: {len(failures)} failure(s), {len(warnings)} warning(s)")
    if failures:
        lines.append("Nothing will run until the failures above are fixed.")
    report.emit(payload, lines, as_json=args.json)
    return STATUS_HELD if failures else STATUS_OK


def cmd_plan(args, runner=None, env=None, moment=None):
    config = config_module.load(args.config)
    paths = state_module.Paths(config.state_dir)
    state = state_module.load_state(paths) if os.path.isfile(paths.state_file) else {}

    try:
        jobs = plan_module.select_jobs(config, args.jobs)
    except KeyError as error:
        sys.stderr.write(f"{error.args[0]}\n")
        return STATUS_USAGE

    if not jobs:
        sys.stderr.write("no jobs to plan\n")
        return STATUS_USAGE

    lock = state_module.Lock(paths)
    with lock:
        plans = []
        for job in jobs:
            last = state_module.last_good(state, job.name) or {}
            if isinstance(job, config_module.Pair):
                built = plan_module.build_sync_plan(
                    job, config, _binary(env),
                    runner=runner, moment=moment,
                    last_manifest=last.get("manifest"),
                )
            else:
                built = plan_module.build_backup_plan(
                    job, config, _binary(env),
                    runner=runner, moment=moment,
                    last_manifest=last.get("manifest"),
                )
            plans.append(built)

        # L1, asserted on the plans actually built rather than only in the argv unit tests.
        problems = plan_module.assert_local_is_never_a_destination(plans)
        if problems:
            bulleted = "\n  - ".join(problems)
            sys.stderr.write(f"refusing to continue:\n  - {bulleted}\n")
            return STATUS_USAGE

        if not args.no_save:
            for built in plans:
                state_module.write_plan(paths, built.to_dict(), moment=moment)

    payload = {
        "status": report.STATUS_HELD if any(p.findings for p in plans) else report.STATUS_OK,
        "plans": [built.to_dict() for built in plans],
    }

    lines = [PLAN_ONLY_NOTE, ""]
    for built in plans:
        headline, details = plan_module.summarise(built)
        lines.append(headline)
        lines.extend(details)
        lines.append("")

    held = [built for built in plans if built.findings]
    lines.append(f"plan: {len(plans)} job(s), {len(held)} held")
    if held and not args.no_save:
        lines.append("Review and apply with `cloudsync run --approve <plan>` (Phase 2).")

    report.emit(payload, lines, as_json=args.json)
    return STATUS_HELD if held else STATUS_OK


def _binary(env=None):
    return rclone.find(env=env)


# --------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None, runner=None, env=None, moment=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "func", None):
        parser.print_help()
        return STATUS_USAGE

    try:
        return args.func(args, runner=runner, env=env, moment=moment)
    except config_module.ConfigError as error:
        sys.stderr.write(f"configuration error:\n{error}\n")
        return STATUS_USAGE
    except state_module.Locked as error:
        sys.stderr.write(f"{error}\n")
        return STATUS_LOCKED
    except state_module.StateError as error:
        sys.stderr.write(f"state error: {error}\n")
        return STATUS_HELD
    except rclone.RcloneError as error:
        sys.stderr.write(f"rclone error: {error}\n")
        return STATUS_HELD
    except KeyboardInterrupt:
        sys.stderr.write("interrupted; nothing was applied\n")
        return STATUS_HELD
