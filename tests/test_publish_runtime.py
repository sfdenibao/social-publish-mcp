import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from contextlib import asynccontextmanager
import threading

from utils.publish_ledger import PublishLedger
from utils.publish_sessions import SessionManager
from utils.publish_worker import PublishWorker
from utils.runtime_lock import FileLock


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = PublishLedger(self.root/'test.sqlite3')
        self.sessions = SessionManager(self.root, self.ledger)
        (self.root/'accounts.json').write_text(json.dumps([dict(id='a',channel='douyin',display_name='test',profile=str(self.root/'profile'))]))
        self.execute, self.reconcile = AsyncMock(), AsyncMock()
        self.worker = PublishWorker(self.ledger, self.execute, self.reconcile, self.sessions)

    def task(self, key='one', account='a'):
        return self.ledger.submit(key, dict(account_id=account,channel='douyin',title=key))

    def due(self, task):
        with self.ledger.connect() as db:
            db.execute('UPDATE task_runtime SET next_check=0 WHERE task_id=?',(task['id'],))

    async def test_restart_only_reconciles_after_click(self):
        task=self.task()
        self.ledger.before_click(task['id'])
        self.ledger.recover()
        await self.worker.tick()
        self.execute.assert_not_awaited()
        self.reconcile.assert_awaited_once()
        self.assertEqual(self.ledger.get(task['id'])['state'],'UNKNOWN_RESULT')

    async def test_restart_recovers_preparation(self):
        task=self.task()
        self.ledger.claim_next()
        self.ledger.recover()
        self.assertEqual(self.ledger.get(task['id'])['state'],'QUEUED')
        await self.worker.tick()
        self.execute.assert_awaited_once()

    async def test_busy_backoff_does_not_starve_other_account(self):
        first=self.task()
        second=self.task('two','b')
        self.ledger.account_status('a',dict(status='READY'))
        self.execute.side_effect=RuntimeError('PROFILE_BUSY')
        await self.worker.tick()
        await self.worker.tick()
        self.assertEqual([call.args[0]['id'] for call in self.execute.await_args_list],[first['id'],second['id']])
        self.assertEqual(self.ledger.get(first['id'])['state'],'WAITING_FOR_ACCOUNT')

    async def test_mismatch_waits_until_identity_ready(self):
        task=self.task()
        self.execute.side_effect=RuntimeError('ACCOUNT_MISMATCH')
        await self.worker.tick()
        await self.worker.tick()
        self.assertEqual(self.execute.await_count,1)
        self.assertEqual(self.ledger.get(task['id'])['state'],'WAITING_FOR_LOGIN')
        self.ledger.account_status('a',dict(status='READY'))
        self.execute.side_effect=None
        await self.worker.tick()
        self.assertEqual(self.execute.await_count,2)

    async def test_auto_checks_are_bounded(self):
        task=self.task()
        self.ledger.before_click(task['id'])
        self.reconcile.side_effect=RuntimeError('UNKNOWN_RESULT')
        for _ in range(12):
            self.due(task)
            await self.worker.tick()
        self.assertEqual(self.reconcile.await_count,10)
        self.execute.assert_not_awaited()
        self.assertFalse(self.ledger.diagnostics(task['id'])['runtime']['error']['retryable'])

    async def test_timing_records_failure_without_secret(self):
        task=self.task()
        with self.assertRaises(RuntimeError):
            with self.ledger.timed(task['id'],'ACCOUNT_CHECK'):
                raise RuntimeError('do not persist raw credentials')
        events=self.ledger.diagnostics(task['id'])['events']
        self.assertEqual([e['event'] for e in events],['START','ERROR'])
        self.assertIsNotNone(events[-1]['elapsed_ms'])
        self.assertNotIn('credentials',json.dumps(events))

    async def test_lock_blocks_other_process_and_releases(self):
        path=self.root/'profile.lock'
        code="from utils.runtime_lock import FileLock; import sys;\nwith FileLock(sys.argv[1]): pass"
        with FileLock(path):
            result=await asyncio.to_thread(subprocess.run,[sys.executable,'-c',code,str(path)],capture_output=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn(b'PROFILE_BUSY',result.stderr)
        result=await asyncio.to_thread(subprocess.run,[sys.executable,'-c',code,str(path)],capture_output=True)
        self.assertEqual(result.returncode,0)

    async def test_login_releases_profile_before_ready(self):
        task=self.task()
        self.ledger.delay(task['id'],'LOGIN_REQUIRED',seconds=0,state='WAITING_FOR_LOGIN')
        released=[]
        context=MagicMock()
        page=MagicMock();page.goto=AsyncMock()
        context.new_page=AsyncMock(return_value=page)
        @asynccontextmanager
        async def opened(*args,**kwargs):
            yield context
            self.assertEqual(self.ledger.get(task['id'])['state'],'WAITING_FOR_LOGIN')
            released.append(True)
        self.sessions.open=opened
        self.sessions.identity=AsyncMock(return_value='READY')
        self.sessions.publish_page=AsyncMock(return_value={'status':'READY'})
        self.sessions.probe=AsyncMock(return_value={'status':'READY'})
        self.sessions.logins['test']=dict(account_id='a',status='STARTING',cancel=threading.Event(),refresh=threading.Event(),qr=b'private')
        await self.sessions._login('test',False)
        self.assertTrue(released)
        self.assertEqual(self.sessions.login_status('test')['status'],'READY')
        self.assertNotIn('qr',self.sessions.login_status('test'))
        self.assertEqual(self.ledger.get(task['id'])['state'],'QUEUED')

    async def test_shared_submit_timeout_cannot_click_twice(self):
        from utils.mcp_publish_adapters import submit_once
        task=self.task()
        uploader=MagicMock(publish_ledger=self.ledger,publish_task_id=task['id'])
        uploader.upload_started=__import__('time').perf_counter()
        uploader.before_submit_check=None
        page=MagicMock();button=MagicMock()
        button.wait_for=AsyncMock();button.is_enabled=AsyncMock(return_value=True)
        button.click=AsyncMock(side_effect=TimeoutError())
        page.get_by_role.return_value=button
        checkpage=MagicMock();checkpage.close=AsyncMock()
        page.context.new_page=AsyncMock(return_value=checkpage)
        verifier=AsyncMock(side_effect=RuntimeError('UNKNOWN_RESULT'))
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError,'UNKNOWN_RESULT'):
                await submit_once(uploader,page,'发布',verifier)
        button.click.assert_awaited_once()
        self.assertEqual(verifier.await_count,2)

    async def test_login_waits_for_publish_page_security_verification(self):
        page=MagicMock();page.goto=AsyncMock();page.frames=[]
        context=MagicMock();context.new_page=AsyncMock(return_value=page)
        released=[]
        @asynccontextmanager
        async def opened(*args,**kwargs):
            yield context
            released.append(True)
        self.sessions.open=opened
        self.sessions.identity=AsyncMock(return_value='READY')
        self.sessions.publish_page=AsyncMock(return_value={'status':'SECURITY_VERIFICATION_REQUIRED'})
        self.sessions.probe=AsyncMock(return_value={'status':'READY'})
        self.sessions.upload_input_ready=AsyncMock(return_value=True)
        observed=[]
        async def blocker(page):
            observed.append(self.sessions.login_status('test')['status'])
            return None  # User has completed the visible challenge.
        self.sessions.page_blocker=blocker
        self.sessions.logins['test']=dict(account_id='a',status='STARTING',cancel=threading.Event(),refresh=threading.Event(),qr=None)
        await self.sessions._login('test',False)
        self.assertEqual(observed,['SECURITY_VERIFICATION_REQUIRED'])
        self.assertEqual(self.sessions.login_status('test')['status'],'READY')
        self.assertTrue(released)

    async def test_home_identity_does_not_imply_publish_ready(self):
        self.sessions.probe=AsyncMock(return_value={'status':'READY'})
        self.sessions.publish_page=AsyncMock(return_value={'status':'PUBLISH_PAGE_UNAVAILABLE'})
        result=await self.sessions.preflight_context(MagicMock(),'a')
        self.assertEqual(result['status'],'PUBLISH_PAGE_UNAVAILABLE')

    async def test_identity_ignores_work_text_with_account_name(self):
        import os
        from patchright.async_api import async_playwright
        from utils.publish_sessions import HOME
        chrome=os.environ.get('SAU_CHROME_PATH','C:/Program Files/Google/Chrome/Application/chrome.exe')
        if not Path(chrome).is_file():
            self.skipTest('Installed Chrome required for local DOM fixture')
        async with async_playwright() as p:
            browser=await p.chromium.launch(executable_path=chrome,headless=True)
            try:
                for channel,html in {
                    'douyin':'<div class="header-test"><div class="name-test">OTHER</div></div>',
                    'wechat_channels':'<div class="account-info"><span class="name">OTHER</span></div>',
                    'xiaohongshu':'<div class="d-topbar"><div class="user-info"><span class="name-box">OTHER</span></div></div>',
                }.items():
                    page=await browser.new_page()
                    await page.route('**/*',lambda route:route.fulfill(body=html+'<article>test</article>',content_type='text/html'))
                    await page.goto(HOME[channel])
                    account=dict(channel=channel,display_name='test')
                    self.assertEqual(await self.sessions.identity(page,account),'VERIFYING_ACCOUNT')
                    await page.set_content(html.replace('OTHER','test'))
                    self.assertEqual(await self.sessions.identity(page,account),'READY')
                    await page.close()
            finally:
                await browser.close()


if __name__=='__main__':
    unittest.main()
