"""Operator guards, graceful shutdown and ordered rollback, without Slack access."""
import asyncio
import ctypes
import importlib.util
import os
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("bridge_operator", Path(__file__).parents[1] / "scripts/bridge_operator.py")
op = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(op)


def setup_state(tmp_path):
    value = {"state_dir": str(tmp_path), "version": "test", "expected_worker_exe": str(tmp_path / "pythonw.exe")}
    run_id = str(uuid.uuid4())
    identity = {"pid": 123, "created": 456, "path": value["expected_worker_exe"]}
    op.write(tmp_path / "current.json", {"run_id": run_id})
    path, _, directory = op.state_paths(value)
    op.write(path, {"run_id": run_id, "version": "test", "observed_ms": op.now_ms(), "process": identity})
    return value, identity, directory


@pytest.mark.parametrize("change", [
    {"run_id": "wrong"}, {"action": "kill"}, {"id": "wrong"}, {"expires_ms": 1000},
    {"expires_ms": 121001},
])
def test_shutdown_rejects_unscoped_or_expired(change):
    command = op.shutdown_command("run", 1000)
    command.update(change)
    assert not op.claim_shutdown(command, "run", set(), 1000)


def test_shutdown_is_one_use():
    command = op.shutdown_command("run", 1000)
    claimed = set()
    assert op.claim_shutdown(command, "run", claimed, 1000)
    assert not op.claim_shutdown(command, "run", claimed, 1000)


@pytest.mark.parametrize("change", [{"version": "wrong"}, {"observed_ms": 0}, {"process_path": "other.exe"}])
async def test_unverified_target_never_receives_stop(tmp_path, change):
    value, identity, directory = setup_state(tmp_path)
    state = op.read(directory / "status.json")
    if "process_path" in change:
        identity["path"] = change.pop("process_path")
        state["process"] = identity
    state.update(change)
    op.write(directory / "status.json", state)
    with pytest.raises(op.OperatorError, match="operator_stop_target_unverified"):
        await op.stop_instance(value, SimpleNamespace(process_identity=lambda _: identity))
    assert not (directory / "command.json").exists()


async def test_pid_reuse_does_not_send_shutdown(tmp_path):
    value, identity, directory = setup_state(tmp_path)
    result = await op.stop_instance(value, SimpleNamespace(process_identity=lambda _: {**identity, "created": 999}))
    assert result["already_stopped"]
    assert not (directory / "command.json").exists()


async def test_graceful_stop_proves_same_handle_exit(tmp_path):
    value, identity, directory = setup_state(tmp_path)
    @contextmanager
    def handle(windows, expected):
        assert expected == identity
        yield lambda: (directory / "command.json").exists()
    result = await op.stop_instance(value, SimpleNamespace(process_identity=lambda _: identity), handle_factory=handle)
    assert result["same_handle_exit"]
    assert op.read(directory / "stop-proof.json") == result


async def test_stop_timeout_never_forces_process(tmp_path):
    value, identity, directory = setup_state(tmp_path)
    @contextmanager
    def handle(*args):
        yield lambda: False
    with pytest.raises(op.OperatorError, match="operator_stop_timeout_no_force"):
        await op.stop_instance(value, SimpleNamespace(process_identity=lambda _: identity), timeout=0, handle_factory=handle)
    assert not (directory / "stop-proof.json").exists()


def test_pending_launch_blocks_second_consumer(tmp_path):
    value, identity, directory = setup_state(tmp_path)
    (directory / "status.json").unlink()
    op.write(directory / "launch-intent.json", {})
    windows = SimpleNamespace(process_identity=lambda _: identity)
    assert op.status(value, windows)["running"]
    op.write(directory / "launch.json", {"process": identity})
    assert op.status(value, windows)["running"]
    assert not op.status(value, SimpleNamespace(process_identity=lambda _: None))["running"]


async def test_start_refuses_a_running_profile_before_loading_credentials(tmp_path):
    value, identity, _ = setup_state(tmp_path)
    @contextmanager
    def lock(name):
        yield
    windows = SimpleNamespace(process_identity=lambda _: identity, single_instance=lock)
    with pytest.raises(op.OperatorError, match="operator_bridge_already_running"):
        await op.start_instance(tmp_path / "deployment.json", {"host_id": "test", "profiles": {"p2": value}},
                                "p4", value, windows)


@pytest.mark.parametrize("changed", ["config", "module"])
def test_profile_rejects_changed_bytes(tmp_path, changed):
    config = tmp_path / "config.json"
    config.write_text("{}")
    runtime = tmp_path / "slack_codex_bridge"
    runtime.mkdir()
    module = runtime / "__init__.py"
    module.write_text("__version__='test'")
    value = {"expected_worker_exe": str(tmp_path / "pythonw.exe"), "config": str(config),
             "config_sha256": op.sha(config), "runtime": str(tmp_path),
             "module_sha256": {module.name: op.sha(module)}}
    assert op.profile({"profiles": {"p4": value}}, "p4") == value
    (config if changed == "config" else module).write_text("changed")
    with pytest.raises(op.OperatorError, match="operator_(config|package)_changed"):
        op.profile({"profiles": {"p4": value}}, "p4")


