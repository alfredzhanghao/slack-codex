import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from dataclasses import replace

import pytest

from slack_codex_bridge import windows
from slack_codex_bridge import __version__
from slack_codex_bridge.cli import main
from slack_codex_bridge.config import child_environment, load
from slack_codex_bridge.model import BridgeError, new_id


def config_file(cfg, path):
    cfg.backend_exe.touch()
    cfg.desktop_exe.touch()
    path.write_text(json.dumps({"host_id": cfg.host_id, "data_dir": str(cfg.data_dir),
        "slack": {"app_id": cfg.app_id, "team_id": cfg.team_id, "owner_user_id": cfg.user_id,
                  "dm_channel_id": cfg.dm_id},
        "desktop": {"entry_route": "verified_controlled_start", "backend_exe": str(cfg.backend_exe),
                    "desktop_exe": str(cfg.desktop_exe), "backend_version": cfg.backend_version,
                    "desktop_version": cfg.desktop_version, "project_allowlist": list(map(str, cfg.projects))}}),
                    encoding="utf-8")
    return path


def test_config_allowlist_and_data_outside_project(cfg, tmp_path):
    path = config_file(cfg, tmp_path / "config.json")
    actual = load(path)
    assert actual.allows(str(cfg.projects[0]))
    assert not actual.allows(str(tmp_path))
    assert not actual.allows(str(tmp_path / "missing"))
    value = json.loads(path.read_text())
    value["data_dir"] = str(cfg.projects[0] / "private")
    path.write_text(json.dumps(value))
    with pytest.raises(BridgeError, match="data_dir_must_be_outside_projects"):
        load(path)


def test_credentials_env_only_and_not_in_child_or_output(cfg, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-synthetic-example")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-synthetic-example")
    assert cfg.credentials()[0].startswith("xapp-")
    env = child_environment(cfg)
    assert "SLACK_APP_TOKEN" not in env and "SLACK_BOT_TOKEN" not in env
    path = config_file(cfg, tmp_path / "config.json")
    value = json.loads(path.read_text())
    value["unexpected"] = "xapp-synthetic-example"
    path.write_text(json.dumps(value))
    assert main(["--config", str(path), "doctor"]) == 1
    output = capsys.readouterr()
    assert "credential_values_forbidden" in output.err
    assert "synthetic-example" not in output.err + output.out


