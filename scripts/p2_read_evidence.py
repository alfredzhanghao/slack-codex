"""Read only P2-owned live evidence; no historical chat body or credential output."""

import argparse
import asyncio
import json
import sqlite3

from p2_acceptance import DATA, cfg, inject_credentials, save, state
from slack_codex_bridge.config import SECRET_PATTERN
from slack_codex_bridge.model import BridgeError
from slack_codex_bridge.slack import Web


async def main(args):
    config = cfg()
    run = state()
    status = json.loads((DATA / "status.json").read_text())
    allowed = set(run["threads"])
    bindings = [b for b in status["bindings"] if b["thread_id"] in allowed]
    if args.mode == "status":
        print(json.dumps({k: status[k] for k in ("observed_ms", "ready", "version", "healthy", "fault",
                                                "backend_generation", "requests")} | {"bindings": bindings}))
        return
    if args.mode == "operations":
        conn = sqlite3.connect(f"file:{(config.data_dir / 'bridge.sqlite3').as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT op_id,source_key,kind,target_thread_id,target_turn_id,status,backend_ref_json,"
                "result_json,error_reason FROM operations WHERE target_thread_id IN ("
                + ",".join("?" for _ in allowed) + ") ORDER BY created_at_ms", tuple(allowed)).fetchall()
            result = [dict(r) for r in rows]
            save("operations.json", result)
            print(json.dumps(result))
        finally:
            conn.close()
        return
    tid = run["slots"][args.slot]["thread_id"]
    binding = next((b for b in bindings if b["thread_id"] == tid), None)
    if not binding:
        raise BridgeError("test_chat_not_bound_yet")
    inject_credentials(config)
    _, bot = config.credentials()
    web = Web(config, bot)
    try:
        result = await web.call("conversations.replies", channel=config.dm_id, ts=binding["root_ts"], limit=100)
        messages = [{k: row.get(k) for k in ("ts", "user", "bot_id", "text", "blocks")}
                    for row in result.get("messages", [])]
        if SECRET_PATTERN.search(json.dumps(messages)):
            raise BridgeError("sensitive_fixture_output_refused")
        save("slack-" + args.slot + ".json", messages)
        print(json.dumps(messages, ensure_ascii=False))
    finally:
        if web.client.session:
            await web.client.session.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["status", "operations", "slack"])
    p.add_argument("slot", nargs="?", default="base")
    try:
        asyncio.run(main(p.parse_args()))
    except BridgeError as error:
        print(json.dumps({"status": "unproven", "code": error.code}))
        raise SystemExit(1)
