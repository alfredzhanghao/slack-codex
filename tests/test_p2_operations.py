import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from slack_codex_bridge.controller import Controller
from slack_codex_bridge.desktop import Desktop
from slack_codex_bridge.inputs import local_file, parse_files
from slack_codex_bridge.model import Action, Binding, BridgeError, Receipt, Thread, new_id, now_ms
from test_controller_store import opened
from test_slack import adapter, drain, message


def add_p2_backend(fake):
    async def efforts(model):
        assert model == fake.current.model
        return ["low", "high"]

    async def skills(tid):
        return [{"name": "test-skill", "path": "C:/skills/test/SKILL.md", "enabled": True}]
    fake.efforts, fake.skills = efforts, skills


@pytest.mark.parametrize("command,kind", [("!compact", "compact"), ("!fork", "fork"),
    ("!skill test-skill -- task", "turn_start"), ('!file "input.txt" -- inspect', "turn_start"),
    ("!effort low", "effort")])
async def test_daily_commands_use_durable_admission_ack_and_duplicate_protection(setup, command, kind):
    c, s, fake, store = setup
    add_p2_backend(fake)
    (c.cfg.projects[0] / "input.txt").write_text("fixture", encoding="utf-8")
    await opened(c, s)
    slack = adapter(c, fake)
    req = message(c.cfg, s, command)

    async def check():
        assert not fake.sent
        op = await store.by_source(f"message:{c.cfg.team_id}:{c.cfg.dm_id}:{req.payload['event']['ts']}")
        assert op.status == "received" and op.kind == kind
    slack.socket.check = check
    await slack.receive(slack.socket, req)
    await drain(slack)
    slack.socket.check = None
    await slack.receive(slack.socket, req)
    await drain(slack)
    assert len(fake.sent) == (0 if kind == "effort" else 1)
    if kind == "effort":
        assert json.loads(s.binding.next_effort_json)["state"] == "pending"


@pytest.mark.parametrize("command", ["!compact", "!fork", "!skill test-skill -- task", '!file "x.txt" -- task'])
async def test_busy_rejections_never_execute_after_idle(setup, command):
    c, s, fake, store = setup
    await opened(c, s)
    fake.current.idle, fake.current.active_turn = False, "busy"
    await c.refresh(s)
    op = await c.prepare_command(s, "same-command", command, s.epoch, now_ms())
    assert op.operation.status == "rejected" and not op.execute
    fake.current.idle, fake.current.active_turn = True, None
    fake.current.confirmed.add("busy")
    await c.refresh(s)
    duplicate = await c.prepare_command(s, "same-command", command, s.epoch, now_ms())
    assert duplicate.operation.op_id == op.operation.op_id and not duplicate.execute
    assert not fake.sent


async def test_exact_skill_path_and_repeated_names_require_disambiguation(setup):
    c, s, fake, _ = setup
    add_p2_backend(fake)
    original = fake.skills

    async def skills(tid):
        return await original(tid) + [{"name": "test-skill", "path": "C:/other/SKILL.md", "enabled": True}]
    fake.skills = skills
    await opened(c, s)
    op = await c.execute(s, await c.prepare_command(s, "ambiguous", "!skill test-skill -- task", s.epoch, now_ms()))
    assert op.status == "rejected" and op.error_reason == "skill_ambiguous_or_unavailable"
    exact = await c.execute(s, await c.prepare_command(s, "exact", "!skill C:/other/SKILL.md -- task", s.epoch, now_ms()))
    assert exact.status == "accepted" and fake.sent[0].data["skill"] == "C:/other/SKILL.md"


