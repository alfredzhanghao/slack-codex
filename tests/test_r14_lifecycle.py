import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from slack_codex_bridge.model import BridgeError, now_ms


def module(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("r14_test", scripts / "p1_r14_lifecycle.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def armed_state():
    record = {"backend": {"pid": 1}, "desktop": {"pid": 2}, "endpoint": "ws://127.0.0.1:1"}
    state = {"overall": "INCOMPLETE", "deadline_ms": now_ms() + 3600000,
        "task_probe_passed": True, "task_canary_passed": True,
        "last_tick": {"observed_ms": now_ms()}, "controlled_session": record,
        "cases": {key: {"status": "passed"} for key in ("R04", "R06", "R08", "R10")}}
    return state, record


@pytest.mark.parametrize("case", ["R04", "R06", "R08", "R10"])
def test_arm_requires_each_real_gui_case_passed(monkeypatch, case):
    lifecycle = module(monkeypatch)
    state, record = armed_state()
    state["cases"][case]["status"] = "not_run"
    with pytest.raises(BridgeError, match="finish_assisted_gui_cases_first"):
        lifecycle.validate_arm(state, record)


@pytest.mark.parametrize("change", [{"task_canary_passed": False}, {"restart_intent": {"attempted": True}},
    {"r14_lifecycle": {"stage": "unknown"}}, {"deadline_ms": 0},
    {"ui_control": {"status": "await_human_continue"}}])
def test_arm_never_reuses_uncertain_or_unproven_run(monkeypatch, change):
    lifecycle = module(monkeypatch)
    state, record = armed_state()
    state.update(change)
    with pytest.raises(BridgeError, match="preconditions_unverified"):
        lifecycle.validate_arm(state, record)


def test_arm_checks_exact_original_owned_session(monkeypatch):
    lifecycle = module(monkeypatch)
    state, record = armed_state()
    lifecycle.validate_arm(state, record)
    with pytest.raises(BridgeError, match="original_session_changed"):
        lifecycle.validate_arm(state, {**record, "backend": {"pid": 3}})


def worker(lifecycle, cfg):
    _, record = armed_state()
    value = lifecycle.Worker(cfg, {"run_id": "run", "id": "intent", "original_record": record,
        "expires_ms": now_ms() + 600000, "notification_root": "11.000001",
        "notification_thread_id": "gui-test"})
    value.guard = lambda **_: None
    value.save = lambda *_, **__: None
    return value


async def test_unknown_model_write_intent_is_never_replayed(cfg, monkeypatch):
    lifecycle = module(monkeypatch)
    value = worker(lifecycle, cfg)
    calls, saves = [], []

    async def call(method, params, **kwargs):
        calls.append(method)
        assert saves == ["sending"]
        raise BridgeError("backend_outcome_unknown", unknown=True)

    value.backend = SimpleNamespace(rpc=SimpleNamespace(call=call))
    value.save = lambda *_a, **_k: saves.append(value.evidence["actions"]["wait_turn"]["stage"])
    with pytest.raises(BridgeError, match="backend_outcome_unknown"):
        await value.once("wait_turn", "turn/start", {})
    with pytest.raises(BridgeError, match="write_not_replayed"):
        await value.once("wait_turn", "turn/start", {})
    assert calls == ["turn/start"] and saves == ["sending", "unknown"]


@pytest.mark.parametrize("actual", [None, "different_refusal", "active_or_untrusted_thread_stop_refused"])
async def test_refusal_assertion_requires_real_expected_error(cfg, monkeypatch, actual):
    lifecycle = module(monkeypatch)
    value = worker(lifecycle, cfg)

    async def stop(_):
        if actual:
            raise BridgeError(actual)

    monkeypatch.setattr(lifecycle.windows, "stop", stop)
    if actual == "active_or_untrusted_thread_stop_refused":
        await value.expect_refusal("active", actual)
        assert value.evidence["assertions"]["active"]["backend_unchanged"]
    else:
        with pytest.raises(BridgeError, match="unexpected"):
            await value.expect_refusal("active", "active_or_untrusted_thread_stop_refused")
        assert value.evidence["assertions"] == {}


def test_gui_restore_never_launches_over_any_existing_desktop(cfg, monkeypatch):
    lifecycle = module(monkeypatch)
    value = worker(lifecycle, cfg)
    monkeypatch.setattr(lifecycle.windows, "processes", lambda: [{"name": cfg.desktop_exe.name}])
    monkeypatch.setattr(lifecycle.windows, "detached", lambda *_: pytest.fail("must not launch"))
    with pytest.raises(BridgeError, match="existing_gui_requires_review"):
        value.reopen_same_backend()


def test_gui_restore_never_retries_an_uncertain_launch(cfg, monkeypatch):
    lifecycle = module(monkeypatch)
    value = worker(lifecycle, cfg)
    value.gui_launch_attempted = True
    monkeypatch.setattr(lifecycle.windows, "detached", lambda *_: pytest.fail("must not retry"))
    with pytest.raises(BridgeError, match="gui_launch_not_replayed"):
        value.reopen_same_backend()


@pytest.mark.parametrize("wrong_app", [False, True])
async def test_close_notice_checks_exact_app_and_never_retries_unknown(cfg, monkeypatch, wrong_app):
    lifecycle = module(monkeypatch)
    value = worker(lifecycle, cfg)
    monkeypatch.setattr(lifecycle, "prepare", lambda: {"assisted_gui": {
        "root_ts": "11.000001", "thread_id": "gui-test"}, "test_threads": ["gui-test"]})
    monkeypatch.setattr(type(cfg), "credentials", lambda _: ("synthetic-app", "synthetic-bot"))
    posts = []

    class Web:
        def __init__(self, *_):
            self.client = SimpleNamespace(session=None)

        async def call(self, method, **kwargs):
            if method == "auth.test":
                return {"team_id": cfg.team_id, "bot_id": "test-bot"}
            assert method == "conversations.replies" and kwargs["channel"] == cfg.dm_id
            return {"messages": [{"ts": "11.000001", "bot_id": "test-bot",
                "app_id": "wrong" if wrong_app else cfg.app_id}]}

        async def post(self, text, **kwargs):
            assert value.evidence["notifications"]["close_prompt"]["stage"] == "sending"
            posts.append((text, kwargs))
            raise BridgeError("slack_outcome_unknown", unknown=True)

    monkeypatch.setattr(lifecycle, "Web", Web)
    with pytest.raises(BridgeError, match="app_identity_unverified" if wrong_app else "slack_outcome_unknown"):
        await value.notify("close_prompt", "test notice")
    if wrong_app:
        assert posts == []
    else:
        with pytest.raises(BridgeError, match="replay_invalid"):
            await value.notify("close_prompt", "test notice")
        assert len(posts) == 1 and posts[0][1] == {"root": "11.000001"}
        assert value.evidence["notifications"]["close_prompt"]["stage"] == "unknown"


async def test_initial_idle_never_waits_out_an_unrelated_active_task(cfg, monkeypatch):
    lifecycle = module(monkeypatch)
    value = worker(lifecycle, cfg)

    async def loaded():
        return [{"thread_id": "unrelated", "status": {"type": "active"}}]

    value.loaded = loaded
    with pytest.raises(BridgeError, match="other_task_not_idle"):
        await value.initial_idle()


async def test_wait_terminal_polls_original_turn_until_confirmed(cfg, monkeypatch):
    lifecycle = module(monkeypatch)
    value = worker(lifecycle, cfg)
    calls = []

    async def final_messages(tid, turn):
        calls.append((tid, turn))
        if len(calls) == 1:
            raise BridgeError("terminal_not_confirmed")
        return "completed", ["done"]

    async def sleep(_):
        pass

    value.backend = SimpleNamespace(final_messages=final_messages)
    monkeypatch.setattr(lifecycle.asyncio, "sleep", sleep)
    assert await value.wait_terminal("fixture", "exact-turn") == "completed"
    assert calls == [("fixture", "exact-turn")] * 2


async def test_wait_terminal_does_not_swallow_connection_errors(cfg, monkeypatch):
    lifecycle = module(monkeypatch)
    value = worker(lifecycle, cfg)

    async def final_messages(*_):
        raise BridgeError("backend_disconnected")

    value.backend = SimpleNamespace(final_messages=final_messages)
    with pytest.raises(BridgeError, match="backend_disconnected"):
        await value.wait_terminal("fixture", "exact-turn")


async def test_final_idle_requires_two_consecutive_quiet_audits(cfg, monkeypatch):
    lifecycle = module(monkeypatch)
    value = worker(lifecycle, cfg)
    value.evidence["actions"]["wait_chat"] = {"thread_id": "fixture"}
    readings = iter([[], [{"thread_id": "fixture", "status": {"type": "active"}}], [], []])
    calls = []

    async def loaded():
        calls.append(True)
        return next(readings)

    async def sleep(_):
        pass

    value.loaded = loaded
    monkeypatch.setattr(lifecycle.asyncio, "sleep", sleep)
    assert await value.final_idle() == []
    assert len(calls) == 4


async def test_final_idle_refuses_unrelated_activity(cfg, monkeypatch):
    lifecycle = module(monkeypatch)
    value = worker(lifecycle, cfg)

    async def loaded():
        return [{"thread_id": "unrelated", "status": {"type": "active"}}]

    value.loaded = loaded
    with pytest.raises(BridgeError, match="final_idle_audit_failed"):
        await value.final_idle()


@pytest.mark.parametrize("changed", [False, True])
async def test_return_requires_fresh_owned_bridge_and_same_session(cfg, monkeypatch, tmp_path, changed):
    import json

    lifecycle = module(monkeypatch)
    value = worker(lifecycle, cfg)
    process = {"pid": 23, "created": 45, "path": "test-pythonw"}
    runtime = {"run_id": "run", "process": process, "observed_ms": now_ms(),
        "slack_ready": True, "backend_connected": True, "healthy": True,
        "bindings": [{"mode": "desktop"}]}
    (tmp_path / "bridge-status.json").write_text(json.dumps(runtime))
    monkeypatch.setattr(lifecycle, "DATA", tmp_path)
    monkeypatch.setattr(lifecycle, "prepare", lambda: {"bridge": process})
    monkeypatch.setattr(lifecycle.windows, "process_identity", lambda _: process)
    record = {**value.record, "endpoint": "changed"} if changed else value.record
    monkeypatch.setattr(lifecycle.windows, "verify_record", lambda _: record)
    saved = []
    value.save = lambda stage, **_: saved.append(stage)
    if changed:
        with pytest.raises(BridgeError, match="returned_session_changed"):
            await value.verify_return()
        assert saved == []
    else:
        await value.verify_return()
        assert saved == ["returned_and_verified"]
