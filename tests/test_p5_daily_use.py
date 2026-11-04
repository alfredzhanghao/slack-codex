import json
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from slack_codex_bridge.desktop import Desktop
from slack_codex_bridge.controller import Controller
from slack_codex_bridge.model import Action, BridgeError, Thread, new_id, now_ms
from test_controller_store import opened
from test_slack import adapter, drain, message
from test_p2_approval import interaction
from test_desktop import FakeRPC


async def test_fresh_listing_finishes_all_pages_and_deduplicates(cfg):
    d = Desktop(cfg, "ws://127.0.0.1:1")
    calls = []
    revision = 0

    async def call(method, params):
        assert method == "thread/list" and params["archived"] is False
        assert {"appServer", "vscode"} <= set(params["sourceKinds"])
        assert not any(source.startswith("subAgent") for source in params["sourceKinds"])
        page = int(params["cursor"] or 0)
        calls.append(page)
        row = {"id": str(page), "name": str(revision), "cwd": str(cfg.projects[0])}
        return {"data": [row, row], "nextCursor": str(page + 1) if page < 12 else None}

    d.rpc = SimpleNamespace(call=call)
    rows = await d.list_threads()
    assert len(rows) == 13 and len(calls) == 13
    revision = 1
    assert all(t.title == "1" for t in await d.list_threads())


async def test_listing_cycle_never_claims_complete(cfg):
    d = Desktop(cfg, "ws://127.0.0.1:1")

    async def call(*args):
        return {"data": [], "nextCursor": "repeated"}

    d.rpc = SimpleNamespace(call=call)
    with pytest.raises(BridgeError, match="thread_listing_incomplete"):
        await d.list_threads()


async def test_selected_project_is_filtered_by_native_query(cfg):
    d = Desktop(cfg, "ws://127.0.0.1:1")

    async def call(method, params):
        assert params["cwd"] == str(cfg.projects[0]) and params["useStateDbOnly"] is True
        return {"data": [], "nextCursor": None}

    d.rpc = SimpleNamespace(call=call)
    assert await d.list_threads(str(cfg.projects[0])) == []


async def test_project_discovery_does_not_grant_binding_or_remote_access(cfg):
    d = Desktop(cfg, "ws://127.0.0.1:1")
    assert not d._thread({"id": "other", "cwd": "D:/other-project"}, discovery=True).allowed
    with pytest.raises(BridgeError, match="thread_outside_allowlist"):
        d._thread({"id": "other", "cwd": "D:/other-project"})
    with pytest.raises(BridgeError, match="thread_environment_unverified"):
        d._thread({"id": "remote", "cwd": "/remote/project"}, discovery=True)


async def test_task_picker_covers_all_matches_and_filters_exact_project(setup):
    c, _, backend, _ = setup
    rows = [Thread(new_id(), f"Chat {i}", str(c.cfg.projects[0])) for i in range(25)]
    rows += [Thread(new_id(), "Other project", "D:/other-project", allowed=False)]

    async def list_threads(project=""):
        return rows + [rows[0]]

    backend.list_threads = list_threads
    slack = adapter(c, backend)
    await slack.tasks()
    text, data = slack.web.posts[-1]
    assert "26" in text and len(data["blocks"]) == 21
    await slack.tasks(page=1)
    assert len(slack.web.posts[-1][1]["blocks"]) == 7
    assert "accessory" not in slack.web.posts[-1][1]["blocks"][-2]
    await slack.tasks(project="D:/other-project")
    assert "1; page" in slack.web.posts[-1][0]
    await slack.projects()
    assert len(slack.web.posts[-1][1]["blocks"]) == 1
    assert not c.cfg.allows("D:/other-project") and not backend.sent


async def test_project_then_tasks_then_session_workflow(setup):
    c, state, backend, _ = setup
    calls = []

    async def list_threads(project=""):
        calls.append(project)
        return [backend.current.thread, backend.current.thread]

    backend.list_threads = list_threads
    slack = adapter(c, backend)
    await slack.command(None, "!tasks", "")
    assert "Choose a project" in slack.web.posts[-1][0] and not calls
    await slack.command(None, "!project", "")
    token = next(t for t, i in slack.buttons.tokens.items() if i.kind == "project")
    _, _, selection = await slack.prepare_interaction(interaction(c.cfg, token, "200.000001"))
    await selection
    assert slack.selected_project == str(c.cfg.projects[0])
    assert calls == [slack.selected_project] and "chats: 1;" in slack.web.posts[-1][0]
    await slack.command(None, "!tasks", "")
    assert calls == [slack.selected_project] * 2
    slack.lost()
    assert slack.selected_project == "" and not backend.sent


