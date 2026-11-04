# Current architecture — 0.3.0a16 candidate

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
terminates native processes or replays operations. Private/unavailable transports
remain non-connectable. See [instance selection](adaptive-instance-selection.md).
