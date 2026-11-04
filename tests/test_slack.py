import asyncio
import json
from types import SimpleNamespace

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from slack_codex_bridge.model import BridgeError, Thread, new_id, now_ms
from slack_codex_bridge.slack import Buttons, Intent, Slack, Web, clean, timestamp, verify_dm


class FakeWeb:
    def __init__(self):
        self.posts = []
        self.updates = []
        self.error = None
        self.client = SimpleNamespace(session=None)

    async def post(self, text, **kwargs):
        self.posts.append((text, kwargs))
        if self.error:
            raise self.error
        return "200.000001"

    async def update(self, *args):
        self.updates.append(args)

    async def call(self, *args, **kwargs):
        return {}


class Socket:
    def __init__(self, check=None):
        self.acks = []
        self.check = check
        self.fail = False

    async def send_socket_mode_response(self, value):
        if self.check:
            await self.check()
        if self.fail:
            raise RuntimeError("simulated ACK loss")
        self.acks.append(value)


def message(cfg, state=None, text="Hello", **changes):
    clock = now_ms()
    event = {"type": "message", "channel": cfg.dm_id, "user": cfg.user_id,
             "text": text, "ts": f"{clock // 1000}.{clock % 1000:03d}000"}
    if state:
        event["thread_ts"] = state.binding.slack_root_ts
    event.update(changes)
    return SimpleNamespace(type="events_api", envelope_id=new_id(), payload={
        "team_id": cfg.team_id, "api_app_id": cfg.app_id, "event": event,
        "authorizations": [{"team_id": cfg.team_id, "user_id": "UBOT123", "is_bot": True}]})


def adapter(c, backend, check=None):
    slack = Slack(c.cfg, c, backend, FakeWeb(), "synthetic-test-value")
    slack.socket = Socket(check)
    slack.ready = True
    slack.opened_ms = 0
    slack.bot_user_id = "UBOT123"
    slack.bot_id = "BBOT123"
    return slack


async def drain(slack):
    await asyncio.gather(*slack.work, return_exceptions=True)


async def test_full_thread_id_picker_does_not_scan_history(setup):
    c, _, _, _ = setup
    tid = new_id()

    class Backend:
        async def get_thread(self, target):
            assert target == tid
            return Thread(tid, "GUI acceptance", str(c.cfg.projects[0]))

        async def list_threads(self):
            pytest.fail("exact ID must not scan history")

    slack = adapter(c, Backend())
    await slack.tasks(tid)
    assert len(slack.web.posts) == 1
    assert tid in slack.web.posts[0][1]["blocks"][0]["text"]["text"]


async def test_picker_read_failure_returns_safe_failure_without_retry(setup):
    c, _, _, _ = setup
    calls = []

    class Backend:
        async def get_thread(self, target):
            calls.append(target)
            raise BridgeError("thread_outside_allowlist")

    slack = adapter(c, Backend())
    tid = new_id()
    await slack.tasks(tid)
    assert calls == [tid]
    assert slack.web.posts == [("Task query failed: thread_outside_allowlist. No task submitted.", {})]


async def test_durable_receive_before_ack_backend_write_after_ack(setup):
    c, s, backend, store = setup
    assert await c.transfer(s, "slack", s.epoch)
    req = message(c.cfg, s)
    key = f"message:{c.cfg.team_id}:{c.cfg.dm_id}:{req.payload['event']['ts']}"

    async def check():
        assert (await store.by_source(key)).status == "received"
        assert not backend.sent
    slack = adapter(c, backend, check)
    await slack.receive(slack.socket, req)
    assert len(slack.socket.acks) == 1
    await drain(slack)
    assert len(backend.sent) == 1
    slack.socket.check = None
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert len(backend.sent) == 1


@pytest.mark.parametrize("change", [
    {"user": "UOTHER"}, {"channel": "DOTHER"}, {"thread_ts": "111.000001"},
    {"subtype": "message_changed"}, {"bot_id": "B123"}, {"text": None}, {}])
async def test_wrong_identity_root_and_top_level_normal_text_never_execute(setup, change):
    c, s, backend, _ = setup
    await c.transfer(s, "slack", s.epoch)
    slack = adapter(c, backend)
    req = message(c.cfg, s if change else None, **change)
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert not backend.sent and len(slack.socket.acks) == 1


@pytest.mark.parametrize("field", ["team_id", "api_app_id"])
async def test_wrong_envelope_identity_ignored(setup, field):
    c, s, backend, _ = setup
    await c.transfer(s, "slack", s.epoch)
    slack = adapter(c, backend)
    req = message(c.cfg, s)
    req.payload[field] = "OTHER"
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert not backend.sent


