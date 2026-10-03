import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from uploader.douyin_uploader.main import DouYinVideo
from utils.publish_ledger import PublishLedger


class SingleSubmitTests(unittest.TestCase):
    def test_click_timeout_still_verifies_and_never_clicks_again(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = PublishLedger(Path(temp) / 'db.sqlite3')
            task = ledger.submit('key', dict(channel='douyin', account_id='test', title='test'))
            uploader = DouYinVideo('test', 'unused.mp4', [], 0, 'unused.json')
            uploader.publish_ledger = ledger
            uploader.publish_task_id = task['id']
            uploader._clear_blocking_overlays = AsyncMock()
            button = MagicMock()
            button.wait_for = AsyncMock()
            button.is_enabled = AsyncMock(return_value=True)
            button.click = AsyncMock(side_effect=TimeoutError('network timeout after click'))
            page = MagicMock()
            page.get_by_role.return_value = button
            verifier = AsyncMock(side_effect=RuntimeError('UNKNOWN_RESULT'))
            with patch('utils.douyin_publish_result.verify_result', verifier):
                for _ in range(2):
                    with self.assertRaisesRegex(RuntimeError, 'UNKNOWN_RESULT'):
                        asyncio.run(uploader.submit_publish(page))
            button.click.assert_awaited_once()
            self.assertEqual(verifier.await_count, 2)
            self.assertEqual(ledger.get(task['id'])['state'], 'UNKNOWN_RESULT')


if __name__ == '__main__':
    unittest.main()
