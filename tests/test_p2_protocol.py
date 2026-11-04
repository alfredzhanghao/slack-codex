import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from slack_codex_bridge.desktop import Desktop
from slack_codex_bridge.model import Action, BridgeError, Receipt, new_id, now_ms
from test_controller_store import opened
from test_desktop import rpc_server


async def test_skill_discovery_uses_target_cwd_and_typed_exact_input(cfg):
    tid = new_id()
    calls = []

    async def call(method, params, *, write=False):
        calls.append((method, params, write))
        if method == "thread/read":
            return {"thread": {"id": tid, "cwd": str(cfg.projects[0])}}
        if method == "skills/list":
            return {"data": [{"cwd": str(cfg.projects[0]), "errors": [], "skills": [
                {"name": "example", "path": "C:/skills/example/SKILL.md", "enabled": True}]}]}
        return {"turn": {"id": "new-turn"}}
    d = Desktop(cfg, "ws://127.0.0.1:1")
    d.rpc = SimpleNamespace(call=call)
    receipt = await d.send(Action("turn_start", tid, text="run example", data={"skill": "C:/skills/example/SKILL.md"}))
    assert receipt.turn_id == "new-turn"
    assert calls[1][1] == {"cwds": [str(cfg.projects[0])], "forceReload": True}
    assert calls[-1] == ("turn/start", {"threadId": tid, "input": [
        {"type": "text", "text": "run example"}, {"type": "skill", "name": "example", "path": "C:/skills/example/SKILL.md"}]}, True)


async def test_changed_disabled_skill_never_becomes_an_untyped_string(cfg):
    d = Desktop(cfg, "ws://127.0.0.1:1")

    async def skills(_):
        return [{"name": "example", "path": "C:/skills/example/SKILL.md", "enabled": False}]
    d.skills = skills
    with pytest.raises(BridgeError, match="skill_changed"):
        await d.send(Action("turn_start", "t", text="task", data={"skill": "C:/skills/example/SKILL.md"}))


async def test_model_options_paginate_match_actual_model_and_never_use_default(cfg):
    d = Desktop(cfg, "ws://127.0.0.1:1")
    calls = []

    async def call(method, params):
        calls.append((method, params))
        if not params["cursor"]:
            return {"data": [{"model": "other", "isDefault": True}], "nextCursor": "page2"}
        return {"data": [{"model": "actual", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}]}
    d.rpc = SimpleNamespace(call=call)
    assert await d.efforts("actual") == ["high"]
    assert len(calls) == 2
    with pytest.raises(BridgeError, match="unavailable"):
        await d.efforts("missing")


async def test_native_compaction_tracks_events_even_before_response_and_retains_protection(cfg):
    d = Desktop(cfg, "ws://127.0.0.1:1")
    calls = []

    async def call(method, params, *, write=False):
        calls.append((method, params, write))
        assert "t" in d.compacting
        d._notify({"method": "item/started", "params": {"threadId": "t", "turnId": "u",
                   "item": {"id": "compact-item", "type": "contextCompaction"}}}, 1)
        d._notify({"method": "item/completed", "params": {"threadId": "t", "turnId": "u",
                   "item": {"id": "compact-item", "type": "contextCompaction"}}}, 1)
        return {}
    d.rpc = SimpleNamespace(call=call, generation=1)
    receipt = await d.send(Action("compact", "t"))
    assert calls == [("thread/compact/start", {"threadId": "t"}, True)]
    assert receipt.result["compaction"] == "pending"
    assert d.compactions["t"]["status"] == "completed"
    assert d.compactions["t"]["operation_token"] == receipt.result["operation_token"]


async def test_fork_is_persistent_no_overrides_no_auto_goal_continuation_and_no_fallible_read_after_write(cfg):
    d = Desktop(cfg, "ws://127.0.0.1:1")
    calls = []

    async def call(method, params, *, write=False):
        calls.append((method, params, write))
        if method == "thread/turns/list":
            return {"data": [{"id": "last-completed", "status": "completed"}]}
        if method == "thread/goal/get":
            return {"goal": None}
        return {"thread": {"id": "child"}, "modelProvider": "same"}
    d.rpc = SimpleNamespace(call=call)
    receipt = await d.send(Action("fork", "parent"))
    assert receipt.thread_id == "child" and len(calls) == 3
    assert calls[-1] == ("thread/fork", {"threadId": "parent", "lastTurnId": "last-completed",
                                        "excludeTurns": True, "ephemeral": False}, True)


async def test_retained_fork_receipt_can_resolve_commit_uncertainty_without_refork(setup, monkeypatch):
    c, s, fake, store = setup
    await opened(c, s)
    fake.receipt = Receipt(thread_id="child", result={"fork_binding": "not_attempted"})
    original = store.transition

    async def once(op_id, expected, status, **fields):
        if expected == "sending" and status == "accepted":
            raise BridgeError("database_unavailable")
        return await original(op_id, expected, status, **fields)
    monkeypatch.setattr(store, "transition", once)
    op = await c.execute(s, await c.prepare_command(s, "fork", "!fork", s.epoch, now_ms()))
    assert op.status == "unknown" and json.loads(op.backend_ref_json)["thread_id"] == "child"
    monkeypatch.setattr(store, "transition", original)
    resolved = await c.operation(s, op.op_id)
    assert resolved.status == "accepted" and len(fake.sent) == 1


