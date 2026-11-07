"""Real staging files and durable input receipts, without Slack/model side effects."""

import asyncio
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from slack_codex_bridge import attachments
from slack_codex_bridge.controller import Controller
from slack_codex_bridge.desktop import Desktop
from slack_codex_bridge.model import Action, BridgeError, new_id
from test_controller_store import opened
from test_slack import adapter, drain, message


def downloads(slack, monkeypatch, *, names=("note.txt", "image.png"), failure=None, mode="hosted"):
    seen = []
    async def info(method, **kwargs):
        assert method == "files.info" and slack.socket.acks
        fid = kwargs["file"]
        return {"file": {"id": fid, "user": slack.cfg.user_id, "name": names[int(fid[1:]) - 1],
                         "mode": mode, "is_external": False, "size": 7,
                         "url_private_download": f"https://files.slack.com/files-pri/T123-{fid}/fixture"}}
    async def fetch(web, url, maximum):
        seen.append(url)
        assert slack.socket.acks and maximum == 7
        if failure and len(seen) == 2:
            raise BridgeError(failure)
        return b"fixture"
    slack.web.call = info
    monkeypatch.setattr(attachments, "fetch", fetch)
    return seen


def file_event(cfg, state, text="inspect these attachments", **kwargs):
    return message(cfg, state, text, **{"subtype": "file_share", "files": [{"id": "F1"}, {"id": "F2"}], **kwargs})


async def test_complete_file_message_is_durable_before_ack_and_submitted_once(setup, monkeypatch):
    c, s, backend, store = setup
    await opened(c, s)
    slack = adapter(c, backend)
    seen = downloads(slack, monkeypatch)
    req = file_event(c.cfg, s)
    key = f"message:{c.cfg.team_id}:{c.cfg.dm_id}:{req.payload['event']['ts']}"
    async def ack():
        assert await store._call(lambda: store.db.execute(
            "SELECT status FROM input_messages WHERE source_key=?", (key,)).fetchone()[0]) == "received"
        assert not seen and not backend.sent
    slack.socket.check = ack
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert len(backend.sent) == 1 and len(seen) == 2
    rows = backend.sent[0].data["attachments"]
    assert all(Path(r["path"]).read_bytes() == b"fixture" for r in rows)
    assert all(not Path(r["path"]).is_relative_to(c.cfg.projects[0]) for r in rows)
    slack.socket.check = None
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert len(backend.sent) == 1 and len(seen) == 2
    assert (await store.by_source(key)).status == "accepted"


async def test_attachment_only_waits_for_instruction_then_consumes_once(setup, monkeypatch):
    c, s, backend, store = setup
    await opened(c, s)
    slack = adapter(c, backend)
    downloads(slack, monkeypatch)
    req = file_event(c.cfg, s, "")
    req.payload["event"].pop("text")
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert not backend.sent and "No task started" in slack.web.posts[-1][0]
    assert await store.pending_input(s.binding.binding_id)
    await asyncio.sleep(0.003)
    followup = message(c.cfg, s, "Compare the supplied note and image")
    await slack.receive(slack.socket, followup)
    await drain(slack)
    assert len(backend.sent) == 1 and len(backend.sent[0].data["attachments"]) == 2
    assert not await store.pending_input(s.binding.binding_id)
    await slack.receive(slack.socket, followup)
    await drain(slack)
    assert len(backend.sent) == 1


async def test_slack_text_snippet_downloads_as_a_normal_document(setup, monkeypatch):
    c, s, backend, _ = setup
    await opened(c, s)
    slack = adapter(c, backend)
    seen = downloads(slack, monkeypatch, names=("note.txt",), mode="snippet")
    req = file_event(c.cfg, s, "Read the attached document", files=[{"id": "F1"}])
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert len(seen) == 1 and len(backend.sent) == 1
    assert Path(backend.sent[0].data["attachments"][0]["path"]).read_bytes() == b"fixture"


