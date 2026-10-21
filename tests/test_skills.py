"""Exercise the optional loader using temporary skills, not retired business SOPs."""
import pytest

from app.skills import SkillManager
from app.tools import skill_tool


@pytest.fixture
def skill_dir(tmp_path):
    directory = tmp_path / 'sample-guide'
    directory.mkdir()
    (directory / 'SKILL.md').write_text(
        '---\nname: sample-guide\ndescription: Test-only guide\n'
        'routes: sample\ntriggers: sample request\n---\n'
        'Read the supplied information and ask about missing details.\n',
        encoding='utf-8',
    )
    return tmp_path


@pytest.fixture
def manager(skill_dir):
    return SkillManager(str(skill_dir))


@pytest.fixture
def bound_manager(manager):
    token = skill_tool._skill_manager.set(manager)
    try:
        yield manager
    finally:
        skill_tool._skill_manager.reset(token)


def test_skill_discovery(manager):
    assert manager.skill_names == ['sample-guide']
    assert manager.skill_count == 1


def test_catalog_generation(manager):
    assert manager.get_catalog() == [
        {'name': 'sample-guide', 'description': 'Test-only guide'}]
    prompt = manager.build_catalog_prompt()
    assert 'sample-guide' in prompt
    assert 'Test-only guide' in prompt
    assert 'Read the supplied information' not in prompt


def test_load_skill_success(bound_manager):
    result = skill_tool.load_skill('sample-guide')
    assert result['success'] is True
    assert result['skill_name'] == 'sample-guide'
    assert result['instructions'] == 'Read the supplied information and ask about missing details.'


def test_load_skill_unknown(bound_manager):
    result = skill_tool.load_skill('unknown-guide')
    assert result['success'] is False
    assert 'sample-guide' in result['error']
    assert 'instructions' not in result


def test_skills_disabled(skill_dir):
    manager = SkillManager(str(skill_dir), enabled=False)
    assert manager.skill_count == 0
    assert manager.build_catalog_prompt() == ''
    assert manager.build_selected_skill_prompt('sample request', 'sample') == ''
    assert manager.load_skill('sample-guide')['success'] is False


def test_lazy_loading(manager):
    skill = manager._skills['sample-guide']
    assert not skill._body_loaded
    assert skill.load_body() == 'Read the supplied information and ask about missing details.'
    assert skill._body_loaded


def test_skill_selection(manager):
    prompt = manager.build_selected_skill_prompt('sample request', 'sample')
    assert 'sample-guide' in prompt
    assert 'Read the supplied information' in prompt
    assert manager.build_selected_skill_prompt('sample request', 'other-route') == ''
    assert manager.build_selected_skill_prompt('unrelated request', 'sample') == ''


@pytest.mark.parametrize('existing_directory', [False, True])
def test_empty_catalog_is_safe(tmp_path, existing_directory):
    directory = tmp_path / 'definitions'
    if existing_directory:
        directory.mkdir()
    manager = SkillManager(str(directory))
    assert manager.skill_count == 0
    assert manager.get_catalog() == []
    assert manager.build_catalog_prompt() == ''
    assert manager.build_selected_skill_prompt('sample request', 'sample') == ''
    assert manager.load_skill('sample-guide')['success'] is False
