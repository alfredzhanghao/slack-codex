# Session usability improvements

## Scope and release boundary

Implement the seven items handed to this project on 2026-11-06, preserving the
a22 baseline. Source changes and local checks do not deploy a runtime, send live
tasks/files, restart Codex, alter native history, or authorize remote integration.

## Design and acceptance

1. Normalize text inputs at the desktop IPC start boundary with `text_elements: []`
   when absent. Validate existing lists, preserve values and caller objects, and
   retain exact owner/version/write/unknown-outcome checks. Other transports keep
   their own wire formats. This prevents new incompatible input; it does not repair
   historical conversations or explain native delete failures.
2. Overlay a bounded, explicitly located `session_index.jsonl` only onto already
   scoped native metadata. Latest valid name wins; names stay literal. Preserve
   known names independently of live status and fall back safely on invalid files.
3. Carry native Unix update timestamps into thread metadata. Globally deduplicate
   and sort before pagination. Project activity is the maximum visible main-chat
   activity within that exact configured project; empty/missing values sort last.
   Listing failures are reported rather than treated as an empty history.
4. Treat attribution-only output shares from configured integrations in desktop
   input mode as delivered outputs, not new inputs. Retain real integration tasks
   and Slack-mode attachment staging, event authorization, ACK-first behavior,
   deduplication and unknown-outcome protection. Explain genuine ownership refusal
   in normal language. No filename/content keyword filters are added.
5. Skip unsupported quota requests. Ordinary refresh is quiet; explicit `!quota`
   explains unavailable capability. Supported transports retain real quota data.
   Quota capability does not overwrite unrelated faults or imply exhaustion.
6. Remove self-navigation from permanent session cards. Reuse verified roots and
   deduplicate navigation feedback. Native Slack replies are the normal entry;
   retain a validated, explicitly labelled web fallback for cross-thread access.
   No unverified `slack://` thread route or DM broadcast is introduced. Target
   phone navigation remains a separate user acceptance item.
7. Preserve exact-turn App Server Steer. The inspected desktop IPC follower method
   does not accept an expected turn ID and can choose/retry a different active
   target. Keep that route disabled, explain the limitation on active cards, and
   reject direct attempts without starting another task. Do not claim IPC Steer
   is restored without an atomic target guarantee.

## Verification

Extend existing protocol, input, quota, metadata, navigation and session tests;
exercise hostile metadata, missing values, pagination, reconnect, lost receipts,
and stale target handling. Run `scripts/check_open_issues.ps1` on Windows.
Hosted/unit checks do not prove mobile navigation or live model/file acceptance.

Slack references: [URL buttons](https://docs.slack.dev/reference/block-kit/block-elements/button-element/),
[deep links](https://docs.slack.dev/interactivity/deep-linking/),
[thread replies](https://docs.slack.dev/reference/methods/chat.postMessage/).
