import importlib.util
import json
import os
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from slack_codex_bridge.model import BridgeError, Operation, Receipt, new_id, now_ms
from slack_codex_bridge.slack import Buttons, Intent


def module(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"p1_acceptance_{name}.py"
    spec = importlib.util.spec_from_file_location(f"acceptance_{name}", path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


@pytest.mark.parametrize("wrong_scope", [None, "team", "user", "channel", "container"])
async def test_old_card_observation_requires_real_prior_issue_and_exact_scope(cfg, tmp_path, monkeypatch, wrong_scope):
    probe_module = probe_at(tmp_path, monkeypatch)
    probe = probe_module.Probe("test-run")
    state = SimpleNamespace(binding=SimpleNamespace(binding_id="binding", codex_thread_id="test-thread",
        slack_root_ts="11.000001"), epoch="old", mode="slack")
    probe.controller = SimpleNamespace(states={"binding": state})
    buttons = Buttons()
    token = buttons.issue(Intent("status", "binding", "old", root="11.000001"))
    disconnected = []

    def lost():
        buttons.clear()
        state.epoch, state.mode = "new", "desktop"

    async def disconnect():
        disconnected.append(True)

    async def original(payload):
        assert buttons.take(payload["actions"][0]["value"]) is None
        return None, None, None

    probe.slack = SimpleNamespace(cfg=cfg, buttons=buttons, lost=lost,
        socket=SimpleNamespace(disconnect=disconnect))
    probe.original_interaction = original
    await probe.close_slack_for_stale_card("test-thread")
    assert disconnected == [True] and token not in buttons.tokens
    payload = {"type": "block_actions", "api_app_id": cfg.app_id,
        "team": {"id": cfg.team_id}, "user": {"id": cfg.user_id}, "channel": {"id": cfg.dm_id},
        "container": {"message_ts": "11.000001"},
        "actions": [{"action_id": "bridge_status", "value": token}]}
    if wrong_scope:
        payload[wrong_scope] = {"id": "wrong", "message_ts": "wrong"}
    assert await probe.observe_interaction(payload) == (None, None, None)
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    observed = [r for r in rows if r["event"] == "old_button_callback"]
    assert len(observed) == (0 if wrong_scope else 1)
    if observed:
        assert observed[0]["rejected_without_operation"]
        assert observed[0]["old_epoch"] != observed[0]["current_epoch"]
        assert probe.hold_until_ms == 0
    assert token not in (tmp_path / "events.jsonl").read_text()


async def test_card_hold_delays_but_does_not_fake_or_repeat_update(tmp_path, monkeypatch):
    probe_module = probe_at(tmp_path, monkeypatch)
    probe = probe_module.Probe("test-run")
    calls = []

    async def update(*args):
        calls.append(args)
        return "actual-result"

    probe.original_update = update
    probe.held_root, probe.hold_until_ms = "11.000001", now_ms() + 10000
    task = asyncio.create_task(probe.delayed_card_update("11.000001", "text", []))
    await asyncio.sleep(0)
    assert calls == []
    probe.hold_until_ms = 0
    assert await task == "actual-result"
    assert calls == [("11.000001", "text", [])]


def test_bootstrap_synthetic_credentials_stay_local(cfg, tmp_path, monkeypatch, capsys):
    bootstrap = module("bootstrap")
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    monkeypatch.setenv(cfg.app_token_env, "")
    monkeypatch.setenv(cfg.bot_token_env, "")
    (tmp_path / "Token.env").write_text("APP=xapp-synthetic-only\nBOT=xoxb-synthetic-only")
    bootstrap.inject_credentials(cfg)
    assert os.environ[cfg.app_token_env] == "xapp-synthetic-only"
    assert os.environ[cfg.bot_token_env] == "xoxb-synthetic-only"
    assert not capsys.readouterr().out


def test_bootstrap_rejects_ambiguous_data_without_partial_injection(cfg, tmp_path, monkeypatch):
    bootstrap = module("bootstrap")
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    monkeypatch.setenv(cfg.app_token_env, "original")
    (tmp_path / "Token.env").write_text("xapp-synthetic-only xoxb-first-example xoxb-second-example")
    with pytest.raises(BridgeError, match="credential_format_invalid"):
        bootstrap.inject_credentials(cfg)
    assert os.environ[cfg.app_token_env] == "original"


def test_preparation_is_idempotent_and_has_no_secret_state(tmp_path, monkeypatch):
    supervisor = module("supervisor")
    monkeypatch.setattr(supervisor, "STATE", tmp_path / "state.json")
    first, second = supervisor.prepare(), supervisor.prepare()
    assert first["run_id"] == second["run_id"]
    assert all(x["status"] == "not_run" for x in first["cases"].values())
    assert not any(value in json.dumps(first).lower() for value in ("xapp-", "xoxb-", "token.env"))


def test_prepare_can_reprobe_after_historical_recovery_fault(tmp_path, monkeypatch):
    supervisor = module("supervisor")
    monkeypatch.setattr(supervisor, "STATE", tmp_path / "state.json")
    state = supervisor.prepare()
    state["last_fault"] = "process_path_unavailable"
    supervisor.write_json(supervisor.STATE, state)
    assert supervisor.main(["prepare"]) == 0
    assert supervisor.prepare()["last_fault"] == "process_path_unavailable"


def test_window_renewal_preserves_evidence_and_requires_fresh_human_grant():
    supervisor = module("supervisor")
    state = {"run_id": "run", "deadline_ms": 1, "cases": {"R01": {"status": "passed"}},
             "handoff_authorization": {"status": "granted", "run_id": "run", "granted_ms": now_ms()}}
    renewed = supervisor.renew_after_human_grant(state)
    assert renewed["cases"]["R01"]["status"] == "passed"
    assert renewed["deadline_ms"] > now_ms() and not renewed["task_probe_passed"]
    assert renewed["window_history"][0]["previous_deadline_ms"] == 1
    state["handoff_authorization"]["granted_ms"] = 0
    with pytest.raises(BridgeError, match="fresh_human_grant_required"):
        supervisor.renew_after_human_grant(state)


def test_verified_continuation_keeps_consumed_handoff_and_original_evidence(cfg, monkeypatch):
    supervisor = module("supervisor")
    record = {"backend": {"pid": 1}, "desktop": {"pid": 2}, "endpoint": "ws://127.0.0.1:1"}
    monkeypatch.setattr(supervisor.windows, "verify_record", lambda _: record)
    monkeypatch.setattr(supervisor.windows, "process_identity", lambda _: None)
    state = {"overall": "INCOMPLETE", "deadline_ms": 0, "controlled_session": record,
             "bridge": {"pid": 3}, "cases": {"R03": {"status": "passed"}},
             "handoff_authorization": {"status": "consumed"}}
    result = supervisor.renew_verified_continuation(state, cfg)
    assert result["deadline_ms"] > now_ms()
    assert result["handoff_authorization"]["status"] == "consumed"
    assert result["cases"]["R03"]["status"] == "passed" and not result["task_canary_passed"]


@pytest.mark.parametrize("change", [
    {"overall": "COMPLETE"}, {"deadline_ms": 2**62}, {"restart_intent": {"attempted": True}},
    {"ui_control": {"status": "await_human_continue"}}, {"controlled_session": {"backend": {"pid": 99}}}])
def test_verified_continuation_cannot_renew_changed_or_unsafe_scope(cfg, monkeypatch, change):
    supervisor = module("supervisor")
    record = {"backend": {"pid": 1}, "desktop": {"pid": 2}, "endpoint": "ws://127.0.0.1:1"}
    monkeypatch.setattr(supervisor.windows, "verify_record", lambda _: record)
    state = {"overall": "INCOMPLETE", "deadline_ms": 0, "controlled_session": record, **change}
    with pytest.raises(BridgeError):
        supervisor.renew_verified_continuation(state, cfg)


@pytest.mark.parametrize("intent", [
    {"kind": "ensure_bridge", "run_id": "wrong", "expires_ms": 2**63},
    {"kind": "ensure_bridge", "run_id": "run", "expires_ms": 0},
    {"kind": "arbitrary_command", "run_id": "run", "expires_ms": 2**63}])
def test_recovery_intents_are_scoped_and_expire(cfg, intent):
    supervisor = module("supervisor")
    state = {"run_id": "run", "task_probe_passed": True, "restart_intent": intent}
    with pytest.raises(BridgeError):
        supervisor.recover(state, cfg)


def test_user_ui_stop_prevents_automatic_restart_even_with_armed_intent(cfg, monkeypatch):
    supervisor = module("supervisor")
    state = {"run_id": "run", "task_probe_passed": True,
             "ui_control": {"status": "await_human_continue"},
             "restart_intent": {"kind": "ensure_controlled_desktop", "run_id": "run", "expires_ms": 2**63}}
    monkeypatch.setattr(supervisor.windows, "verify_record", lambda *_: pytest.fail("must not start recovery"))
    supervisor.recover(state, cfg)
    assert state["restart_intent"] is not None


def probe_at(tmp_path, monkeypatch):
    probe_module = module("probe")
    monkeypatch.setattr(probe_module, "DATA", tmp_path)
    state = {"run_id": "test-run", "task_probe_passed": True,
             "deadline_ms": now_ms() + 60000, "test_threads": ["test-thread"]}
    (tmp_path / "state.json").write_text(json.dumps(state))
    return probe_module


def test_test_control_claims_survive_bridge_restart(tmp_path, monkeypatch):
    probe_module = probe_at(tmp_path, monkeypatch)
    first = probe_module.Probe("test-run")
    assert first.claim("shutdown-once")
    second = probe_module.Probe("test-run")
    assert not second.claim("shutdown-once")
    with pytest.raises(BridgeError, match="probe_not_authorized"):
        probe_module.Probe("another-run")


async def test_checkpoint_evidence_excludes_input_and_fault_is_not_replayed(tmp_path, monkeypatch):
    probe_module = probe_at(tmp_path, monkeypatch)
    op = Operation(new_id(), "message:test", "binding", 1, "turn_start", "test-thread",
                   payload_json=json.dumps({"text": "Synthetic private input must not enter evidence"}))
    (tmp_path / "fault-rule.json").write_text(json.dumps({"run_id": "test-run", "id": "fault-once",
        "expires_ms": now_ms() + 10000, "thread_id": "test-thread", "kind": "turn_start",
        "stage": "receipt", "action": "commit_unavailable_once"}))
    first = probe_module.Probe("test-run")
    with pytest.raises(BridgeError, match="database_unavailable"):
        await first.checkpoint("receipt", op, Receipt("actual-receipt-id"))
    second = probe_module.Probe("test-run")
    await second.checkpoint("receipt", op, Receipt("actual-receipt-id"))
    evidence = (tmp_path / "events.jsonl").read_text()
    assert "Synthetic private input" not in evidence
    assert evidence.count('"event": "fault_claimed"') == 1
    assert "actual-receipt-id" in evidence


def test_invalid_fault_cannot_affect_out_of_scope_thread(tmp_path, monkeypatch):
    probe_module = probe_at(tmp_path, monkeypatch)
    (tmp_path / "fault-rule.json").write_text(json.dumps({"run_id": "test-run", "id": "bad",
        "expires_ms": now_ms() + 10000, "thread_id": "business-thread", "kind": "turn_start",
        "stage": "sending", "action": "commit_unavailable_once"}))
    assert probe_module.Probe("test-run").rule() is None


@pytest.mark.parametrize("audit", [
    {},
    {"orchestrator_thread": "01a1786b-d49d-7243-b5e7-936ba8985fdc", "other_active_threads": ["business"],
     "observed_ms": 2**62},
    {"orchestrator_thread": "01a1786b-d49d-7243-b5e7-936ba8985fdc", "other_active_threads": [],
     "observed_ms": 0},
    {"orchestrator_thread": "01a1786b-d49d-7243-b5e7-936ba8985fdc", "other_active_threads": [],
     "observed_ms": 2**62}])
def test_handoff_refuses_missing_stale_busy_or_future_audit(cfg, audit, monkeypatch):
    supervisor = module("supervisor")
    monkeypatch.setattr(supervisor.windows, "_kernel", lambda: pytest.fail("must not access process control"))
    with pytest.raises(BridgeError, match="other_tasks_not_verified_idle"):
        supervisor.controlled_handoff({"run_id": "test-run", "task_canary_passed": True, "handoff_authorization": {
            "status": "granted", "run_id": "test-run"}}, cfg, {"idle_audit": audit})


def test_handoff_requires_explicit_exception_before_process_control(cfg, monkeypatch):
    supervisor = module("supervisor")
    monkeypatch.setattr(supervisor.windows, "_kernel", lambda: pytest.fail("must not access process control"))
    with pytest.raises(BridgeError, match="explicit_handoff_exception_required"):
        supervisor.controlled_handoff({"run_id": "test-run"}, cfg, {})


def test_handoff_checks_exact_handle_then_proves_controlled_backend(cfg, tmp_path, monkeypatch):
    supervisor = module("supervisor")
    monkeypatch.setattr(supervisor, "DATA", tmp_path)
    monkeypatch.setattr(supervisor, "STATE", tmp_path / "state.json")
    old = {"pid": 52, "created": 1, "path": str(cfg.desktop_exe.resolve())}
    record = {"backend": {"pid": 71}, "desktop": {"pid": 72}, "peer": {"pid": 73},
              "endpoint": "ws://127.0.0.1:1"}
    calls = []

    class Fn:
        def __init__(self, name, result):
            self.name, self.result = name, result

        def __call__(self, *args):
            calls.append((self.name, args))
            return self.result

    kernel = type("Kernel", (), {})()
    kernel.OpenProcess, kernel.TerminateProcess, kernel.CloseHandle = Fn("open", 1234), Fn("terminate", True), Fn("close", True)
    monkeypatch.setattr(supervisor.windows, "_kernel", lambda: kernel)
    monkeypatch.setattr(supervisor.windows, "_handle_identity", lambda *_: old)
    monkeypatch.setattr(supervisor.windows, "record_path", lambda _: tmp_path / "missing-record")
    monkeypatch.setattr(supervisor.windows, "verify_versions", lambda _: None)
    monkeypatch.setattr(supervisor.windows, "process_identity", lambda _: old)
    monkeypatch.setattr(supervisor.windows, "processes", lambda: [])
    monkeypatch.setattr(supervisor.windows, "start", lambda _: record)

    class FakeRPC:
        def __init__(self, *args):
            pass

        async def connect(self):
            calls.append(("rpc_connect", ()))

        async def call(self, method, params):
            calls.append((method, params))
            return {"data": []}

        async def close(self):
            pass
    monkeypatch.setattr("slack_codex_bridge.desktop.RPC", FakeRPC)
    state = {"run_id": "test-run", "task_canary_passed": True, "cases": {"R02": {"status": "not_run"}},
             "handoff_authorization": {"status": "granted", "run_id": "test-run"}}
    intent = {"ordinary_desktop": old, "idle_audit": {
        "orchestrator_thread": "01a1786b-d49d-7243-b5e7-936ba8985fdc", "other_active_threads": [],
        "observed_ms": now_ms()}}
    supervisor.controlled_handoff(state, cfg, intent)
    assert ("terminate", (1234, 0)) in calls
    assert any(name == "thread/list" for name, _ in calls)
    assert state["cases"]["R02"]["status"] == "passed"
    assert state["phase"] == "controlled_ready_for_test_driver"
    assert state["handoff_authorization"]["status"] == "consumed"
    with pytest.raises(BridgeError, match="explicit_handoff_exception_required"):
        supervisor.controlled_handoff(state, cfg, intent)
