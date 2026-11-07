# Session card freshness, localization and public progress

Reopening a known Slack chat must refresh its existing root before posting the
entry link. Use current saved language for both root and entry. Never create a
replacement root, transfer input or start native work as part of navigation.
An unloaded/unavailable native chat still allows opening Slack history, but its
card must show unknown/offline status rather than a previously cached idle state.
Per-chat read failures must not prevent other loaded chats from updating.

Working/idle presentation requires a fresh, trusted snapshot. Active native status
with a missing turn ID still means working; it does not enable exact-turn controls.
Protected pending turns remain protected until existing terminal corroboration
passes. Presentation must not invent start/completion events or release a lock.

For the pinned desktop IPC protocol, expose only the most recent public
`agentMessage` with phase `commentary` from the exact active in-progress turn in a
fresh verified owner snapshot. Never extract reasoning, commands, tool output,
approval rationale or final answers as progress. Show this text literally under a
localized Progress label, bound to its own chat/turn. Old or idle progress is hidden.
Normal WebSocket progress continues through its existing events.

Verify reopening an existing English root under Chinese preferences, unavailable
chat history, fresh active/idle/stale status, current-turn commentary and literal
multiline progress. Preserve the original routing, nonces, unknowns and native
completion checks. No live acceptance or runtime replacement is part of source
repair; the existing deployment remains pinned until separately authorized.
