import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from utils.publish_batches import PublishBatches, ORDER
from utils.publish_ledger import PublishLedger, now


class BatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.ledger=PublishLedger(self.root/'test.db')
        media=self.root/'test.mp4';media.write_bytes(b'isolated fixture')
        self.items=[dict(channel=c,account_id=c,title='test',description='',tags=[],declaration='',
            media_path=str(media),media_sha256=hashlib.sha256(media.read_bytes()).hexdigest()) for c in ORDER]
        self.states={c:'READY' for c in ORDER};self.events=[];self.clicks=[];self.logins={}
        async def check(a):
            self.events.append(('check',a));return dict(status=self.states[a],publish_input_verified=self.states[a]=='READY')
        def start(a):
            self.logins[a]=dict(status='WAITING_FOR_SCAN');return a
        self.sessions=SimpleNamespace(preflight=check,account=lambda a:dict(channel=a),logins=self.logins,
            start_login=start,login_status=lambda sid:self.logins[sid])
        async def reconcile(task,force=False):
            self.events.append(('recheck',task['plan']['channel']))
            return dict(state='PUBLISHED',evidence=task['evidence'])
        self.batches=PublishBatches(self.ledger,self.sessions,reconcile)

    def submit(self,items=None,key='one'):
        return self.batches.submit(key,dict(items=items or self.items))

    async def execute(self,task):
        self.events.append(('execute',task['plan']['channel']))
        self.ledger.before_click(task['id']);self.clicks.append(task['plan']['channel'])
        self.publish(task)

    def publish(self,task):
        self.ledger.record_dom_result(task['id'],'PUBLISHED',dict(channel=task['plan']['channel'],
            account_id=task['plan']['account_id'],title=task['plan']['title'],observed_at=now(),
            management_url='https://example.invalid',matched_by='new_work_id',status_text='fixture'))

    async def tick(self,callback=None):
        with self.ledger.connect() as db:
            db.execute('UPDATE publish_batches SET next_check=0')
        await self.batches.tick(callback or self.execute)

    async def test_logged_out_target_does_not_block_ready_siblings(self):
        self.states[ORDER[-1]]='LOGIN_REQUIRED';batch=self.submit()
        await self.tick()
        self.assertEqual(set(self.clicks),set(ORDER[:-1]))
        self.assertEqual(len([e for e in self.events if e[0]=='check']),3)
        self.assertEqual(self.batches.get(batch['id'])['state'],'WAITING_FOR_LOGIN')
        self.assertIsNone(self.ledger.claim_next())
        with self.assertRaisesRegex(RuntimeError,'BATCH_PREFLIGHT_REQUIRED'):
            self.ledger.before_click(batch['tasks'][-1]['id'])

    async def test_login_completion_rechecks_only_waiting_target(self):
        self.states[ORDER[-1]]='LOGIN_REQUIRED';batch=self.submit();await self.tick()
        self.states[ORDER[-1]]='READY';self.logins[ORDER[-1]]['status']='READY'
        await self.tick();await self.tick();await self.tick()
        self.assertEqual(set(self.clicks),set(ORDER))
        self.assertEqual([e for e in self.events if e==('check',ORDER[0])],[('check',ORDER[0])])
        self.assertEqual(self.batches.get(batch['id'])['state'],'PUBLISHED')

    async def test_cancelled_or_expired_login_does_not_resume(self):
        self.states[ORDER[1]]='LOGIN_REQUIRED';batch=self.submit();await self.tick()
        for state in ('CANCELLED','EXPIRED'):
            self.logins[ORDER[1]]['status']=state
            await self.tick()
            self.assertEqual(set(self.clicks),{ORDER[0],ORDER[2]})
            self.assertEqual(self.batches.get(batch['id'])['state'],'WAITING_FOR_LOGIN')

    async def test_publish_page_failure_isolated_to_target(self):
        self.states[ORDER[1]]='PUBLISH_PAGE_UNAVAILABLE';batch=self.submit();await self.tick()
        self.assertEqual(set(self.clicks),{ORDER[0],ORDER[2]})
        self.assertEqual(self.batches.get(batch['id'])['state'],'FAILED')
        self.assertTrue(self.batches.get(batch['id'])['partial_completed'])

    async def test_upload_login_loss_preserves_completed_siblings(self):
        batch=self.submit()
        async def fail(task):
            if task['plan']['channel']==ORDER[1]:
                raise RuntimeError('LOGIN_REQUIRED')
            await self.execute(task)
        await self.tick(fail)
        self.assertEqual(set(self.clicks),{ORDER[0],ORDER[2]})
        self.assertEqual(self.batches.get(batch['id'])['state'],'WAITING_FOR_LOGIN')
        self.logins[ORDER[1]]['status']='READY'
        await self.tick()
        self.assertEqual(set(self.clicks),set(ORDER))
        self.assertEqual(len(self.clicks),3)

    async def test_uncertain_click_does_not_block_siblings_or_repeat_on_restart(self):
        batch=self.submit()
        async def uncertain(task):
            if task['plan']['channel']!=ORDER[0]:
                return await self.execute(task)
            self.ledger.before_click(task['id']);self.clicks.append(task['plan']['channel'])
            raise TimeoutError()
        await self.tick(uncertain)
        self.ledger.recover();self.batches.recover()
        await self.tick();await self.tick()
        self.assertEqual(set(self.clicks),set(ORDER));self.assertEqual(len(self.clicks),3)
        self.assertEqual(self.batches.get(batch['id'])['state'],'WAITING_RESULT')
        self.publish(self.ledger.get(batch['tasks'][0]['id']))
        await self.tick()
        self.assertEqual(self.batches.get(batch['id'])['state'],'PUBLISHED')
        self.assertEqual(len(self.clicks),3)

    async def test_recreated_plan_and_changed_title_cannot_republish(self):
        original=self.submit()
        await self.tick();await self.tick();await self.tick()
        before=list(self.clicks)
        changed=[{**p,'title':'changed'} for p in self.items]
        duplicate=self.submit(changed,key='two')
        self.assertTrue(all(not t['owned'] for t in duplicate['tasks']))
        await self.tick()
        self.assertEqual(self.clicks,before)
        self.assertEqual(self.batches.get(duplicate['id'])['state'],'PUBLISHED')
        self.assertEqual(len([e for e in self.events if e[0]=='recheck']),3)
        self.assertEqual(self.submit(key='new-key')['id'],original['id'])

    async def test_duplicate_pending_cannot_open_second_batch(self):
        first=self.submit()
        other=self.submit([{**p,'title':'other'} for p in self.items],key='two')
        with self.ledger.connect() as db:
            db.execute("UPDATE publish_batches SET state='BLOCKED' WHERE id=?",(first['id'],))
        await self.tick()
        self.assertEqual(set(self.clicks),set(ORDER))
        self.assertEqual(len(self.clicks),3)
        await self.tick()
        self.assertEqual(self.batches.get(other['id'])['state'],'PUBLISHED')

    async def test_preexisting_single_task_dedup_and_missing_dom_blocks(self):
        old=self.ledger.submit('old',self.items[0]);self.ledger.before_click(old['id']);self.publish(old)
        batch=self.submit()
        async def missing(*args,**kwargs):raise RuntimeError('UNKNOWN_RESULT')
        self.batches.reconcile=missing
        await self.tick()
        self.assertEqual(set(self.clicks),set(ORDER[1:]))
        self.assertEqual(self.batches.get(batch['id'])['state'],'BLOCKED')

    async def test_target_preflight_binding_enforced_at_durable_click_boundary(self):
        batch=self.submit(list(reversed(self.items)))
        self.batches.update(batch['id'],'RUNNING',checks=[dict(account_id=c,status='READY',publish_input_verified=True) for c in ORDER])
        with self.assertRaisesRegex(RuntimeError,'BATCH_PREFLIGHT_REQUIRED'):
            self.ledger.before_click(batch['tasks'][1]['id'])

    async def test_media_change_blocks_before_any_upload(self):
        batch=self.submit();Path(self.items[0]['media_path']).write_bytes(b'changed')
        await self.tick()
        self.assertEqual(self.clicks,[])
        self.assertEqual(self.batches.get(batch['id'])['state'],'FAILED')

    async def test_key_conflict_and_single_ledger_media_guard(self):
        self.submit()
        with self.assertRaisesRegex(ValueError,'IDEMPOTENCY_CONFLICT'):
            self.submit([{**p,'title':'new'} for p in self.items])
        with self.assertRaisesRegex(ValueError,'DUPLICATE_MEDIA'):
            self.ledger.submit('legacy',{**self.items[0],'plan_id':'new-random-plan'})

    async def test_completed_account_expiry_does_not_block_remaining_target(self):
        self.states[ORDER[-1]]='LOGIN_REQUIRED';batch=self.submit();await self.tick()
        self.states[ORDER[-1]]='READY';self.logins[ORDER[-1]]['status']='READY'
        self.states[ORDER[0]]='LOGIN_REQUIRED'
        await self.tick()
        self.assertEqual(set(self.clicks),set(ORDER))
        self.assertEqual(self.batches.get(batch['id'])['state'],'PUBLISHED')

    async def test_parallel_recreated_plans_reserve_media_once(self):
        from concurrent.futures import ThreadPoolExecutor
        def submit(index):
            return self.submit([{**p,'title':str(index)} for p in self.items],key=str(index))
        with ThreadPoolExecutor(max_workers=4) as pool:
            batches=list(pool.map(submit,range(8)))
        self.assertEqual(sum(t['owned'] for b in batches for t in b['tasks']),3)
        self.assertEqual(len({t['id'] for b in batches for t in b['tasks']}),3)

    async def test_new_login_session_can_replace_expired_session(self):
        self.states[ORDER[-1]]='LOGIN_REQUIRED';batch=self.submit();await self.tick()
        self.logins[ORDER[-1]]['status']='EXPIRED'
        self.states[ORDER[-1]]='READY'
        self.logins['replacement']=dict(account_id=ORDER[-1],status='READY',finished_at=now())
        self.ledger.account_status(ORDER[-1],dict(status='READY',checked_at=now()))
        await self.tick()
        self.assertEqual(set(self.clicks),set(ORDER))

    async def test_crash_with_running_parent_never_repeats_click(self):
        batch=self.submit([self.items[0]])
        async def crash(task):
            self.ledger.before_click(task['id']);self.clicks.append(task['plan']['channel'])
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.tick(crash)
        self.ledger.recover();self.batches.recover()
        await self.tick()
        self.assertEqual(self.clicks,[ORDER[0]])
        self.assertEqual(self.batches.get(batch['id'])['state'],'WAITING_RESULT')

if __name__=='__main__':unittest.main()
