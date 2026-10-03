"""Structured failure receipts on isolated ledgers; no browsers or real uploads."""
import asyncio
import hashlib
import importlib
import importlib.util
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from utils.publish_batches import PublishBatches
from utils.publish_errors import DeclarationError, safe_execution_error
from utils.publish_ledger import PublishLedger
from utils.publish_worker import PublishWorker


@pytest.fixture
def runtime(tmp_path):
    ledger = PublishLedger(tmp_path / 'isolated.sqlite3')
    media = tmp_path / 'fixture.mp4'
    media.write_bytes(b'not a video; no browser adapter is invoked')
    plan = dict(channel='douyin', account_id='isolated-account', title='fixture',
                description='', tags=[], declaration='内容由AI生成', media_path=str(media),
                media_sha256=hashlib.sha256(media.read_bytes()).hexdigest())
    sessions = SimpleNamespace(
        accounts=lambda: [], logins={}, account=lambda _: {'channel': 'douyin'},
        preflight=AsyncMock(return_value={'status': 'READY', 'publish_input_verified': True}),
        start_login=lambda *_: pytest.fail('must not initiate login'),
    )
    reconcile = AsyncMock()
    batches = PublishBatches(ledger, sessions, reconcile)
    return SimpleNamespace(ledger=ledger, plan=plan, sessions=sessions, batches=batches, reconcile=reconcile)


def declaration_error(code='DECLARATION_OPTION_NOT_FOUND'):
    return DeclarationError(code, requested='内容由AI生成',
                            available_options=['内容为转载信息'], channel='douyin')


def test_typed_error_has_safe_stable_contract_and_defensive_copy():
    error = DeclarationError('DECLARATION_OPTION_NOT_FOUND', requested='原创',
        available_options=['内容由AI生成', '内容由AI生成', 'token=private', 'https://private.invalid'], channel='douyin')
    assert str(error) == error.code == 'DECLARATION_OPTION_NOT_FOUND'
    first = error.detail
    assert set(first) == {'code', 'message', 'stage', 'details'}
    assert first['stage'] == 'DECLARATION' and '尚未点击发布' in first['message']
    assert first['details']['requested'] == '原创'
    assert first['details']['available_options'] == ['内容由AI生成', '[REDACTED]']
    first['details']['available_options'].append('changed')
    assert 'changed' not in error.to_dict()['details']['available_options']
    assert 'private' not in json.dumps(error.to_dict())


@pytest.mark.parametrize('code', [
    'DECLARATION_DIALOG_UNAVAILABLE', 'DECLARATION_OPTION_NOT_FOUND',
    'DECLARATION_CONFIRM_FAILED', 'DECLARATION_SELECTION_FAILED',
])
def test_batch_persists_declaration_failure_and_does_not_retry(runtime, code):
    batch = runtime.batches.submit('request-key', runtime.plan)
    execute = AsyncMock(side_effect=declaration_error(code))
    asyncio.run(runtime.batches.tick(execute))
    result = runtime.batches.get(batch['id'])
    task = result['tasks'][0]
    assert result['state'] == task['state'] == 'FAILED'
    assert result['error'] == code
    assert result['error_detail'] == task['error'] == task['runtime']['error']
    assert task['error']['message'] == declaration_error(code).message
    assert task['error']['stage'] == task['runtime']['stage'] == 'DECLARATION'
    assert task['error']['details']['requested'] == runtime.plan['declaration']
    assert task['error']['retryable'] is False
    assert task['error']['next_action'] == 'REVIEW_DECLARATION'
    assert task['attempted_at'] is None and task['evidence'] is None
    assert all(event['stage'] != 'SUBMIT_CLICK' for event in runtime.ledger.diagnostics(task['id'])['events'])
    # Persisted safe details survive reconstructing the ledger / batch manager.
    reloaded = PublishBatches(PublishLedger(runtime.ledger.path), runtime.sessions, runtime.reconcile)
    assert reloaded.get(batch['id'])['error_detail'] == result['error_detail']
    runtime.ledger.recover()
    reloaded.recover()
    assert reloaded.submit('request-key', runtime.plan)['id'] == batch['id']
    assert reloaded.submit('another-key', runtime.plan)['tasks'][0]['id'] == task['id']
    asyncio.run(reloaded.tick(execute))
    execute.assert_awaited_once()


def test_worker_persists_declaration_failure_without_auto_retry(runtime):
    task = runtime.ledger.submit('legacy-key', runtime.plan)
    execute = AsyncMock(side_effect=declaration_error())
    worker = PublishWorker(runtime.ledger, execute, runtime.reconcile, runtime.sessions)
    asyncio.run(worker.tick())
    asyncio.run(worker.tick())
    current = runtime.ledger.get(task['id'])
    diagnostic = runtime.ledger.diagnostics(task['id'])
    assert current['state'] == 'FAILED' and current['attempted_at'] is None
    assert diagnostic['runtime']['error']['code'] == 'DECLARATION_OPTION_NOT_FOUND'
    assert diagnostic['runtime']['error']['details']['available_options'] == ['内容为转载信息']
    assert diagnostic['runtime']['stage'] == 'DECLARATION'
    execute.assert_awaited_once()
    runtime.reconcile.assert_not_awaited()


