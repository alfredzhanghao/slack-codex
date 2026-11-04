import copy
import importlib.util
from pathlib import Path

import pytest

from slack_codex_bridge.model import BridgeError, now_ms


def module(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("resume_test", scripts / "p1_r14_resume.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def stopped():
    state = {"run_id": "run", "overall": "INCOMPLETE",
        "cases": {f"R{n:02}": {"status": "passed"} for n in range(1, 14)}}
    record = {"status": "stopped", "backend": {"pid": 1}, "desktop": {"pid": 2}, "endpoint": "local"}
    evidence = {"run_id": "run", "original_decline_count": 1, "original_session": copy.deepcopy(record),
        "final_idle_audit": [{"status": {"type": "idle"}}],
        "actions": {k: {"stage": "accepted"} for k in ("wait_chat", "approval_chat", "wait_turn", "approval_turn")},
        "assertions": {k: {"refusal": v, "backend_unchanged": True} for k, v in {
            "bridge_running": "another_instance_is_running", "desktop_open": "desktop_still_open_stop_refused",
            "active": "active_or_untrusted_thread_stop_refused",
            "approval": "active_or_untrusted_thread_stop_refused"}.items()}}
    return state, record, evidence


def test_resume_accepts_only_proven_stopped_tail(monkeypatch):
    value = module(monkeypatch)
    value.validate_stopped(*stopped())


@pytest.mark.parametrize("change", [{}, {"status": "consumed"}, {"run_id": "other"},
    {"scope": "generic_unattended"}, {"ordinary_desktop": {"pid": 2}}, {"granted_ms": 0}])
def test_switch_requires_fresh_exact_single_use_authorization(monkeypatch, change):
    value = module(monkeypatch)
    app = {"pid": 1}
    auth = {"status": "granted", "run_id": "run", "scope": "r14_stopped_tail_single_switch",
        "ordinary_desktop": app, "granted_ms": now_ms()}
    if not change:
        value.validate_authorization(auth, "run", app)
    else:
        auth.update(change)
        with pytest.raises(BridgeError, match="explicit_current_switch_authorization_required"):
            value.validate_authorization(auth, "run", app)


@pytest.mark.parametrize("change", ["live", "approval_missing", "busy", "duplicate_decline", "unknown",
    "changed_backend", "prior_case_missing"])
def test_resume_refuses_missing_or_uncertain_proof(monkeypatch, change):
    value = module(monkeypatch)
    state, record, evidence = stopped()
    if change == "live":
        record["status"] = "verified"
    elif change == "approval_missing":
        evidence["assertions"].pop("approval")
    elif change == "busy":
        evidence["final_idle_audit"][0]["status"]["activeFlags"] = ["waitingOnApproval"]
    elif change == "duplicate_decline":
        evidence["original_decline_count"] = 2
    elif change == "unknown":
        evidence["actions"]["approval_turn"]["stage"] = "unknown"
    elif change == "changed_backend":
        record["backend"] = {"pid": 3}
    else:
        state["cases"]["R04"]["status"] = "not_run"
    with pytest.raises(BridgeError, match="unproven"):
        value.validate_stopped(state, record, evidence)


@pytest.mark.parametrize("owned", [False, True])
def test_only_verified_ordinary_app_descendants_may_exist_before_switch(cfg, monkeypatch, owned):
    value = module(monkeypatch)
    _, record, _ = stopped()
    app = {"pid": 10, "path": str(cfg.desktop_exe.resolve())}
    child = {"pid": 11, "path": str(cfg.backend_exe.resolve())}
    record.update(backend_hash="hash", desktop_hash="hash")
    monkeypatch.setattr(value.windows, "process_identity", lambda pid: {10: app, 11: child}.get(pid))
    monkeypatch.setattr(value.windows, "verify_versions", lambda _: None)
    monkeypatch.setattr(value.windows, "binary_hash", lambda _: "hash")
    monkeypatch.setattr(value.windows, "processes", lambda: [
        {"pid": 11, "parent": 10 if owned else 12, "name": cfg.backend_exe.name}])
    if owned:
        value.verify_absent(cfg, record, app)
    else:
        with pytest.raises(BridgeError, match="existing_backend_requires_review"):
            value.verify_absent(cfg, record, app)
    with pytest.raises(BridgeError, match="existing_backend_requires_review"):
        value.verify_absent(cfg, record)


@pytest.mark.parametrize("start_fails", [False, True])
async def test_resume_launches_once_without_repeating_model_operations(cfg, monkeypatch, tmp_path, start_fails):
    import json

    value = module(monkeypatch)
    state, record, evidence = stopped()
    for label in ("wait", "approval"):
        evidence["actions"][label + "_chat"]["thread_id"] = label + "-thread"
        evidence["actions"][label + "_turn"]["turn_id"] = label + "-turn"
    source = tmp_path / "R14-stopped-tail-source.json"
    source.write_text(json.dumps(evidence))
    original = source.read_text()
    calls, stages, final_reads = [], [], []
    monkeypatch.setattr(value, "DATA", tmp_path)
    monkeypatch.setattr(value, "prepare", lambda: state)
    monkeypatch.setattr(value.windows, "read_record", lambda _: record)
    monkeypatch.setattr(value, "verify_absent", lambda *_: None)
    monkeypatch.setattr(value, "stop_exact_idle_app", lambda *_: calls.append("app_exit"))
    monkeypatch.setattr(value, "mutate", lambda change: change(state))

    def start(_):
        calls.append("start")
        if start_fails:
            raise BridgeError("launch_outcome_unknown", unknown=True)
        return {**record, "peer": {"pid": 3}}

    monkeypatch.setattr(value.windows, "start", start)

    class Worker:
        def __init__(self, *_):
            self.evidence = {}
            self.notice_web = None

        def save(self, stage, **_):
            stages.append(stage)

        def schedule_bridge(self, _):
            calls.append("bridge")

        async def verify_return(self):
            calls.append("verify")

        async def notify(self, *_):
            calls.append("notice")

    class Desktop:
        def __init__(self, *_):
            pass

        async def connect(self):
            pass

        async def final_messages(self, tid, turn):
            final_reads.append((tid, turn))
            return "completed", []

        async def close(self):
            pass

    monkeypatch.setattr(value, "Worker", Worker)
    monkeypatch.setattr(value, "Desktop", Desktop)
    await value.resume(cfg, {"id": "resume", "evidence_file": source.name, "ordinary_desktop": {"pid": 4}})
    assert source.read_text() == original
    if start_fails:
        assert calls == ["app_exit", "start"] and stages[-1] == "resume_unproven"
        assert final_reads == []
    else:
        assert calls == ["app_exit", "start", "bridge", "verify", "notice"]
        assert final_reads == [("wait-thread", "wait-turn"), ("approval-thread", "approval-turn")]
        assert state["phase"] == "r14_returned_wait_report"
