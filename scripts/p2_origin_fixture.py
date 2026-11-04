"""One real, bounded fixture turn from an independent original backend client."""

import argparse
import asyncio
import json

from p2_acceptance import DATA, cfg, save, state, update
from slack_codex_bridge import windows
from slack_codex_bridge.desktop import Desktop
from slack_codex_bridge.model import BridgeError, now_ms


async def main(slot, trial=None):
    run = state()
    if slot not in {"question", "question-retry", "command-race", "command-accept", "command-decline", "command-ui", "patch"} or slot not in run["slots"]:
        raise BridgeError("origin_fixture_scope_invalid")
    if trial is not None and (slot, trial) not in {
            ("question-retry", "transport"), ("question", "ui"), ("question", "ui-independent")}:
        raise BridgeError("origin_trial_scope_invalid")
    fixture_key = slot if trial is None else slot + ":" + trial
    evidence_slot = slot if trial is None else slot + "-" + trial
    if fixture_key in run.get("origin_fixtures", {}):
        raise BridgeError("origin_turn_not_replayed")
    config = cfg()
    backend = Desktop(config, windows.verify_record(config)["endpoint"])
    await backend.connect()
    async def drain_events():
        while True:
            await backend.events.get()  # _notify already retains exact requests/terminal evidence.
    events = asyncio.create_task(drain_events())
    tid = run["slots"][slot]["thread_id"]
    try:
        snapshot = await backend.snapshot(tid)
        if not snapshot.trusted or not snapshot.idle or snapshot.approval:
            raise BridgeError("origin_fixture_not_idle")
        update(lambda v: v.setdefault("origin_fixtures", {}).update({fixture_key: {"stage": "sending", "observed_ms": now_ms()}}))
        params = {"threadId": tid, "approvalPolicy": "on-request", "approvalsReviewer": "user",
                  "sandboxPolicy": {"type": "readOnly", "networkAccess": False}}
        if slot in {"question", "question-retry"}:
            params["collaborationMode"] = {"mode": "plan", "settings": {"model": snapshot.model,
                                                        "reasoning_effort": "low", "developer_instructions": None}}
            task = ("P2 Q09 real structured question fixture. Call request_user_input exactly once to ask which "
                    "style I prefer, with Simple and Detailed as options. No secrets and no other tools. "
                    "Wait for the user's answer, then summarize that exact answer in one sentence. "
                    "If request_user_input is unavailable, report unsupported; do not invent a call.")
            if trial == "transport":
                task = ("P2 Q09 independent transport fixture. Call request_user_input exactly once to ask "
                        "'Which password-policy description style should be used?', with Simple and Detailed as "
                        "options. This is a terminology test; do not request any actual password or credential. "
                        "Wait for the answer and summarize only that exact choice. No other tools.")
            elif trial == "ui-independent":
                task = ("New independent P2 UI acceptance task, unrelated to the earlier canceled style question. "
                        "That old request remains closed; do not replay or answer it. For this NEW task only, "
                        "call request_user_input exactly once with question id p2_ui_result_label: "
                        "'Which label should this UI-only result use?', options Compact and Expanded. "
                        "Wait for the actual answer, then reply with that exact choice. No other tools. "
                        "The no-retry rule applies within this new task, not to creation of this different question.")
        elif slot == "patch":
            task = ("P2 Q08 real file-change approval fixture. Use apply_patch exactly once to change "
                    "D:/codeX/slack-codex/.work-p2/acceptance/fixtures/approval.txt from BEFORE to AFTER. "
                    "Do not run any shell or other tool. Wait for the actual approval. If declined, stop "
                    "without retry and report declined. Do not change any other file or access network.")
        else:
            task = ("P2 Q10 original-client race fixture. Execute exactly cmd /c echo P2_APPROVAL_OK once, "
                    "using exec_command with sandbox_permissions=require_escalated. No additional commands, "
                    "arguments or persistent prefix rules. Wait for the actual approval. If declined, stop "
                    "without retry and report declined. No file reads, environment reads, network or delegation.")
        params["input"] = [{"type": "text", "text": task}]
        value = await backend.rpc.call("turn/start", params, write=True)
        turn = value["turn"]["id"]
        update(lambda v: v["origin_fixtures"][fixture_key].update(stage="accepted", turn_id=turn))
        print(json.dumps({"stage": "accepted", "slot": slot, "thread_id": tid, "turn_id": turn}), flush=True)
        answered = False
        for _ in range(300):
            requests = [r for r in backend.pending_requests.values() if r.thread_id == tid and r.turn_id == turn]
            for request in requests:
                save("origin-request-" + evidence_slot + ".json", {"request_id": request.message["id"],
                    "method": request.message["method"], "params": request.params, "generation": request.generation})
                if slot == "command-race" and not answered:
                    command = DATA / "origin-response.json"
                    if command.exists():
                        response = json.loads(command.read_text())
                        allowed = request.params.get("availableDecisions") or ["accept", "decline", "cancel"]
                        if (response.get("run_id") == run["run_id"] and response.get("turn_id") == turn
                                and response.get("decision") in {"decline", "cancel"}
                                and response["decision"] in allowed and response.get("expires_ms", 0) > now_ms()):
                            backend.check_request(request)
                            request.status = "submitting"
                            answered = True
                            update(lambda v: v["origin_fixtures"][fixture_key].update(original_response="sending_once"))
                            await request.connection.send_json({"id": request.message["id"], "result": {"decision": response["decision"]}})
                            update(lambda v: v["origin_fixtures"][fixture_key].update(original_response="sent_unconfirmed"))
            rows = await backend.rpc.call("thread/turns/list", {"threadId": tid, "limit": 5,
                                                               "sortDirection": "desc", "itemsView": "full"})
            row = next((r for r in rows.get("data", []) if r.get("id") == turn), None)
            if (row and row.get("status") in {"completed", "failed", "interrupted"}
                    and backend.terminals.get((tid, turn)) == row["status"]):
                save("origin-result-" + evidence_slot + ".json", row)
                update(lambda v: v["origin_fixtures"][fixture_key].update(terminal=row["status"]))
                print(json.dumps({"slot": slot, "terminal": row["status"]}), flush=True)
                return
            await asyncio.sleep(1)
        print(json.dumps({"slot": slot, "stage": "pending_no_retry"}), flush=True)
    finally:
        events.cancel()
        await asyncio.gather(events, return_exceptions=True)
        await backend.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("slot", choices=["question", "question-retry", "command-race", "command-accept", "command-decline", "command-ui", "patch"])
    parser.add_argument("--trial", choices=["transport", "ui", "ui-independent"])
    try:
        args = parser.parse_args()
        asyncio.run(main(args.slot, args.trial))
    except BridgeError as error:
        print(json.dumps({"status": "unproven", "code": error.code, "unknown": error.unknown}))
        raise SystemExit(1)
