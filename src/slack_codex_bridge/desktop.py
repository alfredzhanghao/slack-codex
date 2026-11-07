"""Version-specific desktop protocol adapter. Never owns backend process cleanup."""

import asyncio
import json
import time
from collections.abc import Callable
from urllib.parse import urlparse
from pathlib import Path, PureWindowsPath

import aiohttp

from . import __version__
from .config import Config
from .approval import PendingRequest, presentation, response
from .inputs import file_inputs
from .attachments import attachment_inputs
from .artifacts import linked_files
from .model import Action, BridgeError, Event, Receipt, Snapshot, Thread, new_id, activity_ms, recent_threads
from .thread_names import names as thread_names

THREAD_SOURCES = ["cli", "vscode", "exec", "appServer", "subAgent", "subAgentReview",
                  "subAgentCompact", "subAgentThreadSpawn", "subAgentOther", "unknown"]
MAIN_THREAD_SOURCES = ["cli", "vscode", "exec", "appServer", "unknown"]


class RPC:
    def __init__(self, endpoint: str, timeout: float, notify: Callable):
        self.endpoint, self.timeout, self.notify = endpoint, timeout, notify
        self.session = None
        self.ws = None
        self.reader = None
        self.pending: dict[str, tuple[asyncio.Future, bool]] = {}
        self.methods: dict[str, str] = {}
        self.serial = 0
        self.generation = 0
        self.after_write = None

    @property
    def connected(self):
        return self.ws is not None and not self.ws.closed

    async def connect(self):
        if self.connected:
            raise BridgeError("backend_already_connected")
        await self.close()
        self.generation += 1
        self.session = aiohttp.ClientSession(trust_env=False)
        try:
            async with asyncio.timeout(self.timeout):
                self.ws = await self.session.ws_connect(self.endpoint, heartbeat=20, max_msg_size=32 * 1024 * 1024)
            self.reader = asyncio.create_task(self._read(self.ws, self.generation))
            await self.call("initialize", {"clientInfo": {"name": "slack_codex_bridge", "title": "Slack Codex Bridge",
                                                         "version": __version__},
                                           "capabilities": {"experimentalApi": True}})
            await self.ws.send_json({"method": "initialized", "params": {}})
        except BaseException as error:
            await self.close()
            if isinstance(error, (asyncio.CancelledError, BridgeError)):
                raise
            raise BridgeError("backend_connect_failed") from None

    async def call(self, method: str, params: dict, *, write=False):
        if not self.connected:
            raise BridgeError("backend_unavailable")
        self.serial += 1
        key = str(self.serial)
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = (future, write)
        self.methods[key] = method
        try:
            await self.ws.send_json({"id": self.serial, "method": method, "params": params})
            if write and self.after_write:
                try:
                    await self.after_write(method, self.serial)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise BridgeError("backend_outcome_unknown", unknown=True) from None
            return await asyncio.wait_for(future, self.timeout)
        except asyncio.CancelledError:
            raise
        except BridgeError:
            raise
        except (TimeoutError, aiohttp.ClientError, ConnectionError, RuntimeError):
            raise BridgeError("backend_outcome_unknown" if write else "backend_read_failed", unknown=write) from None
        finally:
            self.pending.pop(key, None)
            self.methods.pop(key, None)
            if not future.done():
                future.cancel()

    async def _read(self, ws, generation):
        try:
            async for frame in ws:
                if frame.type != aiohttp.WSMsgType.TEXT:
                    break
                message = json.loads(frame.data)
                self.dispatch(message, generation)
        except (ValueError, TypeError, AttributeError, KeyError, aiohttp.ClientError, RuntimeError):
            pass
        finally:
            try:
                await ws.close()
            finally:
                if generation == self.generation:
                    for future, write in list(self.pending.values()):
                        if not future.done():
                            future.set_exception(BridgeError("backend_disconnected", unknown=write))
                    self.notify({"method": "bridge/disconnected", "params": {}}, generation)

    def dispatch(self, message: dict, generation: int):
        if generation != self.generation:
            return
        # A server request can have the same numeric ID as our pending client call.
        if not isinstance(message, dict):
            raise ValueError("invalid frame")
        if isinstance(message.get("method"), str):
            self.notify(message, generation)
            return
        if "id" not in message:
            return
        # Client IDs are always integers; never conflate a string ID with an integer.
        key = str(message["id"]) if type(message["id"]) is int else None
        entry = self.pending.get(key)
        if entry and not entry[0].done():
            if "error" in message:
                error = message["error"]
                detail = str(error.get("message", "")).lower() if isinstance(error, dict) else ""
                code = ("backend_thread_not_loaded" if isinstance(error, dict) and error.get("code") == -32600
                        and "thread" in detail and "not loaded" in detail else "backend_rejected")
                if (isinstance(error, dict) and error.get("code") == -32600
                        and self.methods.get(key) == "thread/resume" and "already has an active writer" in detail):
                    code = "backend_thread_active_writer"
                entry[0].set_exception(BridgeError(code))
            elif "result" in message:
                if not isinstance(message["result"], dict):
                    entry[0].set_exception(BridgeError("backend_invalid_response", unknown=entry[1]))
                else:
                    entry[0].set_result(message["result"])

    async def close(self):
        if self.ws:
            await self.ws.close()
        if self.reader and self.reader is not asyncio.current_task():
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        if self.session:
            await self.session.close()
        self.ws = self.session = self.reader = None


