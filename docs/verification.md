# Current verification and acceptance — 2026-11-06

## a23 session usability candidate

See [the seven-item scope and acceptance boundaries](session-usability.md).
Text input normalization is confined to desktop IPC; callers and App Server
wire inputs are unchanged. Bounded chat-name metadata and native activity drive
literal labels and global ordering. Output attribution is handled separately from
real attachment input; unsupported quota polling is skipped and ordinary refresh
does not post unknown quota messages. `!quota` is an explicit capability/data query.
Permanent cards have no self-navigation, and durable root-scoped navigation intent
prevents repeated or uncertain notices from being posted again.

Two capabilities remain limited: the web fallback can still open a browser on
mobile; exact-turn IPC Steer remains disabled because the inspected follower
method cannot atomically honor an expected turn ID. App Server Steer retains its
exact-target request and rejects an unconfirmed target receipt. Neither mobile
acceptance nor historical native chat/delete recovery is claimed by local tests.
This source candidate does not replace the running a22 installation.


## a22 startup after a desktop update

The reusable launcher loads the active deployment profile configuration instead
of the historical root candidate configuration. Both English and Chinese manager
window titles can be restored on a second shortcut launch without creating a duplicate.
Desktop IPC support includes the exact version, executable hash and package identity
for Codex 26.1103.3517.0 (157.0.8103.39), alongside the previously inspected build.
Unknown builds remain blocked. Installed protocol inspection confirms unchanged
initialize, owner discovery, following/snapshot, start-turn and exact-turn interrupt
message versions. A read-only live snapshot check is required before deployment;
this is not model/file/Slack acceptance. Native processes must remain running.

## a21 candidate desktop project display names

Project and conversation pickers show desktop-renamed labels without full project
paths. Session cards reuse the labels; exact directory/thread routing is unchanged.
A bounded selective read extracts only project name metadata from desktop UI state;
unrelated top-level values are skipped. Names refresh when pickers reopen.
See [project name source and fallbacks](project-display-names.md).

Read-only local metadata projection matched the desktop project API's names for
the configured allowlist, including Chinese aliases. Related picker, language,
state and routing regressions passed. Full supported Windows verification passed
**855 tests, zero failures/errors/skips**, Ruff, compile, build, archive integrity,
offline installation and installed imports. Run:
`.work-issues/verification-5372dcf3-1fd0-4d9b-841c-f0c2424b6c27/`. No native project was renamed, and the running a20 was not replaced.

## a20 candidate session language and public progress

Reopening a known root refreshes its language and live status before linking to
Slack history. Missing native state shows unknown instead of stale online/idle.
The pinned IPC adapter exposes the latest public commentary from the exact active
turn; progress text is literal and no start/completion events are synthesized.
Status presentation does not release input/unknown protection. See
[session display](session-display.md).

Targeted regressions reproduced the old failure before the fix; 216 related cases
and 59 focused IPC/session cases passed after repair. Full supported Windows
verification passed **831 tests, zero failures/errors/skips**, Ruff, compilation,
build, archive integrity, offline installation and installed imports. Run:
`.work-issues/verification-9895004a-e06a-4357-bae7-2b1007851387/`. Read-only native schema inspection confirmed the
current desktop supplies active status, in-progress turn IDs and public commentary.
No live Slack task, model task, upload or runtime replacement was performed for a20.
The deployed a19 stays pinned pending separately authorized replacement.

## a19 candidate output delivery and bilingual preferences

Newly completed Slack-tracked tasks automatically send documents/images below
30,000,000 bytes by default. Videos, other supported outputs and files at/above
the threshold receive a one-use direct confirmation. User preferences control
language, automatic delivery, threshold and the outbound ceiling. Windows and
Slack bridge UI share the same non-secret preference file; model replies bypass
translation. Existing deployment configs and native processes are not modified.
See [behavior and configuration](output-delivery-settings.md).

Full supported Windows verification passed **819 tests, zero failures/errors/skips**,
Ruff, compile, wheel/sdist checks, offline installation and installed imports.
Run: `.work-issues/verification-f6808bee-c0bb-483d-8be5-89d8539b94fd/`.
An earlier run had one existing windowless-shutdown process-exit identity race;
814 other cases passed. No test or process-identity safeguard was weakened.
Final focused checks additionally cover the last confirmation-card/path and UI
keyboard-access changes. Packaging is rebuilt after these final edits.

The 50 added cases cover threshold equality, video/manual confirmation, duplicate
clicks, file/epoch/binding/root/user/action changes, ACK failure, policy/ceiling
changes, unknown-upload non-replay, corrupt/atomic preferences, output-only video
support and literal model replies. Existing completion tests now assert default
automatic delivery once without a picker.

Computer Use inspected an isolated synthetic Windows supervisor in English and
Chinese, selected Chinese and clicked Save, then verified the persisted language
and unchanged 30/100 MB values. The test adapter had no live worker/native start
capability. No real Slack/model/file acceptance, deployment or native restart was
performed. The running installation remains separate from this candidate.

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
