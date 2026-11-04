"""Exercise the installed Socket Mode SDK against a local websocket, not Slack."""

import asyncio

import pytest
from aiohttp import web
from slack_sdk.web.async_client import AsyncWebClient

from slack_codex_bridge.model import BridgeError, now_ms
from slack_codex_bridge.slack import ManualSocket, Slack, private_logger
from test_slack import FakeWeb, message


@pytest.mark.parametrize("fault", [None, "reduced_scope", "app", "team", "dm", "root"])
async def test_sdk_hello_ingress_ack_and_server_disconnect_invalidates_epoch(setup, monkeypatch, fault):
    c, s, backend, _ = setup
    connections, acks = [], []

    async def route(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        connections.append(ws)
        await ws.send_json({"type": "hello", "connection_info": {"app_id": "AOTHER" if fault == "app" else c.cfg.app_id}})
        async for frame in ws:
            if frame.type.name == "TEXT":
                acks.append(frame.json())
        return ws

    app = web.Application()
    app.router.add_get("/", route)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    endpoint = f"ws://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    url_calls = []

    class LocalSocket(ManualSocket):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.proxy = None

        async def issue_new_wss_url(self):
            url_calls.append(1)
            return endpoint

    class LocalWeb(FakeWeb):
        def __init__(self):
            super().__init__()
            self.client = AsyncWebClient(token="synthetic", logger=private_logger(), retry_handlers=[])

        async def call(self, method, **kwargs):
            if method == "auth.test":
                return {"team_id": "TOTHER" if fault == "team" else c.cfg.team_id,
                        "user_id": "UBOT123", "bot_id": "BBOT123"}
            if method == "conversations.info":
                if fault == "reduced_scope":
                    raise BridgeError("slack_missing_scope")
                return {"channel": {"id": c.cfg.dm_id, "is_im": True,
                                    "user": "UOTHER" if fault == "dm" else c.cfg.user_id}}
            if method == "conversations.history":
                return {"messages": [{"user": c.cfg.user_id, "text": "Not imported as a task"}]}
            if method == "conversations.replies":
                return {"messages": [{"ts": s.binding.slack_root_ts,
                                      "app_id": "AOTHER" if fault == "root" else c.cfg.app_id, "bot_id": "BBOT123"}]}
            raise AssertionError(method)

    monkeypatch.setattr("slack_codex_bridge.slack.ManualSocket", LocalSocket)
    slack = Slack(c.cfg, c, backend, LocalWeb(), "synthetic")
    try:
        if fault in {"app", "team", "dm"}:
            codes = {"app": "slack_app_mismatch", "team": "slack_account_mismatch",
                     "dm": "slack_dm_owner_mismatch", "root": "persisted_root_app_mismatch"}
            with pytest.raises(BridgeError, match=codes[fault]):
                await slack.connect()
            assert not slack.ready and not backend.sent
            return
        await asyncio.wait_for(slack.connect(), 3)
        if fault == "root":
            assert slack.ready and c.available and s.mode == "suspended"
            assert s.root_fault == "root_identity_unverified"
            assert not await c.transfer(s, "slack", s.epoch) and not backend.sent
            await slack.command(None, "!roots", "")
            assert s.binding.binding_id in slack.web.posts[-1][0]
            return
        assert slack.ready and s.mode == "desktop"
        assert await c.transfer(s, "slack", s.epoch)
        epoch = s.epoch
        req = message(c.cfg, s)
        req.payload["event"]["ts"] = f"{now_ms() // 1000}.{now_ms() % 1000:03d}999"
        await connections[0].send_json({"type": "events_api", "envelope_id": "local-envelope",
                                        "payload": req.payload})
        for _ in range(100):
            if acks and backend.sent:
                break
            await asyncio.sleep(0.01)
        assert acks[0]["envelope_id"] == "local-envelope" and len(backend.sent) == 1
        await connections[0].send_json({"type": "disconnect", "reason": "refresh_requested"})
        for _ in range(100):
            if not slack.ready:
                break
            await asyncio.sleep(0.01)
        assert not slack.ready and s.epoch != epoch and s.mode == "suspended"
        assert len(url_calls) == 1  # SDK force reconnect cannot bypass controller resynchronization
    finally:
        await slack.close()
        await runner.cleanup()
