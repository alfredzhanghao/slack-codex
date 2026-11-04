"""Synthetic transport failures only; no real Slack, credentials or desktop launch."""

import asyncio
from contextlib import nullcontext
import json
from types import SimpleNamespace

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from slack_codex_bridge import cli
from slack_codex_bridge.model import BridgeError, Operation, new_id, now_ms
from slack_codex_bridge.runtime import recovery, reconnect_delay
from slack_codex_bridge.slack import ManualSocket, Web
from slack_codex_bridge.store import Store
from test_p2_approval import backend_request
from test_slack import adapter, drain


class Client:
    def __init__(self, code="ratelimited", headers=None, status=429):
        self.code, self.headers, self.status = code, headers or {}, status
        self.calls = []

    async def api_call(self, method, **kwargs):
        self.calls.append(method)
        if self.code:
            response = AsyncSlackResponse(client=self, http_verb="POST", api_url="synthetic",
                req_args={}, data={"ok": False, "error": self.code}, headers=self.headers, status_code=self.status)
            raise SlackApiError("synthetic failure; must not escape", response)
        return SimpleNamespace(data={"ok": True, "url": "wss://synthetic.invalid/socket"})

    async def apps_connections_open(self, **kwargs):
        return await self.api_call("apps.connections.open", **kwargs)


@pytest.mark.parametrize("headers,delay", [
    ({"Retry-After": "120"}, 120), ({"retry-after": ["75"]}, 75),
    ({"RETRY-AFTER": "0"}, 1), ({}, 60), ({"Retry-After": "NaN"}, 60),
    ({"Retry-After": "inf"}, 60), ({"Retry-After": "-1"}, 60),
    ({"Retry-After": []}, 60), ({"Retry-After": "bad"}, 60)])
async def test_deadline_blocks_same_method_only_and_expires(cfg, monkeypatch, headers, delay):
    clock = [100.0]
    monkeypatch.setattr("slack_codex_bridge.slack.time", SimpleNamespace(monotonic=lambda: clock[0]))
    client = Client(headers=headers)
    web = Web(cfg, "synthetic", client)
    with pytest.raises(BridgeError) as error:
        await web.call("chat.update", write=True)
    assert error.value.code == "slack_rate_limited" and error.value.retry_after == delay
    assert not error.value.unknown
    client.code = None
    with pytest.raises(BridgeError) as blocked:
        await web.call("chat.update", write=True)
    assert blocked.value.retry_after == delay and len(client.calls) == 1
    await web.call("chat.postMessage", write=True)
    clock[0] += delay
    await web.call("chat.update", write=True)
    assert client.calls == ["chat.update", "chat.postMessage", "chat.update"]


async def test_socket_url_uses_single_attempt_and_shared_cooldown(cfg):
    client = Client(headers={"Retry-After": "90"})
    web = Web(cfg, "synthetic", client)
    socket = ManualSocket("synthetic", web_client=client, lost=lambda: None, open_url=web.call)
    for _ in range(2):
        with pytest.raises(BridgeError, match="slack_rate_limited"):
            await socket.issue_new_wss_url()
    assert client.calls == ["apps.connections.open"]


@pytest.mark.parametrize("code,category", [
    ("invalid_auth", "credentials"), ("token_revoked", "credentials"),
    ("token_expired", "credentials"), ("not_authed", "credentials"),
    ("account_inactive", "credentials"), ("missing_scope", "permissions"),
    ("not_allowed_token_type", "permissions"), ("no_permission", "permissions"),
    ("thread_not_found", "root")])
@pytest.mark.parametrize("write", [False, True])
async def test_explicit_slack_denials_keep_fixed_actionable_codes(cfg, code, category, write):
    client = Client(code, status=200)
    with pytest.raises(BridgeError) as error:
        await Web(cfg, "synthetic", client).call("synthetic", write=write)
    policy = recovery(error.value)
    assert not policy.retry and policy.category == category
    assert not error.value.unknown and "synthetic failure" not in str(error.value)
    assert client.calls == ["synthetic"]


