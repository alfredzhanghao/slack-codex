"""Read-only navigation can repeat; business submissions remain one-use."""

import asyncio
from types import SimpleNamespace

import pytest

from slack_codex_bridge.slack import Intent
from test_p2_approval import interaction
from test_p5_navigation import navigation
from test_slack import adapter, drain


@pytest.mark.parametrize('command,query', [
    ('!task', ''), ('!tasks', ''), ('!task exact title', 'exact title'),
    ('!tasks exact title', 'exact title')])
async def test_task_alias_keeps_selected_project_and_filter(setup, command, query):
    controller, _, backend, _ = setup
    slack = adapter(controller, backend)
    slack.selected_project = str(controller.cfg.projects[0])
    calls = []
    async def tasks(text='', project='', page=0):
        calls.append((text, project, page))
    slack.tasks = tasks
    await slack.command(None, command, '')
    assert calls == [(query, slack.selected_project, 0)] and not backend.sent


async def test_task_alias_without_project_opens_project_picker(setup):
    controller, _, backend, _ = setup
    slack = adapter(controller, backend)
    await slack.command(None, '!task', '')
    assert 'Choose a project' in slack.web.posts[-1][0] and not backend.sent


async def test_repeated_open_reuses_binding_and_root_without_model_task(setup):
    controller, state, backend, store = setup
    slack = adapter(controller, backend)
    navigation(slack, state)
    original = state.binding
    before = len(await store.bindings())
    token = slack.buttons.issue(Intent('bind', thread_id=original.codex_thread_id, root='300.000001'))
    payload = interaction(controller.cfg, token, '300.000001')
    payload['actions'][0]['action_id'] = 'bridge_bind'
    for _ in range(2):
        _, prepared, after = await slack.prepare_interaction(payload)
        assert prepared is None and after is not None
        await after
    assert state.binding.binding_id == original.binding_id
    assert state.binding.slack_root_ts == original.slack_root_ts
    assert len(await store.bindings()) == before and not backend.sent
    assert sum(row['stage'] == 'opened' for row in slack.navigation) == 2
    assert len(slack.web.posts)==2


async def test_navigation_notice_is_durable_across_adapter_recreation(setup):
    controller,state,backend,_=setup
    first=adapter(controller,backend)
    navigation(first,state)
    await first.chat_entry(state)
    second=adapter(controller,backend)
    navigation(second,state)
    await second.chat_entry(state)
    assert len(first.web.posts)==2 and not second.web.posts and not backend.sent
    assert not any(e['action_id']=='bridge_entry' for b in second.card(state)[1]
                   if b['type']=='actions' for e in b['elements'])


async def test_uncertain_navigation_notice_is_not_reposted(setup):
    from slack_codex_bridge.model import BridgeError
    controller,state,backend,_=setup
    slack=adapter(controller,backend)
    navigation(slack,state)
    attempted=[]
    async def post(*args,**kwargs):
        attempted.append((args,kwargs))
        raise BridgeError('slack_write_unconfirmed',unknown=True)
    slack.web.post=post
    with pytest.raises(BridgeError):
        await slack.chat_entry(state)
    await slack.chat_entry(state)
    assert len(attempted)==1 and not backend.sent


async def test_new_click_reopens_but_redelivery_of_same_click_only_acks(setup):
    controller, state, backend, _ = setup
    slack = adapter(controller, backend)
    navigation(slack, state)
    token = slack.buttons.issue(Intent('bind', thread_id=state.binding.codex_thread_id, root='300.000001'))
    payload = interaction(controller.cfg, token, '300.000001')
    payload['actions'][0].update(action_id='bridge_bind', action_ts='600.000001')
    async def deliver(envelope):
        await slack.receive(slack.socket, SimpleNamespace(type='interactive', envelope_id=envelope, payload=payload))
        await drain(slack)
    await deliver('first-envelope')
    await deliver('retry-envelope')
    assert sum(row['stage'] == 'opened' for row in slack.navigation) == 1
    assert len(slack.socket.acks) == 2
    payload['actions'][0]['action_ts'] = '601.000001'
    await deliver('second-click')
    assert sum(row['stage'] == 'opened' for row in slack.navigation) == 2
    assert not any('selection expired' in text for text, _ in slack.web.posts) and not backend.sent


