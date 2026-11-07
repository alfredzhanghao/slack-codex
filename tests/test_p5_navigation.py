"""Main-chat discovery and observable, single-attempt navigation failures."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from slack_codex_bridge.desktop import Desktop, RPC, MAIN_THREAD_SOURCES, THREAD_SOURCES
from slack_codex_bridge.model import BridgeError, new_id
from test_desktop import rpc_server
from test_p2_approval import interaction
from test_slack import adapter, drain


async def test_main_picker_filters_sources_without_hiding_similar_user_titles(cfg):
    d = Desktop(cfg, "ws://127.0.0.1:1")
    calls = []
    sources = [{"subAgent": {"other": "guardian"}}, {"subAgent": "review"},
               {"subAgent": "compact"}, {"subAgent": {"thread_spawn": {}}},
               "subAgent", "subAgentReview", "subAgentCompact", "subAgentOther", "unexpected"]

    async def call(method, params):
        assert params['sourceKinds'] == MAIN_THREAD_SOURCES
        calls.append(params['cursor'])
        if params['cursor'] is None:
            return {'data': [{'id': str(i), 'name': 'Phase5', 'source': source, 'cwd': str(cfg.projects[0])}
                             for i, source in enumerate(sources)], 'nextCursor': 'next'}
        row = {'id': 'main', 'name': 'Guardian review', 'source': 'vscode', 'cwd': str(cfg.projects[0])}
        return {'data': [row, row], 'nextCursor': None}

    d.rpc = SimpleNamespace(call=call)
    rows = await d.list_threads(str(cfg.projects[0]))
    assert [(r.id, r.title) for r in rows] == [('main', 'Guardian review')]
    assert calls == [None, 'next']


async def test_internal_session_metadata_lookup_keeps_full_source_scope(cfg):
    d = Desktop(cfg, "ws://127.0.0.1:1")

    async def call(method, params):
        if method == 'thread/read':
            raise BridgeError('backend_thread_not_loaded')
        assert params['sourceKinds'] == THREAD_SOURCES
        return {'data': [{'id': 'internal', 'source': {'subAgent': 'review'}, 'cwd': str(cfg.projects[0])}]}

    d.rpc = SimpleNamespace(call=call)
    assert (await d.get_thread('internal')).id == 'internal'


@pytest.mark.parametrize('method,code,detail,expected', [
    ('thread/resume', -32600, 'thread t already has an active writer', 'backend_thread_active_writer'),
    ('thread/read', -32600, 'thread t already has an active writer', 'backend_rejected'),
    ('thread/resume', -32000, 'thread t already has an active writer', 'backend_rejected'),
    ('thread/resume', -32600, 'other native error with confidential text', 'backend_rejected'),
])
async def test_rpc_writer_conflict_is_classified_only_for_native_resume(method, code, detail, expected):
    async def handler(ws, message):
        await ws.send_json({'id': message['id'], 'error': {'code': code, 'message': detail}})

    async with rpc_server(handler) as endpoint:
        rpc = RPC(endpoint, 1, lambda *_: None)
        await rpc.connect()
        try:
            with pytest.raises(BridgeError) as failed:
                await rpc.call(method, {'threadId': 't'})
            assert failed.value.code == expected and detail not in str(failed.value)
            assert not rpc.pending and not rpc.methods
        finally:
            await rpc.close()


async def test_native_long_history_response_above_eight_megabytes_is_read_without_disconnect():
    payload = 'x' * (8 * 1024 * 1024 + 1024)

    async def handler(ws, message):
        result = {'history': payload} if message['method'] == 'thread/turns/list' else {}
        await ws.send_json({'id': message['id'], 'result': result})

    async with rpc_server(handler) as endpoint:
        rpc = RPC(endpoint, 10, lambda *_: None)
        await rpc.connect()
        try:
            result = await rpc.call('thread/turns/list', {'threadId': 'long-thread'})
            assert result['history'] == payload and rpc.connected
            assert not rpc.pending and not rpc.methods
        finally:
            await rpc.close()


def navigation(slack, state):
    root = state.binding.slack_root_ts

    async def native(method, **kwargs):
        if method == 'conversations.replies':
            return {'messages': [{'ts': root, 'bot_id': slack.bot_id, 'app_id': slack.cfg.app_id}]}
        assert method == 'chat.getPermalink'
        assert kwargs == {'channel': slack.cfg.dm_id, 'message_ts': root}
        return {'channel': slack.cfg.dm_id,
                'permalink': f'https://example.slack.com/archives/{slack.cfg.dm_id}/p{root.replace(".", "")}' }

    slack.web.call = native


async def click(slack, cfg, tid, root='300.000001'):
    from slack_codex_bridge.slack import Intent
    token = slack.buttons.issue(Intent('bind', thread_id=tid, root=root))
    payload = interaction(cfg, token, root)
    payload['actions'][0]['action_id'] = 'bridge_bind'
    request = SimpleNamespace(type='interactive', envelope_id=new_id(), payload=payload)
    await slack.receive(slack.socket, request)
    await drain(slack)
    return request


async def test_active_writer_notice_is_after_ack_without_binding_or_task(setup):
    c, state, backend, store = setup
    c.states.clear()
    calls = []
    slack = adapter(c, backend)
    before = len(await store.bindings())

    async def snapshot(tid):
        assert slack.socket.acks
        calls.append(tid)
        raise BridgeError('backend_thread_active_writer')

    backend.snapshot = snapshot
    request = await click(slack, c.cfg, state.binding.codex_thread_id)
    assert len(calls) == 1 and not backend.sent and len(await store.bindings()) == before
    assert 'active writer' in slack.web.posts[-1][0] and 'Stage: snapshot' in slack.web.posts[-1][0]
    assert slack.ready and slack.stop_error is None
    # Duplicate delivery does not retry snapshot/resume or create another root.
    await slack.receive(slack.socket, request)
    await drain(slack)
    assert len(calls) == 1


async def test_ack_loss_prevents_opening_and_feedback(setup):
    c, state, backend, _ = setup
    slack = adapter(c, backend)
    slack.socket.fail = True
    await click(slack, c.cfg, state.binding.codex_thread_id)
    assert not slack.web.posts and not slack.navigation and not backend.sent


async def test_existing_root_is_verified_then_linked_without_duplicate_root(setup):
    c, state, backend, store = setup
    slack = adapter(c, backend)
    navigation(slack, state)
    await click(slack, c.cfg, state.binding.codex_thread_id)
    assert len(slack.web.posts) == 2 and not backend.sent and len(await store.bindings()) == 1
    assert slack.web.posts[0][1]['root'] == state.binding.slack_root_ts
    button = slack.web.posts[-1][1]['blocks'][-1]['elements'][0]
    assert button['text']['text'] == 'Open thread (web fallback)'
    assert button['url'] == 'https://example.slack.com/archives/D123/p100000001?thread_ts=100.000001&cid=D123'
    assert list(slack.navigation)[-1]['stage'] == 'opened'


@pytest.mark.parametrize('query,valid', [
    ('?thread_ts=100.000001&cid=D123', True),
    ('?cid=D123&thread_ts=100.000001', True),
    ('?thread_ts=200.000001&cid=D123', False),
    ('?thread_ts=100.000001&cid=OTHER', False),
    ('?thread_ts=100.000001&cid=D123&cid=D123', False),
    ('?thread_ts=100.000001&cid=D123&redirect=evil', False),
    ('?thread_ts=&cid=D123', False),
])
async def test_native_thread_permalink_query_is_exact_and_canonical(setup, query, valid):
    c, state, backend, store = setup
    slack = adapter(c, backend)
    navigation(slack, state)
    original = slack.web.call

    async def call(method, **kwargs):
        value = await original(method, **kwargs)
        if method == 'chat.getPermalink':
            value['permalink'] += query
        return value

    slack.web.call = call
    await slack.open_chat(state.binding.codex_thread_id)
    assert not backend.sent and len(await store.bindings()) == 1
    if valid:
        button = slack.web.posts[-1][1]['blocks'][-1]['elements'][0]
        assert button['url'] == 'https://example.slack.com/archives/D123/p100000001?thread_ts=100.000001&cid=D123'
    else:
        assert 'chat_entry_link_unverified' in slack.web.posts[-1][0]


@pytest.mark.parametrize('url', ['https://evil.example/archives/D123/p100000001',
    'https://example.slack.com/archives/OTHER/p100000001',
    'https://example.slack.com/archives/D123/p200000001'])
async def test_unverified_entry_url_is_not_exposed(setup, url):
    c, state, backend, _ = setup
    slack = adapter(c, backend)
    navigation(slack, state)
    original = slack.web.call

    async def call(method, **kwargs):
        if method == 'chat.getPermalink':
            return {'channel': c.cfg.dm_id, 'permalink': url}
        return await original(method, **kwargs)

    slack.web.call = call
    await slack.open_chat(state.binding.codex_thread_id)
    assert 'chat_entry_link_unverified' in slack.web.posts[-1][0]
    assert all('blocks' not in kwargs for _, kwargs in slack.web.posts)


async def test_expired_navigation_feedback_is_scoped_bounded_and_after_ack(setup):
    c, _, backend, _ = setup
    slack = adapter(c, backend)
    payload = interaction(c.cfg, 'expired-synthetic-nonce', '300.000001')
    payload['actions'][0]['action_id'] = 'bridge_bind'
    request = SimpleNamespace(type='interactive', envelope_id='expired', payload=payload)
    await slack.receive(slack.socket, request)
    await drain(slack)
    await slack.receive(slack.socket, request)
    await drain(slack)
    assert len(slack.web.posts) == 1 and 'expired or was already used' in slack.web.posts[0][0]
    assert slack.socket.acks and not backend.sent
    assert 'expired-synthetic-nonce' not in json.dumps(list(slack.navigation))
    payload['user']['id'] = 'OTHER'
    await slack.receive(slack.socket, request)
    await drain(slack)
    assert len(slack.web.posts) == 1


async def test_navigation_diagnostic_survives_unrelated_last_fault_and_is_bounded(setup):
    c, state, backend, _ = setup
    slack = adapter(c, backend)

    async def snapshot(_):
        raise BridgeError('backend_thread_active_writer')

    c.states.clear()
    backend.snapshot = snapshot
    for _ in range(30):
        await slack.open_chat(state.binding.codex_thread_id)
    assert len(slack.navigation) == 50
    slack.last_fault = 'persisted_thread_metadata_unavailable'
    await slack.command(None, '!status', '')
    assert 'backend_thread_active_writer' in slack.web.posts[-1][0]
    assert 'snapshot' in slack.web.posts[-1][0]


async def test_feedback_transport_failure_keeps_native_failure_separate(setup):
    c, state, backend, _ = setup
    c.states.clear()
    slack = adapter(c, backend)
    slack.web.error = BridgeError('slack_write_unconfirmed', unknown=True)

    async def snapshot(_):
        raise BridgeError('backend_thread_active_writer')

    backend.snapshot = snapshot
    await slack.open_chat(state.binding.codex_thread_id)
    assert [(e['stage'], e['code']) for e in list(slack.navigation)[-2:]] == [
        ('snapshot', 'backend_thread_active_writer'), ('feedback', 'slack_write_unconfirmed')]
    assert not backend.sent


async def test_two_fresh_open_requests_create_one_root_and_link_to_it(setup):
    c, _, backend, store = setup
    c.states.clear()
    tid = new_id()
    backend.current.thread = type(backend.current.thread)(tid, 'Main', backend.current.thread.cwd)
    slack = adapter(c, backend)

    async def call(method, **kwargs):
        if method == 'conversations.replies':
            return {'messages': [{'ts': '200.000001', 'bot_id': slack.bot_id, 'app_id': c.cfg.app_id}]}
        assert method == 'chat.getPermalink'
        return {'channel': c.cfg.dm_id, 'permalink': 'https://example.slack.com/archives/D123/p200000001'}

    slack.web.call = call
    await asyncio.gather(slack.open_chat(tid), slack.open_chat(tid))
    roots = [text for text, _ in slack.web.posts if text.startswith('Bound Codex chat')]
    entries = [data for text, data in slack.web.posts if 'Web fallback may open a browser' in text]
    assert len(roots) == 1 and len(entries) == 1
    assert len([b for b in await store.bindings() if b.codex_thread_id == tid]) == 1
    assert all(data['blocks'][-1]['elements'][0]['url'].endswith('thread_ts=200.000001&cid=D123') for data in entries)
    assert not backend.sent


async def test_unknown_root_post_preserves_intent_and_next_open_does_not_repost(setup):
    c, _, backend, store = setup
    c.states.clear()
    tid = new_id()
    backend.current.thread = type(backend.current.thread)(tid, 'Main', backend.current.thread.cwd)
    slack = adapter(c, backend)
    slack.web.error = BridgeError('slack_write_unconfirmed', unknown=True)
    await slack.open_chat(tid)
    rows = [b for b in await store.bindings() if b.codex_thread_id == tid]
    assert len(rows) == 1 and rows[0].slack_root_ts.startswith('pending:')
    assert any(e['stage'] == 'root_post' and e['code'] == 'slack_write_unconfirmed' for e in slack.navigation)
    slack.web.error = None
    await slack.open_chat(tid)
    assert len([text for text, _ in slack.web.posts if text.startswith('Bound Codex chat')]) == 1
    assert 'second root will not be posted' in slack.web.posts[-1][0]
    assert not backend.sent


async def test_connection_lost_during_snapshot_cannot_create_binding(setup):
    c, _, backend, store = setup
    c.states.clear()
    tid = new_id()
    backend.current.thread = type(backend.current.thread)(tid, 'Main', backend.current.thread.cwd)
    slack = adapter(c, backend)
    original = backend.snapshot

    async def snapshot(target):
        value = await original(target)
        slack.lost()
        return value

    backend.snapshot = snapshot
    await slack.open_chat(tid)
    assert not any(b.codex_thread_id == tid for b in await store.bindings())
    assert 'chat_connection_changed' in slack.web.posts[-1][0]
    assert not backend.sent


async def test_deleted_existing_root_has_no_entry_link_or_replacement(setup):
    c, state, backend, store = setup
    slack = adapter(c, backend)
    await slack.open_chat(state.binding.codex_thread_id)
    assert 'Root unavailable' in slack.web.posts[-1][0]
    assert not any('blocks' in data for _, data in slack.web.posts)
    assert len(await store.bindings()) == 1 and not backend.sent


async def test_unexpected_failure_does_not_expose_native_detail(setup):
    c, state, backend, _ = setup
    c.states.clear()
    slack = adapter(c, backend)

    async def snapshot(_):
        raise RuntimeError('sensitive native response fixture')

    backend.snapshot = snapshot
    await slack.open_chat(state.binding.codex_thread_id)
    output = json.dumps(slack.web.posts) + json.dumps(list(slack.navigation))
    assert 'sensitive native response fixture' not in output
    assert 'chat_open_failed' in output
