# Security Policy

## Supported Versions

Only the latest release tag or `main` branch is supported for security updates.

| Version | Supported |
| --- | --- |
| 0.3.x | Yes |
| < 0.3 | No |

## Reporting a Vulnerability

If you discover a security vulnerability in this project:

1. **Do not open a public issue.**
2. Send an email to **alfred.zhanghao@gmail.com** with the subject `[Security] slack-codex: <brief summary>`.
3. Provide:
   - A description of the issue and potential impact.
   - Exact steps or minimal proof of concept to reproduce.
   - Any proposed fix or mitigation if available.

You will receive an initial response within 48 hours. Please allow reasonable time for investigation and patch deployment before any public disclosure.

## Security Architecture & Invariants

This bridge is designed with defense-in-depth principles:
- **No stored credentials**: Tokens are read exclusively from process environment variables (`SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`) and are never written to configuration files, logs, or reports.
- **Process & TCP identity**: Desktop and backend connections verify native process identity and local TCP endpoints, rejecting arbitrary or untrusted process attachments.
- **Single owner boundary**: All interactions are restricted to the configured Slack owner DM channel.
- **SQLite ledger**: Operations are persisted before acknowledgement, and uncertain outcomes are recorded as `unknown` rather than replayed.
