# Usage — Running the Scripts, the Guard and the Tests

> Task-oriented companion to `architecture.md` (what the pieces are) and `secrets.md` (how the
> guard works). This page answers "what do I actually type".
>
> Every command here is copy-pasteable, and the transcripts are captured from real runs — §10
> says exactly which ones, and which examples were **not** executed and why.

## 1. Requirements

| Requirement | Needed for | Notes |
|---|---|---|
| Python 3.9 or newer | everything | the floor declared in `ruff.toml` and exercised in CI on 3.9 and 3.12. |
| `git` | the guard, and any commit | the guard reads the index, the tree and the object store through `git`. |
| `rclone` | syncing only | an external binary. The script checks for it and exits `1` if it is missing. |
| `ruff` | `make lint` only | optional — the target reports that it is skipping, it does not fail. |
| The Google libraries / `twilio` | notifications only | see §5. |
| Network and credentials | notifications only | nothing else here touches the network. |

Only the two notification scripts need third-party packages. The sync script, the secret guard
and the whole test suite are **standard library only**, which is why the guard can run as a
pre-commit hook in a fresh clone with no virtualenv.

## 2. Setup, once per clone

```bash
git clone <repo-url> simple-cloud
cd simple-cloud

make install-hooks          # 1. arm the pre-commit secret guard — once per clone
cp .env.example .env        # 2. the tracked template holds names with blank values
$EDITOR .env                # 3. fill in what you actually need
make test                   # 4. 38 tests, no dependencies required
```

`make install-hooks` runs `git config core.hooksPath .githooks`, which is **local to this
clone** and is not carried by a push or a fetch. A fresh clone has no hook until this runs —
the CI workflow is the backstop for exactly that case (`secrets.md` §7).

`.env` is gitignored; `.env.example` is tracked and must keep **blank values** — the guard fails
the commit if a value appears in it. Fill in only the section you need:

| Variable | Read by | Needed for |
|---|---|---|
| `TWILIO_ACCOUNT_SID` | `send_test_sms.py` | SMS |
| `TWILIO_AUTH_TOKEN` | `send_test_sms.py` | SMS |
| `TWILIO_PHONE_NUMBER` | `send_test_sms.py` | SMS (sender, E.164) |
| `SPAIN_PHONE_NUMBER` | `send_test_sms.py` | SMS (destination) |
| `GMAIL_SENDER` | `send_email.py` | email (the `From` address) |
| `GMAIL_RECIPIENT` | `send_email.py` | email (the `To` address) |

`python-dotenv` is optional: if it is installed, `.env` is loaded automatically; if it is not,
export the variables in your shell instead. Either way both scripts list what is missing rather
than failing inside a provider library (§5).

The Gmail path needs one more thing that this repository does not contain and cannot generate:
`messaging/gmail/credentials.json`, the OAuth client for the installed-app flow. The code
requires only that the file exists at that path next to the script (`send_email.py:22`). The
first run then writes `token.json` beside it (§5).

## 3. Command reference

| Command | What it runs | Notes |
|---|---|---|
| `make help` | prints this list | |
| `make install-hooks` | `python3 scripts/secret_guard.py install-hook` | sets `core.hooksPath` |
| `make secrets-check` | `python3 scripts/secret_guard.py check` | tree + boundaries — the default scope set |
| `make secrets-all` | `python3 scripts/secret_guard.py check --scope all` | adds `staged` and `history`; the slow one |
| `make test` | `python3 -m unittest discover -s test -t . -v` | standard library; no install step |
| `make lint` | `ruff check .`, if `ruff` is on the PATH | skips out loud otherwise |
| `python3 sync_gdrive.py` | mirrors the three pairs | needs `rclone` and the three local directories |
| `./sync_gdrive.sh` | the same mirror, plus an rclone self-update | the updater needs `curl`, `unzip` and `sudo`, and is interactive |
| `python3 messaging/gmail/send_email.py` | sends one email | the only command besides the SMS that leaves the machine |
| `python3 messaging/twilio/send_test_sms.py` | sends one SMS | |

