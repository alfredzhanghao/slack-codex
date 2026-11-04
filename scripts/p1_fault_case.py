"""Bounded real bridge crash at a persisted operation checkpoint; never replay writes."""

import argparse
import asyncio
import json
import re
import sqlite3
import subprocess
from ctypes import wintypes as wt
from pathlib import Path

from slack_codex_bridge import windows
from slack_codex_bridge.config import child_environment, load
from slack_codex_bridge.desktop import Desktop
from slack_codex_bridge.model import BridgeError, new_id, now_ms

from p1_acceptance_supervisor import DATA, ROOT, STATE, prepare, write_json


def validate_checkpoint(state, rule, checkpoint):
    if (rule.get("run_id") != state["run_id"] or checkpoint.get("run_id") != state["run_id"]
            or rule.get("id") != checkpoint.get("fault_id") or rule.get("stage") != checkpoint.get("stage")
            or not 0 <= now_ms() - checkpoint.get("observed_ms", 0) <= 60000
            or rule.get("expires_ms", 0) <= now_ms()):
        raise BridgeError("acceptance_crash_checkpoint_unverified")


async def verify_active_wait(cfg, record, state, status):
    tid = state["chat_preparation"]["controls"]["thread_id"]
    task = state.get("wait_trials", {}).get("controls", {}).get("crash_survival", {})
    binding = next(b for b in status["bindings"] if b["thread_id"] == tid)
    if (task.get("stage") != "accepted" or not task.get("turn_id")
            or binding["active_turn"] != task["turn_id"] or not binding["trusted"]
            or binding["pending_approval"]):
        raise BridgeError("acceptance_active_crash_target_unverified")
    backend = Desktop(cfg, record["endpoint"])
    await backend.connect()
    try:
        snapshot = await backend.snapshot(tid)
        if (not snapshot.trusted or snapshot.idle or snapshot.approval
                or snapshot.active_turn != task["turn_id"]):
            raise BridgeError("acceptance_active_crash_target_unverified")
        return tid, task["turn_id"]
    finally:
        await backend.close()


