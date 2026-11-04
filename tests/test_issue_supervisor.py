"""Deterministic lifecycle recovery tests; no real desktop or Slack effects."""

from contextlib import nullcontext

import pytest

from slack_codex_bridge import windows
from slack_codex_bridge.model import BridgeError, new_id
from slack_codex_bridge.supervisor import OperatorAdapter, Supervisor, physical_file


class Adapter:
    def __init__(self):
        self.status = {"running": False, "version": "test"}
        self.calls = []
        self.native_error = None
        self.start_error = None

    def native(self):
        self.calls.append("verify")
        if self.native_error:
            raise BridgeError(self.native_error)
        return True

    def worker(self):
        self.calls.append("status")
        return dict(self.status)

    def restore(self):
        self.calls.append("explicit_restore")
        return self.native()

    def start(self):
        self.calls.append("start")
        if self.start_error:
            raise BridgeError(self.start_error)
        self.status = {"run_id": new_id(), "running": True, "fresh": True, "ready": True,
                       "healthy": True, "backend_connected": True, "phase": "running", "version": "test"}
        return dict(self.status)

    def stop(self, run_id):
        assert self.status.get("run_id") == run_id
        self.calls.append("stop")
        self.status.update(running=False, ready=False, phase="stopped")


def test_manual_start_and_stop_manage_one_verified_chain_without_duplicate_launch():
    a = Adapter()
    s = Supervisor(a)
    s.poll()
    assert "start" not in a.calls and not s.enabled
    s.action("Start")
    assert s.enabled and s.managed_run == a.status["run_id"]
    s.action("Start")
    assert a.calls.count("start") == 1
    s.action("Stop")
    s.poll()
    assert a.calls.count("stop") == 1 and not s.enabled and not a.status["running"]


def test_poll_observes_existing_worker_but_only_explicit_start_adopts_it():
    a = Adapter()
    a.start()
    s = Supervisor(a)
    s.poll()
    assert s.managed_run is None and s.phase == "external worker (observe only)"
    s.action("Exit")
    assert "stop" not in a.calls and a.status["running"]
    s.action("Start")
    assert s.managed_run == a.status["run_id"]
    s.action("Exit")
    assert not a.status["running"]


@pytest.mark.parametrize("code", ["desktop_process_identity_changed", "backend_process_identity_changed",
                                   "supervisor_unrelated_desktop_close_or_reconnect_manually"])
def test_desktop_closure_suspends_and_never_reopens_or_terminates_native_processes(code):
    a = Adapter()
    s = Supervisor(a)
    s.action("Start")
    restores = a.calls.count("explicit_restore")
    a.native_error = code
    s.poll()
    assert not s.enabled and s.phase == "suspended" and s.code == code
    assert a.calls.count("stop") == 1
    for _ in range(5):
        s.poll()
    assert a.calls.count("explicit_restore") == restores and a.calls.count("start") == 1


def test_unexpected_exit_has_backoff_and_bounded_recovery_without_task_replay():
    a = Adapter()
    clock = [0.0]
    s = Supervisor(a, lambda: clock[0])
    s.action("Start")
    for attempt in range(3):
        a.status.update(running=False, phase="failed", fault="slack_unavailable")
        s.poll()
        assert s.phase == "recovery backoff"
        starts = a.calls.count("start")
        s.poll()
        assert a.calls.count("start") == starts
        clock[0] += 2 ** (attempt + 1)
        s.poll()
        assert a.calls.count("start") == starts + 1
    a.status.update(running=False, phase="failed")
    s.poll()
    assert not s.enabled and s.code == "supervisor_retry_budget_exhausted"
    assert a.calls.count("start") == 4
    assert set(a.calls) <= {"explicit_restore", "verify", "status", "start"}


def test_stale_live_status_and_intentional_stop_never_trigger_duplicate_worker():
    a = Adapter()
    s = Supervisor(a)
    s.action("Start")
    a.status["fresh"] = False
    s.poll()
    assert a.calls.count("start") == 1 and s.code == "supervisor_status_stale_no_duplicate_start"
    a.status.update(running=False, phase="stopped")
    s.poll()
    assert not s.enabled and a.calls.count("start") == 1


