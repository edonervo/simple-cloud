# Secret Guard — Keeping Credentials Out of the Repository

> `scripts/secret_guard.py` · `.githooks/pre-commit` · `.github/workflows/secret-check.yml`
>
> Runs on the standard library alone, on purpose: the hook has to work in a fresh clone
> with no virtualenv and no `pip install`. No network access is used.
>
> This is the page the audit brief calls `security.md`; it follows the reference doc set,
> which names it `secrets.md`.

## 1. What is actually at risk

Three categories, and they fail differently.

| Asset | Where it lives | Consequence of publication |
|---|---|---|
| `TWILIO_AUTH_TOKEN` (+ `TWILIO_ACCOUNT_SID`) | `.env` (ignored, not tracked) | Someone else sends SMS on the owner's Twilio account and spends their balance. Rotatable, but only after noticing. |
| `credentials.json` / `token.json` | working directory (gitignored) | The Gmail OAuth client and a refresh token. A refresh token grants access to the mailbox until revoked — not fixed by rotating a password. |
| Two email addresses | hardcoded in `messaging/gmail/send_email.py:56-57` | Personal data in the repository, publicly readable forever, and *not rotatable*. |

The Twilio token is the one value the owner can rotate cheaply and the one most likely to be
pasted somewhere it should not be — it is read from the environment in
`messaging/twilio/send_test_sms.py`, which means it already exists as a string on the
machine that runs it. The Gmail token is the more damaging of the two if it leaks, because a
mailbox is not something one can reset. The addresses are the only *current* exposure: they
are committed, real-looking, and not secrets — but they are PII that a public repository
publishes permanently.

**Findings from the first full scan of this repository** (§4 records the scopes):

- **No secret value exists in the git history.** Nine files have ever been added
  (`architecture.md` §2), none of them a credential, and no blob in any object — reachable
  or orphaned — matched a rule.
- **The `.gitignore` was under-specified.** It protected `.env`, `credentials.json` and
  `token.json`, but not `.env.*` (so `.env.local`, a common local override, was
  unprotected), not `*.pem` / `*.key`, not service-account JSON, not local SQLite files, and
  not the generated `sync_gdrive.log`. All were added (§5).
- **The pre-commit test produced a live warning.** Adding a fake AWS key to confirm the hook
  blocks, then unstaging it, left the blob orphaned in the object store — which the history
  scan correctly reported as a *warning*, not a block (see the reachability table in §4).

## 2. Design constraints

**The guard must never print what it finds.** A scanner that echoes a value into a terminal,
a CI log, or a JSON report *is* a leak, and CI logs are frequently more widely readable than
the repository. Every finding carries a redacted fingerprint instead:

```
x messaging/x.py:12  [pattern:aws-access-key-id] AKIA… <len=20 sha256:1a5d44a2>
x blob 4a0c303479 [unreachable]:1  [entropy:aws_access_key_id] <redacted len=20 sha256:1a5d44a2>
```

The first four characters are shown only for a **pattern** match, because that prefix is the
provider's own fixed marker and is what tells the owner what kind of credential leaked. A
value read from the owner's own configuration (layer 1, §3) shows **nothing** — the setting's
name already identifies it. Findings carry `path:line` rather than a snippet of source,
because a snippet is the one place the guard would echo file content, and file content is
what is untrusted here.

**It must not cry wolf.** A guard that flags the word "token" in a docstring teaches its owner
to reach for `--no-verify`, which is strictly worse than no guard, because a bypassed guard
still looks like protection. The rules are narrow, placeholders are filtered, and there are
two reviewable escape hatches (§6).

**It must not pass silently when it is broken.** The hook exits non-zero if it cannot find a
Python interpreter, rather than waving the commit through.

## 3. Detection layers

| Layer | Catches | False-positive risk |
|---|---|---|
| **Exact values from configuration** | The real values from the owner's `.env`, anywhere, at any depth — including inside a blob in history | None. A value either appears or it does not. |
| **Provider-shaped patterns** | Credentials this install never held: an old token, someone else's key, a fixture from a tutorial | Low — each rule is anchored to a prefix a provider actually issues (Twilio `AC`/`SK`, Google `AIza`/`GOCSPX-`, AWS `AKIA`, GitHub `ghp_`, Slack `xox…`, Stripe `sk_live_`, and others) |
| **High-entropy assignments** | Providers nobody has written a rule for yet | The only layer that can misfire, so it demands a credential-ish name, ≥16 chars, ≥8 distinct characters, entropy ≥3.5 bits/char, **and** a digit or base64 marker |

Layer 3 is bounded deliberately. It reads `NAME = value` where `NAME` contains
`key`/`token`/`secret`/`passw`/`pwd`/`credential`/`bearer`, and it skips any value that
contains `.`, `(`, `$`, `{`, or a placeholder word — without that, ordinary statements read
as assignments. **Known limit:** an all-alphabetic passphrase with no digit is not caught by
this layer. Guessing at those is what turns a guard into a nuisance.

## 4. Scopes

`--scope` is repeatable (`--scope tree --scope history`); an unrecognised name is
**rejected at the argument layer**, not ignored, because a typo that silently resolves to *no*
scopes produces a confident green over a repository the guard never looked at — the one
outcome worse than a failure.

