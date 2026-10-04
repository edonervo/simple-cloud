"""cloudsync.ini — the data model, and every refusal rule.

This module is the **first guardrail layer**. Anything dangerous is rejected here, before rclone
is invoked at all, because a rule enforced at load time cannot be forgotten at a call site.

Problems are collected rather than raised one at a time: a config with three mistakes should
report three, not send the owner round the loop three times.

Two rules exist because rclone's behaviour is counter-intuitive. Both were verified against
rclone v1.75.1 rather than taken from the documentation:

* ``max_delete`` must be >= 1 whenever ``trash_root`` is set. With ``--backup-dir``, a deletion
  that becomes a *move* into the trash still counts as a deletion against the budget, so
  ``--max-delete 0`` makes the first pending deletion fatal — **during the dry run as well**.
  A planner that aborts on its first deletion hands the safety gate a truncated plan, which is
  strictly worse than having no gate.
* ``trash_root`` must not live inside the job's ``remote``. A nested trash is re-synced into
  itself on the next run: the trash appears as destination-only files, so rclone moves the trash
  into the trash. The sibling requirement is what makes the trash inert.
"""

import configparser
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Optional

# Job modes. `mirror` can delete; `copy` structurally cannot.
MIRROR = "mirror"
COPY = "copy"
MODES = (MIRROR, COPY)

# Whether a deletion needs a human. `always` is the default: the delete budget bounds how much
# damage a bug can do, but it authorises nothing.
APPROVE_ALWAYS = "always"
APPROVE_OVER_BUDGET = "over_budget"
APPROVE_POLICIES = (APPROVE_ALWAYS, APPROVE_OVER_BUDGET)

# What to do when the gate refuses a plan.
ON_WARNING_HOLD = "hold"
ON_WARNING_FAIL = "fail"
ON_WARNING_POLICIES = (ON_WARNING_HOLD, ON_WARNING_FAIL)

DEFAULT_STATE_DIR = "state"
DEFAULT_ON_WARNING = ON_WARNING_HOLD
DEFAULT_MAX_DELETE = 25
DEFAULT_MAX_SHRINK_PERCENT = 10
DEFAULT_MIN_FILES = 1

TRASH_SEGMENT = "_cloudsync-trash"


class ConfigError(Exception):
    """One or more refusals. ``problems`` holds every one of them, in file order."""

    def __init__(self, problems: Sequence[str]):
        self.problems: list[str] = list(problems)
        joined = "\n  - ".join(self.problems)
        message = self.problems[0] if len(self.problems) == 1 else (
            f"{len(self.problems)} problems:\n  - {joined}"
        )
        super().__init__(message)


@dataclass(frozen=True)
class Remote:
    """A ``<name>:<path>`` reference, already split and checked."""

    name: str
    path: str

    def __str__(self) -> str:
        return f"{self.name}:{self.path}"


@dataclass(frozen=True)
class Pair:
    """One sync job: a local directory mirrored to (or copied to) a remote path."""

    name: str
    section: str
    local: str
    remote: Remote
    mode: str
    max_delete: int
    trash_root: Optional[Remote]
    approve_deletes: str
    max_shrink_percent: int
    min_files: int
    protect: tuple[str, ...]
    exclude: tuple[str, ...]

    @property
    def can_delete(self) -> bool:
        """False for `copy` jobs, which rclone cannot make destructive."""
        return self.mode == MIRROR

    @property
    def any_delete_needs_approval(self) -> bool:
        return self.approve_deletes == APPROVE_ALWAYS


@dataclass(frozen=True)
class Backup:
    """One encrypted-backup job. Always `copy`: a backup that can delete is not a backup."""

    name: str
    section: str
    local: str
    remote: Remote

    @property
    def mode(self) -> str:
        return COPY

    @property
    def can_delete(self) -> bool:
        return False


