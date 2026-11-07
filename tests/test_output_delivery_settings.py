"""Output policy/confirmation and shared language preferences; no live Slack writes."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from slack_codex_bridge import preferences
from slack_codex_bridge.artifacts import automatic, capture
from slack_codex_bridge.config import load
from slack_codex_bridge.i18n import translate, blocks
from slack_codex_bridge.inputs import local_file
from slack_codex_bridge.model import BridgeError, new_id, now_ms
from slack_codex_bridge.slack import Web
from test_config_windows import config_file
from test_controller_store import opened
from test_p2_approval import interaction
from test_slack import adapter, drain


@pytest.mark.parametrize('size,auto', [(29_999_999, True), (30_000_000, False), (30_000_001, False)])
def test_default_strict_decimal_mb_boundary(size, auto):
    assert automatic('report.pdf', size, preferences.Preferences()) is auto


@pytest.mark.parametrize('suffix', ['.mp4', '.MOV', '.mkv', '.webm', '.avi', '.wmv', '.m4v', '.mpeg', '.mpg', '.py'])
def test_videos_and_non_document_outputs_never_auto_send(suffix):
    assert not automatic('result' + suffix, 1, preferences.Preferences())


def test_policy_switch_threshold_and_ceiling():
    assert not automatic('small.png', 2, preferences.Preferences(auto_send=False))
    assert not automatic('small.png', 3, preferences.Preferences(max_file_bytes=2))
    assert not automatic('small.png', 2, preferences.Preferences(auto_send_max_bytes=2))
    assert automatic('small.PNG', 1, preferences.Preferences(auto_send_max_bytes=2))


def test_preferences_save_read_and_no_deployment_config_edit(cfg, tmp_path):
    config = config_file(cfg, tmp_path / 'config.json')
    original = config.read_bytes()
    value = preferences.Preferences('zh-CN', False, 20_000_000, 80_000_000)
    preferences.save(cfg, value)
    assert preferences.read(load(config)) == value
    assert config.read_bytes() == original
    assert not list(cfg.data_dir.glob('.bridge-preferences-*.tmp'))


def test_legacy_small_outbound_limit_remains_valid(cfg):
    value = preferences.read(replace(cfg, artifact_max_bytes=10_000_000))
    assert value.max_file_bytes == 10_000_000


@pytest.mark.parametrize('field,value', [('language', 'fr'), ('auto_send', 'yes'), ('auto_send', 1),
    ('max_file_bytes', 100_000_001), ('auto_send_max_bytes', True), ('max_file_bytes', 0)])
def test_invalid_preferences_are_rejected(cfg, field, value):
    with pytest.raises(BridgeError, match='invalid_bridge_preferences'):
        preferences.save(cfg, replace(preferences.Preferences(), **{field: value}))
    assert not preferences.preference_path(cfg).exists()


@pytest.mark.parametrize('text', ['{broken', '{}', '[]', '{"language":"zh-CN","extra":"x"}', 'x' * 4097])
def test_corrupt_preferences_fail_closed(cfg, text):
    cfg.data_dir.mkdir()
    preferences.preference_path(cfg).write_text(text)
    with pytest.raises(BridgeError, match='invalid_bridge_preferences'):
        preferences.read(cfg)


def test_failed_atomic_save_keeps_previous_preferences(cfg, monkeypatch):
    preferences.save(cfg, preferences.Preferences())
    def fail(*_):
        raise OSError
    monkeypatch.setattr(preferences.os, 'replace', fail)
    with pytest.raises(BridgeError, match='save_failed'):
        preferences.save(cfg, preferences.Preferences(language='zh-CN'))
    assert preferences.read(cfg).language == 'en'
    assert not list(cfg.data_dir.glob('.bridge-preferences-*.tmp'))


def test_video_capture_is_output_only_and_preferences_cannot_be_exported(cfg):
    path = cfg.projects[0] / 'video.mp4'
    path.write_bytes(b'synthetic video')
    assert capture(cfg, str(path.parent), path.name).content == path.read_bytes()
    with pytest.raises(BridgeError, match='unsupported_file_type'):
        local_file(cfg, str(path.parent), path.name)
    with pytest.raises(BridgeError, match='credential_file_forbidden'):
        capture(cfg, str(path.parent), 'bridge-preferences.json')


async def output_setup(setup, *, suffix='.pdf', size=7, auto_send=True):
    c, state, backend, store = setup
    preferences.save(c.cfg, preferences.Preferences(auto_send=auto_send))
    path = c.cfg.projects[0] / ('result' + suffix)
    with path.open('wb') as stream:
        stream.truncate(size)
    async def artifacts(thread_id, turn_id=''):
        assert thread_id == state.binding.codex_thread_id
        return [{'path': str(path), 'turn_id': turn_id}]
    backend.artifacts = artifacts
    slack = adapter(c, backend)
    uploads = []
    async def upload(artifact, *, root, guard, checkpoint):
        await guard()
        uploads.append((artifact, root))
        await checkpoint('allocated', 'FFIXTURE')
        return 'FFIXTURE'
    slack.web.upload = upload
    return slack, path, uploads, SimpleNamespace(op_id=new_id())


def confirmation(slack, cfg):
    token = next(t for t, intent in slack.buttons.tokens.items() if intent.kind == 'output_confirm')
    intent = slack.buttons.tokens[token]
    payload = interaction(cfg, token, intent.root)
    payload['actions'][0]['action_id'] = 'bridge_output_confirm'
    return token, payload


@pytest.mark.parametrize('suffix,size,auto_send,automatic_count', [('.pdf', 7, True, 1),
    ('.png', 7, True, 1), ('.mp4', 7, True, 0), ('.pdf', 30_000_000, True, 0),
    ('.txt', 7, False, 0), ('.py', 7, True, 0)])
async def test_output_decision_and_direct_one_click_confirmation(setup, suffix, size, auto_send, automatic_count):
    c, state, backend, store = setup
    slack, path, uploads, parent = await output_setup(setup, suffix=suffix, size=size, auto_send=auto_send)
    assert await slack.deliver_outputs(state, parent, 'exact-turn')
    assert len(uploads) == automatic_count
    if not automatic_count:
        token, payload = confirmation(slack, c.cfg)
        request = SimpleNamespace(type='interactive', envelope_id='confirm', payload=payload)
        await slack.receive(slack.socket, request)
        await drain(slack)
        await slack.receive(slack.socket, request)
        await drain(slack)
        assert token not in slack.buttons.tokens and len(uploads) == 1
    else:
        await slack.deliver_outputs(state, parent, 'exact-turn')
        assert len(uploads) == 1
    assert uploads[0][0].content == path.read_bytes()
    assert uploads[0][1] == state.binding.slack_root_ts and not backend.sent
    assert [op.status for op in await store.uploads(state.binding.binding_id)] == ['accepted']


@pytest.mark.parametrize('change', ['file', 'epoch', 'binding', 'root', 'user', 'action'])
async def test_confirmation_cannot_send_changed_or_crossed_offer(setup, change):
    c, state, _, store = setup
    slack, path, uploads, parent = await output_setup(setup, suffix='.mp4')
    await slack.deliver_outputs(state, parent, 'turn')
    _, payload = confirmation(slack, c.cfg)
    if change == 'file':
        path.write_bytes(b'changed video')
    elif change == 'epoch':
        state.epoch = new_id()
    elif change == 'binding':
        state.binding = replace(state.binding, binding_version=state.binding.binding_version + 1)
    elif change == 'root':
        payload['container']['message_ts'] = '999.000001'
    elif change == 'user':
        payload['user']['id'] = 'UOTHER'
    else:
        payload['actions'][0]['action_id'] = 'bridge_slack'
    await slack.receive(slack.socket, SimpleNamespace(type='interactive', envelope_id='changed', payload=payload))
    await drain(slack)
    assert not uploads
    if change == 'file':
        assert (await store.uploads(state.binding.binding_id))[0].error_reason == 'artifact_changed_since_offer'


async def test_unknown_auto_upload_and_restart_never_replay(setup):
    c, state, backend, store = setup
    await opened(c, state)
    op = await c.execute(state, await c.prepare(state, 'output-task', 'turn_start', 'Produce', state.epoch, now_ms()))
    tid = backend.receipt.turn_id
    backend.current.confirmed.add(tid)
    backend.current.terminals[tid] = 'completed'
    slack, _, uploads, _ = await output_setup(setup)
    original = slack.web.upload
    async def uncertain(*args, **kwargs):
        await original(*args, **kwargs)
        raise BridgeError('slack_upload_bytes_unconfirmed', unknown=True)
    slack.web.upload = uncertain
    await slack.tick(state)
    await slack.tick(state)
    assert len(uploads) == 1
    assert (await store.uploads(state.binding.binding_id))[0].status == 'unknown'
    assert json.loads((await store.operation(op.op_id)).result_json)['files_delivery'] == 'unknown_no_automatic_retry'
    reconnected = adapter(c, backend)
    reconnected.web.upload = uncertain
    await reconnected.tick(state)
    assert len(uploads) == 1


async def test_automatic_policy_rechecked_before_upload(setup, monkeypatch):
    c, state, _, store = setup
    slack, _, uploads, parent = await output_setup(setup)
    original = c.prepare
    async def change(*args, **kwargs):
        result = await original(*args, **kwargs)
        preferences.save(c.cfg, preferences.Preferences(auto_send=False))
        return result
    monkeypatch.setattr(c, 'prepare', change)
    with pytest.raises(BridgeError, match='artifact_output_delivery_incomplete'):
        await slack.deliver_outputs(state, parent, 'turn')
    assert not uploads
    assert (await store.uploads(state.binding.binding_id))[0].error_reason == 'artifact_automatic_policy_changed'


async def test_confirmation_ack_failure_consumes_offer_without_upload(setup):
    c, state, _, store = setup
    slack, _, uploads, parent = await output_setup(setup, suffix='.mp4')
    await slack.deliver_outputs(state, parent, 'turn')
    _, payload = confirmation(slack, c.cfg)
    slack.socket.fail = True
    await slack.receive(slack.socket, SimpleNamespace(type='interactive', envelope_id='lost-ack', payload=payload))
    await drain(slack)
    assert not uploads
    assert (await store.uploads(state.binding.binding_id))[0].error_reason == 'ack_failed_before_send'


async def test_smaller_ceiling_after_confirmation_is_enforced(setup):
    c, state, _, store = setup
    slack, _, uploads, parent = await output_setup(setup, suffix='.mp4')
    await slack.deliver_outputs(state, parent, 'turn')
    _, payload = confirmation(slack, c.cfg)
    preferences.save(c.cfg, preferences.Preferences(max_file_bytes=1))
    await slack.receive(slack.socket, SimpleNamespace(type='interactive', envelope_id='new-limit', payload=payload))
    await drain(slack)
    assert not uploads
    assert (await store.uploads(state.binding.binding_id))[0].error_reason == 'file_too_large'


async def test_corrupt_preferences_disable_automatic_delivery(setup):
    c, state, _, store = setup
    slack, _, uploads, parent = await output_setup(setup)
    preferences.preference_path(c.cfg).write_text('{broken')
    with pytest.raises(BridgeError, match='invalid_bridge_preferences'):
        await slack.deliver_outputs(state, parent, 'turn')
    assert not uploads and not await store.uploads(state.binding.binding_id)


async def test_file_above_ceiling_gets_notice_without_read_or_upload(setup):
    c, state, _, store = setup
    slack, _, uploads, parent = await output_setup(setup, size=8)
    preferences.save(c.cfg, preferences.Preferences(max_file_bytes=7))
    assert await slack.deliver_outputs(state, parent, 'turn')
    assert 'file_too_large' in slack.web.posts[0][0]
    assert not uploads and not await store.uploads(state.binding.binding_id)


def test_translation_preserves_protocol_values_options_and_markdown():
    value = [{'type': 'actions', 'elements': [{'type': 'button', 'action_id': 'send', 'value': 'Send file',
        'text': {'type': 'plain_text', 'text': 'Send file'}}]}, {'type': 'markdown', 'text': 'Send file'},
        {'type': 'static_select', 'options': [{'text': {'type': 'plain_text', 'text': 'Settings'}, 'value': 'v'}]}]
    result = blocks(value, 'zh-CN')
    assert result[0]['elements'][0]['text']['text'] == '发送文件'
    assert result[0]['elements'][0]['value'] == 'Send file'
    assert result[1] == value[1] and result[2] == value[2]
    assert value[0]['elements'][0]['text']['text'] == 'Send file'
    assert translate('model-name / C:/path', 'zh-CN') == 'model-name / C:/path'


async def test_shared_language_changes_without_restarting_and_model_reply_verbatim(cfg):
    web = Web(cfg, 'synthetic')
    calls = []
    async def call(method, **kwargs):
        calls.append(kwargs)
        return {'channel': cfg.dm_id, 'ts': '100.000001'}
    web.call = call
    await web.post('Send file')
    preferences.save(cfg, preferences.Preferences(language='zh-CN'))
    await web.post('Send file')
    await web.post('Send file', blocks=[{'type': 'markdown', 'text': 'Send file'}], verbatim=True)
    assert [item['text'] for item in calls] == ['Send file', '发送文件', 'Send file']
    assert calls[-1]['blocks'][0]['text'] == 'Send file'


async def test_card_language_change_invalidates_render_text_cache(setup):
    c, state, backend, _ = setup
    slack = adapter(c, backend)
    english, _ = slack.card(state)
    preferences.save(c.cfg, preferences.Preferences(language='zh-CN'))
    chinese, card_blocks = slack.card(state)
    assert english != chinese and '剩余上下文' in chinese
    assert '接管输入' in json.dumps(card_blocks, ensure_ascii=False)
    assert state.snapshot.thread.title in chinese
