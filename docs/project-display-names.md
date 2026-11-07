# Desktop project names in Slack

Project selection uses the names saved by the Codex desktop, with directory names
as the fallback. Full project paths are not displayed in the project picker,
conversation picker or selected-project heading. Session cards also use the cached
display name. Native project/chat names remain literal under either UI language.

The adapter reads `.codex-global-state.json` next to the explicitly configured
`desktop.state_db`. Only `local-projects` names/root paths and legacy
`electron-workspace-root-labels` are extracted; other top-level values are skipped
without JSON-decoding them. Native authentication files, credential stores and
conversation history files are not opened. Reads are bounded to 8 MiB with 512 KiB per selected table, verified by an
opened-file path check, and cached by file identity/size/modification time.

Names refresh on connection and whenever the project or conversation picker opens,
so desktop renames are reflected at the next selection. If metadata is missing or
invalid, use directory basenames. Equal labels receive deterministic numeric
suffixes that avoid collisions with existing names. Names never become routing
keys: button capabilities retain the exact original allowlisted directory and
selected native thread ID. No directories are automatically added or enabled.

Verification covers live metadata/name agreement, fallback/corruption, selective
field decoding, changed-handle rejection, duplicate labels, rename refresh,
literal localization and exact routing. This change does not deploy/restart a
bridge or modify native desktop project settings.
