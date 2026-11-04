"""Review regressions use gated fake transports and temporary stores only."""

import asyncio
import html

import pytest

from slack_codex_bridge.model import BridgeError
from test_p2_approval import backend_request, interaction
from test_p3_recovery import unknown
from test_slack import adapter, drain


async def artifact_click(slack, state):
    slack.settings_controls(state)
    token = next(t for t, i in slack.buttons.tokens.items() if i.kind == "artifacts")
    _, _, after = await slack.prepare_interaction(interaction(slack.cfg, token, state.binding.slack_root_ts))
    return after


async def test_artifact_form_opens_before_slow_discovery_and_updates_same_view(setup):
    c, s, backend, _ = setup
    slack = adapter(c, backend)
    entered, release, opened = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def rows(tid):
        entered.set()
        await release.wait()
        return []

    async def call(method, **kwargs):
        calls.append((method, kwargs))
        if method == "views.open":
            opened.set()
            return {"view": {"id": "VFIXTURE", "hash": "fixture-hash"}}
        return {}

    backend.artifacts, slack.web.call = rows, call
    task = asyncio.create_task(await artifact_click(slack, s))
    try:
        await asyncio.wait_for(opened.wait(), 1)
        await asyncio.wait_for(entered.wait(), 1)
        assert [m for m, _ in calls] == ["views.open"]
        initial = calls[0][1]["view"]
        assert any(b.get("block_id") == "path" for b in initial["blocks"])
        release.set()
        await task
        assert [m for m, _ in calls] == ["views.open", "views.update"]
        update = calls[1][1]
        assert update["view_id"] == "VFIXTURE" and update["hash"] == "fixture-hash"
        assert update["view"]["private_metadata"] == initial["private_metadata"]
        def path(v):
            return next(b for b in v["blocks"] if b.get("block_id") == "path")
        assert path(update["view"])["element"] == path(initial)["element"]
        assert not backend.sent
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("change", ["submitted", "epoch", "version", "disconnect"])
async def test_slow_discovery_cannot_update_a_submitted_or_stale_form(setup, change):
    c, s, backend, _ = setup
    slack = adapter(c, backend)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def rows(tid):
        entered.set()
        await release.wait()
        return []

    async def call(method, **kwargs):
        calls.append((method, kwargs))
        return {"view": {"id": "VFIXTURE", "hash": "fixture-hash"}}

    backend.artifacts, slack.web.call = rows, call
    task = asyncio.create_task(await artifact_click(slack, s))
    prepared = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if change == "submitted":
            initial = calls[0][1]["view"]
            payload = {"type": "view_submission", "team": {"id": c.cfg.team_id},
                "user": {"id": c.cfg.user_id}, "api_app_id": c.cfg.app_id,
                "view": {"callback_id": "bridge_artifact", "private_metadata": initial["private_metadata"],
                         "state": {"values": {"path": {"text": {"value": "chosen.txt"}}}}}}
            _, prepared, _ = await slack.prepare_interaction(payload)
            assert prepared.execute
        elif change == "epoch":
            s.epoch = "changed"
        elif change == "version":
            s.binding.binding_version += 1
        else:
            slack.lost()
        release.set()
        await task
        assert [m for m, _ in calls] == ["views.open"]
        assert not backend.sent and not any(i.kind == "artifact_choice" for i in slack.buttons.tokens.values())
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if prepared:
            await c.abandon(s, prepared)


async def test_failed_initial_modal_open_never_waits_for_discovery(setup):
    c, s, backend, _ = setup
    slack = adapter(c, backend)
    discoveries = []

    async def rows(tid):
        discoveries.append(tid)
        return []

    async def call(method, **kwargs):
        raise BridgeError("slack_rate_limited")

    backend.artifacts, slack.web.call = rows, call
    with pytest.raises(BridgeError, match="slack_rate_limited"):
        await (await artifact_click(slack, s))
    assert not discoveries and not backend.sent


@pytest.mark.parametrize("failure", ["backend_read_failed", "database_unavailable"])
async def test_discovery_failure_keeps_manual_path_or_preserves_permanent_failure(setup, failure):
    c, s, backend, _ = setup
    slack = adapter(c, backend)
    calls = []

    async def rows(tid):
        raise BridgeError(failure)

    async def call(method, **kwargs):
        calls.append((method, kwargs))
        return {"view": {"id": "VFIXTURE", "hash": "fixture-hash"}}

    backend.artifacts, slack.web.call = rows, call
    after = await artifact_click(slack, s)
    if failure == "database_unavailable":
        with pytest.raises(BridgeError, match=failure):
            await after
        assert len(calls) == 1
    else:
        await after
        updated = calls[-1][1]["view"]
        assert calls[-1][0] == "views.update" and "unavailable" in updated["blocks"][0]["text"]["text"]
        assert any(b.get("block_id") == "path" for b in updated["blocks"])
    assert not backend.sent


async def test_modal_update_rate_limit_has_no_automatic_retry_or_second_open(setup):
    c, s, backend, _ = setup
    slack = adapter(c, backend)
    calls = []

    async def call(method, **kwargs):
        calls.append(method)
        if method == "views.update":
            raise BridgeError("slack_rate_limited")
        return {"view": {"id": "VFIXTURE", "hash": "fixture-hash"}}

    slack.web.call = call
    with pytest.raises(BridgeError, match="slack_rate_limited"):
        await (await artifact_click(slack, s))
    assert calls == ["views.open", "views.update"] and not backend.sent
    assert any(i.kind == "artifact_submit" for i in slack.buttons.tokens.values())


