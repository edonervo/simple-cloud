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

   ┌──────────────┐   env vars (GMAIL_SENDER, GMAIL_RECIPIENT),        ┌───────────┐
   │ send_email.py│   OAuth via credentials.json → token.json     ─────▶│ Gmail API │
   └──────────────┘   (both files resolved next to the script)         └───────────┘

   ┌──────────────────┐   env vars (TWILIO_*)                 ┌────────────┐
   │ send_test_sms.py ├───────────────────────────────────────▶│ Twilio API │
   └──────────────────┘                                        └────────────┘
```

**The sync jobs and the messaging jobs never interact.** There is no orchestrator; the
`cron` schedule (if any) is held outside the repository.

## 2. Directory layout

```
simple-cloud/
├── .githooks/pre-commit           # the secret guard, wired as a pre-commit hook
├── .github/workflows/
│   ├── ci.yml                     # ruff + the unittest suite, on 3.9 and 3.12
│   └── secret-check.yml           # the guard over staged/tree/history/boundaries
├── docs/                          # this documentation set
├── messaging/
│   ├── gmail/
│   │   └── send_email.py          # Gmail API send — env vars + OAuth installed-app flow
│   └── twilio/
│       └── send_test_sms.py       # Twilio SMS send — env-var credentials
├── scripts/secret_guard.py        # stdlib-only secret scanner (see secrets.md)
├── sync_gdrive.py                 # sync, Python (newer of the two)
├── sync_gdrive.sh                 # sync, Bash (original)
├── test/
│   ├── test_cron.py               # scratch script — NOT a test (§8)
│   ├── test_messaging.py          # the two notification scripts
│   ├── test_secret_guard.py       # the guard's detection layers and end-to-end block
│   └── test_sync_gdrive.py        # sync pairing and invocation
├── .env.example                   # tracked, blank template for the credential names
├── .gitignore
├── .secrets-allowlist             # reviewable escape hatch for the guard
├── Makefile                       # install-hooks, secrets-check, secrets-all, test, lint
├── requirements.txt               # declared and pinned runtime dependencies (§5)
├── ruff.toml                      # lint configuration and the Python floor
├── README.md                      # two lines
└── LICENSE
```

**Wiring, not a package.** Nothing is installed, imported across directories, or built. Every
script is an entry point run from the checkout, which is why `messaging/` and `scripts/` carry
no `__init__.py` and why the tests load the messaging modules by file path.

## 3. Data flow

| Path | Direction | Mechanism |
|---|---|---|
| Local directories → `edo-remote:` | one-way mirror | `rclone sync --interactive <local> <remote> -v`, run once per directory pair |
| Tail of every sync action → `sync_gdrive.log` | append | `log_message()` opens the file in append mode on every call |
| Gmail script → recipient | one message | `gmail.send` scope; a single `EmailMessage` whose sender and recipient come from the environment, with a default subject and body |
| Twilio script → phone | one message | `Client.messages.create(...)` with fixed body |

**The log path differs between the two sync implementations.** `sync_gdrive.py` resolves it next
to the script (`sync_gdrive.py:22`), so the log lands in the checkout wherever `cron` starts the
job. `sync_gdrive.sh` still writes the relative path `sync_gdrive.log` (`sync_gdrive.sh:20`), so
its log lands in the **working directory**, which `cron` must therefore set. The Gmail script's
`credentials.json` / `token.json` are resolved next to the script in both cases.

## 4. Configuration — environment variables and file credentials

Values are listed by **name and purpose only**; no value appears in this document.

### Environment variables

Both notification scripts read their configuration from the environment, and both fail with a
named list of what is missing rather than a provider-side error.

| Variable | Purpose | Required by | Read at |
|---|---|---|---|
| `TWILIO_ACCOUNT_SID` | Twilio account identifier | `send_test_sms.py` | `send_test_sms.py:44` |
| `TWILIO_AUTH_TOKEN` | Twilio API authentication secret | `send_test_sms.py` | `send_test_sms.py:44` |
| `TWILIO_PHONE_NUMBER` | Sending number (E.164) | `send_test_sms.py` | `send_test_sms.py:46` |
| `SPAIN_PHONE_NUMBER` | Destination number | `send_test_sms.py` | `send_test_sms.py:47` |
| `GMAIL_SENDER` | Address the message is sent from | `send_email.py` | `send_email.py:104` |
| `GMAIL_RECIPIENT` | Address the message is sent to | `send_email.py` | `send_email.py:104` |

The names each script requires are declared in its `REQUIRED_VARIABLES` tuple — the single
source of truth the test suite checks `.env.example` against, so the tracked template and the
code cannot drift apart silently.

These are loaded with `python-dotenv`'s `load_dotenv()`, which reads a `.env` file **in the
working directory**; `python-dotenv` is optional (`load_env_file()` returns quietly if it is
absent, and the variables may simply be exported instead). `.env.example` is the tracked, blank
template (see `secrets.md` §5). The two Gmail values are addresses rather than credentials, but
they are personal data, which is why they are configuration and no longer literals in the
script (`secrets.md` §1).

### File credentials (Gmail path)

| File | Purpose | Protection |
|---|---|---|
| `credentials.json` | OAuth client for the installed-app flow | gitignored |
| `token.json` | Cached access/refresh token, written by the script after first consent | gitignored |

Both are resolved **next to the script** (`send_email.py:21-23`), so they are found whatever
directory the script is launched from. Deleting `token.json` forces a fresh consent flow, which
is the documented remedy after changing `SCOPES` (`send_email.py:16`). That consent flow is
interactive and opens a local server on port 8080, so the script is not usable unattended until
a `token.json` exists.

## 5. External services

| Service | Used for | Configured by |
|---|---|---|
| rclone + the `edo-remote` remote | Cloud mirror | rclone's own config — **not in this repository** |
| Gmail API (`gmail.send`) | Email notification | `credentials.json` / `token.json`, plus the `GMAIL_*` variables |
| Twilio REST API | SMS notification | the four `TWILIO_*` / `SPAIN_PHONE_NUMBER` variables |

The `edo-remote` **provider is not verifiable from this repository.** The sync scripts' log
lines say "Google Drive", and the remote name is `edo-remote:`; the actual backend is defined
in the owner's rclone config, which lives outside the repo.

### Python dependencies

Declared and **pinned** in `requirements.txt`, to the newest release of each package whose
metadata still declares support for Python 3.9 (the floor in `ruff.toml`). Pinning the newest
release overall would not install on that floor — the current `google-*` and `python-dotenv`
releases all require Python ≥ 3.10. The set was verified with `pip install --dry-run`, which
resolves it and its transitive tree; it has **not** been executed against the scripts.

| Package | Imported by |
|---|---|
| `google-api-python-client` | `send_email.py` (`googleapiclient.*`, imported inside `send_email()`) |
| `google-auth-oauthlib` | `send_email.py` (`google_auth_oauthlib.flow`, imported inside `authenticate()`) |
| `google-auth` / `google-auth-httplib2` | `send_email.py` (`google.auth.transport.requests`, imported inside `authenticate()`) |
| `twilio` | `send_test_sms.py` (imported lazily, inside `main()`) |
| `python-dotenv` | both notification scripts (optional — its absence is not an error) |

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
| `sync_gdrive.py` | Python 3, stdlib only | Validates rclone is installed, validates the directory pairs, then syncs each pair. The defects the audit found are fixed — see §10. |
| `sync_gdrive.sh` | Bash | Same logic plus an rclone self-update step that downloads and installs the current release into `/usr/local/bin` using `sudo`. The update path is interactive. Unchanged in Phase 4. |
| `messaging/gmail/send_email.py` | Python 3 + Google libs | Sends one message under `__main__`, after checking `GMAIL_SENDER` / `GMAIL_RECIPIENT` are set. The Google libraries are imported **inside** the functions that use them, so importing the module neither needs them nor sends anything. Credentials are resolved next to the script. |
| `messaging/twilio/send_test_sms.py` | Python 3 + `twilio`, `python-dotenv` | Sends under `__main__` only, after the four required variables are checked; `twilio` is imported inside `main()`. |
| `scripts/secret_guard.py` | Python 3, stdlib only | The secret scanner described in `secrets.md`. Refuses to run on an unknown `--scope`; never prints a value it finds. |
| `test/test_cron.py` | Python 3, stdlib only | Prints the output of `ls -l`. Not a test; contains no assertions and is not discovered by any runner. |

## 8. Testing

A `unittest` suite (standard library — no test dependency) covers the secret guard, the sync
pairing and invocation logic, and both notification scripts' configuration handling.
`test/test_cron.py` remains a scratch script and contains no tests.

```bash
make test     # python3 -m unittest discover -s test -t .
make lint     # ruff, if installed
```

The suite is a **characterization** net: it pins the behaviour the Phase 4 refactors had to
preserve (one `rclone sync` per pair, the argument order, the exit status) rather than testing
these scripts against a live remote. Neither notification script is tested against its real
API — the Gmail send is exercised through injected stub modules that capture the encoded
message, and the Twilio send is skipped when `twilio` is not installed. There is still no
integration test, because there is no safe way to run a mirroring sync in CI.

## 9. Build and run

There is no build step. Install the declared dependencies, then invoke a script. This section is
the summary; `usage.md` has the same commands with captured output, the examples worth copying,
and a troubleshooting table.

```bash
python3 -m pip install -r requirements.txt

