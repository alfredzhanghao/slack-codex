"""Existing desktop IPC: real framing, pinned ownership and one-attempt writes."""

import asyncio
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
import json
import sqlite3
import struct
import time

import pytest
import pytest_asyncio

from slack_codex_bridge import coordination as ipc, windows
from slack_codex_bridge.desktop import Desktop
from slack_codex_bridge.model import Action, BridgeError
from slack_codex_bridge.supervisor import OperatorAdapter, instance_description


@pytest.fixture
def profile(cfg, tmp_path):
    db = tmp_path / "state_5.sqlite"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE threads (id TEXT,title TEXT,cwd TEXT,archived INT,updated_at INT,source TEXT)")
        c.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?)", [
            ("chat-1", "Owned chat", str(cfg.projects[0]), 0, 3, "vscode"),
            ("archived", "Archived", str(cfg.projects[0]), 1, 2, "cli"),
            ("subagent", "Subagent", str(cfg.projects[0]), 0, 4, '{"subagent":"review"}'),
            ("outside", "Unrelated", str(tmp_path / "project-other"), 0, 4, "vscode")])
    desktop = tmp_path / ipc.DESKTOP_PACKAGE / "app" / "ChatGPT.exe"
    desktop.parent.mkdir(parents=True)
    desktop.write_bytes(b"Fixture desktop; never executed")
    return replace(cfg, native_state_db=db, desktop_exe=desktop,
                   desktop_version=ipc.DESKTOP_VERSION, request_timeout=1)


def state(cfg):
    return {"id": "chat-1", "hostId": "local", "title": "Owned chat", "cwd": str(cfg.projects[0]),
            "environments": [], "resumeState": "resumed", "requests": [], "latestModel": "fixture-model",
            "latestReasoningEffort": "high", "modelProvider": "fixture-provider",
            "currentPermissions": {"approvalPolicy": "on-request", "sandboxPolicy": {"type": "workspaceWrite"}},
            "threadRuntimeStatus": {"type": "idle", "activeFlags": []},
            "turnHistory": {"kind": "canonical", "history": {"isComplete": True, "entitiesByKey": {
                "turn-1": {"turnId": "turn-1", "turnStartedAtMs": 1, "status": "completed", "items": [
                    {"id": "item-1", "type": "agentMessage", "text": "Fixture final", "phase": "final_answer"}]}}}}}


@pytest_asyncio.fixture
async def peer(profile, monkeypatch):
    current = state(profile)
    messages, connections = [], []
    behavior = {"reply_write": True, "spoof": False}

    async def send(w, value):
        data = json.dumps(value).encode()
        # Deliberately split the length prefix and payload across writes.
        data = struct.pack("<I", len(data)) + data
        w.write(data[:2])
        await w.drain()
        w.write(data[2:])
        await w.drain()

    async def serve(r, w):
        connections.append(w)
        try:
            while True:
                n, = struct.unpack("<I", await r.readexactly(4))
                m = json.loads(await r.readexactly(n))
                messages.append(m)
                method = m.get("method")
                if m.get("type") == "broadcast":
                    if m["params"]["following"]:
                        await send(w, {"type": "broadcast", "method": "thread-stream-state-changed", "version": 11,
                            "sourceClientId": "owner", "params": {"hostId": "local", "conversationId": "chat-1",
                            "change": {"type": "snapshot", "revision": 1, "conversationState": current}}})
                    continue
                result, handled = {}, "owner"
                if method == "initialize":
                    result = {"clientId": "follower"}
                elif method == "thread-owner-discovery":
                    result = {"supportsUntrustedAppInput": True}
                elif method == "thread-follower-start-turn":
                    if not behavior["reply_write"]:
                        continue
                    result = {"result": {"turn": {"id": "new-turn"}}}
                elif method == "thread-follower-interrupt-turn":
                    result = {"interruptedTurnId": m["params"]["expectedTurnId"], "ok": True}
                if behavior["spoof"]:
                    handled = "different-owner"
                await send(w, {"type": "response", "method": method, "requestId": m["requestId"],
                               "resultType": "success", "handledByClientId": handled, "result": result})
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            w.close()
            await w.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def open_fixture():
        r, w = await asyncio.open_connection("127.0.0.1", port)
        return r, w, 123

    monkeypatch.setattr(ipc, "open_pipe", open_fixture)
    monkeypatch.setattr(ipc.CoordinationRPC, "guard", lambda _: None)
    monkeypatch.setattr(windows, "_instance_binary_proof", lambda _: {"desktop_hash": ipc.DESKTOP_HASH})
    backend = Desktop(profile, "codex-ipc://10/1000")
    await backend.connect()
    try:
        yield backend, current, messages, behavior
    finally:
        await backend.close()
        for w in connections:
            w.close()
        server.close()
        await server.wait_closed()