@pytest.mark.parametrize("invalid", [False, True])
async def test_context_uses_last_not_cumulative_and_rejects_invalid(cfg, invalid):
    d = Desktop(cfg, "ws://127.0.0.1:1")
    d.rpc.generation = 7
    event = {"method": "thread/tokenUsage/updated", "params": {"threadId": "chat", "turnId": "turn",
        "tokenUsage": {"last": {"totalTokens": 1200}, "total": {"totalTokens": 900000},
                       "modelContextWindow": True if invalid else 10000}}}
    d._notify(event, 7)
    if invalid:
        assert not d.context_usage
    else:
        assert d.context_usage["chat"]["remaining"] == 8800
        d._notify({"method": "thread/compacted", "params": {"threadId": "chat"}}, 7)
        assert not d.context_usage
    d._notify(event, 6)
    assert not d.context_usage


async def test_context_card_reports_unknown_and_observation_not_account_quota(setup):
    c, state, backend, _ = setup
    slack = adapter(c, backend)
    assert "Context remaining: unknown" in slack.card(state)[0]
    state.snapshot.context = {"window": 10000, "used": 1200, "remaining": 8800,
                              "generation": state.snapshot.generation, "observed_ms": 12345}
    body = slack.card(state)[0]
    assert "8,800 / 10,000" in body and "88.0%" in body and "Context observed" not in body
    assert body.splitlines()[0].startswith("Context remaining:")
    assert body.splitlines()[1].startswith("Model:")


async def test_desktop_model_change_invalidates_previous_context_reading(cfg):
    d = Desktop(cfg, "ws://127.0.0.1:1")
    d.rpc = FakeRPC(cfg)
    thread_id = d.rpc.thread["id"]
    await d.snapshot(thread_id)
    d.context_usage[thread_id] = {"remaining": 8800}
    d.context_models[thread_id] = "previous-model"
    assert not (await d.snapshot(thread_id)).context


def model_backend(backend):
    async def models():
        return [{"model": "alternate", "defaultReasoningEffort": "low"}]

    async def efforts(model):
        assert model in {"alternate", "test-model"}
        return ["low", "high"]

    backend.models, backend.efforts = models, efforts


async def test_model_and_effort_saved_without_turn_then_applied_once(setup):
    c, state, backend, _ = setup
    model_backend(backend)
    await opened(c, state)
    op = await c.execute(state, await c.prepare_command(state, "model", "!model alternate", state.epoch, now_ms()))
    assert op.status == "accepted" and not backend.sent
    assert json.loads(state.binding.next_effort_json)["value"] == "low"
    # A later effort choice refines the pending model, rather than resetting it.
    clock = json.loads(state.binding.next_effort_json)["sent_ms"] + 1
    op = await c.execute(state, await c.prepare_command(state, "effort", "!effort high", state.epoch, clock))
    assert op.status == "accepted" and not backend.sent
    prepared = await c.prepare(state, "task", "turn_start", "Hello", state.epoch, now_ms())
    assert (await c.execute(state, prepared)).status == "accepted"
    assert backend.sent[-1].data == {"model": "alternate", "effort": "high"}
    assert json.loads(state.binding.next_effort_json)["state"] == "applied"


@pytest.mark.parametrize("command", ["!model invented", "!model alternate extreme", "!model alternate low extra"])
async def test_invalid_model_selection_never_calls_backend_write(setup, command):
    c, state, backend, _ = setup
    model_backend(backend)
    await opened(c, state)
    op = await c.execute(state, await c.prepare_command(state, "selection", command, state.epoch, now_ms()))
    assert op.status == "rejected" and not backend.sent


async def test_pending_model_change_revalidated_before_dispatch(setup):
    c, state, backend, _ = setup
    model_backend(backend)
    await opened(c, state)
    await c.execute(state, await c.prepare_command(state, "model", "!model alternate", state.epoch, now_ms()))
    backend.current = replace(backend.current, model="desktop-changed-model")
    op = await c.execute(state, await c.prepare(state, "task", "turn_start", "Hello", state.epoch, now_ms()))
    assert op.error_reason == "pending_effort_model_changed" and not backend.sent


async def test_pending_model_selection_survives_restart_without_automatic_submission(setup):
    c, state, backend, store = setup
    model_backend(backend)
    await opened(c, state)
    await c.execute(state, await c.prepare_command(state, "model", "!model alternate high", state.epoch, now_ms()))
    restarted = Controller(c.cfg, backend, store)
    await restarted.load()
    await restarted.synchronized()
    restored = restarted.states[state.binding.binding_id]
    assert restored.mode == "desktop" and not backend.sent
    await opened(restarted, restored)
    op = await restarted.execute(restored, await restarted.prepare(restored, "after-restart", "turn_start",
        "Hello", restored.epoch, now_ms()))
    assert op.status == "accepted" and backend.sent[-1].data == {"model": "alternate", "effort": "high"}


