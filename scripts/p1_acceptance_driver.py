"""Case-specific real-host assertions; never synthesize Slack user input."""

import argparse
import json
import sys

from slack_codex_bridge import windows
from slack_codex_bridge.config import load
from slack_codex_bridge.model import BridgeError, new_id, now_ms

from p1_acceptance_supervisor import DATA, ROOT, STATE, prepare, write_json


def ordinary_roots(cfg):
    rows = [p for p in windows.processes() if p["name"].casefold() == cfg.desktop_exe.name.casefold()]
    ids = {p["pid"] for p in rows}
    roots = [windows.process_identity(p["pid"]) for p in rows if p["parent"] not in ids]
    if not roots or any(not p or p["path"].casefold() != str(cfg.desktop_exe.resolve()).casefold() for p in roots):
        raise BridgeError("acceptance_ordinary_desktop_identity_unavailable")
    return roots


def r01(cfg):
    before = ordinary_roots(cfg)
    try:
        windows.start(cfg)
    except BridgeError as error:
        if error.code != "desktop_already_open_close_it_manually":
            raise
        if any(windows.process_identity(p["pid"]) != p for p in before):
            raise BridgeError("acceptance_ordinary_desktop_changed")
        return {"status": "passed", "refusal": error.code, "ordinary_roots_unchanged": before}
    raise BridgeError("acceptance_r01_failed_to_refuse")


def main(argv=None):
    parser = argparse.ArgumentParser(description="P1 real-machine bounded case driver")
    parser.add_argument("case", choices=["R01", "arm-handoff", "arm-bridge", "arm-shutdown"])
    args = parser.parse_args(argv)
    stage = "config"
    try:
        cfg = load(ROOT / "bridge.local.json")
        stage = "checkpoint"
        with windows.single_instance("p1-acceptance-supervisor"):
            state = prepare()
            if not state.get("task_probe_passed") or not state.get("preflight", {}).get("socket_app_verified"):
                raise BridgeError("acceptance_preconditions_unverified")
            stage = args.case
            if args.case == "arm-shutdown":
                intent = state.get("restart_intent") or {}
                bridge = state.get("bridge")
                if (intent.get("kind") != "ensure_bridge" or intent.get("run_id") != state["run_id"]
                        or intent.get("expires_ms", 0) <= now_ms() or not bridge
                        or not state.get("task_canary_passed")
                        or now_ms() - state.get("last_tick", {}).get("observed_ms", 0) > 120000
                        or windows.process_identity(bridge["pid"]) != bridge):
                    raise BridgeError("acceptance_shutdown_recovery_not_armed")
                windows.verify_record(cfg)
                command = {"run_id": state["run_id"], "id": new_id(), "action": "shutdown",
                           "expires_ms": now_ms() + 90000}
                write_json(DATA / "command.json", command)
                state["phase"] = "bridge_restart_waiting_exit"
                write_json(STATE, state)
                print(json.dumps({"status": "shutdown_requested", "command_id": command["id"]}))
                return 0
            if args.case == "arm-bridge":
                if (state["deadline_ms"] <= now_ms() or state.get("restart_intent")
                        or state["cases"]["R02"]["status"] != "passed" or not state.get("test_threads")
                        or not state.get("task_canary_passed")
                        or now_ms() - state.get("last_tick", {}).get("observed_ms", 0) > 120000):
                    raise BridgeError("acceptance_bridge_preconditions_unverified")
                windows.verify_record(cfg)
                state["restart_intent"] = {"run_id": state["run_id"], "kind": "ensure_bridge",
                                           "expires_ms": now_ms() + 120000}
                state["phase"] = "bridge_start_armed"
                write_json(STATE, state)
                print(json.dumps({"status": "armed", "task_name": state["task_name"]}))
                return 0
            if args.case == "arm-handoff":
                authorization = state.get("handoff_authorization", {})
                if authorization.get("status") != "granted" or authorization.get("run_id") != state["run_id"]:
                    raise BridgeError("acceptance_explicit_handoff_exception_required")
                if not state.get("task_canary_passed"):
                    raise BridgeError("acceptance_scheduled_child_survival_unverified")
                if state.get("ui_control", {}).get("status") == "await_human_continue":
                    raise BridgeError("acceptance_computer_use_stopped_by_user")
                audit = json.loads((DATA / "app-idle-audit.json").read_text(encoding="utf-8"))
                if (audit.get("orchestrator_thread") != "01a1786b-d49d-7243-b5e7-936ba8985fdc"
                        or audit.get("other_active_threads") != [] or not 0 <= now_ms() - audit.get("observed_ms", 0) <= 60000):
                    raise BridgeError("acceptance_idle_audit_expired_or_busy")
                roots = ordinary_roots(cfg)
                if len(roots) != 1 or state.get("restart_intent"):
                    raise BridgeError("acceptance_handoff_scope_not_unique")
                previous = state["cases"]["R01"]
                if previous.get("status") != "passed" or roots != previous.get("ordinary_roots_unchanged"):
                    raise BridgeError("acceptance_r01_target_changed")
                state["restart_intent"] = {"run_id": state["run_id"], "kind": "replace_ordinary_with_controlled",
                    "ordinary_desktop": roots[0], "idle_audit": audit, "not_before_ms": now_ms() + 25000,
                    "expires_ms": now_ms() + 180000}
                state["phase"] = "restart_armed"
                write_json(STATE, state)
                print(json.dumps({"case": "R02", "status": "armed", "task_name": state["task_name"]}))
                return 0
            result = r01(cfg)
            stage = "evidence"
            result.update(observed_ms=now_ms(), run_id=state["run_id"], case=args.case)
            write_json(DATA / "cases" / f"{args.case}.json", result)
            state["cases"][args.case] = result
            write_json(STATE, state)
        print(json.dumps({"case": args.case, "status": result["status"]}))
        return 0
    except BridgeError as error:
        print(json.dumps({"case": args.case, "status": "not_run", "code": error.code}))
        return 1
    except Exception as error:
        categories = {PermissionError: "permission_denied", UnicodeError: "encoding_error",
                      UnicodeDecodeError: "encoding_error", OSError: "os_error", TypeError: "type_error",
                      KeyError: "schema_error", RuntimeError: "runtime_error"}
        code = categories.get(type(error), "acceptance_driver_failed")
        print(json.dumps({"case": args.case, "status": "not_run", "code": code, "stage": stage}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
