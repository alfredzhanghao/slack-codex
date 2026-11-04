"""Explicit reuse of an existing native chain; no real desktop lifecycle."""

from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import asyncio
import os
import subprocess
import sys

import pytest

from slack_codex_bridge import windows
from slack_codex_bridge.model import BridgeError
from slack_codex_bridge.supervisor import OperatorAdapter


@pytest.fixture
def native(cfg, monkeypatch):
    cfg.desktop_exe.write_bytes(b"desktop fixture")
    cfg.backend_exe.write_bytes(b"backend fixture")
    identities = {
        pid: {"pid": pid, "created": 1000 + pid, "path": str(path.resolve())}
        for pid, path in [(10, cfg.desktop_exe), (11, cfg.desktop_exe),
                          (20, cfg.backend_exe), (30, cfg.desktop_exe), (40, cfg.backend_exe)]
    }
    inventory = [{"pid": p, "parent": parent, "name": path.name}
                 for p, parent, path in [(10, 1, cfg.desktop_exe), (11, 10, cfg.desktop_exe),
                                         (20, 10, cfg.backend_exe), (30, 1, cfg.desktop_exe),
                                         (40, 30, cfg.backend_exe)]]
    rows = []
    for backend, peer, port in [(20, 11, 15000), (40, 30, 16000)]:
        rows.extend([["TCP", f"127.0.0.1:{port}", "0.0.0.0:0", "LISTENING", str(backend)],
                     ["TCP", f"127.0.0.1:{port}", f"127.0.0.1:{port+1}", "ESTABLISHED", str(backend)],
                     ["TCP", f"127.0.0.1:{port+1}", f"127.0.0.1:{port}", "ESTABLISHED", str(peer)]])
    monkeypatch.setattr(windows, "processes", lambda: list(inventory))
    monkeypatch.setattr(windows, "process_identity", lambda pid: deepcopy(identities.get(pid)))
    monkeypatch.setattr(windows, "tcp_rows", lambda: deepcopy(rows))
    monkeypatch.setattr(windows, "verify_versions", lambda _: None)
    monkeypatch.setattr(windows, "same_user_session", lambda _: True)
    monkeypatch.setattr(windows, "single_instance", lambda _: nullcontext())
    monkeypatch.setattr(windows, "detached", lambda *_: pytest.fail("selection must never launch native processes"))
    return identities, rows


def test_discovery_lists_roots_and_exact_backends_without_writing_or_launching(cfg, native):
    candidates = windows.discover_instances(cfg)
    assert {(c["desktop"]["pid"], c["backend"]["pid"], c["port"]) for c in candidates} == {
        (10, 20, 15000), (30, 40, 16000)}
    assert all(c["connectable"] for c in candidates)
    assert not windows.record_path(cfg).exists()


def test_confirm_selected_chain_preserves_previous_record_and_operation_store(cfg, native):
    first, second = windows.discover_instances(cfg)
    windows.adopt_instance(cfg, first)
    original = windows.record_path(cfg).read_bytes()
    database = cfg.data_dir / "bridge.sqlite3"
    database.write_bytes(b"historical unknown operations fixture")
    result = windows.adopt_instance(cfg, second)
    assert result["desktop"]["pid"] == 30 and result["backend"]["pid"] == 40
    assert result["peer"]["pid"] == 30 and result["endpoint"] == "ws://127.0.0.1:16000"
    assert database.read_bytes() == b"historical unknown operations fixture"
    assert any(p.read_bytes() == original for p in cfg.data_dir.glob("desktop-session.*.previous.json"))


@pytest.mark.parametrize("change", ["desktop_pid_reused", "backend_pid_reused", "listener", "peer",
                                   "peer_pid_reused", "binary", "expired", "future_snapshot"])
def test_confirmation_revalidates_snapshot_before_any_record_write(cfg, native, change):
    identities, rows = native
    candidate = windows.discover_instances(cfg)[0]
    if change == "desktop_pid_reused":
        identities[10]["created"] += 1
    elif change == "backend_pid_reused":
        identities[20]["created"] += 1
    elif change == "listener":
        rows[0][4] = "40"
    elif change == "peer":
        rows[2][4] = "30"
    elif change == "peer_pid_reused":
        identities[11]["created"] += 1
    elif change == "binary":
        cfg.backend_exe.write_bytes(b"changed binary")
    elif change == "future_snapshot":
        candidate["observed_ms"] += 60000
    else:
        candidate["observed_ms"] -= 121000
    with pytest.raises(BridgeError):
        windows.adopt_instance(cfg, candidate)
    assert not windows.record_path(cfg).exists()


def test_nonshared_desktop_is_visible_but_never_adopted(cfg, native):
    _, rows = native
    rows.clear()
    candidates = windows.discover_instances(cfg)
    assert len(candidates) == 2
    assert all(not c["connectable"] and c["reason"] == "no_shared_endpoint" for c in candidates)
    with pytest.raises(BridgeError, match="not_connectable"):
        windows.adopt_instance(cfg, candidates[0])
    assert not windows.record_path(cfg).exists()


def test_existing_uncertain_native_launch_is_never_overwritten(cfg, native):
    candidate = windows.discover_instances(cfg)[0]
    record = windows.adopt_instance(cfg, candidate)
    record["status"] = "restoring"
    windows.save_record(cfg, record)
    previous = windows.record_path(cfg).read_bytes()
    with pytest.raises(BridgeError, match="previous_start_unconfirmed"):
        windows.adopt_instance(cfg, candidate)
    assert windows.record_path(cfg).read_bytes() == previous