@pytest.mark.parametrize("change", ["cwd", "policy", "provider", "busy"])
async def test_fork_binding_checks_effective_environment_and_inherited_policy(cfg, change):
    from slack_codex_bridge.model import Snapshot, Thread
    d = Desktop(cfg, "ws://127.0.0.1:1")
    effective = {"approvalPolicy": "on-request", "approvalsReviewer": "user", "sandbox": {"type": "workspaceWrite"},
                 "modelProvider": "openai", "cwd": str(cfg.projects[0])}
    d.effective = {"parent": effective.copy(), "child": effective.copy()}
    parent = Snapshot(Thread("parent", "parent", str(cfg.projects[0])), trusted=True, idle=True)
    child = replace(parent, thread=Thread("child", "child", str(cfg.projects[0])))
    if change == "cwd":
        child.thread = replace(child.thread, cwd=str(cfg.data_dir))
    elif change == "policy":
        d.effective["child"]["approvalPolicy"] = "never"
    elif change == "provider":
        d.effective["child"]["modelProvider"] = "other"
    else:
        child.idle = False

    async def snapshot(tid):
        return parent if tid == "parent" else child
    d.snapshot = snapshot
    with pytest.raises(BridgeError, match="unverified"):
        await d.verify_fork("parent", "child", {"effective": effective})


async def test_file_replaced_by_junction_after_receive_is_rejected_before_backend_write(setup, tmp_path):
    import _winapi
    c, s, fake, _ = setup
    folder = c.cfg.projects[0] / "selected"
    folder.mkdir()
    (folder / "input.txt").write_text("fixture")
    await opened(c, s)
    pending = await c.prepare_command(s, "file", '!file "selected/input.txt" -- inspect', s.epoch, now_ms())
    # Move only the test-owned directory, then replace its original path with a junction.
    target = tmp_path / "test-owned-moved"
    folder.rename(target)
    _winapi.CreateJunction(str(target), str(folder))
    result = await c.execute(s, pending)
    assert result.status == "rejected" and result.error_reason == "file_outside_allowlist" and not fake.sent


async def test_loopback_end_to_end_compact_and_fork_wire_only(cfg):
    received = []

    async def handler(ws, message):
        received.append(message)
        method = message["method"]
        result = ({"thread": {"id": "child"}} if method == "thread/fork" else
                  {"data": [{"id": "last-completed", "status": "completed"}]} if method == "thread/turns/list" else
                  {"goal": None} if method == "thread/goal/get" else {})
        await ws.send_json({"id": message["id"], "result": result})
        if method == "thread/compact/start":
            await ws.send_json({"method": "item/completed", "params": {"threadId": "parent", "turnId": "compact-turn",
                               "item": {"id": "compact", "type": "contextCompaction"}}})
    async with rpc_server(handler) as endpoint:
        d = Desktop(cfg, endpoint)
        await d.connect()
        try:
            await d.send(Action("compact", "parent"))
            assert (await d.send(Action("fork", "parent"))).thread_id == "child"
            for _ in range(20):
                if d.compactions["parent"]["status"] == "completed":
                    break
                await asyncio.sleep(0.01)
            assert d.compactions["parent"]["status"] == "completed"
            assert [m["method"] for m in received] == ["thread/compact/start", "thread/turns/list", "thread/goal/get", "thread/fork"]
        finally:
            await d.close()


async def test_resume_environment_must_match_read_target_before_file_or_skill_operations(cfg):
    from test_desktop import FakeRPC
    d = Desktop(cfg, "ws://127.0.0.1:1")
    rpc = FakeRPC(cfg)
    original = rpc.call

    async def call(method, params, **kwargs):
        value = await original(method, params, **kwargs)
        if method == "thread/resume":
            value["thread"] = {**value["thread"], "environments": [{"environmentId": "unverified-remote",
                                                                       "cwd": str(cfg.projects[0])}]}
        return value
    rpc.call = call
    d.rpc = rpc
    with pytest.raises(BridgeError, match="environment_unverified"):
        await d.snapshot(rpc.thread["id"])


async def test_unloaded_persisted_chat_is_scoped_before_resume_and_no_task_write(cfg):
    from test_desktop import FakeRPC
    d = Desktop(cfg, "ws://127.0.0.1:1")
    rpc = FakeRPC(cfg)
    original = rpc.call
    calls = []

    async def call(method, params, **kwargs):
        calls.append(method)
        if method == "thread/read":
            raise BridgeError("backend_thread_not_loaded")
        if method == "thread/list":
            return {"data": [rpc.thread]}
        return await original(method, params, **kwargs)
    rpc.call = call
    d.rpc = rpc
    assert (await d.snapshot(rpc.thread["id"])).trusted
    assert calls[:3] == ["thread/read", "thread/list", "thread/resume"]
    assert "turn/start" not in calls


async def test_rpc_classifies_only_exact_unloaded_thread_error():
    async def handler(ws, message):
        await ws.send_json({"id": message["id"], "error": {"code": -32600, "message": "thread is not loaded"}})
    from slack_codex_bridge.desktop import RPC
    async with rpc_server(handler) as endpoint:
        rpc = RPC(endpoint, 1, lambda *_: None)
        await rpc.connect()
        try:
            with pytest.raises(BridgeError, match="backend_thread_not_loaded"):
                await rpc.call("thread/read", {"threadId": "unloaded"})
        finally:
            await rpc.close()
