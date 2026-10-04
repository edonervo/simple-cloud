"""Every refusal rule, evaluated against a plan.

`evaluate` is a pure function of `Evidence`: no I/O, no clock, no rclone. It takes the facts a
dry run produced and returns findings. That keeps the guardrails testable in isolation — a table
of inputs and expected findings, with no fake binary involved — which matters because these
rules are the thing that stops data being lost.

A `Finding` is a reason to **hold**: nothing is written, the plan is kept, and a human decides.
Guards never authorise anything. Passing every guard means the plan is eligible to be approved,
not that it is safe.

Rule IDs (R*, L*, W*) match docs/cloudsync.md and the plan, so a finding can be traced to the
paragraph that explains why it exists.
"""

import fnmatch
from dataclasses import dataclass
from typing import Optional

from .config import APPROVE_ALWAYS, COPY, MIRROR


@dataclass(frozen=True)
class Finding:
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.message}"


@dataclass(frozen=True)
class Evidence:
    """Everything the guardrails need to know, and nothing they do not.

    Built by `plan.py` from the dry run and the two listings. Deliberately flat: a guard that
    needs a new fact gets a new field, which makes the dependency visible in the type.
    """

    job: str
    local: str
    remote: str
    mode: str
    # R6 / R1 / R12 / R13 evidence: the paths the run would delete, enumerated per object rather
    # than read off an aggregate counter.
    deletes: tuple[str, ...]
    transfers: int
    errors: int
    fatal: bool
    # W3 / W4 / R15: the source as listed.
    source_files: tuple[str, ...]
    # R16: how many objects the destination holds now, from the last good run or a listing.
    destination_count: int
    # W3: the source's file count at the last known-good run, if there is one.
    last_source_count: Optional[int]
    # The pair's own thresholds, copied in so this module need not import the config layer.
    max_delete: int
    max_shrink_percent: int
    min_files: int
    protect: tuple[str, ...]
    approve_deletes: str


def matches_any(path, patterns):
    """fnmatch against the whole path.

    `*` crosses directory separators here, so `*.pdf` matches `a/b/c.pdf` — which is what someone
    writing `protect = *.pdf` means. `docs/*.pdf` still matches only inside `docs/`.

    A bare string is accepted as a single pattern. Without this, iterating it yields individual
    characters, and a `*` in the pattern would match every path — a protect list that protects
    everything, or protects nothing, depending on which way it is read.
    """
    if isinstance(patterns, str):
        patterns = (patterns,)
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def case_collisions(paths):
    """Paths that differ only by case.

    Google Drive is case-insensitive: `Report.pdf` and `report.pdf` in one tree collapse into a
    single object at the destination, and would collide in the trash as well. The loser is not
    predictable, so the run holds rather than picking one.
    """
    grouped = {}
    for path in paths:
        grouped.setdefault(path.casefold(), []).append(path)
    return sorted(
        tuple(sorted(group)) for group in grouped.values() if len(set(group)) > 1
    )


