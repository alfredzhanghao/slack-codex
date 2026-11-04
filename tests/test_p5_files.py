import json
import asyncio
from types import SimpleNamespace

import pytest

from slack_codex_bridge.artifacts import linked_files
from slack_codex_bridge.desktop import Desktop
from slack_codex_bridge.model import BridgeError, now_ms
from slack_codex_bridge.recovery import replacement
from test_controller_store import opened
from test_p2_approval import interaction
from test_p3_artifacts import enable_upload
from test_slack import adapter, drain


def test_output_references_are_explicit_local_links_not_guessed_paths():
    text = '[one](D:/project/one.docx) [two](<D:/project/two (v1).pdf>) '
    text += '[remote](https://example.com/report.pdf) [anchor](#chapter) [mail](mailto:user@example.com)'
    assert linked_files(text) == ['D:/project/one.docx', 'D:/project/two (v1).pdf']


@pytest.mark.parametrize('text', [
    '<D:/project/result.txt|TXT>',
    '&lt;D:/project/result.txt|TXT&gt;',
    '[TXT](D:/project/result.txt)',
])
def test_slack_and_markdown_file_links_share_discovery_without_rewriting(text):
    original = text
    assert linked_files(text) == ['D:/project/result.txt']
    assert text == original
    assert linked_files('&lt;https://example.com/private|Web&gt;') == []


async def test_native_final_links_are_discovered_for_the_exact_completed_turn(cfg):
    d = Desktop(cfg, 'ws://127.0.0.1:1')

    async def call(method, params):
        if method == 'thread/read':
            return {'thread': {'id': 'chat', 'cwd': str(cfg.projects[0])}}
        return {'data': [
            {'id': 'later', 'items': [{'type': 'agentMessage', 'phase': 'final_answer', 'text': '[bad](later.txt)'}]},
            {'id': 'target', 'status': 'completed', 'items': [{'id': 'final', 'type': 'agentMessage',
                'phase': 'final_answer', 'text': '[report](report.docx) [source](https://example.com)'}]}]}

    d.rpc = SimpleNamespace(call=call)
    rows = await d.artifacts('chat', 'target')
    assert rows == [{'path': 'report.docx', 'turn_id': 'target', 'item_id': 'final'}]


async def selection_form(c, state, backend, slack):
    paths = [c.cfg.projects[0] / name for name in ('report.txt', 'chart.png')]
    for path in paths:
        path.write_bytes(b'output fixture')

    async def rows(thread_id, turn_id=''):
        return [{'path': str(path), 'turn_id': turn_id or 'turn'} for path in paths]

    backend.artifacts = rows
    views = []

    async def call(method, **kwargs):
        views.append(kwargs['view'])
        return {'view': {'id': 'VFIXTURE', 'hash': 'fixture-hash'}}

    slack.web.call = call
    slack.settings_controls(state)
    token = next(t for t, intent in slack.buttons.tokens.items() if intent.kind == 'artifacts')
    _, _, opening = await slack.prepare_interaction(interaction(c.cfg, token, state.binding.slack_root_ts))
    await opening
    view = views[-1]
    field = next(block for block in view['blocks'] if block.get('block_id') == 'recent')['element']
    assert field['type'] == 'multi_static_select'
    payload = {'type': 'view_submission', 'team': {'id': c.cfg.team_id}, 'user': {'id': c.cfg.user_id},
        'api_app_id': c.cfg.app_id, 'view': {'callback_id': 'bridge_artifact',
        'private_metadata': view['private_metadata'], 'state': {'values': {'recent': {'choice': {
            'selected_options': field['options']}}}}}}
    return paths, payload, views


async def test_multi_select_sends_files_once_with_individual_receipts_after_ack(setup):
    c, state, backend, store = setup
    slack = adapter(c, backend)
    sent = enable_upload(slack)
    _, payload, _ = await selection_form(c, state, backend, slack)
    request = SimpleNamespace(type='interactive', envelope_id='batch', payload=payload)
    await slack.receive(slack.socket, request)
    await drain(slack)
    assert len(sent) == 2 and not backend.sent
    batch = await store.by_source('button:' + payload['view']['private_metadata'])
    result = json.loads(batch.result_json)
    assert batch.status == 'accepted' and result['requested'] == 2
    assert [file['status'] for file in result['files']] == ['accepted', 'accepted']
    assert len({file['op_id'] for file in result['files']}) == 2
    await slack.receive(slack.socket, request)
    await drain(slack)
    assert len(sent) == 2


async def test_batch_ack_failure_prevents_every_capture_and_upload(setup, monkeypatch):
    c, state, backend, store = setup
    slack = adapter(c, backend)
    sent = enable_upload(slack)
    _, payload, _ = await selection_form(c, state, backend, slack)
    monkeypatch.setattr('slack_codex_bridge.slack.capture', lambda *args: pytest.fail('capture before ACK'))
    slack.socket.fail = True
    await slack.receive(slack.socket, SimpleNamespace(type='interactive', envelope_id='lost-ack', payload=payload))
    assert not sent
    batch = await store.by_source('button:' + payload['view']['private_metadata'])
    assert batch.status == 'rejected'


async def test_mixed_choices_from_two_forms_reject_before_any_file_operation(setup):
    c, state, backend, store = setup
    slack = adapter(c, backend)
    _, first, _ = await selection_form(c, state, backend, slack)
    _, second, _ = await selection_form(c, state, backend, slack)
    second['view']['state']['values']['recent']['choice']['selected_options'][1] = (
        first['view']['state']['values']['recent']['choice']['selected_options'][1])
    assert await slack.prepare_interaction(second) == (None, None, None)
    assert not await store.uploads(state.binding.binding_id)


