# Shared development rules

These project rules apply to Codex and Antigravity (AGY), subject to the user's
current instructions and higher-priority runtime constraints. Read
[the workflow](docs/repository-workflow.md) before changing this checkout.

## One checkout, one active operator

- Use the existing project directory. Do not create another checkout/worktree
  unless Alfred explicitly changes this arrangement.
- Codex owns development: scoped source/docs/test edits, relevant local checks,
  and local commits on a `codex/<task>` branch based on current `origin/main`.
- AGY owns source review, branch push, PR creation/update, CI follow-up and merge
  for the task Alfred has authorized. A review-only provider invocation remains
  read-only; it does not grant tools or side-effect permissions it lacks.
- Never operate concurrently in this directory. Branch switches, staging,
  commits, fetch/merge/rebase, test artifact generation and remote writes all
  belong to the current operator. The other agent waits or inspects read-only.
- Transfer ownership explicitly through `.work-collaboration/handoff.md`, an
  ignored local record. If its owner is the other agent, or ownership is unclear,
  do not mutate the checkout; clarify ownership first.
- This is a cooperative workflow, not an OS lock. Check branch, HEAD and worktree
  state again before each mutation. Unexpected changes mean stop and inspect,
  not stash/reset/clean or overwrite someone else's work.

## Codex development and handoff

1. Inspect status and the local handoff. Preserve unrelated changes. On a clean
   checkout, fetch `origin` and create a task branch from `origin/main`.
2. Implement the agreed task and run relevant existing checks. Keep changes small.
3. Commit only the task's intended files. Record the branch, exact full commit
   SHA, base SHA, changed files, checks/results, known gaps, runtime impact and
   the next operator in the local handoff. Never include credentials.
4. Transfer to AGY and stop modifying files/Git state. Do not self-approve, open
   or merge the PR in the default workflow. Explicit user overrides take priority.

## AGY review and integration

1. Read the handoff and verify its branch/SHA against Git. Review the exact diff
   from `origin/main`; keep tracked source/tests/docs unchanged during review.
2. Return concrete findings to Codex if changes are needed. Transfer ownership
   back and stop integration. Codex fixes and commits; review the new SHA.
3. After review passes, push only the task branch to `origin`, create/update its
   PR and record the reviewed SHA and findings in the PR description/comment.
4. Require the current PR head's GitHub Actions `windows` check to pass and the
   branch to be up to date. Resolve review discussions; never bypass protection.
5. Merge only the reviewed and checked head SHA. If the head/base changes, inspect
   the new diff and repeat affected review/checks; never assume old CI applies.
6. After merge, verify the worktree is clean, fetch and fast-forward local `main`.
   Record the PR/merge SHA and transfer the next development task to Codex.

## Repository and runtime boundaries

- `origin/main` is the public development baseline. The `private` remote and
  pre-publication branches are archival; do not merge their history into public
  branches or push changes to the private archive as routine synchronization.
- `main` requires a PR, the up-to-date GitHub Actions `windows` check and resolved
  conversations. Force pushes/deletion are disabled, including for administrators.
  Zero formal approvals avoids same-account self-approval deadlock; it does not
  replace the AGY source review and reviewed-SHA record.
- Preserve ignored local configuration, credentials, databases, shortcuts and
  `.work-issues/live-20261103/`. Do not read/print/copy credential-file contents.
- Git sync/PR/merge is not deployment. Do not restart, stop, upgrade or migrate
  the running bridge/native desktop/backend, change their pinned hashes/config,
  or run Slack/model/file acceptance as part of ordinary development/review.
  Obtain explicit task-specific authorization for runtime operations.
- Use existing tests and checks appropriate to the change. Full Windows checks:
  `scripts/check_open_issues.ps1`. CI excludes the positive `desktop_host` gate;
  do not equate hosted CI with that local gate or actual Slack acceptance.
