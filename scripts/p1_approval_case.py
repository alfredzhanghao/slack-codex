"""One original-client approval fixture. P1 bridge never sends a decision."""

import asyncio
import json

from slack_codex_bridge import windows
from slack_codex_bridge.config import load
from slack_codex_bridge.desktop import Desktop
from slack_codex_bridge.model import BridgeError, now_ms

from p1_acceptance_supervisor import DATA, ROOT, prepare, write_json
from p1_live_chat import update


async def run():
    cfg = load(ROOT / "bridge.local.json")
    record = windows.verify_record(cfg)
    state = prepare()
    tid = state["chat_preparation"]["approval"]["thread_id"]
    if tid not in state["test_threads"]:
        raise BridgeError("acceptance_approval_target_unverified")
    backend = Desktop(cfg, record["endpoint"])
    evidence = {"run_id": state["run_id"], "thread_id": tid, "stage": "preparing"}

    def save():
        evidence["observed_ms"] = now_ms()
        write_json(DATA / "R12-original-client.json", evidence)

    await backend.connect()
    try:
        snapshot = await backend.snapshot(tid)
        if not snapshot.trusted or not snapshot.idle or snapshot.approval:
            raise BridgeError("acceptance_approval_target_not_idle")

        def begin(current):
            if "approval_task" in current:
                raise BridgeError("acceptance_approval_not_replayed")
            current["approval_task"] = {"stage": "sending", "observed_ms": now_ms()}

        update(begin)
        evidence["stage"] = "sending"
        save()
        wait_fixture = ROOT / "scripts" / "p1_wait_fixture.py"
        result = await backend.rpc.call("turn/start", {"threadId": tid,
            "approvalPolicy": "on-request", "approvalsReviewer": "user",
            "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
            "input": [{"type": "text", "text": "P1 单次原客户端审批测试：请仅通过 exec_command "
                "使用 sandbox_permissions=require_escalated 申请一次只读命令 Get-Content -LiteralPath "
                f"{wait_fixture}。不要执行该文件，不修改文件，"
                "不读取其他文件、凭证或环境变量，不联网，不申请持久权限；被拒绝就停止，不能重试。"}]}, write=True)
        turn = result["turn"]["id"]
        evidence.update(stage="accepted", turn_id=turn)
        update(lambda current: current["approval_task"].update(stage="accepted", turn_id=turn))
        save()
        deadline = min(state["deadline_ms"], now_ms() + 180000)
        while now_ms() < deadline:
            requests = [request for request in backend.requests.values()
                if request.get("method") == "item/commandExecution/requestApproval"
                and request.get("params", {}).get("threadId") == tid
                and request.get("params", {}).get("turnId") == turn]
            if requests:
                request = requests[0]
                evidence.update(stage="approval_observed", request_id=request["id"],
                    method=request["method"], original_connection_generation=backend.rpc.generation)
                save()
                decision_path = DATA / "R12-decision.json"
                decision = json.loads(decision_path.read_text()) if decision_path.exists() else {}
                if (decision.get("run_id") == state["run_id"] and decision.get("turn_id") == turn
                        and decision.get("request_id") == request["id"]
                        and decision.get("decision") == "decline"
                        and now_ms() < decision.get("expires_ms", 0)):
                    evidence["stage"] = "decline_sending"
                    save()  # Persist before a single response on the original connection.
                    await backend.rpc.ws.send_json({"id": request["id"], "result": {"decision": "decline"}})
                    evidence.update(stage="decline_sent", decision_count=1)
                    save()
                    break
            elif backend.terminals.get((tid, turn)):
                evidence.update(stage="no_server_approval_observed", terminal=backend.terminals[tid, turn])
                save()
                return
            await asyncio.sleep(0.5)
        else:
            evidence["stage"] = "decision_window_expired_unproven"
            save()
            return
        for _ in range(60):
            terminal = backend.terminals.get((tid, turn))
            if terminal:
                evidence.update(stage="terminal_after_decline", terminal=terminal)
                save()
                return
            await asyncio.sleep(0.5)
    except BridgeError as error:
        evidence.update(stage="unproven", code=error.code, unknown=error.unknown)
        save()
        raise
    finally:
        await backend.close()


if __name__ == "__main__":
    asyncio.run(run())