@pytest.mark.parametrize('mode', ['worker', 'batch'])
def test_arbitrary_browser_exception_text_is_not_exposed(runtime, mode):
    execute = AsyncMock(side_effect=RuntimeError('https://private.invalid cookie=SECRET_TOKEN page dump'))
    if mode == 'batch':
        batch = runtime.batches.submit('key', runtime.plan)
        asyncio.run(runtime.batches.tick(execute))
        result = runtime.batches.get(batch['id'])
        error = result['error_detail']
    else:
        task = runtime.ledger.submit('key', runtime.plan)
        worker = PublishWorker(runtime.ledger, execute, runtime.reconcile, runtime.sessions)
        asyncio.run(worker.tick())
        result = runtime.ledger.diagnostics(task['id'])
        error = result['runtime']['error']
    assert error['code'] == 'EXECUTION_FAILED' and error['stage'] == 'UPLOAD_PREPARE'
    assert error['details'] == {} and error['message']
    assert not any(token in json.dumps(result) for token in ('SECRET_TOKEN', 'private.invalid', 'page dump'))


@pytest.mark.parametrize('mode', ['worker', 'batch'])
def test_typed_exception_after_click_keeps_unknown_result_guard(runtime, mode):
    async def execute(task):
        runtime.ledger.before_click(task['id'])
        raise declaration_error()
    called = AsyncMock(side_effect=execute)
    if mode == 'batch':
        batch = runtime.batches.submit('key', runtime.plan)
        asyncio.run(runtime.batches.tick(called))
        result = runtime.batches.get(batch['id'])
        task = result['tasks'][0]
        assert result['state'] == 'WAITING_RESULT'
        asyncio.run(runtime.batches.tick(called))
    else:
        task = runtime.ledger.submit('key', runtime.plan)
        worker = PublishWorker(runtime.ledger, called, runtime.reconcile, runtime.sessions)
        asyncio.run(worker.tick())
        task = runtime.ledger.get(task['id'])
        asyncio.run(worker.tick())
    assert task['state'] == 'UNKNOWN_RESULT' and task['attempted_at']
    error = runtime.ledger.diagnostics(task['id'])['runtime']['error']
    assert error['code'] == 'UNKNOWN_RESULT'
    assert '尚未点击发布' not in json.dumps(error, ensure_ascii=False)
    called.assert_awaited_once()


def test_legacy_codes_remain_readable_and_do_not_acquire_raw_text(runtime):
    batch = runtime.batches.submit('legacy', runtime.plan)
    task = batch['tasks'][0]
    runtime.ledger.preparation_failed(task['id'])
    runtime.ledger.delay(task['id'], 'EXECUTION_FAILED', seconds=0)
    runtime.batches.update(batch['id'], 'FAILED', error='EXECUTION_FAILED')
    result = runtime.batches.get(batch['id'])
    assert result['error'] == result['error_detail']['code'] == 'EXECUTION_FAILED'
    assert 'message' not in result['error_detail']
    assert safe_execution_error(RuntimeError('PROFILE_BUSY'), {'PROFILE_BUSY'})['code'] == 'PROFILE_BUSY'
    assert safe_execution_error(ValueError('UNSUPPORTED_EXPLICIT_DECLARATION'))['stage'] == 'DECLARATION'


ADAPTERS = [
    ('make_douyin_uploader', 'uploader.douyin_uploader.main', 'DouYinVideo', 'apply_self_declaration', 'douyin'),
    ('make_xhs_uploader', 'uploader.xiaohongshu_uploader.main', 'XiaoHongShuVideo', 'check_original_declaration', 'xiaohongshu'),
    ('make_tencent_uploader', 'uploader.tencent_uploader.main', 'TencentVideo', 'apply_original_statement', 'wechat_channels'),
]


@pytest.fixture
def real_adapters(monkeypatch):
    # Keep the actual constructors and uploader methods. Only suppress file
    # logging; importing a logger must not append test output to service logs.
    names = ['douyin', 'tencent', 'xhs', 'tiktok', 'bilibili', 'kuaishou',
             'baijiahao', 'xiaohongshu', 'youtube', 'alipay', 'weibo', 'hupu']
    logging = {name + '_logger': MagicMock() for name in names}
    monkeypatch.setitem(sys.modules, 'utils.log', SimpleNamespace(**logging))
    modules = {}
    for _, module, _, _, channel in ADAPTERS:
        loaded = importlib.import_module(module)
        modules[channel] = loaded
        for name, logger in logging.items():
            if hasattr(loaded, name):
                monkeypatch.setattr(loaded, name, logger)
    return modules