@dataclass(frozen=True)
class Config:
    path: str
    state_dir: str
    on_warning: str
    rclone_config: str
    pairs: tuple[Pair, ...]
    backups: tuple[Backup, ...]

    @property
    def remote_names(self) -> tuple[str, ...]:
        """The allowlist: the only remote names any job may address."""
        names = [p.remote.name for p in self.pairs]
        names += [b.remote.name for b in self.backups]
        names += [p.trash_root.name for p in self.pairs if p.trash_root]
        return tuple(sorted(set(names)))

    def all_locals(self) -> tuple[str, ...]:
        return tuple(p.local for p in self.pairs) + tuple(b.local for b in self.backups)


# --------------------------------------------------------------------------------------------
# Parsing helpers. Each returns a value and appends any problem to `problems`.
# --------------------------------------------------------------------------------------------


def _split_remote(value: str) -> Optional[Remote]:
    """`name:path` -> Remote. Returns None if there is no colon or the path is empty."""
    if ":" not in value:
        return None
    name, path = value.split(":", 1)
    if not name or not path.strip("/"):
        return None
    return Remote(name=name, path=path.strip("/"))


def _check_remote(value, section, field, problems, allowlist=None):
    remote = _split_remote(value)
    if remote is None:
        problems.append(
            f"[{section}] {field} must be '<remote>:<path>' with a non-empty path, got {value!r} "
            "(a job may never address a remote root)"
        )
        return None
    if ".." in remote.path.split("/"):
        problems.append(f"[{section}] {field} must not contain '..': {value}")
        return None
    if allowlist is not None and remote.name not in allowlist:
        declared = ", ".join(sorted(allowlist)) or "none"
        problems.append(
            f"[{section}] {field} names remote {remote.name!r}, which is not declared anywhere "
            f"in this file (declared: {declared})"
        )
        return None
    return remote


def _check_local(value, section, problems):
    """Validate a local source directory. Returns the normalised path, or None."""
    if not value:
        problems.append(f"[{section}] local is required")
        return None

    expanded = os.path.expanduser(value)
    if not os.path.isabs(expanded):
        problems.append(
            f"[{section}] local must be absolute after ~ expansion, got {value!r}"
        )
        return None
    if ".." in expanded.split(os.sep):
        problems.append(f"[{section}] local must not contain '..': {value}")
        return None

    normalised = os.path.normpath(expanded)
    home = os.path.normpath(os.path.expanduser("~"))

    if normalised == os.sep:
        problems.append(f"[{section}] local may not be the filesystem root")
        return None
    if normalised == home:
        problems.append(f"[{section}] local may not be the home directory itself")
        return None
    if os.path.islink(normalised):
        problems.append(
            f"[{section}] local is a symlink ({normalised}); point at the real directory, "
            "because a symlink can be repointed without this config changing"
        )
        return None
    if not os.path.exists(normalised):
        problems.append(f"[{section}] local does not exist: {normalised}")
        return None
    if not os.path.isdir(normalised):
        problems.append(f"[{section}] local is not a directory: {normalised}")
        return None
    return normalised


def _is_within(inner, outer):
    """True if `inner` is `outer` or lies beneath it. Pure string work, after realpath."""
    if inner == outer:
        return True
    return inner.startswith(outer.rstrip(os.sep) + os.sep)


def _check_overlaps(locals_to_sections, problems):
    """No local may contain another: overlapping sources make delete budgets meaningless."""
    resolved = []
    for section, path in locals_to_sections:
        resolved.append((section, os.path.realpath(path)))

    for i, (section_a, path_a) in enumerate(resolved):
        for section_b, path_b in resolved[i + 1:]:
            if _is_within(path_a, path_b) or _is_within(path_b, path_a):
                problems.append(
                    f"[{section_a}] local {path_a} overlaps [{section_b}] local {path_b}; "
                    "jobs must not nest"
                )


