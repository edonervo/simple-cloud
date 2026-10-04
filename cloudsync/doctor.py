"""Environment preflight — `cloudsync doctor`.

Read-only. Every check here is a thing that would otherwise fail *during* a run, which is the
worst time to find out. The remote-existence check lives here rather than in the config layer
because it is the only place that can read rclone's own config and learn which remotes actually
exist; a name checked against a list derived from the same file is a guard that cannot fail.

Two checks exist for reasons specific to this machine, and both are load-bearing:

* **The config path must be outside every git work tree.** rclone's default is
  `~/.config/rclone/rclone.conf`, and `~/.config` here is a symlink to `~/.dotfiles/nvim/.config`
  — inside a repository that pushes to GitHub. Resolving symlinks before walking up is what makes
  this catch the default path rather than the innocuous-looking one.
* **The guardrail flags must exist on the installed binary.** A guardrail that silently does not
  exist on this version is worse than no guardrail, because it reads as protection.
"""

import os
from dataclasses import dataclass
from typing import Optional

from . import config as config_module
from . import rclone


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fatal: bool = True  # a warning, when False
    remedy: str = ""


def inside_git_work_tree(path):
    """Return the work tree a path sits in, or None.

    Symlinks are resolved first. Without that, `~/.config/rclone/rclone.conf` looks like it lives
    in `$HOME` — the symlink to the dotfiles repo is what actually decides where a write lands.
    """
    current = os.path.realpath(os.path.abspath(path))
    if os.path.isfile(current):
        current = os.path.dirname(current)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def check_rclone_binary(env=None):
    try:
        binary = rclone.find(env=env)
    except rclone.RcloneError as error:
        return None, Check(
            "rclone binary",
            False,
            str(error),
            remedy=f"Install rclone, or set {rclone.BINARY_ENV} to its path.",
        )
    return binary, Check("rclone binary", True, binary)


def check_version(binary, runner=None):
    try:
        parsed, line = rclone.version(binary, runner=runner)
    except rclone.RcloneError as error:
        return Check("rclone version", False, str(error), remedy="Reinstall rclone.")

    parsed_text = ".".join(str(part) for part in parsed)
    detail = f"{line} (parsed {parsed_text})"
    if parsed < rclone.MINIMUM_VERSION:
        required = ".".join(str(part) for part in rclone.MINIMUM_VERSION)
        return Check(
            "rclone version",
            False,
            f"{detail} is older than the required {required}",
            remedy="Upgrade rclone; the deletion accounting this project relies on may differ.",
        )
    return Check("rclone version", True, detail)


def check_flags(binary, runner=None):
    """Confirm each required flag and command is present on *this* binary."""
    runner = rclone.run if runner is None else runner
    try:
        flags_out = runner([binary, "help", "flags"])
        help_out = runner([binary, "help"])
    except OSError as error:
        return Check("rclone capabilities", False, f"could not run rclone: {error}")

    flags_text = (flags_out.stdout or "") + (flags_out.stderr or "")
    help_text = (help_out.stdout or "") + (help_out.stderr or "")

    missing_flags = [flag for flag in rclone.REQUIRED_FLAGS if flag not in flags_text]
    missing_commands = [name for name in rclone.REQUIRED_COMMANDS if name not in help_text]

    if missing_flags or missing_commands:
        missing = missing_flags + missing_commands
        return Check(
            "rclone capabilities",
            False,
            f"missing on this binary: {', '.join(missing)}",
            remedy="Upgrade rclone. A guardrail that does not exist on this version is worse "
            "than no guardrail.",
        )
    flags_count = len(rclone.REQUIRED_FLAGS)
    commands_count = len(rclone.REQUIRED_COMMANDS)
    return Check(
        "rclone capabilities",
        True,
        f"{flags_count} flags, {commands_count} commands present",
    )


def check_config_path(config):
    """The config file must exist, and must not sit inside a git work tree."""
    path = config.rclone_config
    if not os.path.isfile(path):
        return Check(
            "rclone config file",
            False,
            f"not found: {path}",
            remedy=f"Run `rclone config` with RCLONE_CONFIG={path} to create the remote.",
        )

    tree = inside_git_work_tree(path)
    if tree:
        return Check(
            "rclone config file",
            False,
            f"{path} sits inside the git work tree {tree}, so a credential could be committed",
            remedy="Point [general] rclone_config at a path outside every repository.",
        )
    return Check("rclone config file", True, f"{path} (outside every git work tree)")


