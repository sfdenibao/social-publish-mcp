"""Durable independent-platform batches with per-target preflight and single-submit guards."""
import asyncio
import hashlib
import json
import os
import time
import uuid
from utils.publish_ledger import now
from utils.publish_dispatch import PublishDispatch, SCHEDULING, resources, concurrency_limit
from utils.publish_declarations import validate_declaration, validate_plan_declarations
from utils.publish_errors import DeclarationError, safe_execution_error

ORDER = ('douyin', 'wechat_channels', 'xiaohongshu')
LOGIN_ERRORS = {'LOGIN_REQUIRED', 'SECURITY_VERIFICATION_REQUIRED', 'ACCOUNT_MISMATCH'}
SAFE_ERRORS = LOGIN_ERRORS | {'PROFILE_BUSY','CHECK_FAILED','PUBLISH_PAGE_UNAVAILABLE',
    'MEDIA_CHANGED_AFTER_PREPARE','ACCOUNT_CHANNEL_MISMATCH','DUPLICATE_RESULT_UNCONFIRMED'}


def items_of(plan):
    return plan.get('items', [plan])


def canonical(plan):
    return json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class PublishBatches:
    def __init__(self, ledger, sessions, reconcile):
        self.ledger, self.sessions, self.reconcile = ledger, sessions, reconcile
        self.active_batch = None

    def submit(self, key, plan):
        if not key or not key.strip():
            raise ValueError('IDEMPOTENCY_KEY_REQUIRED')
        validate_plan_declarations(plan)
        items = sorted(items_of(plan), key=lambda p: ORDER.index(p['channel']))
        body = canonical({'items': items})
        fingerprint = hashlib.sha256(body.encode()).hexdigest()
        with self.ledger.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT * FROM batch_keys WHERE key=?', (key,)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise ValueError('IDEMPOTENCY_CONFLICT')
                batch_id = prior['batch_id']
            else:
                old = db.execute('SELECT id FROM publish_batches WHERE fingerprint=?', (fingerprint,)).fetchone()
                batch_id = old['id'] if old else uuid.uuid4().hex
                if not old:
                    db.execute('INSERT INTO publish_batches(id,fingerprint,plan,state,created_at) VALUES (?,?,?,?,?)',
                               (batch_id, fingerprint, body, 'PREFLIGHT_PENDING', now()))
                    for index, item in enumerate(items):
                        duplicate = self.ledger.media_match(db, item)
                        owned = duplicate is None
                        task_id = duplicate['id'] if duplicate else uuid.uuid4().hex
                        if owned:
                            frozen = canonical(item)
                            db.execute('INSERT INTO publish_attempts VALUES (?,?,?,?,?,?,?,?)',
                                (task_id, 'batch:'+batch_id+':'+str(index), hashlib.sha256(frozen.encode()).hexdigest(),
                                 frozen, 'QUEUED', now(), None, None))
                        db.execute('INSERT INTO batch_tasks VALUES (?,?,?,?)', (batch_id,index,task_id,int(owned)))
                db.execute('INSERT INTO batch_keys VALUES (?,?,?)', (key,batch_id,fingerprint))
        return self.get(batch_id)

    def get(self, batch_id):
        with self.ledger.connect() as db:
            row = db.execute('SELECT * FROM publish_batches WHERE id=?', (batch_id,)).fetchone()
            if row is None:
                raise KeyError('BATCH_NOT_FOUND')
            result = dict(row)
            children = db.execute('SELECT task_id,position,owned FROM batch_tasks WHERE batch_id=? ORDER BY position',(batch_id,)).fetchall()
        result.pop('fingerprint')
        result['plan'] = json.loads(result['plan'])
        result['checks'] = json.loads(result['checks'])
        for check in result['checks']:
            sid=check.get('login_session_id')
            if sid:
                check['login_url']=f"http://127.0.0.1:{os.environ.get('SAU_MCP_PORT','5410')}/login/{sid}"
                check['login_status']=self.sessions.login_status(sid) if sid in self.sessions.logins else {'status':'SESSION_EXPIRED_AFTER_RESTART'}
        result.update(SCHEDULING)
        result['tasks'] = []
        for child in children:
            task = {**dict(child), **self.ledger.get(child['task_id'])}
            runtime = self.ledger.diagnostics(child['task_id'])['runtime']
            task.update(runtime=runtime, error=runtime.get('error'))
            result['tasks'].append(task)
        states = [t['state'] for t in result['tasks']]
        result['progress'] = dict(total=len(states), published=states.count('PUBLISHED'),
            failed=states.count('FAILED'), waiting_result=sum(bool(t['attempted_at']) and t['state'] not in {'PUBLISHED', 'FAILED'} for t in result['tasks']),
            waiting_login=states.count('WAITING_FOR_LOGIN'), active=states.count('PREPARING'),
            pending=sum(s in {'QUEUED', 'WAITING_FOR_ACCOUNT'} for s in states))
        result['partial_completed'] = 0 < states.count('PUBLISHED') < len(states)
        # Keep the legacy top-level error code, and expose a matching safe
        # diagnostic rather than replacing it with raw browser exception text.
        result['error_detail'] = None
        if result['error']:
            candidates = [t.get('error') for t in result['tasks']]
            candidates += [c.get('error_detail') for c in result['checks'] if c.get('status') != 'READY']
            result['error_detail'] = next((e for e in candidates if isinstance(e, dict)
                and (e.get('code') == result['error'] or result['error'] == 'ALL_ACCOUNTS_MUST_BE_READY')), None)
        return result

    def update(self, batch_id, state, checks=None, error=None, seconds=0):
        with self.ledger.connect() as db:
            before = db.execute('SELECT state,error FROM publish_batches WHERE id=?',(batch_id,)).fetchone()
            db.execute('UPDATE publish_batches SET state=?,error=?,next_check=? WHERE id=?',
                       (state,error,time.time()+seconds if seconds else 1e20,batch_id))
            if checks is not None:
                db.execute('UPDATE publish_batches SET checks=? WHERE id=?',(canonical(checks),batch_id))
        if before and (before['state'] != state or before['error'] != error):
            self.ledger.event(batch_id, 'BATCH', state)

    def recover(self):
        with self.ledger.connect() as db:
            db.execute("UPDATE publish_batches SET state='PREFLIGHT_PENDING',next_check=0 WHERE state IN ('PREFLIGHT','RUNNING','PREFLIGHT_PENDING')")
            for row in db.execute('SELECT id,checks FROM publish_batches').fetchall():
                checks=json.loads(row['checks'])
                retained=[c for c in checks if c.get('status')!='PREFLIGHT_RUNNING']
                if retained!=checks:
                    db.execute('UPDATE publish_batches SET checks=? WHERE id=?',(canonical(retained),row['id']))

    def resume(self, batch_id):
        batch = self.get(batch_id)
        if batch['state']=='PUBLISHED':
            return batch
        validate_plan_declarations(batch['plan'])
        if any(t['state']=='PREPARING' for t in batch['tasks']):
            raise ValueError('BATCH_BUSY')
        with self.ledger.connect() as db:
            for task in batch['tasks']:
                if task['owned'] and not task['attempted_at']:
                    db.execute("UPDATE publish_attempts SET state='QUEUED' WHERE id=?",(task['id'],))
                    db.execute('UPDATE task_runtime SET next_check=0,check_count=0,error=NULL WHERE task_id=?',(task['id'],))
                elif task['attempted_at'] and task['state'] not in {'PUBLISHED','FAILED'}:
                    db.execute('UPDATE task_runtime SET next_check=0,check_count=0,error=NULL WHERE task_id=?',(task['id'],))
            db.execute("UPDATE publish_batches SET state='PREFLIGHT_PENDING',next_check=0,error=NULL WHERE id=?",(batch_id,))
            references={task['id'] for task in batch['tasks'] if not task['owned']}
            checks=[c for c in batch['checks'] if c.get('task_id') not in references or c.get('status')=='READY']
            db.execute('UPDATE publish_batches SET checks=? WHERE id=?',(canonical(checks),batch_id))
        return self.get(batch_id)

    async def inspect_item(self, item, own_task_ids=()):
        started = time.perf_counter()
        result = dict(account_id=item['account_id'], channel=item['channel'])
        try:
            validate_declaration(item['channel'], item.get('declaration', ''))
            result.update(await asyncio.wait_for(self.sessions.preflight(item['account_id']), 75))
            if result['status'] == 'READY' and result.get('publish_input_verified') is not True:
                result['status'] = 'PUBLISH_PAGE_UNAVAILABLE'
            if result['status'] == 'READY':
                def digest_media():
                    with open(item['media_path'], 'rb') as media:
                        return hashlib.file_digest(media, 'sha256').hexdigest()
                if await asyncio.to_thread(digest_media) != item['media_sha256']:
                    raise RuntimeError('MEDIA_CHANGED_AFTER_PREPARE')
                if self.sessions.account(item['account_id'])['channel'] != item['channel']:
                    raise RuntimeError('ACCOUNT_CHANNEL_MISMATCH')
        except DeclarationError as exc:
            result.update(status=exc.code, error_detail=exc.to_dict())
        except Exception as exc:
            result['status'] = str(exc) if str(exc) in SAFE_ERRORS else 'CHECK_FAILED'
        with self.ledger.connect() as db:
            duplicate = self.ledger.media_match(db, item)
        result['duplicate_task_id'] = duplicate['id'] if duplicate else None
        if duplicate and duplicate['id'] not in own_task_ids and result['status'] == 'READY':
            try:
                if duplicate['state'] != 'PUBLISHED':
                    raise RuntimeError('DUPLICATE_RESULT_UNCONFIRMED')
                evidence = await self.reconcile(self.ledger.get(duplicate['id']), force=True)
                if evidence.get('state') != 'PUBLISHED':
                    raise RuntimeError('DUPLICATE_RESULT_UNCONFIRMED')
                result['duplicate_evidence'] = evidence['evidence']
            except Exception:
                result['status'] = 'DUPLICATE_RESULT_UNCONFIRMED'
        result.update(checked_at=now(), elapsed_ms=round((time.perf_counter()-started)*1000, 2))
        return result

    async def inspect(self, plan, own_task_ids=()):
        # Read-only snapshot can run concurrently too; aliases of one profile
        # must not open two persistent browser contexts.
        semaphore = asyncio.Semaphore(concurrency_limit())
        locks = {}
        async def check(item):
            keys = sorted(resources(self.sessions, {'plan': item}))
            held = [locks.setdefault(key, asyncio.Lock()) for key in keys]
            acquired = []
            try:
                for lock in held:
                    await lock.acquire()
                    acquired.append(lock)
                async with semaphore:
                    return await self.inspect_item(item, own_task_ids)
            finally:
                for lock in reversed(acquired):
                    lock.release()
        return await asyncio.gather(*(check(item) for item in items_of(plan)))

    def _record_check(self, batch_id, task, check):
        # Merge under one transaction: concurrent targets must not overwrite
        # each other's READY/error/login receipts.
        with self.ledger.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT checks FROM publish_batches WHERE id=?', (batch_id,)).fetchone()
            if not row:
                return
            checks = json.loads(row['checks'])
            check = dict(check, task_id=task['id'], plan_fingerprint=task['fingerprint'])
            checks = [c for c in checks if c.get('task_id') != task['id'] and not (
                c.get('task_id') is None and c.get('account_id') == check['account_id']
                and c.get('channel') == check['channel'])]
            checks.append(check)
            positions = {r['task_id']: r['position'] for r in db.execute(
                'SELECT task_id,position FROM batch_tasks WHERE batch_id=?', (batch_id,))}
            checks.sort(key=lambda c: positions.get(c.get('task_id'), 9999))
            db.execute('UPDATE publish_batches SET checks=? WHERE id=?', (canonical(checks), batch_id))

    def _claim(self, batch_id, task):
        with self.ledger.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            result = db.execute("""UPDATE publish_attempts SET state='PREPARING'
                WHERE id=? AND attempted_at IS NULL AND state IN ('QUEUED','WAITING_FOR_ACCOUNT')
                AND COALESCE((SELECT next_check FROM task_runtime WHERE task_id=?),0)<=?
                AND EXISTS (SELECT 1 FROM batch_tasks WHERE batch_id=? AND task_id=? AND owned=1)""",
                (task['id'], task['id'], time.time(), batch_id, task['id']))
            if result.rowcount != 1:
                return False
            db.execute("UPDATE publish_batches SET state='RUNNING',error=NULL WHERE id=?", (batch_id,))
        self._record_check(batch_id, task, dict(account_id=task['plan']['account_id'], channel=task['plan']['channel'],
            status='PREFLIGHT_RUNNING', checked_at=now()))
        return True

    def _login_done(self, check):
        sid = check.get('login_session_id')
        logins = getattr(self.sessions, 'logins', {})
        if sid and sid in logins and self.sessions.login_status(sid)['status'] == 'READY':
            return True
        if any(login.get('account_id') == check['account_id'] and 'finished_at' not in login
               for login in logins.values()):
            return False
        status = self.ledger.account_status(check['account_id'])
        return status['status'] == 'READY' and (status.get('checked_at') or '') > check.get('checked_at', '')

    def refresh(self, batch_id):
        batch = self.get(batch_id)
        tasks = batch['tasks']
        states = [task['state'] for task in tasks]
        errors = [task.get('error') for task in tasks if task.get('error')]
        error = errors[0]['code'] if errors else None
        references = [task for task in tasks if not task['owned']]
        reference_checks = {c.get('task_id'): c for c in batch['checks']}
        unverified = [task for task in references if reference_checks.get(task['id'], {}).get('status') != 'READY']
        if states and all(state == 'PUBLISHED' for state in states) and not unverified:
            state, error, delay = 'PUBLISHED', None, 0
        elif any(reference_checks.get(task['id'], {}).get('status') == 'PREFLIGHT_RUNNING' for task in unverified):
            state, delay = 'RUNNING', .5
        elif 'PREPARING' in states:
            state, delay = 'RUNNING', .5
        elif any(task['owned'] and task['state'] in {'QUEUED','WAITING_FOR_ACCOUNT'} and not task['attempted_at'] for task in tasks):
            state, delay = 'PREFLIGHT_PENDING', 1
        elif any(task['attempted_at'] and task['state'] not in {'PUBLISHED','FAILED'} for task in tasks):
            state, error, delay = 'WAITING_RESULT', 'READ_ONLY_RECONCILIATION_REQUIRED', 5
        elif 'WAITING_FOR_LOGIN' in states:
            state, delay = 'WAITING_FOR_LOGIN', 5
        elif unverified:
            state, error, delay = 'BLOCKED', 'DUPLICATE_RESULT_UNCONFIRMED', 5
        else:
            state, delay = ('BLOCKED' if error == 'UNSUPPORTED_DECLARATION' else 'FAILED'), 0
            error = error or 'PLATFORM_REJECTED'
        self.update(batch_id, state, error=error, seconds=delay)

    def _block_target(self, batch_id, task, check, diagnostic=None):
        code = check['status']
        if code in LOGIN_ERRORS:
            if code in {'LOGIN_REQUIRED', 'SECURITY_VERIFICATION_REQUIRED'}:
                try:
                    check['login_session_id'] = self.sessions.start_login(task['plan']['account_id'])
                except Exception:
                    # Keep a login-required target visible, without failing siblings.
                    pass
            self.ledger.delay(task['id'], code, seconds=0, state='WAITING_FOR_LOGIN', diagnostic=diagnostic)
        elif code in {'PROFILE_BUSY', 'CHECK_FAILED'}:
            count = self.ledger.diagnostics(task['id'])['runtime'].get('check_count', 0)
            if code == 'PROFILE_BUSY' or count < 2:
                self.ledger.delay(task['id'], code, seconds=15, state='WAITING_FOR_ACCOUNT',
                    counted=code == 'CHECK_FAILED', diagnostic=diagnostic)
            else:
                self.ledger.preparation_failed(task['id'])
                self.ledger.delay(task['id'], code, seconds=0, diagnostic=diagnostic)
        else:
            self.ledger.preparation_failed(task['id'])
            self.ledger.delay(task['id'], code, seconds=0, diagnostic=diagnostic)
        self._record_check(batch_id, task, check)

    async def _run_target(self, batch_id, task, execute):
        self.ledger.event(task['id'], 'TASK', 'START')
        try:
            check = await self.inspect_item(task['plan'], own_task_ids={task['id']})
            self._record_check(batch_id, task, check)
            if check['status'] != 'READY':
                self._block_target(batch_id, task, check, check.get('error_detail'))
                return
            # Adapter repeats account/page verification in the SAME profile used
            # for upload. Its before_click commits uncertainty before the click.
            await execute(task)
            current = self.ledger.get(task['id'])
            if current['attempted_at'] and current['state'] not in {'PUBLISHED','FAILED'}:
                self.ledger.delay(task['id'], 'UNKNOWN_RESULT', seconds=5)
            elif not current['attempted_at']:
                self._block_target(batch_id, task, dict(check, status='SUBMISSION_NOT_ATTEMPTED'))
        except Exception as exc:
            current = self.ledger.get(task['id'])
            if current['attempted_at']:
                if current['state'] not in {'PUBLISHED','FAILED'}:
                    self.ledger.delay(task['id'], 'UNKNOWN_RESULT', seconds=5)
            else:
                diagnostic = safe_execution_error(exc, SAFE_ERRORS)
                check = dict(account_id=task['plan']['account_id'], channel=task['plan']['channel'],
                    status=diagnostic['code'], checked_at=now(), error_detail=diagnostic)
                self._block_target(batch_id, task, check, diagnostic)
                self.ledger.event(task['id'], diagnostic['stage'], diagnostic['code'])
        finally:
            self.refresh(batch_id)

    def _claim_reference(self, batch_id, task):
        # A referenced target is never claimed for upload or re-submission.
        self._record_check(batch_id, task, dict(account_id=task['plan']['account_id'],
            channel=task['plan']['channel'], status='PREFLIGHT_RUNNING', checked_at=now()))
        return True

    async def _check_reference(self, batch_id, task):
        try:
            check = await self.inspect_item(task['plan'])
            self._record_check(batch_id, task, check)
        finally:
            self.refresh(batch_id)

    async def tick(self, execute, dispatch=None):
        local = dispatch is None
        dispatch = dispatch or PublishDispatch(self.sessions)
        with self.ledger.connect() as db:
            # Old WAITING_RESULT/BLOCKED batches are deliberately eligible:
            # unknown targets never re-submit, but untouched siblings can run.
            ids = [row['id'] for row in db.execute(
                "SELECT id FROM publish_batches WHERE state!='PUBLISHED' ORDER BY created_at")]
        for batch_id in ids:
            batch = self.get(batch_id)
            for task in batch['tasks']:
                if not task['owned']:
                    check = next((c for c in batch['checks'] if c.get('task_id') == task['id']), {})
                    if task['state'] == 'PUBLISHED' and not check:
                        dispatch.start(task, lambda task=task, batch_id=batch_id: self._check_reference(batch_id, task),
                            claim=lambda task=task, batch_id=batch_id: self._claim_reference(batch_id, task),
                            batch_id=batch_id, kind='reference_check')
                    continue
                if task['state'] == 'WAITING_FOR_LOGIN' and not task['attempted_at']:
                    check = next((c for c in batch['checks'] if c.get('task_id') == task['id'] or
                        (c.get('account_id') == task['plan']['account_id'] and c.get('channel') == task['plan']['channel'])), {})
                    if check and self._login_done(check):
                        self.ledger.resume_account(task['plan']['account_id'])
                        task = dict(task, **self.ledger.get(task['id']))
                        task['runtime'] = self.ledger.diagnostics(task['id'])['runtime']
                if (not task['owned'] or task['attempted_at'] or task['state'] not in {'QUEUED','WAITING_FOR_ACCOUNT'}
                        or task['runtime'].get('next_check', 0) > time.time()):
                    continue
                dispatch.start(task, lambda task=task, batch_id=batch_id: self._run_target(batch_id, task, execute),
                    claim=lambda task=task, batch_id=batch_id: self._claim(batch_id, task), batch_id=batch_id)
            self.refresh(batch_id)
        if local:
            await dispatch.drain()