def test_rollback_stops_before_start_and_aborts_if_stop_fails(monkeypatch):
    calls = []
    def tool(path, name, command, python):
        calls.append((name, command))
        if command == "stop":
            raise op.OperatorError("operator_stop_timeout_no_force")
    monkeypatch.setattr(op, "run_tool", tool)
    deployment = {"profiles": {"p4": {"python": "p4"}, "p2": {"python": "p2"}}}
    with pytest.raises(op.OperatorError):
        op.rollback("manifest", deployment)
    assert calls == [("p4", "stop")]
    monkeypatch.setattr(op, "run_tool", lambda path, name, command, python: calls.append((name, command)))
    op.rollback("manifest", deployment)
    assert calls[-2:] == [("p4", "stop"), ("p2", "start")]


async def test_monitor_failure_cancels_worker(tmp_path, monkeypatch):
    probe = op.OperatorProbe(tmp_path, str(uuid.uuid4()), "test", None)
    cancelled = asyncio.Event()
    async def root():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    async def monitor(task):
        await asyncio.sleep(0)
        raise ValueError("private detail must not enter status")
    monkeypatch.setattr(probe, "monitor", monitor)
    monkeypatch.setattr(probe, "snapshot", lambda: {"fault": None})
    await probe.manage(root())
    assert cancelled.is_set()
    assert op.read(tmp_path / "status.json")["fault"] == "operator_worker_failed"
    failure = op.read(tmp_path / "failure.json")
    assert failure["exception_type"] == "ValueError"
    assert "private detail" not in str(failure)


def test_atomic_write_tolerates_transient_windows_reader(tmp_path, monkeypatch):
    original = Path.replace
    attempts = []
    def replace(path, destination):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("Windows sharing violation")
        return original(path, destination)
    monkeypatch.setattr(Path, "replace", replace)
    op.write(tmp_path / "status.json", {"ready": True})
    assert len(attempts) == 3 and op.read(tmp_path / "status.json")["ready"]


def test_atomic_write_still_fails_closed_for_persistent_error(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "replace", lambda *args: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(PermissionError):
        op.write(tmp_path / "status.json", {})


@pytest.mark.desktop_host
@pytest.mark.skipif(os.name != "nt", reason="Windows process identity")
async def test_real_windowless_shutdown(tmp_path):
    from slack_codex_bridge import windows
    value, _, directory = setup_state(tmp_path)
    (directory / "status.json").unlink()
    run_id = op.read(tmp_path / "current.json")["run_id"]
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import asyncio, importlib.util, sys\n"
        f"sys.path.insert(0, {str(Path(__file__).parents[1] / 'src')!r})\n"
        "from slack_codex_bridge import windows\n"
        f"s=importlib.util.spec_from_file_location('operator', {str(Path(op.__file__))!r})\n"
        "o=importlib.util.module_from_spec(s); s.loader.exec_module(o)\n"
        f"p=o.OperatorProbe({str(directory)!r}, {run_id!r}, 'test', windows)\n"
        "asyncio.run(p.manage(asyncio.Event().wait()))\n", encoding="utf-8")
    env = {k: os.environ[k] for k in ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP") if k in os.environ}
    launcher = windows.detached([sys.executable, str(worker)], env)
    for _ in range(100):
        if (directory / "status.json").exists():
            break
        await asyncio.sleep(.1)
    state = op.read(directory / "status.json")
    value["expected_worker_exe"] = state["process"]["path"]
    result = await op.stop_instance(value, windows)
    assert result["same_handle_exit"]
    assert windows.process_identity(state["process"]["pid"]) is None
    for _ in range(100):
        if windows.process_identity(launcher["pid"]) is None:
            break
        await asyncio.sleep(.1)
    assert windows.process_identity(launcher["pid"]) is None
    console = subprocess.run([sys.executable, "-c", "import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())"],
                             capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    assert console.returncode == 0 and console.stdout.strip() == "0"


@pytest.mark.desktop_host
@pytest.mark.skipif(os.name != "nt", reason="Windows sharing modes")
def test_real_windows_reader_does_not_stop_status_writer(tmp_path):
    from ctypes import wintypes as wt
    path = tmp_path / "status.json"
    op.write(path, {"ready": False})
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                  wt.DWORD, wt.DWORD, wt.HANDLE]
    kernel.CreateFileW.restype = wt.HANDLE
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    replacement = tmp_path / "old-writer.tmp"
    replacement.write_text("{}")
    with pytest.raises(PermissionError):
        replacement.replace(path)
    timer = threading.Timer(.075, lambda: kernel.CloseHandle(handle))
    timer.start()
    try:
        op.write(path, {"ready": True})
    finally:
        timer.join()
    assert op.read(path)["ready"]
