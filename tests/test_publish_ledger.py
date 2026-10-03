import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from utils.publish_ledger import PublishLedger


class PublishLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'ledger.sqlite3'
        self.ledger = PublishLedger(self.path)
        self.plan = dict(channel='douyin', account_id='local-account', title='test', media_sha256='abc')

    def test_concurrent_repeated_requests_create_one_task(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            tasks = list(pool.map(lambda _: self.ledger.submit('same-key', self.plan), range(16)))
        self.assertEqual(len({t['id'] for t in tasks}), 1)

    def test_same_key_changed_plan_rejected(self):
        self.ledger.submit('key', self.plan)
        with self.assertRaisesRegex(ValueError, 'IDEMPOTENCY_CONFLICT'):
            self.ledger.submit('key', {**self.plan, 'title': 'changed'})

    def test_different_key_same_frozen_plan_returns_original(self):
        first = self.ledger.submit('first', self.plan)
        second = self.ledger.submit('second', self.plan)
        self.assertEqual(first['id'], second['id'])
        with self.assertRaisesRegex(ValueError, 'IDEMPOTENCY_CONFLICT'):
            self.ledger.submit('second', {**self.plan, 'title': 'changed'})

    def test_crash_after_guard_blocks_second_click_after_restart(self):
        task = self.ledger.submit('key', self.plan)
        self.ledger.before_click(task['id'])
        reopened = PublishLedger(self.path)
        self.assertEqual(reopened.get(task['id'])['state'], 'UNKNOWN_RESULT')
        with self.assertRaisesRegex(RuntimeError, 'DUPLICATE_SUBMISSION_BLOCKED'):
            reopened.before_click(task['id'])

    def test_two_workers_cannot_both_click(self):
        task = self.ledger.submit('key', self.plan)
        def claim(_):
            try:
                self.ledger.before_click(task['id'])
                return True
            except RuntimeError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(claim, range(2))), 1)

    def evidence(self):
        return dict(channel='douyin', account_id='local-account', title='test', observed_at='2026-09-08T12:00:00+08:00',
                    management_url='https://creator.douyin.com/creator-micro/content/manage',
                    matched_by='title_and_publish_time', status_text='审核中')

    def test_review_does_not_count_as_published_or_allow_retry(self):
        task = self.ledger.submit('key', self.plan)
        self.ledger.before_click(task['id'])
        self.ledger.record_dom_result(task['id'], 'PENDING_REVIEW', self.evidence())
        self.assertEqual(self.ledger.get(task['id'])['state'], 'PENDING_REVIEW')
        with self.assertRaises(RuntimeError):
            self.ledger.before_click(task['id'])

    def test_title_only_and_wrong_account_evidence_rejected(self):
        task = self.ledger.submit('key', self.plan)
        self.ledger.before_click(task['id'])
        for override in ({'matched_by': 'title'}, {'account_id': 'other-account'}):
            with self.assertRaises(ValueError):
                self.ledger.record_dom_result(task['id'], 'PUBLISHED', {**self.evidence(), **override})


if __name__ == '__main__':
    unittest.main()
