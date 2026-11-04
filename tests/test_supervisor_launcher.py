import importlib.machinery
import importlib.util
import json
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from slack_codex_bridge import config, supervisor, windows
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
    monkeypatch.setattr(config, 'load', lambda _: SimpleNamespace(host_id='test-host'))
    return module


def test_launcher_opens_window_with_stopped_worker_and_no_credential_file_access(tmp_path, monkeypatch):
    launcher = setup_launcher(tmp_path, monkeypatch)
    monkeypatch.setattr(windows, 'single_instance', lambda _: nullcontext())
    monkeypatch.setattr(supervisor, 'OperatorAdapter', lambda *_: SimpleNamespace())
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