@pytest.mark.parametrize("mode", ["external", "post", "unknown", None])
async def test_non_downloadable_slack_modes_are_rejected_before_fetch(setup, monkeypatch, mode):
    c, s, backend, _ = setup
    await opened(c, s)
    slack = adapter(c, backend)
    seen = downloads(slack, monkeypatch, names=("note.txt",), mode=mode)
    await slack.receive(slack.socket, file_event(c.cfg, s, "read it", files=[{"id": "F1"}]))
    await drain(slack)
    assert not seen and not backend.sent
    assert "attachment_metadata_unverified" in slack.web.posts[-1][0]


async def test_allowlisted_file_only_attribution_stages_without_starting_task(setup, monkeypatch):
    c, s, backend, store = setup
    c.cfg = replace(c.cfg, message_apps=("AINTEGRATION",))
    await opened(c, s)
    slack = adapter(c, backend)
    downloads(slack, monkeypatch)
    req = file_event(c.cfg, s, "*发送工具* <@U123>", app_id="AINTEGRATION")
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert not backend.sent and "No task started" in slack.web.posts[-1][0]
    assert await store.pending_input(s.binding.binding_id)
    await asyncio.sleep(0.003)
    await slack.receive(slack.socket, message(c.cfg, s, "Read the document"))
    await drain(slack)
    assert len(backend.sent) == 1 and len(backend.sent[0].data["attachments"]) == 2


async def test_ordinary_attachment_task_body_keeps_literal_attribution(setup, monkeypatch):
    c, s, backend, _ = setup
    c.cfg = replace(c.cfg, message_apps=("AINTEGRATION",))
    await opened(c, s)
    slack = adapter(c, backend)
    downloads(slack, monkeypatch)
    text = "Read this document\n*发送工具* <@U123>"
    await slack.receive(slack.socket, file_event(c.cfg, s, text, app_id="AINTEGRATION"))
    await drain(slack)
    assert len(backend.sent) == 1 and backend.sent[0].text == text


async def test_allowlisted_tool_output_in_desktop_mode_only_records_direction_and_acks(setup, monkeypatch):
    c,s,backend,store=setup
    c.cfg=replace(c.cfg,message_apps=('AINTEGRATION',))
    assert await c.transfer(s,'desktop',s.epoch)
    slack=adapter(c,backend)
    seen=downloads(slack,monkeypatch)
    request=file_event(c.cfg,s,'*发送工具* <@U123>',app_id='AINTEGRATION')
    for _ in range(2):
        await slack.receive(slack.socket,request)
        await drain(slack)
    assert len(slack.socket.acks)==2 and not seen and not backend.sent and not slack.web.posts
    assert not await store.pending_input(s.binding.binding_id) and not slack.pending_inputs
    rows=await store._call(lambda:store.db.execute('select status,code,attachment from input_messages').fetchall())
    assert [tuple(r) for r in rows]==[('output','integration_output_share',0)]


async def test_output_attribution_is_not_trusted_from_unconfigured_source(setup, monkeypatch):
    c,s,backend,store=setup
    assert await c.transfer(s,'desktop',s.epoch)
    slack=adapter(c,backend)
    seen=downloads(slack,monkeypatch)
    await slack.receive(slack.socket,file_event(c.cfg,s,'*发送工具* <@U123>',app_id='UNCONFIGURED'))
    await drain(slack)
    assert not seen and not backend.sent
    assert 'Click Take control' in slack.web.posts[-1][0] and 'desktop_input_mode' not in slack.web.posts[-1][0]
    rows=await store._call(lambda:store.db.execute('select status from input_messages').fetchall())
    assert [r[0] for r in rows]==['rejected']


@pytest.mark.parametrize('field,value',[('user','UOTHER'),('channel','DOTHER')])
async def test_output_direction_never_bypasses_event_identity(setup,field,value):
    c,s,backend,store=setup
    c.cfg=replace(c.cfg,message_apps=('AINTEGRATION',))
    slack=adapter(c,backend)
    req=file_event(c.cfg,s,'*发送工具* <@U123>',app_id='AINTEGRATION')
    req.payload['event'][field]=value
    await slack.receive(slack.socket,req)
    await drain(slack)
    assert not backend.sent and not slack.web.posts
    assert not await store._call(lambda:store.db.execute('select 1 from input_messages').fetchall())


