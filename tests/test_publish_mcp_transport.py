"""Real MCP transport smoke test with an isolated empty account registry."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


BOOT = """
import asyncio,json
import publish_mcp as s
async def fake(task):
    await asyncio.sleep(1)
    s.ledger.before_click(task['id'])
    s.ledger.record_dom_result(task['id'],'PUBLISHED',dict(channel=task['plan']['channel'],account_id=task['plan']['account_id'],title='test',observed_at='test',management_url='https://example.invalid',matched_by='new_work_id',status_text='test fixture'))
s.worker_runtime.execute=fake
ready=False
async def preflight(account_id):
    return dict(status='READY' if ready or account_id!='xiaohongshu' else 'LOGIN_REQUIRED',publish_input_verified=True)
def login(account_id):
    s.sessions.logins['fixture']=dict(status='WAITING_FOR_SCAN')
    return 'fixture'
s.sessions.start_login=login
@s.mcp.tool()
def fixture_finish_login() -> dict:
    global ready
    ready=True
    s.sessions.logins['fixture']['status']='READY'
    return {'ok':True}
s.sessions.preflight=preflight
(s.RUNTIME/'accounts.json').write_text(json.dumps([dict(id=c,channel=c,display_name='test',profile=str(s.RUNTIME/c)) for c in s.ORDER]))
with s.FileLock(s.RUNTIME/'service.lock'):
    s.worker_runtime.start()
    s.mcp.run(transport='streamable-http')
"""


class TransportTests(unittest.TestCase):
    def test_independent_client_lists_and_calls_tools(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            env = {**os.environ, 'SAU_RUNTIME_DIR': temp, 'SAU_MCP_PORT': str(port)}
            process = subprocess.Popen([sys.executable, '-c', BOOT],
                                       cwd=root, env=env, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        self.fail('MCP server exited before listening')
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=.2):
                            break
                    except OSError:
                        time.sleep(.1)
                else:
                    self.fail('MCP server did not listen')

                async def check():
                    async with streamable_http_client(f'http://127.0.0.1:{port}/mcp') as (read, write, _):
                        async with ClientSession(read, write) as client:
                            await client.initialize()
                            names = {t.name for t in (await client.list_tools()).tools}
                            self.assertTrue({'prepare_publish', 'submit_publish', 'get_publish_status'} <= names)
                            result = await client.call_tool('list_publish_channels', {})
                            self.assertFalse(result.isError)
                            missing = await client.call_tool('submit_publish', dict(plan_id='missing', idempotency_key='smoke'))
                            self.assertTrue(missing.isError)
                            media=Path(temp)/'fake.mp4';media.write_bytes(b'isolated test only')
                            plan=await client.call_tool('prepare_publish',dict(media_path=str(media),title='test',description='test',declaration='',tags=[]))
                            plan_id=json.loads(plan.content[0].text)['plan_id']
                            deleted=await client.call_tool('delete_publish_task',dict(target_id=plan_id))
                            self.assertFalse(deleted.isError)
                            self.assertTrue(json.loads(deleted.content[0].text)['deleted'])
                            missing=await client.call_tool('submit_publish',dict(plan_id=plan_id,idempotency_key='deleted'))
                            self.assertTrue(missing.isError)
                            plan=await client.call_tool('prepare_publish',dict(media_path=str(media),title='test，🐱\u200b',description='test',declaration='',tags=[]))
                            cleaned=json.loads(plan.content[0].text)
                            self.assertTrue(cleaned['title_changed'])
                            self.assertEqual(cleaned['title'],'test')
                            self.assertTrue(all(item['title']=='test' for item in cleaned['plan']['items']))
                            plan_id=json.loads(plan.content[0].text)['plan_id']
                            submitted=await client.call_tool('submit_publish',dict(plan_id=plan_id,idempotency_key='isolated'))
                            batch_id=json.loads(submitted.content[0].text)['batch_id']
                    # A second session works after the first client disconnects.
                    async with streamable_http_client(f'http://127.0.0.1:{port}/mcp') as (read, write, _):
                        async with ClientSession(read, write) as client:
                            await client.initialize()
                            result = await client.call_tool('list_accounts', {})
                            self.assertFalse(result.isError)
                            for _ in range(30):
                                status=await client.call_tool('get_publish_batch',dict(batch_id=batch_id))
                                state=json.loads(status.content[0].text)
                                if state['state']=='WAITING_FOR_LOGIN':
                                    break
                                await asyncio.sleep(.2)
                            else:
                                self.fail('Batch did not wait for login')
                            self.assertEqual(state['scheduling_mode'],'parallel')
                            self.assertEqual(state['scheduling_version'],2)
                            self.assertTrue(state['partial_completed'])
                            self.assertTrue(all(t['state']=='PUBLISHED' for t in state['tasks'][:2]))
                            self.assertIsNone(state['tasks'][2]['attempted_at'])
                            self.assertEqual(len(state['checks']),3)
                            await client.call_tool('fixture_finish_login',{})
                            for _ in range(60):
                                status=await client.call_tool('get_publish_batch',dict(batch_id=batch_id))
                                if json.loads(status.content[0].text)['state']=='PUBLISHED':
                                    break
                                await asyncio.sleep(.2)
                            else:
                                self.fail('Worker did not finish after client disconnection')
                            completed=json.loads(status.content[0].text)
                            self.assertEqual([t['plan']['channel'] for t in completed['tasks']],['douyin','wechat_channels','xiaohongshu'])
                            times=[t['attempted_at'] for t in completed['tasks']]
                            self.assertTrue(all(times))
                            self.assertGreaterEqual(times[2],max(times[:2]))
                            health=await client.call_tool('get_service_status',{})
                            self.assertTrue(json.loads(health.content[0].text)['running'])
                asyncio.run(check())
            finally:
                process.terminate()
                process.wait(timeout=15)
                time.sleep(1)


if __name__ == '__main__':
    unittest.main()
