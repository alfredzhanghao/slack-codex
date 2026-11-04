"""Exact-operation review and replacement values; no transport or automatic retry."""

import hashlib
import json
from dataclasses import asdict

from .model import BridgeError

REPLACEABLE = {"turn_start", "steer", "interrupt", "fork", "artifact_upload"}


def fingerprint(op):
    return hashlib.sha256(json.dumps(asdict(op), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def checked_unknown(op, binding, expected, *, acknowledged=False):
    if (not op or op.binding_id != binding.binding_id or op.target_thread_id != binding.codex_thread_id
            or op.binding_version != binding.binding_version or binding.disabled):
        raise BridgeError("operation_not_found_in_current_binding")
    if fingerprint(op) != expected:
        raise BridgeError("operation_changed_refresh_review")
    if op.status != "unknown" or op.unknown_review_state != ("acknowledged" if acknowledged else "pending"):
        raise BridgeError("operation_not_pending_review" if not acknowledged else "replacement_requires_acknowledged_unknown")
    return op


def replacement(op):
    if op.kind not in REPLACEABLE:
        raise BridgeError("use_fresh_native_control_for_this_operation")
    payload = json.loads(op.payload_json)
    old = payload.get("data", {})
    data = {}
    if op.kind == "turn_start":
        if old.get("files"):
            data["files"] = old["files"]
        selector = old.get("skill") or old.get("skill_selector")
        if selector:
            data["skill_selector"] = selector
    elif op.kind == "artifact_upload":
        if old.get("paths"):
            raise BridgeError("review_individual_files_do_not_replay_batch")
        data["path"] = old["path"]
    return op.kind, payload.get("text", ""), data, op.target_turn_id
