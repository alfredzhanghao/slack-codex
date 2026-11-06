"""Names and native activity affect presentation, never thread authorization."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from slack_codex_bridge import thread_names
from slack_codex_bridge.desktop import Desktop
from slack_codex_bridge.model import BridgeError, Thread, activity_ms, recent_threads
from test_slack import adapter


def index(cfg, tmp_path, rows):
    db = tmp_path / 'state_5.sqlite'
    db.touch()
    source = db.with_name('session_index.jsonl')
    source.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows), encoding='utf-8')
    return replace(cfg, native_state_db=db), source


def test_latest_valid_name_wins_and_invalid_lines_do_not_erase_it(cfg, tmp_path):
    cfg, source = index(cfg, tmp_path, [{'id':'one','thread_name':'Old'},
        {'id':'one','thread_name':'优化客户咨询简历'}, {'id':'two','thread_name':'优化客户咨询简历 (2)'},
        {'id':'one','thread_name':None}, {'id':'bad','thread_name':'bad\nname'}])
    source.write_bytes(source.read_bytes() + b'\n{broken\n[]\n\xff')
    assert thread_names.names(cfg) == {'one':'优化客户咨询简历','two':'优化客户咨询简历 (2)'}
    source.write_text('{"id":"one","thread_name":"Renamed again"}', encoding='utf-8')
    assert thread_names.names(cfg) == {'one':'Renamed again'}


def test_unconfigured_missing_oversized_and_symlink_index_fall_back(cfg, tmp_path, monkeypatch):
    assert thread_names.names(cfg) == {}
    cfg, source = index(cfg, tmp_path, [{'id':'one','thread_name':'Name'}])
    monkeypatch.setattr(thread_names, 'LIMIT', 4)
    assert thread_names.names(cfg) == {}
    monkeypatch.setattr(thread_names, 'LIMIT', 8 * 1024**2)
    original = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda p: p == source or original(p))
    assert thread_names.names(cfg) == {}
    monkeypatch.setattr(Path, 'is_symlink', original)
    source.unlink()
    assert thread_names.names(cfg) == {}


def test_changed_physical_handle_is_rejected_before_reading(cfg, tmp_path, monkeypatch):
    cfg, source = index(cfg, tmp_path, [{'id':'one','thread_name':'Name'}])
    class Stream:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def read(self,*_):pytest.fail('must verify opened handle before any bytes are read')
    original = Path.open
    monkeypatch.setattr(Path, 'open', lambda p,*a,**kw: Stream() if p == source else original(p,*a,**kw))
    monkeypatch.setattr(thread_names, 'handle_path', lambda _:tmp_path/'unrelated.jsonl')
    assert thread_names.names(cfg) == {}


def test_oversized_line_is_skipped_without_losing_other_names(cfg, tmp_path, monkeypatch):
    cfg, _ = index(cfg, tmp_path, [{'id':'long','thread_name':'x'*250}, {'id':'ok','thread_name':'Name'}])
    monkeypatch.setattr(thread_names,'LINE_LIMIT',100)
    assert thread_names.names(cfg) == {'ok':'Name'}


def test_name_index_cannot_expand_thread_or_project_visibility(cfg, tmp_path):
    cfg, _ = index(cfg, tmp_path, [{'id':'inside','thread_name':'Settings'},
        {'id':'outside','thread_name':'Hidden rename'}])
    desktop = Desktop(cfg, 'ws://127.0.0.1:1')
    inside = desktop._thread({'id':'inside','cwd':str(cfg.projects[0]),'name':'Old title'})
    assert inside.title == 'Settings'
    with pytest.raises(BridgeError,match='thread_outside_allowlist'):
        desktop._thread({'id':'outside','cwd':str(tmp_path/'unrelated')})
    assert not cfg.allows(str(tmp_path/'unrelated'))


def test_native_id_placeholder_falls_back_to_preview_or_short_id(cfg):
    desktop=Desktop(cfg,'ws://127.0.0.1:1')
    row={'id':'12345678-1234-4234-8234-123456789abc','cwd':str(cfg.projects[0])}
    row['name']=row['id']
    assert desktop._thread(row).title=='12345678'
    assert desktop._thread({**row,'preview':'A readable preview'}).title=='A readable preview'


@pytest.mark.parametrize('value', [None,True,False,'123',0,-1,float('nan'),float('inf'),10**400,253402300800])
def test_invalid_activity_remains_unknown(value):
    assert activity_ms(value) is None


def test_native_seconds_are_normalized_without_using_index_rename_time(cfg, tmp_path):
    cfg, _ = index(cfg, tmp_path, [{'id':'one','thread_name':'Renamed','updated_at':'2999-01-01'}])
    desktop = Desktop(cfg, 'ws://127.0.0.1:1')
    for field in ['updatedAt','updated_at']:
        thread = desktop._thread({'id':'one','cwd':str(cfg.projects[0]),field:1700000000.25})
        assert thread.updated_at_ms == 1700000000250 and thread.title == 'Renamed'


async def test_all_backend_pages_are_deduplicated_then_globally_sorted(cfg):
    desktop = Desktop(cfg, 'ws://127.0.0.1:1')
    cwd = str(cfg.projects[0])
    calls=[]
    async def call(method, params):
        calls.append((method,params))
        if not params['cursor']:
            return {'data':[{'id':'a','name':'Old','cwd':cwd,'updatedAt':10},
                            {'id':'b','name':'Second','cwd':cwd,'updatedAt':20}], 'nextCursor':'second'}
        return {'data':[{'id':'a','name':'Recent','cwd':cwd,'updatedAt':30},
                        {'id':'z','cwd':cwd,'updatedAt':100,'archived':True},
                        {'id':'internal','cwd':cwd,'updatedAt':200,'source':'subAgent'},
                        {'id':'unknown','cwd':cwd}], 'nextCursor':None}
    desktop.rpc.call=call
    rows=await desktop.list_threads()
    assert [(t.id,t.title) for t in rows]==[('a','Recent'),('b','Second'),('unknown','unknown')]
    assert len(calls)==2 and all(p['archived'] is False for _,p in calls)


def test_equal_and_missing_activity_have_stable_order():
    rows=[Thread('z','Same','p'),Thread('b','Same','p',updated_at_ms=10),
          Thread('a','Same','p',updated_at_ms=10),Thread('c','Same','p')]
    assert [r.id for r in recent_threads(rows)]==['a','b','c','z']


async def test_tasks_sort_globally_before_twenty_item_pagination(setup):
    c,state,backend,_=setup
    slack=adapter(c,backend)
    rows=[Thread(str(i),f'Chat {i}',str(c.cfg.projects[0]),updated_at_ms=i*1000) for i in range(25)]
    async def listing(*_):return rows+[replace(rows[0],updated_at_ms=99000)]
    backend.list_threads=listing
    await slack.tasks(page=0)
    first=[v.thread_id for v in slack.buttons.tokens.values() if v.kind=='bind']
    assert first==['0']+[str(i) for i in range(24,5,-1)]
    slack.buttons.clear()
    await slack.tasks(page=1)
    second=[v.thread_id for v in slack.buttons.tokens.values() if v.kind=='bind']
    assert second==['5','4','3','2','1']


async def test_projects_use_exact_visible_main_activity_then_paginate(setup, tmp_path):
    c,_,backend,_=setup
    projects=[]
    for i in range(25):
        path=tmp_path/f'project-{i:02}'
        path.mkdir()
        projects.append(path)
    c.cfg=replace(c.cfg,projects=tuple(projects))
    slack=adapter(c,backend)
    async def listing():return [Thread('recent','Recent',str(projects[-1]),updated_at_ms=30),
        Thread('older','Older',str(projects[0]),updated_at_ms=10),
        Thread('outside','Excluded',str(projects[1]),allowed=False,updated_at_ms=1000),
        Thread('child','Nested',str(projects[2]/'nested'),updated_at_ms=2000)]
    backend.list_threads=listing
    await slack.projects()
    choices=[v.choice for v in slack.buttons.tokens.values() if v.kind=='project']
    assert choices[:2]==[str(projects[-1]),str(projects[0])] and len(choices)==20
    slack.buttons.clear()
    await slack.projects(1)
    assert len([v for v in slack.buttons.tokens.values() if v.kind=='project'])==5
    async def broken():raise BridgeError('backend_disconnected')
    backend.list_threads=broken
    await slack.projects()
    assert 'Activity unavailable' in slack.web.posts[-1][0]


async def test_known_name_survives_unavailable_snapshot_but_state_is_unknown(setup):
    c,state,backend,_=setup
    slack=adapter(c,backend)
    state.snapshot=replace(state.snapshot,thread=replace(state.snapshot.thread,title='My renamed chat'))
    async def missing(_):raise BridgeError('desktop_ipc_open_chat_in_desktop')
    backend.snapshot=missing
    with pytest.raises(BridgeError):
        await slack.refresh_view(state)
    text,_=slack.card(state)
    assert 'My renamed chat' in text and 'syncing/offline | unknown' in text and 'online | idle' not in text