## 4. Syncing your directories

Three pairs, defined in `sync_gdrive.py:6-17`:

| Local | Remote |
|---|---|
| `~/Documents` | `edo-remote:Documents` |
| `~/Books` | `edo-remote:Books` |
| `~/Projects_toClean` | `edo-remote:Projects_toClean` |

```bash
cd ~/dev/simple-cloud
python3 sync_gdrive.py
```

The script checks that `rclone` is installed, that all three local directories exist and that
the two lists are still the same length, then runs one `rclone sync --interactive <local>
<remote> -v` per pair. **This is a mirror, not a copy** — anything on the remote that is not
present locally is deleted there (`architecture.md` §6). ⚠ The transcript below is the sequence
of lines the script writes; an actual run was **not** executed while writing this page, because
it would have mirrored to the live remote.

```
Starting sync with Google Drive...
rclone is installed.
All local directories exist.
Syncing edo-remote:Documents to /home/edo/Documents...
Syncing edo-remote:Books to /home/edo/Books...
Syncing edo-remote:Projects_toClean to /home/edo/Projects_toClean...
Sync completed successfully. Check sync_gdrive.log for details.
```

**Preview before you mirror.** No flag on either script exposes a dry run, so run rclone's own
preview — the same command the script runs, minus `--interactive`, plus `--dry-run`:

```bash
rclone sync --dry-run -v ~/Documents edo-remote:Documents
```

The deletions it *would* make are reported as skipped instead of performed. This is the cheapest
guard against the top risk in `project_scope.md` §7.

**The exit status is meaningful**, which is what makes the script usable from `cron`:

| Exit | Means |
|---|---|
| `0` | every pair synced |
| `1` | `rclone` is missing or broken, a local directory is missing, the two lists differ in length, or at least one pair failed |

A failing pair does not stop the remaining pairs; the failures are collected and named in the
log and on stdout, and the final status is `1`.

**The log.** `sync_gdrive.py` writes `sync_gdrive.log` **next to the script** (`sync_gdrive.py:22`).
`sync_gdrive.sh` still writes the relative path, so its log lands in the **working directory**
(`sync_gdrive.sh:20`) — one of several reasons the two implementations are not interchangeable.

```bash
tail -f ~/dev/simple-cloud/sync_gdrive.log
```

**From cron.** The intended schedule is **TBD** (`project_scope.md` §8) — this is the shape of
an entry, not this repository's actual schedule:

```cron
0 3 * * * cd /home/edo/dev/simple-cloud && /usr/bin/python3 sync_gdrive.py >> cron-sync.log 2>&1
```

The `cd` matters even though the Python script's log is script-relative: `.env` is read from
the working directory, and cron starts a job in an arbitrary one.

## 5. Sending a notification

Both scripts run only under `__main__`, and both check their configuration first. With nothing
configured, they say exactly what is missing and exit `1` — captured:

```console
$ python3 messaging/gmail/send_email.py
Missing environment variables: GMAIL_SENDER, GMAIL_RECIPIENT
Copy .env.example to .env and fill it in.
$ echo $?
1

$ python3 messaging/twilio/send_test_sms.py
Missing environment variables: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, SPAIN_PHONE_NUMBER
Copy .env.example to .env and fill it in.
$ echo $?
1
```

Once configured, one run sends one message and prints its identifier:

| Script | Sends | Prints |
|---|---|---|
| `send_email.py` | subject `Automated draft`, body `This is automated draft mail` | `Message Id: <id>` |
| `send_test_sms.py` | body `Test from simple-cloud!` (from `TWILIO_PHONE_NUMBER` to `SPAIN_PHONE_NUMBER`) | the message SID |

⚠ **These are the only commands on this page that send a real message to a real person**, and the
SMS costs money. Neither was executed while writing this page. The subject and body are the
defaults in `send_email()`'s signature (`send_email.py:64`) and the body is a literal in
`send_test_sms.py:48` — change them there.

**The Gmail first run is interactive.** With no `token.json`, `authenticate()` opens a local
server on port 8080 and waits for you to complete consent in a browser, then writes `token.json`
next to the script (`send_email.py:52-60`). Every later run reuses that token. Two consequences:

- Doing this on a headless box does not work — complete consent once on a machine with a
  browser, or copy a valid `token.json` to `messaging/gmail/`.
- Changing `SCOPES` invalidates the token. Delete `messaging/gmail/token.json` and run it again
  (`send_email.py:16`).

## 6. The secret guard

```bash
make secrets-check    # tree + boundaries — fast, the default
make secrets-all      # staged, tree, history, boundaries — the one CI runs
```

Covered in full in `secrets.md`; this is the day-to-day. A clean run:

```console
$ python3 scripts/secret_guard.py check --scope staged --scope boundaries

secret-guard: scopes [staged, boundaries] — 0 blocking, 0 warning(s)
```

A blocked commit — this is the real output of the guard run against a scratch repository with
one fake key staged:

```console
$ python3 scripts/secret_guard.py check --scope staged
x config.py:1  [pattern:aws-access-key-id] AKIA… <len=20 sha256:1a5d44a2>
x config.py:1  [entropy:AWS_ACCESS_KEY_ID] <redacted len=20 sha256:1a5d44a2>

secret-guard: scopes [staged] — 2 blocking, 0 warning(s)

How to fix:
  - If the value is real: remove it from the file and rotate it.
  - If it is a false positive: add `# pragma: allowlist secret` to the line,
    or add the exact literal to .secrets-allowlist.
  - Never commit with `--no-verify` to get past this.
$ echo $?
1
```

Two lines for one value is expected: the provider-shaped rule and the entropy rule both fire, and
the location is the same. The guard never prints the value — only a redacted preview and a
fingerprint (`secrets.md` §2).

An orphaned blob — the value was staged and then unstaged, so no ref can reach it. It is a
**warning**, not a block, and the exit status stays `0` unless you ask otherwise:

```console
$ python3 scripts/secret_guard.py check --scope history
! blob 474076c050 [unreachable]:1  [entropy:token] <redacted len=24 sha256:c1ae7a8b>

secret-guard: scopes [history] — 0 blocking, 1 warning(s)
$ echo $?
0
```

| Flag | Effect |
|---|---|
| `--scope NAME` | `staged`, `tree`, `history`, `boundaries` or `all`; repeatable. An unknown name is **rejected with exit `2`**, never silently ignored. Default: `tree boundaries`. |
| `--json` | one JSON object: `scopes`, `findings[]` (`severity`, `location`, `kind`, `label`, `preview`), `blocking`, `warnings` |
| `--strict` | treat warnings as failures (exit `1`) — for a history you intend to publish |
| `--root PATH` | scan a checkout other than the current directory |
| `install-hook` | what `make install-hooks` runs |

Exit codes: `0` clean, `1` blocking findings (or warnings under `--strict`), `2` the guard could
not run — not a git repository, an unknown scope, or no scopes selected.

**When it fires on something harmless.** Prefer the inline pragma, scoped to the one line:

```python
FAKE_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"  # pragma: allowlist secret
```

Or add the exact literal, never a prefix, to `.secrets-allowlist`. Both are committed and
reviewable on purpose. Never delete a rule to silence one finding, and never reach for
`--no-verify`: a bypassed guard still looks like protection.

**Also enable GitHub's own scanning.** This guard checks what *would* be pushed. In the
repository settings on GitHub, turn on **secret scanning** and **push protection** — they are
the complementary control for anything that gets past a local hook, and they cover the
provider-side detection this tool cannot do.

## 7. Tests and lint

```bash
make test     # python3 -m unittest discover -s test -t . -v
make lint     # ruff check .
```

```console
$ make test
...
Ran 38 tests in 0.040s

