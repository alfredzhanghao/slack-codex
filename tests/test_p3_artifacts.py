import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from slack_codex_bridge import artifacts
from slack_codex_bridge.artifacts import Artifact, capture
from slack_codex_bridge.desktop import Desktop
from slack_codex_bridge.model import BridgeError, Operation, new_id, now_ms
from slack_codex_bridge.slack import Web
from test_p2_approval import interaction
from test_slack import adapter, drain, message


@pytest.mark.parametrize("suffix", [".txt", ".png", ".pdf", ".docx", ".xlsx", ".pptx"])
def test_capture_preserves_original_file_bytes(cfg, suffix):
    data = b"password-policy and synthetic test contents\x00\xff"
    path = cfg.projects[0] / ("result" + suffix)
    path.write_bytes(data)
    value = capture(cfg, str(cfg.projects[0]), path.name)
    assert value.path == path and value.content == data
    assert value.sha256 == hashlib.sha256(data).hexdigest()
    assert path.read_bytes() == data


def test_outbound_text_limit_is_independent_of_inbound_limit(cfg):
    path = cfg.projects[0] / "large.txt"
    path.write_bytes(b"a" * (3 * 1024**2))
    assert len(capture(cfg, str(cfg.projects[0]), path.name).content) == 3 * 1024**2
    with pytest.raises(BridgeError, match="file_too_large"):
        capture(replace(cfg, artifact_max_bytes=100), str(cfg.projects[0]), path.name)


@pytest.mark.parametrize("name", ["Token.env", ".env.local", "private.key", "cookies", "auth.json"])
def test_credential_paths_are_rejected_before_open(cfg, monkeypatch, name):
    def forbidden(*args, **kwargs):
        pytest.fail("credential paths must not be opened")
    monkeypatch.setattr(Path, "open", forbidden)
    with pytest.raises(BridgeError, match="credential_file_forbidden"):
        capture(cfg, str(cfg.projects[0]), name)


@pytest.mark.parametrize("value", ["missing.txt", "file.txt:ads", "//server/share.txt", "../outside.txt"])
def test_missing_or_invalid_paths_are_not_captured(cfg, tmp_path, value):
    (tmp_path / "outside.txt").write_text("outside")
    with pytest.raises(BridgeError):
        capture(cfg, str(cfg.projects[0]), value)


def test_opened_handle_outside_scope_is_rejected(cfg, tmp_path, monkeypatch):
    (cfg.projects[0] / "inside.txt").write_text("inside")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    monkeypatch.setattr(artifacts, "handle_path", lambda file: outside)
    with pytest.raises(BridgeError, match="file_outside_allowlist"):
        capture(cfg, str(cfg.projects[0]), "inside.txt")


