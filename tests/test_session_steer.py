from dataclasses import replace
from types import SimpleNamespace
import time

import pytest

from slack_codex_bridge.desktop import Desktop
from slack_codex_bridge.model import Action, BridgeError, Receipt, new_id
from slack_codex_bridge.slack import Intent
from test_slack import adapter


@pytest.mark.parametrize('same_target',[True,False])
async def test_app_server_steer_keeps_exact_target_and_original_wire_input(cfg,same_target):
    desktop=Desktop(cfg,'ws://127.0.0.1:1')
    calls=[]
    async def call(method,params,**kwargs):
        calls.append((method,params,kwargs))
        return {'turnId':'target' if same_target else 'other'}
    desktop.rpc=SimpleNamespace(call=call)
    if same_target:
        await desktop.send(Action('steer','chat','target',text='Guide this exact task'))
    else:
        with pytest.raises(BridgeError,match='backend_target_unconfirmed') as error:
            await desktop.send(Action('steer','chat','target',text='Guide this exact task'))
        assert error.value.unknown
    assert calls==[('turn/steer',{'threadId':'chat','expectedTurnId':'target',
                                'input':[{'type':'text','text':'Guide this exact task'}]},{'write':True})]


@pytest.mark.parametrize('change',['none','completed','different_turn','reconnect','expired','wrong_user'])
async def test_steer_form_is_single_use_and_cannot_choose_a_new_target(setup,change):
    c,state,backend,_=setup
    target=new_id()
    backend.current=replace(backend.current,idle=False,active_turn=target)
    backend.receipt=Receipt(target)
    await c.refresh(state)
    slack=adapter(c,backend)
    intent=Intent('steer_submit',state.binding.binding_id,state.epoch,target,root=state.binding.slack_root_ts)
    token=slack.buttons.issue(intent)
    payload={'type':'view_submission','api_app_id':c.cfg.app_id,'team':{'id':c.cfg.team_id},
             'user':{'id':c.cfg.user_id},'view':{'callback_id':'bridge_steer','private_metadata':token,
                'state':{'values':{'instruction':{'text':{'value':'Guide original turn'}}}}}}
    if change=='completed':
        backend.current=replace(backend.current,idle=True,active_turn=None)
    elif change=='different_turn':
        backend.current=replace(backend.current,active_turn=new_id())
    elif change=='reconnect':
        c.invalidate()
        await c.synchronized()
    elif change=='expired':
        intent.expires=time.monotonic()-1
    elif change=='wrong_user':
        payload['user']['id']='UOTHER'
    _,prepared,_=await slack.prepare_interaction(payload)
    if change=='none':
        assert prepared and prepared.execute
        await slack.submit(state,prepared)
        assert len(backend.sent)==1 and backend.sent[0].kind=='steer' and backend.sent[0].turn_id==target
        assert await slack.prepare_interaction(payload)==(None,None,None)
    else:
        if prepared:
            result=await c.execute(state,prepared)
            assert result.status=='rejected'
        assert not backend.sent


async def test_ipc_card_explains_why_steer_is_unavailable_while_preserving_stop(setup):
    c,state,backend,_=setup
    backend.desktop_only_controls={'steer'}
    backend.current=replace(backend.current,idle=False,active_turn=new_id())
    await c.refresh(state)
    slack=adapter(c,backend)
    text,blocks=slack.card(state)
    actions=[e['action_id'] for b in blocks if b['type']=='actions' for e in b['elements']]
    assert 'cannot guarantee the original active turn' in text
    assert 'bridge_interrupt' in actions and 'bridge_steer' not in actions and 'bridge_entry' not in actions
