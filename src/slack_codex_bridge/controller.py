"""Durable admission and exactly-one-attempt submission, independent of Slack/wire formats."""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Protocol
from pathlib import Path

from .config import Config, safe_text
from .model import Action, Binding, BridgeError, Operation, Receipt, Snapshot, new_id, now_ms
from .store import Store
from .inputs import parse_files, file_inputs, local_file
from .attachments import attachment_inputs
from .recovery import fingerprint, replacement


class Backend(Protocol):
    async def snapshot(self, thread_id: str) -> Snapshot: ...
    async def send(self, action: Action) -> Receipt: ...


@dataclass
class State:
    binding: Binding
    root_fault: str | None = None
    mode: str = "suspended"
    epoch: str = field(default_factory=new_id)
    opened_ms: int = 2**63 - 1
    snapshot: Snapshot | None = None
    known_title: str = ""
    active_turn: str | None = None
    blocked: bool = False
    blocker_ids: tuple[str, ...] = ()
    syncing: bool = True
    compaction_pending: bool = False
    effort_options: tuple[str, ...] = ()
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reservations: dict[str, tuple[str, int, str]] = field(default_factory=dict)
    input_pending: bool = False
    transfer_refusal: str | None = None


@dataclass(frozen=True)
class Prepared:
    operation: Operation
    execute: bool = False


