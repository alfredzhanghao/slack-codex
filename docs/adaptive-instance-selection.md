# Adaptive Windows instance selection

## Outcome

The supervisor can connect to an already running desktop after explicit selection
and confirmation without restarting the desktop/backend. Two distinct transports
are supported; a process name or PID alone never grants access.

| Route | Identity evidence | Capabilities |
| --- | --- | --- |
| Shared loopback WebSocket | Desktop/backend creation identities, binary pins, actual TCP peer, native initialize | Existing full native RPC adapter |
| Existing Windows desktop IPC | Desktop creation identity, configured path/version/hash/package build, named-pipe server PID, same Windows user/session, initialize | Already loaded local chats, ordinary tasks and attachments, exact-turn Stop, final answers and output retrieval |

Private desktop IPC is a version-specific compatibility adapter, not the documented
app-server control socket. It is pinned to Windows package
`OpenAI.Codex_26.1027.3384.0_x64__2p2nqsd0c76g0`, desktop file version
`156.0.8078.48` and its verified executable hash. A different package/build is not
silently accepted even if its Electron file version matches.

## Selection and persistence

Discovery enumerates desktop roots and verified loopback peers. If no shared TCP
peer exists, the pinned IPC adapter checks the existing `codex-ipc` named pipe.
Opening a handle for server identity does not register a client, start a daemon
or submit a task. An unsupported/unavailable instance remains visible with a reason.

Snapshots expire after 120 seconds. Refresh clears selection. Connect needs explicit
confirmation and rechecks creation identities, configured paths, versions/hashes,
user/session and the exact endpoint owner. Probe initialize before persistence;
revalidate again under the launcher mutex before atomic record replacement. Preserve
the previous record and leave bindings, operations and unknown outcomes unchanged.
A different running bridge worker must be stopped before switching native chains.

IPC records explicitly use `backend: null`, `port: null` and a canonical endpoint
containing only the desktop PID/creation time. They do not claim that the pipe is a
TCP listener or invent ownership of a private backend child. Native Stop/cleanup is
refused for an adopted IPC desktop. Supervisor Stop/Exit still owns only its worker.

## IPC follower behavior

The worker registers as a follower. For each chat it discovers the exact owner and
receives targeted full state snapshots from that owner. It never registers as a
thread owner or handles another client's business requests. Foreign owners, hosts,
chat IDs and unsupported stream versions cannot supply trusted state. Unknown
patches invalidate cached state and require a new full snapshot. Reconnect clears
state and revokes input ownership through the existing controller.

A read-only `state_5.sqlite` metadata query supplies the project/chat directory.
Only non-archived ID/title/cwd fields under configured project roots are selected;
no history, first-message, account or authentication fields are queried. The default
is `~/.codex/state_5.sqlite`; `desktop.state_db` can specify a different absolute
`state_5.sqlite` location. The live owner snapshot must corroborate the exact chat
and project. Normalize Windows extended drive/UNC prefixes in this adapter before
applying the unchanged project allowlist. Persisted metadata alone is not live
state, permission to resume a chat or permission to write.

A chat must already be open/resumed in the selected desktop. This adapter never
loads it into a second app-server. The existing controller and durable operation
ledger still admit each ordinary task once. Forward `turn/start` to the exact
owner with inherited thread settings. Stop uses the owner protocol's explicit
`expectedTurnId` and validates the returned interrupted ID. A missing/mismatched
write receipt or disconnect remains unknown; never retry it automatically.

Terminal snapshots do not manufacture completion events. The existing time-separated
readback logic confirms terminal turns; incomplete canonical history must be loaded
and corroborated before final output retrieval. No model task is used as a probe.

## Capability boundary

The IPC route does not expose a general native RPC proxy. Steer, approvals/structured
answers, model/effort selection, Skills, Compact, Fork and account limits remain in
the Codex desktop. In particular, its steer method lacks the bridge's exact-turn
contract, so it is not mapped. Unsupported RPC methods are rejected locally before
writing to the pipe. Slack cards hide unavailable controls and explain this scope;
the supervisor confirmation also states it. Streaming progress text is not mapped;
final answers are read from the exact terminal turn.

The shared WebSocket route keeps its existing capabilities. No private stdio handle
interception, injection, runtime patch, daemon start, native restart, startup task,
credential-file access or approval replay is involved in IPC connection.

## Verification and delivery

Tests exercise framing, allowlists, extended paths, private protocol/package pins,
owner/generation changes, write uncertainty, unsupported methods, UI capabilities,
stale selection, prior-record preservation and adopted lifecycle refusal, together
with the existing WebSocket/supervisor regressions. The standard-host full check
also includes actual Windows process/TCP fixtures, packaging and offline import.

On 2026-11-04 the source adapter connected to the current real desktop pipe and
read the exact active chat snapshot. Desktop/backend process identities and the
original connection-record hash stayed unchanged. No task, approval, upload or
Slack acceptance message was sent. This establishes the no-restart read connection;
actual Slack task/attachment delivery through IPC remains user acceptance.

Source integration and a pinned runtime update are separate. Preserve the existing
installation/store and hand the committed candidate to AGY for exact-head review,
CI, PR and merge before preparing the new installation.
