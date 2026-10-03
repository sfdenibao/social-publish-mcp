"""Durable deletion receipts, atomic safety and real SDK roundtrip with an inert service."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from utils.publish_batches import PublishBatches
from utils.publish_delete import delete_publish, deletion_receipt
from utils.publish_ledger import PublishLedger


class DeletionReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.ledger=PublishLedger(self.root/'test.db')
        self.plan=dict(channel='douyin',account_id='a',media_sha256='hash',title='test',declaration='')
        with self.ledger.connect() as db:
            db.execute('CREATE TABLE frozen_plans(id TEXT PRIMARY KEY, body TEXT)')
            for pid in ('plan-one','same-content-alias'):
                db.execute('INSERT INTO frozen_plans VALUES (?,?)',(pid,json.dumps(self.plan)))
        self.batches=PublishBatches(self.ledger,None,None)

    def rows(self):
        with self.ledger.connect() as db:
            return {table:[tuple(row) for row in db.execute('SELECT * FROM '+table)] for table in
                ('publish_attempts','publish_batches','batch_tasks','batch_keys','publish_keys','task_events',
                 'task_runtime','frozen_plans','publish_deletions','publish_deletion_aliases')}

    def test_receipt_and_full_snapshot_survive_restart_and_all_exact_aliases(self):
        batch=self.batches.submit('key',self.plan);tid=batch['tasks'][0]['id']
        self.ledger.event(tid,'DECLARATION','UNSUPPORTED_DECLARATION')
        before=self.rows();receipt=delete_publish(self.ledger,batch['id'])
        self.assertEqual(receipt['state'],'DELETED');self.assertTrue(receipt['deleted'])
        self.assertEqual(receipt['mode'],'never_submitted');self.assertFalse(receipt['remote_content_deleted'])
        self.assertEqual(set(receipt['deleted_plan_ids']),{'plan-one','same-content-alias'})
        restarted=PublishLedger(self.root/'test.db')
        for alias in [batch['id'],tid,'plan-one','same-content-alias']:
            self.assertEqual(deletion_receipt(restarted,alias),receipt)
            self.assertEqual(delete_publish(restarted,alias),receipt)
        with restarted.connect() as db:
            audit=db.execute('SELECT audit FROM publish_deletions').fetchone()['audit']
            self.assertEqual(hashlib.sha256(audit.encode()).hexdigest(),receipt['snapshot_sha256'])
            snapshot=json.loads(audit)
            self.assertEqual(snapshot['batch_keys'][0]['key'],'key')
            self.assertEqual(snapshot['tasks'][0]['plan'],before['publish_attempts'][0][3])
            self.assertTrue(any(e['event']=='UNSUPPORTED_DECLARATION' for e in snapshot['task_events']))
            self.assertEqual(db.execute('SELECT count(*) FROM publish_deletions').fetchone()[0],1)

    def test_missing_target_never_creates_a_deleted_receipt(self):
        before=self.rows();result=delete_publish(self.ledger,'missing')
        self.assertFalse(result['deleted']);self.assertEqual(result['state'],'NOT_FOUND')
        self.assertIsNone(deletion_receipt(self.ledger,'missing'));self.assertEqual(self.rows(),before)

    def test_audit_insert_failure_rolls_back_all_deletions(self):
        batch=self.batches.submit('key',self.plan)
        with self.ledger.connect() as db:
            db.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON publish_deletions BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        before=self.rows()
        with self.assertRaises(sqlite3.IntegrityError):delete_publish(self.ledger,batch['id'])
        self.assertEqual(self.rows(),before)

    def test_unknown_review_evidence_or_click_without_timestamp_still_rejected(self):
        batch=self.batches.submit('key',self.plan);tid=batch['tasks'][0]['id']
        for state in ('UNKNOWN_RESULT','SUBMITTED','PENDING_REVIEW','PUBLISHED'):
            with self.subTest(state=state):
                with self.ledger.connect() as db:db.execute('UPDATE publish_attempts SET state=?,attempted_at=NULL WHERE id=?',(state,tid))
                before=self.rows()
                with self.assertRaisesRegex(ValueError,'SUBMITTED_TASK_DELETE_FORBIDDEN'):delete_publish(self.ledger,batch['id'])
                self.assertEqual(self.rows(),before)
        with self.ledger.connect() as db:db.execute("UPDATE publish_attempts SET state='FAILED',evidence='{}' WHERE id=?",(tid,))
        with self.assertRaisesRegex(ValueError,'SUBMITTED_TASK_DELETE_FORBIDDEN'):delete_publish(self.ledger,batch['id'])
        with self.ledger.connect() as db:db.execute('UPDATE publish_attempts SET evidence=NULL WHERE id=?',(tid,))
        self.ledger.event(tid,'SUBMIT_CLICK','START')
        before=self.rows()
        with self.assertRaisesRegex(ValueError,'SUBMITTED_TASK_DELETE_FORBIDDEN'):delete_publish(self.ledger,batch['id'])
        self.assertEqual(self.rows(),before)

    def test_unknown_runtime_and_busy_parent_without_timestamp_rejected(self):
        batch=self.batches.submit('key',self.plan);tid=batch['tasks'][0]['id']
        self.ledger.delay(tid,'UNKNOWN_RESULT',seconds=0)
        with self.assertRaisesRegex(ValueError,'SUBMITTED_TASK_DELETE_FORBIDDEN'):delete_publish(self.ledger,batch['id'])
        with self.ledger.connect() as db:db.execute('UPDATE task_runtime SET error=NULL WHERE task_id=?',(tid,))
        self.batches.update(batch['id'],'WAITING_RESULT')
        with self.assertRaisesRegex(ValueError,'SUBMITTED_TASK_DELETE_FORBIDDEN'):delete_publish(self.ledger,batch['id'])

    def test_incomplete_or_wrong_owned_task_plan_block_atomic_deletion(self):
        batch=self.batches.submit('key',{'items':[self.plan,{**self.plan,'channel':'xiaohongshu','account_id':'b'}]})
        tid=batch['tasks'][1]['id']
        with self.ledger.connect() as db:db.execute('DELETE FROM batch_tasks WHERE task_id=?',(tid,))
        before=self.rows()
        with self.assertRaisesRegex(ValueError,'INCOMPLETE_BATCH_DELETE_FORBIDDEN'):delete_publish(self.ledger,batch['id'])
        self.assertEqual(self.rows(),before)

    def test_parallel_repeat_delete_returns_one_receipt_and_never_touches_new_task(self):
        batch=self.batches.submit('key',self.plan)
        with ThreadPoolExecutor(4) as pool:
            results=list(pool.map(lambda _:delete_publish(self.ledger,batch['id']),range(8)))
        self.assertTrue(all(r==results[0] for r in results))
        new=self.batches.submit('key',self.plan)
        self.assertNotEqual(new['id'],batch['id'])
        self.assertNotEqual(new['tasks'][0]['id'],batch['tasks'][0]['id'])
        self.assertEqual(delete_publish(self.ledger,batch['id']),results[0])
        self.assertEqual(self.batches.get(new['id'])['state'],'PREFLIGHT_PENDING')

    def test_prepared_only_plan_receipt_is_readable_without_creating_tasks(self):
        receipt=delete_publish(self.ledger,'plan-one')
        self.assertEqual(receipt['deleted_task_ids'],[]);self.assertEqual(receipt['deleted_batch_ids'],[])
        self.assertEqual(deletion_receipt(self.ledger,'same-content-alias'),receipt)
        self.assertEqual(self.rows()['publish_attempts'],[])


BOOT = """
import json
import publish_mcp as s
(s.RUNTIME/'accounts.json').write_text(json.dumps([dict(id='fixture',channel='douyin',display_name='fixture',profile=str(s.RUNTIME/'profile'))]))
async def preflight(account_id):
    return dict(status='READY',publish_input_verified=True)
