# Current architecture — 0.3.0a18 candidate

The Python package in `src/slack_codex_bridge/` is the application. Tests cover
native RPC/Slack protocol fixtures and Windows lifecycle primitives. Historical
phase labels in test/helper filenames identify regression provenance, not separate
applications. Keep helpers imported by those tests and the local launch boundary.

## Routing and admission

The fixed authenticated Slack DM selects an allowlisted project and a native main
chat. One durable binding maps that chat to one verified Slack root/reply thread.
Repeated/concurrent opens reuse the mapping. Missing/uncertain roots require
explicit recovery; top-level text/files never target a last-used chat implicitly.
Fork is explicit. Thread permalinks must match the expected channel and root.

Ordinary tasks require fresh Slack ownership and an idle native target. Ownership
is revoked on reconnect/restart/wake. Steer/interrupt capture an exact active turn;
old epochs/targets are refused. Native single-writer conflicts remain refusals.
Approval schemas, original socket/generation, one-use nonces and expiry are retained.

## Persistence and uncertainty

SQLite admission is durable before event acknowledgement. Operations transition
through received/sending/accepted/rejected/unknown; task completion is independent
of backend acceptance. Restart rejects received operations, marks sending unknown
and never replays them. An unknown result is retained until exact evidence or an
explicit audited review/new request; matching text is not completion proof.

Long native history responses remain bounded at 32 MiB. Terminal recovery requires
exact-turn corroboration, not an assumed result. Slack notifications and file
uploads are not automatically retried after uncertain delivery.

## Attachments and artifacts

Slack files use authenticated metadata, bounded HTTPS downloads without credential
redirects, isolated staging and all-or-none admission. Inputs are bound to the exact
session/version/epoch. Attachment-only events stage context for one subsequent
instruction; disconnect/restart revokes it. Native images use localImage, documents
use actual local task-environment paths. File count/type/size and local credential
path protections remain enforced.

Retrieval resolves known outputs in the same session. Unique matches upload current
bytes; ambiguity opens a chooser. It does not rerun the producing model task.

Newly completed Slack-tracked tasks apply the [output delivery policy](output-delivery-settings.md)
to explicit references only. Documents/images below the threshold auto-upload;
videos and larger files require a one-use direct confirmation bound to the exact
root, epoch, binding version, task and file identity. All sends use durable artifact
operations and the existing one-attempt upload pipeline. A separate atomic,
non-secret preference file controls policy and bridge-owned English/Chinese UI
without changing pinned deployment files. Model replies bypass translation.

## Operations and presentation

The on-demand supervisor uses the pinned operator, exact identities and actual TCP
peer, with bounded unexpected-worker recovery and explicit Stop/Exit. See
[operations](operations.md). Normal answers use Slack Markdown, while diagnostic
receipts remain in explicit status/operation inspection. Model/task content is
relayed literally under the [transport policy](content-transport-policy.md).

Existing desktop selection is read-only until explicit confirmation. A short-lived
snapshot binds the exact desktop/backend creation identities and actual loopback
TCP peer. Confirmation rechecks versions/hashes and refuses to switch a different
running worker. It backs up only the previous native connection record; it never
terminates native processes or replays operations. A pinned Windows desktop can
also use its existing coordination pipe: verify the pipe server's exact desktop
identity and follow the chat's owner instead of launching another app-server.
This route has an explicit capability boundary; unsupported private transports
remain non-connectable. See [instance selection](adaptive-instance-selection.md).


## Session presentation freshness

Entry navigation refreshes a reused root under the current language before
posting the thread link. Unavailable per-chat state renders unknown instead of
retaining an old online/idle card. Fresh native active status determines working
presentation, while exact-turn controls and terminal corroboration stay separate.
IPC snapshots expose only current-turn public commentary as literal progress;
reasoning/tool output and older turns are excluded. See [session display](session-display.md).


## Project display metadata

Project labels are read from the desktop UI metadata next to the configured
state database and projected onto exact existing allowlist entries. Pickers refresh
the cache, show names rather than directories, and preserve literal native names
under localization. Button capabilities retain canonical paths and native thread
IDs; presentation never expands admission. See [project display names](project-display-names.md).