async def test_model_command_socket_path_never_becomes_ordinary_task(setup):
    c, state, backend, _ = setup
    model_backend(backend)
    await opened(c, state)
    slack = adapter(c, backend)
    await slack.receive(slack.socket, message(c.cfg, state, "!model alternate high"))
    await drain(slack)
    assert not backend.sent and json.loads(state.binding.next_effort_json)["model"] == "alternate"


async def test_model_wire_override_only_on_turn_start(cfg):
    d = Desktop(cfg, "ws://127.0.0.1:1")
    calls = []

    async def call(method, params, *, write=False):
        calls.append((method, params, write))
        return {"turn": {"id": "new-turn"}}

    d.rpc = SimpleNamespace(call=call)
    await d.send(Action("turn_start", "chat", text="Hello", data={"model": "alternate", "effort": "high"}))
    assert calls == [("turn/start", {"threadId": "chat", "input": [{"type": "text", "text": "Hello"}],
                                     "model": "alternate", "effort": "high"}, True)]


@pytest.mark.parametrize("kind,choice", [("model_picker", "alternate"), ("effort_picker", "high")])
async def test_native_slack_settings_dropdown_saves_once_without_starting_task(setup, kind, choice):
    c, state, backend, store = setup
    model_backend(backend)
    await opened(c, state)
    slack = adapter(c, backend)
    slack.settings_controls(state)
    token = next(t for t, i in slack.buttons.tokens.items() if i.kind == kind)
    views = []

    async def call(method, **kwargs):
        assert method in {"views.open", "views.update"}
        views.append(kwargs["view"])
        return {"view": {"id": "VSETTINGS", "hash": "settings-hash"}}

    slack.web.call = call
    _, _, opening = await slack.prepare_interaction(interaction(c.cfg, token, state.binding.slack_root_ts))
    await opening
    assert "submit" not in views[0] and len(views) == 2
    view = views[-1]
    options = view["blocks"][-1]["element"]["options"]
    assert choice in [o["value"] for o in options]
    payload = {"type": "view_submission", "team": {"id": c.cfg.team_id}, "user": {"id": c.cfg.user_id},
        "api_app_id": c.cfg.app_id, "view": {"callback_id": "bridge_settings",
        "private_metadata": view["private_metadata"], "state": {"values": {
            "setting": {"choice": {"selected_option": {"value": choice}}}}}}}
    req = SimpleNamespace(type="interactive", envelope_id="settings", payload=payload)
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert not backend.sent
    op = await store.by_source("settings:" + view["private_metadata"])
    assert op.status == "accepted"
    setting = json.loads(state.binding.next_effort_json)
    assert setting["model"] == (choice if kind == "model_picker" else "test-model")
    assert setting["value"] == ("low" if kind == "model_picker" else choice)
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert not backend.sent and state.binding.next_effort_json == json.dumps(setting)


async def test_loading_settings_view_precedes_native_reads_and_cannot_submit(setup):
    c, state, backend, _ = setup
    slack = adapter(c, backend)
    slack.settings_controls(state)
    token = next(t for t, i in slack.buttons.tokens.items() if i.kind == "effort_picker")
    calls, views = [], []
    catalog_entered, release_catalog = asyncio.Event(), asyncio.Event()
    original = backend.snapshot

    async def snapshot(thread_id):
        assert calls == ["views.open"]
        return await original(thread_id)

    async def efforts(model):
        catalog_entered.set()
        await release_catalog.wait()
        return ["low", "high"]

    async def call(method, **kwargs):
        calls.append(method)
        views.append(kwargs["view"])
        return {"view": {"id": "VSETTINGS", "hash": "settings-hash"}}

    backend.snapshot, backend.efforts, slack.web.call = snapshot, efforts, call
    _, _, opening = await slack.prepare_interaction(interaction(c.cfg, token, state.binding.slack_root_ts))
    task = asyncio.create_task(opening)
    await asyncio.wait_for(catalog_entered.wait(), 2)
    view = views[0]
    assert "submit" not in view and calls == ["views.open"]
    payload = {"type": "view_submission", "team": {"id": c.cfg.team_id}, "user": {"id": c.cfg.user_id},
        "api_app_id": c.cfg.app_id, "view": {"callback_id": "bridge_settings",
        "private_metadata": view["private_metadata"], "state": {"values": {
            "setting": {"choice": {"selected_option": {"value": "high"}}}}}}}
    assert await slack.prepare_interaction(payload) == (None, None, None)
    release_catalog.set()
    await task
    assert calls == ["views.open"] and not backend.sent and state.binding.next_effort_json is None