| Scope | Question | Used by |
|---|---|---|
| `staged` | What is about to enter history, right now? | the pre-commit hook |
| `tree` | What would a clone receive? | default |
| `history` | What is already in history and would need rewriting? | CI (with `fetch-depth: 0`) |
| `boundaries` | Do the ignore rules still protect what they claim to? | default; the hook |

`staged` reads the **index**, not the working tree. A file can be staged with a key and then
cleaned up in the editor: the working tree looks innocent while the commit carries the key.

`history` reads `--batch-all-objects` rather than walking refs, so it also sees objects that
are no longer reachable — the leftovers of `commit --amend`, `reset --hard`, `rebase`, and of
a staged-then-unstaged file. It then **grades each hit by reachability**:

| Blob is | Means | Severity | What to do |
|---|---|---|---|
| reachable from some ref | pushed, or about to be | **block** | rotate the value, then rewrite history |
| reachable from no ref | orphaned locally; git pushes and clones only what its refs lead to | **warn** | `git gc --prune=now` |

`git rm` does not un-leak: the blob stays reachable from the old commit, so it blocks. But
`git add` a key and then unstage it leaves an orphan that nobody else ever received, so
blocking would be crying wolf — and staying silent would be worse, because an object one
`git tag` away from publication is not a thing to say nothing about. The guard's own test run
over this repository produced exactly that case (`secrets.md` §1).

## 5. Boundary checks — the rules, not the files

`boundaries` checks the **ignore rules**, because the likeliest way a credential reaches
GitHub is not a missing rule but a rule that stopped applying.

- **`.gitignore` never applies to a file git already tracks.** One `git add -f .env`, and the
  rule reads as protective while doing nothing — indistinguishable, in review, from the state
  before the mistake. A tracked *and* ignored file is a blocking finding.
- **Seed files a clone needs must not be excluded.** `.env.example` is protected from
  accidental exclusion by a `!.env.example` negation, and it is checked for *presence* and for
  **blank values** — the published template may hold credential *names*, never values.
- **Required rules are asserted by name.** Deleting `.env`, `.env.*`, `*.pem`, `*.key`, the
  credential files, the service-account patterns, the local-database patterns, or the
  notebook-output rule fails the scan.

## 6. Escape hatches

Both are committed and reviewable, which is the point: every entry is a hole in the guard,
and the diff that opens one should be visible.

```python
API_KEY = "..."  # pragma: allowlist secret       # one line, next to what it exempts
```

```
# .secrets-allowlist — one literal per line, # starts a comment
test-bulk-key
```

Prefer the inline pragma: it is scoped to one line. An allowlist entry suppresses any finding
whose value contains that literal, so keep entries narrow — the exact string, never a prefix.

## 7. Wiring

```bash
make install-hooks                          # one command; sets core.hooksPath=.githooks
make secrets-check                          # tree + boundary checks
make secrets-all                            # staged, tree, history, boundaries
python3 scripts/secret_guard.py check --scope all --json   # machine-readable

# equivalently, without make:
python3 scripts/secret_guard.py install-hook
```

The hook is committed at `.githooks/pre-commit` rather than generated into `.git/hooks`,
because a hook that lives only in `.git` is invisible in review, is not shared by a clone, and
is lost on the next clone. `make install-hooks` must be run **once per clone**; the CI
workflow is the backstop for a clone where it was not.

`.github/workflows/secret-check.yml` runs on push, on PR, and weekly, and covers what the hook
cannot: `--no-verify`, a missing hook, a merge from a machine that had no hook, and a rule
edited after the fact. `fetch-depth: 0` is load-bearing — a shallow clone has almost no
history, and the history scan would then report a confident green over exactly the commits
most likely to hold an old secret.

## 8. What this does not cover

Stated so the guard is not trusted past its evidence.

- **A secret that is transformed.** Base64-encoded, encrypted, split across lines, or
  embedded in a binary blob is not matched. Layer 1 matches a literal value only.
- **A secret already pushed before the guard existed.** The history scope can find it, but
  nothing rewrites it automatically — see §9.
- **The remote.** This checks what *would* be pushed, not what is already on GitHub.
  GitHub's own **secret scanning** and **push protection** are the complementary control and
  must be enabled in the repository settings (see the audit's manual steps).
- **A secret in a commit message, an issue, or a PR body.**
- **PII that is not a credential** — including the two email addresses in `send_email.py`.
  The guard looks for credentials, not for personal data. Those addresses are a `project_scope.md`
  §8 open question, not something this tool will flag.
- **Not a substitute for rotation.** A guard reduces the odds. It does not make a leaked key
  safe again.

## 9. If it fires

1. **If the value is real, rotate it first.** Everything else is secondary; the value may
   already have been fetched.
2. **Not yet committed** — remove it from the file, and check whether it was staged earlier
   (`git diff --cached`).
3. **Committed but not pushed** — `git reset --soft HEAD~1` and edit, or `git commit --amend`.
4. **Pushed** — assume it is compromised. Rotate, then rewrite history (`git filter-repo`),
   force-push, and tell anyone who has a clone. Deleting it in a new commit leaves it readable
   in the old one, which is the whole reason the `history` scope exists.
5. **Orphaned blob warning only** — nothing left the machine; clear it with
   `git gc --prune=now`.
6. **If it is a false positive** — add `# pragma: allowlist secret` to the line, or the exact
   literal to `.secrets-allowlist`. Narrow the entry to the exact string; never add a prefix,
   and never delete a rule to silence one finding.
