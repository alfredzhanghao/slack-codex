# Windows operations — 0.3.0a16 candidate

## Existing instance selection

The candidate supervisor scans existing configured desktop instances on startup.
Select an instance and click **Connect selected**, then confirm its PID, path,
version and backend/port. Refresh clears the selection. Snapshots expire after
two minutes; identities, hashes and actual TCP peer are checked again at Connect.
A read-only native initialize handshake must also succeed before the old record
is replaced, followed by a second identity/peer check.
Desktop/backend/peer must belong to the current Windows user and login session;
other-user/session instances cannot be attached by confirmation.
Start/Restore also opens selection when a desktop is already running.

Only an existing shared loopback WebSocket chain can be attached by this version.
A private stdio instance remains visible with its compatibility reason and is left
running. Confirmation does not create a missing transport. No desktop restart,
backend termination or daemon start is performed by selection. Stop the bridge
explicitly before selecting a different native chain. The previous connection
record is backed up; operation databases and unknown outcomes are preserved.

See [design and validation](adaptive-instance-selection.md). The currently pinned
local a15 runtime is unchanged by source development; deploying this candidate is
a separate operation after AGY review and CI.

## Existing local installation

Open the existing **Slack Codex Bridge** desktop shortcut, then select Start or
Restore connection. Closing the window hides it; the shortcut or tray icon opens
it again. Exit stops its managed bridge and monitoring. Launching the shortcut
after Exit opens the status window even when the worker is stopped.

The local deployment currently depends on `.work-issues/live-20261103/`, including
its pinned environment, `quiet-fix/runtime`, operator, manifest, candidate config
and supervisor launcher. Preserve that directory until a separately validated
migration updates both the launch entry and private configuration. A directory
name beginning with `.work` does not make a live dependency disposable.

The maintained reusable launcher is `scripts/launch_supervisor.pyw`. To create or
safely migrate a shortcut, supply the verified physical paths:

```powershell
./scripts/install_supervisor_shortcut.ps1 -RunDir '<private run directory>' -Pythonw '<pinned pythonw.exe>' -Desktop
```

Use `-Bootstrap '<trusted local adapter.py>'` only with authorization for that local
credential source. The existing machine-specific entry depends on
`scripts/p1_acceptance_bootstrap.py`; its historical filename does not make it
removable. Standard application entry points accept credentials only through
process environment variables. Do not display credential files or copy their values
into arguments, reports or version control.

## Installation and identity

Build and verify the current source before installing a wheel. Use canonical
physical paths accessible outside the desktop application container. The private
manifest pins package/module/config/operator hashes, version, pythonw executable,
state directory and verified native identities. Changing source does not update
an immutable running installation; prepare a separate verified runtime and manifest.

The supervisor validates desktop/backend creation identities and the actual TCP
peer. A matching process name alone is insufficient. Wrong/unrelated desktops are
reported without force attachment or silently opening a second backend. Desktop
closure suspends the bridge; automatic recovery never repeatedly reopens it.

Unexpected worker exit may recover at most three times after identity validation,
with 2/4/8-second backoff. Intentional Stop, permission/identity/storage failures and
uncertain lifecycle outcomes do not cause automatic restarts. Stop/Exit use exact
process identities and same-handle exit proof, and do not terminate unrelated
processes or native tasks. The pinned operator retries only transient local status
file PermissionError reads, at most nine attempts separated by 25 ms.

## Recovery and retained state

For the pinned Windows desktop, **Existing desktop IPC** attaches without a native
restart or daemon start. Confirmation shows its capability limits. Open the target
chat in Codex first; this route does not resume it in another backend. The optional
non-secret `desktop.state_db` selects an absolute `state_5.sqlite` metadata location
when the native app uses a different state directory. IPC uses read-only metadata
and corroborated owner snapshots, never authentication files. An adopted IPC desktop
has no bridge-owned backend process: native stop is refused, and supervisor Stop/Exit
stops only its bridge worker. See [connection modes](adaptive-instance-selection.md).

Preserve current bindings, operations and unknown outcomes. An uncertain start,
task, upload or approval is not permission to repeat it. Read `!status`, `!op OP_ID`
and the native desktop first. Root recovery uses `!roots` and the separately
confirmed `!root-replace BINDING_ID`; it does not rerun a task.

Keep the independent a14 compatibility manifest/store and external original
installations until rollback is deliberately retired. Do not replace a live store
with an older snapshot. Git rollback, worker stop, task interruption and Slack file
removal are distinct operations. No login startup or recurring Windows task is
required. Historical P1 recovery automation remains retired.

See [verification](verification.md) and [user guidance](user-guide.zh-CN.md).
