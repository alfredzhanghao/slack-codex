import importlib.util
import json
from pathlib import Path


def test_partial_or_missing_completion_marker_never_crashes_or_proves_success(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/p1_recovery_canary.py"
    spec = importlib.util.spec_from_file_location("canary_marker_test", script)
    canary = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(canary)
    path = tmp_path / "completion.json"
    assert not canary.completion_observed(path, "run", "request")
    for text in ("", "{\"run_id\":", "[]", "null", json.dumps({"run_id": "run", "request_id": "old"})):
        path.write_text(text)
        assert not canary.completion_observed(path, "run", "request")
    path.write_text(json.dumps({"run_id": "run", "request_id": "request"}))
    assert canary.completion_observed(path, "run", "request")
