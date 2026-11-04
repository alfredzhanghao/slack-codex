# Current verification and acceptance — 2026-11-03

## Automated checks

Run `scripts/check_open_issues.ps1` on the supported Windows host. It performs
pytest, Ruff, compilation, wheel/sdist verification, isolated offline installation
and installed imports. Each run writes a result/logs under
`.work-issues/verification-UUID/`. GitHub CI intentionally deselects the positive
`desktop_host` breakaway-survival gate; hosted green does not prove that gate.

Before repository cleanup, the final core package passed 676 tests with zero
failures/errors/skips, plus lint/build/install checks. The subsequent reusable
launcher/lifecycle focused run passed 22 tests. The cleanup changes no application
modules; it updates the packaged documentation inventory and its archive tests.
Post-cleanup full Windows verification passed **679 tests, zero failures/errors/
skips**, plus Ruff, compilation, build, archive checks, offline installation and
installed imports. Result: `.work-issues/verification-be6fdf3f-8585-43d7-bc1c-e06d6f7233ff/result.json`.
Application source and the pinned deployed runtime retained their pre-cleanup
SHA256 values. Documentation-only completion below is rebuilt/rechecked against
the same source after recording these results.

## Actual bounded acceptance

Native Windows/Slack acceptance established fresh project commands, a persistent
exact test thread across repeated opens and worker restart, Skills pagination,
exact-path Details/selection preview, Settings, native image content, the complete
attachment-only document -> staging -> follow-up -> completed-task chain, unique
natural-language retrieval and ambiguity selection. Two files were selected in
one actual Slack modal submission; downloaded output bytes matched their sources.

Actual supervisor acceptance exercised Start/Stop/Restore/Exit, owned-worker crash
recovery and observations of at least 120 seconds. The reproduced transient status
read fault was repaired: 2,751 successful reads over 120.047 seconds included one
recovered transient denial and zero errors. Exit/reopen/Start and hide/same-window
restore were observed. Alfred confirmed #8's remaining manual verification passed
on 2026-11-03; this is user confirmation, not an automated tray-click observation.

Navigation permalink shape and >8 MiB native-history failures were reproduced and
repaired with regressions. The latest Slack entry retest was left to Alfred at his
request. Native MCP elicitation remains outside the accepted scope. These bounded
observations do not imply unrestricted production acceptance.

Issues #6, #8, #9, #10, #11 and #12 were closed as completed on 2026-11-03. PR #13
merged documentation at bca5e0d; runtime fixes were already on main in d80ee1e and
f903ec7. Git history retains the detailed historical reports removed from the
working tree. Private operational records remain local and are not release assets.

## Cleanup boundaries

The live deployment directory, local credentials/configuration, current operation
ledger and independent rollback state are retained. Retired phase workspaces,
probe sources, duplicate verification builds and old documents are removed.
No model task, Slack message/file, service restart or application deployment is
part of repository cleanup. A pre-cleanup read observed the worker already stopped
with backend_rejected; this cleanup does not claim to restore that connection.

The 26 initially permission-blocked pytest/build directories were subsequently
removed through a fixed-allowlist administrator helper. Independent readback
confirmed all 26 roots absent and the residual list empty. The helper did not
traverse directory links or change credential/data-store permissions. Application
and pinned-runtime module hashes remained unchanged.
