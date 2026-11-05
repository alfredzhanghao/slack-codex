"""Project labels are presentation metadata; exact directory admission is unchanged."""

from dataclasses import replace
import json
from pathlib import Path

import pytest

from slack_codex_bridge import preferences, project_names
from slack_codex_bridge.i18n import blocks
from test_p2_approval import interaction
from test_slack import adapter


def metadata(cfg, tmp_path, value):
    db = tmp_path / 'state_5.sqlite'
    db.touch()
    source = db.with_name('.codex-global-state.json')
    source.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return replace(cfg, native_state_db=db), source


def test_renamed_project_metadata_matches_exact_root_without_changing_access(cfg, tmp_path):
    path = str(cfg.projects[0])
    cfg, source = metadata(cfg, tmp_path, {'local-projects': {
        'known': {'name': '企业管理项目', 'rootPaths': [path]},
        'outside': {'name': 'Do not expose', 'rootPaths': [str(tmp_path / 'outside')]}}})
    before = source.read_bytes()
    assert project_names.labels(cfg) == {path: '企业管理项目'}
    assert source.read_bytes() == before and not cfg.allows(str(tmp_path / 'outside'))


def test_native_rename_refreshes_on_next_read_and_new_names_override_legacy(cfg, tmp_path):
    path = str(cfg.projects[0])
    cfg, source = metadata(cfg, tmp_path, {'electron-workspace-root-labels': {path: 'Legacy name'},
        'local-projects': {'project': {'name': 'First name', 'rootPaths': [path]}}})
    assert project_names.labels(cfg)[path] == 'First name'
    source.write_text(json.dumps({'local-projects': {'project': {'name': 'New desktop name', 'rootPaths': [path]}}}))
    assert project_names.labels(cfg)[path] == 'New desktop name'


@pytest.mark.parametrize('invalid', ['', ' ', None, 4, ['name'], 'bad\nname', 'x' * 201])
def test_invalid_new_name_retains_valid_legacy_name(cfg, tmp_path, invalid):
    path = str(cfg.projects[0])
    cfg, _ = metadata(cfg, tmp_path, {'electron-workspace-root-labels': {path: 'Legacy name'},
        'local-projects': {'project': {'name': invalid, 'rootPaths': [path]}}})
    assert project_names.labels(cfg)[path] == 'Legacy name'


@pytest.mark.parametrize('value', ['{broken', '[]', '{"local-projects":', '{"local-projects":{},}'])
def test_missing_or_invalid_metadata_falls_back_to_folder_name(cfg, tmp_path, value):
    assert project_names.labels(cfg) == {str(cfg.projects[0]): cfg.projects[0].name}
    cfg, source = metadata(cfg, tmp_path, {})
    source.write_text(value)
    assert project_names.labels(cfg) == {str(cfg.projects[0]): cfg.projects[0].name}
    source.unlink()
    assert project_names.labels(cfg) == {str(cfg.projects[0]): cfg.projects[0].name}


def test_only_selected_top_level_metadata_values_are_decoded(monkeypatch):
    raw = json.dumps({'unrelated': {'private_message': 'unrelated-sentinel " } , \\ text'},
        'local-projects': {'p': {'name': 'Project', 'rootPaths': []}},
        'electron-workspace-root-labels': {'D:/project': 'Name'}})
    original, decoded = json.loads, []
    def selected(text):
        decoded.append(text)
        return original(text)
    monkeypatch.setattr(project_names.json, 'loads', selected)
    result = project_names._selected(raw)
    assert set(result) == project_names.FIELDS
    assert not any('unrelated-sentinel' in text for text in decoded)


def test_nested_lookalike_project_key_is_not_a_project_source():
    assert project_names._selected('{"other":{"local-projects":{"p":{"name":"Wrong"}}}}') == {}


@pytest.mark.parametrize('text', ['{"local-projects":{},"local-projects":{}}',
    '{"local-projects":' + json.dumps({'extra': 'x' * (512 * 1024)}) + '}'])
def test_duplicate_or_oversized_metadata_tables_are_rejected(text):
    with pytest.raises(ValueError):
        project_names._selected(text)


def test_duplicate_labels_stay_unique_without_paths_even_when_suffix_already_named(cfg, tmp_path):
    roots = [cfg.projects[0], tmp_path / 'second', tmp_path / 'third']
    for root in roots:
        root.mkdir(exist_ok=True)
    cfg = replace(cfg, projects=tuple(roots))
    cfg, _ = metadata(cfg, tmp_path, {'local-projects': {
        str(i): {'name': name, 'rootPaths': [str(path)]}
        for i, (path, name) in enumerate(zip(roots, ['Same', 'Same', 'Same (1)']))}})
    labels = project_names.labels(cfg)
    assert len({name.casefold() for name in labels.values()}) == 3
    assert labels[str(roots[2])] == 'Same (1)'
    assert all(str(root) not in labels[str(root)] for root in roots)


