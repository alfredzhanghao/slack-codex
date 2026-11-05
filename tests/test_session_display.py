import json
from dataclasses import replace

import pytest

from slack_codex_bridge import preferences
from slack_codex_bridge.model import BridgeError, Snapshot, Thread, new_id, now_ms
from slack_codex_bridge.slack import Web
from test_slack import adapter


def recording_web(slack, state):
    cfg = slack.cfg
    web = Web(cfg, 'synthetic')
    calls = []
    async def call(method, **kwargs):
        calls.append((method, kwargs))
        if method == 'conversations.replies':
            return {'messages': [{'ts': state.binding.slack_root_ts, 'bot_id': slack.bot_id, 'app_id': cfg.app_id}]}
        if method == 'chat.getPermalink':
            root = state.binding.slack_root_ts
            seconds, fraction = root.split('.')
            return {'channel': cfg.dm_id, 'permalink': f'https://example.slack.com/archives/{cfg.dm_id}/p{seconds}{fraction.ljust(6,"0")}'}
        return {'channel': cfg.dm_id, 'ts': state.binding.slack_root_ts if method == 'chat.update' else '200.000001'}
    web.call = call
    slack.web = web
    return calls


async def test_enter_existing_root_refreshes_language_and_buttons_without_new_root(setup):
    c, state, backend, store = setup
    slack = adapter(c, backend)
    slack.last_render[state.binding.binding_id] = (0, slack.card(state)[0])
    preferences.save(c.cfg, preferences.Preferences(language='zh-CN'))
    calls = recording_web(slack, state)
    await slack.open_chat(state.binding.codex_thread_id)
    updates = [data for method, data in calls if method == 'chat.update']
    assert len(updates) == 1 and updates[0]['ts'] == state.binding.slack_root_ts
    assert '状态：在线' in updates[0]['text'] and '接管输入' in json.dumps(updates[0]['blocks'], ensure_ascii=False)
    assert calls[-1][0] == 'chat.postMessage' and '会话控件已就绪' in calls[-1][1]['text']
    assert len(await store.bindings()) == 1 and not backend.sent


async def test_unloaded_existing_chat_replaces_old_idle_with_unknown_history_view(setup):
    c, state, backend, _ = setup
    slack = adapter(c, backend)
    preferences.save(c.cfg, preferences.Preferences(language='zh-CN'))
    calls = recording_web(slack, state)
    async def missing(_):
        raise BridgeError('desktop_ipc_open_chat_in_desktop')
    backend.snapshot = missing
    await slack.chat_entry(state)
    update = next(data for method, data in calls if method == 'chat.update')
    assert '同步中/离线' in update['text'] and '状态未知' in update['text']
    assert '空闲' not in update['text'] and '在线' not in update['text']
    assert '历史记录' in calls[-1][1]['text'] and not backend.sent


@pytest.mark.parametrize('change', ['stale', 'untrusted', 'syncing'])
async def test_stale_or_suspended_cards_do_not_claim_online_idle(setup, change):
    c, state, backend, _ = setup
    slack = adapter(c, backend)
    if change == 'stale':
        state.snapshot = replace(state.snapshot, observed_ms=now_ms() - 6000)
    elif change == 'untrusted':
        state.snapshot = replace(state.snapshot, trusted=False)
    else:
        state.syncing = True
    text, _ = slack.card(state)
    assert 'syncing/offline | unknown' in text and 'online | idle' not in text


async def test_native_active_without_turn_id_is_working_without_a_stop_target(setup):
    c, state, backend, _ = setup
    slack = adapter(c, backend)
    state.snapshot = replace(state.snapshot, idle=False, active_turn=None)
    text, blocks = slack.card(state)
    assert 'online | working' in text
    assert not any(element.get('action_id') == 'bridge_interrupt' for element in blocks[-1]['elements'])


async def test_ipc_public_progress_is_literal_and_cleared_when_idle(setup):
    c, state, backend, _ = setup
    slack = adapter(c, backend)
    preferences.save(c.cfg, preferences.Preferences(language='zh-CN'))
    source = '检查文档\nSend file\nStatus: online | idle'
    state.snapshot = replace(state.snapshot, idle=False, active_turn='active-turn', progress=source)
    text, _ = slack.card(state)
    assert '工作中' in text and '进展：' in text and source in text
    calls = recording_web(slack, state)
    await slack.render(state, force=True)
    update = calls[-1][1]
    assert source in update['text'] and source in json.dumps(update['blocks'], ensure_ascii=False).replace('\\n','\n')
    state.snapshot = replace(state.snapshot, idle=True, active_turn=None)
    state.active_turn = None
    text, _ = slack.card(state)
    assert '进展：' not in text and source not in text


async def test_unavailable_chat_updates_unknown_without_blocking_loaded_chat(setup):
    c, old, backend, store = setup
    binding = replace(old.binding, binding_id=new_id(), codex_thread_id=new_id(), slack_root_ts='100.000002')
    async def snapshot(tid):
        if tid == old.binding.codex_thread_id:
            raise BridgeError('desktop_ipc_open_chat_in_desktop')
        assert tid == binding.codex_thread_id
        return Snapshot(Thread(tid, 'Working chat', binding.project_path), trusted=True,
                        active_turn='current-turn', progress='Checking the document', generation=1)
    backend.snapshot = snapshot
    working = await c.bind(binding)
    slack = adapter(c, backend)
    slack.schedule_tick(old)
    slack.schedule_tick(working)
    from test_slack import drain
    await drain(slack)
    cards = {root: text for root, text, _ in slack.web.updates}
    assert 'syncing/offline | unknown' in cards[old.binding.slack_root_ts]
    assert 'online | working' in cards[working.binding.slack_root_ts]
    assert 'Checking the document' in cards[working.binding.slack_root_ts]
    assert old.snapshot is None and old.syncing and c.healthy and c.available and slack.ready
    assert not await store.accepted(binding.codex_thread_id) and not backend.sent


async def test_protected_terminal_is_confirming_until_original_corroboration(setup):
    c, state, backend, _ = setup
    state.active_turn = 'protected-turn'
    slack = adapter(c, backend)
    assert 'online | confirming completion' in slack.card(state)[0]
    assert state.active_turn == 'protected-turn'
    assert 'Progress:' not in slack.card(state)[0]


async def test_entry_language_change_does_not_rebind_or_take_input_control(setup):
    c, state, backend, store = setup
    await c.transfer(state, 'slack', state.epoch)
    epoch = state.epoch
    slack = adapter(c, backend)
    calls = recording_web(slack, state)
    for language in ('zh-CN', 'en'):
        preferences.save(c.cfg, preferences.Preferences(language=language))
        await slack.chat_entry(state)
        update = next(data for method, data in reversed(calls) if method == 'chat.update')
        assert ('状态：在线' in update['text']) == (language == 'zh-CN')
    assert state.mode == 'slack' and state.epoch == epoch
    assert len(await store.bindings()) == 1 and not backend.sent