@pytest.mark.parametrize('change', ['root', 'action', 'business_action'])
async def test_wrong_navigation_context_cannot_consume_or_retarget_valid_button(setup, change):
    controller, state, backend, _ = setup
    slack = adapter(controller, backend)
    navigation(slack, state)
    token = slack.buttons.issue(Intent('bind', thread_id=state.binding.codex_thread_id, root='300.000001'))
    payload = interaction(controller.cfg, token, '300.000001')
    payload['actions'][0]['action_id'] = 'bridge_bind'
    if change == 'root':
        payload['container']['message_ts'] = '400.000001'
    elif change == 'action':
        payload['actions'][0]['action_id'] = 'bridge_projects'
    else:
        payload['actions'][0]['action_id'] = 'bridge_interrupt'
    _, prepared, after = await slack.prepare_interaction(payload)
    assert prepared is None
    if after:
        await after
    assert not any(row['stage'] == 'opened' for row in slack.navigation)
    payload['container']['message_ts'] = '300.000001'
    payload['actions'][0]['action_id'] = 'bridge_bind'
    _, prepared, after = await slack.prepare_interaction(payload)
    assert prepared is None and after is not None
    await after
    assert any(row['stage'] == 'opened' for row in slack.navigation) and not backend.sent


async def test_reuse_does_not_extend_expiry_or_survive_reconnect(setup):
    controller, state, backend, _ = setup
    slack = adapter(controller, backend)
    intent = Intent('bind', thread_id=state.binding.codex_thread_id, root='300.000001')
    token = slack.buttons.issue(intent)
    expires = intent.expires
    payload = interaction(controller.cfg, token, intent.root)
    payload['actions'][0]['action_id'] = 'bridge_bind'
    _, _, after = await slack.prepare_interaction(payload)
    after.close()  # This test inspects the navigation capability without executing it.
    assert intent.expires == expires
    intent.expires -= 601
    _, prepared, after = await slack.prepare_interaction(payload)
    assert prepared is None
    await after
    assert 'expired' in slack.web.posts[-1][0]
    token = slack.buttons.issue(Intent('bind', thread_id=state.binding.codex_thread_id, root='500.000001'))
    slack.lost()
    assert token not in slack.buttons.tokens and not backend.sent


@pytest.mark.parametrize('kind', ['approval', 'compose_submit', 'artifact_submit', 'interrupt'])
async def test_navigation_action_cannot_make_business_token_reusable(setup, kind):
    controller, state, backend, _ = setup
    slack = adapter(controller, backend)
    intent = Intent(kind, state.binding.binding_id, state.epoch, root='300.000001')
    token = slack.buttons.issue(intent)
    payload = interaction(controller.cfg, token, intent.root)
    payload['actions'][0]['action_id'] = 'bridge_bind'
    _, prepared, after = await slack.prepare_interaction(payload)
    assert prepared is None
    if after:
        await after
    assert slack.buttons.take(token) is intent and slack.buttons.take(token) is None
    assert not backend.sent


@pytest.mark.parametrize('picker', ['projects', 'tasks'])
async def test_concurrent_picker_posts_get_independent_root_scoped_tokens(setup, picker):
    controller, _, backend, _ = setup
    slack = adapter(controller, backend)
    calls = []
    both = asyncio.Event()
    async def post(text, **kwargs):
        index = len(calls)
        calls.append(kwargs['blocks'])
        if len(calls) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 1)
        return f'{300 + index}.000001'
    slack.web.post = post
    if picker == 'tasks':
        async def threads(project=''):
            return [backend.current.thread]
        backend.list_threads = threads
    await asyncio.gather(getattr(slack, picker)(), getattr(slack, picker)())
    issued = []
    for index, blocks in enumerate(calls):
        tokens = [block['accessory']['value'] for block in blocks if 'accessory' in block]
        tokens += [element['value'] for block in blocks if block['type'] == 'actions'
                   for element in block['elements']]
        for token in tokens:
            assert slack.buttons.tokens[token].root == f'{300 + index}.000001'
        issued.append(set(tokens))
    assert not issued[0] & issued[1] and not backend.sent