async def test_restart_revokes_staged_context_and_followup_does_not_silently_omit_it(setup, monkeypatch):
    c, s, backend, store = setup
    await opened(c, s)
    slack = adapter(c, backend)
    downloads(slack, monkeypatch)
    req = file_event(c.cfg, s, "")
    await slack.receive(slack.socket, req)
    await drain(slack)
    restored = Controller(c.cfg, backend, store)
    await restored.load()
    await restored.synchronized()
    current = restored.states[s.binding.binding_id]
    await opened(restored, current)
    second = adapter(restored, backend)
    await asyncio.sleep(0.003)
    await second.receive(second.socket, message(c.cfg, current, "Use the attachments"))
    await drain(second)
    assert not backend.sent
    assert "attachment_context_unavailable_resend_files" in second.web.posts[-1][0]
    await second.receive(second.socket, req)
    await drain(second)
    assert not backend.sent  # The old file message is never reprocessed.


async def test_staged_attachments_and_explicit_skill_form_one_task(setup, monkeypatch):
    c, s, backend, _ = setup
    await opened(c, s)
    slack = adapter(c, backend)
    downloads(slack, monkeypatch)
    async def skills(tid):
        return [{"name": "test", "path": "C:/skills/test/SKILL.md", "enabled": True}]
    backend.skills = skills
    await slack.receive(slack.socket, file_event(c.cfg, s, ""))
    await drain(slack)
    await asyncio.sleep(0.003)
    await slack.receive(slack.socket, message(c.cfg, s, "!skill C:/skills/test/SKILL.md -- inspect both attachments"))
    await drain(slack)
    assert len(backend.sent) == 1
    assert backend.sent[0].data["skill"] == "C:/skills/test/SKILL.md"
    assert len(backend.sent[0].data["attachments"]) == 2


async def test_file_share_missing_file_objects_never_runs_only_the_text(setup):
    c, s, backend, _ = setup
    await opened(c, s)
    slack = adapter(c, backend)
    await slack.receive(slack.socket, message(c.cfg, s, "summarize the attached document", subtype="file_share"))
    await drain(slack)
    assert not backend.sent and "attachment_files_unavailable_check_files_read" in slack.web.posts[-1][0]


@pytest.mark.parametrize("failure", ["attachment_download_failed", "slack_missing_scope"])
async def test_partial_download_failure_never_submits_text_or_retains_partial_batch(setup, monkeypatch, failure):
    c, s, backend, _ = setup
    await opened(c, s)
    slack = adapter(c, backend)
    downloads(slack, monkeypatch, failure=failure)
    await slack.receive(slack.socket, file_event(c.cfg, s))
    await drain(slack)
    assert not backend.sent and failure in slack.web.posts[-1][0]
    assert not list((c.cfg.data_dir / "attachments").rglob("*.txt"))


async def test_disconnect_during_download_revokes_epoch_without_submission(setup, monkeypatch):
    c, s, backend, _ = setup
    await opened(c, s)
    slack = adapter(c, backend)
    downloads(slack, monkeypatch)
    async def fetch(*args):
        slack.lost()
        return b"fixture"
    monkeypatch.setattr(attachments, "fetch", fetch)
    await slack.receive(slack.socket, file_event(c.cfg, s))
    await drain(slack)
    assert not backend.sent and "attachment_binding_changed" in slack.web.posts[-1][0]


async def test_staging_does_not_take_control_and_unbound_files_do_not_download(setup, monkeypatch):
    c, s, backend, _ = setup
    slack = adapter(c, backend)
    seen = downloads(slack, monkeypatch)
    await slack.receive(slack.socket, file_event(c.cfg, s, ""))
    await drain(slack)
    assert not backend.sent and not seen and s.mode == "desktop"
    await slack.receive(slack.socket, file_event(c.cfg, None, ""))
    await drain(slack)
    assert not seen and "!project" in slack.web.posts[-1][0]


@pytest.mark.parametrize("value", ["http://files.slack.com/files-pri/file", "https://evil.example/files-pri/file",
    "https://files.slack.com.evil.example/files-pri/file", "https://files.slack.com@evil.example/files-pri/file",
    "https://files.slack.com:444/files-pri/file", "https://files.slack.com/upload/file",
    "https://files.slack.com/files-pri/file?redirect=evil", "https://files.slack.com/files-pri/file#fragment"])
