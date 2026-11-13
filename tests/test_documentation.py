from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = ['README.md', 'README.zh-CN.md', 'DESIGN.md', 'DESIGN_OVERVIEW.md', 'DESIGN_TRUSTED_RESEARCH_LIFECYCLE.md']


@pytest.mark.parametrize('name', DOCUMENTS)
def test_document_is_utf8_and_references_real_files(name):
    content = (ROOT / name).read_bytes().decode('utf-8', errors='strict')
    assert content.strip() and content.startswith('# ')
    assert not re.search(r'\?{3,}|\ufffd', content)
    if name != 'README.md':
        assert re.search(r'[\u4e00-\u9fff]{8,}', content)
        for line in content.splitlines():
            if line.startswith('#'):
                assert re.search(r'[\u4e00-\u9fff]', line), line
    for path in re.findall(r'(?:tests|app)/[\w./-]+\.py', content):
        assert (ROOT / path).is_file(), path


def test_documented_atomicity_and_conditional_model_have_implementations():
    database = (ROOT / 'app/infrastructure/database.py').read_text(encoding='utf-8')
    model = (ROOT / 'tests/test_autonomous_research_e2e.py').read_text(encoding='utf-8')
    for symbol in ['BEGIN IMMEDIATE', 'def _require_owner', 'def _end_tool_call', 'def _transition_task']:
        assert symbol in database
    assert 'class ConditionalResearchModel(BaseChatModel)' in model
    assert 'CONTRADICTS_HYPOTHESIS:' in model
    assert 'SUPPORTS_HYPOTHESIS:' in model


def test_unmocked_provider_is_blocked_even_on_loopback():
    import openai
    client = openai.OpenAI(api_key='offline-placeholder', base_url='http://127.0.0.1:12345/v1')
    with pytest.raises(AssertionError, match='Provider request'):
        client.chat.completions.create(model='offline', messages=[{'role': 'user', 'content': 'blocked'}])
