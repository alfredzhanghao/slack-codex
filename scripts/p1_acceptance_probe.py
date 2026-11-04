"""Explicit acceptance-only hooks. No production diagnostic listener or bypass.

Commands affect actual transports/lifecycle, never backend facts or user admission.
Every command/rule is bound to the current run and disposable thread allowlist.
"""

import asyncio
import contextvars
import json
from pathlib import Path

from slack_codex_bridge import windows
from slack_codex_bridge.model import BridgeError, now_ms

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".work-p1" / "acceptance"
CURRENT_OP = contextvars.ContextVar("acceptance_operation", default=None)


def save(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value), encoding="utf-8")
    tmp.replace(path)


class Probe:
    def __init__(self, run_id):
        state = json.loads((DATA / "state.json").read_text(encoding="utf-8"))
        if state["run_id"] != run_id or not state.get("task_probe_passed") or state["deadline_ms"] <= now_ms():
            raise BridgeError("acceptance_probe_not_authorized")
        self.run_id = run_id
        self.threads = set(state.get("test_threads", []))
        self.task = None
        self.controller = self.backend = self.slack = self.root_task = None
        self.stale_controls = {}
        self.held_root = None
        self.hold_until_ms = 0
        self.slack_loss_ms = 0
        claimed = DATA / "claims.json"
        old = json.loads(claimed.read_text(encoding="utf-8")) if claimed.exists() else {}
        self.seen = set(old.get("ids", [])) if old.get("run_id") == run_id else set()

    def claim(self, decision_id):
        if not isinstance(decision_id, str) or decision_id in self.seen:
            return False
        self.seen.add(decision_id)
        save(DATA / "claims.json", {"run_id": self.run_id, "ids": sorted(self.seen)})
        return True

    def event(self, event_name, **fields):
        row = {"observed_ms": now_ms(), "run_id": self.run_id, "event": event_name, **fields}
        with (DATA / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")

    def rule(self):
        path = DATA / "fault-rule.json"
        if not path.exists():
            return None
        rule = json.loads(path.read_text(encoding="utf-8"))
        if rule.get("run_id") != self.run_id or not now_ms() < rule.get("expires_ms", 0) <= now_ms() + 180000:
            return None
        if rule.get("id") in self.seen or rule.get("thread_id") not in self.threads:
            return None
        if rule.get("action") not in {"pause", "commit_unavailable_once"}:
            return None
        if rule.get("action") == "commit_unavailable_once" and rule.get("stage") != "receipt":
            return None
        return rule

    async def checkpoint(self, stage, op, receipt=None):
        if op.target_thread_id not in self.threads:
            return
        minimal = {"op_id": op.op_id, "thread_id": op.target_thread_id, "turn_id": op.target_turn_id,
                   "kind": op.kind}
        CURRENT_OP.set(minimal)
        self.event(stage, **minimal, **({"receipt_turn_id": receipt.turn_id} if receipt else {}))
        rule = self.rule()
        if rule and rule.get("stage") == stage and rule.get("kind") == op.kind:
            await self.apply(rule, stage, minimal)

    async def after_write(self, method, rpc_id):
        op = CURRENT_OP.get()
        if not op or op["thread_id"] not in self.threads:
            return
        self.event("transport_written", method=method, rpc_id=rpc_id, **op)
        rule = self.rule()
        if rule and rule.get("stage") == "transport_written" and rule.get("kind") == op["kind"]:
            await self.apply(rule, "transport_written", op)

    async def apply(self, rule, stage, op):
        if rule["thread_id"] != op["thread_id"]:
            return
        if not self.claim(rule["id"]):
            return
        self.event("fault_claimed", fault_id=rule["id"], stage=stage, **op)
        save(DATA / "fault-checkpoint.json", {"run_id": self.run_id, "fault_id": rule["id"],
             "stage": stage, "op_id": op["op_id"], "observed_ms": now_ms()})
        if rule["action"] == "commit_unavailable_once":
            if stage != "receipt":
                raise BridgeError("acceptance_invalid_commit_fault")
            raise BridgeError("database_unavailable")
        # The independent driver may kill the exact bridge PID while this is paused.
        # Expiry is a conservative cancellation, never an implicit resend or continuation.
        while now_ms() < rule["expires_ms"]:
            release = DATA / "fault-release.json"
            if release.exists():
                value = json.loads(release.read_text(encoding="utf-8"))
                if value.get("run_id") == self.run_id and value.get("fault_id") == rule["id"]:
                    return
            await asyncio.sleep(0.1)
        raise asyncio.CancelledError

    def start(self, controller, backend, slack, root_task):
        self.controller, self.backend, self.slack, self.root_task = controller, backend, slack, root_task
        backend.rpc.after_write = self.after_write
        self.original_interaction = slack.prepare_interaction
        self.original_update = slack.web.update
        slack.prepare_interaction = self.observe_interaction
        slack.web.update = self.delayed_card_update
        self.task = asyncio.create_task(self.poll())

    async def delayed_card_update(self, root, text, blocks):
        # Keep a genuinely issued pre-disconnect card visible for one live callback.
        # This only delays notification delivery; it never changes controller facts
        # or returns a fabricated Slack update receipt. All real sends stay single-shot.
        while root == self.held_root and now_ms() < self.hold_until_ms:
            await asyncio.sleep(.1)
        return await self.original_update(root, text, blocks)

    async def close_slack_for_stale_card(self, thread_id):
        if thread_id not in self.threads:
            raise BridgeError("acceptance_command_target_invalid")
        state = next(s for s in self.controller.states.values()
            if s.binding.codex_thread_id == thread_id)
        self.stale_controls = {key: {"kind": value.kind, "epoch": value.epoch,
            "binding_id": value.binding_id, "thread_id": thread_id, "root": value.root}
            for key, value in self.slack.buttons.tokens.items()
            if value.binding_id == state.binding.binding_id and value.kind == "status"
            and value.root == state.binding.slack_root_ts}
        if not self.stale_controls:
            raise BridgeError("acceptance_old_card_control_unavailable")
        self.held_root = state.binding.slack_root_ts
        self.slack_loss_ms = now_ms()
        self.hold_until_ms = now_ms() + 30000
        self.slack.lost()
        await self.slack.socket.disconnect()
        self.event("stale_card_window_started", thread_id=thread_id, root=self.held_root,
            hold_until_ms=self.hold_until_ms)

    async def observe_interaction(self, payload):
        actions = payload.get("actions", [])
        old = self.stale_controls.get(actions[0].get("value")) if len(actions) == 1 else None
        cfg = self.slack.cfg
        scoped = (old and payload.get("type") == "block_actions"
            and payload.get("api_app_id") == cfg.app_id and payload.get("team", {}).get("id") == cfg.team_id
            and payload.get("user", {}).get("id") == cfg.user_id
            and payload.get("channel", {}).get("id") == cfg.dm_id
            and payload.get("container", {}).get("message_ts") == old["root"]
            and actions[0].get("action_id") == "bridge_status")
        result = await self.original_interaction(payload)
        if scoped:
            state = self.controller.states[old["binding_id"]]
            self.event("old_button_callback", thread_id=old["thread_id"], root=old["root"],
                kind=old["kind"], issued_before_actual_loss=True, loss_ms=self.slack_loss_ms,
                old_epoch=old["epoch"], current_epoch=state.epoch, input_mode_after=state.mode,
                rejected_without_operation=all(part is None for part in result))
            self.hold_until_ms = 0
            self.stale_controls.clear()
        return result

    async def poll(self):
        while True:
            try:
                self.status()
                command_path = DATA / "command.json"
                if command_path.exists():
                    command = json.loads(command_path.read_text(encoding="utf-8"))
                    if (command.get("run_id") == self.run_id and command.get("id") not in self.seen
                            and now_ms() < command.get("expires_ms", 0) <= now_ms() + 180000):
                        if not self.claim(command["id"]):
                            raise BridgeError("acceptance_command_id_invalid")
                        action = command.get("action")
                        if action == "close_slack":
                            self.slack.lost()
                            await self.slack.socket.disconnect()
                        elif action == "close_slack_stale_card":
                            await self.close_slack_for_stale_card(command.get("thread_id"))
                        elif action == "close_backend":
                            await self.backend.close()
                        elif action == "refresh_test_binding":
                            thread_id = command.get("thread_id")
                            if thread_id not in self.threads:
                                raise BridgeError("acceptance_command_target_invalid")
                            state = next(s for s in self.controller.states.values()
                                if s.binding.codex_thread_id == thread_id)
                            # Real read-only reconciliation; never replace admission facts.
                            await self.controller.refresh(state)
                        elif action == "shutdown":
                            self.event("graceful_shutdown", command_id=command["id"])
                            self.root_task.cancel()
                            return
                        else:
                            raise BridgeError("acceptance_command_invalid")
                        self.event("command_completed", command_id=command["id"], action=action)
            except BridgeError as error:
                self.event("probe_fault", code=error.code)
            except Exception:
                self.event("probe_fault", code="acceptance_probe_failed")
            await asyncio.sleep(0.5)

    def status(self):
        rows = []
        for state in self.controller.states.values():
            if state.binding.codex_thread_id not in self.threads:
                continue
            snapshot = state.snapshot
            rows.append({"binding_id": state.binding.binding_id, "thread_id": state.binding.codex_thread_id,
                         "root_ts": state.binding.slack_root_ts, "mode": state.mode, "epoch": state.epoch,
                         "active_turn": snapshot.active_turn if snapshot else None,
                         "protected_turn": state.active_turn, "blocked": state.blocked,
                         "blocker_ids": state.blocker_ids, "trusted": bool(snapshot and snapshot.trusted),
                         "pending_approval": bool(snapshot and snapshot.approval)})
        save(DATA / "bridge-status.json", {"observed_ms": now_ms(), "run_id": self.run_id,
             "process": windows.process_identity(__import__("os").getpid()),
             "slack_ready": self.slack.ready, "backend_generation": self.backend.rpc.generation,
             "backend_connected": self.backend.connected, "healthy": self.controller.healthy,
             "slack_fault": self.slack.last_fault, "bindings": rows})

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
