"""Read-only live readiness checks. Never loads or prints credential material."""

import asyncio
import hashlib
import json
import os
from pathlib import Path

from slack_codex_bridge import windows
from slack_codex_bridge.config import load
from slack_codex_bridge.desktop import Desktop
from slack_codex_bridge.model import BridgeError, now_ms
from slack_codex_bridge.quota import quota_text

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".work-p2" / "acceptance"


async def preflight():
    cfg = load(ROOT / "bridge.local.json")
    record = windows.verify_record(cfg)
    result = {
        "observed_ms": now_ms(), "status": "preflight_incomplete",
        "authorization": "user_grant_20261031_p2_unattended_existing_dm_max8_chats_1_fork",
        "desktop_backend_unchanged": True, "controlled_session": record,
        "credential_environment_present": {name: bool(os.environ.get(name)) for name in
                                           (cfg.app_token_env, cfg.bot_token_env)},
        "methods": [], "test_chats_created": [], "forks_created": [],
        "slack_posts": 0, "model_turns": 0, "approval_replies": 0,
        "runtime_switched": False, "credential_files_read": False,
    }
    directory = ROOT / "dist" / "p2-20261031-candidate"
    expected = dict(line.split("  ", 1)[::-1] for line in
                    (directory / "SHA256SUMS").read_text(encoding="ascii").splitlines() if line.strip())
    result["candidate_hashes_verified"] = all(
        (directory / name).parent == directory
        and hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest
        for name, digest in expected.items())
    if not result["candidate_hashes_verified"]:
        raise BridgeError("p2_candidate_hash_mismatch")
    backend = Desktop(cfg, record["endpoint"])
    try:
        await backend.connect()
        result["methods"].extend(["initialize", "initialized"])
        try:
            await backend.refresh_limits(force=True)
            result["methods"].append("account/rateLimits/read")
            result["quota_display"] = quota_text(backend, cfg)
        except BridgeError as error:
            result["quota_error"] = error.code
        try:
            value = await backend.rpc.call("thread/list", {"limit": 100, "sortKey": "updated_at",
                                                            "sortDirection": "desc"})
            result["methods"].append("thread/list")
            rows = [r for r in value.get("data", []) if cfg.allows(r.get("cwd", ""))]
            result["allowed_recent_threads"] = len(rows)
            environments = [r.get("environments") for r in rows if r.get("environments")]
            result["explicit_thread_environments"] = environments
        except BridgeError as error:
            result["thread_list_error"] = error.code
        try:
            value = await backend.rpc.call("skills/list", {"cwds": [str(cfg.projects[0])], "forceReload": True})
            result["methods"].append("skills/list")
            rows = [s for entry in value.get("data", []) for s in entry.get("skills", [])
                    if s.get("name") == "karpathy-guidelines"]
            result["target_skill"] = [{k: r.get(k) for k in ("name", "path", "enabled")} for r in rows]
        except BridgeError as error:
            result["skills_error"] = error.code
    finally:
        await backend.close()  # Only this read-only client's socket, never the server.
    current = windows.verify_record(cfg)
    result["desktop_backend_unchanged"] = all(current.get(k) == record.get(k) for k in ("backend", "desktop", "endpoint"))
    result["status"] = ("blocked_missing_credential_environment" if
                        not all(result["credential_environment_present"].values()) else "preflight_ready")
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "preflight.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(preflight())
