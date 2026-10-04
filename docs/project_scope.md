# Project Scope — Personal Cloud Sync & Notification Scripts

> Status: **operational utility, lightly structured.** This page records what the code
> verifiably does; items that are not verifiable from the repository are marked **TBD** and
> collected in §8 and in the audit's Open Questions.

## 1. Purpose

Three small jobs, kept in one repository because they share a machine and an owner:

1. **Mirror local working directories to a cloud remote.** `~/Documents`, `~/Books`, and
   `~/Projects_toClean` are kept in step with a remote named `edo-remote`, so the same files
   exist on more than one device.
2. **Send an email** through the Gmail API — a notification channel, exercised by
   `messaging/gmail/send_email.py`.
3. **Send an SMS** through Twilio — a second notification channel, exercised by
   `messaging/twilio/send_test_sms.py`.

There is no service, no daemon, and no user interface. Each job is a script intended to be
run by hand or from `cron`.

## 2. Core design principle

> **Validate the pairing before touching the remote.**

Both sync scripts define two parallel lists — local directories and remote directories — and
refuse to proceed unless the lists are the same length and every local directory exists
(`sync_gdrive.py:43`, `sync_gdrive.sh:64`). This is the one deliberate safety property in the
code, and it matters because the operation being performed is not a copy.

**This principle is inferred from the code, not declared by the owner** — see §8.

## 3. In scope

| Area | Included |
|---|---|
| Cloud sync | One-way mirror of three local directories to three remote paths, via `rclone sync` |
| Sync tooling | Two interchangeable implementations — a Bash script (original) and a Python script (newer) |
| Notifications | One Gmail send path (OAuth installed-app flow) and one Twilio SMS path |
| Scheduling | Intended to be run by `cron` — a scratch script for this exists (`test/test_cron.py`), and `test_cron` is named as if it verifies cron behaviour |
| Logging | The sync scripts append a running log to `sync_gdrive.log` in the working directory |

## 4. Out of scope — explicit non-goals

These are observed exclusions, not omissions. Where the reason is not visible in the code it
is marked **TBD**.

| Not present | Note |
|---|---|
| Multi-user or shared operation | Paths are `$HOME`-relative and the credentials are one owner's. |
| Bidirectional sync | `rclone sync` is a one-way mirror by design (`architecture.md` §6). The scripts pass the local directory as the source and the remote as the destination. |
| Automated notification triggers | The messaging scripts send a fixed test message; nothing calls them on an event. Wiring alerting to sync success/failure is **TBD**. |
| CI beyond the secret scan | The only workflow is the secret-guard check. There is no build, test, or lint job. `test/test_cron.py` is a scratch script, not a test (`architecture.md` §8). |
| Dependency pinning | Dependencies are declared in `requirements.txt`, but no version is pinned (§5 of `architecture.md`). No lock file. |
| Secrets management | Twilio credentials come from environment variables (via `python-dotenv`); Gmail credentials come from two files in the working directory. Neither is documented outside the code. |

## 5. Locked decisions

A "locked decision" is a choice the owner made deliberately. The repository records the
*outcome* of such choices but not the *reasoning*, so this table lists only what the artifacts
show and leaves rationale as **TBD**.

| Decision | What the code shows | Rationale |
|---|---|---|
| Sync engine | `rclone` (external binary), remote name `edo-remote` | **TBD** — the remote is configured in rclone's own config, which is not in this repository. The scripts' log strings call it "Google Drive". |
| Sync implementations | Both `.sh` and `.py` exist and are near-identical | **TBD** — the two are maintained in parallel; `architecture.md` §10 records what the history shows about their order. |
| Notification providers | Gmail API and Twilio | **TBD** |
| Twilio configuration | Environment variables, loaded by `python-dotenv` | **TBD** |
| Gmail configuration | `credentials.json` (OAuth client) and `token.json` (cached token), both in the working directory | **TBD** — both are gitignored. |

## 6. Success criteria

Stated as the outcomes the code is built to produce:

1. `rclone sync` completes for all three directory pairs with no error reported as success.
2. A new rclone version is noticed and the owner is prompted before updating (Bash script only).
3. A Gmail message is sent from the configured account to the configured recipient, and its
   message id is printed.
4. A Twilio SMS is sent from the configured number to the configured destination, and its
   SID is printed.

Whether these are *currently* met cannot be verified without network credentials and the
owner's rclone config; see §8.

## 7. Risks

| Risk | Why it matters | Mitigated today? |
|---|---|---|
| **`rclone sync` deletes at the destination.** | `sync` makes the destination match the source. Any file on the remote that is not present locally is **removed from the remote**. A local directory that is empty by mistake therefore empties its cloud counterpart. | Partially — `--interactive` is passed, which gives the owner a chance to intervene. There is no dry-run default and no backup. |
| **A failure is reported as success.** | `sync_gdrive.py` used to catch every exception, log a failure, then unconditionally print "Sync completed successfully." and exit `0`, so a `cron` job could not tell a failed sync from a good one. | Yes — fixed: failures are reported and the exit status is non-zero (`architecture.md` §10). |
| **Credentials live next to the code.** | `credentials.json` and `token.json` are resolved relative to the *working directory*, not the script, so running from a different directory either fails or reads a different file. They are gitignored but not otherwise protected. | Partially — gitignore rules exist; see `secrets.md`. |
| **Undeclared dependencies.** | Nothing recorded which packages are required, so a fresh machine could not reproduce the environment from the repository alone. | Partially — `requirements.txt` now declares them, but versions are not pinned. |
| **No tests.** | Refactoring any of this has no safety net. | Partially — a standard-library `unittest` suite now covers the guard and the sync logic (`architecture.md` §8). No integration test exists. |

## 8. Required inputs from owner

| Input | Needed for | Status |
|---|---|---|
| rclone remote definition for `edo-remote` | Confirming the provider and that `sync` (not `copy`) is intended | **TBD** — not in this repository; lives in the owner's rclone config. |
| Confirmation that mirror-deletes are intended | Assessing risk §7 | **TBD** |
| Whether both `sync_gdrive.sh` and `sync_gdrive.py` are still wanted | Removing duplicated logic | **TBD** |
| How the messaging scripts are meant to be triggered | Alerting design | **TBD** |
| Whether `cron` scheduling is documented anywhere | `test/test_cron.py`'s purpose | **TBD** |
| Recipient/sender email addresses in `send_email.py:56-57` | Whether they are placeholders or live values | **TBD** — they are real-looking addresses; if they are live, they are PII committed to the repository (see `secrets.md` §1). |
