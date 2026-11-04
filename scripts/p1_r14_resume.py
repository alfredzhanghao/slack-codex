"""Resume only the proven stopped-backend tail; never repeat model writes or approvals."""

import argparse
import asyncio
import json
import os
import time
from ctypes import wintypes as wt

from slack_codex_bridge import windows
from slack_codex_bridge.config import load
from slack_codex_bridge.desktop import Desktop
from slack_codex_bridge.model import BridgeError, new_id, now_ms

from p1_acceptance_supervisor import DATA, ROOT, prepare
from p1_r14_lifecycle import Worker, mutate, trigger_task


def validate_authorization(auth, run_id, ordinary):
    if (auth.get("status") != "granted" or auth.get("run_id") != run_id
            or auth.get("scope") != "r14_stopped_tail_single_switch"
            or auth.get("ordinary_desktop") != ordinary
            or not 0 <= now_ms() - auth.get("granted_ms", 0) <= 600000):
        raise BridgeError("r14_explicit_current_switch_authorization_required")


def validate_stopped(state, record, evidence):
    expected = {"bridge_running": "another_instance_is_running",
        "desktop_open": "desktop_still_open_stop_refused",
        "active": "active_or_untrusted_thread_stop_refused",
        "approval": "active_or_untrusted_thread_stop_refused"}
    if (state.get("overall") != "INCOMPLETE" or record.get("status") != "stopped"
            or evidence.get("run_id") != state.get("run_id")
            or evidence.get("original_decline_count") != 1
            or not evidence.get("final_idle_audit")
            or any(row["status"].get("type") != "idle" or row["status"].get("activeFlags")
                for row in evidence["final_idle_audit"])
            or any(evidence.get("assertions", {}).get(key, {}).get("refusal") != code
                or not evidence["assertions"][key].get("backend_unchanged") for key, code in expected.items())
            or set(evidence.get("actions", {})) != {"wait_chat", "approval_chat", "wait_turn", "approval_turn"}
            or any(row.get("stage") != "accepted" for row in evidence["actions"].values())
            or any(record.get(key) != evidence["original_session"].get(key)
                for key in ("backend", "desktop", "endpoint"))):
        raise BridgeError("r14_stopped_tail_evidence_unproven")
    if any(state["cases"][f"R{n:02}"]["status"] != "passed" for n in range(1, 14)):
        raise BridgeError("r14_prior_cases_unproven")


def verify_absent(cfg, record, ordinary=None):
    if (windows.process_identity(record["backend"]["pid"]) is not None
            or windows.process_identity(record["desktop"]["pid"]) is not None):
        raise BridgeError("r14_original_process_still_present")
    windows.verify_versions(cfg)
    if any(windows.binary_hash(path) != record[key] for path, key in (
            (cfg.backend_exe, "backend_hash"), (cfg.desktop_exe, "desktop_hash"))):
        raise BridgeError("r14_binary_changed")
    rows = windows.processes()
    parents = {row["pid"]: row["parent"] for row in rows}
    if ordinary and windows.process_identity(ordinary["pid"]) != ordinary:
        raise BridgeError("r14_app_identity_changed")
    for row in rows:
        if row["name"].casefold() == cfg.backend_exe.name.casefold():
            identity = windows.process_identity(row["pid"])
            if identity and identity["path"].casefold() == str(cfg.backend_exe.resolve()).casefold():
                ancestor = row["pid"]
                for _ in range(16):
                    if ordinary and ancestor == ordinary["pid"]:
                        break
                    ancestor = parents.get(ancestor)
                else:
                    raise BridgeError("r14_existing_backend_requires_review")


def stop_exact_idle_app(cfg, intent):
    validate_authorization(intent.get("explicit_authorization", {}), intent["run_id"], intent["ordinary_desktop"])
    audit = intent["app_idle_audit"]
    if (not 0 <= now_ms() - audit["observed_ms"] <= 120000
            or audit["other_active_threads"] or not audit["coverage_since_app_start"]):
        raise BridgeError("r14_app_idle_audit_unproven")
    identity = intent["ordinary_desktop"]
    if identity["path"].casefold() != str(cfg.desktop_exe.resolve()).casefold():
        raise BridgeError("r14_app_path_changed")
    if windows.process_identity(identity["pid"]) != identity:
        raise BridgeError("r14_app_identity_changed")
    kernel = windows._kernel()
    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.OpenProcess.restype = wt.HANDLE
    handle = kernel.OpenProcess(1 | 0x1000 | 0x100000, False, identity["pid"])
    if not handle:
        raise BridgeError("r14_app_process_access_failed")
    try:
        if windows._handle_identity(kernel, handle, identity["pid"]) != identity:
            raise BridgeError("r14_app_identity_changed")
        kernel.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
        kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
        kernel.WaitForSingleObject.restype = wt.DWORD
        # Explicitly authorized non-UI lifecycle operation; no window messaging/input.
        if not kernel.TerminateProcess(handle, 0) or kernel.WaitForSingleObject(handle, 10000) != 0:
            raise BridgeError("r14_app_exit_unproven")
    finally:
        kernel.CloseHandle.argtypes = [wt.HANDLE]
        kernel.CloseHandle(handle)
    for _ in range(40):
        if not any(r["name"].casefold() == cfg.desktop_exe.name.casefold() for r in windows.processes()):
            return
        time.sleep(.25)
    raise BridgeError("r14_app_children_remain")


