# Content transport policy

Slack is a transport. Ordinary task text, output, questions, answers and approval
evidence are not classified or filtered by sensitive words, topic, isSecret,
format or writeOnly metadata. Supported structured questions retain their IDs,
text, options and answers. Slack escaping and explicit length limits preserve
literal meaning; visible truncation is not semantic filtering.

Identity, fixed DM, exact session/turn, binding version, original socket/generation,
expiry, schema, allowed decisions and one-use nonces still govern execution.
Missing evidence cannot authorize execution. Unsupported formats are read-only
previews requiring the original client; the bridge does not invent response schemas.

Local service credentials come from the process environment. Local credential
files/stores, unsafe paths, cross-allowlist links, invalid types and oversized files
remain protected. Literal content relay is not authorization to read credential
stores or unrelated local material. Do not place real credentials in task messages,
logs, reports or source control. See [architecture](architecture.md) and
[operations](operations.md) for retained admission and recovery boundaries.
