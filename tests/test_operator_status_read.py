import importlib.util
from pathlib import Path

import pytest


def operator_module():
    path = Path(__file__).resolve().parents[1] / 'scripts/bridge_operator.py'
    spec = importlib.util.spec_from_file_location('status_read_test_operator', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_transient_windows_file_sharing_denial_recovers_without_other_effects(monkeypatch):
    operator = operator_module()
    reads, sleeps = [], []

    def read_text(path, **kwargs):
        reads.append(path)
        if len(reads) < 3:
            raise PermissionError('synthetic sharing denial')
        return '{"run_id":"same-worker"}'

    monkeypatch.setattr(Path, 'read_text', read_text)
    monkeypatch.setattr(operator.time, 'sleep', sleeps.append)
    assert operator.read('status.json') == {'run_id': 'same-worker'}
    assert len(reads) == 3 and sleeps == [0.025, 0.025]


def test_persistent_denial_stops_at_bound_without_exposing_path_or_raw_error(monkeypatch):
    operator = operator_module()
    reads = []

    def denied(*args, **kwargs):
        reads.append(1)
        raise PermissionError('private raw material')

    monkeypatch.setattr(Path, 'read_text', denied)
    monkeypatch.setattr(operator.time, 'sleep', lambda _: None)
    with pytest.raises(operator.OperatorError, match='^operator_state_read_unavailable$'):
        operator.read('private-fixture.json')
    assert len(reads) == 9


def test_malformed_status_is_never_retried_or_trusted(monkeypatch):
    operator = operator_module()
    reads = []

    def malformed(*args, **kwargs):
        reads.append(1)
        return '{invalid'

    monkeypatch.setattr(Path, 'read_text', malformed)
    monkeypatch.setattr(operator.time, 'sleep', lambda _: pytest.fail('unexpected retry'))
    with pytest.raises(ValueError):
        operator.read('status.json')
    assert len(reads) == 1
