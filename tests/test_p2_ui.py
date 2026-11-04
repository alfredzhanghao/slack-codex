import json
from types import SimpleNamespace

from slack_codex_bridge.approval import QUESTIONS
from slack_codex_bridge.desktop import Desktop
from slack_codex_bridge.model import now_ms
from test_controller_store import opened
from test_desktop import FakeRPC
from test_p2_approval import backend_request, interaction
from test_p2_operations import add_p2_backend
from test_slack import adapter, drain


async def test_compose_modal_combines_exact_skill_files_and_task_in_one_operation(setup):
    c, s, fake, store = setup
    add_p2_backend(fake)
    (c.cfg.projects[0] / "input.txt").write_text("fixture", encoding="utf-8")
    await opened(c, s)
    slack = adapter(c, fake)
    await slack.skills_card(s)
    token = next(t for t, i in slack.buttons.tokens.items() if i.kind == "skill_use")
    views = []

    async def call(method, **kwargs):
        views.append(kwargs["view"])
        return {}
    slack.web.call = call
    _, _, open_modal = await slack.prepare_interaction(interaction(c.cfg, token, slack.buttons.tokens[token].root))
    await open_modal
    view = views[0]
    assert view["callback_id"] == "bridge_compose"
    payload = {"type": "view_submission", "team": {"id": c.cfg.team_id}, "user": {"id": c.cfg.user_id},
        "api_app_id": c.cfg.app_id, "view": {"callback_id": "bridge_compose", "private_metadata": view["private_metadata"],
        "state": {"values": {"task": {"text": {"value": "inspect"}},
                              "skill": {"text": {"value": "test-skill"}},
                              "files": {"text": {"value": '"input.txt"'}}}}}}
    req = SimpleNamespace(type="interactive", envelope_id="compose", payload=payload)
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert len(fake.sent) == 1
    assert fake.sent[0].text == "inspect" and fake.sent[0].data["skill"] == "C:/skills/test/SKILL.md"
    assert fake.sent[0].data["files"] == [str(c.cfg.projects[0] / "input.txt")]
    op = await store.by_source("button:" + view["private_metadata"])
    assert json.loads(op.payload_json)["data"]["skill"] == "C:/skills/test/SKILL.md"
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert len(fake.sent) == 1


async def test_structured_answer_modal_uses_question_fields_then_exact_server_shape(setup):
    c, s, fake, store = setup
    backend, request, wire = backend_request(c.cfg, QUESTIONS, params={
        "threadId": s.binding.codex_thread_id, "turnId": "turn", "questions": [
            {"id": "q1", "question": "Preferred style?", "header": "Style"}]})
    backend.snapshot = fake.snapshot
    fake.current.active_turn, fake.current.idle, fake.current.approval = "turn", False, True
    c.backend = backend
    await c.refresh(s)
    slack = adapter(c, backend)
    await slack.render_requests(s)
    token = slack.web.posts[0][1]["blocks"][-1]["elements"][0]["value"]
    views = []

    async def call(method, **kwargs):
        views.append(kwargs["view"])
        return {}
    slack.web.call = call
    _, _, opening = await slack.prepare_interaction(interaction(c.cfg, token, "200.000001"))
    await opening
    view = views[0]
    assert view["blocks"][-1]["block_id"] == "q0"
    payload = {"type": "view_submission", "team": {"id": c.cfg.team_id}, "user": {"id": c.cfg.user_id},
        "api_app_id": c.cfg.app_id, "view": {"callback_id": "bridge_answer", "private_metadata": view["private_metadata"],
        "state": {"values": {"q0": {"answer": {"value": "Simple"}}}}}}
    await slack.receive(slack.socket, SimpleNamespace(type="interactive", envelope_id="answer", payload=payload))
    await drain(slack)
    assert wire.sent == [{"id": 7, "result": {"answers": {"q1": {"answers": ["Simple"]}}}}]
    assert (await store.by_source("approval:" + request.token)).status == "unknown"
    await slack.render_requests(s)
    assert slack.web.updates[-1][2][0]["type"] == "section"
    assert len(slack.web.updates[-1][2]) == 1  # No actionable buttons on the closed card.