async def test_background_permanent_fault_immediately_revokes_input(setup):
    c, s, backend, _ = setup
    assert await c.transfer(s, "slack", s.epoch)
    slack = adapter(c, backend)
    slack.card(s)
    epoch = s.epoch

    async def revoked():
        raise BridgeError("slack_write_token_revoked")

    slack.spawn(revoked())
    await drain(slack)
    assert slack.stop_error.code == "slack_write_token_revoked"
    assert not slack.ready and not c.available and not slack.buttons.tokens
    assert s.mode == "suspended" and s.epoch != epoch and not backend.sent


async def test_notification_429_preserves_healthy_input_and_failed_render_is_not_cached(setup):
    c, s, backend, _ = setup
    assert await c.transfer(s, "slack", s.epoch)
    slack = adapter(c, backend)
    attempts = []

    async def update(*args):
        attempts.append(args)
        if len(attempts) == 1:
            raise BridgeError("slack_rate_limited", retry_after=60)

    slack.web.update = update
    epoch = s.epoch
    slack.schedule_tick(s)
    await drain(slack)
    assert not slack.last_render and slack.ready and c.available
    assert s.mode == "slack" and s.epoch == epoch and not slack.stop_error
    await slack.render(s)
    assert len(attempts) == 2 and s.binding.binding_id in slack.last_render
    assert not backend.sent


@pytest.mark.parametrize("failure,retry", [("slack_rate_limited", True), ("slack_write_unconfirmed", False)])
async def test_approval_notice_429_can_be_reobserved_but_unknown_post_cannot(setup, failure, retry):
    c, s, _, _ = setup
    backend, request, wire = backend_request(c.cfg, params={"threadId": s.binding.codex_thread_id})
    slack = adapter(c, backend)
    slack.web.error = BridgeError(failure, unknown=not retry)
    with pytest.raises(BridgeError, match=failure):
        await slack.render_requests(s)
    slack.web.error = None
    await slack.render_requests(s)
    assert len(slack.web.posts) == (2 if retry else 1)
    assert bool(slack.request_cards[request.token]["ts"]) == retry
    assert not wire.sent  # Notification retries never answer the native request.


async def test_approval_update_rate_limit_retains_refresh_and_precedes_root_render(setup):
    c, s, _, _ = setup
    backend, request, wire = backend_request(c.cfg, params={"threadId": s.binding.codex_thread_id})
    slack = adapter(c, backend)
    await slack.render_requests(s)
    card = slack.request_cards[request.token]
    card["needs_refresh"] = True
    calls = []

    async def update(root, *args):
        calls.append(root)
        if len(calls) == 1:
            raise BridgeError("slack_rate_limited")

    slack.web.update = update
    async def observe():
        await slack.render_requests(s)
        await slack.render(s, force=True)
    with pytest.raises(BridgeError):
        await observe()
    assert card["ts"] == calls[0] and len(calls) == 1
    assert slack.request_cards[request.token]["needs_refresh"]
    await observe()
    assert calls == [card["ts"], card["ts"], s.binding.slack_root_ts]
    assert not wire.sent


async def test_new_approval_notice_precedes_rate_limited_retirement_update(setup):
    c, s, _, _ = setup
    backend, old, wire = backend_request(c.cfg, params={"threadId": s.binding.codex_thread_id})
    slack = adapter(c, backend)
    await slack.render_requests(s)
    backend.pending_requests.clear()
    backend._notify({"id": 8, "method": old.message["method"], "params": old.params}, 1)
    current = next(iter(backend.pending_requests.values()))

    async def update(*args):
        raise BridgeError("slack_rate_limited")

    slack.web.update = update
    with pytest.raises(BridgeError):
        await slack.render_requests(s)
    assert len(slack.web.posts) == 2 and slack.request_cards[current.token]["ts"]
    assert not slack.request_cards[old.token].get("closed") and not wire.sent


