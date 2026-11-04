import asyncio
import time
from types import SimpleNamespace

import pytest

from slack_codex_bridge.approval import COMMAND, FILES, FORM, PERMISSIONS, QUESTIONS, presentation, response
from slack_codex_bridge.desktop import Desktop
from slack_codex_bridge.model import Action, BridgeError, now_ms
from test_desktop import rpc_server
from test_slack import adapter, drain


class Wire:
    closed = False

    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)


def backend_request(cfg, method=COMMAND, *, request_id=7, params=None):
    backend = Desktop(cfg, "ws://127.0.0.1:1")
    wire = Wire()
    backend.rpc = SimpleNamespace(ws=wire, generation=1, connected=True)
    p = {"threadId": "thread", "turnId": "turn", "itemId": "item", "startedAtMs": now_ms(),
         "cwd": str(cfg.projects[0]), "command": "echo approved"}
    p.update(params or {})
    backend._notify({"id": request_id, "method": method, "params": p}, 1)
    return backend, next(iter(backend.pending_requests.values())), wire


@pytest.mark.parametrize("request_id", [7, "7"])
async def test_approval_sends_once_to_original_socket_and_never_claims_acceptance(cfg, request_id):
    backend, request, wire = backend_request(cfg, request_id=request_id)
    action = Action("approval", "thread", "turn", data={"request_token": request.token, "choice": "accept"})
    with pytest.raises(BridgeError, match="approval_sent_unconfirmed") as error:
        await backend.send(action)
    assert error.value.unknown
    assert wire.sent == [{"id": request_id, "result": {"decision": "accept"}}]
    with pytest.raises(BridgeError):
        await backend.send(action)
    assert len(wire.sent) == 1


@pytest.mark.parametrize("change", ["socket", "generation", "expiry", "resolved", "turn", "decision"])
async def test_stale_competing_or_unoffered_decision_never_sends(cfg, change):
    backend, request, wire = backend_request(cfg, params={"availableDecisions": ["decline"]})
    data = {"request_token": request.token, "choice": "decline"}
    turn = "turn"
    if change == "socket":
        backend.rpc.ws = Wire()
    elif change == "generation":
        backend.rpc.generation = 2
    elif change == "expiry":
        request.expires = time.monotonic() - 1
    elif change == "resolved":
        backend._notify({"method": "serverRequest/resolved", "params": {"requestId": 7}}, 1)
    elif change == "turn":
        turn = "later-turn"
    elif change == "decision":
        data["choice"] = "accept"
    with pytest.raises(BridgeError):
        await backend.send(Action("approval", "thread", turn, data=data))
    assert not wire.sent


async def test_two_choices_race_only_one_original_socket_response(cfg):
    backend, request, wire = backend_request(cfg)
    results = await asyncio.gather(*[
        backend.send(Action("approval", "thread", "turn", data={"request_token": request.token, "choice": c}))
        for c in ("accept", "decline")], return_exceptions=True)
    assert all(isinstance(r, BridgeError) for r in results) and len(wire.sent) == 1


@pytest.mark.parametrize("command", ["echo password", "cat Token.env", "cat .env", "cat .ssh/id_rsa"])
def test_command_content_is_relayed_without_keyword_filter(cfg, command):
    backend, request, _ = backend_request(cfg, params={"command": command})
    text, choices, _ = presentation(request, backend.items)
    assert command in text and "accept" in choices


def test_file_approval_requires_correlated_complete_diff_and_no_session_grant(cfg):
    backend, request, _ = backend_request(cfg, FILES)
    with pytest.raises(BridgeError, match="evidence_incomplete"):
        presentation(request, backend.items)
    backend.items["thread", "turn", "item"] = {"type": "fileChange", "changes": [
        {"path": "a.txt", "kind": {"type": "update"}, "diff": "-old\n+new"}]}
    text, choices, _ = presentation(request, backend.items)
    assert "-old\\n+new" in text and "accept" in choices
    request.params["grantRoot"] = str(cfg.projects[0])
    assert "accept" not in presentation(request, backend.items)[1]


