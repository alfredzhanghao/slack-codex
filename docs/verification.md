# Current verification and acceptance — 2026-11-04

## a18 candidate reusable navigation and command alias

`!task` aliases `!tasks`, including project/filter handling. A valid navigation
button can reopen its exact existing chat/root. Each picker message gets independent
root-scoped tokens even when picker posts overlap. Expiry and connection invalidation
still apply; business controls remain one-use. Distinct clicks may navigate again,
but redelivery of the same action timestamp/envelope is acknowledged without
repeating navigation. No native task is used to test these changes.

The current verification run and local installation status are retained in the
per-task handoff. The a17 evidence below remains the IPC transport baseline.

## a17 candidate no-restart desktop IPC

The current Windows desktop's existing coordination pipe was verified against its
actual server process identity, configured executable/hash/package version and
Windows user/session. Source discovery reports a connectable IPC instance without
a TCP backend peer. Initialize, exact chat owner discovery, model-settings reads
and targeted active-chat snapshots succeeded without a native restart.

An isolated test connection record exercised selection validation, initialize,
adoption, persisted-record verification and two fresh exact-chat snapshots. The
connection remained open, all four known native process creation identities and
the original connection-record hash stayed unchanged. This adopted only a separate
test record; the installed a15 profile, its store and the running desktop were
not replaced. Private sanitized results are retained in
`.work-issues/shared-interface/live-readback.json` and
`live-isolated-adoption.json`.

Full a17 verification passed **752 tests with zero failures/errors/skips**, Ruff,
compilation, wheel/sdist integrity, offline installation and installed imports.
Run: `.work-issues/verification-4f844d45-17ee-412c-88a3-8daa7eef43f3/`.
The final result documentation is repackaged and archive-checked separately;
application/test source is unchanged from this full run.

The 47 new regression cases cover actual framing, source/project filtering and
pagination, read-only metadata, Windows extended paths, exact owner/turn mapping,
missing write receipts, unsupported parameters/methods, capability-aware Slack
cards, version/package pins, stale selection, record preservation and native-stop
refusal. The full Windows check and packaging result are recorded in the current
per-task handoff. No live Slack/model task, approval, upload or deployed runtime
update is claimed. IPC task/file delivery remains user acceptance; the successful
read connection is not presented as full WebSocket capability parity.

See [the transport contract](adaptive-instance-selection.md) for supported actions
and why Steer/approvals/settings/Skills/Compact/Fork stay in the desktop on IPC.

## Prior a16 instance selection baseline

The new candidate scans existing desktop roots and asks for explicit selection
and confirmation. The pinned local a15 installation is not replaced. Tests cover
stale/reused identities, listener/peer/binary changes, previous uncertain launches,
unsupported transports, one-use selection and running-worker refusal. An actual
loopback native-protocol fixture receives only initialize/initialized. A real
Windows two-process TCP fixture verifies attachment preserves both process identities.
The latter needs standard host process/TCP visibility; the restricted local tool
sandbox could not see its peer, while the identical standard-host test passed.

A synthetic native supervisor window displayed both supported and unsupported
instances. Cancelling confirmation left only the scan event; confirming produced
one fake attachment/start, and Exit stopped only that fake worker. This is UI
acceptance with synthetic instances, not an actual native Codex attachment.
At the a16 baseline, read-only discovery of this host's desktop reported no_shared_endpoint and
leaves its identity record unchanged. No real Codex restart/adoption, Slack/model
request, upload or deployed-runtime update was performed.

Full a16 candidate verification passed **705 tests, zero failures/errors/skips**,
Ruff, compilation, build, archive/source checks, offline installation and installed
imports. Run: `.work-issues/verification-68682815-92b4-49e8-b43d-01f60822266a/`.
The final documentation was then rebuilt/rechecked without changing application
modules. Archive identities and the exact reviewed source target are retained in
the private verification run and per-task AGY handoff. The records below describe
the prior a15 acceptance baseline, not acceptance of a private stdio transport.

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
