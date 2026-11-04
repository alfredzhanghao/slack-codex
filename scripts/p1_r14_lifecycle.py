"""Independent, one-shot R14 worker. The human closes the GUI; this code never does."""

import argparse
import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path

from slack_codex_bridge import windows
from slack_codex_bridge.config import child_environment, load
from slack_codex_bridge.desktop import Desktop
from slack_codex_bridge.model import BridgeError, new_id, now_ms
from slack_codex_bridge.slack import Web

from p1_acceptance_supervisor import DATA, ROOT, STATE, prepare, write_json
from p1_live_chat import approval_instructions, wait_instructions


def mutate(change):
    with windows.single_instance("p1-acceptance-supervisor"):
        state = prepare()
        result = change(state)
        write_json(STATE, state)
        return result


def validate_arm(state, record):
    if (state.get("overall") != "INCOMPLETE" or state["deadline_ms"] - now_ms() < 900000
            or state.get("restart_intent") or state.get("r14_lifecycle")
            or not state.get("task_probe_passed") or not state.get("task_canary_passed")
            or not 0 <= now_ms() - state.get("last_tick", {}).get("observed_ms", 0) <= 120000
            or state.get("ui_control", {}).get("status") == "await_human_continue"):
        raise BridgeError("r14_recovery_preconditions_unverified")
    if any(state["cases"][case]["status"] != "passed" for case in ("R04", "R06", "R08", "R10")):
        raise BridgeError("r14_finish_assisted_gui_cases_first")
    owned = state["controlled_session"]
    if any(record.get(key) != owned.get(key) for key in ("backend", "desktop", "endpoint")):
        raise BridgeError("r14_original_session_changed")


def trigger_task(state, cfg):
    task = state.get("task_name", "")
    if (task != "CodexP1Acceptance-" + state["run_id"]
            or not re.fullmatch(r"CodexP1Acceptance-[0-9a-f-]{36}", task)):
        raise BridgeError("r14_task_identity_invalid")
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-Command",
        "Start-ScheduledTask -TaskName '" + task + "'"], env=child_environment(cfg),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW, timeout=15, check=True)