def test_discovery_reports_incompatible_version_without_native_effects(cfg, native, monkeypatch):
    def incompatible(_):
        raise BridgeError("version_mismatch_writes_disabled")
    monkeypatch.setattr(windows, "verify_versions", incompatible)
    candidates = windows.discover_instances(cfg)
    assert all(not c["connectable"] and c["reason"] == "version_mismatch_writes_disabled" for c in candidates)
    assert not windows.record_path(cfg).exists()


@pytest.mark.parametrize("pid", [10, 20, 11])
def test_owner_or_login_session_change_never_replaces_connection(cfg, native, monkeypatch, pid):
    candidate = windows.discover_instances(cfg)[0]
    monkeypatch.setattr(windows, "same_user_session", lambda target: target != pid)
    with pytest.raises(BridgeError, match="different_user_or_session"):
        windows.adopt_instance(cfg, candidate)
    assert not windows.record_path(cfg).exists()


def test_unreadable_backend_owner_does_not_hide_another_valid_instance(cfg, native, monkeypatch):
    def visible(pid):
        if pid == 20:
            raise BridgeError("instance_owner_unavailable")
        return True
    monkeypatch.setattr(windows, "same_user_session", visible)
    candidates = windows.discover_instances(cfg)
    assert any(c["desktop"]["pid"] == 30 and c["connectable"] for c in candidates)
    assert not any(c["desktop"]["pid"] == 10 and c["connectable"] for c in candidates)


def test_protocol_failure_does_not_replace_native_record(cfg, native):
    a = OperatorAdapter.__new__(OperatorAdapter)
    a.cfg = cfg
    a.command = lambda _: {"running": False}
    async def rejected(_):
        raise BridgeError("backend_connect_failed")
    a.probe = rejected
    with pytest.raises(BridgeError, match="backend_connect_failed"):
        a.attach(windows.discover_instances(cfg)[0])
    assert not windows.record_path(cfg).exists()


def test_protocol_probe_revalidates_peer_before_persisting(cfg, native):
    identities, _ = native
    a = OperatorAdapter.__new__(OperatorAdapter)
    a.cfg = cfg
    a.command = lambda _: {"running": False}
    async def changed(_):
        identities[11]["created"] += 1
    a.probe = changed
    with pytest.raises(BridgeError, match="peer_changed"):
        a.attach(windows.discover_instances(cfg)[0])
    assert not windows.record_path(cfg).exists()


async def test_native_protocol_probe_sends_only_initialize_without_task_requests(cfg):
    from aiohttp import web
    methods = []
    async def endpoint(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        first = await ws.receive_json()
        methods.append(first["method"])
        await ws.send_json({"id": first["id"], "result": {"userAgent": "synthetic"}})
        async for frame in ws:
            methods.append(frame.json()["method"])
        return ws
    app = web.Application()
    app.router.add_get("/", endpoint)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    a = OperatorAdapter.__new__(OperatorAdapter)
    a.cfg = cfg
    try:
        port = site._server.sockets[0].getsockname()[1]
        await a.probe(f"ws://127.0.0.1:{port}")
    finally:
        await runner.cleanup()
    assert methods == ["initialize", "initialized"]


@pytest.mark.skipif(os.name != "nt", reason="Windows process identity and actual TCP correlation")
async def test_existing_real_windows_tcp_pair_is_adopted_without_process_restart(cfg, monkeypatch):
    """Two owned Python fixtures stand in for native peers; no Codex/Slack access."""
    executable = Path(sys._base_executable).resolve()
    server_code = ("import socket,sys; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); "
                   "print(s.getsockname()[1],flush=True); c,_=s.accept(); print('accepted',flush=True); "
                   "sys.stdin.buffer.read(1)")
    client_code = ("import socket,sys; c=socket.create_connection(('127.0.0.1',int(sys.argv[1]))); "
                   "print('connected',flush=True); sys.stdin.buffer.read(1)")
    children = []
    try:
        server = await asyncio.create_subprocess_exec(str(executable), "-u", "-c", server_code,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        children.append(server)
        port = int(await asyncio.wait_for(server.stdout.readline(), 10))
        client = await asyncio.create_subprocess_exec(str(executable), "-u", "-c", client_code, str(port),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        children.append(client)
        assert await asyncio.wait_for(client.stdout.readline(), 10) == b"connected\r\n"
        assert await asyncio.wait_for(server.stdout.readline(), 10) == b"accepted\r\n"
        before = {p.pid: windows.process_identity(p.pid) for p in children}
        monkeypatch.setattr(windows, "processes", lambda: [
            {"pid": p.pid, "parent": os.getpid(), "name": executable.name} for p in children])
        monkeypatch.setattr(windows, "detached", lambda *_: pytest.fail("must not launch a native process"))
        cfg = replace(cfg, desktop_exe=executable, backend_exe=executable,
                      desktop_version=windows.file_version(executable), backend_version="Python " + sys.version.split()[0])
        candidates = windows.discover_instances(cfg)
        selected = next((c for c in candidates if c["connectable"] and c["desktop"]["pid"] == client.pid), None)
        assert selected is not None, candidates
        result = windows.adopt_instance(cfg, selected)
        assert result["backend"] == before[server.pid] and result["peer"] == before[client.pid]
        assert result["port"] == port
        assert all(windows.process_identity(pid) == identity for pid, identity in before.items())
    finally:
        for process in children:
            process.stdin.close()
        for process in children:
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.terminate()
                await process.wait()
