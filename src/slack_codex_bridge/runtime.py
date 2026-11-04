"""Fixed recovery guidance; connection retries never replay business operations."""

import math
from dataclasses import dataclass

from .model import BridgeError


@dataclass(frozen=True)
class Recovery:
    category: str
    retry: bool
    action: str


def recovery(error: BridgeError) -> Recovery:
    code = error.code
    if code == "database_unavailable":
        return Recovery("storage", False, "Check the local store and disk before restarting the bridge.")
    if code in {"credentials_not_in_environment", "slack_read_invalid_auth", "slack_write_invalid_auth",
                "slack_read_token_revoked", "slack_write_token_revoked", "slack_read_token_expired",
                "slack_write_token_expired", "slack_read_not_authed", "slack_write_not_authed",
                "slack_read_account_inactive", "slack_write_account_inactive"}:
        return Recovery("credentials", False, "Repair the local credential injection or App installation, then restart.")
    if code in {"slack_missing_scope", "slack_write_missing_scope", "slack_read_not_allowed_token_type",
                "slack_write_not_allowed_token_type", "slack_read_no_permission", "slack_write_no_permission",
                "slack_read_channel_not_found", "slack_write_channel_not_found",
                "slack_read_not_in_channel", "slack_write_not_in_channel"}:
        return Recovery("permissions", False, "Verify the configured App, DM and required permissions, then restart.")
    if code in {"persisted_root_app_mismatch", "slack_read_thread_not_found", "slack_read_message_not_found",
                "slack_write_thread_not_found", "slack_write_message_not_found"}:
        return Recovery("root", False, "Inspect the exact bound root locally; do not automatically create another root.")
    if code in {"slack_account_mismatch", "slack_app_mismatch", "slack_dm_owner_mismatch",
                "slack_dm_owner_evidence_unavailable", "persisted_binding_identity_changed",
                "persisted_binding_outside_allowlist",
                "unverified_backend_endpoint", "controlled_session_changed_restart_bridge",
                "version_mismatch_writes_disabled", "version_check_failed", "session_version_changed",
                "desktop_path_changed", "backend_path_changed", "backend_process_identity_changed",
                "binary_hash_changed", "desktop_process_identity_changed", "session_identity_mismatch",
                "controlled_session_not_verified", "controlled_session_record_unavailable",
                "desktop_ipc_identity_changed", "desktop_ipc_protocol_unverified"}:
        return Recovery("identity", False, "Verify the controlled desktop/backend identity and configuration before restarting.")
    if code == "slack_rate_limited":
        return Recovery("rate_limit", True, "Wait for Retry-After; old business submissions are not replayed.")
    return Recovery("connection", True, "Reconnect and synchronize; input returns to desktop mode.")


def retry_after(headers) -> float:
    """Slack headers may use a different case or a one-element list in the SDK."""
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), 60)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else 60
    try:
        delay = float(value)
        return max(1, delay) if math.isfinite(delay) and delay >= 0 else 60
    except (TypeError, ValueError, OverflowError):
        return 60


def reconnect_delay(error: BridgeError, failures: int) -> float:
    return max(min(30, 2 ** min(failures, 5)), error.retry_after or 0)
