"""Root rotation identity checks, independent of Slack transport."""

import json
from dataclasses import replace

from .model import BridgeError

ROOT_ERRORS = {"slack_read_thread_not_found", "slack_read_message_not_found",
               "slack_write_thread_not_found", "slack_write_message_not_found"}
IDENTITY = ("binding_id", "slack_team_id", "slack_channel_id", "owner_user_id", "host_id",
            "codex_thread_id", "project_path", "environment_key")


def review_binding(op, binding, rotations):
    """An audited UI relocation may carry review access, never approval authority."""
    if not op or op.binding_version == binding.binding_version:
        return binding
    if (op.binding_id != binding.binding_id or not 0 < binding.binding_version - op.binding_version <= 32):
        raise BridgeError("root_lineage_unverified")
    expected_root = None
    for version in range(op.binding_version, binding.binding_version):
        matches = []
        for rotation in rotations:
            try:
                audit = json.loads(rotation.result_json or "{}")
                if audit.get("binding_rotated") is not True:
                    continue  # A refused preview/submission never advanced a version.
                before = json.loads(rotation.payload_json)["data"]["binding"]
                if (rotation.binding_version == version and rotation.kind == "root_replace"
                        and rotation.binding_id == binding.binding_id
                        and rotation.target_thread_id == binding.codex_thread_id
                        and audit.get("binding_rotated") is True):
                    matches.append((before, audit))
            except (KeyError, TypeError, ValueError):
                raise BridgeError("root_lineage_unverified") from None
        if len(matches) != 1:
            raise BridgeError("root_lineage_unverified")
        before, audit = matches[0]
        if (any(before.get(key) != getattr(binding, key) for key in IDENTITY)
                or before.get("disabled") or before.get("binding_version") != version
                or audit.get("from_version") != version or audit.get("to_version") != version + 1
                or audit.get("previous_root") != before.get("slack_root_ts")
                or not isinstance(audit.get("root_ts"), str)
                or (expected_root is not None and before.get("slack_root_ts") != expected_root)):
            raise BridgeError("root_lineage_unverified")
        expected_root = audit["root_ts"]
    if expected_root != binding.slack_root_ts:
        raise BridgeError("root_lineage_unverified")
    return replace(binding, binding_version=op.binding_version)
