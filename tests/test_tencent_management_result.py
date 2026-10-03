import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from utils.mcp_publish_adapters import verify_tencent, tencent_management_titles


@pytest.mark.parametrize('state,text,stamp', [
    ('PUBLISHED', '2026年09月12日 12:27\n分享\n可见权限', '2026-09-12T12:27:00+08:00'),
    ('SUBMITTED', '处理中\n删除', None),
    ('PENDING_REVIEW', '2026年09月12日 12:27\n审核中', '2026-09-12T12:27:00+08:00'),
])
def test_composed_manager_content_matches_without_claiming_processing_is_published(tmp_path, state, text, stamp):
    plan = dict(channel='wechat_channels', account_id='fixture', title='肥猫太悠闲了',
                tags=['萌宠', '猫咪'], description='胖狸花')
    task = dict(plan=plan, attempted_at='2026-09-12T04:27:37+00:00')
    card = dict(title='肥猫太悠闲了 #萌宠 #猫咪 胖狸花', text=text)
    page = MagicMock()
    page.url = 'https://channels.weixin.qq.com/platform/post/list'
    page.goto = AsyncMock(); page.screenshot = AsyncMock()
    page.get_by_text.return_value.filter.return_value.first.wait_for = AsyncMock()
    page.get_by_text.return_value.filter.return_value.first.click = AsyncMock()
    page.locator.return_value.first.wait_for = AsyncMock()
    page.locator.return_value.evaluate_all = AsyncMock(return_value=[card])
    receipt = {}
    ledger = SimpleNamespace(path=tmp_path / 'ledger.db', get=lambda _: task,
        record_dom_result=lambda task_id, actual, evidence: receipt.update(state=actual, evidence=evidence))
    asyncio.run(verify_tencent(page, ledger, 'fixture', max_checks=1))
    assert receipt['state'] == state
    assert receipt['evidence']['published_at'] == stamp


def test_composed_title_matching_is_exact_not_substring():
    titles = tencent_management_titles(dict(title='肥猫太悠闲了', tags=['萌宠'], description='胖狸花'))
    assert '肥猫太悠闲了 #萌宠 胖狸花' in titles
    assert '肥猫太悠闲了 #萌宠 其他内容' not in titles
