import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from aiohttp import web

from slack_codex_bridge.desktop import Desktop, RPC
from slack_codex_bridge.model import Action, BridgeError, new_id


@pytest.mark.parametrize("wrong_id,outside", [(False, False), (True, False), (False, True)])
async def test_direct_thread_lookup_enforces_identity_and_directory(cfg, tmp_path, wrong_id, outside):
    desktop = Desktop(cfg, "ws://127.0.0.1:1")
    tid = new_id()
    other = tmp_path / "outside"
    other.mkdir()
    calls = []

    async def call(method, params):
        calls.append((method, params))
        return {"thread": {"id": new_id() if wrong_id else tid,
            "cwd": str(other if outside else cfg.projects[0]), "preview": "P1 GUI"}}

    desktop.rpc = SimpleNamespace(call=call)
    if wrong_id or outside:
        with pytest.raises(BridgeError, match="thread_identity_mismatch|thread_outside_allowlist"):
            await desktop.get_thread(tid)
    else:
        assert (await desktop.get_thread(tid)).id == tid
    assert calls == [("thread/read", {"threadId": tid, "includeTurns": False})]


@asynccontextmanager
async def rpc_server(handler):
    async def route(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for frame in ws:
            message = frame.json()
            if message.get("method") == "initialize":
                await ws.send_json({"id": message["id"], "result": {}})
            elif message.get("method") != "initialized":
                await handler(ws, message)
        return ws

    app = web.Application()
    app.router.add_get("/", route)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


async def test_bidirectional_id_collision_and_string_id_are_distinct():
    events = []

    async def handler(ws, m):
        await ws.send_json({"id": m["id"], "method": "item/commandExecution/requestApproval",
                            "params": {"threadId": "t", "turnId": "u"}})
        await ws.send_json({"id": str(m["id"]), "result": {"wrong": True}})
        await ws.send_json({"id": m["id"], "result": {"correct": True}})
    async with rpc_server(handler) as endpoint:
        rpc = RPC(endpoint, 1, lambda m, g: events.append(m))
        await rpc.connect()
        assert await rpc.call("read", {}) == {"correct": True}
        assert events[0]["method"].endswith("requestApproval")
        await rpc.close()


async def test_timeout_and_late_response_never_retry_write():
    calls = []
    allow_late = asyncio.Event()
    sent_late = asyncio.Event()

    async def handler(ws, m):
        calls.append(m)
        await allow_late.wait()
        await ws.send_json({"id": m["id"], "result": {"accepted": True}})
        sent_late.set()
    async with rpc_server(handler) as endpoint:
        rpc = RPC(endpoint, 1, lambda *_: None)
        await rpc.connect()
        rpc.timeout = 0.02
        with pytest.raises(BridgeError) as error:
            await rpc.call("turn/start", {}, write=True)
        assert error.value.unknown
        allow_late.set()
        await asyncio.wait_for(sent_late.wait(), 1)
        assert len(calls) == 1 and not rpc.pending
        await rpc.close()


async def test_disconnect_inflight_write_is_unknown():
    async def handler(ws, m):
        await ws.close()
    async with rpc_server(handler) as endpoint:
        rpc = RPC(endpoint, 1, lambda *_: None)
        await rpc.connect()
        with pytest.raises(BridgeError) as error:
            await rpc.call("turn/start", {}, write=True)
        assert error.value.unknown
        await rpc.close()


async def test_failure_in_after_write_probe_remains_unknown_and_never_retries():
    calls = []

    async def handler(ws, m):
        calls.append(m)
        await ws.send_json({"id": m["id"], "result": {"accepted": True}})

    async def fail_probe(*_):
        raise RuntimeError("synthetic instrumentation failure")
    async with rpc_server(handler) as endpoint:
        rpc = RPC(endpoint, 1, lambda *_: None)
        await rpc.connect()
        rpc.after_write = fail_probe
        with pytest.raises(BridgeError) as error:
            await rpc.call("turn/start", {}, write=True)
        assert error.value.unknown
        await asyncio.sleep(0.02)
        assert len(calls) == 1
        await rpc.close()


async def test_malformed_backend_frame_fails_closed():
    async def handler(ws, m):
        await ws.send_json(["invalid"])
    async with rpc_server(handler) as endpoint:
        rpc = RPC(endpoint, 1, lambda *_: None)
        await rpc.connect()
        with pytest.raises(BridgeError, match="backend_disconnected"):
            await rpc.call("read", {})
        assert not rpc.connected
        await rpc.close()


class FakeRPC:
    def __init__(self, cfg):
        self.generation = 1
        self.connected = True
        self.calls = []
        self.thread = {"id": new_id(), "cwd": str(cfg.projects[0]), "name": "Existing",
                       "status": {"type": "active", "activeFlags": []}}
        self.rows = [{"id": new_id(), "status": "inProgress"}, {"id": new_id(), "status": "inProgress"}]

    async def call(self, method, params, **kwargs):
        self.calls.append((method, params, kwargs))
        if method == "thread/read":
            return {"thread": self.thread}
        if method == "thread/resume":
            return {"thread": self.thread, "model": "actual-model", "reasoningEffort": "high"}
        if method == "thread/turns/list":
            return {"data": self.rows}
        if method == "turn/start":
            return {"turn": {"id": "new"}}
        if method == "turn/steer":
            return {"turnId": params["expectedTurnId"]}
        return {}


async def test_authoritative_latest_active_target_and_original_resume_policy(cfg):
    desktop = Desktop(cfg, "ws://127.0.0.1:12345")
    desktop.rpc = rpc = FakeRPC(cfg)
    snapshot = await desktop.snapshot(rpc.thread["id"])
    assert snapshot.active_turn == rpc.rows[0]["id"]
    assert snapshot.model == "actual-model" and snapshot.effort == "high"
    params = next(p for m, p, _ in rpc.calls if m == "thread/resume")
    assert params == {"threadId": rpc.thread["id"], "excludeTurns": True}
    rpc.thread["status"]["type"] = "idle"
    snapshot = await desktop.snapshot(rpc.thread["id"])
    assert snapshot.active_turn is None  # historical inProgress is not a control target


async def test_transient_terminal_and_late_event_do_not_confirm_wrong_turn(cfg, monkeypatch):
    desktop = Desktop(cfg, "ws://127.0.0.1:12345")
    desktop.rpc = rpc = FakeRPC(cfg)
    rpc.thread["status"]["type"] = "idle"
    tid = rpc.rows[0]["id"]
    rpc.rows[0]["status"] = "interrupted"
    clock = [100.0]
    monkeypatch.setattr("slack_codex_bridge.desktop.time.monotonic", lambda: clock[0])
    first = await desktop.snapshot(rpc.thread["id"])
    assert tid not in first.confirmed
    desktop._notify({"method": "turn/completed", "params": {"threadId": rpc.thread["id"],
                     "turn": {"id": rpc.rows[1]["id"], "status": "completed"}}}, 1)
    clock[0] += 4
    rpc.rows[0]["status"] = "completed"  # terminal changed: restart corroboration
    assert tid not in (await desktop.snapshot(rpc.thread["id"])).confirmed
    clock[0] += 4
    assert tid in (await desktop.snapshot(rpc.thread["id"])).confirmed
    rpc.generation = 2
    assert tid not in (await desktop.snapshot(rpc.thread["id"])).confirmed


async def test_event_and_snapshot_must_agree_and_approval_has_no_reply(cfg):
    desktop = Desktop(cfg, "ws://127.0.0.1:12345")
    desktop.rpc = rpc = FakeRPC(cfg)
    tid = rpc.rows[0]["id"]
    desktop._notify({"id": 7, "method": "item/fileChange/requestApproval",
                     "params": {"threadId": rpc.thread["id"], "turnId": tid}}, 1)
    assert (await desktop.snapshot(rpc.thread["id"])).approval
    assert desktop.requests["7"]["method"] == "item/fileChange/requestApproval"
    assert not any("result" in params for _, params, _ in rpc.calls)
    rpc.rows[0]["status"] = "completed"
    desktop._notify({"method": "turn/completed", "params": {"threadId": rpc.thread["id"],
                     "turn": {"id": tid, "status": "interrupted"}}}, 1)
    assert tid not in (await desktop.snapshot(rpc.thread["id"])).confirmed
    desktop._notify({"method": "turn/completed", "params": {"threadId": rpc.thread["id"],
                     "turn": {"id": tid, "status": "completed"}}}, 1)
    assert tid in (await desktop.snapshot(rpc.thread["id"])).confirmed
    desktop._notify({"method": "serverRequest/resolved", "params": {"requestId": 7}}, 1)
    assert not (await desktop.snapshot(rpc.thread["id"])).approval
    assert not desktop.requests


async def test_exact_wire_targets_and_multiple_final_messages(cfg):
    desktop = Desktop(cfg, "ws://127.0.0.1:12345")
    desktop.rpc = rpc = FakeRPC(cfg)
    tid = rpc.rows[0]["id"]
    for kind in ("turn_start", "steer", "interrupt"):
        await desktop.send(Action(kind, rpc.thread["id"], tid, "Ordinary text"))
    steer = next(p for m, p, _ in rpc.calls if m == "turn/steer")
    interrupt = next(p for m, p, _ in rpc.calls if m == "turn/interrupt")
    assert steer["expectedTurnId"] == tid and interrupt["turnId"] == tid
    rpc.rows[0].update(status="completed", items=[
        {"type": "agentMessage", "phase": "commentary", "text": "Progress"},
        {"type": "agentMessage", "phase": "final_answer", "text": "Final 1"},
        {"type": "agentMessage", "phase": "final_answer", "text": "Final 2"}])
    assert await desktop.final_messages(rpc.thread["id"], tid) == ("completed", ["Final 1", "Final 2"])


def test_remote_endpoint_refused(cfg):
    with pytest.raises(BridgeError, match="unverified_backend_endpoint"):
        Desktop(cfg, "ws://example.com:1234")