def test_catalog_is_read_only_scoped_and_excludes_archived(profile):
    before = profile.native_state_db.read_bytes()
    assert ipc.catalog(profile) == [{"id": "chat-1", "name": "Owned chat", "cwd": str(profile.projects[0])}]
    assert ipc.catalog(profile, "outside") == []
    assert profile.native_state_db.read_bytes() == before


def test_windows_extended_metadata_path_uses_same_allowlist(profile):
    root = str(profile.projects[0])
    if len(root) < 2 or root[1] != ":":
        return  # The fixture uses Windows drive metadata only on Windows.
    with sqlite3.connect(profile.native_state_db) as c:
        c.execute("UPDATE threads SET cwd=? WHERE id='chat-1'", ("\\\\?\\" + root,))
    assert ipc.catalog(profile, "chat-1")[0]["cwd"] == root
    assert ipc.native_path(r"\\?\UNC\server\share") == r"\\server\share"


def test_same_electron_binary_from_different_package_is_not_protocol_proof(profile):
    assert ipc.supported({"desktop_hash": ipc.DESKTOP_HASH}, profile)
    different = replace(profile, desktop_exe=profile.desktop_exe.parent.parent.parent / "different" / "app.exe")
    assert not ipc.supported({"desktop_hash": ipc.DESKTOP_HASH}, different)


@pytest.mark.parametrize("address", ["codex-ipc://10/1000?secret=value", "codex-ipc://10:4/1000",
    "codex-ipc://10/0", "codex-ipc://user@10/1000", "codex-ipc://010/1000", "ws://10/1000"])
def test_endpoint_accepts_only_exact_pid_and_creation_identity(address):
    with pytest.raises(BridgeError, match="unverified_backend_endpoint"):
        ipc.parse_endpoint(address)


async def test_real_framing_reads_existing_state_without_resume_or_task(peer):
    backend, _, messages, _ = peer
    snap = await backend.snapshot("chat-1")
    assert snap.trusted and snap.idle and snap.model == "fixture-model" and snap.effort == "high"
    assert snap.known_turns == {"turn-1"} and snap.terminals == {"turn-1": "completed"}
    assert await backend.final_messages("chat-1", "turn-1") == ("completed", ["Fixture final"])
    assert {m["method"] for m in messages} <= {
        "initialize", "thread-owner-discovery", "thread-stream-following-changed"}


async def test_terminal_readback_requires_time_separated_fresh_snapshots(peer, monkeypatch):
    backend, _, _, _ = peer
    a = await backend.snapshot("chat-1")
    assert not a.confirmed
    # Exercise the existing Desktop recovery logic; a second cache hit is not
    # enough to count as a time-separated terminal readback.
    b = await backend.snapshot("chat-1")
    assert not b.confirmed
    for key, (fingerprint, clock, generation) in list(backend.recovery_reads.items()):
        backend.recovery_reads[key] = (fingerprint, clock - 4, generation)
    backend.rpc.stamps.clear()
    assert (await backend.snapshot("chat-1")).confirmed == {"turn-1"}