class Controller:
    def __init__(self, cfg: Config, backend: Backend, store: Store, checkpoint=None):
        self.cfg, self.backend, self.store = cfg, backend, store
        self.states: dict[str, State] = {}
        self.healthy = True
        self.available = False
        self.checkpoint = checkpoint
        self.admission = asyncio.Lock()

    async def load(self):
        await self.store.recover()
        for binding in await self.store.bindings():
            if (binding.slack_team_id, binding.slack_channel_id, binding.owner_user_id, binding.host_id) != (
                    self.cfg.team_id, self.cfg.dm_id, self.cfg.user_id, self.cfg.host_id):
                raise BridgeError("persisted_binding_identity_changed")
            if not self.cfg.allows(binding.project_path):
                raise BridgeError("persisted_binding_outside_allowlist")
            self.states[binding.binding_id] = State(binding)
            self.states[binding.binding_id].input_pending = await self.store.pending_input(binding.binding_id)
            blockers = await self.store.blockers(binding.codex_thread_id)
            self.states[binding.binding_id].blocked = bool(blockers)
            self.states[binding.binding_id].blocker_ids = tuple(op.op_id for op in blockers)
            # Restore the protection, never the input authority or a send queue.
            for op in await self.store.accepted(binding.codex_thread_id):
                ref, result = json.loads(op.backend_ref_json or "{}"), json.loads(op.result_json or "{}")
                if op.kind == "turn_start" and not result.get("terminal") and ref.get("turn_id"):
                    self.states[binding.binding_id].active_turn = ref["turn_id"]
                    break

    async def bind(self, binding: Binding) -> State:
        stored = await self.store.bind(binding)
        state = self.states.setdefault(stored.binding_id, State(stored))
        await self.refresh(state)
        return state

    def by_root(self, root_ts: str) -> State | None:
        return next((s for s in self.states.values() if s.binding.slack_root_ts == root_ts), None)

    def invalidate(self, reason: str = "connection_lost"):
        self.available = False
        for state in self.states.values():
            self._suspend(state)

    @staticmethod
    def _suspend(state):
        state.mode = "suspended"
        state.epoch = new_id()
        state.opened_ms = 2**63 - 1
        state.syncing = True

    async def refresh(self, state: State):
        snapshot = await self.backend.snapshot(state.binding.codex_thread_id)
        if snapshot.thread.id != state.binding.codex_thread_id or not self.cfg.allows(snapshot.thread.cwd):
            self._suspend(state)
            raise BridgeError("snapshot_target_mismatch")
        if (Path(snapshot.thread.cwd).resolve() != Path(state.binding.project_path).resolve()
                or snapshot.thread.environment != state.binding.environment_key):
            self._suspend(state)
            raise BridgeError("root_replacement_target_changed" if state.root_fault else "snapshot_target_mismatch")
        blockers = await self.store.blockers(snapshot.thread.id)
        compactions = [op for op in await self.store.accepted(snapshot.thread.id)
                       if op.kind == "compact" and not json.loads(op.result_json or "{}").get("terminal")]
        async with state.lock:
            previous = state.snapshot
            if previous and previous.generation != snapshot.generation:
                self._suspend(state)
            state.snapshot = snapshot
            state.known_title = snapshot.thread.title
            if previous and previous.model != snapshot.model:
                state.effort_options = ()
            state.blocked = bool(blockers)
            state.blocker_ids = tuple(op.op_id for op in blockers)
            state.compaction_pending = bool(compactions)
            if snapshot.active_turn:
                state.active_turn = snapshot.active_turn
            if state.active_turn and state.active_turn in snapshot.confirmed:
                state.active_turn = None
            if state.syncing and snapshot.trusted and self.available and not state.root_fault:
                state.mode = "desktop"
                state.epoch = new_id()
                state.syncing = False

    async def synchronized(self):
        self.available = True
        for state in self.states.values():
            try:
                await self.refresh(state)
            except BridgeError as error:
                self._suspend(state)
                if error.code == "database_unavailable":
                    self.healthy = False
                    self.invalidate("database_write_failed")
                    raise

    def _fresh(self, state):
        s = state.snapshot
        return bool(self.available and s and s.trusted and not state.root_fault
                    and not state.binding.disabled and now_ms() - s.observed_ms <= 5000)

    async def transfer(self, state: State, mode: str, epoch: str) -> bool:
        await self.refresh(state)
        async with state.lock:
            s = state.snapshot
            state.transfer_refusal = (
                "state_not_trusted" if not self.healthy or not self._fresh(state) else
                "invalid_input_mode" if mode not in {"slack", "desktop"} else
                "old_control_epoch" if state.epoch != epoch else
                "approval_pending" if s.approval else
                "compaction_pending" if s.compacting or state.compaction_pending else
                "unknown_outcome_requires_review" if state.blocked else
                "session_has_active_task" if not s.idle or state.active_turn else
                "submission_in_progress" if state.reservations else None)
            if state.transfer_refusal:
                return False
            state.mode = mode
            state.epoch = new_id()
            state.opened_ms = now_ms() if mode == "slack" else 2**63 - 1
            return True

    def _reason(self, state: State, kind: str, target: str | None, epoch: str,
                *, own_op: str | None = None) -> str | None:
        s = state.snapshot
        if not self.healthy:
            return "database_unavailable"
        if kind == "root_replace":
            if (not self.available or not s or not s.trusted or now_ms() - s.observed_ms > 5000
                    or state.binding.disabled or state.binding.environment_key != "windows"):
                return "root_replacement_state_untrusted"
            if (Path(s.thread.cwd).resolve() != Path(state.binding.project_path).resolve()
                    or s.thread.environment != state.binding.environment_key):
                return "root_replacement_target_changed"
            if epoch != state.epoch or not state.root_fault:
                return "root_replacement_preview_expired"
            if any(key != own_op for key in state.reservations):
                return "root_replacement_submission_in_progress"
            return None
        if state.root_fault:
            return "binding_root_unavailable"
        if not self._fresh(state):
            return "state_not_trusted"
        if epoch != state.epoch:
            return "old_control_epoch"
        if kind not in {"turn_start", "steer", "interrupt", "compact", "fork", "effort", "model", "approval",
                        "artifact_upload", "unknown_review"}:
            return "unsupported_operation"
        pending = {key: value for key, value in state.reservations.items() if key != own_op}
        if kind in {"artifact_upload", "unknown_review"}:
            if state.binding.disabled or state.binding.environment_key != "windows":
                return "artifact_binding_unavailable" if kind == "artifact_upload" else "review_binding_unavailable"
            if any(v[2] == kind for v in pending.values()):
                return "artifact_transfer_busy" if kind == "artifact_upload" else "review_in_progress"
            return None
        pending = {key: value for key, value in pending.items() if value[2] not in {"artifact_upload", "unknown_review"}}
        if kind == "interrupt":
            if any(value[2] == "interrupt" for value in pending.values()):
                return "control_submission_in_progress"
            return None if target and s.active_turn == target else "target_turn_changed"
        if pending:
            return "submission_in_progress"
        if state.blocked:
            return "unknown_or_semantic_mismatch"
        if kind == "approval":
            return None if target and s.active_turn == target else "target_turn_changed"
        if s.approval or s.compacting or state.compaction_pending:
            return "approval_or_compaction_pending"
        if kind == "steer":
            return None if target and s.active_turn == target else "target_turn_changed"
        if state.mode != "slack":
            return "desktop_input_mode"
        if kind in {"effort", "model"}:
            return None
        if not s.idle or state.active_turn:
            return "busy_use_explicit_steer"
        for other in self.states.values():
            if other is state or Path(other.binding.project_path).resolve() != Path(state.binding.project_path).resolve():
                continue
            if (other.active_turn or any(v[2] != "artifact_upload" for v in other.reservations.values())
                    or other.compaction_pending
                    # A missing Slack root does not make its native workspace idle.
                    or (other.snapshot and other.snapshot.trusted and now_ms() - other.snapshot.observed_ms <= 5000
                        and (other.snapshot.compacting or not other.snapshot.idle))):
                return "shared_directory_busy_or_untrusted"
        return None

    async def prepare(self, state: State, key: str, kind: str, text: str, epoch: str,
                      sent_ms: int, target: str | None = None, *, data: dict | None = None,
                      supersedes: Operation | None = None) -> Prepared:
        async with self.admission, state.lock:
            try:
                existing = await self.store.by_source(key)
            except BridgeError:
                self.healthy = False
                self.invalidate("database_receive_failed")
                raise
            if existing:
                if existing.binding_id != state.binding.binding_id:
                    raise BridgeError("operation_binding_mismatch")
                return Prepared(existing)
            reason = self._reason(state, kind, target, epoch)
            if not safe_text(text):
                reason = "invalid_input"
            if kind in {"turn_start", "steer"} and not text.strip():
                reason = "empty_task_input"
            if sent_ms > now_ms() + 30000 or now_ms() - sent_ms > self.cfg.message_age_ms:
                reason = "message_expired_or_future"
            if reason is None and kind in {"turn_start", "compact", "fork", "effort", "model"} and sent_ms < max(
                    state.opened_ms, state.binding.effective_from_ms):
                reason = "input_precedes_opening"
            if kind == "artifact_upload" and sent_ms < state.binding.effective_from_ms:
                reason = "input_precedes_binding"
            if data and not safe_text(json.dumps(data)):
                reason = "invalid_input"
            payload = {"text": text, "data": data or {}, "sent_ms": sent_ms}
            if supersedes:
                payload["supersedes_fingerprint"] = fingerprint(supersedes)
                try:
                    await self.store.check_unknown(await self.store.operation(supersedes.op_id), state.binding,
                                    fingerprint(supersedes), acknowledged=True)
                except BridgeError as error:
                    reason = error.code
            op = Operation(new_id(), key, state.binding.binding_id, state.binding.binding_version, kind,
                           state.binding.codex_thread_id, target,
                           json.dumps(payload) if not reason else "{}",
                           status="rejected" if reason else "received", error_reason=reason,
                           supersedes_op_id=supersedes.op_id if supersedes else None)
            try:
                stored, fresh = await self.store.record_once(op)
            except (BridgeError, asyncio.CancelledError):
                self.healthy = False
                self.invalidate("database_receive_failed")
                raise
            if fresh and not reason:
                state.reservations[stored.op_id] = (epoch, state.snapshot.generation, kind)
                if self.checkpoint:
                    await self.checkpoint("received", stored)
                return Prepared(stored, True)
            return Prepared(stored)

    async def abandon(self, state: State, prepared: Prepared):
        if not prepared.execute:
            return
        async with state.lock:
            try:
                await self.store.transition(prepared.operation.op_id, "received", "rejected",
                                            error_reason="ack_failed_before_send")
            finally:
                state.reservations.pop(prepared.operation.op_id, None)

    async def _uncertain(self, state: State, op_id: str, receipt: Receipt | None = None,
                         reason="write_outcome_unknown"):
        try:
            current = await self.store.operation(op_id)
            if current.status == "received":
                await self.store.transition(op_id, "received", "rejected", error_reason="cancelled_before_send")
            elif current.status == "sending":
                fields = {"error_reason": reason}
                if receipt:
                    fields["backend_ref_json"] = json.dumps({"turn_id": receipt.turn_id,
                                                              "thread_id": receipt.thread_id, "rpc_accepted": True})
                    if receipt.result:
                        fields["result_json"] = json.dumps(receipt.result)
                await self.store.transition(op_id, "sending", "unknown", **fields)
                state.blocked = True
                self._suspend(state)
        except BridgeError:
            self.healthy = False
            self.invalidate("database_write_failed")

    async def execute(self, state: State, prepared: Prepared) -> Operation:
        op = prepared.operation
        if not prepared.execute:
            return op
        receipt = None
        effort = None
        try:
            await self.refresh(state)
            if op.kind in {"turn_start", "compact", "fork"}:
                for other in list(self.states.values()):
                    if other is not state and Path(other.binding.project_path).resolve() == Path(state.binding.project_path).resolve():
                        try:
                            await self.refresh(other)
                        except BridgeError as error:
                            self._suspend(other)
                            if error.code == "database_unavailable":
                                raise
            payload = json.loads(op.payload_json)
            data = payload.get("data", {})
            if op.supersedes_op_id:
                await self.replacement_source(state, op.supersedes_op_id, payload["supersedes_fingerprint"])
            if op.kind == "turn_start":
                if data.get("attachments"):
                    await asyncio.to_thread(attachment_inputs, self.cfg, state.snapshot, data["attachments"], payload["text"])
                if data.get("skill_selector"):
                    rows = await self.backend.skills(op.target_thread_id)
                    matches = [s for s in rows if s.get("enabled") and data["skill_selector"] in
                               {s.get("name"), s.get("path")}]
                    if len(matches) != 1:
                        raise BridgeError("skill_ambiguous_or_unavailable")
                    data["skill"] = matches[0]["path"]
                if data.get("files"):
                    file_inputs(self.cfg, state.snapshot, data["files"], payload["text"])
                    data["files"] = [str(local_file(self.cfg, state.snapshot.thread.cwd, p)) for p in data["files"]]
                effort = json.loads(state.binding.next_effort_json or "{}")
                if effort.get("state") == "applying":
                    raise BridgeError("effort_outcome_requires_review")
                if effort.get("state") == "pending":
                    expected = effort.get("source_model", effort.get("model"))
                    if (expected != state.snapshot.model or effort.get("binding_version") != state.binding.binding_version
                            or effort.get("value") not in await self.backend.efforts(effort.get("model"))):
                        raise BridgeError("pending_effort_model_changed")
                    data["effort"] = effort["value"]
                    if effort.get("source_model"):
                        data["model"] = effort["model"]
            selected_model = state.snapshot.model
            source_model = None
            if op.kind == "model":
                if not isinstance(state.snapshot.model, str) or not state.snapshot.model:
                    raise BridgeError("source_model_unconfirmed")
                if data.get("source_model") and data["source_model"] != state.snapshot.model:
                    raise BridgeError("model_selector_changed")
                parts = payload["text"].split()
                rows = [m for m in await self.backend.models() if parts and m["model"] == parts[0]]
                if len(rows) != 1 or len(parts) > 2:
                    raise BridgeError("unsupported_model")
                selected_model = rows[0]["model"]
                source_model = state.snapshot.model
                data["value"] = parts[1] if len(parts) == 2 else rows[0].get("defaultReasoningEffort")
                if data["value"] not in await self.backend.efforts(selected_model):
                    raise BridgeError("unsupported_reasoning_effort")
            if op.kind == "effort":
                pending = json.loads(state.binding.next_effort_json or "{}")
                if pending.get("state") == "applying":
                    raise BridgeError("effort_outcome_requires_review")
                if pending.get("state") == "pending" and pending.get("source_model"):
                    if pending["source_model"] != state.snapshot.model:
                        raise BridgeError("pending_effort_model_changed")
                    selected_model, source_model = pending["model"], pending["source_model"]
                if data.get("selected_model") and data["selected_model"] != selected_model:
                    raise BridgeError("effort_selector_model_changed")
                if data.get("model") and data["model"] != state.snapshot.model:
                    raise BridgeError("effort_selector_model_changed")
                if payload["text"] not in await self.backend.efforts(selected_model):
                    raise BridgeError("unsupported_reasoning_effort")
            if op.kind == "unknown_review":
                if (not isinstance(data.get("reason"), str) or not data["reason"].strip()
                        or len(data["reason"]) > 1000):
                    raise BridgeError("review_reason_required")
                await self.store.check_unknown(await self.store.operation(data["review_op_id"]), state.binding, data["fingerprint"])
            async with state.lock:
                reservation = state.reservations.get(op.op_id)
                reason = "reservation_lost" if not reservation else self._reason(
                    state, op.kind, op.target_turn_id, reservation[0], own_op=op.op_id)
                if reservation and state.snapshot.generation != reservation[1]:
                    reason = "backend_generation_changed"
                if reason:
                    return await self.store.transition(op.op_id, "received", "rejected", error_reason=reason)
                known = set(state.snapshot.known_turns)
                if not safe_text(json.dumps(data)):
                    raise BridgeError("invalid_input")
                await self.store.transition(op.op_id, "received", "sending",
                                            payload_json=json.dumps({**payload, "data": data}))
                if self.checkpoint:
                    await self.checkpoint("sending", op)
                if effort and effort.get("state") == "pending":
                    state.binding = await self.store.effort(state.binding, {**effort, "state": "applying",
                                                                           "turn_op_id": op.op_id})
            if op.kind in {"effort", "model"}:
                setting = {"value": data.get("value", payload["text"]), "model": selected_model,
                           "binding_version": state.binding.binding_version, "sent_ms": payload["sent_ms"],
                           "state": "pending", "op_id": op.op_id}
                if source_model:
                    setting["source_model"] = source_model
                state.binding = await self.store.effort(state.binding, setting)
                state.effort_options = ()
                return await self.store.transition(op.op_id, "sending", "accepted",
                                                   result_json=json.dumps({"setting": "next_turn_pending"}))
            if op.kind == "unknown_review":
                async with state.lock:
                    reservation = state.reservations.get(op.op_id)
                    reason = "reservation_lost" if not reservation else self._reason(
                        state, op.kind, op.target_turn_id, reservation[0], own_op=op.op_id)
                    if reason:
                        return await self.store.transition(op.op_id, "sending", "rejected", error_reason=reason)
                    reviewed = await self.store.acknowledge_unknown(op, self.cfg.user_id)
                await self.refresh(state)
                return reviewed
            receipt = await self.backend.send(Action(op.kind, op.target_thread_id, op.target_turn_id,
                                                      payload.get("text", ""), data))
            if self.checkpoint:
                await self.checkpoint("receipt", op, receipt)
            mismatch = receipt.semantic_mismatch or (op.kind == "turn_start" and receipt.turn_id in known)
            op = await self.store.transition(op.op_id, "sending", "accepted",
                                            backend_ref_json=json.dumps({"turn_id": receipt.turn_id,
                                                                         "thread_id": receipt.thread_id,
                                                                         "rpc_accepted": True}),
                                            result_json=json.dumps(receipt.result) if receipt.result else None,
                                            error_reason="semantic_mismatch" if mismatch else None)
            async with state.lock:
                if op.kind == "turn_start":
                    state.active_turn = receipt.turn_id
                    if effort and effort.get("state") == "pending":
                        state.binding = await self.store.effort(state.binding, {**effort, "state": "applied"})
                if op.kind == "compact":
                    state.compaction_pending = True
                if mismatch:
                    state.blocked = True
                    self._suspend(state)
            return op
        except asyncio.CancelledError:
            await asyncio.shield(self._uncertain(state, op.op_id, receipt))
            raise
        except BridgeError as error:
            if error.code == "database_unavailable":
                self.healthy = False
                self.invalidate("database_write_failed")
            try:
                current = await self.store.operation(op.op_id)
                if error.unknown or receipt or error.code == "database_unavailable":
                    await self._uncertain(state, op.op_id, receipt, error.code)
                elif current.status in {"received", "sending"}:
                    await self.store.transition(op.op_id, current.status, "rejected", error_reason=error.code)
                    if effort and effort.get("state") == "pending":
                        state.binding = await self.store.effort(state.binding, effort)
                return await self.store.operation(op.op_id)
            except BridgeError:
                self.healthy = False
                self.invalidate("database_write_failed")
                raise
        except Exception:
            await self._uncertain(state, op.op_id, receipt)
            raise BridgeError("unexpected_submission_outcome", unknown=True) from None
        finally:
            async with state.lock:
                state.reservations.pop(prepared.operation.op_id, None)

    async def operation(self, state: State, op_id: str) -> Operation:
        op = await self.store.operation(op_id)
        if not op or op.binding_id != state.binding.binding_id:
            raise BridgeError("operation_not_found_in_binding")
        # Only an exact retained RPC receipt can resolve an unknown in P1. A matching
        # prompt or somebody else's interrupt/completed event is not causal proof.
        ref = json.loads(op.backend_ref_json or "{}")
        if (op.status == "unknown" and ref.get("rpc_accepted") is True
                and (ref.get("turn_id") or (op.kind == "fork" and ref.get("thread_id"))
                     or (op.kind == "compact" and json.loads(op.result_json or "{}").get("operation_token")))):
            op = await self.store.transition(op.op_id, "unknown", "accepted", error_reason=None)
            async with state.lock:
                if op.kind == "turn_start":
                    state.active_turn = ref["turn_id"]
            await self.refresh(state)
        return op

    async def replacement_source(self, state, op_id, expected):
        op = await self.store.check_unknown(await self.store.operation(op_id), state.binding, expected, acknowledged=True)
        replacement(op)
        return op

    async def prepare_replacement(self, state, key, op_id, expected, epoch, sent_ms):
        old = await self.replacement_source(state, op_id, expected)
        kind, text, data, target = replacement(old)
        return await self.prepare(state, key, kind, text, epoch, sent_ms, target, data=data, supersedes=old)

    async def prepare_command(self, state, key, text, epoch, sent_ms):
        """Parse P2 task metadata without network/file reads on the ACK path."""
        data, kind, content = {}, "turn_start", text
        try:
            if text in {"!compact", "!fork"}:
                kind, content = text[1:], ""
            elif text.startswith("!effort "):
                kind, content = "effort", text.split(maxsplit=1)[1].strip()
            elif text.startswith("!model "):
                kind, content = "model", text.split(maxsplit=1)[1].strip()
            elif text.startswith("!skill "):
                selector, sep, content = text[7:].partition(" -- ")
                if not sep or not selector.strip() or not content.strip():
                    raise BridgeError("use_skill_name_then_task")
                data = {"skill_selector": selector.strip().strip('"')}
            elif text.startswith("!file "):
                paths, content = parse_files(text[6:])
                data = {"files": paths}
            else:
                raise BridgeError("invalid_p2_command")
            return await self.prepare(state, key, kind, content.strip(), epoch, sent_ms, data=data)
        except BridgeError:
            # A malformed task command must not become a deferred local command.
            prepared = await self.prepare(state, key, "invalid", "", epoch, sent_ms)
            return prepared

    async def completed(self, state: State) -> list[Operation]:
        if not self._fresh(state):
            return []
        result = []
        operations = await self.store.accepted(state.binding.codex_thread_id)
        handled = {json.loads(op.backend_ref_json or "{}").get("turn_id") for op in operations
                   if json.loads(op.result_json or "{}").get("terminal")}
        for op in reversed(operations):
            if op.error_reason == "semantic_mismatch":
                continue
            tid = json.loads(op.backend_ref_json or "{}").get("turn_id")
            previous = json.loads(op.result_json or "{}")
            if op.kind == "compact":
                evidence = getattr(self.backend, "compactions", {}).get(op.target_thread_id, {})
                if (not previous.get("terminal") and evidence.get("status") in {"completed", "failed", "interrupted"}
                        and previous.get("operation_token") and evidence.get("operation_token") == previous["operation_token"]
                        and evidence.get("generation") == state.snapshot.generation):
                    updated = await self.store.result(op, {"terminal": evidence["status"], "compaction": evidence,
                                                           "delivery": "not_attempted"})
                    state.compaction_pending = False
                    result.append(updated)
                continue
            if op.kind in {"fork", "effort", "model", "approval", "artifact_upload", "unknown_review", "root_replace"}:
                continue
            if tid in state.snapshot.confirmed and not previous.get("terminal"):
                updated = await self.store.result(op, {"terminal": state.snapshot.terminals[tid],
                                                       "delivery": "same_turn_already_recorded" if tid in handled
                                                       else "not_attempted"})
                if tid not in handled:
                    result.append(updated)
                handled.add(tid)
        return result
