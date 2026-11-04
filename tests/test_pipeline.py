"""Real local websocket + SQLite + controller + Slack ingress; no external service."""

import asyncio
from types import SimpleNamespace

from slack_codex_bridge.controller import Controller
from slack_codex_bridge.desktop import Desktop
from slack_codex_bridge.model import Binding, new_id, now_ms
from slack_codex_bridge.store import Store
from test_desktop import rpc_server
from test_slack import adapter, drain, message


async def test_two_rounds_then_gui_steer_old_interrupt_and_disconnect(cfg):
    thread_id, turn_id = new_id(), [None]
    rows, writes = [], []
    meta = {"id": thread_id, "cwd": str(cfg.projects[0]), "name": "Pipeline test",
            "status": {"type": "idle", "activeFlags": []}}
    sockets = []

    async def handler(ws, m):
        if ws not in sockets:
            sockets.append(ws)
        method, p = m["method"], m["params"]
        if method == "thread/read":
            result = {"thread": meta}
        elif method == "thread/resume":
            result = {"thread": meta, "model": "actual-test", "reasoningEffort": "medium"}
        elif method == "thread/turns/list":
            result = {"data": rows}
        elif method == "turn/start":
            writes.append(m)
            turn_id[0] = new_id()
            rows.insert(0, {"id": turn_id[0], "status": "inProgress", "items": []})
            meta["status"]["type"] = "active"
            result = {"turn": rows[0]}
        elif method == "turn/steer":
            writes.append(m)
            assert p["expectedTurnId"] == turn_id[0]
            result = {"turnId": turn_id[0]}
        elif method == "turn/interrupt":
            writes.append(m)
            assert p["turnId"] == turn_id[0]
            result = {}
        elif method == "account/rateLimits/read":
            result = {"rateLimits": {"limitId": "test", "primary": None, "secondary": None}}
        else:
            raise AssertionError(method)
        await ws.send_json({"id": m["id"], "result": result})

    async with rpc_server(handler) as endpoint:
        backend = Desktop(cfg, endpoint)
        store = await Store(cfg.data_dir / "pipeline.sqlite3").open()
        try:
            await backend.connect()
            c = Controller(cfg, backend, store)
            await c.load()
            await c.synchronized()
            s = await c.bind(Binding(new_id(), cfg.team_id, cfg.dm_id, "123.000001", cfg.user_id,
                                    cfg.host_id, thread_id, str(cfg.projects[0]), "windows", effective_from_ms=0))
            slack = adapter(c, backend)
            assert await c.transfer(s, "slack", s.epoch)
            for n in range(2):
                req = message(cfg, s, text=f"Round {n}", ts=f"{now_ms() // 1000}.{now_ms() % 1000:03d}00{n}")
                await slack.receive(slack.socket, req)
                await drain(slack)
                assert len(writes) == n + 1
                rows[0].update(status="completed", items=[{"type": "agentMessage", "phase": "final_answer",
                                                           "text": f"Answer {n}"}])
                meta["status"]["type"] = "idle"
                await sockets[0].send_json({"method": "turn/completed", "params": {
                    "threadId": thread_id, "turn": rows[0]}})
                for _ in range(20):
                    if (thread_id, turn_id[0]) in backend.terminals:
                        break
                    await asyncio.sleep(0.01)
                await slack.tick(s)
                assert s.active_turn is None
            assert [text for text, kwargs in slack.web.posts if any(b['type'] == 'markdown' for b in kwargs.get('blocks', []))] == ['Answer 0', 'Answer 1']
            assert await c.transfer(s, "desktop", s.epoch)
            old_turn = turn_id[0]
            # Simulate an independently GUI-started turn at the server.
            turn_id[0] = new_id()
            rows.insert(0, {"id": turn_id[0], "status": "inProgress", "items": []})
            meta["status"]["type"] = "active"
            await c.refresh(s)
            for text in (f"!steer {turn_id[0]} -- Adjust plan", f"!interrupt {old_turn}"):
                await slack.receive(slack.socket, message(cfg, s, text=text))
                await drain(slack)
            assert [x["method"] for x in writes] == ["turn/start", "turn/start", "turn/steer"]
            await backend.close()
            # Runtime consumes disconnect and invalidates all input authority.
            while not backend.events.empty():
                event = backend.events.get_nowait()
                if event.kind == "disconnected":
                    slack.lost()
            assert not slack.ready and s.mode == "suspended"
            await backend.connect()
            await c.synchronized()
            assert s.mode == "desktop" and len(writes) == 3
            # A stale SDK client cannot submit even if it still invokes its listener.
            await slack.receive(SimpleNamespace(send_socket_mode_response=slack.socket.send_socket_mode_response),
                                message(cfg, s))
            assert len(writes) == 3
        finally:
            await backend.close()
            await store.close()