@pytest.mark.parametrize("code", ["database_unavailable", "slack_missing_scope", "slack_read_invalid_auth",
                                   "controlled_session_changed_restart_bridge"])
def test_nonrecoverable_worker_faults_require_explicit_repair(code):
    a = Adapter()
    s = Supervisor(a)
    s.action("Start")
    a.status.update(running=False, phase="failed", fault=code)
    s.poll()
    assert not s.enabled and s.code == code and a.calls.count("start") == 1


def test_stop_refuses_changed_worker_ownership():
    a = Adapter()
    s = Supervisor(a)
    s.action("Start")
    a.status["run_id"] = new_id()
    s.poll()
    assert not s.enabled and s.code == "supervisor_worker_ownership_changed"
    assert "stop" not in a.calls


def test_stable_recovery_and_explicit_adoption_reset_retry_budget():
    a = Adapter()
    clock = [0.0]
    s = Supervisor(a, lambda: clock[0])
    s.action("Start")
    s.retries = 2
    clock[0] = 60.0
    s.poll()
    assert s.retries == 0
    s.retries = 3
    s.enabled = False
    s.action("Restore connection")
    assert s.enabled and s.retries == 0 and a.calls.count("start") == 1


def test_action_failure_remains_actionable_after_unrelated_status_poll_error():
    a = Adapter()
    a.native_error = "supervisor_unrelated_desktop_close_or_reconnect_manually"
    s = Supervisor(a)
    s.action("Start")
    a.native_error = "supervisor_deployment_path_unavailable"
    s.poll()
    assert s.code == "supervisor_deployment_path_unavailable"
    assert s.last_action_code == "supervisor_unrelated_desktop_close_or_reconnect_manually"
    assert s.last_action_code in s.status_text()
    assert "start" not in a.calls


def test_uncertain_start_is_not_automatically_resubmitted_and_error_is_fixed():
    a = Adapter()
    a.start_error = "supervisor_operator_unconfirmed_inspect_before_retry"
    s = Supervisor(a)
    s.action("Start")
    for _ in range(4):
        s.poll()
    assert a.calls.count("start") == 1 and not s.enabled
    assert "inspect_before_retry" in s.status_text()
    a.start_error = "raw payload with confidential detail"
    s.action("Start")
    assert "confidential" not in s.status_text() and s.code == "supervisor_operation_failed"


def test_canonical_file_rejects_alias_resolving_to_a_different_physical_file(tmp_path, monkeypatch):
    path = tmp_path / "module.py"
    path.write_text("fixture")
    monkeypatch.setattr("slack_codex_bridge.supervisor.handle_path", lambda stream: tmp_path / "physical.py")
    with pytest.raises(BridgeError, match="canonical_absolute_paths"):
        physical_file(path)


def test_operator_adapter_requires_worker_to_report_the_same_native_chain(tmp_path):
    a = OperatorAdapter.__new__(OperatorAdapter)
    a.value = {"version": "test", "expected_worker_exe": str(tmp_path / "pythonw.exe")}
    a.record = {k: k for k in ("endpoint", "desktop", "backend", "peer")}
    result = {"running": True, "version": "test", "process": {"path": a.value["expected_worker_exe"]},
              "native_chain": dict(a.record)}
    a.command = lambda name: result
    assert a.worker() == result
    result["native_chain"]["backend"] = "other"
    with pytest.raises(BridgeError, match="supervisor_worker_backend_unverified"):
        a.worker()


def test_restore_never_attaches_an_existing_unrelated_desktop(cfg, monkeypatch):
    monkeypatch.setattr(windows, "single_instance", lambda *args: nullcontext())
    monkeypatch.setattr(windows, "verify_record", lambda *args, **kwargs: {})
    monkeypatch.setattr(windows, "processes", lambda: [{"name": cfg.desktop_exe.name}])
    monkeypatch.setattr(windows, "detached", lambda *args: pytest.fail("must not open another desktop"))
    with pytest.raises(BridgeError, match="unrelated_desktop"):
        windows.restore_desktop(cfg)