def _check_trash_outside_destination(remote, trash, section, problems):
    """A trash inside the destination is deleted by the next run, into itself."""
    if remote.name != trash.name:
        return
    if _is_within(trash.path, remote.path):
        problems.append(
            f"[{section}] trash_root {trash} is inside the destination {remote}; a nested trash "
            f"is re-synced into itself on the next run. Use a sibling path, "
            f"e.g. {remote.name}:{TRASH_SEGMENT}/<name>"
        )


def _get_int(parser, section, field, default, problems, minimum=None):
    raw = parser.get(section, field, fallback=None)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        problems.append(f"[{section}] {field} must be an integer, got {raw!r}")
        return default
    if minimum is not None and value < minimum:
        problems.append(f"[{section}] {field} must be >= {minimum}, got {value}")
        return default
    return value


def _get_list(parser, section, field):
    raw = parser.get(section, field, fallback="") or ""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _get_choice(parser, section, field, default, allowed, problems):
    raw = (parser.get(section, field, fallback=default) or default).strip()
    if raw not in allowed:
        choices = " | ".join(allowed)
        problems.append(f"[{section}] {field} must be one of {choices}, got {raw!r}")
        return default
    return raw


# --------------------------------------------------------------------------------------------
# The loader
# --------------------------------------------------------------------------------------------