class Desktop:
    def __init__(self, cfg: Config, endpoint: str):
        address = urlparse(endpoint)
        self.cfg = cfg
        if address.scheme == "codex-ipc":
            from .coordination import CoordinationRPC
            self.rpc = CoordinationRPC(cfg, endpoint, self._notify)
        else:
            if address.scheme != "ws" or address.hostname != "127.0.0.1" or not address.port or address.username:
                raise BridgeError("unverified_backend_endpoint")
            self.rpc = RPC(endpoint, cfg.request_timeout, self._notify)
        self.desktop_only_controls = getattr(self.rpc, "desktop_only_controls", frozenset())
        self.events: asyncio.Queue[Event] = asyncio.Queue(maxsize=512)
        self.terminals: dict[tuple[str, str], str] = {}
        self.approvals: dict[str, str] = {}
        self.requests: dict[str, dict] = {}
        self.compacting: set[str] = set()
        self.compaction_uncertain: set[str] = set()
        self.subscribed: set[str] = set()
        self.recovery_reads: dict[tuple[str, str], tuple[str, float, int]] = {}
        self.pending_requests: dict[str, PendingRequest] = {}
        self.items: dict[tuple[str, str, str], dict] = {}
        self.compactions: dict[str, dict] = {}
        self.limits: dict[str, tuple[dict, float, int]] = {}
        self.limits_read_at = None
        self.effective: dict[str, dict] = {}
        self.rerouted: dict[tuple[str, str], str] = {}
        self.context_usage: dict[str, dict] = {}
        self.context_models: dict[str, str | None] = {}

    @property
    def connected(self):
        return self.rpc.connected

    async def connect(self):
        self.subscribed.clear()
        self.recovery_reads.clear()
        self.terminals.clear()
        self.approvals.clear()
        self.requests.clear()
        self.invalidate_requests()
        self.items.clear()
        self.rerouted.clear()
        self.context_usage.clear()
        self.context_models.clear()
        self.compaction_uncertain.update(self.compacting)
        self.compacting.clear()
        await self.rpc.connect()

    async def close(self):
        self.invalidate_requests()
        await self.rpc.close()

    def invalidate_requests(self, thread_id=None):
        for key, request in list(self.pending_requests.items()):
            if thread_id is not None and request.thread_id != thread_id:
                continue
            request.status = "closed"
            self.pending_requests.pop(key, None)

    def _notify(self, message, generation):
        if generation != self.rpc.generation:
            return
        method, p = message.get("method"), message.get("params") or {}
        if not isinstance(p, dict):
            raise ValueError("invalid params")
        thread = p.get("threadId") or p.get("thread", {}).get("id", "")
        turn = p.get("turnId") or p.get("turn", {}).get("id", "")
        item = p.get("item") or {}
        kind, text, status = "changed", "", ""
        if method == "bridge/disconnected":
            self.context_usage.clear()
            self.compaction_uncertain.update(self.compacting)
            self.invalidate_requests()
            kind = "disconnected"
        elif "id" in message:
            self.approvals[json.dumps(message["id"])] = thread
            self.requests[json.dumps(message["id"])] = message
            key = json.dumps(message["id"])
            if key not in self.pending_requests:
                self.pending_requests[key] = PendingRequest(message, getattr(self.rpc, "ws", None), generation)
            kind = "approval"
        elif method == "serverRequest/resolved":
            key = json.dumps(p.get("requestId"))
            thread = self.approvals.pop(key, None) or thread
            self.requests.pop(key, None)
            pending = self.pending_requests.pop(key, None)
            if pending:
                pending.status = "closed"
            kind = "approval_resolved"
        elif method == "account/rateLimits/updated":
            self._limits(p)
            kind = "limits"
        elif method == "thread/tokenUsage/updated" and thread:
            usage = p.get("tokenUsage") or {}
            window = usage.get("modelContextWindow")
            used = (usage.get("last") or {}).get("totalTokens")
            if type(window) is int and window > 0 and type(used) is int and used >= 0:
                self.context_usage[thread] = {"window": window, "used": used,
                    "remaining": max(0, window - used), "observed_ms": int(time.time() * 1000),
                    "generation": generation, "turn_id": turn}
            else:
                self.context_usage.pop(thread, None)
        elif method == "account/updated":
            self.limits = {key: (bucket, observed, -1) for key, (bucket, observed, _) in self.limits.items()}
            self.limits_read_at = None
        elif method == "model/rerouted" and thread and turn and isinstance(p.get("toModel"), str):
            self.rerouted[thread, turn] = p["toModel"]
            self.context_usage.pop(thread, None)
        elif method == "turn/completed":
            status = p.get("turn", {}).get("status", "")
            if thread and turn and status in {"completed", "failed", "interrupted"}:
                self.terminals[thread, turn] = status
            if thread in self.compacting and status in {"failed", "interrupted"}:
                self.compacting.discard(thread)
                self.compactions[thread] = {**self.compactions.get(thread, {}), "generation": generation,
                                             "status": status, "turn_id": turn}
            kind = "terminal"
        elif method == "turn/started":
            kind = "started"
        elif method == "item/agentMessage/delta":
            kind, text = "progress", p.get("delta", "")
        elif method == "item/completed" and item.get("type") == "agentMessage":
            kind = "final" if item.get("phase") == "final_answer" else "commentary"
            # Some pinned versions omit phase. Completion rendering uses full-turn readback.
            text = item.get("text", "")
        if item.get("type") == "contextCompaction":
            self.context_usage.pop(thread, None)
            if method == "item/started":
                self.compacting.add(thread)
            elif method == "item/completed":
                self.compacting.discard(thread)
                self.compaction_uncertain.discard(thread)
                self.compactions[thread] = {**self.compactions.get(thread, {}), "generation": generation, "status": "completed",
                                             "turn_id": turn, "item_id": item.get("id")}
        if method in {"item/started", "item/completed"} and thread and turn and item.get("id"):
            if item.get("type") == "fileChange":
                self.items[thread, turn, item["id"]] = item
        if method == "thread/compacted" and thread:
            self.context_usage.pop(thread, None)
            self.compacting.discard(thread)
            self.compaction_uncertain.discard(thread)
            self.compactions[thread] = {**self.compactions.get(thread, {}), "generation": generation,
                                       "status": "completed", "turn_id": turn}
        event = Event(kind, thread, turn, item.get("id", p.get("itemId", "")), text, status, generation)
        if self.events.full():
            if kind == "progress":
                return
            # Never silently drop terminal/approval/lifecycle changes.
            asyncio.create_task(self.rpc.close())
            return
        self.events.put_nowait(event)

    def _thread(self, obj, *, discovery=False, allowed=None) -> Thread:
        cwd, tid = obj.get("cwd"), obj.get("id")
        if not isinstance(cwd, str) or not isinstance(tid, str):
            raise BridgeError("thread_outside_allowlist")
        if allowed is None or not discovery:
            allowed = self.cfg.allows(cwd)
        if not discovery and not allowed:
            raise BridgeError("thread_outside_allowlist")
        if discovery and not allowed and not PureWindowsPath(cwd).is_absolute():
            raise BridgeError("thread_environment_unverified")
        # The pinned route is native Windows only. Never guess WSL/remote mappings.
        environments = obj.get("environments") or []
        if environments and (len(environments) != 1 or not self.cfg.environment_id
                             or environments[0].get("environmentId") != self.cfg.environment_id
                             or Path(environments[0].get("cwd", "")).resolve() != Path(cwd).resolve()):
            raise BridgeError("thread_environment_unverified")
        renamed = thread_names(self.cfg).get(tid) if allowed else None
        title = next((value for value in (obj.get("name"), obj.get("title"), obj.get("preview"))
                      if isinstance(value, str) and value.strip() and value != tid), tid[:8])
        return Thread(tid, (renamed or title)[:160], cwd,
                      allowed=allowed, updated_at_ms=activity_ms(obj.get("updatedAt", obj.get("updated_at"))))

    async def get_thread(self, thread_id: str) -> Thread:
        thread = self._thread(await self._metadata(thread_id))
        if thread.id != thread_id:
            raise BridgeError("thread_identity_mismatch")
        return thread

    async def _metadata(self, thread_id):
        try:
            return (await self.rpc.call("thread/read", {"threadId": thread_id, "includeTurns": False}))["thread"]
        except BridgeError as error:
            if error.code != "backend_thread_not_loaded":
                raise
        # Unloaded persisted chats require resume. First establish their exact
        # cwd from metadata; never resume an unverified arbitrary target/path.
        cursor, seen = None, set()
        while True:
            value = await self.rpc.call("thread/list", {"limit": 100, "cursor": cursor,
                                                        "sortKey": "updated_at", "sortDirection": "desc",
                                                        "archived": False, "sourceKinds": THREAD_SOURCES,
                                                        "useStateDbOnly": True})
            match = next((row for row in value.get("data", []) if row.get("id") == thread_id), None)
            if match:
                self._thread(match)
                return match
            cursor = value.get("nextCursor")
            if not cursor:
                break
            if cursor in seen:
                raise BridgeError("thread_listing_incomplete")
            seen.add(cursor)
        raise BridgeError("persisted_thread_metadata_unavailable")

    async def list_threads(self, project="") -> list[Thread]:
        result, cursor, seen, allowed_cwds = {}, None, set(), {}
        while True:
            value = await self.rpc.call("thread/list", {"limit": 100, "cursor": cursor,
                                                        "sortKey": "updated_at", "sortDirection": "desc",
                                                        "archived": False, "sourceKinds": MAIN_THREAD_SOURCES,
                                                        "useStateDbOnly": True,
                                                        **({"cwd": project} if project else {})})
            for item in value.get("data", []):
                # Defend against a backend returning internal sessions despite the filter.
                source = item.get("source", "unknown")
                if not isinstance(source, str) or source not in MAIN_THREAD_SOURCES or item.get("archived"):
                    continue
                try:
                    cwd = item.get("cwd")
                    if isinstance(cwd, str) and cwd not in allowed_cwds:
                        allowed_cwds[cwd] = self.cfg.allows(cwd)
                    thread = self._thread(item, discovery=True,
                                          allowed=allowed_cwds.get(cwd) if isinstance(cwd, str) else None)
                    previous = result.get(thread.id)
                    if previous is None or (thread.updated_at_ms or 0) > (previous.updated_at_ms or 0):
                        result[thread.id] = thread
                except BridgeError:
                    continue
            cursor = value.get("nextCursor")
            if not cursor:
                return recent_threads(result.values())
            if cursor in seen:
                raise BridgeError("thread_listing_incomplete")
            seen.add(cursor)

    async def snapshot(self, thread_id: str) -> Snapshot:
        try:
            return await self._snapshot(thread_id)
        except (KeyError, ValueError, TypeError, AttributeError):
            raise BridgeError("backend_invalid_snapshot") from None

    async def _snapshot(self, thread_id: str) -> Snapshot:
        meta = await self._metadata(thread_id)
        thread = self._thread(meta)
        if thread.id != thread_id:
            raise BridgeError("thread_identity_mismatch")
        resumed = await self.rpc.call("thread/resume", {"threadId": thread_id, "excludeTurns": True})
        resumed_thread = self._thread(resumed.get("thread", {}))
        if (resumed_thread.id != thread_id or Path(resumed_thread.cwd).resolve() != Path(thread.cwd).resolve()):
            raise BridgeError("thread_identity_mismatch")
        self.effective[thread_id] = {key: resumed.get(key) for key in
                                    ("approvalPolicy", "approvalsReviewer", "sandbox", "modelProvider", "cwd")}
        # Resume re-joins this live thread without overriding its model, cwd or policy.
        self.subscribed.add(thread_id)
        rows = (await self.rpc.call("thread/turns/list", {"threadId": thread_id, "limit": 20,
                                                        "sortDirection": "desc", "itemsView": "notLoaded"})).get("data", [])
        current = resumed.get("thread", {}).get("status") or meta.get("status") or {}
        status = current.get("type")
        flags = current.get("activeFlags", [])
        latest = rows[0] if rows else {}
        active = latest.get("id") if status == "active" and latest.get("status") == "inProgress" else None
        terminals = {x["id"]: x["status"] for x in rows if x.get("status") in {"completed", "failed", "interrupted"}}
        confirmed = {tid for tid, value in terminals.items() if self.terminals.get((thread_id, tid)) == value}
        # After a lost connection/event, use two full, identical terminal readbacks
        # separated by >=3 seconds and corroborated idle metadata. One terminal
        # snapshot, an old inProgress row or a completed event alone never releases input.
        recovery = set(terminals) - confirmed
        full = None
        if recovery and status == "idle" and not flags:
            full = await self.rpc.call("thread/turns/list", {"threadId": thread_id, "limit": 20,
                                                              "sortDirection": "desc", "itemsView": "full"})
        for tid in recovery:
            key = (thread_id, tid)
            if status != "idle" or flags:
                self.recovery_reads.pop(key, None)
                continue
            row = next((x for x in full.get("data", []) if x.get("id") == tid), None)
            if not row or row.get("status") != terminals[tid]:
                self.recovery_reads.pop(key, None)
                continue
            fingerprint = json.dumps(row, sort_keys=True, separators=(",", ":"))
            prior = self.recovery_reads.get(key)
            clock = time.monotonic()
            if prior and prior[0] == fingerprint and prior[2] == self.rpc.generation:
                if clock - prior[1] >= 3:
                    confirmed.add(tid)
            else:
                self.recovery_reads[key] = (fingerprint, clock, self.rpc.generation)
        for key in list(self.recovery_reads):
            if key[0] == thread_id and key[1] not in terminals:
                self.recovery_reads.pop(key)
        if status == "idle" and not flags and latest.get("id") in confirmed:
            self.compaction_uncertain.discard(thread_id)
        model = self.rerouted.get((thread_id, active), resumed.get("model"))
        if thread_id in self.context_models and self.context_models[thread_id] != model:
            self.context_usage.pop(thread_id, None)
        self.context_models[thread_id] = model
        return Snapshot(thread, trusted=status in {"idle", "active"}, idle=status == "idle", active_turn=active,
                        approval="waitingOnApproval" in flags or thread_id in self.approvals.values()
                        or "" in self.approvals.values(),
                        compacting=thread_id in self.compacting or thread_id in self.compaction_uncertain,
                        model=model,
                        effort=None if (thread_id, active) in self.rerouted else resumed.get("reasoningEffort"),
                        generation=self.rpc.generation, terminals=terminals, confirmed=confirmed,
                        known_turns={x["id"] for x in rows if x.get("id")},
                        context=dict(self.context_usage.get(thread_id, {})),
                        progress=self.rpc.public_progress(thread_id, active)
                        if hasattr(self.rpc, "public_progress") else "")

    async def send(self, action: Action) -> Receipt:
        if action.kind == "turn_start":
            inputs = [{"type": "text", "text": action.text}]
            if action.data.get("files"):
                snapshot = await self.snapshot(action.thread_id)
                inputs = file_inputs(self.cfg, snapshot, action.data["files"], action.text)
            if action.data.get("attachments"):
                if action.data.get("files"):
                    raise BridgeError("mixed_file_input_sources")
                snapshot = await self.snapshot(action.thread_id)
                inputs = await asyncio.to_thread(attachment_inputs, self.cfg, snapshot, action.data["attachments"], action.text)
            if action.data.get("skill"):
                rows = await self.skills(action.thread_id)
                selected = [s for s in rows if s.get("path") == action.data["skill"] and s.get("enabled")]
                if len(selected) != 1:
                    raise BridgeError("skill_changed_or_unavailable")
                inputs.append({"type": "skill", "name": selected[0]["name"], "path": selected[0]["path"]})
            params = {"threadId": action.thread_id, "input": inputs}
            if action.data.get("model"):
                self.context_usage.pop(action.thread_id, None)
                params["model"] = action.data["model"]
            if action.data.get("effort"):
                params["effort"] = action.data["effort"]
            value = await self.rpc.call("turn/start", params, write=True)
            tid = value.get("turn", {}).get("id")
            if not tid:
                raise BridgeError("backend_missing_turn_id", unknown=True)
            return Receipt(tid)
        if action.kind == "compact":
            self.context_usage.pop(action.thread_id, None)
            # Set protection before sending: events may arrive before the RPC response.
            self.compacting.add(action.thread_id)
            token = new_id()
            self.compactions[action.thread_id] = {"generation": self.rpc.generation, "status": "accepted_pending",
                                                  "operation_token": token}
            try:
                await self.rpc.call("thread/compact/start", {"threadId": action.thread_id}, write=True)
            except BridgeError as error:
                if not error.unknown:
                    self.compacting.discard(action.thread_id)
                raise
            return Receipt(result={"compaction": "pending", "operation_token": token})
        if action.kind == "fork":
            rows = await self.rpc.call("thread/turns/list", {"threadId": action.thread_id, "limit": 1,
                                                            "sortDirection": "desc", "itemsView": "notLoaded"})
            last = next(iter(rows.get("data", [])), {})
            if not last.get("id") or last.get("status") not in {"completed", "failed", "interrupted"}:
                raise BridgeError("fork_completed_history_required")
            goal = await self.rpc.call("thread/goal/get", {"threadId": action.thread_id})
            params = {"threadId": action.thread_id, "lastTurnId": last["id"],
                      "excludeTurns": True, "ephemeral": False}
            # A no-goal source needs no goal-continuation override. Never trigger
            # an inherited active goal just by creating a user-requested branch.
            if goal.get("goal") is not None:
                params["deferGoalContinuation"] = True
            value = await self.rpc.call("thread/fork", params, write=True)
            # Persist the returned ID before any fallible readback or Slack call.
            tid = value.get("thread", {}).get("id")
            if not isinstance(tid, str) or not tid or tid == action.thread_id:
                raise BridgeError("backend_missing_fork_id", unknown=True)
            return Receipt(thread_id=tid, result={"fork_binding": "not_attempted",
                           "effective": {k: value.get(k) for k in ("approvalPolicy", "approvalsReviewer", "sandbox",
                                                                   "modelProvider", "cwd")}})
        if action.kind == "approval":
            request = self.request(action.data.get("request_token", ""))
            result = response(request, self.items, action.data.get("choice", ""), action.data.get("answers"))
            if request.thread_id != action.thread_id or request.turn_id != action.turn_id:
                raise BridgeError("approval_target_changed")
            # No await between the last original-socket check, the one-shot claim and send.
            self.check_request(request)
            request.status = "submitting"
            try:
                await request.connection.send_json({"id": request.message["id"], "result": result})
            except asyncio.CancelledError:
                raise
            except Exception:
                raise BridgeError("approval_delivery_unknown", unknown=True) from None
            # JSON-RPC server responses have no correlated receipt. Resolved can be a
            # desktop winner/cleanup, so never claim our choice was accepted.
            raise BridgeError("approval_sent_unconfirmed", unknown=True)
        if not action.turn_id:
            raise BridgeError("missing_target_turn")
        if action.kind == "steer":
            value = await self.rpc.call("turn/steer", {"threadId": action.thread_id,
                                                       "expectedTurnId": action.turn_id,
                                                       "input": [{"type": "text", "text": action.text}]}, write=True)
            if value.get("turnId") != action.turn_id:
                raise BridgeError("backend_target_unconfirmed", unknown=True)
        elif action.kind == "interrupt":
            await self.rpc.call("turn/interrupt", {"threadId": action.thread_id, "turnId": action.turn_id}, write=True)
        else:
            raise BridgeError("unsupported_operation")
        return Receipt(action.turn_id)

    def check_request(self, request):
        if (request.status != "pending" or request.expires < time.monotonic()
                or request.connection is not self.rpc.ws or request.generation != self.rpc.generation
                or not self.connected or request.connection.closed
                or self.pending_requests.get(json.dumps(request.message["id"])) is not request):
            raise BridgeError("approval_expired_or_connection_changed")

    def request(self, token):
        request = next((r for r in self.pending_requests.values() if r.token == token), None)
        if not request:
            raise BridgeError("approval_not_pending")
        self.check_request(request)
        return request

    def request_view(self, token):
        request = self.request(token)
        return presentation(request, self.items)

    async def skills(self, thread_id):
        thread = await self.get_thread(thread_id)
        value = await self.rpc.call("skills/list", {"cwds": [thread.cwd], "forceReload": True})
        entries = [e for e in value.get("data", []) if Path(e.get("cwd", "")).resolve() == Path(thread.cwd).resolve()]
        if len(entries) != 1 or entries[0].get("errors"):
            raise BridgeError("skills_discovery_incomplete")
        return entries[0].get("skills", [])

    async def verify_fork(self, parent_id, fork_id, receipt):
        parent = await self.snapshot(parent_id)
        child = await self.snapshot(fork_id)
        expected = self.effective[parent_id]
        actual = self.effective[fork_id]
        returned = receipt.get("effective", {})
        if (not parent.trusted or not child.trusted or not child.idle or child.active_turn
                or Path(parent.thread.cwd).resolve() != Path(child.thread.cwd).resolve()
                or any(expected.get(k) is None or actual.get(k) != expected[k] or returned.get(k) != actual[k]
                       for k in ("approvalPolicy", "approvalsReviewer", "sandbox", "modelProvider", "cwd"))):
            raise BridgeError("fork_inherited_config_unverified")

    async def models(self):
        cursor, result, seen = None, {}, set()
        while True:
            value = await self.rpc.call("model/list", {"cursor": cursor, "limit": 100, "includeHidden": True})
            for row in value.get("data", []):
                if isinstance(row.get("model"), str):
                    result.setdefault(row["model"], row)
            cursor = value.get("nextCursor")
            if not cursor:
                return list(result.values())
            if cursor in seen:
                raise BridgeError("model_listing_incomplete")
            seen.add(cursor)

    async def efforts(self, model):
        matches = [m for m in await self.models() if model == m.get("model")]
        if len(matches) != 1:
            raise BridgeError("model_efforts_unavailable")
        return [r["reasoningEffort"] for r in matches[0].get("supportedReasoningEfforts", [])]

    def _limits(self, value, *, replace=False):
        buckets = value.get("rateLimitsByLimitId")
        if not isinstance(buckets, dict):
            bucket = value.get("rateLimits")
            buckets = {bucket.get("limitId") or "default": bucket} if isinstance(bucket, dict) else {}
        if replace:
            self.limits.clear()
        for name, bucket in buckets.items():
            if isinstance(bucket, dict):
                # Store only display fields, never account IDs, reset-credit IDs or metadata.
                windows = {key: {k: b.get(k) for k in ("usedPercent", "windowDurationMins", "resetsAt")}
                           for key in ("primary", "secondary") if isinstance(b := bucket.get(key), dict)}
                self.limits[str(name)] = ({"name": bucket.get("limitName") or str(name), **windows},
                                         time.time(), self.rpc.generation)

    @property
    def rate_limits_supported(self):
        return getattr(self.rpc, "rate_limits_supported", True)

    async def refresh_limits(self, *, force=False):
        if not self.rate_limits_supported:
            return
        if not force and self.limits_read_at is not None and time.monotonic() - self.limits_read_at < self.cfg.quota_refresh:
            return
        self.limits_read_at = time.monotonic()
        generation = self.rpc.generation
        value = await self.rpc.call("account/rateLimits/read", {})
        if generation == self.rpc.generation:
            self._limits(value, replace=True)

    async def latest_artifacts(self, thread_id: str) -> list[dict]:
        return await self.artifacts(thread_id, completed_only=True)

    async def artifacts(self, thread_id: str, turn_id="", *, completed_only=False) -> list[dict]:
        await self.get_thread(thread_id)
        value = await self.rpc.call("thread/turns/list", {"threadId": thread_id, "limit": 20 if turn_id or completed_only else 1,
                                                         "sortDirection": "desc", "itemsView": "full"})
        rows = value.get("data", [])
        if completed_only:
            rows = [row for row in rows if row.get("status") == "completed"]
            if not rows and value.get("nextCursor"):
                raise BridgeError("artifact_completed_history_unavailable")
        if turn_id:
            rows = [row for row in rows if row.get("id") == turn_id]
            if not rows:
                raise BridgeError("artifact_turn_unavailable")
        result = []
        for turn in rows[:1]:
            for item in turn.get("items", []):
                final = item.get("type") == "agentMessage" and item.get("phase") in {None, "final_answer"}
                if item.get("status") != "completed" and not final:
                    continue
                paths = []
                if item.get("type") == "fileChange":
                    for change in item.get("changes", []):
                        kind = change.get("kind", {})
                        if kind.get("type") in {"add", "update"}:
                            paths.append(kind.get("move_path") or change.get("path"))
                elif item.get("type") == "imageGeneration":
                    paths.append(item.get("savedPath"))
                elif final:
                    paths.extend(linked_files(item.get("text", "")))
                for path in paths:
                    if isinstance(path, str) and path:
                        result.append({"path": path, "turn_id": turn["id"], "item_id": item["id"]})
        return result[:20]

    async def final_messages(self, thread_id: str, turn_id: str) -> tuple[str, list[str]]:
        value = await self.rpc.call("thread/turns/list", {"threadId": thread_id, "limit": 20,
                                                         "sortDirection": "desc", "itemsView": "full"})
        row = next((x for x in value.get("data", []) if x.get("id") == turn_id), None)
        if not row or row.get("status") not in {"completed", "failed", "interrupted"}:
            raise BridgeError("terminal_not_confirmed")
        messages = [x for x in row.get("items", []) if x.get("type") == "agentMessage"]
        final = [x for x in messages if x.get("phase") == "final_answer"]
        if not final and all(x.get("phase") is None for x in messages):
            final = messages
        return row["status"], [x.get("text", "") for x in final if x.get("text")]