def test_explicit_restore_reuses_original_backend_and_verifies_peer_before_saving(cfg, monkeypatch):
    record = {"port": 9999, "backend": {"pid": 10}, "endpoint": "ws://127.0.0.1:9999"}
    saves, launches = [], []
    monkeypatch.setattr(windows, "single_instance", lambda *args: nullcontext())
    monkeypatch.setattr(windows, "verify_record", lambda *args, **kwargs: record)
    monkeypatch.setattr(windows, "processes", lambda: [])
    monkeypatch.setattr(windows, "tcp_rows", lambda: [["TCP", "127.0.0.1:9999", "0.0.0.0:0", "LISTENING", "10"]])
    monkeypatch.setattr(windows, "detached", lambda args, env: launches.append((args, env)) or {"pid": 20})
    monkeypatch.setattr(windows, "save_record", lambda cfg, r: saves.append(dict(r)))
    monkeypatch.setattr(windows, "correlate", lambda r: {"pid": 21})
    monkeypatch.setattr(windows.time, "sleep", lambda *args: None)
    result = windows.restore_desktop(cfg)
    assert len(launches) == 1 and launches[0][0] == [str(cfg.desktop_exe)]
    assert launches[0][1]["CODEX_APP_SERVER_WS_URL"] == record["endpoint"]
    assert result["backend"] == {"pid": 10} and result["peer"] == {"pid": 21}
    assert [r["status"] for r in saves] == ["restoring", "verified"]


class SelectingAdapter(Adapter):
    def __init__(self):
        super().__init__()
        self.options = [{"id": "candidate-1", "connectable": True}]

    def discover(self):
        self.calls.append("scan")
        return [dict(c) for c in self.options]

    def attach(self, candidate):
        self.calls.append("attach:" + candidate["id"])


def test_start_with_existing_desktop_waits_for_selection_without_native_restore():
    a = SelectingAdapter()
    s = Supervisor(a)
    s.action("Start")
    for _ in range(5):
        s.poll()
    assert s.selection_pending and not s.enabled
    assert a.calls == ["scan"]
    s.action("Connect selected", "candidate-1")
    assert s.enabled and a.calls.count("start") == 1
    assert "explicit_restore" not in a.calls
    s.action("Connect selected", "candidate-1")
    assert a.calls.count("attach:candidate-1") == 1 and a.calls.count("start") == 1


def test_unsupported_candidate_and_cancelled_selection_never_start_or_restore():
    a = SelectingAdapter()
    a.options[0]["connectable"] = False
    s = Supervisor(a)
    s.action("Scan instances")
    assert a.calls == ["scan"]  # Closing/cancelling the UI never invokes Connect.
    s.action("Connect selected", "candidate-1")
    assert s.last_action_code == "instance_not_connectable" and a.calls == ["scan"]


def test_no_desktop_keeps_explicit_controlled_start_path():
    a = SelectingAdapter()
    a.options = []
    s = Supervisor(a)
    s.action("Start")
    assert a.calls[:2] == ["scan", "explicit_restore"] and s.enabled


def test_scan_failure_preserves_an_already_managed_worker():
    a = SelectingAdapter()
    s = Supervisor(a)
    s.action("Start")
    s.action("Connect selected", "candidate-1")
    run = s.managed_run
    a.discover = lambda: (_ for _ in ()).throw(BridgeError("process_inventory_unavailable"))
    s.action("Scan instances")
    assert s.enabled and s.managed_run == run and a.status["running"]
    assert s.last_action_code == "process_inventory_unavailable"
    assert s.candidates == [] and not s.selection_pending


def test_adapter_refuses_switching_a_running_worker_without_touching_native_record(tmp_path, monkeypatch):
    a = OperatorAdapter.__new__(OperatorAdapter)
    a.cfg = object()
    candidate = {"desktop": {"pid": 20}, "backend": {"pid": 30}, "peer": {"pid": 20}, "port": 9000}
    a.command = lambda _: {"running": True, "native_chain": {"desktop": {"pid": 99}}}
    a.native = lambda: True
    monkeypatch.setattr(windows, "validate_instance", lambda *_: {})
    monkeypatch.setattr(windows, "adopt_instance", lambda *_: pytest.fail("must retain existing chain"))
    with pytest.raises(BridgeError, match="stop_bridge_before_switch"):
        a.attach(candidate)