def load(path):
    """Parse and fully validate a config file.

    Raises ConfigError listing every problem found. Never returns a partially-validated Config:
    a caller may rely on `Config` being safe to act on.
    """
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        raise ConfigError([f"config file not found: {path}"])

    # interpolation=None: a literal `%` in a path would otherwise raise from the first get(),
    # long after the read, and a filesystem path has no business being interpolated anyway.
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, encoding="utf-8") as handle:
            parser.read_file(handle)
    except (configparser.Error, OSError) as error:
        raise ConfigError([f"{path} could not be read as INI: {error}"]) from error

    problems: list[str] = []
    base_dir = os.path.dirname(path)

    # --- [general] ---------------------------------------------------------------------
    if not parser.has_section("general"):
        problems.append("missing the [general] section")

    raw_state = parser.get("general", "state_dir", fallback=DEFAULT_STATE_DIR) or DEFAULT_STATE_DIR
    state_dir = os.path.normpath(
        raw_state if os.path.isabs(os.path.expanduser(raw_state))
        else os.path.join(base_dir, os.path.expanduser(raw_state))
    )
    on_warning = _get_choice(
        parser, "general", "on_warning", DEFAULT_ON_WARNING, ON_WARNING_POLICIES, problems
    )

    # Required, deliberately: if it were optional, an omission would silently fall back to
    # rclone's own default, which on this machine is ~/.config/rclone/rclone.conf -- inside a
    # symlinked git work tree that pushes to GitHub.
    raw_rclone_config = parser.get("general", "rclone_config", fallback="") or ""
    if not raw_rclone_config.strip():
        problems.append(
            "[general] rclone_config is required; rclone's own default is inside a symlinked "
            "git work tree on this machine and must not be used"
        )
        rclone_config = ""
    else:
        rclone_config = os.path.normpath(os.path.expanduser(raw_rclone_config.strip()))

    # The set of remotes some job already writes to. This is deliberately built from `remote`
    # fields only, and used to check `trash_root` only.
    #
    # It cannot validate a `remote` field, because it is derived from them -- whatever name is
    # typed there joins this set and passes. That check belongs to preflight, which is the only
    # place that can read rclone's own config and learn which remotes actually exist. Pretending
    # to make it here would be a guard that cannot fail.
    #
    # For `trash_root` it is meaningful: the trash must be a remote this file already treats as
    # a destination, so a typo cannot silently create a second, unmanaged trash location.
    declared = set()
    for section in parser.sections():
        if section.startswith("pair:") or section.startswith("backup:"):
            declared.update(
                remote.name
                for remote in [_split_remote(parser.get(section, "remote", fallback="") or "")]
                if remote
            )

    # --- [pair:*] ----------------------------------------------------------------------
    pairs: list[Pair] = []
    for section in parser.sections():
        if not section.startswith("pair:"):
            continue
        name = section.split(":", 1)[1].strip()
        if not name:
            problems.append(f"[{section}] has no name after 'pair:'")
            continue

        local = _check_local(parser.get(section, "local", fallback=""), section, problems)
        # No allowlist for `remote`: see the note on `declared` above. Shape only, here.
        remote = _check_remote(
            parser.get(section, "remote", fallback=""), section, "remote", problems
        )
        mode = _get_choice(parser, section, "mode", MIRROR, MODES, problems)
        # The default depends on the mode: a `copy` job that omits the key must not inherit the
        # mirror default and then be refused for carrying a budget it never asked for.
        max_delete = _get_int(
            parser, section, "max_delete", 0 if mode == COPY else DEFAULT_MAX_DELETE, problems,
            minimum=0,
        )
        approve_deletes = _get_choice(
            parser, section, "approve_deletes", APPROVE_ALWAYS, APPROVE_POLICIES, problems
        )

        raw_trash = (parser.get(section, "trash_root", fallback="") or "").strip()
        trash = None
        if raw_trash:
            trash = _check_remote(raw_trash, section, "trash_root", problems, declared)

        if mode == COPY and max_delete:
            problems.append(
                f"[{section}] mode = copy cannot carry max_delete = {max_delete}; copy never "
                "deletes, so the setting would be silently meaningless"
            )
        if mode == MIRROR and trash and max_delete < 1:
            problems.append(
                f"[{section}] max_delete must be >= 1 when trash_root is set, got {max_delete}. "
                "With --backup-dir a deletion-to-trash still counts as a deletion, so 0 makes the "
                "first pending deletion fatal -- the dry run fails too, truncating the plan the "
                "gate inspects"
            )
        if remote and trash:
            _check_trash_outside_destination(remote, trash, section, problems)

        if local is None or remote is None:
            continue

        pairs.append(
            Pair(
                name=name,
                section=section,
                local=local,
                remote=remote,
                mode=mode,
                max_delete=max_delete,
                trash_root=trash,
                approve_deletes=approve_deletes,
                max_shrink_percent=_get_int(
                    parser, section, "max_shrink_percent", DEFAULT_MAX_SHRINK_PERCENT, problems,
                    minimum=0,
                ),
                min_files=_get_int(
                    parser, section, "min_files", DEFAULT_MIN_FILES, problems, minimum=0
                ),
                protect=_get_list(parser, section, "protect"),
                exclude=_get_list(parser, section, "exclude"),
            )
        )

    # --- [backup:*] --------------------------------------------------------------------
    backups: list[Backup] = []
    for section in parser.sections():
        if not section.startswith("backup:"):
            continue
        name = section.split(":", 1)[1].strip()
        if not name:
            problems.append(f"[{section}] has no name after 'backup:'")
            continue
        local = _check_local(parser.get(section, "local", fallback=""), section, problems)
        remote = _check_remote(
            parser.get(section, "remote", fallback=""), section, "remote", problems
        )
        if local is None or remote is None:
            continue
        backups.append(
            Backup(name=name, section=section, local=local, remote=remote)
        )

    if not pairs and not backups:
        problems.append("no [pair:*] or [backup:*] sections; nothing to do")

    # Sync pairs only. Two mirrors onto nested sources make each other's delete budget
    # meaningless, which is the risk this rule addresses. A backup job may legitimately share a
    # source with a sync pair -- that is the whole shape of the monthly workflow -- and being
    # `copy` it cannot delete anything, so it carries none of that risk.
    _check_overlaps([(p.section, p.local) for p in pairs], problems)

    if problems:
        raise ConfigError(problems)

    return Config(
        path=path,
        state_dir=state_dir,
        on_warning=on_warning,
        rclone_config=rclone_config,
        pairs=tuple(pairs),
        backups=tuple(backups),
    )
