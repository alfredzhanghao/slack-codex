# Output delivery and language settings

## Behavior

On a newly completed, Slack-tracked task, inspect at most 20 explicit native
output references. Never scan the project for files. Send documents/images smaller
than 30 MB automatically by default. MB means 1,000,000 bytes; equality requires
confirmation. Videos always require confirmation. Other supported output types
also require confirmation. Turning automatic delivery off requires confirmation
for every output. A confirmation button sends that single file directly into the
original session thread, without a modal or another model task.

Reuse durable artifact operations and the existing one-attempt Slack upload
pipeline. Persist the parent delivery attempt before effects. Bind each offer to
its task, root, binding version, connection epoch and file identity. Revalidate
identity, size, policy and project scope before uploading. A changed file needs a
fresh manual retrieval. Repeated clicks and reconnects never replay an uncertain
upload. Unknown historical operations remain untouched.

The default outbound ceiling is 100 MB, configurable from 1 to 100 MB. Files over
the ceiling are listed as unavailable, never silently discarded. Inbound file
types and limits remain independent. Local credential paths remain forbidden.

## Configuration

The Windows supervisor exposes language (English / 中文), automatic document/image
delivery, automatic threshold, and outbound ceiling. Save a separate, non-secret
`bridge-preferences.json` inside the configured data directory, atomically, without
editing the pinned deployment configuration. Both the manager and worker read
these preferences; changes apply to subsequent UI renders and file decisions
without restarting native Codex. Pending file decisions must revalidate policy.
Corrupt preference data blocks delivery rather than enabling automatic uploads.

Deployment JSON can supply initial defaults without a preference file:

```json
{
  "ui": {"language": "en"},
  "artifacts": {
    "auto_send": true,
    "auto_send_max_bytes": 30000000,
    "max_file_bytes": 100000000
  }
}
```

Saved supervisor preferences take precedence. Existing explicitly configured size
ceilings are preserved until the user saves a different preference. The automatic
threshold can exceed that ceiling; the stricter outbound ceiling always wins.
Confirmation controls expire after ten minutes and are invalidated on reconnect;
use explicit retrieval to request a fresh decision instead of replaying one.
Uploads use Slack's documented [allocation](https://docs.slack.dev/reference/methods/files.getUploadURLExternal/)
and [completion](https://docs.slack.dev/reference/methods/files.completeUploadExternal/)
endpoints, retaining the original session root as `thread_ts`.

Language applies to bridge-authored Windows and Slack UI. Model replies, file
names/contents, chat/project names, commands and diagnostic identifiers retain
their original values. The model response delivery explicitly bypasses translation.

## Verification

Cover the strict threshold boundary, video confirmation, disabled auto delivery,
file changes, duplicate clicks, binding/epoch changes, uncertain uploads and
restart non-replay. Verify atomic preference persistence and invalid settings,
language propagation and literal model replies. Run existing regression, lint,
compile and package checks. Local checks do not claim live Slack acceptance.
No running bridge/native process is changed by developing this feature.
