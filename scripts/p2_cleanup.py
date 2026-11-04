"""Delete only the completed, explicitly disposable P2 run's native test chats."""

import argparse
import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

from p2_acceptance import DATA, ROOT, Desktop, cfg, inject_credentials, now_ms, save, state, update, windows
from slack_codex_bridge.slack import Web


def targets(run):
    slots = {row["thread_id"] for row in run["slots"].values()}
    forks = set(run["forks"])
    owned = set(run["threads"])
    assert run["run_id"] == "6a3ddb51-6894-4892-b629-243cd3210561"
    assert len(slots) == 8 and len(forks) == 1 and len(owned) == 9
    assert owned == slots | forks and not slots & forks
    assert run["acceptance_result"]["full_p2_gate"] == "passed_controlled_scope"
    assert json.loads((DATA / "ui-final-proof.json").read_text())["status"] == "passed_controlled_p2_scope"
    return list(sorted(forks)) + list(sorted(slots))


def digest(rows):
    return hashlib.sha256(json.dumps([dict(r) for r in rows], sort_keys=True).encode()).hexdigest()


def database(config, owned, *, retire=False):
    path = config.data_dir / "bridge.sqlite3"
    conn = sqlite3.connect(path if retire else f"file:{path.as_posix()}?mode=ro", uri=not retire)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM bindings ORDER BY binding_id").fetchall()
        ours = [r for r in rows if r["codex_thread_id"] in owned]
        assert len(ours) == len(owned)
        for row in ours:
            assert row["host_id"] == config.host_id and row["owner_user_id"] == config.user_id
            assert row["slack_team_id"] == config.team_id and row["slack_channel_id"] == config.dm_id
            assert Path(row["project_path"]).resolve() == ROOT.resolve()
        ops = conn.execute("SELECT * FROM operations ORDER BY op_id").fetchall()
        assert not any(r["target_thread_id"] in owned and r["status"] in {"received", "sending"} for r in ops)
        result = {"nonowned_bindings_sha256": digest([r for r in rows if r["codex_thread_id"] not in owned]),
                  "operations_sha256": digest(ops),
                  "owned_roots": {r["codex_thread_id"]: r["slack_root_ts"] for r in ours}}
        if retire:
            backup = DATA / "bridge-before-p2-cleanup.sqlite3"
            assert not backup.exists()
            destination = sqlite3.connect(backup)
            try:
                conn.backup(destination)
            finally:
                destination.close()
            with conn:
                for row in ours:
                    assert row["disabled"] == 0
                    count = conn.execute("UPDATE bindings SET disabled=1,binding_version=binding_version+1,"
                                         "updated_at_ms=? WHERE binding_id=? AND codex_thread_id=? AND disabled=0",
                                         (now_ms(), row["binding_id"], row["codex_thread_id"])).rowcount
                    assert count == 1
            after = conn.execute("SELECT * FROM bindings ORDER BY binding_id").fetchall()
            assert digest([r for r in after if r["codex_thread_id"] not in owned]) == result["nonowned_bindings_sha256"]
            assert digest(conn.execute("SELECT * FROM operations ORDER BY op_id").fetchall()) == result["operations_sha256"]
            assert all(r["disabled"] == 1 for r in after if r["codex_thread_id"] in owned)
            result["owned_bindings_disabled"] = len(ours)
        return result
    finally:
        conn.close()


async def quiet(backend, owned):
    rows = []
    for tid in owned:
        snap = await backend.snapshot(tid)
        assert snap.trusted and snap.idle and not snap.active_turn and not snap.approval and not snap.compacting
        assert Path(snap.thread.cwd).resolve() == ROOT.resolve()
        assert snap.thread.title.startswith("P2 acceptance 20261031")
        rows.append({"thread_id": tid, "title": snap.thread.title, "idle": True})
    assert not any(r.thread_id in owned and r.status == "pending" for r in backend.pending_requests.values())
    return rows


