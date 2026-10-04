# simple-cloud — Documentation

> Index for this directory. Pages mirror the doc set in `job-finder/docs`
> (`project_scope.md`, `architecture.md`, `secrets.md`); the missing pages and why are
> listed at the bottom.

| Page | Covers |
|---|---|
| [`project_scope.md`](project_scope.md) | What this repository is for, what is in and out of scope, risks, and what is still unconfirmed. |
| [`architecture.md`](architecture.md) | Components, directory layout, data flow, external services, environment variables, the sync model, and how to run and test everything. |
| [`usage.md`](usage.md) | How to run it: setup, the command reference, worked examples, the guard day to day, and troubleshooting. |
| [`secrets.md`](secrets.md) | The secret guard: what is at risk, how it detects credentials, how to install it, and what to do if one leaks. |

Read `project_scope.md` first for what the repository is, `architecture.md` for how it is put
together, `usage.md` to actually run something, and `secrets.md` before handling credentials.

## Relation to the reference doc set

The reference repository (`job-finder`) documents five pages. Three apply here as-is, one is an
addition, and two do not apply, and inventing them would be worse than omitting them:

| Reference page | Here | Why |
|---|---|---|
| `project_scope.md` | ✅ present | Applies to any repository. |
| `architecture.md` | ✅ present | Applies to any repository. |
| `secrets.md` | ✅ present | Applies to any repository; the guard is stdlib-only and portable. |
| `agents.md` | ❌ omitted | This repository contains no LLM agents, prompts, or model calls. There is nothing to document. |
| `memory.md` | ❌ omitted | A decision log records *decisions made by the owner*. No such record exists for this repository, and fabricating one would violate the audit's "do not invent" rule. The verifiable substitute — what the git history actually shows — is in `architecture.md` §10. |
| — | ➕ `usage.md` | **Not in the reference set.** Added because this repository's setup is not a single command: it needs a hook installed, a `.env` created, and a distinction between the commands that are safe to run and the four that are not. `architecture.md` §9 records the invocations; `usage.md` is where the examples, transcripts and troubleshooting live. |

There is no `security.md` separate from `secrets.md`: the audit brief names a security page,
and the reference template names it `secrets.md`. Following the template, the guard
documentation lives in `secrets.md` and covers install, operation, and leak response.