async def test_final_notice_429_is_known_undelivered_and_never_replays_task(setup):
    c, s, backend, store = setup
    assert await c.transfer(s, "slack", s.epoch)
    prepared = await c.prepare(s, "current-completion", "turn_start", "fixture", s.epoch, now_ms())
    op = await c.execute(s, prepared)
    tid = json.loads(op.backend_ref_json)["turn_id"]
    backend.current.terminals[tid] = "completed"
    backend.current.confirmed.add(tid)
    slack = adapter(c, backend)
    slack.web.error = BridgeError("slack_rate_limited", retry_after=90)
    await slack.tick(s)
    slack.web.error = None
    await slack.tick(s)
    saved = await store.operation(op.op_id)
    assert saved.status == "accepted" and len(backend.sent) == 1
    assert json.loads(saved.result_json)["delivery"] == "rate_limited_not_delivered"
    assert len(slack.web.posts) == 1 and slack.ready and not slack.stop_error


@pytest.mark.parametrize("code", ["slack_read_invalid_auth", "slack_write_missing_scope",
    "controlled_session_changed_restart_bridge", "database_unavailable", "persisted_root_app_mismatch"])
async def test_runtime_stops_permanent_failure_and_leaves_native_process_alone(setup, monkeypatch, code):
    c, s, backend, _ = setup
    # Separate synthetic database; never open the live bridge's store.
    seed = await Store(c.cfg.data_dir / "bridge.sqlite3").open()
    await seed.bind(s.binding)
    ops = [Operation(new_id(), status, s.binding.binding_id, 1, "turn_start", s.binding.codex_thread_id,
                     status=status) for status in ("received", "sending")]
    for op in ops:
        await seed.record_once(op)
    await seed.close()
    closed, instances = [], []
    backend.connected = True
    backend.rpc = SimpleNamespace(generation=1)
    backend.events = asyncio.Queue()

    async def close_backend():
        closed.append("backend_transport")
    backend.close = close_backend

    class BrokenSlack:
        def __init__(self, cfg, controller, *args):
            self.controller, self.ready, self.stop_error = controller, False, None
            instances.append(self)
            self.connects = 0

        def lost(self):
            self.ready = False
            self.controller.invalidate()

        async def connect(self):
            self.connects += 1
            raise BridgeError(code)

        async def close(self):
            closed.append("slack_transport")

    monkeypatch.setattr(cli.windows, "single_instance", lambda _: nullcontext())
    monkeypatch.setattr(cli.windows, "verify_record", lambda *args, **kwargs: {
        "endpoint": "synthetic", "backend": {}, "desktop": {}, "peer": {}})
    monkeypatch.setattr(cli, "Desktop", lambda *args: backend)
    monkeypatch.setattr(cli, "Slack", BrokenSlack)
    monkeypatch.setattr(type(c.cfg), "credentials", lambda _: ("synthetic", "synthetic"))
    with pytest.raises(BridgeError, match=code):
        await cli.run(c.cfg)
    assert instances[0].connects == 1 and not backend.sent
    assert closed == ["slack_transport", "backend_transport"]
    check = await Store(c.cfg.data_dir / "bridge.sqlite3").open()
    try:
        assert (await check.operation(ops[0].op_id)).status == "rejected"
        unknown = await check.operation(ops[1].op_id)
        assert unknown.status == "unknown" and unknown.unknown_review_state == "pending"
    finally:
        await check.close()


def test_reconnect_waits_full_cooldown_and_admission_errors_do_not_stop_bridge():
    assert reconnect_delay(BridgeError("slack_rate_limited", retry_after=120), 1) == 120
    assert reconnect_delay(BridgeError("backend_unavailable"), 10) == 30
    assert recovery(BridgeError("thread_outside_allowlist")).retry
    assert recovery(BridgeError("unknown_operation_not_found")).retry