async def test_native_start_uses_exact_owner_and_preserves_policy(peer):
    backend, _, messages, _ = peer
    receipt = await backend.send(Action("turn_start", "chat-1", text="Fixture only"))
    assert receipt.turn_id == "new-turn"
    writes = [m for m in messages if m["method"] == "thread-follower-start-turn"]
    assert len(writes) == 1 and writes[0]["version"] == 2 and writes[0]["targetClientId"] == "owner"
    assert writes[0]["params"]["turnStart"] == {"request": {
        "threadId": "chat-1", "input": [{"type": "text", "text": "Fixture only"}]},
        "context": {"inheritThreadSettings": True}}


async def test_lost_write_receipt_is_unknown_and_never_retried(peer):
    backend, _, messages, behavior = peer
    behavior["reply_write"] = False
    with pytest.raises(BridgeError) as error:
        await backend.send(Action("turn_start", "chat-1", text="Fixture only"))
    assert error.value.unknown
    assert sum(m["method"] == "thread-follower-start-turn" for m in messages) == 1


async def test_wrong_owner_response_after_write_is_unknown(peer):
    backend, _, messages, behavior = peer
    original = backend.rpc.request
    async def request(method, *args, **kwargs):
        if method == "thread-follower-start-turn":
            behavior["spoof"] = True
        return await original(method, *args, **kwargs)
    backend.rpc.request = request
    with pytest.raises(BridgeError) as error:
        await backend.send(Action("turn_start", "chat-1", text="Fixture only"))
    assert error.value.unknown
    assert sum(m["method"] == "thread-follower-start-turn" for m in messages) == 1


@pytest.mark.parametrize("change", ["active", "approval", "unloaded", "cwd"])
async def test_untrusted_or_active_snapshot_cannot_start_task(peer, change, tmp_path):
    backend, current, messages, _ = peer
    if change == "active":
        current["threadRuntimeStatus"]["type"] = "active"
    elif change == "approval":
        current["requests"] = [{"id": "desktop-only-approval"}]
    elif change == "unloaded":
        current["resumeState"] = "notResumed"
    else:
        current["cwd"] = str(tmp_path)
    with pytest.raises(BridgeError):
        await backend.send(Action("turn_start", "chat-1", text="Fixture only"))
    assert not any(m["method"] == "thread-follower-start-turn" for m in messages)


@pytest.mark.parametrize("method", ["turn/steer", "thread/fork", "thread/compact/start", "skills/list",
                                   "model/list", "account/rateLimits/read", "account/read", "anything"])
async def test_unmapped_methods_never_reach_pipe(peer, method):
    backend, _, messages, _ = peer
    before = len(messages)
    with pytest.raises(BridgeError, match="unsupported_operation"):
        await backend.rpc.call(method, {"threadId": "chat-1"}, write=True)
    assert len(messages) == before


async def test_interrupt_pins_expected_turn_at_owner(peer):
    backend, _, messages, _ = peer
    assert (await backend.send(Action("interrupt", "chat-1", turn_id="turn-1"))).turn_id == "turn-1"
    m = next(m for m in messages if m["method"] == "thread-follower-interrupt-turn")
    assert m["version"] == 4 and m["params"] == {
        "conversationId": "chat-1", "mode": "user-stop", "expectedTurnId": "turn-1"}


async def test_foreign_broadcast_and_unknown_patch_cannot_supply_trusted_state(peer):
    backend, current, _, _ = peer
    await backend.snapshot("chat-1")
    message = {"type": "broadcast", "method": "thread-stream-state-changed", "version": 11,
               "sourceClientId": "different-owner", "params": {"hostId": "local", "conversationId": "chat-1",
               "change": {"type": "snapshot", "conversationState": dict(current, cwd="forged")}}}
    backend.rpc.broadcast(message)
    assert backend.rpc.states["chat-1"]["cwd"] == current["cwd"]
    message["sourceClientId"] = "owner"
    message["params"]["change"] = {"type": "patches", "patches": ["untrusted patch"]}
    backend.rpc.broadcast(message)
    assert "chat-1" not in backend.rpc.states
    assert (await backend.snapshot("chat-1")).trusted