@pytest.mark.parametrize(('factory', 'module', 'base', 'method', 'channel'), ADAPTERS)
def test_real_adapter_constructor_and_explicit_no_declaration(runtime, real_adapters, factory, module, base, method, channel):
    from utils import mcp_publish_adapters
    plan = {**runtime.plan, 'channel': channel, 'title': '测试视频标题一',
            'description': '完整测试描述', 'tags': ['猫猫', '日常'], 'declaration': ''}
    session_file = str(Path(runtime.ledger.path).parent / 'unused-account.json')
    uploader = getattr(mcp_publish_adapters, factory)(plan, {'session_file': session_file})
    assert isinstance(uploader, getattr(real_adapters[channel], base))
    assert (uploader.title, uploader.file_path, uploader.tags, uploader.desc) == (
        plan['title'], plan['media_path'], plan['tags'], plan['description'])
    assert uploader.publish_date == 0 and uploader.headless is False
    assert str(uploader.account_file) == session_file
    if channel == 'wechat_channels':
        assert uploader.short_title == plan['title']
    if channel == 'douyin':
        assert uploader.declaration is None
        uploader.set_self_declaration = AsyncMock(side_effect=AssertionError('must not invent a declaration'))
    page = MagicMock()
    asyncio.run(getattr(uploader, method)(page))
    assert page.mock_calls == []
    if channel == 'douyin':
        uploader.set_self_declaration.assert_not_awaited()


@pytest.mark.parametrize(('factory', 'module', 'base', 'method', 'channel'), ADAPTERS[1:])
def test_real_non_douyin_adapter_rejects_nonempty_declaration(runtime, real_adapters, factory, module, base, method, channel):
    from utils import mcp_publish_adapters
    uploader = getattr(mcp_publish_adapters, factory)({**runtime.plan, 'channel': channel},
        {'session_file': str(Path(runtime.ledger.path).parent / 'unused-account.json')})
    page = MagicMock()
    with pytest.raises(DeclarationError) as raised:
        asyncio.run(getattr(uploader, method)(page))
    assert raised.value.code == 'UNSUPPORTED_EXPLICIT_DECLARATION'
    assert raised.value.details['channel'] == channel
    assert page.mock_calls == []


def test_real_douyin_error_crosses_execute_batch_and_mcp_status(runtime, real_adapters, monkeypatch, tmp_path):
    from utils import mcp_publish_adapters
    # Import the real MCP endpoint module with an isolated runtime. No server or
    # worker is started, and its execute function uses fake account/browser I/O.
    monkeypatch.setenv('SAU_RUNTIME_DIR', str(tmp_path / 'mcp-runtime'))
    service_path = Path(__file__).resolve().parents[1] / 'publish_mcp.py'
    spec = importlib.util.spec_from_file_location('_error_receipt_fixture_service', service_path)
    service = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(service)
    service.ledger = runtime.ledger
    service.batches = runtime.batches
    account = {'id': runtime.plan['account_id'], 'channel': 'douyin',
               'session_file': str(tmp_path / 'unused-account.json')}
    runtime.sessions.account = lambda _: account
    runtime.sessions.preflight_context = AsyncMock(return_value={'status': 'READY'})
    context = SimpleNamespace(sau_identity_verified=True)
    @asynccontextmanager
    async def opened(*args, **kwargs):
        yield context
    runtime.sessions.open = opened
    service.sessions = runtime.sessions
    real_factory = mcp_publish_adapters.make_douyin_uploader
    created = []
    expected = declaration_error()
    def factory(plan, selected_account):
        uploader = real_factory(plan, selected_account)
        assert uploader.declaration == runtime.plan['declaration']
        uploader.set_self_declaration = AsyncMock(side_effect=expected)
        async def isolated_upload(playwright):
            await uploader.apply_self_declaration(None)
        uploader.upload = AsyncMock(side_effect=isolated_upload)
        created.append(uploader)
        return uploader
    monkeypatch.setattr(mcp_publish_adapters, 'make_douyin_uploader', factory)
    batch = runtime.batches.submit('end-to-end-key', runtime.plan)
    asyncio.run(runtime.batches.tick(service.execute_task))
    # These are the real endpoint functions used by tools/call, JSON roundtrip
    # included. Web rendering is separately tested by the parent integration.
    response = json.loads(json.dumps(service.get_publish_batch(batch['id']), ensure_ascii=False))
    task = response['tasks'][0]
    status = service.get_publish_status(task['id'])
    for field in ('code', 'message', 'stage', 'details'):
        assert response['error_detail'][field] == task['error'][field] == expected.detail[field]
        assert status['runtime']['error'][field] == expected.detail[field]
    assert response['state'] == task['state'] == 'FAILED'
    assert task['attempted_at'] is None and task['evidence'] is None
    assert all(e['stage'] != 'SUBMIT_CLICK' for e in status['events'])
    created[0].set_self_declaration.assert_awaited_once_with(None, runtime.plan['declaration'])
    assert len(created) == 1