def evaluate(evidence):
    """Return every finding for this plan. An empty list means "eligible for approval"."""
    findings = []

    # W6 -- an aborted dry run is never treated as a plan. Its stats describe a partial run, so
    # it is not gated against and never replayed. Checked first, because every other number here
    # is untrustworthy once it fires.
    if evidence.fatal:
        findings.append(
            Finding(
                "W6",
                "the dry run ended in a fatal error, so its plan is partial and cannot be "
                "trusted; nothing will be applied from it",
            )
        )
        return findings

    # Counted once: every rule below compares these against a threshold, and reading them from a
    # local keeps each message about the rule rather than about the arithmetic.
    source_count = len(evidence.source_files)
    delete_count = len(evidence.deletes)

    # W1 -- any rclone error holds the job.
    if evidence.errors > 0:
        findings.append(Finding("W1", f"the dry run reported {evidence.errors} error(s)"))

    # W4 -- an empty source holds. This is the shape of an unmounted or mis-pointed directory,
    # where rclone would read the entire destination as disposable.
    if source_count < evidence.min_files:
        findings.append(
            Finding(
                "W4",
                f"the source holds {source_count} file(s), below the configured "
                f"min_files of {evidence.min_files}",
            )
        )

    # W3 -- a shrinking source holds.
    if evidence.last_source_count:
        floor = evidence.last_source_count * (1 - evidence.max_shrink_percent / 100.0)
        if source_count < floor:
            findings.append(
                Finding(
                    "W3",
                    f"the source fell from {evidence.last_source_count} to {source_count} file(s), "
                    f"more than the allowed {evidence.max_shrink_percent}%",
                )
            )

    # R15 -- case collisions hold.
    for group in case_collisions(evidence.source_files):
        findings.append(
            Finding(
                "R15",
                "case-only name collision in the source: "
                f"{', '.join(group)}. Google Drive cannot hold both, so one would silently "
                "replace the other",
            )
        )

    if evidence.mode == MIRROR:
        # R6 -- a configured glob is undeletable.
        for deleted in evidence.deletes:
            if matches_any(deleted, evidence.protect):
                findings.append(
                    Finding(
                        "R6",
                        f"the plan would delete {deleted}, which matches a protected pattern",
                    )
                )

        # R1 -- the finite budget. rclone enforces this too, but checking here means the refusal
        # arrives as a clear hold rather than as a fatal mid-run.
        if delete_count > evidence.max_delete:
            findings.append(
                Finding(
                    "R1",
                    f"the plan would delete {delete_count} object(s), above the max_delete "
                    f"budget of {evidence.max_delete}",
                )
            )

        # R16 -- a mass delete holds even under budget. This is the signature of the source being
        # unmounted, emptied or mis-pointed, where the whole destination reads as disposable.
        if evidence.destination_count:
            allowed = evidence.destination_count * evidence.max_shrink_percent / 100.0
            if delete_count > allowed:
                findings.append(
                    Finding(
                        "R16",
                        f"the plan would delete {delete_count} of "
                        f"{evidence.destination_count} destination object(s), more than the "
                        f"allowed {evidence.max_shrink_percent}%",
                    )
                )

        # R12 -- any deletion needs approval, not merely an over-budget one. The budget bounds
        # how much a *bug* can do; it authorises nothing. Placed last so the specific reasons
        # above are read first.
        if evidence.deletes and evidence.approve_deletes == APPROVE_ALWAYS:
            findings.append(
                Finding(
                    "R12",
                    f"{delete_count} deletion(s) require approval (approve_deletes = always). "
                    "Review with `cloudsync plan --json`, then apply with "
                    "`cloudsync run --approve <plan>`",
                )
            )

    elif evidence.mode == COPY:
        # R5 -- a copy job is structurally incapable of deleting. If a delete appears anyway,
        # the argv is not what this code thought it built, so refuse rather than reason about it.
        if evidence.deletes:
            findings.append(
                Finding(
                    "R5",
                    f"a copy job reported {delete_count} deletion(s); copy cannot delete, so the "
                    "argv is not what it should be. Refusing.",
                )
            )

    return findings


def summarise(findings):
    """One line per finding, for humans. Empty when there is nothing to report."""
    return "\n".join(f"  - {finding}" for finding in findings)


def any_of(finding_list, rules):
    """True if any finding carries one of the given rule ids. Used by tests and the reporter."""
    wanted = set(rules)
    return any(finding.rule in wanted for finding in finding_list)


def evidence_for(pair, **overrides):
    """Build Evidence for a pair with sane defaults, so tests state only what they vary.

    The defaults describe a comfortably healthy plan: one source file, a destination large enough
    that a single deletion is nowhere near the mass-delete threshold, no errors. A test that
    varies one field therefore sees the finding for *that* rule rather than a pile-up.
    """
    base = dict(
        job=pair.name,
        local=pair.local,
        remote=str(pair.remote),
        mode=pair.mode,
        deletes=(),
        transfers=0,
        errors=0,
        fatal=False,
        source_files=("a.txt",),
        destination_count=100,
        last_source_count=None,
        max_delete=pair.max_delete,
        max_shrink_percent=pair.max_shrink_percent,
        min_files=pair.min_files,
        protect=pair.protect,
        approve_deletes=pair.approve_deletes,
    )
    base.update(overrides)
    return Evidence(**base)