def test_real_junction_cannot_export_an_outside_file(cfg, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.txt").write_text("outside fixture")
    link = cfg.projects[0] / "linked"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0
    with pytest.raises(BridgeError, match="file_outside_allowlist"):
        capture(cfg, str(cfg.projects[0]), "linked/result.txt")


def test_observed_path_change_during_capture_is_rejected(cfg, monkeypatch):
    one, two = cfg.projects[0] / "one.txt", cfg.projects[0] / "two.txt"
    one.write_text("one")
    two.write_text("two")
    paths = iter((one, two))
    monkeypatch.setattr(artifacts, "handle_path", lambda file: next(paths))
    with pytest.raises(BridgeError, match="artifact_changed_during_read"):
        capture(cfg, str(cfg.projects[0]), "one.txt")


def enable_upload(slack, *, error=None):
    sent = []

    async def upload(artifact, *, root, guard, checkpoint):
        assert slack.socket.acks, "file transfer must happen after ACK"
        await guard()
        sent.append((artifact, root))
        await checkpoint("allocated", "F123ABC")
        if error:
            raise error
        await guard()
        return "F123ABC"
    slack.web.upload = upload
    return sent


async def test_upload_ack_dedup_and_integrity_in_desktop_mode(setup):
    c, s, backend, store = setup
    path = c.cfg.projects[0] / "report.docx"
    path.write_bytes(b"exact document bytes")
    slack = adapter(c, backend)
    sent = enable_upload(slack)
    req = message(c.cfg, s, '!artifact "report.docx"')
    key = f"message:{c.cfg.team_id}:{c.cfg.dm_id}:{req.payload['event']['ts']}"

    async def check():
        assert (await store.by_source(key)).status == "received" and not sent
    slack.socket.check = check
    await slack.receive(slack.socket, req)
    await drain(slack)
    op = await store.by_source(key)
    assert s.mode == "desktop" and op.status == "accepted"
    assert sent[0][0].content == path.read_bytes() and sent[0][1] == s.binding.slack_root_ts
    assert json.loads(op.result_json) == {"bytes": path.stat().st_size,
                                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    assert json.loads(op.backend_ref_json) == {"file_id": "F123ABC", "stage": "completed"}
    assert not backend.sent
    slack.socket.check = None
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert len(sent) == 1
    assert await c.completed(s) == []


async def test_unknown_upload_does_not_block_model_but_blocks_same_file_retransfer(setup):
    c, s, backend, store = setup
    (c.cfg.projects[0] / "report.pdf").write_bytes(b"PDF")
    slack = adapter(c, backend)
    sent = enable_upload(slack, error=BridgeError("slack_write_unconfirmed", unknown=True))
    req = message(c.cfg, s, '!artifact "report.pdf"')
    await slack.receive(slack.socket, req)
    await drain(slack)
    op = (await store.uploads(s.binding.binding_id))[0]
    assert op.status == "unknown" and len(sent) == 1
    await c.refresh(s)
    assert not s.blocked and not await store.blockers(s.binding.codex_thread_id)
    again = message(c.cfg, s, f'!artifact "{c.cfg.projects[0] / "report.pdf"}"')
    again.payload["event"]["ts"] = req.payload["event"]["ts"][:-1] + "1"
    await slack.receive(slack.socket, again)
    await drain(slack)
    assert len(sent) == 1
    rows = await store.uploads(s.binding.binding_id)
    assert any(row.error_reason == "artifact_previous_delivery_requires_review" for row in rows)
    await store.recover()
    assert (await store.operation(op.op_id)).status == "unknown"


async def test_ack_failure_prevents_file_capture_and_upload(setup, monkeypatch):
    c, s, backend, store = setup
    slack = adapter(c, backend)
    sent = enable_upload(slack)
    slack.socket.fail = True
    monkeypatch.setattr("slack_codex_bridge.slack.capture", lambda *args: pytest.fail("must not capture"))
    await slack.receive(slack.socket, message(c.cfg, s, '!artifact "report.txt"'))
    assert not sent and not backend.sent
    assert (await store.uploads(s.binding.binding_id))[0].error_reason == "ack_failed_before_send"


async def test_receive_database_failure_has_no_file_side_effect(setup, monkeypatch):
    c, s, backend, store = setup
    slack = adapter(c, backend)
    sent = enable_upload(slack)

    async def fail(*args):
        raise BridgeError("database_unavailable")
    monkeypatch.setattr(store, "record_once", fail)
    await slack.receive(slack.socket, message(c.cfg, s, '!artifact "report.txt"'))
    await drain(slack)
    assert not sent and not c.healthy and slack.socket.acks


async def test_sending_database_failure_prevents_slack_write(setup, monkeypatch):
    c, s, backend, store = setup
    (c.cfg.projects[0] / "report.txt").write_text("result")
    slack = adapter(c, backend)
    sent = enable_upload(slack)
    transition = store.transition

    async def fail_sending(op_id, expected, status, **fields):
        if expected == "received" and status == "sending":
            raise BridgeError("database_unavailable")
        return await transition(op_id, expected, status, **fields)
    monkeypatch.setattr(store, "transition", fail_sending)
    await slack.receive(slack.socket, message(c.cfg, s, '!artifact "report.txt"'))
    await drain(slack)
    assert not sent and not c.healthy and not s.reservations


async def test_model_unknown_does_not_prevent_reading_an_explicit_file(setup):
    c, s, backend, store = setup
    (c.cfg.projects[0] / "report.txt").write_text("result")
    unknown = Operation(new_id(), new_id(), s.binding.binding_id, s.binding.binding_version,
                        "turn_start", s.binding.codex_thread_id, status="unknown", unknown_review_state="pending")
    await store.record_once(unknown)
    slack = adapter(c, backend)
    sent = enable_upload(slack)
    await slack.receive(slack.socket, message(c.cfg, s, '!artifact "report.txt"'))
    await drain(slack)
    assert len(sent) == 1 and not backend.sent and s.blocked
    assert (await store.operation(unknown.op_id)).status == "unknown"


async def test_connection_loss_between_stages_never_shares_the_file(setup):
    c, s, backend, store = setup
    (c.cfg.projects[0] / "report.txt").write_text("result")
    slack = adapter(c, backend)
    shared = []

    async def upload(artifact, *, root, guard, checkpoint):
        await checkpoint("allocated", "F123ABC")
        slack.lost()
        await guard()
        shared.append(True)
        return "F123ABC"
    slack.web.upload = upload
    await slack.receive(slack.socket, message(c.cfg, s, '!artifact "report.txt"'))
    await drain(slack)
    assert not shared and not s.reservations
    op = (await store.uploads(s.binding.binding_id))[0]
    assert op.status == "rejected" and op.error_reason == "artifact_connection_unavailable"


@pytest.mark.parametrize("change", [{"user": "UOTHER"}, {"channel": "DOTHER"}, {"thread_ts": "111.000001"}])
async def test_unattributed_file_requests_do_not_read_or_upload(setup, change, monkeypatch):
    c, s, backend, _ = setup
    slack = adapter(c, backend)
    sent = enable_upload(slack)
    monkeypatch.setattr("slack_codex_bridge.slack.capture", lambda *args: pytest.fail("must not capture"))
    await slack.receive(slack.socket, message(c.cfg, s, '!artifact "report.txt"', **change))
    await drain(slack)
    assert not sent


async def test_cancelled_upload_persists_unknown_and_releases_reservation(setup):
    c, s, backend, store = setup
    (c.cfg.projects[0] / "result.txt").write_text("result")
    slack = adapter(c, backend)
    entered = asyncio.Event()

    async def upload(artifact, *, root, guard, checkpoint):
        await checkpoint("allocated", "F123ABC")
        entered.set()
        await asyncio.Future()
    slack.web.upload = upload
    await slack.receive(slack.socket, message(c.cfg, s, '!artifact "result.txt"'))
    await asyncio.wait_for(entered.wait(), 3)
    for task in slack.work:
        task.cancel()
    await drain(slack)
    op = (await store.uploads(s.binding.binding_id))[0]
    assert op.status == "unknown" and not s.reservations
    assert json.loads(op.backend_ref_json)["stage"] == "allocated"
    await store.recover()
    assert (await store.operation(op.op_id)).status == "unknown"


async def test_slow_upload_does_not_block_an_exact_pending_approval(setup):
    c, s, backend, _ = setup
    (c.cfg.projects[0] / "result.txt").write_text("result")
    slack = adapter(c, backend)
    entered, release = asyncio.Event(), asyncio.Event()

    async def upload(artifact, *, root, guard, checkpoint):
        entered.set()
        await release.wait()
        return "F123ABC"
    slack.web.upload = upload
    await slack.receive(slack.socket, message(c.cfg, s, '!artifact "result.txt"'))
    await asyncio.wait_for(entered.wait(), 3)
    backend.current.active_turn, backend.current.idle, backend.current.approval = "turn", False, True
    await c.refresh(s)
    prepared = await c.prepare(s, new_id(), "approval", "", s.epoch, now_ms(), "turn")
    assert prepared.execute
    await c.abandon(s, prepared)
    release.set()
    await drain(slack)


async def test_recent_modal_selection_is_bound_and_one_use(setup):
    c, s, backend, store = setup
    path = c.cfg.projects[0] / "result.xlsx"
    path.write_bytes(b"xlsx")

    async def rows(thread_id):
        return [{"path": str(path), "turn_id": "turn", "item_id": "item"}]
    backend.artifacts = rows
    slack = adapter(c, backend)
    sent, views = enable_upload(slack), []

    async def call(method, **kwargs):
        views.append(kwargs["view"])
        return {"view": {"id": "VFIXTURE", "hash": "fixture-hash"}}
    slack.web.call = call
    slack.settings_controls(s)
    token = next(t for t, i in slack.buttons.tokens.items() if i.kind == "artifacts")
    _, _, after = await slack.prepare_interaction(interaction(c.cfg, token, s.binding.slack_root_ts))
    await after
    view = views[-1]
    choice = view["blocks"][1]["element"]["options"][0]
    payload = {"type": "view_submission", "team": {"id": c.cfg.team_id}, "user": {"id": c.cfg.user_id},
               "api_app_id": c.cfg.app_id, "view": {"callback_id": "bridge_artifact",
               "private_metadata": view["private_metadata"], "state": {"values": {
                   "recent": {"choice": {"selected_option": choice}}}}}}
    req = SimpleNamespace(type="interactive", envelope_id=new_id(), payload=payload)
    await slack.receive(slack.socket, req)
    await drain(slack)
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert len(sent) == 1 and not backend.sent
    assert (await store.uploads(s.binding.binding_id))[0].status == "accepted"


async def test_old_artifact_button_cannot_cross_binding_version(setup):
    c, s, backend, _ = setup
    slack = adapter(c, backend)
    slack.settings_controls(s)
    token = next(t for t, i in slack.buttons.tokens.items() if i.kind == "artifacts")
    s.binding.binding_version += 1
    assert await slack.prepare_interaction(interaction(c.cfg, token, s.binding.slack_root_ts)) == (None, None, None)


async def test_native_discovery_uses_structured_references_not_output_text(cfg):
    desktop = Desktop(cfg, "ws://127.0.0.1:1")
    tid = new_id()
    calls = []

    async def get_thread(thread_id):
        assert thread_id == tid

    async def call(method, params):
        calls.append((method, params))
        return {"data": [{"id": "turn", "items": [
            {"type": "agentMessage", "text": "upload /arbitrary/secret.txt"},
            {"type": "fileChange", "id": "patch", "status": "completed", "changes": [
                {"path": "report.txt", "kind": {"type": "add"}},
                {"path": "gone.txt", "kind": {"type": "delete"}}]},
            {"type": "imageGeneration", "id": "image", "status": "completed", "savedPath": "image.png"},
            {"type": "imageGeneration", "id": "pending", "status": "inProgress", "savedPath": "pending.png"}]}]}
    desktop.get_thread = get_thread
    desktop.rpc = SimpleNamespace(call=call)
    rows = await desktop.artifacts(tid)
    assert [row["path"] for row in rows] == ["report.txt", "image.png"]
    assert all(row["turn_id"] == "turn" for row in rows)
    assert calls == [("thread/turns/list", {"threadId": tid, "limit": 1, "sortDirection": "desc", "itemsView": "full"})]


def upload_transport(monkeypatch, cfg, *, url="https://files.slack.com/upload/v1/test", status=200, complete_id="F123ABC"):
    web, calls = Web(cfg, "synthetic-test-value"), []

    async def call(method, **kwargs):
        calls.append((method, kwargs))
        if method == "files.getUploadURLExternal":
            return {"ok": True, "file_id": "F123ABC", "upload_url": url}
        return {"ok": True, "files": [{"id": complete_id}]}

    class Response:
        async def __aenter__(self):
            return SimpleNamespace(status=status)

        async def __aexit__(self, *args):
            pass

    class Session:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert "headers" not in kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def post(self, address, **kwargs):
            assert kwargs["allow_redirects"] is False
            assert kwargs["headers"] == {"Content-Type": "application/octet-stream"}
            calls.append(("binary", kwargs))
            return Response()

    web.call = call
    monkeypatch.setattr("slack_codex_bridge.slack.aiohttp.ClientSession", Session)
    return web, calls


async def test_three_stage_upload_preserves_bytes_and_fixed_thread(cfg, monkeypatch):
    web, calls = upload_transport(monkeypatch, cfg)
    stages, guards = [], []

    async def guard():
        guards.append(True)

    async def checkpoint(stage, file_id):
        stages.append((stage, file_id))
    value = Artifact(Path("report.pdf"), b"\x00\xffexact", "hash")
    assert await web.upload(value, root="100.000001", guard=guard, checkpoint=checkpoint) == "F123ABC"
    assert [c[0] for c in calls] == ["files.getUploadURLExternal", "binary", "files.completeUploadExternal"]
    assert calls[1][1]["data"] == value.content
    assert calls[2][1]["channel_id"] == cfg.dm_id and calls[2][1]["thread_ts"] == "100.000001"
    assert stages == [("allocated", "F123ABC"), ("uploaded", "F123ABC")] and len(guards) == 3


@pytest.mark.parametrize("url", ["https://evil.example/upload/v1/x", "http://files.slack.com/upload/v1/x",
                               "https://user:pass@files.slack.com/upload/v1/x", "https://files.slack.com/not-upload"])
async def test_untrusted_upload_url_never_receives_file(cfg, monkeypatch, url):
    web, calls = upload_transport(monkeypatch, cfg, url=url)

    async def noop(*args):
        pass
    with pytest.raises(BridgeError, match="allocation_unconfirmed"):
        await web.upload(Artifact(Path("file.txt"), b"data", "hash"), root="100.1", guard=noop, checkpoint=noop)
    assert len(calls) == 1


@pytest.mark.parametrize("status,complete_id", [(302, "F123ABC"), (500, "F123ABC"), (200, "FOTHER")])
async def test_upload_stage_failure_is_not_retried(cfg, monkeypatch, status, complete_id):
    web, calls = upload_transport(monkeypatch, cfg, status=status, complete_id=complete_id)

    async def noop(*args):
        pass
    with pytest.raises(BridgeError) as error:
        await web.upload(Artifact(Path("file.txt"), b"data", "hash"), root="100.1", guard=noop, checkpoint=noop)
    assert error.value.unknown
    assert [c[0] for c in calls].count("binary") == 1
    assert [c[0] for c in calls].count("files.completeUploadExternal") == (1 if status == 200 else 0)