OK (skipped=1)
```

The skip is the Twilio send test, which needs `twilio` installed; it skips rather than fails
(`test/test_messaging.py:121`). One class or one test:

```bash
python3 -m unittest test.test_messaging.SendEmailTests -v
python3 -m unittest test.test_messaging.SendEmailTests.test_a_failed_send_is_reported_and_returns_non_zero
```

What the suite covers, and what it does not, is in `architecture.md` §8. In short: the guard's
detection layers, the sync pairing and invocation logic, and both notification scripts'
configuration handling — **not** a live remote, a real email, or a real SMS.

`make lint` reports that it is skipping when `ruff` is not installed. CI runs it unconditionally
on Python 3.9 and 3.12 (`.github/workflows/ci.yml`).

## 8. Worked example — from edit to commit

The intended loop, with the guard as the gate:

```bash
# 1. You add something that looks like a credential.
$ $EDITOR config.py

# 2. Check before staging, so the finding never becomes a commit.
$ make secrets-check
x config.py:1  [pattern:aws-access-key-id] AKIA… <len=20 sha256:1a5d44a2>
...
$ echo $?
1

# 3. The value is real, so it belongs in the environment, not in the code:
#    put it in .env (gitignored) and read it with os.environ in config.py.
$ $EDITOR .env config.py

# 4. Clean.
$ make secrets-check

secret-guard: scopes [tree, boundaries] — 0 blocking, 0 warning(s)

# 5. Commit. The hook runs the staged and boundary scopes for you and names itself.
$ git add config.py .gitignore && git commit -m "read the key from the environment"
secret-guard: scopes [staged, boundaries] — 0 blocking, 0 warning(s)
[main 1a2b3c4] read the key from the environment
```

Step 5 is the point of `make install-hooks`: the check runs whether or not you remembered it.
If it blocks and the value is **real**, rotate it first — everything else is secondary
(`secrets.md` §9).

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Missing environment variables: …` (exit `1`) | No `.env`, or it does not hold those names | `cp .env.example .env` and fill it in, or export the variables |
| `rclone is not installed. Please install rclone and try again.` | No `rclone` on the PATH | Install it; `sync_gdrive.sh` can self-update, or see rclone's docs |
| `Directory … does not exist.` | One of the three local directories is missing | Create it, or edit `LOCAL_DIRS` / `REMOTE_DIRS` **together** — the lengths must match |
| The hook does not run | `core.hooksPath` is per-clone | `make install-hooks` |
| `secret-guard: no Python interpreter found; refusing to commit.` | No `python3`/`python` on the PATH | Install Python 3 — or `git commit --no-verify` deliberately, knowing what that skips |
| `secret-guard: … is not a git repository` (exit `2`) | Ran outside a checkout, or `--root` is wrong | Run from the repository root, or pass `--root` |
| The guard flags a test fixture | It cannot tell a fixture from a live key | `# pragma: allowlist secret` on the line, or the exact literal in `.secrets-allowlist` |
| `make lint` prints `ruff not installed; skipping` | `ruff` is optional here | `python3 -m pip install ruff`, or rely on CI |
| Gmail: consent flow never completes | No browser — a headless box | Complete it once where a browser exists, then copy `token.json` into `messaging/gmail/` |
| Gmail: `An error occurred: …` | Expired or revoked token, or the API is not enabled for the project | Delete `token.json` and re-run; it re-consents |
| `sync_gdrive.log` is not where you expect | The Bash script's log is working-directory relative | Run `sync_gdrive.sh` from the directory you want the log in, or use `sync_gdrive.py` |

## 10. What was verified for this page

Captured from real runs on 2026-10-04 in this checkout: `make help`, `make secrets-check`, the
guard's `--help` and `--json` output, both scripts' missing-variable output and exit status, the
blocked-commit and orphaned-blob transcripts (§6 — produced against a **scratch repository in
`/tmp`, since discarded**, so no blob entered this one), `make test`, `make lint`, and the
single-class test invocation.

Executed on **Python 3.14.0** with **ruff 0.14.14**. CI declares 3.9 and 3.12, which have not
been run locally; Python 3.9 *grammar* compatibility was checked over every tracked `.py` file
with `ast.parse(..., feature_version=(3, 9))`, which is not the same as executing it there.

**Not executed, deliberately:** any real sync (it mirrors, so it deletes at the destination), any
email, and any SMS. Their outputs are described from the code, and marked as such above.