async def listed(backend):
    ids = set()
    for archived in (False, True):
        cursor = None
        for _ in range(20):
            params = {"cwd": str(ROOT), "archived": archived, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            result = await backend.rpc.call("thread/list", params)
            ids.update(r["id"] for r in result.get("data", []))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        else:
            raise RuntimeError("thread_listing_not_exhausted")
    return ids


async def missing_by_id(backend, tid):
    """Record exact native unavailability after confirmed deletion; do not claim disk erasure."""
    request_id = backend.rpc.serial + 1
    original = backend.rpc.dispatch
    errors = []

    def dispatch(message, generation):
        if message.get("id") == request_id and isinstance(message.get("error"), dict):
            errors.append(message["error"])
        original(message, generation)

    backend.rpc.dispatch = dispatch
    try:
        try:
            await backend.rpc.call("thread/read", {"threadId": tid, "includeTurns": False})
        except Exception:
            assert len(errors) == 1, "deletion_read_error_not_identified"
            message = str(errors[0].get("message", "")).lower()
            assert any(word in message for word in (
                "not found", "no rollout", "does not exist", "deleted", "thread not loaded: " + tid))
            return {"thread_id": tid, "unavailable": True, "error_code": errors[0].get("code"),
                    "native_state": "not_loaded" if "thread not loaded" in message else "not_found"}
        raise RuntimeError("deleted_thread_still_readable")
    finally:
        backend.rpc.dispatch = original


async def main(mode):
    run = state()
    owned = targets(run)
    config = cfg()
    backend = Desktop(config, windows.verify_record(config)["endpoint"])
    await backend.connect()

    async def drain():
        while True:
            await backend.events.get()

    events = asyncio.create_task(drain())
    try:
        if mode == "finalize":
            # Native deletes already returned success. Never resend them during verification recovery.
            assert run["cleanup"]["status"] == "executing"
            deleted = run["cleanup"]["deletions"]
            assert len(deleted) == 9 and {r["thread_id"] for r in deleted} == set(owned)
            assert all(r["status"] == "confirmed" for r in deleted)
            idle = []
        else:
            idle = await quiet(backend, owned)
        before = database(config, owned)
        if mode == "preflight":
            save("cleanup-preflight.json", {"observed_ms": now_ms(), "owned": idle, **before})
            print(json.dumps({"status": "ready", "exact_idle_chats": len(idle), "writes": False}))
            return
        status = json.loads((DATA / "status.json").read_text())
        assert windows.process_identity(status["process"]["pid"]) != status["process"], "bridge_must_be_stopped"
        if mode == "execute":
            assert run["cleanup"]["status"] == "authorized_condition_met_preparing"
            # Ordinary lists may omit a fork; quiet() positively verified every exact target.
            assert set(owned) - set(run["forks"]) <= await listed(backend)
            update(lambda v: v["cleanup"].update(status="executing", started_ms=now_ms(), deletions=[]))
            for tid in owned:
                update(lambda v: v["cleanup"]["deletions"].append({"thread_id": tid, "status": "sending_once"}))
                try:
                    result = await backend.rpc.call("thread/delete", {"threadId": tid}, write=True)
                    assert result == {}
                except Exception:
                    update(lambda v: v["cleanup"]["deletions"][-1].update(status="unknown_do_not_retry"))
                    raise
                update(lambda v: v["cleanup"]["deletions"][-1].update(status="confirmed"))
        remaining = await listed(backend)
        assert not set(owned) & remaining
        not_found = [await missing_by_id(backend, tid) for tid in owned]
        retired = database(config, owned, retire=True)
        assert retired["operations_sha256"] == before["operations_sha256"]
        assert retired["nonowned_bindings_sha256"] == before["nonowned_bindings_sha256"]
        save("cleanup-result.json", {"status": "passed", "observed_ms": now_ms(), "native_deleted": owned,
                                    "native_active_and_archived_lists_absent": True,
                                    "exact_id_unavailable": not_found, **retired})
        update(lambda v: v["cleanup"].update(status="completed", native_deleted_count=len(owned),
                                             owned_bindings_disabled=len(owned), completed_ms=now_ms()))
        # Retire controls on only the exact disposable roots; retain Slack replies as evidence.
        inject_credentials(config)
        _, bot = config.credentials()
        web = Web(config, bot)
        messages = []
        try:
            for tid in owned:
                text = "P2 acceptance completed. This test chat was deleted; controls retired. Chat: " + tid
                try:
                    await web.update(before["owned_roots"][tid], text,
                                     [{"type": "section", "text": {"type": "plain_text", "text": text}}])
                    messages.append({"thread_id": tid, "status": "controls_retired"})
                except Exception:
                    messages.append({"thread_id": tid, "status": "update_unconfirmed_no_retry"})
        finally:
            if web.client.session:
                await web.client.session.close()
        save("cleanup-slack-controls.json", messages)
        print(json.dumps({"status": "completed", "native_chats_deleted": len(owned),
                          "bindings_disabled": len(owned), "operations_preserved": True,
                          "nonowned_bindings_preserved": True, "slack_controls": messages}))
    finally:
        events.cancel()
        await asyncio.gather(events, return_exceptions=True)
        await backend.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["preflight", "execute", "finalize"])
    asyncio.run(main(parser.parse_args().mode))
