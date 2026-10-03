"""Declaration barriers use only temporary ledgers and inert account/browser functions."""
import asyncio
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from utils.publish_batches import ORDER, PublishBatches
from utils.publish_declarations import SelfDeclaration, declaration_capability, validate_declaration
from utils.publish_errors import DeclarationError
from utils.publish_ledger import PublishLedger


class DeclarationBarrierTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / 'inert.mp4'
        self.media.write_bytes(b'no real video or upload')
        self.ledger = PublishLedger(self.root / 'test.sqlite3')
        self.preflight = AsyncMock(return_value={'status': 'READY', 'publish_input_verified': True})
        self.sessions = SimpleNamespace(preflight=self.preflight, account=lambda aid: {'channel': aid},
                                        logins={}, start_login=lambda *_: self.fail('No login expected'))
        self.reconcile = AsyncMock()
        self.batches = PublishBatches(self.ledger, self.sessions, self.reconcile)

    def item(self, channel='douyin', declaration='', **overrides):
        return dict(channel=channel, account_id=channel, title='test', description='frozen', tags=[],
                    declaration=declaration, media_path=str(self.media),
                    media_sha256=hashlib.sha256(self.media.read_bytes()).hexdigest(), **overrides)

    def rows(self):
        with self.ledger.connect() as db:
            return {name: [tuple(row) for row in db.execute('SELECT * FROM ' + name)]
                    for name in ('publish_batches', 'batch_keys', 'batch_tasks', 'publish_attempts', 'task_runtime', 'task_events')}

    def legacy_batch(self, item, state='FAILED'):
        # Creates only an isolated fixture representing a plan frozen by old code.
        with patch('utils.publish_batches.validate_plan_declarations'):
            batch = self.batches.submit('legacy-' + item['channel'] + item['declaration'], item)
        self.batches.update(batch['id'], state, error='previous failure')
        with self.ledger.connect() as db:
            db.execute("UPDATE publish_attempts SET state='FAILED' WHERE id=?", (batch['tasks'][0]['id'],))
        return self.batches.get(batch['id'])

    def test_exact_enum_and_no_implicit_normalization(self):
        self.assertEqual([value.value for value in SelfDeclaration],
                         ['', '内容由AI生成', '内容为转载信息', '内容为个人观点或见解'])
        for value in ('原创', '转载', '原创声明', ' 内容由AI生成 ', ' ', None):
            with self.subTest(value=value), self.assertRaises(DeclarationError) as error:
                validate_declaration('douyin', value)
            self.assertEqual(error.exception.code, 'UNSUPPORTED_DECLARATION')
            self.assertIn('available_options', error.exception.to_dict()['details'])

    async def test_all_four_enum_values_by_three_channels_preflight(self):
        for channel in ORDER:
            for declaration in SelfDeclaration:
                with self.subTest(channel=channel, declaration=declaration.value):
                    self.preflight.reset_mock()
                    item = self.item(channel, declaration.value)
                    frozen = copy.deepcopy(item)
                    checks = await self.batches.inspect(item)
                    supported = channel == 'douyin' or declaration is SelfDeclaration.NONE
                    self.assertEqual(checks[0]['status'], 'READY' if supported else 'UNSUPPORTED_DECLARATION')
                    self.assertEqual(item, frozen)
                    self.assertEqual(self.preflight.await_count, int(supported))
                    if not supported:
                        detail = checks[0]['error_detail']
                        self.assertEqual(detail['stage'], 'DECLARATION')
                        self.assertEqual(detail['details']['requested'], declaration.value)
                        self.assertEqual(detail['details']['available_options'], [''])

    def test_all_four_enum_values_by_three_channels_submit(self):
        for channel in ORDER:
            for declaration in SelfDeclaration:
                with self.subTest(channel=channel, declaration=declaration.value):
                    item = self.item(channel, declaration.value)
                    before = self.rows()
                    key = channel + '-' + declaration.name
                    if channel == 'douyin' or declaration is SelfDeclaration.NONE:
                        batch = self.batches.submit(key, item)
                        self.assertEqual(batch['plan']['items'][0]['declaration'], declaration.value)
                    else:
                        with self.assertRaises(DeclarationError):
                            self.batches.submit(key, item)
                        self.assertEqual(self.rows(), before)

    def test_all_four_enum_values_by_three_channels_resume(self):
        for channel in ORDER:
            for declaration in SelfDeclaration:
                with self.subTest(channel=channel, declaration=declaration.value):
                    # A distinct hash avoids fixture dedup reusing earlier variants.
                    item = self.item(channel, declaration.value)
                    item['media_sha256'] = hashlib.sha256((channel + declaration.name).encode()).hexdigest()
                    batch = self.legacy_batch(item)
                    before = self.rows()
                    if channel == 'douyin' or declaration is SelfDeclaration.NONE:
                        result = self.batches.resume(batch['id'])
                        self.assertEqual(result['state'], 'PREFLIGHT_PENDING')
                        self.assertEqual(result['plan'], batch['plan'])
                    else:
                        with self.assertRaises(DeclarationError):
                            self.batches.resume(batch['id'])
                        self.assertEqual(self.rows(), before)

    async def test_original_rejected_before_profile_media_read_or_upload(self):
        item = self.item(declaration='原创')
        item['media_path'] = str(self.root / 'missing.mp4')
        checks = await self.batches.inspect(item)
        self.assertEqual(checks[0]['status'], 'UNSUPPORTED_DECLARATION')
        self.assertEqual(checks[0]['error_detail']['details']['requested'], '原创')
        self.preflight.assert_not_awaited()
        self.assertEqual(self.rows()['publish_attempts'], [])

    async def test_legacy_original_resume_preserves_ids_keys_plan_and_state(self):
        batch = self.legacy_batch(self.item(declaration='原创'))
        before = self.rows()
        with self.assertRaises(DeclarationError):
            self.batches.resume(batch['id'])
        self.assertEqual(self.rows(), before)
        self.assertIsNone(self.batches.get(batch['id'])['tasks'][0]['attempted_at'])

    async def test_recovered_legacy_batch_stops_before_upload(self):
        batch = self.legacy_batch(self.item(declaration='原创'), state='PREFLIGHT_PENDING')
        with self.ledger.connect() as db:
            db.execute('UPDATE publish_batches SET next_check=0 WHERE id=?', (batch['id'],))
            db.execute("UPDATE publish_attempts SET state='QUEUED' WHERE id=?", (batch['tasks'][0]['id'],))
        execute = AsyncMock()
        await self.batches.tick(execute)
        after = self.batches.get(batch['id'])
        self.assertEqual(after['state'], 'BLOCKED')
        self.assertEqual(after['error'], 'UNSUPPORTED_DECLARATION')
        self.assertEqual(after['plan'], batch['plan'])
        self.assertEqual(after['tasks'][0]['id'], batch['tasks'][0]['id'])
        self.assertIsNone(after['tasks'][0]['attempted_at'])
        execute.assert_not_awaited()
        self.preflight.assert_not_awaited()

    async def test_legacy_running_batch_cannot_bypass_new_gate(self):
        batch = self.legacy_batch(self.item(declaration='原创'), state='RUNNING')
        with self.ledger.connect() as db:
            db.execute('UPDATE publish_batches SET next_check=0 WHERE id=?', (batch['id'],))
            db.execute("UPDATE publish_attempts SET state='QUEUED' WHERE id=?", (batch['tasks'][0]['id'],))
        execute = AsyncMock()
        await self.batches.tick(execute)
        after = self.batches.get(batch['id'])
        self.assertEqual(after['state'], 'BLOCKED')
        self.assertEqual(after['checks'][0]['error_detail']['code'], 'UNSUPPORTED_DECLARATION')
        execute.assert_not_awaited()

    async def test_legacy_mixed_batch_rejects_only_invalid_targets(self):
        items = [self.item(channel, '内容由AI生成') for channel in ORDER]
        with patch('utils.publish_batches.validate_plan_declarations'):
            batch = self.batches.submit('mixed-legacy', {'items': items})
        execute = AsyncMock()
        await self.batches.tick(execute)
        after = self.batches.get(batch['id'])
        self.assertEqual([check['status'] for check in after['checks']],
                         ['SUBMISSION_NOT_ATTEMPTED', 'UNSUPPORTED_DECLARATION', 'UNSUPPORTED_DECLARATION'])
        self.assertEqual(after['state'], 'FAILED')
        execute.assert_awaited_once()
        self.assertEqual(execute.await_args.args[0]['plan']['channel'], 'douyin')


class DeclarationMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = Path(__file__).resolve().parents[1] / 'publish_mcp.py'
        spec = importlib.util.spec_from_file_location('isolated_declaration_mcp', source)
        self.service = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'SAU_RUNTIME_DIR': str(self.root), 'SAU_MCP_PORT': '19879'}):
            spec.loader.exec_module(self.service)
        self.media = self.root / 'inert.mp4'
        self.media.write_bytes(b'not uploaded')
        (self.root / 'accounts.json').write_text(json.dumps([
            {'id': c, 'channel': c, 'display_name': 'fixture', 'profile': str(self.root / c)} for c in ORDER]))

    def prepared_count(self):
        with self.service.ledger.connect() as db:
            return db.execute('SELECT count(*) FROM frozen_plans').fetchone()[0]

    def test_all_four_enum_values_by_three_channels_prepare(self):
        for channel in ORDER:
            for declaration in SelfDeclaration:
                with self.subTest(channel=channel, declaration=declaration.value):
                    before = self.prepared_count()
                    kwargs = dict(media_path=str(self.media), title='test', description='', tags=[],
                                  declaration=declaration, channels=[channel])
                    if channel == 'douyin' or declaration is SelfDeclaration.NONE:
                        result = self.service.prepare_publish(**kwargs)
                        self.assertEqual(result['plan']['declaration'], declaration.value)
                    else:
                        with self.assertRaises(ValueError) as error:
                            self.service.prepare_publish(**kwargs)
                        detail = json.loads(str(error.exception))
                        self.assertEqual(detail['code'], 'UNSUPPORTED_DECLARATION')
                        self.assertEqual(detail['details']['available_options'], [''])
                        self.assertEqual(self.prepared_count(), before)

    async def test_native_mcp_schema_exposes_enum_and_rejects_original(self):
        tool = next(t for t in await self.service.mcp.list_tools() if t.name == 'prepare_publish')
        schema = tool.inputSchema
        declaration = schema['properties']['declaration']
        if '$ref' in declaration:
            declaration = schema['$defs'][declaration['$ref'].rsplit('/', 1)[1]]
        self.assertEqual(declaration['enum'], [v.value for v in SelfDeclaration])
        with self.assertRaises(Exception) as error:
            await self.service.mcp.call_tool('prepare_publish', dict(media_path=str(self.media), title='test',
                description='', tags=[], declaration='原创', channels=['douyin']))
        self.assertIn('内容由AI生成', str(error.exception))
        self.assertIn('UNSUPPORTED_DECLARATION', str(error.exception))
        self.assertIn('不能自动改为其他声明或不添加', str(error.exception))
        self.assertEqual(self.prepared_count(), 0)

    def test_original_prepare_rejected_before_media_hash_and_no_mutation(self):
        with patch.object(self.service, 'media_digest', side_effect=AssertionError('must not read media')):
            with self.assertRaises(ValueError) as error:
                self.service.prepare_publish(str(self.root / 'missing.mp4'), 'test', '', '原创', [], ['douyin'])
        self.assertEqual(json.loads(str(error.exception))['code'], 'UNSUPPORTED_DECLARATION')
        self.assertEqual(self.prepared_count(), 0)

    def test_channel_capabilities_preserve_verified_subset_and_blank(self):
        for channel in self.service.list_publish_channels():
            self.assertEqual(channel['declaration'], declaration_capability(channel['channel']))
            self.assertNotIn('原创', channel['declaration']['supported_values'])
            self.assertIn('并非平台全部选项', channel['declaration']['note'])

    async def test_legacy_single_task_execute_blocks_before_opening_profile(self):
        self.service.sessions.account = lambda *_: self.fail('must not look up/open profile')
        with self.assertRaises(DeclarationError):
            await self.service.execute_task({'plan': {'channel': 'douyin', 'declaration': '原创'}})


if __name__ == '__main__':
    unittest.main()
