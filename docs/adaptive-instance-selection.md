# Adaptive Windows instance selection

## Problem and intended behavior

A reopened desktop can use a different native backend while the bridge retains
the old verified process record. Start/Restore previously rejected it without a
selection path. The supervisor must discover existing desktops on startup, show
their identity and connection capability, and let the user select and confirm a
compatible instance without restarting or terminating native processes.

## Scope

- Enumerate process identities and owned loopback TCP connections, never process
  command lines, environment blocks, credential files or task payloads.
- Require the desktop, backend and actual peer to belong to the current Windows
  user and login session. Compare OS token-owner metadata locally without exposing
  SID values; an unreadable owner is not permission to attach.
- Show matching desktop roots with PID/path/version, backend/port when an actual
  desktop-to-backend connection exists, and an actionable compatibility reason.
- A list item is a short-lived snapshot. Refresh clears selection; Connect needs
  an explicit confirmation and revalidates creation identity, configured executable
  paths, versions/hashes, listening owner and the actual desktop TCP peer.
- Before changing the record, perform only the native initialize handshake, then
  revalidate the snapshot again. A non-RPC listener or a changed peer leaves the
  previous record intact. No task, approval or model request is used as a probe.
- Preserve the previous identity record before atomic replacement. Never modify
  the operation database, replay work or stop a native process during attachment.
- Refuse changing the native chain while a different bridge worker is running.
  Start/Restore with an existing desktop opens selection instead of launching or
  restarting a desktop. With no desktop, the existing explicit start path remains.
- Consume a selected snapshot before any effect. Expired, changed, unreadable,
  ambiguous or unsupported instances require refresh/repair, not a guessed target.

## Transport boundary

This increment attaches already shared loopback WebSocket instances. The existing
RPC transport is WebSocket; a private stdio process cannot be attached merely
because its PID exists. An unavailable control socket is not created implicitly.
No daemon start, startup registration, native restart, private stdio interception
or identity-check bypass is included. These require separate transport/lifecycle
work. Display non-connectable desktop instances rather than hiding them or
claiming that selection alone makes their transport compatible.

The 2026-11-04 host diagnosis found the original backend listening on loopback,
but no established peer from the current desktop. The default control socket was
absent and a bounded official proxy initialize probe produced no response. This
does not prove every possible private native transport is unavailable.

## Validation and delivery

Regression coverage must include multiple desktops/ports, selected exact peer,
PID reuse, stale selection, changed binary/port/peer, cancelled selection, running
worker refusal, no process lifecycle calls on detection/selection, and retained
legacy start/recovery behavior. Run focused and full existing checks. Keep the
installed immutable runtime unchanged; hand the committed source to AGY for
review, PR, current CI and merge. Deployment/live adoption is a separate step.

## Implementation result

Source candidate version: 0.3.0a16. Full supported-host verification passed 705
tests with zero failures/errors/skips, plus lint/build/package/offline-install
checks. Synthetic native UI selection, Cancel, explicit confirmation and Exit
were exercised; a real Windows process/TCP fixture retained both process identities.
The native-protocol fixture confirmed initialize/initialized only, and failure or
peer change before persistence left the original record untouched.

The restricted local execution sandbox could not observe the real TCP fixture's
peer; the same test passed on the standard Windows host. No test was weakened or
excluded to hide that environment difference. The deployed a15 runtime/entry hashes
remained unchanged. Current real discovery reports no_shared_endpoint, so this
source implementation does not claim to have attached the currently private
Codex desktop. AGY review, current PR CI and a separately authorized deployment
remain subsequent steps.
