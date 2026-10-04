# Architecture

> Companion to `project_scope.md` (what/why) and `secrets.md` (credentials).
> This file covers structure, data flow, configuration, and the verified/pending boundary.

## 1. Component overview

Three independent jobs share one repository. Nothing imports anything else — each script is a
standalone entry point.

```
        ~/Documents                                ┌───────────────────┐
        ~/Books            ┌──────────────┐        │  edo-remote:      │
        ~/Projects_toClean │ sync_gdrive  │ rclone │  Documents        │
   ┌──────────────────────┤  .py  |  .sh ├───────▶│  Books            │
   │  local filesystem    └──────┬───────┘  sync  │  Projects_toClean │
   └─────────────────────────────┘   (mirror)     └───────────────────┘
                                  │
                            sync_gdrive.log

   ┌──────────────┐   OAuth (credentials.json → token.json)   ┌───────────┐
   │ send_email.py├───────────────────────────────────────────▶│ Gmail API │
   └──────────────┘                                            └───────────┘

   ┌──────────────────┐   env vars (TWILIO_*)                 ┌────────────┐
   │ send_test_sms.py ├───────────────────────────────────────▶│ Twilio API │
   └──────────────────┘                                        └────────────┘
```

**The sync jobs and the messaging jobs never interact.** There is no orchestrator; the
`cron` schedule (if any) is held outside the repository.

## 2. Directory layout

```
simple-cloud/
├── docs/                          # this documentation set
├── messaging/
│   ├── gmail/
│   │   └── send_email.py          # Gmail API send — OAuth installed-app flow
│   └── twilio/
│       └── send_test_sms.py       # Twilio SMS send — env-var credentials
├── sync_gdrive.py                 # sync, Python (newer of the two)
├── sync_gdrive.sh                 # sync, Bash (original)
├── test/
│   └── test_cron.py               # scratch script — NOT a test (§8)
├── .gitignore
├── README.md                      # two lines
└── LICENSE
```

⚠ **There is no test framework, no dependency manifest, and no CI.** No `requirements.txt`,
no `pyproject.toml`, no `.github/`. Each is a gap, not a decision that is documented anywhere.

## 3. Data flow

| Path | Direction | Mechanism |
|---|---|---|
| Local directories → `edo-remote:` | one-way mirror | `rclone sync --interactive <local> <remote> -v`, run once per directory pair |
| Tail of every sync action → `sync_gdrive.log` | append | `log_message()` opens the file in append mode on every call |
| Gmail script → recipient | one message | `gmail.send` scope; a single `EmailMessage` with fixed content |
| Twilio script → phone | one message | `Client.messages.create(...)` with fixed body |

**The log path is the working directory, not the script's directory** (`sync_gdrive.py:22`,
`sync_gdrive.sh:20`), so `cron` must set a working directory or the log lands somewhere
unexpected. The same applies to `credentials.json` / `token.json` in the Gmail script.

## 4. Configuration — environment variables and file credentials

Values are listed by **name and purpose only**; no value appears in this document.

### Environment variables (Twilio path)

| Variable | Purpose | Read at |
|---|---|---|
| `TWILIO_ACCOUNT_SID` | Twilio account identifier | `messaging/twilio/send_test_sms.py:7` |
| `TWILIO_AUTH_TOKEN` | Twilio API authentication secret | `messaging/twilio/send_test_sms.py:8` |
| `TWILIO_PHONE_NUMBER` | Sending number (E.164) | `messaging/twilio/send_test_sms.py:12` |
| `SPAIN_PHONE_NUMBER` | Destination number | `messaging/twilio/send_test_sms.py:13` |

These are loaded with `python-dotenv`'s `load_dotenv()`, which reads a `.env` file **in the
working directory**. `.env.example` is the tracked, blank template (see `secrets.md` §5).

### File credentials (Gmail path)

| File | Purpose | Protection |
|---|---|---|
| `credentials.json` | OAuth client for the installed-app flow | gitignored |
| `token.json` | Cached access/refresh token, written by the script after first consent | gitignored |

Both are read from the **working directory** (`send_email.py:21,28,33`). Deleting `token.json`
forces a fresh consent flow, which is the documented remedy after changing `SCOPES`
(`send_email.py:12`).

## 5. External services

| Service | Used for | Configured by |
|---|---|---|
| rclone + the `edo-remote` remote | Cloud mirror | rclone's own config — **not in this repository** |
| Gmail API (`gmail.send`) | Email notification | `credentials.json` / `token.json` |
| Twilio REST API | SMS notification | the four `TWILIO_*` / `SPAIN_PHONE_NUMBER` variables |

The `edo-remote` **provider is not verifiable from this repository.** The sync scripts' log
lines say "Google Drive", and the remote name is `edo-remote:`; the actual backend is defined
in the owner's rclone config, which lives outside the repo.

### Python dependencies

Declared in `requirements.txt`. Versions are **not pinned** — see the note at the top of that
file; pin them with `pip freeze` on the machine that runs the scripts.

| Package | Imported by |
|---|---|
| `google-api-python-client` | `send_email.py` (`googleapiclient.*`) |
| `google-auth-oauthlib` | `send_email.py` (`google_auth_oauthlib.flow`) |
| `google-auth` / `google-auth-httplib2` | `send_email.py` (`google.auth.transport.requests`) |
| `twilio` | `send_test_sms.py` (imported lazily, inside `main()`) |
| `python-dotenv` | `send_test_sms.py` (optional — its absence is not an error) |