def test_permissions_are_exact_requested_subset_and_turn_scoped(cfg):
    permissions = {"network": {"enabled": True}, "fileSystem": {"write": [str(cfg.projects[0])]}}
    backend, request, _ = backend_request(cfg, PERMISSIONS, params={"permissions": permissions})
    assert response(request, backend.items, "grant for this turn") == {"permissions": permissions, "scope": "turn"}
    assert response(request, backend.items, "deny") == {"permissions": {}, "scope": "turn"}


def test_question_ids_and_answer_shape_preserved_including_secret_flag(cfg):
    backend, request, _ = backend_request(cfg, QUESTIONS, params={"questions": [
        {"id": "q1", "header": "Choice", "question": "Which style?", "isSecret": False,
         "options": [{"label": "Simple"}]}]})
    assert response(request, backend.items, "answer", {"q1": "Simple"}) == {
        "answers": {"q1": {"answers": ["Simple"]}}}
    with pytest.raises(BridgeError):
        response(request, backend.items, "answer", {"q2": "Simple"})
    request.params["questions"][0]["isSecret"] = True
    request.params["questions"][0]["question"] = "Which password policy should be used?"
    text, _, _ = presentation(request, backend.items)
    assert '"isSecret": true' in text
    assert response(request, backend.items, "answer", {"q1": "xapp-synthetic-example"}) == {
        "answers": {"q1": {"answers": ["xapp-synthetic-example"]}}}


@pytest.mark.parametrize("value,valid", [(3, True), (True, False), (11, False), ("3", False)])
def test_mcp_primitive_form_validates_types_bounds_and_no_auth_flow(cfg, value, valid):
    backend, request, _ = backend_request(cfg, FORM, params={"mode": "form", "message": "Choose count",
        "requestedSchema": {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 10}}}})
    if valid:
        assert response(request, backend.items, "answer", {"n": value}) == {"action": "accept", "content": {"n": 3}}
    else:
        with pytest.raises(BridgeError):
            response(request, backend.items, "answer", {"n": value})
    request.params["mode"] = "url"
    with pytest.raises(BridgeError, match="original_client"):
        presentation(request, backend.items)


async def test_real_jsonrpc_server_response_keeps_server_id_type_and_no_client_retry(cfg):
    replies = []
    delivered = asyncio.Event()

    async def handler(ws, message):
        if message.get("method") == "request-fixture":
            await ws.send_json({"id": "server-7", "method": COMMAND, "params": {
                "threadId": "thread", "turnId": "turn", "itemId": "item", "command": "echo fixture",
                "cwd": str(cfg.projects[0])}})
            await ws.send_json({"id": message["id"], "result": {}})
        else:
            replies.append(message)
            delivered.set()
    async with rpc_server(handler) as endpoint:
        backend = Desktop(cfg, endpoint)
        await backend.connect()
        try:
            await backend.rpc.call("request-fixture", {})
            request = next(iter(backend.pending_requests.values()))
            with pytest.raises(BridgeError, match="sent_unconfirmed"):
                await backend.send(Action("approval", "thread", "turn", data={"request_token": request.token, "choice": "decline"}))
            await asyncio.wait_for(delivered.wait(), 1)
            assert replies == [{"id": "server-7", "result": {"decision": "decline"}}]
        finally:
            await backend.close()


def interaction(cfg, token, ts):
    return {"type": "block_actions", "team": {"id": cfg.team_id}, "user": {"id": cfg.user_id},
            "api_app_id": cfg.app_id, "channel": {"id": cfg.dm_id}, "container": {"message_ts": ts},
            "actions": [{"value": token}]}


