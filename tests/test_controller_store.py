import asyncio
import json
import sqlite3
import threading
from dataclasses import replace

import pytest

from slack_codex_bridge.controller import Controller
from slack_codex_bridge.model import BridgeError, Operation, Receipt, new_id, now_ms


async def opened(controller, state):
    assert await controller.transfer(state, "slack", state.epoch)
    return state.epoch


async def prepare(controller, state, key="message:1", kind="turn_start", target=None):
    return await controller.prepare(state, key, kind, "Hello", state.epoch, now_ms(), target)


async def test_dedup_and_acceptance_are_not_completion(setup):
    c, s, backend, store = setup
    await opened(c, s)
    first = await prepare(c, s)
    duplicate = await prepare(c, s)
    assert first.execute and not duplicate.execute
    op = await c.execute(s, first)
    assert op.status == "accepted" and op.result_json is None
    assert len(backend.sent) == 1
    assert not (await prepare(c, s)).execute
    assert not await c.completed(s)
    assert (await store.operation(op.op_id)).source_key == "message:1"


async def test_single_submission_slot_concurrent_inputs(setup):
    c, s, backend, _ = setup
    await opened(c, s)
    values = await asyncio.gather(*(prepare(c, s, f"message:{n}") for n in range(20)))
    assert sum(x.execute for x in values) == 1
    assert sum(x.operation.status == "rejected" for x in values) == 19
    await asyncio.gather(*(c.execute(s, x) for x in values))
    assert len(backend.sent) == 1


async def test_busy_rejection_never_replays_after_idle_reconnect_restart(setup):
    c, s, backend, store = setup
    await opened(c, s)
    backend.current.idle = False
    backend.current.active_turn = new_id()
    await c.refresh(s)
    rejected = await prepare(c, s)
    assert rejected.operation.status == "rejected"
    backend.current.idle = True
    backend.current.active_turn = None
    c.invalidate()
    await c.synchronized()
    restarted = Controller(c.cfg, backend, store)
    await restarted.load()
    await restarted.synchronized()
    rs = restarted.states[s.binding.binding_id]
    # Business dedup precedes both the current time check and input authority.
    duplicate = await restarted.prepare(rs, "message:1", "turn_start", "Hello", "old", 0)
    assert duplicate.operation.op_id == rejected.operation.op_id and not duplicate.execute
    assert not backend.sent


@pytest.mark.parametrize("kind", ["steer", "interrupt"])
async def test_gui_started_turn_control_without_takeover_and_old_target_guard(setup, kind):
    c, s, backend, _ = setup
    target = new_id()
    backend.current = replace(backend.current, idle=False, active_turn=target)
    backend.receipt = Receipt(target)
    await c.refresh(s)
    assert s.mode == "desktop"
    wrong = await prepare(c, s, "wrong", kind, new_id())
    assert wrong.operation.status == "rejected"
    exact = await prepare(c, s, "exact", kind, target)
    assert exact.execute
    assert (await c.execute(s, exact)).status == "accepted"
    assert backend.sent[0].turn_id == target


async def test_reconnect_old_epoch_and_offline_messages(setup):
    c, s, backend, _ = setup
    epoch = await opened(c, s)
    pending = await prepare(c, s)
    c.invalidate()
    await c.synchronized()
    assert s.mode == "desktop" and s.epoch != epoch
    assert (await c.execute(s, pending)).status == "rejected"
    await opened(c, s)
    old = await c.prepare(s, "old", "turn_start", "Hi", s.epoch, 0)
    assert not old.execute and old.operation.status == "rejected"
    assert not backend.sent


async def test_unknown_blocks_dependent_writes_but_not_exact_interrupt(setup):
    c, s, backend, store = setup
    await opened(c, s)
    backend.send_error = BridgeError("backend_outcome_unknown", unknown=True)
    failed = await c.execute(s, await prepare(c, s))
    assert failed.status == "unknown" and failed.unknown_review_state == "pending"
    target = new_id()
    backend.current = replace(backend.current, idle=False, active_turn=target)
    await c.refresh(s)
    assert not (await prepare(c, s, "steer", "steer", target)).execute
    backend.send_error = None
    backend.receipt = Receipt(target)
    exact = await prepare(c, s, "interrupt", "interrupt", target)
    assert exact.execute
    await c.execute(s, exact)
    assert (await c.operation(s, failed.op_id)).status == "unknown"
    assert await store.blockers(s.binding.codex_thread_id)


@pytest.mark.parametrize("after_send", [False, True])
async def test_cancellation_never_replays(setup, after_send):
    c, s, backend, store = setup
    await opened(c, s)
    pending = await prepare(c, s)
    if after_send:
        backend.release = asyncio.Event()
    else:
        original = backend.snapshot

        async def waiting(tid):
            backend.entered.set()
            await asyncio.Event().wait()
            return await original(tid)
        backend.snapshot = waiting
    task = asyncio.create_task(c.execute(s, pending))
    await backend.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    op = await store.operation(pending.operation.op_id)
    assert op.status == ("unknown" if after_send else "rejected")
    assert len(backend.sent) == int(after_send)
    assert not (await prepare(c, s)).execute


