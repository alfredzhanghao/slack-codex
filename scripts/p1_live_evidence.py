"""Read-only test-thread/database correlation; never exposes stored input payloads."""

import argparse
import asyncio
import json
import sqlite3

from slack_codex_bridge import windows
from slack_codex_bridge.config import load
from slack_codex_bridge.desktop import Desktop
from slack_codex_bridge.model import now_ms

from p1_acceptance_supervisor import DATA, ROOT, prepare, write_json


async def read(slot):
    cfg = load(ROOT / "bridge.local.json")
    state = prepare()
    tid = state["chat_preparation"][slot]["thread_id"]
    assert tid in state["test_threads"]
    record = windows.verify_record(cfg)
    backend = Desktop(cfg, record["endpoint"])
    await backend.connect()
    try:
        snapshot = await backend.snapshot(tid)
        turns = await backend.rpc.call("thread/turns/list", {
            "threadId": tid, "limit": 20, "sortDirection": "desc", "itemsView": "full"})
        with sqlite3.connect((cfg.data_dir / "bridge.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            operations = [dict(row) for row in db.execute(
                "SELECT op_id,source_key,kind,target_turn_id,status,backend_ref_json,result_json,error_reason "
                "FROM operations WHERE target_thread_id=? ORDER BY created_at_ms", (tid,))]
        evidence = {"run_id": state["run_id"], "observed_ms": now_ms(), "thread_id": tid,
            "idle": snapshot.idle, "trusted": snapshot.trusted, "active_turn": snapshot.active_turn,
            "operations": operations, "turns": [{"id": row["id"], "status": row["status"],
                "item_types": [item.get("type") for item in row.get("items", [])],
                "final": [item.get("text") for item in row.get("items", [])
                    if item.get("type") == "agentMessage" and item.get("phase") in {None, "final_answer"}]}
                for row in turns.get("data", [])]}
        write_json(DATA / f"live-{slot}.json", evidence)
        print(json.dumps(evidence, ensure_ascii=False))
    finally:
        await backend.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("slot", choices=["primary", "secondary", "wait", "controls", "approval", "gui"])
    asyncio.run(read(parser.parse_args().slot))