def check_remotes(config, binary, runner=None):
    """Every remote named in the config must exist in rclone's own config."""
    runner = rclone.run if runner is None else runner
    completed = runner(rclone.listremotes_argv(binary, config.rclone_config))
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "").strip()[:200]
        return Check(
            "remotes",
            False,
            f"could not list remotes: {message}",
            remedy="Check that [general] rclone_config points at a valid rclone config.",
        )

    known = {
        line.strip().rstrip(":")
        for line in (completed.stdout or "").splitlines()
        if line.strip()
    }
    declared = set(config.remote_names)
    missing = sorted(name for name in declared if name not in known)

    if missing:
        return Check(
            "remotes",
            False,
            f"declared in cloudsync.ini but absent from rclone's config: {', '.join(missing)}",
            remedy="Create them with `rclone config`, or correct cloudsync.ini.",
        )
    names = ", ".join(sorted(declared))
    return Check("remotes", True, f"{len(declared)} present: {names}")


def check_backup_remotes_are_crypt(config, binary, runner=None):
    """`[backup:*]` must not be a plain sync destination.

    This is what is verifiable *without* reading secrets: a remote's type lives in rclone's config
    alongside its token, and the only command that prints types -- `config dump` -- prints the
    tokens too, so this project never calls it. What is checked here is the structural property
    that matters: a backup is not written to the same remote as an unencrypted sync. Phase 3
    creates the crypt overlay and verifies it with `cryptcheck`, which proves encryption directly.
    """
    if not config.backups:
        return Check("backup encryption", True, "no backup jobs declared")

    plain = {pair.remote.name for pair in config.pairs}
    unencrypted = sorted({backup.remote.name for backup in config.backups} & plain)

    if unencrypted:
        return Check(
            "backup encryption",
            False,
            f"backup remote(s) {', '.join(unencrypted)} are also plain sync destinations, "
            "so the backups would be stored unencrypted",
            remedy="Create a crypt overlay with `cloudsync remote add-crypt` (Phase 3).",
        )
    names = ", ".join(sorted({backup.remote.name for backup in config.backups}))
    return Check(
        "backup encryption",
        True,
        f"backup remote(s) are distinct from every sync destination: {names}",
    )


def check_state_dir(paths, state_module):
    try:
        state_module.ensure(paths)
    except state_module.StateError as error:
        return Check("state directory", False, str(error), remedy="Fix its permissions or path.")
    return Check("state directory", True, paths.root)


def check_lock(paths, state_module):
    if os.path.exists(paths.lock):
        return Check(
            "lock",
            False,
            f"a lock file exists at {paths.lock}",
            remedy="If no run is active, remove it.",
            fatal=False,
        )
    return Check("lock", True, "not held")


def check_plan_freshness(state, cadence_days, moment=None, state_module=None):
    stale, description = state_module.is_stale(state, cadence_days, moment=moment)
    if stale:
        return Check(
            "schedule",
            False,
            description,
            remedy="The monthly timer may not be installed or running; "
            "see `cloudsync install-timer`.",
            fatal=False,
        )
    return Check("schedule", True, description)


def run_checks(config, paths, state, state_module, env=None, runner=None, moment=None,
               cadence_days=35):
    """Every preflight check, in order. Returns (checks, binary_or_None)."""
    binary, binary_check = check_rclone_binary(env=env)
    checks: list[Check] = [binary_check]

    if binary is None:
        return checks, None

    checks.append(check_version(binary, runner=runner))
    checks.append(check_flags(binary, runner=runner))
    checks.append(check_config_path(config))
    if os.path.isfile(config.rclone_config):
        checks.append(check_remotes(config, binary, runner=runner))
        checks.append(check_backup_remotes_are_crypt(config, binary, runner=runner))
    checks.append(check_state_dir(paths, state_module))
    checks.append(check_lock(paths, state_module))
    checks.append(check_plan_freshness(state, cadence_days, moment=moment,
                                       state_module=state_module))
    return checks, binary


def failed(checks):
    return [check for check in checks if not check.ok and check.fatal]


def warned(checks):
    return [check for check in checks if not check.ok and not check.fatal]


def format_checks(checks) -> list[str]:
    lines = []
    for check in checks:
        if check.ok:
            marker = "ok  "
        elif check.fatal:
            marker = "FAIL"
        else:
            marker = "warn"
        line = f"{marker}  {check.name:<22} {check.detail}"
        lines.append(line)
        if not check.ok and check.remedy:
            lines.append(f"      -> {check.remedy}")
    return lines


def load_and_check(config_path, state_module, env=None, runner=None, moment=None):
    """Convenience wrapper used by the CLI: load the config, then run every check."""
    config = config_module.load(config_path)
    paths = state_module.Paths(config.state_dir)
    state = state_module.load_state(paths) if os.path.isfile(paths.state_file) else {}
    checks, binary = run_checks(
        config, paths, state, state_module, env=env, runner=runner, moment=moment
    )
    return config, paths, checks, binary


def git_tree_of(path) -> Optional[str]:
    """Exposed for the CLI's own reporting and for tests."""
    return inside_git_work_tree(path)
