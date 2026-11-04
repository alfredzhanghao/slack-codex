"""A bounded Windows Task Scheduler recovery entry point; no credentials in state.

Only explicit, identity-checked recovery intents may launch a controlled session.
There is no implicit desktop shutdown and no business-operation replay.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from ctypes import wintypes as wt

from slack_codex_bridge import windows
from slack_codex_bridge.config import child_environment, load
from slack_codex_bridge.model import BridgeError, new_id, now_ms

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".work-p1" / "acceptance"
STATE = DATA / "state.json"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def prepare():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    state = {"schema": 1, "run_id": new_id(), "created_ms": now_ms(),
             "deadline_ms": now_ms() + 8 * 3600000, "overall": "INCOMPLETE", "phase": "preparing",
             "automation_id": "python-p1", "task_name": None, "restart_intent": None,
             "cases": {f"R{n:02}": {"status": "not_run"} for n in range(1, 15)}}
    write_json(STATE, state)
    return state


def renew_after_human_grant(state):
    authorization = state.get("handoff_authorization", {})
    age = now_ms() - authorization.get("granted_ms", 0)
    if (authorization.get("status") != "granted" or authorization.get("run_id") != state["run_id"]
            or not 0 <= age <= 600000 or state.get("restart_intent")):
        raise BridgeError("acceptance_fresh_human_grant_required_for_renewal")
    state.setdefault("window_history", []).append({"previous_deadline_ms": state["deadline_ms"],
                                                    "renewed_ms": now_ms()})
    state["deadline_ms"] = now_ms() + 8 * 3600000
    state["phase"] = "await_test_driver"
    state["task_probe_passed"] = False
    state["task_canary_passed"] = False
    state["last_fault"] = None
    return state


def renew_verified_continuation(state, cfg):
    """Continue the already-authorized P1 scope without granting a desktop handoff."""
    if (state.get("overall") != "INCOMPLETE" or state["deadline_ms"] >= now_ms()
            or state.get("restart_intent")
            or state.get("ui_control", {}).get("status") == "await_human_continue"):
        raise BridgeError("acceptance_verified_continuation_preconditions_failed")
    bridge = state.get("bridge")
    if bridge and windows.process_identity(bridge["pid"]) is not None:
        raise BridgeError("acceptance_existing_bridge_requires_review")
    record = windows.verify_record(cfg)
    owned = state.get("controlled_session", {})
    if any(record.get(key) != owned.get(key) for key in ("backend", "desktop", "endpoint")):
        raise BridgeError("acceptance_original_controlled_session_changed")
    state.setdefault("window_history", []).append({"previous_deadline_ms": state["deadline_ms"],
        "renewed_ms": now_ms(), "basis": "standing_unattended_p1_scope_original_session_verified"})
    state["deadline_ms"] = now_ms() + 8 * 3600000
    state["phase"] = "await_test_driver"
    state["task_probe_passed"] = state["task_canary_passed"] = False
    state["last_fault"] = None
    # In particular, never reset or synthesize handoff_authorization.
    return state


def scheduled_canary(state, cfg):
    request_path = DATA / "canary-request.json"
    if not request_path.exists():
        return
    request = json.loads(request_path.read_text(encoding="utf-8-sig"))
    if (request.get("run_id") != state["run_id"] or request.get("request_id") == state.get("canary", {}).get("request_id")
            or not now_ms() < request.get("expires_ms", 0) <= now_ms() + 90000):
        return
    child = windows.detached([sys.executable, str(ROOT / "scripts/p1_recovery_canary.py"),
                              state["run_id"], request["request_id"]], child_environment(cfg))
    state["canary"] = {"request_id": request["request_id"], "child": child, "launched_ms": now_ms()}
    state["task_canary_passed"] = False


def probe(state, cfg):
    # Preflight requires captured stdout even when the supervisor runs under pythonw.
    console_python = str(Path(sys.executable).with_name("python.exe"))
    result = subprocess.run([console_python, str(ROOT / "scripts/p1_acceptance_bootstrap.py"), "preflight"],
                            cwd=ROOT, env=child_environment(cfg), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=60,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise BridgeError("acceptance_preflight_result_unavailable") from None
    permitted = {"status", "code", "versions_match", "credentials_valid", "team_verified",
                 "dm_owner_verified", "socket_app_verified", "dm_verification"}
    if not isinstance(value, dict) or not set(value) <= permitted:
        raise BridgeError("acceptance_preflight_result_invalid")
    value["observed_ms"] = now_ms()
    write_json(DATA / "preflight.json", value)
    state["preflight"] = value
    if result.returncode:
        raise BridgeError(value.get("code", "acceptance_preflight_failed"))


def recover(state, cfg):
    intent = state.get("restart_intent")
    if not intent:
        return
    if state.get("ui_control", {}).get("status") == "await_human_continue":
        return
    if now_ms() < intent.get("not_before_ms", 0):
        return
    # The driver must arm this before the lifecycle action, not after it.
    if not state.get("task_probe_passed") or intent.get("run_id") != state["run_id"]:
        raise BridgeError("acceptance_recovery_not_armed")
    if intent.get("expires_ms", 0) < now_ms():
        raise BridgeError("acceptance_recovery_intent_expired")
    if intent.get("kind") == "replace_ordinary_with_controlled":
        controlled_handoff(state, cfg, intent)
    elif intent.get("kind") in {"r14_lifecycle", "r14_resume_stopped"}:
        if not state.get("task_canary_passed"):
            raise BridgeError("acceptance_scheduled_child_survival_unverified")
        launcher = intent.get("worker_launcher")
        if launcher and windows.process_identity(launcher["pid"]) == launcher:
            return
        if intent.get("attempted"):
            raise BridgeError("r14_worker_launch_not_replayed")
        # Recheck that no earlier GUI case was waived after arming.
        if any(state["cases"][key]["status"] != "passed" for key in ("R04", "R06", "R08", "R10")):
            raise BridgeError("r14_finish_assisted_gui_cases_first")
        if intent["kind"] == "r14_resume_stopped":
            from p1_r14_resume import validate_stopped, verify_absent
            record = windows.read_record(cfg)
            evidence = json.loads((DATA / "R14-stopped-tail-source.json").read_text())
            validate_stopped(state, record, evidence)
            verify_absent(cfg, record, intent["ordinary_desktop"])
        else:
            record = windows.verify_record(cfg)
        if any(record.get(key) != intent["original_record"].get(key)
                for key in ("backend", "desktop", "endpoint")):
            raise BridgeError("r14_original_session_changed")
        intent["attempted"] = True
        write_json(STATE, state)
        intent["worker_launcher"] = windows.detached([sys.executable,
            str(ROOT / "scripts/p1_acceptance_bootstrap.py"),
            "r14-resume" if intent["kind"] == "r14_resume_stopped" else "r14-worker", "--run-id", state["run_id"],
            "--intent-id", intent["id"]], child_environment(cfg))
        write_json(STATE, state)
        return
    elif intent.get("kind") == "ensure_controlled_desktop":
        try:
            record = windows.verify_record(cfg)
        except BridgeError as error:
            # Only a missing record permits a fresh start. Never overwrite an uncertain
            # owned record or adopt a reachable port as proof of identity.
            if error.code != "controlled_session_record_unavailable":
                raise
            record = windows.start(cfg)
        state["controlled_session"] = {"backend": record["backend"], "desktop": record["desktop"]}
    elif intent.get("kind") == "ensure_bridge":
        windows.verify_record(cfg)
        old = state.get("bridge")
        if old and windows.process_identity(old["pid"]) == old:
            return
        if intent.get("attempted"):
            raise BridgeError("acceptance_bridge_launch_not_replayed")
        intent["attempted"] = True
        write_json(STATE, state)
        state["bridge_launcher"] = windows.detached([sys.executable,
            str(ROOT / "scripts/p1_acceptance_bootstrap.py"), "bridge", "--run-id", state["run_id"]], child_environment(cfg))
        for _ in range(60):
            status_path = DATA / "bridge-status.json"
            if status_path.exists():
                runtime = json.loads(status_path.read_text(encoding="utf-8"))
                actual = runtime.get("process")
                parents = {p["pid"]: p["parent"] for p in windows.processes()}
                ancestor = actual.get("pid") if actual else None
                owned = False
                for _ in range(16):
                    if ancestor == state["bridge_launcher"]["pid"]:
                        owned = windows.process_identity(ancestor) == state["bridge_launcher"]
                        break
                    ancestor = parents.get(ancestor)
                    if not ancestor:
                        break
                if (runtime.get("run_id") == state["run_id"] and actual and owned
                        and windows.process_identity(actual["pid"]) == actual):
                    state["bridge"] = actual
                    break
            time.sleep(0.1)
        else:
            raise BridgeError("acceptance_bridge_identity_unavailable")
    else:
        raise BridgeError("acceptance_recovery_intent_invalid")
    state["restart_intent"] = None
    if state["phase"] != "controlled_ready_for_test_driver":
        state["phase"] = "await_test_driver"


def controlled_handoff(state, cfg, intent):
    authorization = state.get("handoff_authorization", {})
    if authorization.get("status") != "granted" or authorization.get("run_id") != state.get("run_id"):
        raise BridgeError("acceptance_explicit_handoff_exception_required")
    if not state.get("task_canary_passed"):
        raise BridgeError("acceptance_scheduled_child_survival_unverified")
    audit = intent.get("idle_audit", {})
    if (audit.get("orchestrator_thread") != "01a1786b-d49d-7243-b5e7-936ba8985fdc"
            or audit.get("other_active_threads") != [] or not 0 <= now_ms() - audit.get("observed_ms", 0) <= 120000):
        raise BridgeError("acceptance_other_tasks_not_verified_idle")
    old = intent["ordinary_desktop"]
    if old["path"].casefold() != str(cfg.desktop_exe.resolve()).casefold():
        raise BridgeError("acceptance_handoff_target_mismatch")
    if intent.get("attempted"):
        raise BridgeError("acceptance_handoff_not_replayed")
    if windows.record_path(cfg).exists():
        raise BridgeError("acceptance_existing_controlled_record_requires_review")
    windows.verify_versions(cfg)
    current = windows.process_identity(old["pid"])
    if current != old:
        raise BridgeError("acceptance_handoff_process_identity_changed")
    intent["attempted"] = True
    authorization["status"] = "consumed"
    state["phase"] = "controlled_handoff_in_progress"
    write_json(STATE, state)
    # Non-UI process lifecycle action explicitly authorized for this exact restart.
    # Open and verify the same handle before terminating to avoid PID reuse.
    kernel = windows._kernel()
    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.OpenProcess.restype = wt.HANDLE
    handle = kernel.OpenProcess(1 | 0x1000, False, old["pid"])
    if not handle:
        raise BridgeError("acceptance_handoff_process_access_failed")
    try:
        if windows._handle_identity(kernel, handle, old["pid"]) != old:
            raise BridgeError("acceptance_handoff_process_identity_changed")
        kernel.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
        if not kernel.TerminateProcess(handle, 0):
            raise BridgeError("acceptance_handoff_stop_failed")
    finally:
        kernel.CloseHandle.argtypes = [wt.HANDLE]
        kernel.CloseHandle(handle)
    state["ordinary_desktop_stopped_ms"] = now_ms()
    write_json(STATE, state)
    try:
        for _ in range(40):
            if not any(p["name"].casefold() == cfg.desktop_exe.name.casefold() for p in windows.processes()):
                break
            time.sleep(0.25)
        else:
            raise BridgeError("acceptance_desktop_children_still_running")
        record = windows.start(cfg)

        async def verify_backend():
            from slack_codex_bridge.desktop import RPC
            rpc = RPC(record["endpoint"], cfg.request_timeout, lambda *_: None)
            try:
                await rpc.connect()
                await rpc.call("thread/list", {"limit": 1})
            finally:
                await rpc.close()
        asyncio.run(verify_backend())
        state["controlled_session"] = {"backend": record["backend"], "desktop": record["desktop"],
                                       "peer": record["peer"], "endpoint": record["endpoint"]}
        evidence = {"status": "passed", "case": "R02", "run_id": state["run_id"],
                    "observed_ms": now_ms(), "controlled_session": state["controlled_session"],
                    "backend_read_verified": True, "credentials_not_in_desktop_environment": True}
        write_json(DATA / "cases/R02.json", evidence)
        state["cases"]["R02"] = evidence
        state["phase"] = "controlled_ready_for_test_driver"
    except Exception:
        # Preserve all owned evidence. Restore the app only if there is no configured
        # desktop at all; never repeatedly launch on an uncertain existing instance.
        if not any(p["name"].casefold() == cfg.desktop_exe.name.casefold() for p in windows.processes()):
            state["fallback_desktop"] = windows.detached([str(cfg.desktop_exe)], child_environment(cfg))
        state["restart_intent"] = None
        state["phase"] = "handoff_failed_app_restore_attempted"
        write_json(STATE, state)
        raise BridgeError("acceptance_controlled_handoff_failed") from None


def tick(*, preflight=False, scheduled=False):
    cfg = load(ROOT / "bridge.local.json")
    with windows.single_instance("p1-acceptance-supervisor"):
        state = prepare()
        identity = windows.process_identity(os.getpid())
        state["last_tick"] = {"observed_ms": now_ms(), "process": identity, "scheduled": scheduled}
        parent = next((p["parent"] for p in windows.processes() if p["pid"] == os.getpid()), None)
        if parent:
            state["last_tick"]["parent"] = windows.process_identity(parent)
        if state["deadline_ms"] < now_ms():
            state["phase"] = "expired"
            state["restart_intent"] = None
            write_json(STATE, state)
            return state
        try:
            if scheduled:
                state["task_probe_passed"] = True
                scheduled_canary(state, cfg)
            if preflight:
                probe(state, cfg)
            intent = state.get("restart_intent")
            if intent and now_ms() < intent.get("not_before_ms", 0):
                # Service-owned scheduled invocation waits briefly, independently of
                # the agent/desktop, so the initiating turn can finish saving its work.
                time.sleep(min(30, max(0, (intent["not_before_ms"] - now_ms()) / 1000)))
            recover(state, cfg)
            state["last_fault"] = None
            if state["phase"] == "preparing":
                state["phase"] = "await_test_driver"
        except BridgeError as error:
            state["last_fault"] = error.code
        except Exception:
            state["last_fault"] = "acceptance_supervisor_failed"
        write_json(STATE, state)
        return state


def main(argv=None):
    parser = argparse.ArgumentParser(description="P1 bounded independent recovery supervisor")
    parser.add_argument("mode", choices=["prepare", "renew", "renew-verified", "register", "confirm-canary", "tick", "preflight", "scheduled", "watch"])
    args = parser.parse_args(argv)
    try:
        if args.mode == "prepare":
            state = prepare()
        elif args.mode == "renew":
            with windows.single_instance("p1-acceptance-supervisor"):
                state = renew_after_human_grant(prepare())
                write_json(STATE, state)
        elif args.mode == "renew-verified":
            with windows.single_instance("p1-acceptance-supervisor"):
                state = renew_verified_continuation(prepare(), load(ROOT / "bridge.local.json"))
                write_json(STATE, state)
        elif args.mode == "register":
            with windows.single_instance("p1-acceptance-supervisor"):
                state = prepare()
                state["task_name"] = "CodexP1Acceptance-" + state["run_id"]
                write_json(STATE, state)
        elif args.mode == "confirm-canary":
            with windows.single_instance("p1-acceptance-supervisor"):
                state = prepare()
                result = json.loads((DATA / "canary-result.json").read_text(encoding="utf-8"))
                request = json.loads((DATA / "canary-request.json").read_text(encoding="utf-8-sig"))
                if (result.get("run_id") != state["run_id"] or result.get("request_id") != request.get("request_id")
                        or result.get("request_id") != state.get("canary", {}).get("request_id")
                        or result.get("status") != "passed" or not result.get("task_completed_before_child_exit")):
                    raise BridgeError("acceptance_scheduled_child_survival_unverified")
                state["task_canary_passed"] = True
                write_json(STATE, state)
        elif args.mode == "watch":
            while True:
                state = tick()
                if state["phase"] in {"expired", "complete"}:
                    break
                time.sleep(20)
        else:
            state = tick(preflight=args.mode == "preflight", scheduled=args.mode == "scheduled")
        print(json.dumps({k: state.get(k) for k in
                          ("run_id", "phase", "task_name", "task_probe_passed", "last_fault")}))
        # Read/registration commands did not execute the previously failed recovery.
        # Preserve its diagnostic without reporting that historical fault as a new
        # preparation failure, which would prevent a fresh scheduled probe.
        return 0 if args.mode in {"prepare", "renew", "renew-verified", "register", "confirm-canary"} or not state.get("last_fault") else 1
    except BridgeError as error:
        print(json.dumps({"status": "blocked", "code": error.code}))
        return 1
    except Exception:
        print(json.dumps({"status": "blocked", "code": "acceptance_supervisor_failed"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
