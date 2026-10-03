"""Comment submission state tests: fake browser, isolated real comment ledger."""
from contextlib import asynccontextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from utils.douyin_comments import DouyinCommentService


class CommentFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.account = dict(id='fixture-account', channel='douyin', display_name='fixture-author')
        self.task = dict(id='fixture-publish', state='PUBLISHED',
                         attempted_at='2026-10-03T00:00:00+00:00',
                         plan=dict(channel='douyin', account_id=self.account['id'], title='fixture-work'))
        self.ledger = MagicMock()
        self.ledger.get.return_value = self.task
        self.sessions = MagicMock()
        self.sessions.account.return_value = self.account
        self.sessions.probe = AsyncMock(return_value={'status': 'READY'})
        self.sessions.page_blocker = AsyncMock(return_value=None)
        self.context = MagicMock()
        self.opens = 0

        @asynccontextmanager
        async def opened(*args, **kwargs):
            self.opens += 1
            yield self.context
        self.sessions.open = opened

        self.service = DouyinCommentService(self.ledger, self.sessions, temporary.name)
        self.request = self.service.prepare(self.task['id'], '回复来自codex', 'fixture-key')
        self.page = MagicMock()
        self.page.url = 'https://creator.douyin.com/creator-micro/content/work-detail/123?tabKey=commentManagement'
        self.page.reload = AsyncMock()
        self.editor = MagicMock()
        self.editor.filter.return_value = self.editor
        self.editor.count = AsyncMock(return_value=1)
        self.editor.fill = AsyncMock()
        self.editor.inner_text = AsyncMock(return_value=self.request['text'])
        self.editor.wait_for = AsyncMock()
        self.send = MagicMock()
        self.send.filter.return_value = self.send
        self.send.count = AsyncMock(return_value=1)
        self.send.is_enabled = AsyncMock(return_value=True)
        self.send.click = AsyncMock()
        self.page.locator.return_value = self.editor
        self.page.get_by_role.return_value = self.send
        self.page.get_by_text.return_value.wait_for = AsyncMock()
        self.service._open_comments = AsyncMock(return_value=self.page)
        self.service._snapshot = AsyncMock(return_value={
            'screenshot_path': str(Path(temporary.name) / 'fixture.png'),
            'url': self.page.url,
        })
        self.match = dict(text=self.request['text'], account=self.account['display_name'])
        self.service._matching_comments = AsyncMock(side_effect=[[], [self.match]])
        self.sleep = patch('utils.douyin_comments.asyncio.sleep', new=AsyncMock())
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    async def test_guard_is_committed_before_only_click_and_new_key_cannot_repeat(self):
        async def assert_guard(**kwargs):
            stored = self.service.comments.get(self.request['id'])
            self.assertIsNotNone(stored['attempted_at'])
            self.assertEqual(stored['state'], 'UNKNOWN_RESULT')
        self.send.click.side_effect = assert_guard
        result = await self.service.post(self.request['id'])
        self.assertEqual(result['state'], 'VERIFIED')
        self.assertTrue(result['evidence']['verified_after_reload'])
        self.page.reload.assert_awaited_once()
        reused = self.service.prepare(self.task['id'], self.request['text'], 'new-key')
        again = await self.service.post(reused['id'])
        self.assertEqual(again['state'], 'VERIFIED')
        self.send.click.assert_awaited_once()
        self.assertEqual(self.opens, 1)

    async def test_click_timeout_consumes_attempt_and_never_retries(self):
        self.send.click.side_effect = TimeoutError('send timed out')
        first = await self.service.post(self.request['id'])
        self.assertEqual(first['state'], 'UNKNOWN_RESULT')
        self.assertIsNotNone(first['attempted_at'])
        again = await self.service.post(self.request['id'])
        self.assertEqual(again['attempted_at'], first['attempted_at'])
        self.send.click.assert_awaited_once()
        self.assertEqual(self.opens, 1)

    async def test_failure_before_click_remains_retryable_without_consuming_attempt(self):
        self.service._open_comments.side_effect = RuntimeError('COMMENT_UNAVAILABLE')
        failed = await self.service.post(self.request['id'])
        self.assertEqual(failed['state'], 'FAILED')
        self.assertIsNone(failed['attempted_at'])
        self.send.click.assert_not_awaited()
        self.service._open_comments.side_effect = None
        result = await self.service.post(self.request['id'])
        self.assertEqual(result['state'], 'VERIFIED')
        self.send.click.assert_awaited_once()

    async def test_multiline_browser_error_is_preserved_without_masking_preclick_failure(self):
        message = (
            'Locator.wait_for: Timeout 20000ms exceeded.\n'
            'Call log:\n'
            '  - waiting for locator("[contenteditable=true]") to be visible\n'
            '\t- locator resolved to hidden element'
        )
        self.service._open_comments.side_effect = TimeoutError(message)
        result = await self.service.post(self.request['id'])
        self.assertEqual(result['state'], 'FAILED')
        self.assertIsNone(result['attempted_at'])
        self.assertEqual(result['error'], ' '.join(message.split()))
        self.assertIn('Timeout 20000ms exceeded.', result['error'])
        self.assertIn('locator resolved to hidden element', result['error'])
        persisted = self.service.comments.get(self.request['id'])
        self.assertEqual(persisted['error'], result['error'])
        self.assertEqual(persisted['state'], 'FAILED')
        self.send.click.assert_not_awaited()
        self.editor.fill.assert_not_awaited()

    async def test_wrong_identity_never_opens_composer_or_clicks(self):
        self.sessions.probe.return_value = {'status': 'ACCOUNT_MISMATCH'}
        result = await self.service.post(self.request['id'])
        self.assertEqual(result['state'], 'FAILED')
        self.assertIsNone(result['attempted_at'])
        self.service._open_comments.assert_not_awaited()
        self.send.click.assert_not_awaited()

    async def test_identity_change_before_click_is_blocked(self):
        self.sessions.probe.side_effect = [{'status': 'READY'}, {'status': 'ACCOUNT_MISMATCH'}]
        result = await self.service.post(self.request['id'])
        self.assertEqual(result['state'], 'FAILED')
        self.assertIsNone(result['attempted_at'])
        self.send.click.assert_not_awaited()

    async def test_successful_click_without_refreshed_dom_match_is_unknown(self):
        self.service._matching_comments.side_effect = [[], []]
        result = await self.service.post(self.request['id'])
        self.assertEqual(result['state'], 'UNKNOWN_RESULT')
        self.assertFalse(result['evidence']['verified_after_reload'])
        self.page.reload.assert_awaited_once()
        self.send.click.assert_awaited_once()

    async def test_ambiguous_refreshed_matches_are_not_verified(self):
        self.service._matching_comments.side_effect = [[], [self.match, self.match]]
        result = await self.service.post(self.request['id'])
        self.assertEqual(result['state'], 'UNKNOWN_RESULT')
        self.send.click.assert_awaited_once()

    async def test_recheck_after_timeout_only_reads_and_can_confirm(self):
        self.send.click.side_effect = TimeoutError('send timed out')
        await self.service.post(self.request['id'])
        self.service._matching_comments.side_effect = None
        self.service._matching_comments.return_value = [self.match]
        result = await self.service.recheck(self.request['id'])
        self.assertEqual(result['state'], 'VERIFIED')
        self.send.click.assert_awaited_once()

    async def test_already_present_does_not_send(self):
        self.service._matching_comments.side_effect = None
        self.service._matching_comments.return_value = [self.match]
        result = await self.service.post(self.request['id'])
        self.assertEqual(result['state'], 'ALREADY_PRESENT')
        self.assertIsNone(result['attempted_at'])
        self.send.click.assert_not_awaited()

    async def test_changed_account_after_prepare_never_sends(self):
        self.sessions.account.return_value = {**self.account, 'id': 'other-account'}
        with self.assertRaisesRegex(ValueError, 'ACCOUNT_CHANGED'):
            await self.service.post(self.request['id'])
        self.send.click.assert_not_awaited()
        self.assertEqual(self.opens, 0)

    async def test_expected_video_id_is_passed_to_opening_guard(self):
        expected = '7692098400824708398'
        result = await self.service.post(self.request['id'], expected)
        self.assertEqual(result['state'], 'VERIFIED')
        self.service._open_comments.assert_awaited_once_with(self.context, self.task, expected)

    async def test_actual_different_video_id_fails_before_comment_attempt(self):
        self.context.new_page = AsyncMock(return_value=self.page)
        self.page.wait_for_url = AsyncMock()
        card = MagicMock()
        card.locator.return_value.click = AsyncMock()
        self.service._work_card = AsyncMock(return_value=card)
        self.service._open_comments = DouyinCommentService._open_comments.__get__(self.service)
        result = await self.service.post(self.request['id'], '7692098400824708398')
        self.assertEqual(result['state'], 'FAILED')
        self.assertEqual(result['error'], 'WORK_ID_MISMATCH')
        self.assertIsNone(result['attempted_at'])
        self.send.click.assert_not_awaited()
        self.editor.fill.assert_not_awaited()

    async def test_recheck_rejects_changed_account_before_browser_open(self):
        self.service.comments.mark_attempted(self.request['id'])
        self.sessions.account.return_value = {**self.account, 'id': 'other-account'}
        with self.assertRaisesRegex(ValueError, 'ACCOUNT_CHANGED'):
            await self.service.recheck(self.request['id'])
        self.assertEqual(self.opens, 0)
        self.send.click.assert_not_awaited()

    def popup_fixture(self):
        management = MagicMock(name='management-tab')
        management.url = 'https://creator.douyin.com/creator-micro/content/manage'
        management.wait_for_url = AsyncMock(side_effect=AssertionError('Must not wait on old management tab'))
        management.locator.side_effect = AssertionError('Must not inspect composer on old management tab')
        management.get_by_text.side_effect = AssertionError('Must not check comment labels on old management tab')
        management.reload = AsyncMock(side_effect=AssertionError('Must not reload old management tab'))
        unrelated = MagicMock(name='previously-open-unrelated-tab')
        self.context.pages = [unrelated, management]
        self.context.new_page = AsyncMock(return_value=management)
        expected = '7692098400824708398'
        self.page.url = (
            'https://creator.douyin.com/creator-micro/content/work-detail/'
            + expected + '?tabKey=commentManagement'
        )

        async def wait_for_url(pattern, **kwargs):
            self.assertRegex(self.page.url, pattern)
        self.page.wait_for_url = AsyncMock(side_effect=wait_for_url)
        card = MagicMock()
        entry = card.locator.return_value
        entry.click = AsyncMock()
        self.service._work_card = AsyncMock(return_value=card)
        self.service._open_comments = DouyinCommentService._open_comments.__get__(self.service)
        return management, entry, expected

    async def test_new_comment_tab_receives_every_check_submission_and_reload(self):
        management, entry, expected = self.popup_fixture()

        async def open_popup(**kwargs):
            self.context.pages.append(self.page)
        entry.click.side_effect = open_popup
        result = await self.service.post(self.request['id'], expected)
        self.assertEqual(result['state'], 'VERIFIED')
        self.service._work_card.assert_awaited_once_with(management, self.task)
        self.page.wait_for_url.assert_awaited_once()
        self.assertEqual(self.page.get_by_text.call_args_list[0].args, ('加载中，请稍候...',))
        self.assertEqual(self.page.get_by_text.call_args_list[1].args, ('全部评论',))
        self.assertTrue(all(call.args == (self.page,) for call in self.sessions.page_blocker.await_args_list))
        self.assertTrue(all(call.args[0] is self.page for call in self.service._matching_comments.await_args_list))
        self.assertTrue(all(call.args[0] is self.page for call in self.service._snapshot.await_args_list))
        self.editor.fill.assert_awaited_once_with(self.request['text'])
        self.send.click.assert_awaited_once()
        self.page.reload.assert_awaited_once()
        management.wait_for_url.assert_not_awaited()
        management.reload.assert_not_awaited()

    async def test_delayed_popup_is_selected_instead_of_old_or_existing_tabs(self):
        management, entry, expected = self.popup_fixture()
        ticks = 0

        async def advance_browser(delay):
            nonlocal ticks
            if delay == .1:
                ticks += 1
                if ticks == 2:
                    self.context.pages.append(self.page)
        with patch('utils.douyin_comments.asyncio.sleep', new=AsyncMock(side_effect=advance_browser)):
            opened = await self.service._open_comments(self.context, self.task, expected)
        self.assertIs(opened, self.page)
        self.assertEqual(ticks, 2)
        self.page.wait_for_url.assert_awaited_once()
        self.sessions.page_blocker.assert_awaited_once_with(self.page)
        management.wait_for_url.assert_not_awaited()
        self.editor.wait_for.assert_awaited_once()
        self.send.click.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