async def test_effort_button_from_previous_model_cannot_configure_new_model(setup):
    c, s, fake, _ = setup
    add_p2_backend(fake)
    await opened(c, s)
    s.effort_options = ("high",)
    slack = adapter(c, fake)
    from slack_codex_bridge.slack import Intent
    token = slack.buttons.issue(Intent("effort", s.binding.binding_id, s.epoch, thread_id=s.snapshot.model,
                                      root=s.binding.slack_root_ts, choice="high"))
    fake.current.model = "new-model"
    _, prepared, _ = await slack.prepare_interaction(interaction(c.cfg, token, s.binding.slack_root_ts))
    op = await c.execute(s, prepared)
    assert op.status == "rejected" and op.error_reason == "effort_selector_model_changed"
    assert not fake.sent and s.binding.next_effort_json is None


async def test_current_turn_reroute_displays_actual_model_with_effort_unconfirmed(cfg):
    d = Desktop(cfg, "ws://127.0.0.1:1")
    d.rpc = rpc = FakeRPC(cfg)
    tid = rpc.rows[0]["id"]
    d._notify({"method": "model/rerouted", "params": {"threadId": rpc.thread["id"], "turnId": tid,
               "fromModel": "old", "toModel": "actual"}}, 1)
    snapshot = await d.snapshot(rpc.thread["id"])
    assert snapshot.model == "actual" and snapshot.effort is None and snapshot.observed_ms <= now_ms()


async def test_modal_nonce_cannot_be_used_with_another_callback(setup):
    from slack_codex_bridge.slack import Intent
    c, s, fake, _ = setup
    slack = adapter(c, fake)
    token = slack.buttons.issue(Intent("compose_submit", s.binding.binding_id, s.epoch,
                                      root=s.binding.slack_root_ts))
    payload = {"type": "view_submission", "team": {"id": c.cfg.team_id}, "user": {"id": c.cfg.user_id},
               "api_app_id": c.cfg.app_id, "view": {"callback_id": "bridge_answer", "private_metadata": token}}
    assert await slack.prepare_interaction(payload) == (None, None, None)
    assert not fake.sent


async def test_only_configured_message_integration_attribution_is_removed(setup):
    from dataclasses import replace
    from test_slack import message
    c, s, fake, _ = setup
    slack = adapter(c, fake)
    calls = []

    async def bind(tid):
        calls.append(tid)
    slack.open_chat = bind
    tid = s.binding.codex_thread_id
    for i, allowed in enumerate((False, True)):
        slack.cfg = replace(c.cfg, message_apps=("AATTRIBUTION",) if allowed else ())
        req = message(c.cfg, text=f"!open {tid} *发送工具* <@UATTRIBUTION>", app_id="AATTRIBUTION",
                      ts=f"{now_ms()//1000}.{i+1:06d}")
        await slack.receive(slack.socket, req)
        await drain(slack)
    assert calls == [tid]


async def test_slack_action_identifiers_are_unique_per_block_and_skill_menu_is_bounded(setup):
    c, s, fake, _ = setup
    slack = adapter(c, fake)
    s.effort_options = ("low", "medium", "high")
    for block in slack.card(s)[1]:
        ids = [e["action_id"] for e in block.get("elements", [])]
        assert len(ids) == len(set(ids))

    async def skills(_):
        return [{"name": f"skill-{n}", "path": f"C:/skills/{n}/SKILL.md", "enabled": True, "description": "a" * 200}
                for n in range(100)]
    fake.skills = skills
    await slack.command(s, "!skills", s.epoch)
    assert len(slack.web.posts[-1][0]) < 3200


async def test_slow_slack_delivery_does_not_starve_other_chat_observation_or_duplicate_ticks(setup):
    import asyncio
    from dataclasses import replace
    from slack_codex_bridge.controller import State
    c, s, fake, _ = setup
    other = State(replace(s.binding, binding_id="other", codex_thread_id="other", slack_root_ts="201.000001"))
    slack = adapter(c, fake)
    entered, release, other_observed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def tick(state):
        calls.append(state.binding.binding_id)
        if state is s:
            entered.set()
            await release.wait()  # Represents slow Slack delivery after a valid backend read.
        else:
            other_observed.set()
    slack.tick = tick
    slack.schedule_tick(s)
    await entered.wait()
    slack.schedule_tick(s)
    slack.schedule_tick(other)
    await asyncio.wait_for(other_observed.wait(), 1)
    assert calls.count(s.binding.binding_id) == 1
    release.set()
    await asyncio.gather(*slack.work)
