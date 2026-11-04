# Repository workflow

Codex develops; AGY reviews and manages PR, CI and merge. Both use one existing
checkout, with one active operator at a time. The shared entry point is
[AGENTS.md](../AGENTS.md). There is no simultaneous development in this directory
and no separate worktree arrangement.

## Ownership and synchronization

| Stage | Active operator | Permitted work |
| --- | --- | --- |
| Development | Codex | Branch preparation, source/docs/tests, local checks and commits |
| Review | AGY | Read the frozen diff, run checks, report findings; no tracked-file edits |
| Rework | Codex | Fix findings and commit; AGY pauses integration |
| Integration | AGY | Push topic branch, PR, CI, merge the verified SHA, sync local main |

Transfer ownership using `.work-collaboration/handoff.md`. It is ignored by the
existing `.work*/` rule and must not be included in a public PR. Start each task
by checking that record, `git status --short`, the current branch and full HEAD
SHA. Do not auto-stash, discard files or reset a branch to force synchronization.
An ownership record coordinates cooperating agents; it does not lock the OS.

Codex starts a task from current public `origin/main`, on `codex/<task>`, after
confirming the checkout is clean and AGY has handed it back. Codex commits the
intended changes before transferring to AGY. While AGY is reviewing or integrating,
Codex must not edit files, switch branches, stage/commit, fetch/merge/rebase or run
tests that generate files in this checkout.

If AGY finds a defect or CI needs a code fix, it records findings, transfers back
to Codex and pauses. The changed SHA must be reviewed again. A moving HEAD or
unexpected worktree edit invalidates the previous handoff until reconciled.

## Handoff record

Use this local format, with no credentials, private task payloads or full environment:

```text
Owner: AGY
Stage: review
Task: <agreed scope>
Repository: <this checkout>
Branch: codex/<task>
Base: <full origin/main SHA>
Head: <full committed SHA>
Changed files: <explicit list>
Checks: <commands and pass/fail/not-run results>
Known gaps: <specific limitations or none>
Runtime impact: <deployed runtime unchanged, or separately authorized scope>
Next: AGY reviews this exact head; return findings to Codex or proceed with PR/CI/merge
```

After integration, record `Owner: Codex`, `Stage: idle`, the PR and merge SHA.
Start the next task only after the owner confirms that transfer. Root `handoff.md`
is historical local context, not the current per-task ownership record.

## GitHub main protection

The public `main` branch requires a pull request, an up-to-date passing `windows`
check from GitHub Actions and resolved conversations. Protection applies to
administrators. Force pushes and branch deletion are disabled. Required formal
approvals are zero because one account cannot approve its own PR; AGY review is
recorded against the exact SHA in the PR, rather than represented as a different
GitHub identity. Branch protection enforces PR/CI, not which AI performed review.

AGY pushes only the agreed topic branch and uses the PR template. Merge only after
review and current CI pass, with an expected head SHA so a changed head is refused.
If main advances, update the topic branch, inspect the resulting diff and rerun
affected checks before merging. Do not disable checks, use administrator bypass
or force-push main to resolve a conflict.

After merge, AGY verifies a clean checkout, fetches origin and fast-forwards local
main before handing control back. Public origin/main is the source of truth for
new work. The private remote/old branches are archival, with no routine two-way
sync or history merge into the newly public repository.

## Checks and live runtime

Run existing checks appropriate to the changed behavior. The full supported
Windows check is `scripts/check_open_issues.ps1`; CI runs hosted regression, Ruff,
compile and package integrity checks. Its positive `desktop_host` gate is excluded
and requires local evidence. Green CI does not prove actual Slack/model/file
acceptance or replace AGY review.

A local commit, push, PR, merge or branch switch does not deploy a bridge runtime.
Keep `.work-issues/live-20261103/`, credentials/configuration, databases, pinned
operator/runtime and shortcuts intact. Ordinary workflow must not restart/stop
the bridge or native desktop/backend, migrate state, read credentials or send live
acceptance tasks/files/messages. Runtime changes require their own explicit scope.

Keep latest public documentation in README and docs. Historical plans/probes remain
in archive history; do not restore them into public main as part of synchronization.
