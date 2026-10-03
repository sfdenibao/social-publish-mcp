"""Process-owned durable queue and bounded read-only reconciliation."""
import asyncio
import threading
import time

from utils.publish_errors import safe_execution_error
from utils.publish_dispatch import PublishDispatch, SCHEDULING


class PublishWorker:
    def __init__(self, ledger, execute, reconcile, sessions, max_parallel=None):
        self.ledger,self.execute,self.reconcile,self.sessions=ledger,execute,reconcile,sessions
        self.stop=threading.Event()
        self.mutation_lock=threading.Lock()
        self.thread=None
        self.last_tick=None
        self.active_task=None
        self.last_error=None
        self.batches=None
        self.dispatch=PublishDispatch(sessions, self.mutation_lock, max_parallel)

    def start(self):
        self.ledger.recover()
        if self.batches:
            self.batches.recover()
        self.thread=threading.Thread(target=lambda:asyncio.run(self.run()),daemon=True,name='publish-worker')
        self.thread.start()

    async def _recheck(self, task):
        try:
            await self.reconcile(task)
            state=self.ledger.get(task['id'])['state']
            if state not in {'PUBLISHED','FAILED'}:
                count=self.ledger.diagnostics(task['id'])['runtime'].get('check_count',0)+1
                self.ledger.delay(task['id'],state,seconds=30 if count<10 else 0,counted=True)
        except Exception as exc:
            runtime=self.ledger.diagnostics(task['id'])['runtime']
            count=runtime.get('check_count',0)+1
            busy=str(exc)=='PROFILE_BUSY'
            self.ledger.delay(task['id'],'PROFILE_BUSY' if busy else 'UNKNOWN_RESULT',
                seconds=15 if busy or count<10 else 0,counted=not busy)
        finally:
            if self.batches:
                with self.ledger.connect() as db:
                    ids=[r['batch_id'] for r in db.execute('SELECT batch_id FROM batch_tasks WHERE task_id=?',(task['id'],))]
                for batch_id in ids:
                    self.batches.refresh(batch_id)

    async def _execute(self, task):
        self.ledger.event(task['id'],'TASK','START')
        try:
            await self.execute(task)
        except Exception as exc:
            current=self.ledger.get(task['id'])
            known={'PROFILE_BUSY','LOGIN_REQUIRED','ACCOUNT_MISMATCH','CHECK_FAILED','MEDIA_CHANGED_AFTER_PREPARE',
                   'PUBLISH_BUTTON_DISABLED','ACCOUNT_SESSION_NOT_CONFIGURED','SESSION_FILE_MISSING'}
            diagnostic=safe_execution_error(exc, known)
            code=diagnostic['code']
            if current['attempted_at']:
                if current['state'] not in {'PUBLISHED','FAILED'}:
                    self.ledger.delay(task['id'],'UNKNOWN_RESULT',seconds=5)
            elif code=='PROFILE_BUSY':
                self.ledger.delay(task['id'],code,seconds=5,state='WAITING_FOR_ACCOUNT')
            elif code in {'LOGIN_REQUIRED','ACCOUNT_MISMATCH'}:
                self.ledger.delay(task['id'],code,seconds=0,state='WAITING_FOR_LOGIN')
                if code=='LOGIN_REQUIRED':
                    self.sessions.start_login(task['plan']['account_id'])
            elif code=='CHECK_FAILED' and self.ledger.diagnostics(task['id'])['runtime'].get('check_count',0)<2:
                self.ledger.delay(task['id'],code,seconds=10,state='WAITING_FOR_ACCOUNT',counted=True)
            else:
                self.ledger.preparation_failed(task['id'])
                self.ledger.delay(task['id'],code,seconds=0,diagnostic=diagnostic)
            self.ledger.event(task['id'],diagnostic['stage'] if not current['attempted_at'] else 'TASK',code)
        else:
            current=self.ledger.get(task['id'])
            if current['attempted_at'] and current['state'] not in {'PUBLISHED','FAILED'}:
                self.ledger.delay(task['id'],'PENDING_REVIEW',seconds=15)
            self.ledger.event(task['id'],'TASK','END')
        finally:
            pass

    def _claim_standalone(self, task):
        with self.ledger.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            result=db.execute("""UPDATE publish_attempts SET state='PREPARING'
                WHERE id=? AND attempted_at IS NULL AND state IN ('QUEUED','WAITING_FOR_ACCOUNT')
                AND COALESCE((SELECT next_check FROM task_runtime WHERE task_id=?),0)<=?
                AND NOT EXISTS(SELECT 1 FROM batch_tasks WHERE task_id=?)""",
                (task['id'],task['id'],time.time(),task['id']))
            return result.rowcount==1

    async def tick(self, *, wait=True):
        self.last_tick=time.time()
        self.dispatch.reap()
        # Login workers release their profile before recording READY. Do not
        # revive a prior READY check after a later login failure.
        for account in self.sessions.accounts():
            status=self.ledger.account_status(account['id'])
            if status['status']=='READY':
                with self.ledger.connect() as db:
                    rows=db.execute("""SELECT p.id,r.updated_at FROM publish_attempts p
                        LEFT JOIN task_runtime r ON r.task_id=p.id
                        WHERE p.state='WAITING_FOR_LOGIN' AND p.attempted_at IS NULL AND
                        json_extract(p.plan,'$.account_id')=?""",(account['id'],)).fetchall()
                if any(not row['updated_at'] or not status.get('checked_at') or
                       status['checked_at']>=row['updated_at'] for row in rows):
                    self.ledger.resume_account(account['id'])
        # Prefer never-started authorized targets. Slow read-only checks run in
        # the same bounded pool and cannot make other profiles wait for them.
        if self.batches:
            await self.batches.tick(self.execute, self.dispatch)
        for task in self.ledger.due_checks(limit=1000):
            self.dispatch.start(task, lambda task=task: self._recheck(task), kind='reconcile')
        with self.ledger.connect() as db:
            rows=db.execute("""SELECT p.* FROM publish_attempts p LEFT JOIN task_runtime r ON r.task_id=p.id
                WHERE p.state IN ('QUEUED','WAITING_FOR_ACCOUNT') AND p.attempted_at IS NULL
                AND COALESCE(r.next_check,0)<=? AND NOT EXISTS (SELECT 1 FROM batch_tasks b WHERE b.task_id=p.id)
                ORDER BY p.created_at""",(time.time(),)).fetchall()
        for row in rows:
            task=self.ledger._decode(row)
            self.dispatch.start(task, lambda task=task: self._execute(task),
                claim=lambda task=task: self._claim_standalone(task))
        if wait:
            await self.dispatch.drain()

    async def run(self):
        try:
            while not self.stop.is_set():
                try:
                    await self.tick(wait=False)
                    self.last_error=self.dispatch.last_error
                except Exception as exc:
                    self.last_error=type(exc).__name__
                await asyncio.sleep(.5)
        finally:
            # Cancellation never resets a durable attempted_at. Recovery can
            # only requeue PREPARING tasks that never reached before_click.
            await self.dispatch.cancel()

    def status(self):
        active=self.dispatch.status()
        return dict(running=bool(self.thread and self.thread.is_alive()),last_tick=self.last_tick,
            active_task=active[0]['task_id'] if active else None,
            active_batch=next((entry['batch_id'] for entry in active if entry['batch_id']),None),
            active_tasks=active,max_parallel=self.dispatch.max_parallel,last_error=self.last_error,**SCHEDULING)