def test_changed_open_handle_cannot_read_another_file(cfg, tmp_path, monkeypatch):
    cfg, source = metadata(cfg, tmp_path, {})
    class File:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, *_): pytest.fail('must reject handle before reading bytes')
    monkeypatch.setattr(Path, 'open', lambda *_args, **_kwargs: File())
    monkeypatch.setattr(project_names, 'handle_path', lambda _: tmp_path / 'another.json')
    assert project_names.labels(cfg)[str(cfg.projects[0])] == cfg.projects[0].name


def test_symlink_is_rejected_without_opening_metadata(cfg, tmp_path, monkeypatch):
    cfg, source = metadata(cfg, tmp_path, {})
    monkeypatch.setattr(Path, 'is_symlink', lambda _: True)
    monkeypatch.setattr(Path, 'open', lambda *_args, **_kwargs: pytest.fail('must not open symlink'))
    assert project_names.labels(cfg)[str(cfg.projects[0])] == cfg.projects[0].name


def test_unconfigured_metadata_paths_are_never_resolved(cfg, tmp_path, monkeypatch):
    path = str(cfg.projects[0])
    cfg, source = metadata(cfg, tmp_path, {'electron-workspace-root-labels': {
        path: 'Approved label', '//unconfigured-server/share': 'Ignored', str(tmp_path / 'outside'): 'Ignored'}})
    original = Path.resolve
    def scoped(self, *args, **kwargs):
        assert self in {source.parent, cfg.projects[0]}, 'must not resolve unconfigured metadata paths'
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', scoped)
    assert project_names.labels(cfg) == {path: 'Approved label'}


async def test_project_picker_displays_name_only_and_click_keeps_exact_directory(setup, tmp_path):
    c, state, backend, _ = setup
    cfg, _ = metadata(c.cfg, tmp_path, {'local-projects': {
        'project': {'name': '桌面项目名称', 'rootPaths': [str(c.cfg.projects[0])]}}})
    c.cfg = cfg
    slack = adapter(c, backend)
    await slack.projects()
    section = slack.web.posts[-1][1]['blocks'][0]
    assert section['text']['text'] == '桌面项目名称'
    assert str(cfg.projects[0]) not in json.dumps(slack.web.posts[-1], ensure_ascii=False)
    payload = interaction(cfg, section['accessory']['value'], '200.000001')
    payload['actions'][0]['action_id'] = 'bridge_project'
    calls = []
    async def tasks(**kwargs): calls.append(kwargs)
    slack.tasks = tasks
    _, _, after = await slack.prepare_interaction(payload)
    await after
    assert calls == [{'project': str(cfg.projects[0])}] and not backend.sent


async def test_task_picker_and_card_use_literal_desktop_name_without_visible_path(setup, tmp_path):
    c, state, backend, _ = setup
    name = 'State: Custom project'
    cfg, _ = metadata(c.cfg, tmp_path, {'local-projects': {
        'project': {'name': name, 'rootPaths': [str(c.cfg.projects[0])]}}})
    c.cfg = cfg
    preferences.save(cfg, preferences.Preferences(language='zh-CN'))
    slack = adapter(c, backend)
    async def threads(project=''): return [backend.current.thread]
    backend.list_threads = threads
    await slack.tasks(project=str(cfg.projects[0]))
    text, options = slack.web.posts[-1]
    assert name in text and str(cfg.projects[0]) not in text
    visible = json.dumps(blocks(options['blocks'], 'zh-CN'), ensure_ascii=False)
    assert name in visible and str(cfg.projects[0]) not in visible
    card, card_blocks = slack.card(state)
    assert name in card and name in json.dumps(blocks(card_blocks, 'zh-CN'), ensure_ascii=False)


async def test_alias_that_matches_a_translatable_button_stays_literal(setup, tmp_path):
    c, _, backend, _ = setup
    cfg, _ = metadata(c.cfg, tmp_path, {'local-projects': {
        'project': {'name': 'Settings', 'rootPaths': [str(c.cfg.projects[0])]}}})
    c.cfg = cfg
    slack = adapter(c, backend)
    await slack.projects()
    translated = blocks(slack.web.posts[-1][1]['blocks'], 'zh-CN')
    assert translated[0]['text']['text'] == 'Settings'