async def test_effort_monotonic_model_revalidation_and_only_next_turn_override(setup):
    c, s, fake, _ = setup
    add_p2_backend(fake)
    await opened(c, s)
    clock = now_ms() + 2
    latest = await c.prepare_command(s, "new-choice", "!effort high", s.epoch, clock)
    assert (await c.execute(s, latest)).status == "accepted" and not fake.sent
    old = await c.prepare_command(s, "old-choice", "!effort low", s.epoch, clock - 1)
    assert (await c.execute(s, old)).error_reason == "older_effort_choice"
    assert json.loads(s.binding.next_effort_json)["value"] == "high"
    start = await c.prepare(s, "new-task", "turn_start", "hello", s.epoch, now_ms())
    assert (await c.execute(s, start)).status == "accepted"
    assert fake.sent[-1].data["effort"] == "high"
    assert json.loads(s.binding.next_effort_json)["state"] == "applied"
    fake.current.confirmed.add(fake.receipt.turn_id)
    await c.refresh(s)
    next_turn = await c.prepare(s, "later-task", "turn_start", "later", s.epoch, now_ms())
    await c.execute(s, next_turn)
    assert "effort" not in fake.sent[-1].data


async def test_pending_effort_never_silently_applies_to_changed_model(setup):
    c, s, fake, _ = setup
    add_p2_backend(fake)
    await opened(c, s)
    await c.execute(s, await c.prepare_command(s, "choice", "!effort low", s.epoch, now_ms()))
    fake.current.model = "other-model"
    op = await c.execute(s, await c.prepare(s, "task", "turn_start", "hello", s.epoch, now_ms()))
    assert op.status == "rejected" and op.error_reason == "pending_effort_model_changed" and not fake.sent


async def test_missing_old_binding_does_not_prevent_healthy_chat_sync(setup):
    c, s, fake, _ = setup
    original = fake.snapshot
    unavailable_id = new_id()
    from slack_codex_bridge.controller import State
    bad_binding = replace(s.binding, binding_id=new_id(), codex_thread_id=unavailable_id)
    bad = State(bad_binding)
    c.states[bad_binding.binding_id] = bad

    async def snapshot(tid):
        if tid == unavailable_id:
            raise BridgeError("persisted_thread_metadata_unavailable")
        return await original(tid)
    fake.snapshot = snapshot
    await c.synchronized()
    assert c.available and s.mode == "desktop" and bad.mode == "suspended" and not fake.sent
    bad.blocked = True  # Another chat's historical unknown must not block every chat in its cwd.
    assert await c.transfer(s, "slack", s.epoch)
    pending = await c.prepare(s, "healthy-task", "turn_start", "hello", s.epoch, now_ms())
    assert pending.execute and (await c.execute(s, pending)).status == "accepted"


async def test_unknown_effort_override_and_restart_never_reapply(setup):
    c, s, fake, store = setup
    add_p2_backend(fake)
    await opened(c, s)
    await c.execute(s, await c.prepare_command(s, "choice", "!effort high", s.epoch, now_ms()))
    fake.send_error = BridgeError("backend_outcome_unknown", unknown=True)
    op = await c.execute(s, await c.prepare(s, "task", "turn_start", "hello", s.epoch, now_ms()))
    assert op.status == "unknown" and json.loads(s.binding.next_effort_json)["state"] == "applying"
    restarted = Controller(c.cfg, fake, store)
    await restarted.load()
    await restarted.synchronized()
    state = restarted.states[s.binding.binding_id]
    assert state.mode == "desktop" and state.blocked
    assert not await restarted.transfer(state, "slack", state.epoch) and len(fake.sent) == 1


async def test_shared_directory_concurrent_submissions_have_one_writer(setup):
    c, s, fake, _ = setup
    await opened(c, s)
    second = await c.bind(Binding(new_id(), c.cfg.team_id, c.cfg.dm_id, "101.000001", c.cfg.user_id,
        c.cfg.host_id, s.binding.codex_thread_id, s.binding.project_path, "windows"))
    # Store correctly returns the old binding for the same thread. Use a second real thread.
    assert second is s
    original_snapshot = fake.snapshot
    tid = new_id()

    async def snapshot(target):
        if target == tid:
            return replace(fake.current, thread=Thread(tid, "fork", s.binding.project_path), observed_ms=now_ms())
        return await original_snapshot(target)
    fake.snapshot = snapshot
    other = await c.bind(Binding(new_id(), c.cfg.team_id, c.cfg.dm_id, "102.000001", c.cfg.user_id,
        c.cfg.host_id, tid, s.binding.project_path, "windows", effective_from_ms=0))
    assert await c.transfer(other, "slack", other.epoch)
    results = await asyncio.gather(*[c.prepare(x, "key-" + str(i), "turn_start", "task", x.epoch, now_ms())
                                   for i, x in enumerate((s, other))])
    assert sum(p.execute for p in results) == 1
    loser = next(p for p in results if not p.execute)
    assert loser.operation.error_reason == "shared_directory_busy_or_untrusted"


