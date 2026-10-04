#!/usr/bin/env python3
"""secret_guard — keep credentials out of this repository.

Standard library only, on purpose: the pre-commit hook has to run in a fresh clone with no
virtualenv and no ``pip install``. No network access is used.

The guard never prints a value it finds. Every finding carries a redacted fingerprint
(a short prefix for provider-shaped matches, and length + SHA-256 for everything), because a
scanner that echoes a secret into a terminal or a CI log *is* a leak.

Scopes (repeatable on the command line):

    staged      the git index — what the next commit would record
    tree        files tracked at HEAD — what a clone receives
    history     every blob object, graded by reachability
    boundaries  the ignore rules and the seed files, not their contents
    all         every scope above

Usage:

    python3 scripts/secret_guard.py check
    python3 scripts/secret_guard.py check --scope staged
    python3 scripts/secret_guard.py check --scope tree --scope history --json
    python3 scripts/secret_guard.py install-hook
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

ALLOWLIST_FILE = ".secrets-allowlist"
ENV_FILE = ".env"
ENV_EXAMPLE = ".env.example"
INLINE_PRAGMA = "pragma: allowlist secret"

# A blob larger than this is not scanned for content (still enumerated in history).
MAX_BLOB_BYTES = 5_000_000

SEVERITY_BLOCK = "block"
SEVERITY_WARN = "warn"


class Finding:
    __slots__ = ("severity", "location", "kind", "label", "preview")

    def __init__(self, severity, location, kind, label, preview):
        self.severity = severity
        self.location = location  # "path:line" or "blob <sha> [unreachable]"
        self.kind = kind          # "pattern" | "configured-secret" | "entropy" | "boundary"
        self.label = label        # pattern name, env var name, or rule name
        self.preview = preview    # redacted, never a raw value

    def render(self):
        mark = "x" if self.severity == SEVERITY_BLOCK else "!"
        return f"{mark} {self.location}  [{self.kind}:{self.label}] {self.preview}"

    def as_dict(self):
        return {
            "severity": self.severity,
            "location": self.location,
            "kind": self.kind,
            "label": self.label,
            "preview": self.preview,
        }


# --------------------------------------------------------------------------------------
# Detection layer 2 — provider-shaped patterns.
# Each rule is anchored to a prefix a provider actually issues, which is what keeps the
# false-positive rate low.
# --------------------------------------------------------------------------------------

PATTERNS = [
    ("twilio-account-sid", re.compile(r"\bAC[0-9a-fA-F]{32}\b")),
    ("twilio-api-key-sid", re.compile(r"\bSK[0-9a-fA-F]{32}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("google-oauth-client-secret", re.compile(r"\bGOCSPX-[0-9A-Za-z_\-]{20,}\b")),
    ("google-service-account", re.compile(r'"type"\s*:\s*"service_account"')),
    ("private-key-block", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[0-9A-Za-z]{36}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[0-9A-Za-z_]{22,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("stripe-secret-key", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{20,}\b")),
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[0-9A-Za-z_\-]{20,}\b")),
    ("sendgrid-key", re.compile(r"\bSG\.[0-9A-Za-z_\-]{22}\.[0-9A-Za-z_\-]{43}\b")),
    ("npm-token", re.compile(r"\bnpm_[0-9A-Za-z]{36}\b")),
    ("json-web-token", re.compile(
        r"\beyJ[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{10,}\b"
    )),
    ("password-in-url", re.compile(r"://[^/\s:@]{1,64}:[^/\s:@]{3,64}@")),
]

# --------------------------------------------------------------------------------------
# Detection layer 3 — high-entropy assignments. The only layer that can misfire, so it is
# deliberately bounded: a credential-ish name, >=16 chars, >=8 distinct, entropy >=3.5
# bits/char, and a digit or a base64 marker.
# --------------------------------------------------------------------------------------

ASSIGNMENT = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(?P<q>['\"])?(?P<val>[^\s'\"]{16,})(?P=q)"
)
CREDENTIAL_NAME = re.compile(r"key|token|secret|passw|pwd|credential|bearer", re.IGNORECASE)
_PLACEHOLDER_START = (
    "your", "example", "changeme", "change_me", "placeholder", "redacted",
    "dummy", "sample", "xxxx", "todo", "insert", "replace", "here",
)


def _entropy(value):
    counts = Counter(value)
    n = len(value)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _looks_placeholder(value):
    if any(ch in value for ch in "<>$%{}()[]."):
        return True
    low = value.lower()
    if low in {"none", "null", "true", "false"}:
        return True
    return low.startswith(_PLACEHOLDER_START)


def _is_high_entropy(value):
    if len(value) < 16 or len(set(value)) < 8:
        return False
    if _looks_placeholder(value):
        return False
    if not (any(ch.isdigit() for ch in value) or any(ch in "+/=" for ch in value)):
        return False
    return _entropy(value) >= 3.5


# --------------------------------------------------------------------------------------
# Redaction. Nothing here ever returns a full secret.
# --------------------------------------------------------------------------------------

def _fingerprint(value):
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:8]


def _redact(value, *, show_prefix):
    if show_prefix:
        head = value[:4]
        return f"{head}… <len={len(value)} sha256:{_fingerprint(value)}>"
    return f"<redacted len={len(value)} sha256:{_fingerprint(value)}>"


# --------------------------------------------------------------------------------------
# Allowlist / inline pragma.
# --------------------------------------------------------------------------------------

def load_allowlist(root):
    entries = []
    path = os.path.join(root, ALLOWLIST_FILE)
    if not os.path.isfile(path):
        return entries
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            entries.append(line)
    return entries


def _allowed(value, entries):
    return any(entry and (entry == value or entry in value) for entry in entries)


# --------------------------------------------------------------------------------------
# Content scanning.
# --------------------------------------------------------------------------------------

def scan_text(text, location_prefix, allowlist, configured):
    """Return findings for a block of text.

    ``location_prefix`` is a path (for tree/staged) or a label such as "blob 1a2b…".
    ``configured`` is a list of (label, value) pairs from the owner's own configuration.
    """
    findings = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if INLINE_PRAGMA in line:
            continue
        loc = f"{location_prefix}:{lineno}"

        # Layer 1 — exact values from configuration. No value is ever shown.
        for label, value in configured:
            if value and value in line and not _allowed(value, allowlist):
                findings.append(Finding(
                    SEVERITY_BLOCK, loc, "configured-secret", label,
                    _redact(value, show_prefix=False),
                ))

        # Layer 2 — provider-shaped patterns.
        for name, rx in PATTERNS:
            for match in rx.finditer(line):
                value = match.group(0)
                if _allowed(value, allowlist):
                    continue
                findings.append(Finding(
                    SEVERITY_BLOCK, loc, "pattern", name, _redact(value, show_prefix=True)
                ))

        # Layer 3 — high-entropy assignments.
        for match in ASSIGNMENT.finditer(line):
            name = match.group("name")
            value = match.group("val")
            if not CREDENTIAL_NAME.search(name):
                continue
            if not _is_high_entropy(value):
                continue
            if _allowed(value, allowlist):
                continue
            findings.append(Finding(
                SEVERITY_BLOCK, loc, "entropy", name, _redact(value, show_prefix=False)
            ))
    return findings


def _looks_binary(content):
    return b"\x00" in content[:8192]


# --------------------------------------------------------------------------------------
# Configuration values (layer 1 source).
# --------------------------------------------------------------------------------------

def load_configured_values(root):
    """Read the owner's local .env, if present, to learn the real values to hunt for.

    Values are held in memory only for comparison; they are never printed.
    """
    configured = []
    path = os.path.join(root, ENV_FILE)
    if not os.path.isfile(path):
        return configured
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                name = name.strip()
                value = value.strip().strip("'\"")
                if len(value) >= 8:
                    configured.append((name, value))
    except OSError:
        pass
    return configured


# --------------------------------------------------------------------------------------
# Git helpers.
# --------------------------------------------------------------------------------------

def _git(root, *args, check=True):
    return subprocess.run(
        ["git", "-C", root, *args],
        check=check, capture_output=True,
    )


def _is_git_repo(root):
    try:
        return _git(root, "rev-parse", "--is-inside-work-tree", check=False).returncode == 0
    except OSError:
        return False


# --------------------------------------------------------------------------------------
# Scope: staged — read the index, not the working tree.
# --------------------------------------------------------------------------------------

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def scan_staged(root, allowlist, configured):
    proc = _git(root, "diff", "--cached", "--unified=0", "--no-color", "--diff-filter=ACMR")
    text = proc.stdout.decode("utf-8", "replace")
    findings = []
    path = None
    lineno = 0
    for line in text.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path == "/dev/null":
                path = None
            elif path.startswith("b/"):
                path = path[2:]
            continue
        if line.startswith("@@"):
            m = _HUNK.match(line)
            if m:
                lineno = int(m.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if path is not None:
                for f in scan_text(line[1:], path, allowlist, configured):
                    f.location = f"{path}:{lineno}"
                    findings.append(f)
            lineno += 1
    return findings


# --------------------------------------------------------------------------------------
# Scope: tree — files tracked at HEAD.
# --------------------------------------------------------------------------------------

def scan_tree(root, allowlist, configured):
    proc = _git(root, "ls-files", "-z")
    names = [n for n in proc.stdout.decode("utf-8", "replace").split("\0") if n]
    findings = []
    for name in names:
        full = os.path.join(root, name)
        try:
            with open(full, "rb") as fh:
                content = fh.read(MAX_BLOB_BYTES)
        except OSError:
            continue
        if _looks_binary(content):
            continue
        findings.extend(scan_text(content.decode("utf-8", "replace"), name, allowlist, configured))
    return findings


# --------------------------------------------------------------------------------------
# Scope: history — every blob, graded by reachability.
# --------------------------------------------------------------------------------------

def _blob_map(root):
    """Return (all_blob_shas, reachable_shas, sha_to_path)."""
    reachable = set()
    path_of = {}
    proc = _git(root, "rev-list", "--objects", "--all")
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split(" ", 1)
        if not parts or not parts[0]:
            continue
        sha = parts[0]
        reachable.add(sha)
        if len(parts) == 2:
            path_of.setdefault(sha, parts[1])

    check = _git(
        root, "cat-file", "--batch-all-objects",
        "--batch-check=%(objectname) %(objecttype)",
    )
    blobs = []
    for line in check.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "blob":
            blobs.append(parts[0])
    return blobs, reachable, path_of


def _read_blobs(root, shas):
    if not shas:
        return {}
    proc = subprocess.run(
        ["git", "-C", root, "cat-file", "--batch"],
        input="\n".join(shas).encode(), check=False, capture_output=True,
    )
    data = proc.stdout
    out = {}
    pos = 0
    while pos < len(data):
        nl = data.find(b"\n", pos)
        if nl < 0:
            break
        header = data[pos:nl].decode("utf-8", "replace").split()
        if len(header) < 3:
            break
        sha, _typ, size = header[0], header[1], int(header[2])
        start = nl + 1
        out[sha] = data[start:start + size]
        pos = start + size + 1
    return out


def scan_history(root, allowlist, configured):
    blobs, reachable, path_of = _blob_map(root)
    findings = []
    for sha, content in _read_blobs(root, blobs).items():
        if len(content) > MAX_BLOB_BYTES or _looks_binary(content):
            continue
        label = f"blob {sha[:10]}"
        path = path_of.get(sha)
        if path:
            label = f"{label} ({path})"
        if sha not in reachable:
            label = f"{label} [unreachable]"
        for f in scan_text(content.decode("utf-8", "replace"), label, allowlist, configured):
            if sha not in reachable:
                f.severity = SEVERITY_WARN
            findings.append(f)
    return findings


# --------------------------------------------------------------------------------------
# Scope: boundaries — the rules, not the content.
# --------------------------------------------------------------------------------------

REQUIRED_IGNORE_PATTERNS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "credentials.json",
    "token.json",
    "*.sqlite3",
    # cloudsync's state directory (run records, saved plans, rclone logs, the lock). It is
    # generated, it contains absolute paths and argv, and an rclone error string inside it can
    # carry a credential fragment — so it is held to the same rule as a credential file.
    "state/",
]
REQUIRED_IGNORE_HINTS = [
    ("service account json", re.compile(r"(service[-_]?account|serviceAccount)", re.IGNORECASE)),
    ("notebook outputs", re.compile(r"ipynb_checkpoints", re.IGNORECASE)),
]


def scan_boundaries(root):
    findings = []
    gitignore = os.path.join(root, ".gitignore")
    text = ""
    if os.path.isfile(gitignore):
        with open(gitignore, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    lines = {ln.strip() for ln in text.splitlines()}

    for pattern in REQUIRED_IGNORE_PATTERNS:
        if pattern not in lines:
            findings.append(Finding(
                SEVERITY_BLOCK, ".gitignore", "boundary", f"missing-rule:{pattern}",
                f"add a line: {pattern}",
            ))
    for label, rx in REQUIRED_IGNORE_HINTS:
        if not rx.search(text):
            findings.append(Finding(
                SEVERITY_BLOCK, ".gitignore", "boundary", f"missing-rule:{label}",
                "no ignore rule matches this category",
            ))

    # A rule does not protect a file git already tracks.
    tracked = _git(root, "ls-files", "-z").stdout.decode("utf-8", "replace").split("\0")
    for name in tracked:
        if not name:
            continue
        if name in {ENV_EXAMPLE}:
            continue
        proc = _git(root, "check-ignore", "--no-index", "-q", "--", name, check=False)
        if proc.returncode == 0:
            findings.append(Finding(
                SEVERITY_BLOCK, name, "boundary", "tracked-and-ignored",
                "file is tracked by git *and* matches an ignore rule — remove it from the "
                "index with: git rm --cached " + name,
            ))

    # Seed files must NOT be excluded, or a clone loses its bootstrap.
    if os.path.isfile(os.path.join(root, ENV_EXAMPLE)):
        proc = _git(root, "check-ignore", "--no-index", "-q", "--", ENV_EXAMPLE, check=False)
        if proc.returncode == 0:
            findings.append(Finding(
                SEVERITY_BLOCK, ENV_EXAMPLE, "boundary", "seed-excluded",
                "the published template is ignored; add a negation line: !.env.example",
            ))
        findings.extend(_check_env_example_blank(root))
    else:
        findings.append(Finding(
            SEVERITY_BLOCK, ENV_EXAMPLE, "boundary", "seed-missing",
            "no .env.example — a clone has no list of the variables it needs",
        ))
    return findings


def _check_env_example_blank(root):
    findings = []
    path = os.path.join(root, ENV_EXAMPLE)
    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            _name, _, value = line.partition("=")
            if value.strip():
                findings.append(Finding(
                    SEVERITY_BLOCK, f"{ENV_EXAMPLE}:{lineno}", "boundary", "seed-has-value",
                    "the published template must hold names only — every value blank",
                ))
    return findings


# --------------------------------------------------------------------------------------
# Command line.
# --------------------------------------------------------------------------------------

SCOPES = ("staged", "tree", "history", "boundaries")


def _resolve_scopes(requested):
    resolved = []
    for name in requested:
        if name == "all":
            for s in SCOPES:
                if s not in resolved:
                    resolved.append(s)
        elif name in SCOPES:
            if name not in resolved:
                resolved.append(name)
        else:
            raise ValueError(
                f"unknown scope: {name!r} (choose from {', '.join(SCOPES)}, all)"
            )
    return resolved


def run_check(root, scopes, allowlist, configured):
    findings = []
    for scope in scopes:
        try:
            if scope == "staged":
                findings.extend(scan_staged(root, allowlist, configured))
            elif scope == "tree":
                findings.extend(scan_tree(root, allowlist, configured))
            elif scope == "history":
                findings.extend(scan_history(root, allowlist, configured))
            elif scope == "boundaries":
                findings.extend(scan_boundaries(root))
        except subprocess.CalledProcessError as exc:  # pragma: no cover - defensive
            detail = exc.stderr.decode(errors="replace")
            raise RuntimeError(f"git failed during '{scope}' scan: {detail}") from exc
    return findings


def cmd_check(args):
    root = os.path.abspath(args.root)
    if not _is_git_repo(root):
        print(f"secret-guard: {root} is not a git repository", file=sys.stderr)
        return EXIT_ERROR
    try:
        scopes = _resolve_scopes(args.scope)
    except ValueError as exc:
        print(f"secret-guard: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if not scopes:
        print("secret-guard: refusing to scan with no scopes selected", file=sys.stderr)
        return EXIT_ERROR

    allowlist = load_allowlist(root)
    configured = load_configured_values(root)
    findings = run_check(root, scopes, allowlist, configured)

    blocking = [f for f in findings if f.severity == SEVERITY_BLOCK]
    warnings = [f for f in findings if f.severity == SEVERITY_WARN]

    if args.json:
        print(json.dumps({
            "scopes": scopes,
            "findings": [f.as_dict() for f in findings],
            "blocking": len(blocking),
            "warnings": len(warnings),
        }, indent=2))
    else:
        for f in findings:
            print(f.render())
        print()
        print(f"secret-guard: scopes [{', '.join(scopes)}] — "
              f"{len(blocking)} blocking, {len(warnings)} warning(s)")
        if blocking:
            print()
            print("How to fix:")
            print("  - If the value is real: remove it from the file and rotate it.")
            print("  - If it is a false positive: add `# pragma: allowlist secret` to the line,")
            print(f"    or add the exact literal to {ALLOWLIST_FILE}.")
            print("  - Never commit with `--no-verify` to get past this.")

    if blocking:
        return EXIT_FINDINGS
    if args.strict and warnings:
        return EXIT_FINDINGS
    return EXIT_CLEAN


def cmd_install_hook(args):
    root = os.path.abspath(args.root)
    _git(root, "config", "core.hooksPath", ".githooks")
    hook = os.path.join(root, ".githooks", "pre-commit")
    if os.path.isfile(hook):
        os.chmod(hook, 0o755)
    print("secret-guard: core.hooksPath set to .githooks")
    print("secret-guard: the pre-commit hook is now active for this clone")
    return EXIT_CLEAN


def build_parser():
    parser = argparse.ArgumentParser(
        prog="secret_guard",
        description="Detect credentials in the staged index, the tree, and the history.",
    )
    parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    sub = parser.add_subparsers(dest="command")

    check = sub.add_parser("check", help="scan for secrets")
    check.add_argument(
        "--scope", action="append", default=[],
        help="staged | tree | history | boundaries | all (repeatable; default: tree boundaries)",
    )
    check.add_argument("--json", action="store_true", help="machine-readable output")
    check.add_argument("--strict", action="store_true", help="treat warnings as failures")
    check.set_defaults(func=cmd_check)

    install = sub.add_parser("install-hook", help="activate the committed git hook")
    install.set_defaults(func=cmd_install_hook)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        args.command = "check"
        args.scope = ["tree", "boundaries"]
        args.json = False
        args.strict = False
        args.func = cmd_check
    if args.command == "check" and not args.scope:
        args.scope = ["tree", "boundaries"]
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