async def test_slack_approval_ack_before_send_durable_once_and_competing_card(setup):
    c, s, fake, store = setup
    backend, request, wire = backend_request(c.cfg, params={"threadId": s.binding.codex_thread_id, "turnId": "turn"})
    backend.snapshot = fake.snapshot
    fake.current.active_turn, fake.current.idle, fake.current.approval = "turn", False, True
    c.backend = backend
    await c.refresh(s)
    slack = adapter(c, backend)
    await slack.render_requests(s)
    choices = slack.web.posts[0][1]["blocks"][-1]["elements"]
    assert len({x["action_id"] for x in choices}) == len(choices)
    first, second = choices[0]["value"], choices[1]["value"]

    async def ack_check():
        assert not wire.sent
        assert (await store.by_source("approval:" + request.token)).status == "received"
    slack.socket.check = ack_check
    req = SimpleNamespace(type="interactive", envelope_id="click", payload=interaction(c.cfg, first, "200.000001"))
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert len(wire.sent) == 1
    op = await store.by_source("approval:" + request.token)
    assert op.status == "unknown" and s.blocked
    slack.socket.check = None
    await slack.receive(slack.socket, SimpleNamespace(type="interactive", envelope_id="click2",
                        payload=interaction(c.cfg, second, "200.000001")))
    await drain(slack)
    assert len(wire.sent) == 1
    assert (await c.operation(s, op.op_id)).status == "unknown"


async def test_slack_refresh_preserves_backend_pending_but_wake_invalidates(cfg, setup):
    c, s, _, _ = setup
    backend, request, _ = backend_request(cfg, params={"threadId": s.binding.codex_thread_id})
    slack = adapter(c, backend)
    await slack.render_requests(s)
    old = slack.web.posts[0][1]["blocks"][-1]["elements"][0]["value"]
    slack.lost()
    assert backend.request(request.token) is request and slack.buttons.take(old) is None
    backend.invalidate_requests()
    with pytest.raises(BridgeError):
        backend.request(request.token)


@pytest.mark.parametrize("field", ["user", "team", "channel", "root", "version"])
async def test_approval_card_scope_cannot_be_forged(setup, field):
    c, s, _, _ = setup
    backend, _, wire = backend_request(c.cfg, params={"threadId": s.binding.codex_thread_id})
    slack = adapter(c, backend)
    await slack.render_requests(s)
    token = slack.web.posts[0][1]["blocks"][-1]["elements"][0]["value"]
    payload = interaction(c.cfg, token, "200.000001")
    if field in {"user", "team", "channel"}:
        payload[field]["id"] = "OTHER"
    elif field == "root":
        payload["container"]["message_ts"] = "300.000001"
    else:
        s.binding.binding_version += 1
    assert await slack.prepare_interaction(payload) == (None, None, None)
    assert not wire.sent


async def test_real_message_reply_shares_card_decision_key_and_cannot_change_root(setup):
    from test_slack import message
    c, s, fake, store = setup
    backend, request, wire = backend_request(c.cfg, params={"threadId": s.binding.codex_thread_id, "turnId": "turn"})
    backend.snapshot = fake.snapshot
    fake.current.active_turn, fake.current.idle, fake.current.approval = "turn", False, True
    c.backend = backend
    await c.refresh(s)
    slack = adapter(c, backend)
    await slack.render_requests(s)
    req = message(c.cfg, s, "!decision " + request.token + " decline")
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert wire.sent == [{"id": 7, "result": {"decision": "decline"}}]
    assert (await store.by_source("approval:" + request.token)).status == "unknown"
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert len(wire.sent) == 1


async def test_unsupported_wire_format_is_still_forwarded_readonly(setup):
    c, s, _, _ = setup
    backend, _, wire = backend_request(c.cfg, FORM, params={"threadId": s.binding.codex_thread_id,
        "mode": "url", "message": "Authentication token terminology fixture", "url": "https://example.com/fixture"})
    slack = adapter(c, backend)
    await slack.render_requests(s)
    text, payload = slack.web.posts[-1]
    assert "Authentication token terminology fixture" in text and "https://example.com/fixture" in text
    assert not any(block["type"] == "actions" for block in payload["blocks"])
    assert not wire.sent