async def test_batch_storage_failure_stops_before_upload(setup, monkeypatch):
    c, state, backend, store = setup
    slack = adapter(c, backend)
    sent = enable_upload(slack)
    _, payload, _ = await selection_form(c, state, backend, slack)
    original = store.transition

    async def fail(op_id, expected, status, **fields):
        if expected == 'received' and status == 'sending':
            raise BridgeError('database_unavailable')
        return await original(op_id, expected, status, **fields)

    monkeypatch.setattr(store, 'transition', fail)
    await slack.receive(slack.socket, SimpleNamespace(type='interactive', envelope_id='storage', payload=payload))
    await drain(slack)
    assert not sent and not c.healthy and not state.reservations


async def test_batch_known_file_failure_does_not_replay_or_prevent_other_files(setup):
    c, state, backend, store = setup
    slack = adapter(c, backend)
    sent = enable_upload(slack)
    paths, payload, _ = await selection_form(c, state, backend, slack)
    paths[0].unlink()
    await slack.receive(slack.socket, SimpleNamespace(type='interactive', envelope_id='missing-file', payload=payload))
    await drain(slack)
    batch = await store.by_source('button:' + payload['view']['private_metadata'])
    assert batch.status == 'rejected' and len(sent) == 1
    assert [f['status'] for f in json.loads(batch.result_json)['files']] == ['rejected', 'accepted']


async def test_unknown_batch_is_not_replaceable_as_a_whole(setup):
    c, state, backend, store = setup
    slack = adapter(c, backend)
    sent = enable_upload(slack, error=BridgeError('slack_write_unconfirmed', unknown=True))
    _, payload, _ = await selection_form(c, state, backend, slack)
    request = SimpleNamespace(type='interactive', envelope_id='unknown-files', payload=payload)
    await slack.receive(slack.socket, request)
    await drain(slack)
    batch = await store.by_source('button:' + payload['view']['private_metadata'])
    assert batch.status == 'unknown' and len(sent) == 2
    with pytest.raises(BridgeError, match='do_not_replay_batch'):
        replacement(batch)
    await slack.receive(slack.socket, request)
    await drain(slack)
    assert len(sent) == 2


async def test_interrupted_batch_retains_sent_and_unknown_file_receipts_without_replay(setup):
    c, state, backend, store = setup
    slack = adapter(c, backend)
    _, payload, _ = await selection_form(c, state, backend, slack)
    entered = asyncio.Event()
    shared = []
    attempts = []

    async def upload(artifact, *, root, guard, checkpoint):
        await guard()
        attempts.append(str(artifact.path))
        await checkpoint('allocated', 'FFIXTURE')
        if len(attempts) == 2:
            entered.set()
            await asyncio.Event().wait()
        shared.append(str(artifact.path))
        return 'FFIXTURE'

    slack.web.upload = upload
    request = SimpleNamespace(type='interactive', envelope_id='cancelled-batch', payload=payload)
    await slack.receive(slack.socket, request)
    await asyncio.wait_for(entered.wait(), 5)
    for task in list(slack.work):
        task.cancel()
    await drain(slack)
    await store.recover()
    batch = await store.by_source('button:' + payload['view']['private_metadata'])
    assert batch.status == 'unknown' and len(shared) == 1 and not state.reservations
    assert sorted(op.status for op in await store.uploads(state.binding.binding_id)) == ['accepted', 'unknown', 'unknown']
    await slack.receive(slack.socket, request)
    await drain(slack)
    assert len(attempts) == 2 and len(shared) == 1


async def test_completion_lists_outputs_once_and_file_card_opens_exact_turn(setup):
    c, state, backend, store = setup
    await opened(c, state)
    op = await c.execute(state, await c.prepare(state, 'task', 'turn_start', 'Produce files', state.epoch, now_ms()))
    backend.current.terminals[backend.receipt.turn_id] = 'completed'
    backend.current.confirmed.add(backend.receipt.turn_id)
    slack = adapter(c, backend)
    _, _, views = await selection_form(c, state, backend, slack)
    await slack.tick(state)
    result = json.loads((await store.operation(op.op_id)).result_json)
    assert result['delivery'] == 'confirmed' and result['files_delivery'] == 'confirmed'
    output = next((text, kwargs) for text, kwargs in slack.web.posts if text.startswith('Output files referenced'))
    token = output[1]['blocks'][-1]['elements'][0]['value']
    intent = slack.buttons.tokens[token]
    assert intent.turn_id == backend.receipt.turn_id
    _, _, opening = await slack.prepare_interaction(interaction(c.cfg, token, '200.000001'))
    await opening
    assert views[-1]['callback_id'] == 'bridge_artifact'
    before = len(slack.web.posts)
    await slack.tick(state)
    assert len(slack.web.posts) == before and not any(i.kind == 'artifacts_open' for i in slack.buttons.tokens.values())


async def test_artifacts_command_refreshes_selectable_list_without_running_task(setup):
    c, state, backend, _ = setup
    slack = adapter(c, backend)
    paths, _, _ = await selection_form(c, state, backend, slack)
    await slack.command(state, '!artifacts', state.epoch)
    text, kwargs = slack.web.posts[-1]
    assert all(str(path) in text for path in paths)
    assert kwargs['blocks'][-1]['elements'][0]['text']['text'] == 'Get output files'
    assert not backend.sent
