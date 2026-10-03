import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from utils.douyin_publish_result import match_record, verify_result


class DouyinResultTests(unittest.TestCase):
    attempt = '2026-09-08T01:31:35+00:00'

    def record(self, stamp='2026年09月08日 09:31', status='已发布'):
        return dict(title='test', text=f'test\n{stamp}\n{status}\n播放\n100')

    def test_match_minute_precision_and_timezone(self):
        self.assertEqual(match_record([self.record()], 'test', self.attempt)['state'], 'PUBLISHED')

    def test_old_same_title_is_not_success(self):
        self.assertIsNone(match_record([self.record('2026年09月07日 09:31')], 'test', self.attempt))

    def test_platform_combines_title_and_description(self):
        record = {**self.record(), 'title': 'test caption'}
        self.assertIsNotNone(match_record([record], 'test', self.attempt, 'caption'))
        self.assertIsNone(match_record([record], 'test', self.attempt, 'different'))

    def test_complete_caption_with_ordered_tags_matches(self):
        record = {**self.record(), 'title': 'test caption #cat #pets'}
        self.assertEqual(match_record([record], 'test', self.attempt, 'caption', ['cat', 'pets'])['state'], 'PUBLISHED')

    def test_caption_whitespace_is_normalized_without_removing_content(self):
        record = {**self.record(), 'title': '  test\ncaption\t#cat   #pets\u00a0'}
        self.assertIsNotNone(match_record([record], 'test', self.attempt, ' caption ', ['cat', 'pets']))

    def test_caption_without_description_keeps_all_tags(self):
        record = {**self.record(), 'title': 'test #cat #pets'}
        self.assertIsNotNone(match_record([record], 'test', self.attempt, tags=['cat', 'pets']))

    def test_wrong_extra_missing_or_reordered_tags_do_not_match(self):
        for caption in ['test', 'test caption', 'test caption #cat',
                        'test caption #cat #wrong', 'test caption #cat #pets #extra',
                        'test caption #pets #cat', 'test caption #cat #cat #pets',
                        'test caption #cat #petsuffix', 'test different #cat #pets']:
            with self.subTest(caption=caption):
                record = {**self.record(), 'title': caption}
                self.assertIsNone(match_record([record], 'test', self.attempt, 'caption', ['cat', 'pets']))

    def test_empty_tags_preserve_both_legacy_title_forms(self):
        for caption in ['test', 'test caption']:
            with self.subTest(caption=caption):
                record = {**self.record(), 'title': caption}
                self.assertIsNotNone(match_record([record], 'test', self.attempt, 'caption', []))

    def test_complete_caption_still_requires_unique_recent_record(self):
        record = {**self.record(), 'title': 'test caption #cat #pets'}
        self.assertIsNone(match_record([record, record], 'test', self.attempt, 'caption', ['cat', 'pets']))
        old = {**self.record('2026年09月07日 09:31'), 'title': record['title']}
        self.assertIsNone(match_record([old], 'test', self.attempt, 'caption', ['cat', 'pets']))
        future = {**self.record('2026年09月08日 09:42'), 'title': record['title']}
        self.assertIsNone(match_record([future], 'test', self.attempt, 'caption', ['cat', 'pets']))

    def test_ambiguous_duplicates_are_not_success(self):
        self.assertIsNone(match_record([self.record(), self.record()], 'test', self.attempt))

    def test_review_and_failure_are_distinct(self):
        for label, state in [('审核中', 'PENDING_REVIEW'), ('审核未通过', 'FAILED')]:
            self.assertEqual(match_record([self.record(status=label)], 'test', self.attempt)['state'], state)

    def test_missing_time_or_status_is_unknown(self):
        for record in [dict(title='test', text='已发布'), self.record(status='播放')]:
            self.assertIsNone(match_record([record], 'test', self.attempt))


class DouyinVerifyResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_verify_result_passes_frozen_tags_and_records_only_full_match(self):
        plan = dict(title='test', description='caption', tags=['cat', 'pets'], account_id='fixture')
        task = dict(plan=plan, attempted_at=DouyinResultTests.attempt, state='UNKNOWN_RESULT')
        for caption, success in [('test caption #cat #pets', True), ('test caption', False)]:
            with self.subTest(caption=caption):
                ledger = MagicMock()
                ledger.path = 'fixture.sqlite3'
                ledger.get.side_effect = [task, {**task, 'state': 'PUBLISHED'}]
                page = MagicMock()
                page.goto = AsyncMock()
                page.locator.return_value.first.wait_for = AsyncMock()
                page.screenshot = AsyncMock()
                records = [dict(title=caption, text=caption + '\n2026年09月08日 09:31\n已发布')]
                with patch('utils.douyin_publish_result.read_records', AsyncMock(return_value=records)):
                    if success:
                        result = await verify_result(page, ledger, 'fixture-task', max_checks=1)
                        self.assertEqual(result['state'], 'PUBLISHED')
                        ledger.record_dom_result.assert_called_once()
                        self.assertEqual(ledger.record_dom_result.call_args.args[:2], ('fixture-task', 'PUBLISHED'))
                        page.screenshot.assert_awaited_once()
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'UNKNOWN_RESULT'):
                            await verify_result(page, ledger, 'fixture-task', max_checks=1)
                        ledger.record_dom_result.assert_not_called()
                        page.screenshot.assert_not_awaited()
                page.get_by_role.assert_not_called()


if __name__ == '__main__':
    unittest.main()