def native_profile(profile, monkeypatch):
    identity = {"pid": 10, "created": 1000, "path": str(profile.desktop_exe.resolve())}
    proof = {"desktop_hash": ipc.DESKTOP_HASH, "backend_hash": "backend-proof"}
    monkeypatch.setattr(windows, "processes", lambda: [{"pid": 10, "parent": 1,
                                                       "name": profile.desktop_exe.name}])
    monkeypatch.setattr(windows, "tcp_rows", lambda: [])
    monkeypatch.setattr(windows, "process_identity", lambda pid: deepcopy(identity) if pid == 10 else None)
    monkeypatch.setattr(windows, "same_user_session", lambda _: True)
    monkeypatch.setattr(windows, "_instance_binary_proof", lambda _: dict(proof))
    monkeypatch.setattr(windows, "pipe_server_identity", lambda *a: deepcopy(identity))
    monkeypatch.setattr(windows, "single_instance", lambda _: nullcontext())
    monkeypatch.setattr(windows, "detached", lambda *a: pytest.fail("must not launch native processes"))
    return identity, proof


def test_discover_and_confirm_pipe_keeps_original_record_and_database(profile, monkeypatch):
    identity, _ = native_profile(profile, monkeypatch)
    first = windows.discover_instances(profile)[0]
    assert first["connectable"] and first["transport"] == "desktop_ipc" and first["backend"] is None
    assert "IPC" in instance_description(first)
    assert not windows.record_path(profile).exists()
    profile.data_dir.mkdir()
    windows.record_path(profile).write_text("original record")
    db = profile.data_dir / "bridge.sqlite3"
    db.write_bytes(b"unknown operations remain unknown")
    record = windows.adopt_instance(profile, first)
    assert record["endpoint"] == "codex-ipc://10/1000" and record["desktop"] == identity
    assert record["backend"] is None and db.read_bytes() == b"unknown operations remain unknown"
    assert any(p.read_text() == "original record" for p in profile.data_dir.glob("*.previous.json"))


@pytest.mark.parametrize("change", ["pipe_owner", "pid_reused", "hash", "expired", "peer"])
def test_changed_ipc_selection_never_replaces_record(profile, monkeypatch, change):
    identity, proof = native_profile(profile, monkeypatch)
    candidate = windows.discover_instances(profile)[0]
    if change == "pipe_owner":
        monkeypatch.setattr(windows, "pipe_server_identity", lambda *a: None)
    elif change == "pid_reused":
        identity["created"] += 1
    elif change == "hash":
        proof["desktop_hash"] = "changed"
    elif change == "expired":
        candidate["observed_ms"] = int(time.time() * 1000) - 121000
    else:
        candidate["peer"]["created"] += 1
    with pytest.raises(BridgeError):
        windows.adopt_instance(profile, candidate)
    assert not windows.record_path(profile).exists()


def test_ipc_selection_probes_before_persistence(profile, monkeypatch):
    native_profile(profile, monkeypatch)
    candidate = windows.discover_instances(profile)[0]
    adapter = OperatorAdapter.__new__(OperatorAdapter)
    adapter.cfg = profile
    adapter.command = lambda _: {"running": False}
    async def failed(_):
        raise BridgeError("desktop_ipc_initialize_failed")
    adapter.probe = failed
    with pytest.raises(BridgeError, match="initialize_failed"):
        adapter.attach(candidate)
    assert not windows.record_path(profile).exists()


async def test_native_stop_is_refused_for_adopted_desktop(profile, monkeypatch):
    native_profile(profile, monkeypatch)
    windows.adopt_instance(profile, windows.discover_instances(profile)[0])
    with pytest.raises(BridgeError, match="native_lifecycle_not_owned"):
        await windows.stop(profile)


