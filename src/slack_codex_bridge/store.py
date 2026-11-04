"""All SQLite work, including connection creation, stays on one dedicated thread."""

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from .model import Binding, BridgeError, Operation, now_ms
from .recovery import checked_unknown
from .roots import review_binding

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS bindings (
 binding_id TEXT PRIMARY KEY, slack_team_id TEXT NOT NULL, slack_channel_id TEXT NOT NULL,
 slack_root_ts TEXT NOT NULL, owner_user_id TEXT NOT NULL, host_id TEXT NOT NULL,
 codex_thread_id TEXT NOT NULL, project_path TEXT NOT NULL, environment_key TEXT NOT NULL,
 binding_version INTEGER NOT NULL DEFAULT 1, disabled INTEGER NOT NULL DEFAULT 0,
 effective_from_ms INTEGER NOT NULL, next_effort_json TEXT, created_at_ms INTEGER NOT NULL,
 updated_at_ms INTEGER NOT NULL, UNIQUE(slack_team_id,slack_channel_id,slack_root_ts)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_root ON bindings(host_id,codex_thread_id) WHERE disabled=0;
CREATE TABLE IF NOT EXISTS operations (
 op_id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, binding_id TEXT NOT NULL REFERENCES bindings(binding_id),
 binding_version INTEGER NOT NULL, kind TEXT NOT NULL, target_thread_id TEXT NOT NULL, target_turn_id TEXT,
 payload_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('received','sending','accepted','rejected','unknown')),
 backend_ref_json TEXT, result_json TEXT, error_reason TEXT,
 unknown_review_state TEXT NOT NULL DEFAULT 'not_required' CHECK(unknown_review_state IN ('not_required','pending','acknowledged')),
 review_json TEXT, supersedes_op_id TEXT REFERENCES operations(op_id), created_at_ms INTEGER NOT NULL,
 updated_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS operations_by_thread ON operations(target_thread_id,status,created_at_ms);
CREATE TABLE IF NOT EXISTS input_messages (
 source_key TEXT PRIMARY KEY, binding_id TEXT NOT NULL REFERENCES bindings(binding_id),
 binding_version INTEGER NOT NULL, epoch TEXT NOT NULL, status TEXT NOT NULL,
 code TEXT, created_at_ms INTEGER NOT NULL, attachment INTEGER NOT NULL DEFAULT 0
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bridge-sqlite")
        self.db: sqlite3.Connection | None = None

    async def _call(self, fn, *args):
        try:
            return await asyncio.get_running_loop().run_in_executor(self.worker, fn, *args)
        except sqlite3.Error:
            raise BridgeError("database_unavailable") from None

    async def open(self):
        await self._call(self._open)
        return self

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=0.2)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)

    async def close(self):
        if self.db is not None:
            await self._call(self.db.close)
            self.db = None
        self.worker.shutdown(wait=True)

    async def bind(self, value: Binding) -> Binding:
        return await self._call(self._bind, value)

    def _bind(self, value):
        existing = self.db.execute("SELECT * FROM bindings WHERE host_id=? AND codex_thread_id=? AND disabled=0",
                                   (value.host_id, value.codex_thread_id)).fetchone()
        if existing:
            return Binding(**dict(existing))
        data = asdict(value)
        with self.db:
            self.db.execute(f"INSERT INTO bindings ({','.join(data)}) VALUES ({','.join('?' for _ in data)})",
                            tuple(data.values()))
        return value

    async def bindings(self) -> list[Binding]:
        return await self._call(lambda: [Binding(**dict(r)) for r in self.db.execute(
            "SELECT * FROM bindings WHERE disabled=0 ORDER BY created_at_ms")])

    async def attach_root(self, binding_id: str, root_ts: str) -> Binding:
        def attach():
            with self.db:
                cursor = self.db.execute("UPDATE bindings SET slack_root_ts=?,updated_at_ms=? "
                                         "WHERE binding_id=? AND slack_root_ts LIKE 'pending:%'",
                                         (root_ts, now_ms(), binding_id))
                if cursor.rowcount != 1:
                    raise BridgeError("binding_root_state_changed")
            return Binding(**dict(self.db.execute("SELECT * FROM bindings WHERE binding_id=?",
                                                   (binding_id,)).fetchone()))
        return await self._call(attach)

    async def begin_root_replacement(self, op):
        def begin():
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                current = self._operation("op_id", op.op_id)
                row = self.db.execute("SELECT * FROM bindings WHERE binding_id=?", (op.binding_id,)).fetchone()
                before = json.loads(current.payload_json)["data"]["binding"] if current else None
                if (not row or not current or current.kind != "root_replace" or current.status != "received"
                        or dict(row) != before or row["disabled"] or row["binding_version"] != op.binding_version
                        or row["codex_thread_id"] != op.target_thread_id):
                    raise BridgeError("binding_root_state_changed")
                if self.db.execute("SELECT 1 FROM operations WHERE binding_id=? AND op_id!=? "
                                   "AND status IN ('received','sending') LIMIT 1", (op.binding_id, op.op_id)).fetchone():
                    raise BridgeError("root_replacement_submission_in_progress")
                version, pending, clock = op.binding_version + 1, "pending:" + op.op_id, now_ms()
                self.db.execute("UPDATE bindings SET slack_root_ts=?,binding_version=?,next_effort_json=NULL,"
                                "effective_from_ms=?,updated_at_ms=? WHERE binding_id=?",
                                (pending, version, clock, clock, op.binding_id))
                audit = {"binding_rotated": True, "from_version": op.binding_version,
                         "to_version": version, "previous_root": row["slack_root_ts"], "root_ts": pending}
                self.db.execute("UPDATE operations SET status='sending',result_json=?,updated_at_ms=? WHERE op_id=?",
                                (json.dumps(audit), clock, op.op_id))
            return Binding(**dict(self.db.execute("SELECT * FROM bindings WHERE binding_id=?",
                                                  (op.binding_id,)).fetchone()))
        return await self._call(begin)

    async def finish_root_replacement(self, op, binding, root):
        def finish():
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                current = self._operation("op_id", op.op_id)
                row = self.db.execute("SELECT * FROM bindings WHERE binding_id=?", (op.binding_id,)).fetchone()
                if (not current or current.status != "sending" or current.kind != "root_replace"
                        or not row or dict(row) != asdict(binding) or row["disabled"]
                        or binding.binding_version != op.binding_version + 1
                        or binding.slack_root_ts != "pending:" + op.op_id):
                    raise BridgeError("binding_root_state_changed")
                result = {**json.loads(current.result_json), "root_ts": root}
                self.db.execute("UPDATE bindings SET slack_root_ts=?,updated_at_ms=? WHERE binding_id=?",
                                (root, now_ms(), op.binding_id))
                self.db.execute("UPDATE operations SET status='accepted',result_json=?,backend_ref_json=?,"
                                "updated_at_ms=? WHERE op_id=?", (json.dumps(result), json.dumps({"root_ts": root}),
                                                                 now_ms(), op.op_id))
            return Binding(**dict(self.db.execute("SELECT * FROM bindings WHERE binding_id=?",
                                                  (op.binding_id,)).fetchone()))
        return await self._call(finish)

    async def check_unknown(self, op, binding, expected, *, acknowledged=False):
        return await self._call(self._check_unknown, op, binding, expected, acknowledged)

    def _check_unknown(self, op, binding, expected, acknowledged=False):
        rotations = []
        if op and op.binding_version != binding.binding_version:
            rotations = [Operation(**dict(r)) for r in self.db.execute(
                "SELECT * FROM operations WHERE binding_id=? AND kind='root_replace' "
                "AND binding_version>=? AND binding_version<?", (binding.binding_id, op.binding_version,
                                                                 binding.binding_version))]
        effective = review_binding(op, binding, rotations)
        return checked_unknown(op, effective, expected, acknowledged=acknowledged)

    async def root_operations(self, binding_id):
        return await self._call(lambda: [Operation(**dict(row)) for row in self.db.execute(
            "SELECT * FROM operations WHERE binding_id=? AND kind='root_replace' ORDER BY created_at_ms DESC LIMIT 3",
            (binding_id,))])

    async def by_source(self, key: str) -> Operation | None:
        return await self._call(self._operation, "source_key", key)

    async def effort(self, binding: Binding, setting: dict) -> Binding:
        def save():
            with self.db:
                row = self.db.execute("SELECT * FROM bindings WHERE binding_id=? AND binding_version=? AND disabled=0",
                                      (binding.binding_id, binding.binding_version)).fetchone()
                if not row:
                    raise BridgeError("binding_state_changed")
                old = json.loads(row["next_effort_json"] or "{}")
                if setting["sent_ms"] < old.get("sent_ms", -1):
                    raise BridgeError("older_effort_choice")
                if setting["sent_ms"] == old.get("sent_ms") and setting["op_id"] != old.get("op_id"):
                    raise BridgeError("older_effort_choice")
                self.db.execute("UPDATE bindings SET next_effort_json=?,updated_at_ms=? WHERE binding_id=?",
                                (json.dumps(setting), now_ms(), binding.binding_id))
            return Binding(**dict(self.db.execute("SELECT * FROM bindings WHERE binding_id=?",
                                                  (binding.binding_id,)).fetchone()))
        return await self._call(save)

    async def operation(self, op_id: str) -> Operation | None:
        return await self._call(self._operation, "op_id", op_id)

    def _operation(self, column, value):
        row = self.db.execute(f"SELECT * FROM operations WHERE {column}=?", (value,)).fetchone()
        return Operation(**dict(row)) if row else None

    async def record_once(self, op: Operation) -> tuple[Operation, bool]:
        return await self._call(self._record_once, op)

    def _record_once(self, op):
        data = asdict(op)
        with self.db:
            cursor = self.db.execute(f"INSERT OR IGNORE INTO operations ({','.join(data)}) "
                                     f"VALUES ({','.join('?' for _ in data)})", tuple(data.values()))
        return self._operation("source_key", op.source_key), cursor.rowcount == 1

    async def transition(self, op_id: str, expected: str, status: str, **fields) -> Operation:
        permitted = {"backend_ref_json", "result_json", "error_reason", "unknown_review_state", "payload_json"}
        if not set(fields) <= permitted:
            raise ValueError("unsupported transition field")
        return await self._call(self._transition, op_id, expected, status, fields)

    def _transition(self, op_id, expected, status, fields):
        legal = {"received": {"sending", "rejected"}, "sending": {"sending", "accepted", "rejected", "unknown"},
                 "unknown": {"accepted", "rejected"}, "accepted": {"accepted"}}
        if status not in legal.get(expected, set()):
            raise BridgeError("invalid_operation_transition")
        if expected == status == "sending":
            current = self._operation("op_id", op_id)
            if not current or current.kind != "artifact_upload":
                raise BridgeError("invalid_operation_transition")
        fields.update(status=status, updated_at_ms=now_ms())
        if status == "unknown":
            fields["unknown_review_state"] = "pending"
        elif expected == "unknown":
            fields["unknown_review_state"] = "not_required"
        with self.db:
            if expected == "received" and status == "sending":
                if not self.db.in_transaction:
                    self.db.execute("BEGIN IMMEDIATE")
                child = self._operation("op_id", op_id)
                if child and child.supersedes_op_id:
                    parent = self._operation("op_id", child.supersedes_op_id)
                    row = self.db.execute("SELECT * FROM bindings WHERE binding_id=?", (child.binding_id,)).fetchone()
                    self._check_unknown(parent, Binding(**dict(row)),
                                    json.loads(child.payload_json)["supersedes_fingerprint"], acknowledged=True)
            cursor = self.db.execute(f"UPDATE operations SET {','.join(k+'=?' for k in fields)} "
                                     "WHERE op_id=? AND status=?", (*fields.values(), op_id, expected))
            if cursor.rowcount != 1:
                raise BridgeError("operation_state_changed")
        return self._operation("op_id", op_id)

    async def recover(self):
        await self._call(self._recover)

    def _recover(self):
        with self.db:
            self.db.execute("UPDATE input_messages SET status='revoked',code='restart_no_replay' "
                            "WHERE status IN ('received','staged')")
            self.db.execute("UPDATE operations SET status='rejected',error_reason='restart_before_send',"
                            "updated_at_ms=? WHERE status='received'", (now_ms(),))
            self.db.execute("UPDATE operations SET status='unknown',unknown_review_state='pending',"
                            "error_reason='restart_during_send',updated_at_ms=? WHERE status='sending'", (now_ms(),))

    async def claim_input(self, key, binding, epoch, *, attachment=False):
        def claim():
            with self.db:
                return self.db.execute("INSERT OR IGNORE INTO input_messages VALUES (?,?,?,?,?,?,?,?)",
                    (key, binding.binding_id, binding.binding_version, epoch, "received", None,
                     now_ms(), int(attachment))).rowcount == 1
        return await self._call(claim)

    async def finish_input(self, key, status, code=None):
        def finish():
            with self.db:
                self.db.execute("UPDATE input_messages SET status=?,code=? WHERE source_key=?", (status, code, key))
                if status in {"consumed", "rejected"}:
                    self.db.execute("UPDATE input_messages SET status='resolved' WHERE attachment=1 "
                        "AND status IN ('staged','revoked') AND binding_id="
                        "(SELECT binding_id FROM input_messages WHERE source_key=?)", (key,))
        await self._call(finish)

    async def pending_input(self, binding_id):
        return await self._call(lambda: self.db.execute(
            "SELECT 1 FROM input_messages WHERE binding_id=? AND attachment=1 AND status IN ('staged','revoked') LIMIT 1",
            (binding_id,)).fetchone() is not None)

    async def blockers(self, thread_id: str) -> list[Operation]:
        return await self._call(lambda: [Operation(**dict(r)) for r in self.db.execute(
            "SELECT * FROM operations WHERE target_thread_id=? AND kind NOT IN ('artifact_upload','unknown_review','root_replace') AND "
            "(status='sending' OR (status='unknown' AND unknown_review_state!='acknowledged') "
            "OR (status='accepted' AND error_reason='semantic_mismatch'))",
            (thread_id,))])

    async def accepted(self, thread_id: str) -> list[Operation]:
        return await self._call(lambda: [Operation(**dict(r)) for r in self.db.execute(
            "SELECT * FROM operations WHERE target_thread_id=? AND status='accepted' ORDER BY created_at_ms DESC",
            (thread_id,))])

    async def uploads(self, binding_id: str, *, pending=False) -> list[Operation]:
        suffix = (" AND (status='sending' OR (status='unknown' AND unknown_review_state!='acknowledged'))"
                  if pending else " ORDER BY created_at_ms DESC LIMIT 20")
        return await self._call(lambda: [Operation(**dict(r)) for r in self.db.execute(
            "SELECT * FROM operations WHERE binding_id=? AND kind='artifact_upload'" + suffix, (binding_id,))])

    async def acknowledge_unknown(self, review, actor):
        def acknowledge():
            with self.db:
                if not self.db.in_transaction:
                    self.db.execute("BEGIN IMMEDIATE")
                current = self._operation("op_id", review.op_id)
                row = self.db.execute("SELECT * FROM bindings WHERE binding_id=?", (review.binding_id,)).fetchone()
                binding = Binding(**dict(row)) if row else None
                if (not binding or binding.disabled or binding.binding_version != review.binding_version
                        or binding.owner_user_id != actor or binding.codex_thread_id != review.target_thread_id
                        or not current or current.kind != "unknown_review" or current.status != "sending"):
                    raise BridgeError("review_binding_or_state_changed")
                data = json.loads(current.payload_json)["data"]
                original = self._check_unknown(self._operation("op_id", data["review_op_id"]), binding, data["fingerprint"])
                audit = {"review_op_id": review.op_id, "actor": actor, "at_ms": now_ms(),
                         "binding_version": binding.binding_version, "original_binding_version": original.binding_version,
                         "reason": data["reason"],
                         "outcome": "unknown", "action": "acknowledge_risk_only"}
                self.db.execute("UPDATE operations SET unknown_review_state='acknowledged',review_json=?,updated_at_ms=? "
                                "WHERE op_id=?", (json.dumps(audit), now_ms(), original.op_id))
                self.db.execute("UPDATE operations SET status='accepted',result_json=?,updated_at_ms=? WHERE op_id=?",
                                (json.dumps({"reviewed_op_id": original.op_id, "outcome": "unknown",
                                             "review_state": "acknowledged"}), now_ms(), review.op_id))
                return self._operation("op_id", review.op_id)
        return await self._call(acknowledge)

    async def result(self, op: Operation, result: dict):
        return await self.transition(op.op_id, "accepted", "accepted", result_json=json.dumps(result))
