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
| CI beyond lint and tests | Two workflows, both checks only: `ci.yml` (ruff plus the `unittest` suite, on Python 3.9 and 3.12) and `secret-check.yml` (the guard). Nothing is built, packaged, released, or deployed, and no workflow runs a real sync or sends a real message. `test/test_cron.py` is a scratch script, not a test (`architecture.md` §8). |
| Dependency lock file | Dependencies are declared and pinned in `requirements.txt` (§5 of `architecture.md`), but there is no lock file covering transitive dependencies. |
| Secrets management | Twilio credentials and both notification addresses come from environment variables (via `python-dotenv`, which is optional); the Gmail OAuth client and cached token come from two files next to the script. No secret manager, vault, or encryption at rest is used — protection is gitignore plus the pre-commit guard (`secrets.md`). |

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
| Gmail configuration | `credentials.json` (OAuth client) and `token.json` (cached token), both next to the script and both gitignored; sender and recipient from `GMAIL_SENDER` / `GMAIL_RECIPIENT` | **TBD** — the addresses used to be literals in the script and were moved out in Phase 4. |

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
| **Credentials live next to the code.** | `credentials.json` and `token.json` sit in `messaging/gmail/` in plaintext — there is no vault and no encryption at rest. A refresh token grants mailbox access until revoked. They are resolved relative to the *script* (fixed in Phase 4, so the working directory no longer decides which file is read), and they are gitignored, but nothing else protects them. | Partially — gitignore rules exist, and the pre-commit guard blocks them from being committed; see `secrets.md`. |
| **Personal data is already in the history.** | The sender and recipient addresses were literals in `send_email.py` until Phase 4. They are gone from the current tree and from `.env.example`, but every commit that carried them still does, and history is what a clone receives. Removing them going forward does not remove them from the past. | Partially — the values are configuration now (`secrets.md` §1). Whether the history needs rewriting is an owner decision (§8). |
| **Undeclared dependencies.** | Nothing recorded which packages are required, so a fresh machine could not reproduce the environment from the repository alone. | Yes — `requirements.txt` declares and pins them (`architecture.md` §5). |
| **No tests.** | Refactoring any of this has no safety net. | Partially — a standard-library `unittest` suite now covers the guard and the sync logic (`architecture.md` §8). No integration test exists. |

## 8. Required inputs from owner

| Input | Needed for | Status |
|---|---|---|
| rclone remote definition for `edo-remote` | Confirming the provider and that `sync` (not `copy`) is intended | **TBD** — not in this repository; lives in the owner's rclone config. |
| Confirmation that mirror-deletes are intended | Assessing risk §7 | **TBD** |
| Whether both `sync_gdrive.sh` and `sync_gdrive.py` are still wanted | Removing duplicated logic | **TBD** |
| How the messaging scripts are meant to be triggered | Alerting design | **TBD** |
| Whether `cron` scheduling is documented anywhere | `test/test_cron.py`'s purpose | **TBD** |
| Recipient/sender email addresses | Whether they were live values, and whether the history should be rewritten if so | **TBD** — they were committed literals until Phase 4 and are now `GMAIL_SENDER` / `GMAIL_RECIPIENT`. They are out of the current tree but still in the history (`secrets.md` §1). |
| Whether the `.env` on the owner's machine holds those two addresses | So the refactored Gmail script keeps working | **TBD** — the script now fails with a named list if they are unset. |