# sync (Python) — writes sync_gdrive.log next to the script
python3 sync_gdrive.py

# sync (Bash — also offers an rclone self-update)
./sync_gdrive.sh

# notifications — both read .env (or exported variables) and name what is missing
cp .env.example .env      # then fill it in; the Gmail path also needs credentials.json
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
- The git history contains **no secret value** — verified by the guard's `history` scope
  (`secrets.md` §1). The tree held nine files when the audit began and holds 25 now; every one
  of them is listed in §2, and the growth is entirely the Phase 3–4 work.

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
- `messaging/gmail/send_email.py` imported the whole Google stack at module level, so it could
  not even be imported without it, and it read `credentials.json` / `token.json` from the
  **working directory** — the same `cron` hazard as the log file. The imports moved inside the
  functions that use them and the paths are now resolved next to the script.
- The same script hardcoded the sender and recipient addresses as literals at
  `send_email.py:55-56` — personal data committed to the repository. They are now the
  `GMAIL_SENDER` / `GMAIL_RECIPIENT` variables; see `secrets.md` §1 for what that does and does
  not undo.

**Also removed:** `parse_sync_output()` was an empty, unreferenced stub carrying a `TODO`.
Its intent — surfacing rclone's output — is partly served now by the failure path, which logs
rclone's `stderr`. Summarising a *successful* run is a feature, not a cleanup, and is not
built.

**Still open:** no lock file for transitive dependencies (§5), and no integration test that
runs a real mirroring sync — there is no safe way to do that in CI, so the suite stays a
characterization net over logic rather than an end-to-end test.

**Pending / not verifiable from here:**

- The provider behind `edo-remote`, and whether mirror-deletes are intended.
- Whether the two Gmail addresses are placeholders or live. They are no longer in the
  current tree, but they remain in the git history, and only the owner can say whether that
  history is public and whether a rewrite is warranted.
- The intended `cron` schedule and whether notification scripts are meant to be wired to it.
- Whether `sync_gdrive.sh` or `sync_gdrive.py` is the one actually used — the history shows the
  Bash script first (`274d28b`) and the Python script later (`204f127`), which suggests the
  Python version succeeded it, but the Bash version is still present and was not removed.