async def test_binding_created_during_peer_refresh_does_not_cancel_submission(setup):
    from slack_codex_bridge.controller import State
    c, s, fake, _ = setup
    await opened(c, s)
    peer_id, added_id = new_id(), new_id()
    c.states[peer_id] = State(replace(s.binding, binding_id=peer_id, codex_thread_id=peer_id))
    original = fake.snapshot

    async def snapshot(tid):
        if tid == peer_id:
            c.states[added_id] = State(replace(s.binding, binding_id=added_id, codex_thread_id=added_id))
            return replace(fake.current, thread=Thread(peer_id, "peer", s.binding.project_path), observed_ms=now_ms())
        return await original(tid)
    fake.snapshot = snapshot
    pending = await c.prepare(s, "new-binding-during-submit", "turn_start", "hello", s.epoch, now_ms())
    assert (await c.execute(s, pending)).status == "accepted" and len(fake.sent) == 1


async def test_compaction_receipt_is_not_completion_and_restart_keeps_protection(setup):
    c, s, fake, store = setup
    await opened(c, s)
    fake.receipt = Receipt(result={"compaction": "pending", "operation_token": "exact"})
    fake.compactions = {}
    op = await c.execute(s, await c.prepare_command(s, "compact", "!compact", s.epoch, now_ms()))
    assert op.status == "accepted" and s.compaction_pending
    assert not await c.completed(s)
    assert not (await c.prepare(s, "task", "turn_start", "hello", s.epoch, now_ms())).execute
    fake.compactions[s.binding.codex_thread_id] = {"status": "completed", "generation": 1, "operation_token": "other"}
    assert not await c.completed(s)
    restarted = Controller(c.cfg, fake, store)
    await restarted.load()
    await restarted.synchronized()
    state = restarted.states[s.binding.binding_id]
    assert state.compaction_pending
    fake.compactions[s.binding.codex_thread_id]["operation_token"] = "exact"
    result = await restarted.completed(state)
    assert len(result) == 1 and json.loads(result[0].result_json)["terminal"] == "completed"
    assert not state.compaction_pending


@pytest.mark.parametrize("kind", ["compact", "fork"])
async def test_cancel_after_write_is_unknown_without_replay(setup, kind):
    c, s, fake, store = setup
    await opened(c, s)
    fake.release = asyncio.Event()
    prepared = await c.prepare_command(s, "operation", "!" + kind, s.epoch, now_ms())
    task = asyncio.create_task(c.execute(s, prepared))
    await fake.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await store.operation(prepared.operation.op_id)).status == "unknown"
    assert not (await c.prepare_command(s, "operation", "!" + kind, s.epoch, now_ms())).execute
    assert len(fake.sent) == 1


async def test_fork_receipt_survives_entry_failure_and_repair_never_forks_again(setup):
    c, s, fake, store = setup
    await opened(c, s)
    child = new_id()
    fake.receipt = Receipt(thread_id=child, result={"fork_binding": "not_attempted"})
    slack = adapter(c, fake)
    slack.web.error = BridgeError("slack_write_unconfirmed", unknown=True)
    original_finish_fork = slack.finish_fork
    async def fail_entry(*_):
        raise BridgeError("slack_write_unconfirmed", unknown=True)
    slack.finish_fork = fail_entry
    prepared = await c.prepare_command(s, "fork-once", "!fork", s.epoch, now_ms())
    with pytest.raises(BridgeError):
        await slack.submit(s, prepared)
    op = await store.operation(prepared.operation.op_id)
    assert op.status == "accepted" and json.loads(op.backend_ref_json)["thread_id"] == child
    repaired = []
    original_snapshot = fake.snapshot

    async def snapshot(tid):
        if tid == child:
            return replace(fake.current, thread=Thread(child, "fork", s.binding.project_path), observed_ms=now_ms())
        return await original_snapshot(tid)

    async def bind(tid):
        repaired.append(tid)
    fake.snapshot, slack.bind = snapshot, bind
    slack.web.error = None
    slack.finish_fork = original_finish_fork
    await slack.command(s, "!fork-bind " + op.op_id, s.epoch)
    assert repaired == [child] and len(fake.sent) == 1
    assert not (await c.prepare_command(s, "fork-once", "!fork", s.epoch, now_ms())).execute


