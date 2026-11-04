import importlib.util
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from slack_codex_bridge.model import BridgeError, now_ms


def live_module(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("p1_live_chat_test", scripts / "p1_live_chat.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


async def test_uncertain_chat_creation_is_persisted_and_never_repeated(cfg, monkeypatch):
    live = live_module(monkeypatch)
    state = {"deadline_ms": now_ms() + 60000, "cases": {"R02": {"status": "passed"}}}
    calls, saved = [], []
    monkeypatch.setattr(live, "load", lambda _: cfg)
    monkeypatch.setattr(live, "prepare", lambda: state)
    monkeypatch.setattr(live, "write_json", lambda _, value: saved.append(value["chat_preparation"]["primary"]["stage"]))
    monkeypatch.setattr(live.windows, "single_instance", lambda _: nullcontext())
    monkeypatch.setattr(live.windows, "verify_record", lambda _: {"endpoint": "ws://127.0.0.1:1"})

    class Backend:
        def __init__(self, *_):
            self.rpc = self

        async def connect(self):
            pass

        async def close(self):
            pass

        async def call(self, method, params, **kwargs):
            assert saved == ["creating"]
            calls.append(method)
            raise BridgeError("backend_outcome_unknown", unknown=True)

    monkeypatch.setattr(live, "Desktop", Backend)
    args = SimpleNamespace(mode="prepare", slot="primary")
    with pytest.raises(BridgeError, match="backend_outcome_unknown"):
        await live.main_async(args)
    with pytest.raises(BridgeError, match="not_replayed"):
        await live.main_async(args)
    assert calls == ["thread/start"]


def test_expired_live_window_prevents_intent_mutation(monkeypatch):
    live = live_module(monkeypatch)
    monkeypatch.setattr(live.windows, "single_instance", lambda _: nullcontext())
    monkeypatch.setattr(live, "prepare", lambda: {"deadline_ms": 0})
    with pytest.raises(BridgeError, match="preconditions_unverified"):
        live.update(lambda _: pytest.fail("must not mutate expired run"))


@pytest.mark.parametrize("prior", [{"stage": "sending"},
    {"stage": "accepted", "turn_id": "old", "status": "inProgress"}])
async def test_named_trial_never_repeats_uncertain_or_active_wait(cfg, monkeypatch, prior):
    live = live_module(monkeypatch)
    state = {"chat_preparation": {"controls": {"thread_id": "test"}},
        "test_threads": ["test"], "wait_policy": {"controls": {"stage": "accepted"}},
        "wait_tasks": {"controls": prior}}
    monkeypatch.setattr(live, "load", lambda _: cfg)
    monkeypatch.setattr(live, "prepare", lambda: state)
    monkeypatch.setattr(live.windows, "verify_record", lambda _: {"endpoint": "ws://127.0.0.1:1"})
    monkeypatch.setattr(live, "update", lambda _: pytest.fail("must not persist new write intent"))

    class Backend:
        def __init__(self, *_):
            pass

        async def connect(self):
            pass

        async def close(self):
            pass

        async def snapshot(self, _):
            return SimpleNamespace(trusted=True, idle=True, approval=False)

        async def final_messages(self, *_):
            return prior["status"], []

    monkeypatch.setattr(live, "Desktop", Backend)
    with pytest.raises(BridgeError, match="previous_wait"):
        await live.main_async(SimpleNamespace(mode="wait-start", slot="controls", trial="busy_interrupt"))


def test_named_trials_preserve_original_wait_intent(monkeypatch):
    live = live_module(monkeypatch)
    original = {"stage": "sending"}
    state = {"wait_tasks": {"controls": original}}
    records, key = live.wait_record(state, "controls", "busy_interrupt")
    records[key] = {"stage": "sending"}
    assert state["wait_tasks"]["controls"] is original
    assert live.wait_record(state, "controls", "busy_interrupt")[0][key]["stage"] == "sending"


@pytest.mark.parametrize("change", [
    {"run_id": "another-run"}, {"fault_id": "another-fault"},
    {"stage": "sending"}, {"observed_ms": 0}, {"observed_ms": 2**62}])
def test_crash_rejects_wrong_or_stale_real_checkpoint(monkeypatch, change):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("p1_fault_case_test", scripts / "p1_fault_case.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    state = {"run_id": "run"}
    rule = {"run_id": "run", "id": "fault", "stage": "received", "expires_ms": now_ms() + 10000}
    checkpoint = {"run_id": "run", "fault_id": "fault", "stage": "received", "observed_ms": now_ms(), **change}
    with pytest.raises(BridgeError, match="checkpoint_unverified"):
        value.validate_checkpoint(state, rule, checkpoint)


async def test_active_crash_rejects_changed_turn_before_opening_backend(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("p1_active_crash_test", scripts / "p1_fault_case.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    monkeypatch.setattr(value, "Desktop", lambda *_: pytest.fail("wrong target must not connect"))
    state = {"chat_preparation": {"controls": {"thread_id": "test"}},
        "wait_trials": {"controls": {"crash_survival": {"stage": "accepted", "turn_id": "old"}}}}
    status = {"bindings": [{"thread_id": "test", "active_turn": "different", "trusted": True,
        "pending_approval": False}]}
    with pytest.raises(BridgeError, match="active_crash_target_unverified"):
        await value.verify_active_wait(None, {}, state, status)