class Worker:
    def __init__(self, cfg, intent):
        self.cfg, self.intent = cfg, intent
        self.record = intent["original_record"]
        self.original = self.record.copy()
        self.backend = Desktop(cfg, self.record["endpoint"])
        self.evidence = {"run_id": intent["run_id"], "intent_id": intent["id"],
            "stage": "preparing", "actions": {}, "assertions": {}, "notifications": {},
            "original_session": {key: self.record.get(key) for key in ("backend", "desktop", "peer", "endpoint")}}
        self.gui_launch_attempted = self.new_session_attempted = False
        self.notice_web = None

    async def notify(self, label, text):
        state = prepare()
        root = state.get("assisted_gui", {}).get("root_ts", "")
        if (not re.fullmatch(r"\d+\.\d+", root) or root != self.intent.get("notification_root")
                or self.intent.get("notification_thread_id") not in state.get("test_threads", [])
                or state.get("assisted_gui", {}).get("thread_id") != self.intent.get("notification_thread_id")
                or label in self.evidence["notifications"]):
            raise BridgeError("r14_notification_target_or_replay_invalid")
        if self.notice_web is None:
            _, bot = self.cfg.credentials()  # Injected only by the isolated bootstrap.
            self.notice_web = Web(self.cfg, bot)
            auth = await self.notice_web.call("auth.test")
            rows = (await self.notice_web.call("conversations.replies", channel=self.cfg.dm_id,
                ts=root, limit=1)).get("messages", [])
            msg = rows[0] if rows else {}
            app = msg.get("app_id") or msg.get("bot_profile", {}).get("app_id")
            if (auth.get("team_id") != self.cfg.team_id or not auth.get("bot_id")
                    or msg.get("ts") != root or msg.get("bot_id") != auth["bot_id"] or app != self.cfg.app_id):
                raise BridgeError("r14_notification_app_identity_unverified")
        entry = {"stage": "sending"}
        self.evidence["notifications"][label] = entry
        self.save()
        try:
            entry["message_ts"] = await self.notice_web.post(text, root=root)
        except BaseException:
            entry["stage"] = "unknown"
            self.save()
            raise
        entry["stage"] = "sent"
        self.save()

    def save(self, stage=None, **fields):
        if stage:
            self.evidence["stage"] = stage
            self.evidence.setdefault("stages", []).append({"stage": stage, "observed_ms": now_ms()})
        if "code" in fields:
            self.evidence.setdefault("errors", []).append({"stage": self.evidence["stage"],
                "code": fields["code"], "observed_ms": now_ms()})
        self.evidence.update(observed_ms=now_ms(), **fields)
        write_json(DATA / "R14-worker.json", self.evidence)
        mutate(lambda state: state["r14_lifecycle"].update(stage=self.evidence["stage"],
            observed_ms=self.evidence["observed_ms"]))

    def guard(self, *, restoring=False):
        # Reserve two minutes for GUI/bridge return after an assertion fails.
        if now_ms() >= self.intent["expires_ms"] - (0 if restoring else 120000):
            raise BridgeError("r14_window_expired")
        if not restoring and prepare().get("ui_control", {}).get("status") == "await_human_continue":
            raise BridgeError("r14_human_ui_stop")
        current = windows.read_record(self.cfg)
        if (current.get("backend") != self.original["backend"]
                or current.get("endpoint") != self.original["endpoint"]
                or windows.process_identity(current["backend"]["pid"]) != current["backend"]):
            raise BridgeError("r14_backend_identity_changed")

    async def once(self, key, method, params):
        if key in self.evidence["actions"]:
            raise BridgeError("r14_write_not_replayed")
        self.guard()
        entry = {"stage": "sending", "method": method, "observed_ms": now_ms()}
        self.evidence["actions"][key] = entry
        self.save()
        try:
            result = await self.backend.rpc.call(method, params, write=True)
        except BaseException:
            entry["stage"] = "unknown"
            self.save()
            raise
        entry["stage"] = "accepted"
        if "thread" in result:
            entry["thread_id"] = result["thread"]["id"]
        if "turn" in result:
            entry["turn_id"] = result["turn"]["id"]
        self.save()
        return result

    async def loaded(self):
        self.guard()
        rows, cursor = [], None
        for _ in range(100):
            page = await self.backend.rpc.call("thread/loaded/list", {"limit": 100, "cursor": cursor})
            for tid in page["data"]:
                thread = (await self.backend.rpc.call("thread/read", {
                    "threadId": tid, "includeTurns": False}))["thread"]
                rows.append({"thread_id": tid, "status": thread.get("status", {})})
            cursor = page.get("nextCursor")
            if not cursor:
                return rows
        raise BridgeError("r14_idle_audit_coverage_unproven")

    async def wait_terminal(self, tid, turn, seconds=150):
        deadline = min(self.intent["expires_ms"] - 120000, now_ms() + seconds * 1000)
        while now_ms() < deadline:
            self.guard()
            try:
                status, _ = await self.backend.final_messages(tid, turn)
                if status in {"completed", "interrupted", "failed"}:
                    return status
            except BridgeError as error:
                if error.code != "terminal_not_confirmed":
                    raise
            await asyncio.sleep(1)
        raise BridgeError("r14_fixture_terminal_unproven")

    async def initial_idle(self):
        for _ in range(60):
            rows = await self.loaded()
            busy = [r for r in rows if r["status"].get("type") != "idle" or r["status"].get("activeFlags")]
            if not busy:
                return rows
            if any(r["thread_id"] != "01a1786b-d49d-7243-b5e7-936ba8985fdc" for r in busy):
                raise BridgeError("r14_other_task_not_idle")
            # Let the initiating orchestrator finish its final handoff message.
            await asyncio.sleep(1)
        raise BridgeError("r14_orchestrator_not_idle")

    async def final_idle(self):
        allowed = {"01a1786b-d49d-7243-b5e7-936ba8985fdc"}
        allowed.update(value["thread_id"] for key, value in self.evidence["actions"].items()
            if key.endswith("_chat") and value.get("thread_id"))
        quiet = 0
        for _ in range(30):
            rows = await self.loaded()
            busy = [r for r in rows if r["status"].get("type") != "idle" or r["status"].get("activeFlags")]
            if any(row["thread_id"] not in allowed for row in busy):
                raise BridgeError("r14_final_idle_audit_failed")
            quiet = quiet + 1 if not busy else 0
            if quiet >= 2:
                return rows
            await asyncio.sleep(1)
        raise BridgeError("r14_final_idle_audit_failed")

    async def expect_refusal(self, label, code):
        self.guard()
        try:
            await windows.stop(self.cfg)
        except BridgeError as error:
            if error.code != code:
                raise BridgeError("r14_unexpected_stop_refusal") from None
            self.guard()
            self.evidence["assertions"][label] = {"refusal": error.code,
                "backend_unchanged": True, "observed_ms": now_ms()}
            self.save()
            return
        raise BridgeError("r14_stop_unexpectedly_succeeded")

    async def shutdown_bridge(self):
        state = prepare()
        bridge = state["bridge"]
        if windows.process_identity(bridge["pid"]) != bridge:
            raise BridgeError("r14_bridge_identity_changed")
        command = {"run_id": self.intent["run_id"], "id": new_id(), "action": "shutdown",
            "expires_ms": now_ms() + 60000}
        self.save("bridge_shutdown_requested", shutdown_command_id=command["id"])
        write_json(DATA / "command.json", command)
        for _ in range(40):
            if windows.process_identity(bridge["pid"]) is None:
                self.save("bridge_disconnected")
                return
            await asyncio.sleep(.5)
        raise BridgeError("r14_bridge_shutdown_unproven")

    async def fixtures(self):
        for label, instructions in (("wait", wait_instructions()), ("approval", approval_instructions())):
            value = await self.once(label + "_chat", "thread/start", {"cwd": str(ROOT),
                "sandbox": "read-only", "approvalPolicy": "never", "developerInstructions": instructions})
            tid = value["thread"]["id"]
            if not self.cfg.allows(value["thread"]["cwd"]):
                raise BridgeError("r14_fixture_outside_scope")
            def attach(state):
                if tid not in state["test_threads"]:
                    state["test_threads"].append(tid)
            mutate(attach)

    async def active_and_approval(self):
        tid = self.evidence["actions"]["wait_chat"]["thread_id"]
        py_exe = ROOT / ".venv" / "Scripts" / "python.exe"
        wait_fixture = ROOT / "scripts" / "p1_wait_fixture.py"
        result = await self.once("wait_turn", "turn/start", {"threadId": tid, "input": [{"type": "text",
            "text": f"P1 wait fixture: run exactly once: & \"{py_exe}\" "
                f"-I \"{wait_fixture}\". No other command, network, "
                "file writes, credentials or environment reads. Poll only this session until it exits naturally."}]})
        turn = result["turn"]["id"]
        self.save("await_human_gui_close", active_thread_id=tid, active_turn_id=turn)
        await self.notify("close_prompt", "R14 独立脚本已就绪：现在请在40秒内正常退出 Codex 一次。"
            "只退出 Codex，不关闭 Slack、不重启 Windows。脚本会自动完成停止保护测试并重新打开 Codex。"
            "如已超过40秒或看到作废提示，请保持窗口打开。")
        for _ in range(100):
            self.guard()
            if windows.process_identity(self.original["desktop"]["pid"]) is None:
                if any(p["name"].casefold() == self.cfg.desktop_exe.name.casefold() for p in windows.processes()):
                    await asyncio.sleep(.5)
                    continue
                break
            if self.backend.terminals.get((tid, turn)):
                raise BridgeError("r14_task_finished_before_gui_closed")
            await asyncio.sleep(.5)
        else:
            raise BridgeError("r14_human_close_window_missed")
        snapshot = await self.backend.snapshot(tid)
        if not snapshot.trusted or snapshot.idle or snapshot.active_turn != turn or snapshot.approval:
            raise BridgeError("r14_active_target_unproven")
        self.save("gui_closed_active_task_verified")
        await self.expect_refusal("active", "active_or_untrusted_thread_stop_refused")
        if await self.wait_terminal(tid, turn) != "completed":
            raise BridgeError("r14_wait_not_completed")
        tid = self.evidence["actions"]["approval_chat"]["thread_id"]
        result = await self.once("approval_turn", "turn/start", {"threadId": tid,
            "approvalPolicy": "on-request", "approvalsReviewer": "user",
            "sandboxPolicy": {"type": "readOnly", "networkAccess": False}, "input": [{"type": "text",
                "text": "P1 approval fixture: request exactly once with sandbox_permissions=require_escalated: "
                    f"Get-Content -LiteralPath {wait_fixture}. "
                    "Do not execute the file, read other files/environment/credentials, write files or access network. "
                    "No persistent permission; stop without retry if declined."}]})
        turn = result["turn"]["id"]
        for _ in range(90):
            self.guard()
            requests = [r for r in self.backend.requests.values()
                if r.get("method") == "item/commandExecution/requestApproval"
                and r.get("params", {}).get("threadId") == tid and r.get("params", {}).get("turnId") == turn]
            if requests:
                request = requests[0]
                self.save("original_approval_pending", request_id=request["id"], approval_turn_id=turn)
                await self.expect_refusal("approval", "active_or_untrusted_thread_stop_refused")
                self.save("original_decline_sending")
                await self.backend.rpc.ws.send_json({"id": request["id"], "result": {"decision": "decline"}})
                self.save("original_decline_sent", original_decline_count=1)
                await self.wait_terminal(tid, turn, 60)
                return
            if self.backend.terminals.get((tid, turn)):
                break
            await asyncio.sleep(1)
        raise BridgeError("r14_original_approval_unproven")

    def reopen_same_backend(self):
        self.guard(restoring=True)
        if self.gui_launch_attempted:
            raise BridgeError("r14_gui_launch_not_replayed")
        if any(p["name"].casefold() == self.cfg.desktop_exe.name.casefold() for p in windows.processes()):
            raise BridgeError("r14_existing_gui_requires_review")
        windows.verify_record(self.cfg, require_desktop=False)
        self.gui_launch_attempted = True
        self.save("gui_restore_attempted")
        env = child_environment(self.cfg)
        env["CODEX_APP_SERVER_WS_URL"] = self.original["endpoint"]
        record = windows.read_record(self.cfg)
        record["desktop"] = windows.detached([str(self.cfg.desktop_exe)], env)
        windows.save_record(self.cfg, record)
        for _ in range(45):
            try:
                record["peer"] = windows.correlate(record)
                windows.save_record(self.cfg, record)
                return windows.verify_record(self.cfg)
            except BridgeError:
                time.sleep(1)
        raise BridgeError("r14_gui_return_unproven")

    def schedule_bridge(self, record):
        def recover(state):
            state["controlled_session"] = {k: record[k] for k in ("backend", "desktop", "peer", "endpoint")}
            state["restart_intent"] = {"run_id": state["run_id"], "kind": "ensure_bridge",
                "expires_ms": min(state["deadline_ms"], now_ms() + 180000)}
            state["phase"] = "r14_bridge_return_pending"
            return state
        state = mutate(recover)
        trigger_task(state, self.cfg)

    async def verify_return(self):
        deadline = min(self.intent["expires_ms"], now_ms() + 90000)
        while now_ms() < deadline:
            state = prepare()
            path = DATA / "bridge-status.json"
            runtime = json.loads(path.read_text()) if path.exists() else {}
            process = runtime.get("process")
            if (runtime.get("run_id") == self.intent["run_id"] and process
                    and process == state.get("bridge") and not state.get("restart_intent")
                    and 0 <= now_ms() - runtime.get("observed_ms", 0) < 5000
                    and runtime.get("slack_ready") and runtime.get("backend_connected")
                    and runtime.get("healthy") and all(b["mode"] == "desktop" for b in runtime["bindings"])
                    and windows.process_identity(process["pid"]) == process):
                record = windows.verify_record(self.cfg)
                if any(record.get(key) != self.record.get(key) for key in ("backend", "desktop", "endpoint")):
                    raise BridgeError("r14_returned_session_changed")
                self.save("returned_and_verified", verified_bridge=process, default_desktop=True)
                return
            await asyncio.sleep(1)
        raise BridgeError("r14_bridge_return_unproven")

    async def run(self):
        passed = False
        await self.backend.connect()
        try:
            await self.expect_refusal("bridge_running", "another_instance_is_running")
            await self.shutdown_bridge()
            await self.expect_refusal("desktop_open", "desktop_still_open_stop_refused")
            self.save("waiting_initial_idle", initial_idle_audit=await self.initial_idle())
            await self.fixtures()
            await self.active_and_approval()
            rows = await self.final_idle()
            self.save("inactive_stop_attempted", final_idle_audit=rows)
            await windows.stop(self.cfg)
            if windows.process_identity(self.original["backend"]["pid"]) is not None:
                raise BridgeError("r14_backend_stop_unproven")
            if windows.read_record(self.cfg).get("status") != "stopped":
                raise BridgeError("r14_stopped_record_unproven")
            self.save("owned_inactive_backend_stopped")
            self.new_session_attempted = True
            self.save("new_controlled_session_attempted")
            self.record = windows.start(self.cfg)
            passed = True
        except BaseException as error:
            self.save("unproven", code=error.code if isinstance(error, BridgeError) else "r14_worker_failed")
            if "close_prompt" in self.evidence["notifications"]:
                try:
                    await self.notify("close_prompt_cancelled", "R14 本次关闭提示已作废：请保持 Codex 打开，"
                        "不要按旧提示退出。脚本正在尝试恢复原连接；此项仍未通过。")
                except BaseException:
                    pass
        finally:
            await self.backend.close()
            # Never kill tasks, delete ownership records or retry an uncertain launch.
            try:
                if not passed:
                    if windows.process_identity(self.original["desktop"]["pid"]) == self.original["desktop"]:
                        self.record = windows.verify_record(self.cfg)
                    elif not self.new_session_attempted:
                        self.record = self.reopen_same_backend()
                    else:
                        raise BridgeError("r14_uncertain_launch_requires_review")
                self.save("automatic_bridge_return", lifecycle_assertions_passed=passed,
                    returned_session={k: self.record[k] for k in ("backend", "desktop", "peer", "endpoint")})
                self.schedule_bridge(self.record)
                await self.verify_return()
                if passed:
                    await self.notify("return_complete", "R14 独立流程已完成，Codex 和桥接均已自动恢复。"
                        "请保持窗口打开，不要再次退出；后续由原验收聊天生成报告。")
                mutate(lambda state: state.update(phase="r14_returned_wait_report" if passed else "r14_returned_unproven"))
            except BaseException as error:
                self.save("return_unproven", code=error.code if isinstance(error, BridgeError) else "r14_return_failed")
            finally:
                if self.notice_web and self.notice_web.client.session:
                    await self.notice_web.client.session.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["arm", "worker"])
    parser.add_argument("--intent-id")
    args = parser.parse_args(argv)
    cfg = load(ROOT / "bridge.local.json")
    if args.mode == "arm":
        record = windows.verify_record(cfg)
        def arm(state):
            validate_arm(state, record)
            gui = state.get("assisted_gui", {})
            if (gui.get("thread_id") not in state.get("test_threads", [])
                    or not re.fullmatch(r"\d+\.\d+", gui.get("root_ts", ""))):
                raise BridgeError("r14_notification_target_unverified")
            intent = {"kind": "r14_lifecycle", "id": new_id(), "run_id": state["run_id"],
                "original_record": record, "not_before_ms": now_ms() + 25000,
                "notification_root": gui["root_ts"], "notification_thread_id": gui["thread_id"],
                "expires_ms": min(state["deadline_ms"], now_ms() + 600000)}
            state["restart_intent"] = intent
            state["r14_lifecycle"] = {"id": intent["id"], "stage": "armed"}
            return state
        state = mutate(arm)
        trigger_task(state, cfg)
        print(json.dumps({"status": "armed", "intent_id": state["restart_intent"]["id"]}))
        return
    with windows.single_instance("p1-r14-lifecycle"):
        state = prepare()
        intent = state.get("restart_intent") or {}
        # The supervisor persists the actual launcher immediately after CreateProcess.
        for _ in range(50):
            if intent.get("worker_launcher"):
                break
            time.sleep(.1)
            state = prepare()
            intent = state.get("restart_intent") or {}
        if (intent.get("id") != args.intent_id or intent.get("kind") != "r14_lifecycle"
                or intent.get("run_id") != state["run_id"] or intent.get("expires_ms", 0) <= now_ms()
                or not state.get("task_canary_passed")):
            raise BridgeError("r14_worker_intent_unverified")
        # Only a scheduler-launched descendant of the persisted worker launcher may act.
        launcher = intent.get("worker_launcher")
        parents = {p["pid"]: p["parent"] for p in windows.processes()}
        ancestor = os.getpid()
        for _ in range(16):
            if launcher and ancestor == launcher["pid"] and windows.process_identity(ancestor) == launcher:
                break
            ancestor = parents.get(ancestor)
        else:
            raise BridgeError("r14_worker_independence_unverified")
        asyncio.run(Worker(cfg, intent).run())


if __name__ == "__main__":
    try:
        main()
    except BridgeError as error:
        print(json.dumps({"status": "unproven", "code": error.code}))
        raise SystemExit(1) from None