def test_download_destination_never_forwards_credentials_to_untrusted_urls(value):
    with pytest.raises(BridgeError, match="attachment_download_url_forbidden"):
        attachments.download_url(value)


@pytest.mark.parametrize("name", ["token.env", ".env.local", "auth.json", "../result.txt", "a/b.png",
                                  "file.txt:ads", "private.key", "bad.exe", "note.txt."])
def test_attachment_names_preserve_credential_path_guards(name):
    with pytest.raises(BridgeError):
        attachments.safe_name(name)


async def test_native_image_and_document_input_use_verified_actual_files(setup, monkeypatch):
    c, s, backend, _ = setup
    slack = adapter(c, backend)
    slack.socket.acks.append("fixture")
    downloads(slack, monkeypatch)
    rows = await attachments.stage(c.cfg, slack.web, s.binding.codex_thread_id, [{"id": "F1"}, {"id": "F2"}])
    desktop = Desktop(c.cfg, "ws://127.0.0.1:1")
    desktop.snapshot = backend.snapshot
    calls = []
    async def call(method, params, **kwargs):
        calls.append((method, params))
        return {"turn": {"id": new_id()}}
    desktop.rpc = SimpleNamespace(call=call)
    await desktop.send(Action("turn_start", s.binding.codex_thread_id, text="inspect", data={"attachments": rows}))
    inputs = calls[0][1]["input"]
    assert inputs[1] == {"type": "localImage", "path": rows[1]["path"]}
    assert rows[0]["path"].replace("\\", "\\\\") in inputs[0]["text"]
    assert calls[0][0] == "turn/start"
    other = replace(backend.current, thread=replace(backend.current.thread, id=new_id()))
    with pytest.raises(BridgeError, match="attachment_session_mismatch"):
        attachments.attachment_inputs(c.cfg, other, rows, "task")
    Path(rows[0]["path"]).write_bytes(b"changed")
    with pytest.raises(BridgeError, match="attachment_changed"):
        attachments.attachment_inputs(c.cfg, backend.current, rows, "task")


async def test_metadata_validation_precedes_downloads_and_limits_batch(setup, monkeypatch):
    c, s, backend, _ = setup
    slack = adapter(c, backend)
    slack.socket.acks.append("fixture")
    seen = downloads(slack, monkeypatch, names=("valid.txt", "token.env"))
    with pytest.raises(BridgeError, match="attachment_filename_forbidden"):
        await attachments.stage(c.cfg, slack.web, s.binding.codex_thread_id, [{"id": "F1"}, {"id": "F2"}])
    assert not seen
    with pytest.raises(BridgeError, match="attachment_count_invalid"):
        await attachments.stage(c.cfg, slack.web, s.binding.codex_thread_id, [{"id": "F1"}] * 6)


async def test_retention_removes_only_expired_owned_batches(setup, monkeypatch):
    c, s, backend, _ = setup
    slack = adapter(c, backend)
    slack.socket.acks.append("fixture")
    downloads(slack, monkeypatch)
    rows = await attachments.stage(c.cfg, slack.web, s.binding.codex_thread_id, [{"id": "F1"}])
    directory = Path(rows[0]["path"]).parent
    unrelated = c.cfg.data_dir / "attachments" / "user-sources"
    unrelated.mkdir()
    old = time.time() - attachments.RETENTION_SECONDS - 10
    os.utime(directory, (old, old))
    os.utime(unrelated, (old, old))
    attachments.cleanup(c.cfg)
    assert not directory.exists() and unrelated.exists()


async def test_http_redirect_is_rejected_without_second_authenticated_request(monkeypatch):
    requests = []
    class Response:
        status = 302
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    class Session:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def get(self, url, **kwargs):
            requests.append((url, kwargs))
            return Response()
    monkeypatch.setattr(attachments.aiohttp, "ClientSession", Session)
    with pytest.raises(BridgeError, match="attachment_download_failed"):
        await attachments.fetch(SimpleNamespace(client=SimpleNamespace(token="synthetic")),
                                "https://files.slack.com/files-pri/T123-F1/fixture", 10)
    assert len(requests) == 1 and requests[0][1]["allow_redirects"] is False
    assert "synthetic" not in json.dumps(requests[0][0])