async def test_database_failure_before_and_after_send(setup, monkeypatch):
    c, s, backend, store = setup
    await opened(c, s)
    original = store.transition
    pending = await prepare(c, s)

    async def fail(*args, **kwargs):
        raise BridgeError("database_unavailable")
    monkeypatch.setattr(store, "transition", fail)
    op = await c.execute(s, pending)
    assert not backend.sent and not c.healthy and op.status == "received"
    monkeypatch.setattr(store, "transition", original)
    await store.recover()
    assert (await store.operation(op.op_id)).status == "rejected"


async def test_receipt_retained_on_single_commit_failure_can_resolve_unknown(setup, monkeypatch):
    c, s, backend, store = setup
    await opened(c, s)
    original = store.transition

    async def once(opid, expected, status, **fields):
        if expected == "sending" and status == "accepted":
            raise BridgeError("database_unavailable")
        return await original(opid, expected, status, **fields)
    monkeypatch.setattr(store, "transition", once)
    op = await c.execute(s, await prepare(c, s))
    assert op.status == "unknown" and not c.healthy and len(backend.sent) == 1
    monkeypatch.setattr(store, "transition", original)
    assert (await c.operation(s, op.op_id)).status == "accepted"
    assert s.active_turn == backend.receipt.turn_id
    # Evidence query does not restore input authority or database-health latch.
    assert s.mode != "slack" and not c.healthy


async def test_semantic_merge_is_accepted_and_blocked(setup):
    c, s, backend, _ = setup
    backend.current.known_turns.add(backend.receipt.turn_id)
    await opened(c, s)
    op = await c.execute(s, await prepare(c, s))
    assert op.status == "accepted" and op.error_reason == "semantic_mismatch"
    await c.refresh(s)
    assert s.blocked and not await c.transfer(s, "slack", s.epoch)


async def test_single_terminal_snapshot_does_not_release_activity(setup):
    c, s, backend, _ = setup
    await opened(c, s)
    op = await c.execute(s, await prepare(c, s))
    tid = backend.receipt.turn_id
    backend.current.terminals[tid] = "interrupted"
    await c.refresh(s)
    assert s.active_turn == tid and not (await prepare(c, s, "second")).execute
    # A late event for another turn is also insufficient.
    backend.current.confirmed = {new_id()}
    await c.refresh(s)
    assert s.active_turn == tid
    backend.current.terminals[tid] = "completed"
    backend.current.confirmed = {tid}
    await c.refresh(s)
    assert s.active_turn is None
    completed = await c.completed(s)
    assert completed[0].op_id == op.op_id
    assert not await c.completed(s)


async def test_restart_restores_protection_not_input_or_queue(setup):
    c, s, backend, store = setup
    await opened(c, s)
    accepted = await c.execute(s, await prepare(c, s))
    restarted = Controller(c.cfg, backend, store)
    await restarted.load()
    await restarted.synchronized()
    rs = restarted.states[s.binding.binding_id]
    assert rs.mode == "desktop" and rs.active_turn == backend.receipt.turn_id
    assert not await restarted.transfer(rs, "slack", rs.epoch)
    assert (await store.operation(accepted.op_id)).status == "accepted"
    assert len(backend.sent) == 1


async def test_store_worker_thread_and_crash_recovery(setup):
    _, s, _, store = setup
    main_thread = threading.get_ident()
    assert await store._call(threading.get_ident) != main_thread
    names = await store._call(lambda: [r[0] for r in store.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")])
    assert set(names) == {"bindings", "operations", "input_messages"}
    for status in ("received", "sending", "rejected", "accepted", "unknown"):
        op = Operation(new_id(), status, s.binding.binding_id, 1, "turn_start", s.binding.codex_thread_id,
                       status=status)
        await store.record_once(op)
    await store.recover()
    assert (await store.by_source("received")).status == "rejected"
    assert (await store.by_source("sending")).status == "unknown"
    assert (await store.by_source("accepted")).status == "accepted"
    assert len(await store.blockers(s.binding.codex_thread_id)) == 2
    # Enforce uniqueness and CAS, including under concurrent duplicates.
    existing = await store.by_source("accepted")
    values = await asyncio.gather(*(store.record_once(replace(existing, op_id=new_id())) for _ in range(10)))
    assert all(not fresh for _, fresh in values)
    with pytest.raises(BridgeError, match="operation_state_changed"):
        await store.transition(existing.op_id, "sending", "accepted")


async def test_failed_receive_transaction_has_no_backend_side_effect(setup, monkeypatch):
    c, s, backend, store = setup
    await opened(c, s)

    def disk_failure(*_):
        raise sqlite3.OperationalError("synthetic failure")
    monkeypatch.setattr(store, "_record_once", disk_failure)
    with pytest.raises(BridgeError, match="database_unavailable"):
        await prepare(c, s)
    assert not c.healthy and not backend.sent


async def test_unknown_query_requires_exact_receipt_and_binding(setup):
    c, s, backend, store = setup
    op = Operation(new_id(), "unknown", s.binding.binding_id, 1, "turn_start", s.binding.codex_thread_id,
                   status="unknown", backend_ref_json=json.dumps({"turn_id": new_id()}))
    await store.record_once(op)
    assert (await c.operation(s, op.op_id)).status == "unknown"
    with pytest.raises(BridgeError, match="operation_not_found"):
        await c.operation(s, new_id())
    assert not backend.sent