async def test_existing_slack_cards_hide_unavailable_controls_and_explain_scope(setup):
    from test_slack import adapter
    controller, state, backend, _ = setup
    backend.desktop_only_controls = ipc.CoordinationRPC.desktop_only_controls
    slack = adapter(controller, backend)
    state.snapshot.active_turn = "turn-1"
    _, blocks = slack.card(state)
    elements = [e for b in blocks if b.get("type") == "actions" for e in b["elements"]]
    assert not any(e["action_id"] == "bridge_steer" for e in elements)
    assert any(e["action_id"] == "bridge_interrupt" for e in elements)
    assert [e["action_id"] for e in slack.settings_controls(state)] == ["bridge_artifacts"]
    await slack.settings_card(state)
    assert "use the Codex desktop" in slack.web.posts[-1][0]


def test_catalog_refuses_arbitrary_files_before_opening_them(profile, monkeypatch):
    wrong = replace(profile, native_state_db=profile.native_state_db.with_name("credentials.json"))
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: pytest.fail("must not open arbitrary files"))
    with pytest.raises(BridgeError, match="catalog_unavailable"):
        ipc.catalog(wrong)


async def test_selected_project_listing_does_not_include_another_allowed_project(peer, tmp_path):
    backend, _, _, _ = peer
    other = tmp_path / "another-project"
    other.mkdir()
    backend.rpc.cfg = replace(backend.rpc.cfg, projects=backend.rpc.cfg.projects + (other,))
    with sqlite3.connect(backend.rpc.cfg.native_state_db) as c:
        c.execute("INSERT INTO threads VALUES (?,?,?,?,?,?)", ("other", "Other", str(other), 0, 5, "cli"))
    all_rows = await backend.rpc.call("thread/list", {})
    assert {r["id"] for r in all_rows["data"]} == {"chat-1", "other"}
    selected = await backend.rpc.call("thread/list", {"cwd": str(backend.cfg.projects[0])})
    assert [r["id"] for r in selected["data"]] == ["chat-1"]


@pytest.mark.parametrize("method,params,write", [
    ("thread/resume", {"threadId": "chat-1", "excludeTurns": True, "cwd": "unverified"}, False),
    ("thread/resume", {"threadId": "chat-1"}, False),
    ("thread/read", {"threadId": "chat-1", "includeTurns": True}, False),
    ("thread/list", {"archived": True}, False),
    ("thread/list", {"sourceKinds": ["subAgent"]}, False),
    ("thread/turns/list", {"threadId": "chat-1", "limit": 1, "itemsView": "full", "cursor": "unknown"}, False),
    ("turn/start", {"threadId": "chat-1", "input": []}, False)])
async def test_unimplemented_parameter_semantics_are_refused_before_forwarding(peer, method, params, write):
    backend, _, messages, _ = peer
    before = len(messages)
    with pytest.raises(BridgeError, match="unsupported_operation"):
        await backend.rpc.call(method, params, write=write)
    assert len(messages) == before


async def test_catalog_pagination_and_source_filter_keep_native_query_semantics(peer):
    backend, _, _, _ = peer
    with sqlite3.connect(backend.rpc.cfg.native_state_db) as c:
        c.execute("INSERT INTO threads VALUES (?,?,?,?,?,?)",
                  ("cli-chat", "CLI", str(backend.cfg.projects[0]), 0, 1, "cli"))
    first = await backend.rpc.call("thread/list", {"limit": 1})
    second = await backend.rpc.call("thread/list", {"limit": 1, "cursor": first["nextCursor"]})
    assert [r["id"] for r in first["data"] + second["data"]] == ["chat-1", "cli-chat"]
    assert second["nextCursor"] is None
    only_cli = await backend.rpc.call("thread/list", {"sourceKinds": ["cli"]})
    assert [r["id"] for r in only_cli["data"]] == ["cli-chat"]