async def test_wrong_bot_authorization_ignored(setup):
    c, s, backend, _ = setup
    await c.transfer(s, "slack", s.epoch)
    slack = adapter(c, backend)
    req = message(c.cfg, s)
    req.payload["authorizations"][0]["user_id"] = "UOTHERBOT"
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert not backend.sent


async def test_ack_failure_rejects_without_send_and_resets_authority(setup):
    c, s, backend, store = setup
    await c.transfer(s, "slack", s.epoch)
    slack = adapter(c, backend)
    slack.socket.fail = True
    req = message(c.cfg, s)
    await slack.receive(slack.socket, req)
    key = f"message:{c.cfg.team_id}:{c.cfg.dm_id}:{req.payload['event']['ts']}"
    assert (await store.by_source(key)).status == "rejected"
    assert not backend.sent and not slack.ready and s.mode == "suspended"


async def test_database_failure_still_transport_acks_without_claiming_receipt(setup, monkeypatch):
    c, s, backend, store = setup
    slack = adapter(c, backend)

    async def failure(*_):
        raise BridgeError("database_unavailable")
    monkeypatch.setattr(store, "by_source", failure)
    await slack.receive(slack.socket, message(c.cfg, s))
    assert len(slack.socket.acks) == 1 and not backend.sent and not c.healthy
    assert not slack.web.posts


@pytest.mark.parametrize("payload", [None, {"event": []}, {"team_id": "T123", "api_app_id": "A123", "event": []}])
async def test_malformed_input_acknowledged_without_execution(setup, payload):
    c, _, backend, _ = setup
    slack = adapter(c, backend)
    req = SimpleNamespace(type="events_api", envelope_id="bad", payload=payload)
    await slack.receive(slack.socket, req)
    assert len(slack.socket.acks) == 1 and not backend.sent


async def test_one_shot_button_repeated_old_epoch_and_wrong_root(setup):
    c, s, backend, _ = setup
    target = new_id()
    backend.current.idle = False
    backend.current.active_turn = target
    await c.refresh(s)
    slack = adapter(c, backend)

    def payload(token, root=None):
        return {"type": "block_actions", "api_app_id": c.cfg.app_id,
                "team": {"id": c.cfg.team_id}, "user": {"id": c.cfg.user_id},
                "channel": {"id": c.cfg.dm_id}, "container": {"message_ts": root or s.binding.slack_root_ts},
                "actions": [{"value": token}]}

    token = slack.buttons.issue(Intent("interrupt", s.binding.binding_id, s.epoch, target,
                                      root=s.binding.slack_root_ts))
    _, first, _ = await slack.prepare_interaction(payload(token))
    assert first.execute
    assert await slack.prepare_interaction(payload(token)) == (None, None, None)
    await c.abandon(s, first)
    token = slack.buttons.issue(Intent("interrupt", s.binding.binding_id, s.epoch, target,
                                      root=s.binding.slack_root_ts))
    assert await slack.prepare_interaction(payload(token, "wrong")) == (None, None, None)
    token = slack.buttons.issue(Intent("interrupt", s.binding.binding_id, s.epoch, target,
                                      root=s.binding.slack_root_ts))
    c.invalidate()
    assert await slack.prepare_interaction(payload(token)) == (None, None, None)
    assert not backend.sent


async def test_slack_notification_failure_does_not_change_submission_or_retry(setup):
    c, s, backend, store = setup
    await c.transfer(s, "slack", s.epoch)
    slack = adapter(c, backend)
    slack.web.error = BridgeError("slack_write_unconfirmed", unknown=True)
    req = message(c.cfg, s)
    await slack.receive(slack.socket, req)
    await drain(slack)
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert len(backend.sent) == 1
    op = await store.by_source(f"message:{c.cfg.team_id}:{c.cfg.dm_id}:{req.payload['event']['ts']}")
    assert op.status == "accepted"


async def test_final_delivery_unknown_not_automatically_posted_again(setup):
    c, s, backend, store = setup
    await c.transfer(s, "slack", s.epoch)
    prepared = await c.prepare(s, "test", "turn_start", "Hello", s.epoch, now_ms())
    op = await c.execute(s, prepared)
    tid = backend.receipt.turn_id
    backend.current.confirmed = {tid}
    backend.current.terminals[tid] = "completed"
    slack = adapter(c, backend)
    slack.web.error = BridgeError("slack_write_unconfirmed", unknown=True)
    await slack.tick(s)
    await slack.tick(s)
    assert len(slack.web.posts) == 1 and len(backend.sent) == 1
    saved = await store.operation(op.op_id)
    assert json.loads(saved.result_json)["delivery"] == "unknown_no_automatic_retry"