def test_cli_help_version_and_module():
    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0
    result = subprocess.run([sys.executable, "-m", "slack_codex_bridge", "--version"],
                            text=True, capture_output=True, check=True,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert result.stdout.strip() == __version__


@pytest.mark.skipif(os.name != "nt", reason="Windows identity primitive")
def test_native_identity_inventory_and_single_instance():
    identity = windows.process_identity(os.getpid())
    assert identity["pid"] == os.getpid() and identity["created"] > 0
    assert identity == windows.process_identity(os.getpid())
    assert os.getpid() in {p["pid"] for p in windows.processes()}
    with windows.single_instance("test-" + (name := new_id())):
        with pytest.raises(BridgeError, match="another_instance"):
            with windows.single_instance("test-" + name):
                pytest.fail("second instance entered")


@pytest.mark.skipif(os.name != "nt", reason="Windows retained exited process handle")
def test_exited_process_with_retained_handle_has_no_live_identity():
    child = subprocess.Popen([sys.executable, "-I", "-c", "pass"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    assert child.wait(timeout=10) == 0
    # Popen retains its native process handle after wait(), so OpenProcess can still
    # succeed even though the process has disappeared from the live inventory.
    assert windows.process_identity(child.pid) is None


@pytest.mark.parametrize("code", ["process_path_unavailable", "process_start_time_unavailable"])
@pytest.mark.parametrize("after", [0, 258, 0xFFFFFFFF])
def test_identity_exit_race_requires_same_signaled_handle(monkeypatch, code, after):
    class Call:
        def __init__(self, fn):
            self.fn = fn

        def __call__(self, *args):
            return self.fn(*args)

    from types import SimpleNamespace

    waits, closed = [], []
    results = iter([258, after])

    def wait(handle, timeout):
        waits.append((handle, timeout))
        return next(results)

    kernel = SimpleNamespace(OpenProcess=Call(lambda *_: 123), WaitForSingleObject=Call(wait),
        CloseHandle=Call(lambda handle: closed.append(handle)))
    monkeypatch.setattr(windows, "_kernel", lambda: kernel)

    def read(*_):
        raise BridgeError(code)

    monkeypatch.setattr(windows, "_handle_identity", read)
    if after == 0:
        assert windows.process_identity(42) is None
    else:
        with pytest.raises(BridgeError, match=code):
            windows.process_identity(42)
    assert waits == [(123, 0), (123, 0)] and closed == [123]


def test_existing_desktop_refuses_launch_without_touching_processes(cfg, monkeypatch):
    monkeypatch.setattr(windows, "verify_versions", lambda _: None)
    monkeypatch.setattr(windows, "processes", lambda: [{"name": cfg.desktop_exe.name, "pid": 123}])
    launched = []
    monkeypatch.setattr(windows, "detached", lambda *args: launched.append(args))
    with pytest.raises(BridgeError, match="desktop_already_open"):
        windows.start(cfg)
    assert not launched


def test_correlate_searches_all_clients_including_bridge(cfg, monkeypatch):
    rows = [
        ["TCP", "127.0.0.1:1234", "127.0.0.1:8000", "ESTABLISHED", "10"],
        ["TCP", "127.0.0.1:8000", "127.0.0.1:1234", "ESTABLISHED", "20"],
        ["TCP", "127.0.0.1:1234", "127.0.0.1:8001", "ESTABLISHED", "10"],
        ["TCP", "127.0.0.1:8001", "127.0.0.1:1234", "ESTABLISHED", "31"]]
    monkeypatch.setattr(windows, "tcp_rows", lambda: rows)
    monkeypatch.setattr(windows, "processes", lambda: [{"pid": 31, "parent": 30}, {"pid": 20, "parent": 1}])
    monkeypatch.setattr(windows, "process_identity", lambda pid: {"pid": pid})
    assert windows.correlate({"port": 1234, "backend": {"pid": 10}, "desktop": {"pid": 30}}) == {"pid": 31}


async def test_stop_refused_while_bridge_running(cfg):
    with windows.single_instance(cfg.host_id + "-run"):
        with pytest.raises(BridgeError, match="another_instance"):
            await windows.stop(cfg)


async def test_stop_refused_while_desktop_open(cfg, monkeypatch):
    monkeypatch.setattr(windows, "read_record", lambda _: {})
    monkeypatch.setattr(windows, "processes", lambda: [{"name": cfg.desktop_exe.name}])
    with pytest.raises(BridgeError, match="desktop_still_open"):
        await windows.stop(cfg)


@pytest.mark.parametrize("wait_result", [0, 258])
async def test_owned_stop_saves_stopped_only_after_kernel_exit(cfg, monkeypatch, wait_result):
    from contextlib import nullcontext
    from types import SimpleNamespace

    class Call:
        def __init__(self, fn):
            self.fn = fn

        def __call__(self, *args):
            return self.fn(*args)

    record = {"backend": {"pid": 10, "path": str(cfg.backend_exe.resolve())},
        "backend_hash": "hash", "endpoint": "local"}
    saved, checks = [], []

    def wait(handle, milliseconds):
        checks.append((handle, milliseconds))
        assert saved == []
        return wait_result

    def open_process(access, *_):
        assert access & 0x100000
        return 123

    kernel = SimpleNamespace(OpenProcess=Call(open_process), TerminateProcess=Call(lambda *_: True),
        WaitForSingleObject=Call(wait), CloseHandle=Call(lambda *_: None))
    monkeypatch.setattr(windows, "_kernel", lambda: kernel)
    monkeypatch.setattr(windows, "single_instance", lambda _: nullcontext())
    monkeypatch.setattr(windows, "read_record", lambda _: record)
    monkeypatch.setattr(windows, "processes", lambda: [])
    monkeypatch.setattr(windows, "process_identity", lambda _: record["backend"])
    monkeypatch.setattr(windows, "binary_hash", lambda _: "hash")
    monkeypatch.setattr(windows, "_handle_identity", lambda *_: record["backend"])
    monkeypatch.setattr(windows, "save_record", lambda *_: saved.append(record.copy()))

    class RPC:
        def __init__(self, *_):
            pass

        async def connect(self):
            pass

        async def call(self, *_):
            return {"data": []}

        async def close(self):
            pass

    import slack_codex_bridge.desktop
    monkeypatch.setattr(slack_codex_bridge.desktop, "RPC", RPC)
    if wait_result == 0:
        await windows.stop(cfg)
        assert saved[0]["status"] == "stopped"
    else:
        with pytest.raises(BridgeError, match="owned_backend_exit_unproven"):
            await windows.stop(cfg)
        assert saved == []
    assert checks == [(123, 10000)]


@pytest.mark.skipif(os.name != "nt", reason="Windows detached lifecycle")
@pytest.mark.desktop_host
async def test_detached_child_survives_launcher_exit(cfg, tmp_path):
    identity_path = tmp_path / "identity.json"
    heartbeat_path = tmp_path / "child-alive"
    child_code = ("import time, pathlib, sys, ctypes; time.sleep(1); "
                  "pathlib.Path(sys.argv[1]).write_text(str(ctypes.windll.kernel32.GetConsoleWindow()))")
    helper_code = ("import json, sys, os; from pathlib import Path; from slack_codex_bridge.windows import detached; "
                   "sys.path.insert(0, sys.argv[4]); from p3_launch_diagnostic import trace_launch\n"
                   "with trace_launch():\n"
                   "    identity=detached([sys.executable, '-c', sys.argv[2], sys.argv[3]], dict(os.environ))\n"
                   "Path(sys.argv[1]).write_text(json.dumps(identity))")
    helper = await asyncio.create_subprocess_exec(sys.executable, "-c", helper_code, str(identity_path),
                                                   child_code, str(heartbeat_path), str(Path(__file__).resolve().parents[1] / "scripts"),
                                                     env=child_environment(cfg),
                                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                     creationflags=subprocess.CREATE_NO_WINDOW)
    stdout, stderr = await helper.communicate()
    # Do not skip unsupported Windows Job breakaway: a failure must remain visible.
    assert helper.returncode == 0, (
        "detached launcher failed under the current Windows job: " + stderr.decode(errors="replace")[-2000:])
    assert not stdout and not stderr
    assert json.loads(identity_path.read_text())["created"] > 0
    for _ in range(30):
        if heartbeat_path.exists():
            break
        await asyncio.sleep(0.1)
    assert heartbeat_path.read_text() == "0", "background Python must not allocate a console"


def test_launch_diagnostic_reports_only_fixed_fields_and_preserves_error(monkeypatch, capsys):
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts/p3_launch_diagnostic.py"
    spec = importlib.util.spec_from_file_location("launch_diagnostic", path)
    diagnostic = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diagnostic)
    calls = []
    error = OSError(13, "synthetic exception text not for logs")
    error.winerror = 5

    def fail(*args, **kwargs):
        calls.append(True)
        raise error
    monkeypatch.setattr(windows.subprocess, "Popen", fail)
    monkeypatch.setattr(diagnostic, "job_context", lambda: {"in_job": True, "job_limit_flags": 0})
    with diagnostic.trace_launch(), pytest.raises(OSError) as caught:
        windows.subprocess.Popen(["unlogged-command"], env={"unlogged-environment": "fixture"}, creationflags=0x1000000)
    evidence = json.loads(capsys.readouterr().err.split("=", 1)[1])
    assert evidence == {"in_job": True, "job_limit_flags": 0, "winerror": 5, "errno": 13, "creationflags": 0x1000000}
    assert caught.value is error and calls == [True] and windows.subprocess.Popen is fail


@pytest.mark.skipif(os.name != "nt", reason="Windows Job policy")
def test_detached_refuses_restrictive_job_without_fallback(cfg, tmp_path):
    import importlib.util

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    spec = importlib.util.spec_from_file_location("launch_job_fixture", scripts / "p3_launch_diagnostic.py")
    diagnostic = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diagnostic)
    marker = tmp_path / "unexpected-child"
    child_code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('unexpected')"
    helper_code = (
        "import json, os, sys; sys.path.insert(0, sys.argv[1]); "
        "sys.path.insert(0, sys.argv[4]); "
        "from p3_launch_diagnostic import trace_launch; "
        "from slack_codex_bridge.windows import detached; from slack_codex_bridge.model import BridgeError\n"
        "assert sys.stdin.read(2) == 'go'\n"
        "try:\n"
        "    with trace_launch():\n"
        "        detached([sys.executable, '-c', sys.argv[2], sys.argv[3]], dict(os.environ))\n"
        "except BridgeError as error:\n"
        "    print(json.dumps({'code': error.code}))\n"
        "else:\n"
        "    raise AssertionError('restricted launch must refuse rather than fall back')\n")
    # The venv launcher may create the interpreter before assignment. Launch the
    # actual interpreter so the gated process handle is the one assigned to our job.
    child = subprocess.Popen([sys._base_executable, "-c", helper_code, str(scripts), child_code, str(marker),
                              str(Path(__file__).resolve().parents[1] / "src")],
                             env=child_environment(cfg), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        # Python starts, but it cannot launch a child until assigned to our owned job.
        with diagnostic.restrictive_job(child):
            stdout, stderr = child.communicate(b"go", timeout=10)
        assert child.returncode == 0, stderr.decode(errors="replace")
        assert json.loads(stdout) == {"code": "detached_launch_failed_use_normal_terminal"}
        evidence = json.loads(stderr.decode().strip().removeprefix("P3_LAUNCH_DIAGNOSTIC="))
        assert evidence["winerror"] == 5 and evidence["in_job"]
        assert not evidence["breakaway_ok"] and not evidence["silent_breakaway_ok"]
        assert evidence["creationflags"] & 0x01000000  # No silent inherited-job fallback.
        assert not marker.exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


def test_python_detached_uses_matching_windowless_launcher(tmp_path, monkeypatch):
    python = tmp_path / "python.exe"
    pythonw = tmp_path / "pythonw.exe"
    pythonw.touch()
    observed = {}
    monkeypatch.setattr(windows, "require_windows", lambda: None)

    def launch(args, **kwargs):
        observed.update(args=args, **kwargs)
        return type("Child", (), {"pid": 123})()

    monkeypatch.setattr(windows.subprocess, "Popen", launch)
    monkeypatch.setattr(windows, "process_identity", lambda _: {"pid": 123})
    windows.detached([str(python), "helper.py"], {})
    assert Path(observed["args"][0]) == pythonw
    assert observed["args"][1:] == ["helper.py"]
    assert observed["creationflags"] & subprocess.DETACHED_PROCESS


def test_python_detached_refuses_missing_windowless_launcher(tmp_path, monkeypatch):
    monkeypatch.setattr(windows, "require_windows", lambda: None)
    monkeypatch.setattr(windows.subprocess, "Popen", lambda *_a, **_k: pytest.fail("must not launch console"))
    with pytest.raises(BridgeError, match="windowless_python_required"):
        windows.detached([str(tmp_path / "python.exe"), "helper.py"], {})


def test_version_mismatch_disables_writes(cfg, monkeypatch):
    monkeypatch.setattr(windows.subprocess, "run", lambda *a, **k: type("Result", (), {"stdout": "wrong"})())
    with pytest.raises(BridgeError, match="version_mismatch_writes_disabled"):
        windows.verify_versions(cfg)


def test_session_versions_and_pid_creation_time_must_match(cfg, monkeypatch):
    record = {"status": "verified", "backend_version": cfg.backend_version,
              "desktop_version": cfg.desktop_version, "backend": {"pid": 10, "created": 1,
              "path": str(cfg.backend_exe.resolve())}, "desktop": {"pid": 30,
              "created": 2, "path": str(cfg.desktop_exe.resolve())}}
    monkeypatch.setattr(windows, "read_record", lambda _: record)
    monkeypatch.setattr(windows, "process_identity", lambda _: {"pid": 10, "created": 999,
                                                               "path": str(cfg.backend_exe.resolve())})
    with pytest.raises(BridgeError, match="backend_process_identity_changed"):
        windows.verify_record(cfg, check_binaries=False)
    with pytest.raises(BridgeError, match="session_version_changed"):
        windows.verify_record(replace(cfg, desktop_version="different"), check_binaries=False)
