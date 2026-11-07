import importlib.machinery
import importlib.util
import json
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from slack_codex_bridge import supervisor, windows
from slack_codex_bridge.model import BridgeError


def setup_launcher(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / 'scripts/launch_supervisor.pyw'
    loader = importlib.machinery.SourceFileLoader('reusable_launcher_test', str(source))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    (tmp_path / 'current-run.json').write_text(json.dumps({'candidate_runtime':str(tmp_path),
        'active_deployment':str(tmp_path / 'deployment.json'),'active_operator':str(tmp_path / 'operator.py')}))
    monkeypatch.setattr(sys, 'path', list(sys.path))
    monkeypatch.setattr(supervisor, 'OperatorAdapter', lambda *_: SimpleNamespace(cfg=SimpleNamespace(host_id='test-host')))
    return module


def test_launcher_opens_window_with_stopped_worker_and_no_credential_file_access(tmp_path, monkeypatch):
    launcher = setup_launcher(tmp_path, monkeypatch)
    monkeypatch.setattr(windows, 'single_instance', lambda _: nullcontext())
    shown = []
    monkeypatch.setattr(supervisor, 'show_window', shown.append)
    launcher.main(['--run-dir',str(tmp_path)])
    assert len(shown) == 1 and shown[0].managed_run is None and not shown[0].enabled


@pytest.mark.parametrize('available', [True, False])
def test_second_launch_restores_existing_window_or_reports_fixed_failure(tmp_path, monkeypatch, available):
    launcher = setup_launcher(tmp_path, monkeypatch)
    @contextmanager
    def already_running(_):
        raise BridgeError('another_instance_is_running')
        yield
    monkeypatch.setattr(windows, 'single_instance', already_running)
    calls = []
    monkeypatch.setattr(launcher, 'reopen_window', lambda: calls.append('restore') or available)
    monkeypatch.setattr(supervisor, 'show_window', lambda _: pytest.fail('must not create duplicate GUI'))
    if available:
        launcher.main(['--run-dir',str(tmp_path)])
    else:
        with pytest.raises(BridgeError,match='supervisor_window_not_ready'):
            launcher.main(['--run-dir',str(tmp_path)])
    assert calls == ['restore']


def test_launcher_uses_active_profile_configuration(tmp_path, monkeypatch):
    launcher = setup_launcher(tmp_path, monkeypatch)
    seen = []
    active = SimpleNamespace(cfg=SimpleNamespace(host_id='active-profile-host'))
    def adapter(*args):
        seen.append(args)
        return active
    monkeypatch.setattr(supervisor, 'OperatorAdapter', adapter)
    monkeypatch.setattr(windows, 'single_instance', lambda key: seen.append(key) or nullcontext())
    monkeypatch.setattr(supervisor, 'show_window', lambda manager: seen.append(manager.adapter))
    # No root candidate-config.json exists: only the active deployment owns config.
    launcher.main(['--run-dir', str(tmp_path)])
    assert seen[0] == (tmp_path / 'deployment.json', 'p5', tmp_path / 'operator.py')
    assert seen[1:3] == ['active-profile-host-supervisor-launch', 'active-profile-host-supervisor']
    assert seen[3] is active


@pytest.mark.parametrize('title', ['Slack Codex Bridge Supervisor', 'Slack Codex 桥接管理程序', None])
def test_reopen_window_handles_both_languages_without_duplicate_launch(tmp_path, monkeypatch, title):
    launcher = setup_launcher(tmp_path, monkeypatch)
    class Function:
        def __init__(self, callback): self.callback = callback
        def __call__(self, *args): return self.callback(*args)
    shown = []
    user = SimpleNamespace(
        FindWindowW=Function(lambda cls, name: 123 if cls == 'SlackCodexSupervisorStatus' and name == title else 0),
        ShowWindow=Function(lambda *args: shown.append(args)),
        SetForegroundWindow=Function(lambda handle: shown.append(handle)))
    monkeypatch.setattr(launcher.ctypes, 'WinDLL', lambda *_args, **_kwargs: user, raising=False)
    assert launcher.reopen_window() is (title is not None)
    assert shown == ([(123, 9), 123] if title else [])
