# Repository workflow

Use a `codex/` branch for a scoped change. Preserve unrelated local edits. Run the
checks appropriate to the change; the full maintained Windows check is
`scripts/check_open_issues.ps1`. Do not read credentials or send live acceptance
requests as part of routine tests.

A commit saves local history; push updates GitHub; a PR exposes the diff; merge
adds it to main. None of these deploys an installed bridge or reruns native work.
Create/push/merge only within the user's authorized scope. Read the PR checks and
merge the verified head SHA. Do not bypass failing checks or force-push main.

CI runs tests, Ruff, compilation and package integrity checks. The hosted Windows
job explicitly excludes one positive desktop-host lifecycle gate, which must pass
locally before release. CI success is not native Slack/model/file acceptance or
an independent code review. No automatic approval/merge policy is implied.

Current documentation lives in README and docs. Superseded plans, phase reports,
prototype probes and design drafts are recoverable from Git history; do not restore
them into the working tree merely for historical links. Local `.work-issues`
operational files, environment credentials, SQLite stores and shortcuts are ignored
and must not enter release archives. Preserve active/rollback dependencies before
cleaning local artifacts.