@pytest.mark.parametrize("path", ["../outside.txt", r"\\server\share\a.txt",
    r"C:relative.txt", "input.txt:stream"])
def test_local_files_fail_closed_before_any_file_content_read(cfg, path):
    with pytest.raises(BridgeError):
        local_file(cfg, str(cfg.projects[0]), path)


def test_windows_quoted_path_parser_preserves_backslashes_and_rejects_unquoted():
    assert parse_files('"D:\\with space\\a.txt" "b.md" -- inspect') == (["D:\\with space\\a.txt", "b.md"], "inspect")
    with pytest.raises(BridgeError):
        parse_files("a.txt -- inspect")


@pytest.mark.parametrize("name", ["passwords.txt", "token-notes.txt", "secret-design.md"])
def test_explicit_file_name_is_not_classified(cfg, name, monkeypatch):
    from pathlib import Path
    file = cfg.projects[0] / name
    file.write_text("dummy fixture only", encoding="utf-8")

    def no_content_read(*args, **kwargs):
        raise AssertionError("The bridge must not inspect the file content")
    monkeypatch.setattr(Path, "read_text", no_content_read)
    assert local_file(cfg, str(cfg.projects[0]), name) == file.resolve()


@pytest.mark.parametrize("name", ["Token.env", ".env", "auth.json", "key.pem"])
def test_existing_credential_file_boundary_is_preserved(cfg, name):
    file = cfg.projects[0] / name
    file.write_text("dummy fixture only", encoding="utf-8")
    with pytest.raises(BridgeError, match="credential_file_forbidden"):
        local_file(cfg, str(cfg.projects[0]), name)


async def test_task_content_is_forwarded_without_keyword_or_token_rewrite(setup):
    c, s, fake, _ = setup
    await opened(c, s)
    text = "Discuss password policy, API tokens, MFA and xapp-synthetic-example literally."
    op = await c.execute(s, await c.prepare(s, "literal-content", "turn_start", text, s.epoch, now_ms()))
    assert op.status == "accepted" and fake.sent[0].text == text


def test_file_resolution_rechecks_symlink_destination(cfg, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("fixture", encoding="utf-8")
    link = cfg.projects[0] / "link.txt"
    import _winapi
    target = tmp_path / "junction-target"
    target.mkdir()
    (target / "outside.txt").write_text("fixture", encoding="utf-8")
    junction = cfg.projects[0] / "junction"
    _winapi.CreateJunction(str(target), str(junction))
    link = junction / "outside.txt"
    with pytest.raises(BridgeError, match="outside_allowlist"):
        local_file(cfg, str(cfg.projects[0]), str(link))


async def test_native_image_and_office_path_use_only_supported_input_types(cfg):
    image, office = cfg.projects[0] / "image.png", cfg.projects[0] / "sample.docx"
    image.write_bytes(b"fixture")
    office.write_bytes(b"fixture")
    desktop = Desktop(cfg, "ws://127.0.0.1:1")
    writes = []
    snapshot = SimpleNamespace(thread=Thread("thread", "test", str(cfg.projects[0])))

    async def snap(_):
        return snapshot

    async def call(method, params, *, write=False):
        writes.append((method, params, write))
        return {"turn": {"id": "new-turn"}}
    desktop.snapshot = snap
    desktop.rpc = SimpleNamespace(call=call)
    await desktop.send(Action("turn_start", "thread", text="inspect", data={"files": [str(image), str(office)]}))
    inputs = writes[0][1]["input"]
    assert [x["type"] for x in inputs] == ["text", "localImage"]
    assert str(office).replace("\\", "\\\\") in inputs[0]["text"]
    assert all(x["type"] != "file" for x in inputs)
