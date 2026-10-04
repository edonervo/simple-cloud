"""A fake rclone: canned responses, no process, no network, no remote.

Deliberately named `fake_rclone.py` rather than `test_fake_rclone.py`: `unittest discover`'s
default pattern is `test*.py`, so this file is importable as `test.fake_rclone` but is not
collected as a test module of its own.

`FakeRclone` routes on the rclone subcommand found in the argv, so a test states the situation
("the source holds these files, the dry run would delete these") rather than scripting calls in
order. Every argv it sees is recorded, which is how tests assert on what the tool *would* have
run — including the invariant that no test ever reaches the real binary.
"""

import json
from types import SimpleNamespace

# The subcommands the tool can issue. Used to find the command in an argv that may carry flags
# and a binary path before it.
KNOWN_COMMANDS = {
    "sync", "copy", "check", "cryptcheck", "lsjson", "about", "lsd", "size",
    "listremotes", "version", "config", "help",
}


def result(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def entries(*paths, is_dir=False, size=10, modified="2026-10-04T19:00:00.000000000Z"):
    return [
        {"Path": path, "IsDir": is_dir, "Size": size, "ModTime": modified}
        for path in paths
    ]


def lsjson_payload(items):
    return result(0, json.dumps(items))


def dry_run_payload(deletions=(), transfers=0, errors=0, fatal=False, exit_code=0,
                    extra_events=()):
    """NDJSON as `--use-json-log` emits it: one object per line, deletion per object.

    Mirrors the schema verified against rclone v1.75.1 — `{level, msg, object, objectType,
    skipped, size}` — so a test exercises the same shape the real binary produces.
    """
    lines = []
    for name in deletions:
        lines.append(json.dumps({
            "level": "notice",
            "msg": f"{name}: Deleted",
            "object": name,
            "objectType": "file",
            "skipped": "move into backup dir",
            "size": 10,
        }))
    for event in extra_events:
        lines.append(json.dumps(event))
    if fatal:
        lines.append(json.dumps({"level": "error", "msg": "fatal", "fatalError": True}))
    lines.append(json.dumps({
        "level": "info",
        "msg": "Transferred: 0 B / 10 B",
        "stats": {
            "errors": errors,
            "transfers": transfers,
            "deletes": len(deletions),
            "bytes": transfers * 10,
        },
    }))
    return result(exit_code, "\n".join(lines) + "\n")


class FakeRclone:
    """A callable that stands in for `cloudsync.rclone.run`."""

    def __init__(
        self,
        source=(),
        destination=(),
        deletions=(),
        transfers=0,
        errors=0,
        fatal=False,
        exit_code=0,
        version="rclone v1.75.1\n",
        remotes="edo-remote:\n",
        source_missing=False,
        destination_missing=False,
        extra_events=(),
        help_flags=None,
    ):
        self.source = list(source)
        self.destination = list(destination)
        self.deletions = list(deletions)
        self.transfers = transfers
        self.errors = errors
        self.fatal = fatal
        self.exit_code = exit_code
        self.version_text = version
        self.remotes = remotes
        self.source_missing = source_missing
        self.destination_missing = destination_missing
        self.extra_events = list(extra_events)
        self.help_flags = help_flags
        self.calls = []

    # -- routing --------------------------------------------------------------------------

    def command_of(self, argv):
        for token in argv[1:]:
            if token in KNOWN_COMMANDS:
                return token
        return None

    def argument_after(self, argv, command):
        try:
            return argv[argv.index(command) + 1]
        except (ValueError, IndexError):
            return None

    def __call__(self, argv):
        self.calls.append(list(argv))
        command = self.command_of(argv)

        if command == "version":
            return result(0, self.version_text)
        if command == "listremotes":
            return result(0, self.remotes)
        if command == "lsjson":
            path = self.argument_after(argv, "lsjson")
            return self._listing(path)
        if command in ("sync", "copy"):
            return dry_run_payload(
                self.deletions, self.transfers, self.errors, self.fatal, self.exit_code,
                self.extra_events,
            )
        if command == "help":
            text = self.help_flags if self.help_flags is not None else ""
            return result(0, text)
        if command == "check" or command == "cryptcheck":
            return result(0, "")
        if command == "about":
            return result(0, json.dumps({"total": 1000, "used": 500, "free": 500}))
        if command == "ldd" or command == "lsd":
            return result(0, "")
        return result(0, "")

    def _listing(self, path):
        if not path:
            return lsjson_payload([])
        if ":" in path:
            if self.destination_missing:
                return result(3, "", "directory not found")
            return lsjson_payload(self.destination)
        if self.source_missing:
            return result(3, "", "directory not found")
        return lsjson_payload(self.source)

    # -- assertions helpers ---------------------------------------------------------------

    def argvs_for(self, command):
        return [argv for argv in self.calls if self.command_of(argv) == command]

    def wrote_anything(self):
        """True if any call could have modified a remote.

        Every `sync`/`copy` this fake answers is a dry run as far as the tool is concerned; this
        reports the calls that were *not* dry runs, which would be real writes.
        """
        writes = []
        for argv in self.calls:
            if self.command_of(argv) in ("sync", "copy") and "--dry-run" not in argv:
                writes.append(argv)
        return writes
