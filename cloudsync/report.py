"""Rendering: human lines, or one JSON object.

Every command produces a payload and a set of human lines for the same facts, and `--json` picks
which one is printed. Keeping both derivations next to each other is what stops them drifting —
the JSON is the machine contract, the lines are what a person reads in a terminal, and a test can
assert on either.
"""

import json
import sys

# Stable keys, so a consumer of --json can rely on them across releases.
STATUS_OK = "ok"
STATUS_HELD = "held"
STATUS_LOCKED = "locked"
STATUS_USAGE = "usage-error"


def emit(payload, human_lines, as_json=False, stream=None):
    """Print either the JSON payload or the human lines. Never both."""
    stream = sys.stdout if stream is None else stream
    if as_json:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
    else:
        for line in human_lines:
            stream.write(line + "\n")
    return payload


def table(rows, headers=None) -> list[str]:
    """A plain aligned table. Empty input yields no lines rather than a bare header."""
    if not rows:
        return []
    widths = [max(len(str(row[index])) for row in rows) for index in range(len(rows[0]))]
    if headers:
        widths = [
            max(width, len(str(header))) for width, header in zip(widths, headers)
        ]
    lines = []
    if headers:
        lines.append("  ".join(str(header).ljust(width) for header, width in zip(headers, widths)))
        lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append(
            "  ".join(str(cell).ljust(width) for cell, width in zip(row, widths)).rstrip()
        )
    return lines


def plural(count, singular, plural_form=None):
    if count == 1:
        return f"1 {singular}"
    label = plural_form or singular + "s"
    return f"{count} {label}"


def bytes_human(value):
    """A byte count a person can read. Binary units, because that is what file managers show."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "?"
    if abs(amount) < 1024.0:
        return f"{int(amount)} B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        amount /= 1024.0
        if abs(amount) < 1024.0 or unit == "TiB":
            return f"{amount:.1f} {unit}"
    return f"{amount:.1f} TiB"


def finding_lines(findings) -> list[str]:
    return [f"  - {finding}" for finding in findings]
