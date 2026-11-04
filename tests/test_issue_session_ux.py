"""Session control, skill identity and conversational retrieval regressions."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from slack_codex_bridge.model import BridgeError
from slack_codex_bridge.desktop import Desktop
from slack_codex_bridge.retrieval import is_retrieval
from slack_codex_bridge.retrieval import matching_outputs
from slack_codex_bridge.skill_view import description
from test_controller_store import opened
from test_p2_approval import interaction
from test_p3_artifacts import enable_upload
from test_slack import adapter, drain, message


@pytest.mark.parametrize("text", ["send me the Word file from the last task", "请把上次任务的Word文件发给我",
    "发给我上次的PDF文件", "download report.docx", "please send the output files"])
def test_known_retrieval_intents(text):
    assert is_retrieval(text)


@pytest.mark.parametrize("text", ["write a Word file and send it to me", "生成Word文件发给我", "修改文件后发给我",
    "explain how file downloads work", "Create a report", "Get the file and edit it", "分析这个文件"])
def test_task_instructions_are_not_intercepted_as_file_retrieval(text):
    assert not is_retrieval(text)


def test_explicit_missing_filename_never_substitutes_another_file_of_the_same_type():
    rows = [{"path": "report.docx"}]
    assert matching_outputs("send missing.docx", rows) == []
    assert matching_outputs("send report.docx and missing.docx", rows) == []
    assert matching_outputs("请把report.docx文件发给我", rows) == rows


async def test_last_task_retrieval_excludes_files_from_the_current_unfinished_turn(cfg):
    desktop = Desktop(cfg, "ws://127.0.0.1:1")
    async def get_thread(tid):
        return None
    async def call(method, params):
        assert method == "thread/turns/list" and params["limit"] == 20
        return {"data": [
            {"id": "active", "status": "inProgress", "items": [{"id": "partial", "type": "agentMessage",
                "phase": "final_answer", "text": "[unfinished](unfinished.docx)"}]},
            {"id": "previous", "status": "completed", "items": [{"id": "final", "type": "agentMessage",
                "phase": "final_answer", "text": "[report](report.docx)"}]}]}
    desktop.get_thread = get_thread
    desktop.rpc = SimpleNamespace(call=call)
    assert await desktop.latest_artifacts("thread") == [
        {"path": "report.docx", "turn_id": "previous", "item_id": "final"}]


async def test_unambiguous_retrieval_sends_existing_file_once_without_model_rerun(setup):
    c, s, backend, store = setup
    path = c.cfg.projects[0] / "report.docx"
    path.write_bytes(b"fixture document")
    async def artifacts(tid):
        assert tid == s.binding.codex_thread_id
        return [{"path": str(path), "turn_id": "last-turn"}]
    backend.latest_artifacts = artifacts
    async def active_artifacts(tid):
        pytest.fail("retrieval must select the latest completed task, not the active task")
    backend.artifacts = active_artifacts
    slack = adapter(c, backend)
    sent = enable_upload(slack)
    req = message(c.cfg, s, "send me the Word file from the last task")
    await slack.receive(slack.socket, req)
    await drain(slack)
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert not backend.sent and len(sent) == 1 and sent[0][0].content == b"fixture document"
    assert sent[0][1] == s.binding.slack_root_ts
    assert len(await store.uploads(s.binding.binding_id)) == 1


async def test_ambiguous_retrieval_offers_selection_without_sending_or_running_model(setup):
    c, s, backend, _ = setup
    paths = [c.cfg.projects[0] / name for name in ("a.docx", "b.docx")]
    for path in paths:
        path.write_bytes(b"fixture")
    async def artifacts(tid):
        return [{"path": str(path)} for path in paths]
    backend.artifacts = artifacts
    slack = adapter(c, backend)
    sent = enable_upload(slack)
    await slack.receive(slack.socket, message(c.cfg, s, "send me the Word file"))
    await drain(slack)
    assert not backend.sent and not sent
    assert "a.docx" in slack.web.posts[-1][0] and "b.docx" in slack.web.posts[-1][0]
    assert slack.web.posts[-1][1]["blocks"][-1]["elements"][0]["text"]["text"] == "Get output files"


async def test_uncertain_natural_retrieval_is_not_retried(setup):
    c, s, backend, store = setup
    path = c.cfg.projects[0] / "report.pdf"
    path.write_bytes(b"fixture")
    async def artifacts(tid):
        return [{"path": str(path)}]
    backend.artifacts = artifacts
    slack = adapter(c, backend)
    sent = enable_upload(slack, error=BridgeError("slack_upload_bytes_unconfirmed", unknown=True))
    req = message(c.cfg, s, "send me the PDF file")
    await slack.receive(slack.socket, req)
    await drain(slack)
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert len(sent) == 1 and not backend.sent
    assert (await store.uploads(s.binding.binding_id))[0].status == "unknown"


async def test_primary_card_is_concise_and_stop_is_prominent_without_compose_or_artifacts(setup):
    c, s, backend, _ = setup
    slack = adapter(c, backend)
    body, blocks = slack.card(s)
    assert s.snapshot.thread.title in body and c.cfg.projects[0].name in body
    labels = [e["text"]["text"] for b in blocks if b["type"] == "actions" for e in b["elements"]]
    assert "Settings" in labels and "Compose task" not in labels and "Artifacts" not in labels
    assert "Model" not in labels and "Fork" not in labels
    s.snapshot.active_turn = "active"
    _, blocks = slack.card(s)
    stop = next(e for b in blocks if b["type"] == "actions" for e in b["elements"] if e["text"]["text"] == "Stop")
    assert stop["style"] == "danger"
    token = slack.buttons.tokens[stop["value"]]
    assert token.kind == "interrupt" and token.turn_id == "active"


@pytest.mark.parametrize("change", ["project", "environment"])
async def test_bound_session_cannot_silently_move_to_another_allowed_project_or_environment(setup, change):
    c, s, backend, _ = setup
    if change == "project":
        other = c.cfg.projects[0] / "other"
        other.mkdir()
        assert c.cfg.allows(str(other))
        backend.current.thread = replace(backend.current.thread, cwd=str(other))
    else:
        backend.current.thread = replace(backend.current.thread, environment="wsl")
    with pytest.raises(BridgeError, match="snapshot_target_mismatch"):
        await c.refresh(s)
    assert s.mode == "suspended" and not backend.sent


async def test_secondary_settings_click_preserves_exact_root_identity(setup):
    c, s, backend, _ = setup
    slack = adapter(c, backend)
    slack.card(s)
    token = next(t for t, i in slack.buttons.tokens.items() if i.kind == "settings")
    _, _, after = await slack.prepare_interaction(interaction(c.cfg, token, s.binding.slack_root_ts))
    await after
    labels = [e["text"]["text"] for e in slack.web.posts[-1][1]["blocks"][-1]["elements"]]
    assert {"Model", "Reasoning effort", "Skills", "Compact", "Fork", "Get output files"} <= set(labels)
    token = next(t for t, i in slack.buttons.tokens.items() if i.kind == "compact")
    intent = slack.buttons.tokens[token]
    assert intent.binding_root == s.binding.slack_root_ts
    wrong = interaction(c.cfg, token, "999.000001")
    assert await slack.prepare_interaction(wrong) == (None, None, None)
    assert not backend.sent


def test_skill_descriptions_use_concise_metadata_or_complete_sentences():
    assert description({"shortDescription": "Short summary.", "description": "long " * 100}) == "Short summary."
    assert description({"interface": {"shortDescription": "Native display summary."}}) == "Native display summary."
    assert description({"description": "First complete sentence. Second complete sentence. " + "long " * 100}) == (
        "First complete sentence. Second complete sentence.")
    assert "Details" in description({"description": "long " * 100})
    assert "Details" in description({})


async def test_skills_paginate_and_duplicate_names_keep_exact_path_selection(setup):
    c, s, backend, _ = setup
    rows = [{"name": "same", "path": f"C:/skills/{i}/SKILL.md", "description": "Use this for a bounded task.",
             "enabled": i != 9} for i in range(11)]
    async def skills(tid):
        return rows
    backend.skills = skills
    slack = adapter(c, backend)
    await slack.skills_card(s)
    text, data = slack.web.posts[-1]
    visible = json.dumps(data["blocks"])
    assert "SKILL.md" not in visible and "same [1/11]" in visible
    assert len(data["blocks"]) <= 50 and "Next" in visible
    uses = [i for i in slack.buttons.tokens.values() if i.kind == "skill_use"]
    assert len(uses) == 8 and len({i.choice for i in uses}) == 8
    page_token = next(t for t, i in slack.buttons.tokens.items() if i.kind == "skills_page")
    _, _, after = await slack.prepare_interaction(interaction(c.cfg, page_token, "200.000001"))
    await after
    assert "Page 2" in slack.web.posts[-1][0]
    assert "same [11/11]" in json.dumps(slack.web.posts[-1][1])
    disabled_paths = [i.choice for i in slack.buttons.tokens.values() if i.kind == "skill_use"]
    assert rows[9]["path"] not in disabled_paths
    assert not backend.sent


@pytest.mark.parametrize("page", [1, 2])
async def test_middle_skill_pages_satisfy_slack_action_identity_contract(setup, page):
    c, s, backend, _ = setup
    async def skills(tid):
        return [{"name": f"skill-{i}", "path": f"C:/skills/{i}/SKILL.md",
                 "description": "A bounded task skill.", "enabled": True} for i in range(25)]
    backend.skills = skills
    slack = adapter(c, backend)
    post = slack.web.post
    async def validate(text, **kwargs):
        for block in kwargs["blocks"]:
            if block["type"] == "actions":
                ids = [element["action_id"] for element in block["elements"]]
                if len(ids) != len(set(ids)):
                    raise BridgeError("slack_write_invalid_blocks")
        return await post(text, **kwargs)
    slack.web.post = validate
    await slack.skills_card(s, page=page)
    navigation = slack.web.posts[-1][1]["blocks"][-1]["elements"]
    assert [e["text"]["text"] for e in navigation] == ["Previous", "Next"]
    assert not backend.sent


async def test_skill_choice_is_immutable_even_with_same_names_and_tampered_form(setup):
    c, s, backend, store = setup
    await opened(c, s)
    rows = [{"name": "same", "path": f"C:/skills/{i}/SKILL.md", "description": "Task skill.", "enabled": True}
            for i in range(2)]
    async def skills(tid):
        return rows
    backend.skills = skills
    slack = adapter(c, backend)
    await slack.skills_card(s)
    token = next(t for t, i in slack.buttons.tokens.items() if i.kind == "skill_use" and i.choice == rows[1]["path"])
    views = []
    async def call(method, **kwargs):
        views.append(kwargs["view"])
        return {}
    slack.web.call = call
    _, _, after = await slack.prepare_interaction(interaction(c.cfg, token, "200.000001"))
    await after
    view = views[0]
    payload = {"type": "view_submission", "team": {"id": c.cfg.team_id}, "user": {"id": c.cfg.user_id},
        "api_app_id": c.cfg.app_id, "view": {"callback_id": "bridge_compose", "private_metadata": view["private_metadata"],
        "state": {"values": {"task": {"text": {"value": "inspect"}},
                             "skill": {"text": {"value": rows[0]["path"]}}}}}}
    await slack.receive(slack.socket, SimpleNamespace(type="interactive", envelope_id="skill", payload=payload))
    await drain(slack)
    assert backend.sent[0].data["skill"] == rows[1]["path"]
    op = await store.by_source("button:" + view["private_metadata"])
    assert op.status == "accepted"
