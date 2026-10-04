"""cloudsync — configurable directory sync and encrypted backup, with deletion guardrails.

Standard library only, Python 3.9 floor (see ruff.toml). The package is run as
`python3 -m cloudsync`, or imported by the test suite.

The modules, and the one thing each is responsible for:

| Module | Responsibility |
|---|---|
| `config` | reads and validates `cloudsync.ini`; the refusals decidable from the file alone |
| `rclone` | locating rclone, reading its version, building argv; pure except `run` |
| `guards` | the refusal rules evaluated against a plan; a pure function of `Evidence` |
| `state` | last-good manifests, saved plans, run records, and the lock |
| `plan` | the read-only half: dry run, then read what it would do |
| `doctor` | environment preflight |
| `report` | human lines and JSON for the same facts |
| `cli` | argument parsing, dispatch, and the exit-code contract |

The dependency direction is one way: `cli` → `plan`/`doctor` → `guards`/`rclone`/`state` →
`config`. Nothing in `guards` or `rclone` imports a module that performs I/O, which is what keeps
the guardrails testable without a fake remote.
"""

__version__ = "0.1.0"