async def test_recent_choice_cannot_move_between_two_forms_for_the_same_binding(setup):
    c, s, backend, _ = setup
    slack = adapter(c, backend)
    path = c.cfg.projects[0] / "report.txt"
    path.write_text("fixture", encoding="utf-8")
    views = []

    async def rows(tid):
        return [{"path": str(path)}]

    async def call(method, **kwargs):
        views.append(kwargs["view"])
        return {"view": {"id": "VFIXTURE", "hash": "fixture-hash"}}

    backend.artifacts, slack.web.call = rows, call
    await (await artifact_click(slack, s))
    first = views[-1]
    choice = next(b for b in first["blocks"] if b.get("block_id") == "recent")["element"]["options"][0]
    await (await artifact_click(slack, s))
    second = views[-1]
    assert first["private_metadata"] != second["private_metadata"]
    payload = {"type": "view_submission", "team": {"id": c.cfg.team_id}, "user": {"id": c.cfg.user_id},
        "api_app_id": c.cfg.app_id, "view": {"callback_id": "bridge_artifact", "private_metadata": second["private_metadata"],
        "state": {"values": {"recent": {"choice": {"selected_option": choice}}}}}}
    assert await slack.prepare_interaction(payload) == (None, None, None)
    assert not backend.sent


@pytest.mark.parametrize("text", ["<node/>" * 400, "&" * 1800, "中文<&>🙂" * 400])
async def test_unknown_preview_stays_within_slack_limit_and_preserves_every_character(setup, text):
    c, s, backend, store = setup
    op = await unknown(c, s, store, text=text)
    slack = adapter(c, backend)
    await slack.recovery_card(s, op.op_id, replace=False, epoch=s.epoch)
    blocks = slack.web.posts[-1][1]["blocks"]
    sections = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    assert all(0 < len(part) <= 3000 for part in sections)
    assert "".join(html.unescape(part) for part in sections) == slack.web.posts[-1][0]
    assert any(b["type"] == "actions" for b in blocks) and not backend.sent


async def test_native_request_preview_uses_the_same_complete_length_budget(setup):
    c, s, _, _ = setup
    backend, _, wire = backend_request(c.cfg, params={"threadId": s.binding.codex_thread_id, "command": "<node/>" * 400})
    slack = adapter(c, backend)
    await slack.render_requests(s)
    text, kwargs = slack.web.posts[-1]
    parts = [b["text"]["text"] for b in kwargs["blocks"] if b["type"] == "section"]
    assert all(len(p) <= 3000 for p in parts)
    assert "".join(html.unescape(p) for p in parts) == text and not wire.sent


@pytest.mark.parametrize("failure,stops", [
    ("slack_write_message_not_found", False), ("slack_write_thread_not_found", False),
    ("slack_write_token_revoked", True), ("slack_rate_limited", False)])
async def test_missing_closed_approval_card_is_local_but_real_denials_still_apply(setup, failure, stops):
    c, s, _, _ = setup
    backend, request, wire = backend_request(c.cfg, params={"threadId": s.binding.codex_thread_id})
    slack = adapter(c, backend)
    await slack.render_requests(s)
    card = slack.request_cards[request.token]
    backend.invalidate_requests()

    async def no_limits(**kwargs):
        pass

    async def call(method, **kwargs):
        assert method == "conversations.replies"
        return {"messages": [{"ts": s.binding.slack_root_ts, "bot_id": slack.bot_id, "app_id": c.cfg.app_id}]}

    async def update(root, *args):
        if root == card["ts"]:
            raise BridgeError(failure)

    slack.refresh_limits, slack.web.call, slack.web.update = no_limits, call, update
    slack.schedule_tick(s)
    await drain(slack)
    assert bool(slack.stop_error) == stops
    assert slack.ready == (not stops) and c.available == (not stops)
    assert not s.root_fault and not wire.sent
    if failure in {"slack_write_message_not_found", "slack_write_thread_not_found"}:
        assert card["closed"] and card["delivery"] == "message_unavailable"
        await slack.tick(s)  # A known missing card is not retried on each tick.
    elif failure == "slack_rate_limited":
        assert not card.get("closed")


async def test_deleted_closed_card_does_not_mask_a_missing_root(setup):
    c, s, _, _ = setup
    backend, request, wire = backend_request(c.cfg, params={"threadId": s.binding.codex_thread_id})
    slack = adapter(c, backend)
    await slack.render_requests(s)
    old = slack.web.posts[0][1]["blocks"][-1]["elements"][0]["value"]
    backend.invalidate_requests()

    async def no_limits(**kwargs):
        pass

    async def update(*args):
        raise BridgeError("slack_write_message_not_found")

    slack.refresh_limits, slack.web.update = no_limits, update
    slack.schedule_tick(s)
    await drain(slack)
    assert s.root_fault == "root_missing" and slack.ready and c.available and not slack.stop_error
    assert await slack.prepare_interaction(interaction(c.cfg, old, "200.000001")) == (None, None, None)
    assert not wire.sent