async def resume(cfg, intent):
    source = DATA / intent["evidence_file"]
    if source.parent != DATA or source.name != "R14-stopped-tail-source.json":
        raise BridgeError("r14_evidence_path_invalid")
    evidence = json.loads(source.read_text())
    record = windows.read_record(cfg)
    validate_stopped(prepare(), record, evidence)
    verify_absent(cfg, record, intent["ordinary_desktop"])
    worker = Worker(cfg, intent)
    worker.evidence = evidence
    worker.evidence["resume_intent_id"] = intent["id"]
    worker.evidence["prior_failure_code"] = worker.evidence.pop("code", None)
    try:
        worker.save("stopped_tail_verified", owned_inactive_backend_stopped=True)
        worker.save("idle_app_exit_sending")
        stop_exact_idle_app(cfg, intent)
        worker.save("idle_app_exited")
        verify_absent(cfg, record)
        worker.save("new_controlled_session_attempted")
        worker.record = windows.start(cfg)
        worker.save("new_controlled_session_verified")
        backend = Desktop(cfg, worker.record["endpoint"])
        await backend.connect()
        try:
            terminals = {}
            for label in ("wait", "approval"):
                tid = evidence["actions"][label + "_chat"]["thread_id"]
                turn = evidence["actions"][label + "_turn"]["turn_id"]
                status, _ = await backend.final_messages(tid, turn)
                if status != "completed":
                    raise BridgeError("r14_original_fixture_not_completed")
                terminals[label] = {"thread_id": tid, "turn_id": turn, "status": status}
        finally:
            await backend.close()
        worker.save("automatic_bridge_return", lifecycle_assertions_passed=True,
            original_fixture_terminals=terminals,
            returned_session={k: worker.record[k] for k in ("backend", "desktop", "peer", "endpoint")})
        worker.schedule_bridge(worker.record)
        await worker.verify_return()
        await worker.notify("resume_return_complete", "R14 停止后的恢复检查已完成：受控桌面与桥接已自动恢复。"
            "无需再次关闭或回复，验收会话将核对证据并生成报告。")
        mutate(lambda s: s.update(phase="r14_returned_wait_report"))
    except BaseException as error:
        worker.save("resume_unproven", code=error.code if isinstance(error, BridgeError) else "r14_resume_failed")
    finally:
        if worker.notice_web and worker.notice_web.client.session:
            await worker.notice_web.client.session.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["arm", "worker"])
    parser.add_argument("--intent-id")
    args = parser.parse_args(argv)
    cfg = load(ROOT / "bridge.local.json")
    if args.mode == "arm":
        def arm(state):
            evidence = json.loads((DATA / "R14-stopped-tail-source.json").read_text())
            record = windows.read_record(cfg)
            validate_stopped(state, record, evidence)
            ready = state["r14_resume_preparation"]
            validate_authorization(ready.get("explicit_authorization", {}), state["run_id"], ready["ordinary_desktop"])
            verify_absent(cfg, record, ready["ordinary_desktop"])
            if (not state.get("task_canary_passed") or not state.get("task_probe_passed")
                    or state.get("restart_intent") or ready.get("armed")
                    or state["deadline_ms"] - now_ms() < 600000
                    or not 0 <= now_ms() - state["last_tick"]["observed_ms"] <= 120000):
                raise BridgeError("r14_resume_preconditions_unproven")
            intent = {"kind": "r14_resume_stopped", "id": new_id(), "run_id": state["run_id"],
                "original_record": record, "evidence_file": "R14-stopped-tail-source.json",
                "ordinary_desktop": ready["ordinary_desktop"], "app_idle_audit": ready["app_idle_audit"],
                "explicit_authorization": ready["explicit_authorization"].copy(),
                "notification_root": state["assisted_gui"]["root_ts"],
                "notification_thread_id": state["assisted_gui"]["thread_id"],
                "not_before_ms": now_ms() + 25000, "expires_ms": now_ms() + 480000}
            state["restart_intent"] = intent
            ready["armed"] = True
            ready["explicit_authorization"]["status"] = "consumed"
            return state
        state = mutate(arm)
        trigger_task(state, cfg)
        print(json.dumps({"status": "armed", "kind": "r14_resume_stopped"}))
        return
    with windows.single_instance("p1-r14-lifecycle"):
        state = prepare()
        intent = state.get("restart_intent") or {}
        for _ in range(50):
            if intent.get("worker_launcher"):
                break
            time.sleep(.1)
            intent = prepare().get("restart_intent") or {}
        if (intent.get("kind") != "r14_resume_stopped" or intent.get("id") != args.intent_id
                or intent.get("expires_ms", 0) <= now_ms()):
            raise BridgeError("r14_resume_intent_invalid")
        launcher = intent["worker_launcher"]
        parents = {p["pid"]: p["parent"] for p in windows.processes()}
        ancestor = os.getpid()
        for _ in range(16):
            if ancestor == launcher["pid"] and windows.process_identity(ancestor) == launcher:
                break
            ancestor = parents.get(ancestor)
        else:
            raise BridgeError("r14_resume_independence_unproven")
        asyncio.run(resume(cfg, intent))


if __name__ == "__main__":
    main()