s.sessions.preflight=preflight
# No worker starts: this fixture cannot upload, login, or click any platform.
s.mcp.run(transport='streamable-http')
"""


class DeletionSDKTests(unittest.TestCase):
    def test_real_sdk_delete_repeat_readonly_recovery_and_same_media_new_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            env={**os.environ,'SAU_RUNTIME_DIR':temp,'SAU_MCP_PORT':str(port)}
            process=subprocess.Popen([sys.executable,'-c',BOOT],cwd=Path(__file__).resolve().parents[1],env=env,
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                for _ in range(100):
                    if process.poll() is not None:self.fail('inert server exited')
                    try:
                        with socket.create_connection(('127.0.0.1',port),timeout=.2):break
                    except OSError:time.sleep(.1)
                else:self.fail('inert server not ready')
                media=Path(temp)/'inert.mp4';media.write_bytes(b'no actual video upload')
                async def run():
                    async with streamable_http_client(f'http://127.0.0.1:{port}/mcp') as (read,write,_):
                        async with ClientSession(read,write) as client:
                            await client.initialize()
                            async def call(name,args):
                                result=await client.call_tool(name,args)
                                self.assertFalse(result.isError,str(result.content))
                                return json.loads(result.content[0].text)
                            names={t.name for t in (await client.list_tools()).tools}
                            self.assertTrue({'delete_publish_task','get_publish_status','get_publish_batch','recover_uncertain_publish'} <= names)
                            self.assertNotIn('cancel_publish_batch',names)
                            args=dict(media_path=str(media),title='test',description='',declaration='',tags=[],channels=['douyin'])
                            oldplan=await call('prepare_publish',args)
                            submitted=await call('submit_publish',dict(plan_id=oldplan['plan_id'],idempotency_key='old'))
                            bid=submitted['batch_id'];tid=submitted['task_id']
                            deleted=await call('delete_publish_task',dict(target_id=bid))
                            self.assertTrue(deleted['deleted']);self.assertEqual(deleted['state'],'DELETED')
                            self.assertEqual(await call('delete_publish_task',dict(target_id=bid)),deleted)
                            for name,arg in [('get_publish_batch',{'batch_id':bid}),('get_publish_status',{'task_id':tid}),('get_publish_status',{'task_id':oldplan['plan_id']})]:
                                self.assertEqual(await call(name,arg),deleted)
                            stale=await client.call_tool('submit_publish',dict(plan_id=oldplan['plan_id'],idempotency_key='old'))
                            self.assertTrue(stale.isError)
                            missing=await call('delete_publish_task',dict(target_id='unrecorded-old-id'))
                            self.assertEqual(missing['state'],'NOT_FOUND')
                            newplan=await call('prepare_publish',{**args,'title':'new title'})
                            checked=await call('preflight_publish',{'plan_id':newplan['plan_id']})
                            self.assertTrue(checked['ready'])
                            new=await call('submit_publish',dict(plan_id=newplan['plan_id'],idempotency_key='new'))
                            self.assertNotEqual(new['batch_id'],bid);self.assertNotEqual(new['task_id'],tid)
                            state=await call('get_publish_batch',{'batch_id':new['batch_id']})
                            self.assertIsNone(state['tasks'][0]['attempted_at'])
                            self.assertEqual(state['tasks'][0]['plan']['title'],'new title')
                            self.assertEqual(await call('get_publish_batch',{'batch_id':bid}),deleted)
                asyncio.run(run())
            finally:process.terminate();process.wait(timeout=10)


if __name__=='__main__':unittest.main()