`sync_gdrive.py` uses the standard library only. The Bash script additionally needs `rclone`,
and its updater path needs `curl` and `unzip`.

## 6. The sync model — `rclone sync` is a mirror

`rclone sync source dest` makes `dest` identical to `source`. It is **not** `copy`: files that
exist at the destination but not at the source are **deleted at the destination**.

```
   Before                                   After  rclone sync ~/Books edo-remote:Books
   ~/Books            edo-remote:Books       ~/Books            edo-remote:Books
   ├── a.pdf          ├── a.pdf              ├── a.pdf          ├── a.pdf
   └── b.pdf          ├── b.pdf              └── b.pdf
                      └── old.pdf                               (old.pdf DELETED)
```

The scripts pass `--interactive`, which is rclone's interactive mode and gives a chance to
intervene, but the default posture is destructive. Losing the local directory — or pointing a
pair at the wrong one — propagates to the cloud. `project_scope.md` §7 records this as the top
risk. A `--dry-run` first run is the usual mitigation and is not built in.

## 7. Scripts reference

| Script | Runtime | Notes |
|---|---|---|
| `sync_gdrive.py` | Python 3, stdlib only | Validates rclone is installed, validates the directory pairs, then syncs each pair. Has known defects — see §10. |
| `sync_gdrive.sh` | Bash | Same logic plus an rclone self-update step that downloads and installs the current release into `/usr/local/bin` using `sudo`. The update path is interactive. |
| `messaging/gmail/send_email.py` | Python 3 + Google libs | Runs its send on import-guard (`__main__`). Content, recipient, and sender are hardcoded. |
| `messaging/twilio/send_test_sms.py` | Python 3 + `twilio`, `python-dotenv` | Sends **at import time** — there is no `__main__` guard and no validation that the environment variables are set. Importing it as a module would send a message. |
| `test/test_cron.py` | Python 3, stdlib only | Prints the output of `ls -l`. Not a test; contains no assertions and is not discovered by any runner. |

## 8. Testing

A `unittest` suite (standard library — no test dependency) covers the secret guard, the sync
pairing and invocation logic, and the Twilio script's configuration handling. `test/test_cron.py`
remains a scratch script and contains no tests.

```bash
make test     # python3 -m unittest discover -s test -t .
make lint     # ruff, if installed
```

The suite is a **characterization** net: it pins the behaviour the Phase 4 refactors had to
preserve (one `rclone sync` per pair, the argument order, the exit status) rather than testing
these scripts against a live remote. There is still no integration test, because there is no
safe way to run a mirroring sync in CI.

## 9. Build and run

There is no build step. Install the declared dependencies, then invoke a script:

```bash
python3 -m pip install -r requirements.txt

# sync (Python) — writes sync_gdrive.log next to the script
python3 sync_gdrive.py

# sync (Bash — also offers an rclone self-update)
./sync_gdrive.sh

# notifications (credentials must be present first)
python3 messaging/gmail/send_email.py
python3 messaging/twilio/send_test_sms.py
```

`cron` entries are not stored in the repository, so the intended schedule is **TBD**
(`project_scope.md` §8). Because `sync_gdrive.py` now resolves its log path relative to the
script and returns a non-zero status on failure, a `cron` job can act on the result without
depending on the working directory.

## 10. Verified vs. pending

**Verified** (read directly from the code or from git history):

- The directory pairs, the `rclone sync` invocation, and the log behaviour in both sync scripts.
- The four Twilio variable names and the two Gmail credential files.
- The dependency list in §5 is derived from the actual `import` statements.
- The git history contains **nine files ever added**, all listed in §2, and **no secret value**
  (`secrets.md` §1, confirmed by the guard's history scan).

**Fixed in Phase 4** — defects that were present in the audited tree:

- `sync_output` was assigned *inside* the `try`; if `subprocess.run` raised before returning,
  the `except` body read an unbound name and raised `NameError`, masking the original failure.
  Removed; the exception object is used instead.
- The `except` was bare, so it also swallowed `KeyboardInterrupt`. Now it catches
  `subprocess.CalledProcessError` and `OSError` only.
- On failure the script still logged and printed "Sync completed successfully." and exited
  `0`, so `cron` could not tell a failed sync from a good one. It now reports failures and
  returns a non-zero status.
- `messaging/twilio/send_test_sms.py` sent an SMS **on import** and built a Twilio client from
  unvalidated variables. Sending now happens only under `__main__`, after the four required
  variables are checked.

**Still open:**

- `parse_sync_output()` is an empty stub with a `TODO` — left as the owner's note, not removed.
- No version pins in `requirements.txt` (§5).
- No `ruff` configuration file, so `make lint` uses ruff's defaults.

**Pending / not verifiable from here:**

- The provider behind `edo-remote`, and whether mirror-deletes are intended.
- Whether the Gmail addresses in `send_email.py:56-57` are placeholders or live (PII if live).
- The intended `cron` schedule and whether notification scripts are meant to be wired to it.
- Whether `sync_gdrive.sh` or `sync_gdrive.py` is the one actually used — the history shows the
  Bash script first (`274d28b`) and the Python script later (`204f127`), which suggests the
  Python version succeeded it, but the Bash version is still present and was not removed.