async def test_unknown_card_exposes_queryable_operation_id(setup):
    c, s, backend, _ = setup
    await c.transfer(s, "slack", s.epoch)
    backend.send_error = BridgeError("backend_outcome_unknown", unknown=True)
    prepared = await c.prepare(s, "unknown-card", "turn_start", "Hello", s.epoch, now_ms())
    op = await c.execute(s, prepared)
    await c.refresh(s)
    text, _ = adapter(c, backend).card(s)
    assert op.status == "unknown" and "!op " + op.op_id in text


async def test_unconfirmed_root_creation_does_not_create_second_root(setup):
    c, _, backend, store = setup
    c.states.clear()
    # Use a different chat, keeping the fixture's original binding intact.
    backend.current.thread = type(backend.current.thread)(new_id(), "Second", backend.current.thread.cwd)
    slack = adapter(c, backend)
    slack.web.error = BridgeError("slack_write_unconfirmed", unknown=True)
    with pytest.raises(BridgeError):
        await slack.bind(backend.current.thread.id)
    assert any(x.slack_root_ts.startswith("pending:") for x in await store.bindings())
    slack.web.error = None
    await slack.bind(backend.current.thread.id)
    assert len(slack.web.posts) == 2  # failed root attempt, then diagnostic only
    assert "second root will not be posted" in slack.web.posts[1][0]


async def test_web_rate_limit_honors_retry_after_without_retry(cfg):
    class Client:
        count = 0

        async def api_call(self, *args, **kwargs):
            self.count += 1
            response = AsyncSlackResponse(client=self, http_verb="POST", api_url="test", req_args={},
                                          data={"ok": False, "error": "ratelimited"},
                                          headers={"Retry-After": "120"}, status_code=429)
            raise SlackApiError("rate limit", response)
    client = Client()
    web = Web(cfg, "synthetic", client)
    for _ in range(2):
        with pytest.raises(BridgeError, match="slack_rate_limited"):
            await web.call("chat.postMessage", write=True)
    assert client.count == 1


def test_timestamp_precision_literal_content_and_buttons():
    assert timestamp("1793200000.123456") == 1793200000123
    with pytest.raises(BridgeError):
        timestamp(1.0)
    # Synthetic fragments are deliberately not real credentials.
    assert clean("xapp-synthetic-example & <tag>") == "xapp-synthetic-example &amp; &lt;tag&gt;"
    assert clean("<@U123>") == "&lt;@U123&gt;"
    buttons = Buttons()
    token = buttons.issue(Intent("status"))
    assert buttons.issue(Intent("status")) == token
    assert buttons.take(token) and buttons.take(token) is None
    token = buttons.issue(Intent("status"))
    buttons.clear()
    assert buttons.take(token) is None


@pytest.mark.parametrize("authors,expected", [
    (["U123", "UBOT"], "fixed_dm_history_owner"),
    (["UOTHER", "U123"], "slack_dm_owner_mismatch"),
    (["UBOT"], "slack_dm_owner_evidence_unavailable"),
    ([], "slack_dm_owner_evidence_unavailable")])
async def test_reduced_scope_dm_requires_real_owner_evidence(cfg, authors, expected):
    calls = []

    async def call(method, **kwargs):
        calls.append((method, kwargs))
        if method == "conversations.info":
            raise BridgeError("slack_missing_scope")
        return {"messages": [{"user": a, "text": "This history must not execute"} for a in authors]}
    if expected == "fixed_dm_history_owner":
        assert await verify_dm(cfg, "UBOT", call) == expected
    else:
        with pytest.raises(BridgeError, match=expected):
            await verify_dm(cfg, "UBOT", call)
    assert all(kwargs["channel"] == cfg.dm_id for _, kwargs in calls)


async def test_dm_network_failure_does_not_fall_back_to_history(cfg):
    calls = []

    async def call(method, **kwargs):
        calls.append(method)
        raise BridgeError("slack_read_failed")
    with pytest.raises(BridgeError, match="slack_read_failed"):
        await verify_dm(cfg, "UBOT", call)
    assert calls == ["conversations.info"]


async def test_slack_conversation_reads_use_query_parameters(cfg):
    calls = []

    class Client:
        async def api_call(self, method, **kwargs):
            calls.append((method, kwargs))
            return SimpleNamespace(data={"ok": True})
    web = Web(cfg, "synthetic", Client())
    for method in ("conversations.info", "conversations.history", "conversations.replies"):
        await web.call(method, channel=cfg.dm_id)
    await web.call("chat.postMessage", write=True, channel=cfg.dm_id, text="Test")
    assert all(kwargs == {"http_verb": "GET", "params": {"channel": cfg.dm_id}} for _, kwargs in calls[:3])
    assert calls[3][1] == {"json": {"channel": cfg.dm_id, "text": "Test"}}