def run(args):
    cfg = load(ROOT / "bridge.local.json")
    with windows.single_instance("p1-acceptance-supervisor"):
        state = prepare()
        record = windows.verify_record(cfg)
        status = json.loads((DATA / "bridge-status.json").read_text())
        bridge = state["bridge"]
        if (status.get("process") != bridge or windows.process_identity(bridge["pid"]) != bridge
                or now_ms() - status["observed_ms"] > 5000 or state["deadline_ms"] <= now_ms()):
            raise BridgeError("acceptance_crash_bridge_identity_unverified")
        if args.mode == "arm":
            tid = state["chat_preparation"][args.slot]["thread_id"]
            binding = next(b for b in status["bindings"] if b["thread_id"] == tid)
            if (state.get("restart_intent") or not state.get("task_canary_passed")
                    or now_ms() - state.get("last_tick", {}).get("observed_ms", 0) > 120000
                    or binding["mode"] != "slack" or binding["active_turn"] or binding["blocked"]
                    or not binding["trusted"] or binding["pending_approval"]
                    or not re.fullmatch(r"\d+\.\d+", binding["root_ts"])):
                raise BridgeError("acceptance_crash_preconditions_unverified")
            rule = {"run_id": state["run_id"], "id": new_id(), "thread_id": tid,
                    "stage": args.stage, "kind": "turn_start",
                    "action": "commit_unavailable_once" if args.stage == "receipt" else "pause",
                    "expires_ms": now_ms() + 180000}
            state["restart_intent"] = {"run_id": state["run_id"], "kind": "ensure_bridge",
                                       "expires_ms": now_ms() + 300000}
            state["phase"] = "crash_test_armed_" + args.stage
            write_json(STATE, state)
            write_json(DATA / "fault-rule.json", rule)
            prefix = "R13-" if args.stage == "receipt" else "R11-"
            write_json(DATA / (prefix + rule["id"] + "-before.json"), status)
            print(json.dumps({"status": "armed", "fault_id": rule["id"], "stage": args.stage}))
            return
        if args.mode == "crash-active":
            tid, turn_id = asyncio.run(verify_active_wait(cfg, record, state, status))
            rule = {"id": "active-" + turn_id, "stage": "active_wait", "thread_id": tid}
            checkpoint = {"op_id": None}
            expected = "active_wait"
        else:
            rule = json.loads((DATA / "fault-rule.json").read_text())
            if rule.get("stage") == "receipt":
                raise BridgeError("acceptance_receipt_fault_is_not_crash_target")
            checkpoint = json.loads((DATA / "fault-checkpoint.json").read_text())
            validate_checkpoint(state, rule, checkpoint)
            expected = "received" if rule["stage"] == "received" else "sending"
        intent = state.get("restart_intent") or {}
        if (intent.get("run_id") != state["run_id"] or intent.get("kind") != "ensure_bridge"
                or intent.get("expires_ms", 0) <= now_ms() or intent.get("attempted")
                or not state.get("task_canary_passed")
                or now_ms() - state.get("last_tick", {}).get("observed_ms", 0) > 120000):
            raise BridgeError("acceptance_crash_recovery_not_armed")
        if args.mode != "crash-active":
            with sqlite3.connect((cfg.data_dir / "bridge.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
                row = db.execute("SELECT status,target_thread_id FROM operations WHERE op_id=?",
                                 (checkpoint["op_id"],)).fetchone()
            if row != (expected, rule["thread_id"]):
                raise BridgeError("acceptance_crash_operation_mismatch")
        if (state["task_name"] != "CodexP1Acceptance-" + state["run_id"]
                or not re.fullmatch(r"CodexP1Acceptance-[0-9a-f-]{36}", state["task_name"])):
            raise BridgeError("acceptance_recovery_task_invalid")
        kernel = windows._kernel()
        kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE
        handle = kernel.OpenProcess(1 | 0x1000, False, bridge["pid"])
        if not handle:
            raise BridgeError("acceptance_crash_process_access_failed")
        try:
            if windows._handle_identity(kernel, handle, bridge["pid"]) != bridge:
                raise BridgeError("acceptance_crash_process_identity_changed")
            evidence = {"run_id": state["run_id"], "fault_id": rule["id"], "stage": rule["stage"],
                        "op_id": checkpoint["op_id"], "thread_id": rule["thread_id"],
                        "bridge": bridge, "pre_crash_status": expected, "observed_ms": now_ms()}
            if args.mode == "crash-active":
                evidence.update(turn_id=turn_id, original_controlled_session=record,
                    task_origin="acceptance_backend_client_not_gui")
            write_json(DATA / ("R11-" + rule["id"] + "-crash.json"), evidence)
            kernel.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
            if not kernel.TerminateProcess(handle, 17):
                raise BridgeError("acceptance_crash_stop_failed")
        finally:
            kernel.CloseHandle.argtypes = [wt.HANDLE]
            kernel.CloseHandle(handle)
        # Independent scheduler is already armed; trigger it immediately as well.
        subprocess.run([str(Path(__import__("os").environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
                        "-NoProfile", "-NonInteractive", "-Command",
                        "Start-ScheduledTask -TaskName '" + state["task_name"] + "'"],
                       env=child_environment(cfg), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, check=True, timeout=15)
        print(json.dumps({"status": "crashed_recovery_triggered", "op_id": checkpoint["op_id"], "stage": rule["stage"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["arm", "crash", "crash-active"])
    parser.add_argument("--stage", choices=["received", "sending", "transport_written", "receipt"], default="received")
    parser.add_argument("--slot", choices=["primary", "secondary", "controls"], default="primary")
    try:
        run(parser.parse_args())
    except BridgeError as error:
        print(json.dumps({"status": "unproven", "code": error.code}))
        raise SystemExit(1) from None
