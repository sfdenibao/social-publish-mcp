"""Durable single-submit guard. Never infer remote success from local execution."""
import hashlib
import json
import sqlite3
import uuid
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class PublishLedger:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS publish_attempts (
                id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL, plan TEXT NOT NULL,
                state TEXT NOT NULL, created_at TEXT NOT NULL,
                attempted_at TEXT, evidence TEXT)""")
            db.execute('CREATE TABLE IF NOT EXISTS publish_keys (key TEXT PRIMARY KEY, task_id TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS task_runtime (
                task_id TEXT PRIMARY KEY, next_check REAL DEFAULT 0, check_count INTEGER DEFAULT 0,
                stage TEXT, error TEXT, updated_at TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                at TEXT NOT NULL, stage TEXT NOT NULL, event TEXT NOT NULL, elapsed_ms REAL)''')
            db.execute('CREATE TABLE IF NOT EXISTS account_checks (account_id TEXT PRIMARY KEY, result TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS publish_recoveries (
                request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, task_id TEXT NOT NULL,
                recovered_at TEXT NOT NULL, previous_attempt TEXT NOT NULL,
                observation TEXT NOT NULL, authorization TEXT NOT NULL)''')
            db.execute("""CREATE TABLE IF NOT EXISTS publish_batches (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, plan TEXT NOT NULL,
                state TEXT NOT NULL, created_at TEXT NOT NULL, checks TEXT NOT NULL DEFAULT '[]',
                next_check REAL NOT NULL DEFAULT 0, error TEXT)""")
            db.execute('CREATE TABLE IF NOT EXISTS batch_keys (key TEXT PRIMARY KEY, batch_id TEXT NOT NULL, fingerprint TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS batch_tasks (batch_id TEXT NOT NULL, position INTEGER NOT NULL, task_id TEXT NOT NULL, owned INTEGER NOT NULL, PRIMARY KEY(batch_id,position))')
            db.execute('''CREATE TABLE IF NOT EXISTS publish_deletions (
                id TEXT PRIMARY KEY, target_id TEXT NOT NULL UNIQUE, deleted_at TEXT NOT NULL,
                receipt TEXT NOT NULL, audit TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS publish_deletion_aliases (
                target_id TEXT PRIMARY KEY, deletion_id TEXT NOT NULL)''')


    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, key, plan):
        if not isinstance(key, str) or not key.strip():
            raise ValueError('IDEMPOTENCY_KEY_REQUIRED')
        canonical = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM publish_attempts WHERE idempotency_key=? OR id IN (SELECT task_id FROM publish_keys WHERE key=?)', (key, key)).fetchone()
            if existing:
                if existing['fingerprint'] != fingerprint:
                    raise ValueError('IDEMPOTENCY_CONFLICT')
                return self._decode(existing)
            existing = db.execute('SELECT * FROM publish_attempts WHERE fingerprint=?', (fingerprint,)).fetchone()
            if existing:
                # A new key for the exact frozen plan is still the same task.
                db.execute('INSERT INTO publish_keys VALUES (?,?)', (key, existing['id']))
                return self._decode(existing)
            duplicate = self.media_match(db, plan)
            if duplicate:
                raise ValueError('DUPLICATE_MEDIA:' + duplicate['id'])
            task_id = uuid.uuid4().hex
            db.execute('INSERT INTO publish_attempts VALUES (?,?,?,?,?,?,?,?)',
                       (task_id, key, fingerprint, canonical, 'QUEUED', now(), None, None))
            return self._decode(db.execute('SELECT * FROM publish_attempts WHERE id=?', (task_id,)).fetchone())

    @staticmethod
    def media_match(db, plan):
        if not plan.get('media_sha256'):
            return None
        for row in db.execute('SELECT * FROM publish_attempts ORDER BY created_at'):
            old=json.loads(row['plan'])
            if all(old.get(k)==plan.get(k) for k in ('channel','account_id','media_sha256')):
                return row
        return None

    def before_click(self, task_id):
        # Commit uncertainty BEFORE touching the external page. A crash here must
        # never make this task eligible for an automatic second click.
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            owner=db.execute('SELECT b.* FROM publish_batches b JOIN batch_tasks t ON t.batch_id=b.id WHERE t.task_id=? AND t.owned=1',(task_id,)).fetchone()
            if owner:
                checks=json.loads(owner['checks'])
                target=db.execute('SELECT * FROM publish_attempts WHERE id=?',(task_id,)).fetchone()
                plan=json.loads(target['plan'])
                ready=any(c.get('task_id')==task_id and c.get('account_id')==plan['account_id']
                          and c.get('channel')==plan['channel'] and c.get('status')=='READY'
                          and c.get('publish_input_verified') is True
                          and c.get('plan_fingerprint')==target['fingerprint'] for c in checks)
                if owner['state']!='RUNNING' or not ready:
                    raise RuntimeError('BATCH_PREFLIGHT_REQUIRED')
            result = db.execute("UPDATE publish_attempts SET state='UNKNOWN_RESULT', attempted_at=? WHERE id=? AND attempted_at IS NULL AND state IN ('QUEUED','PREPARING')",
                                (now(), task_id))
            if result.rowcount != 1:
                raise RuntimeError('DUPLICATE_SUBMISSION_BLOCKED')

    def claim_next(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("""SELECT p.* FROM publish_attempts p LEFT JOIN task_runtime r ON r.task_id=p.id
                WHERE (p.state='QUEUED' OR p.state='WAITING_FOR_ACCOUNT') AND COALESCE(r.next_check,0)<=?
                AND NOT EXISTS (SELECT 1 FROM batch_tasks b WHERE b.task_id=p.id)
                ORDER BY p.created_at LIMIT 1""", (time.time(),)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE publish_attempts SET state='PREPARING' WHERE id=?", (row['id'],))
            return self._decode(row)

    def preparation_failed(self, task_id):
        with self.connect() as db:
            db.execute("UPDATE publish_attempts SET state='FAILED' WHERE id=? AND attempted_at IS NULL", (task_id,))

    def event(self, task_id, stage, event, elapsed_ms=None):
        with self.connect() as db:
            db.execute('INSERT INTO task_events(task_id,at,stage,event,elapsed_ms) VALUES (?,?,?,?,?)',
                       (task_id, now(), stage, event, elapsed_ms))
            db.execute('''INSERT INTO task_runtime(task_id,stage,updated_at) VALUES (?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET stage=excluded.stage,updated_at=excluded.updated_at''',
                       (task_id, stage, now()))

    @contextmanager
    def timed(self, task_id, stage):
        started = time.perf_counter()
        self.event(task_id, stage, 'START')
        try:
            yield
        except BaseException:
            self.event(task_id, stage, 'ERROR', round((time.perf_counter()-started)*1000, 2))
            raise
        else:
            self.event(task_id, stage, 'END', round((time.perf_counter()-started)*1000, 2))

    def delay(self, task_id, code, *, seconds=15, state=None, counted=False, diagnostic=None):
        error = dict(code=code, retryable=bool(seconds) and code in {'PROFILE_BUSY','CHECK_FAILED','UNKNOWN_RESULT','PENDING_REVIEW'},
                     next_action='WAIT_FOR_AUTOMATIC_CHECK' if seconds else 'CHECK_ACCOUNT_OR_PLATFORM')
        stage = None
        if diagnostic and diagnostic.get('code') == code:
            error.update({key: diagnostic[key] for key in ('message', 'stage', 'details') if key in diagnostic})
            stage = diagnostic.get('stage')
            if stage == 'DECLARATION':
                error['next_action'] = 'REVIEW_DECLARATION'
        with self.connect() as db:
            db.execute('''INSERT INTO task_runtime(task_id,next_check,check_count,error,updated_at,stage)
                VALUES (?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
                next_check=excluded.next_check,check_count=task_runtime.check_count+?,
                error=excluded.error,updated_at=excluded.updated_at,
                stage=COALESCE(excluded.stage,task_runtime.stage)''',
                (task_id, time.time()+seconds if seconds else 1e20, int(counted), json.dumps(error), now(), stage, int(counted)))
            if state:
                db.execute('UPDATE publish_attempts SET state=? WHERE id=? AND attempted_at IS NULL', (state,task_id))

    def recover(self):
        with self.connect() as db:
            # No external submission can precede before_click's durable commit.
            db.execute("UPDATE publish_attempts SET state='QUEUED' WHERE state='PREPARING' AND attempted_at IS NULL")

    def due_checks(self, limit=100):
        with self.connect() as db:
            rows = db.execute('''SELECT p.* FROM publish_attempts p LEFT JOIN task_runtime r ON r.task_id=p.id
                WHERE p.attempted_at IS NOT NULL AND p.state IN ('UNKNOWN_RESULT','SUBMITTED','PENDING_REVIEW')
                AND COALESCE(r.next_check,0)<=? AND COALESCE(r.check_count,0)<10
                ORDER BY COALESCE(r.next_check,0),p.created_at LIMIT ?''', (time.time(), max(1, min(1000, int(limit))))).fetchall()
            return [self._decode(row) for row in rows]

    def account_status(self, account_id, result=None):
        with self.connect() as db:
            if result is not None:
                db.execute('INSERT OR REPLACE INTO account_checks VALUES (?,?)', (account_id,json.dumps(result)))
            row=db.execute('SELECT result FROM account_checks WHERE account_id=?',(account_id,)).fetchone()
            return json.loads(row[0]) if row else dict(status='UNKNOWN',checked_at=None)

    def resume_account(self, account_id):
        with self.connect() as db:
            for row in db.execute("SELECT id,plan FROM publish_attempts WHERE state='WAITING_FOR_LOGIN' AND attempted_at IS NULL").fetchall():
                if json.loads(row['plan'])['account_id']==account_id:
                    db.execute("UPDATE publish_attempts SET state='QUEUED' WHERE id=?",(row['id'],))
                    db.execute('UPDATE task_runtime SET next_check=0 WHERE task_id=?',(row['id'],))

    def diagnostics(self, task_id):
        with self.connect() as db:
            runtime=db.execute('SELECT * FROM task_runtime WHERE task_id=?',(task_id,)).fetchone()
            events=[dict(r) for r in db.execute('SELECT at,stage,event,elapsed_ms FROM task_events WHERE task_id=? ORDER BY id',(task_id,))]
        runtime=dict(runtime) if runtime else {}
        runtime['error']=json.loads(runtime['error']) if runtime.get('error') else None
        return dict(runtime=runtime,events=events)

    def record_dom_result(self, task_id, state, evidence):
        if state not in {'SUBMITTED', 'PENDING_REVIEW', 'PUBLISHED', 'FAILED'}:
            raise ValueError('INVALID_REMOTE_STATE')
        required = {'channel', 'account_id', 'title', 'observed_at', 'management_url', 'matched_by', 'status_text'}
        if not isinstance(evidence, dict) or any(not evidence.get(k) for k in required):
            raise ValueError('DOM_EVIDENCE_REQUIRED')
        if evidence['matched_by'] not in {'new_work_id', 'title_and_publish_time'}:
            raise ValueError('AMBIGUOUS_WORK_MATCH')
        with self.connect() as db:
            row = db.execute('SELECT * FROM publish_attempts WHERE id=?', (task_id,)).fetchone()
            if not row or not row['attempted_at']:
                raise ValueError('SUBMISSION_NOT_ATTEMPTED')
            plan = json.loads(row['plan'])
            if any(evidence[k] != plan.get(k) for k in ('channel', 'account_id', 'title')):
                raise ValueError('EVIDENCE_PLAN_MISMATCH')
            if row['state'] in {'PUBLISHED', 'FAILED'}:
                raise ValueError('TERMINAL_RESULT_ALREADY_RECORDED')
            db.execute('UPDATE publish_attempts SET state=?, evidence=? WHERE id=?',
                       (state, json.dumps(evidence, ensure_ascii=False), task_id))
            db.execute('UPDATE task_runtime SET error=NULL WHERE task_id=?',(task_id,))

    def get(self, task_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM publish_attempts WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise KeyError('TASK_NOT_FOUND')
            result = self._decode(row)
            result['previous_attempts'] = [dict(request_id=r['request_id'], recovered_at=r['recovered_at'],
                attempted_at=json.loads(r['previous_attempt'])['attempted_at'], state='UNKNOWN_RESULT')
                for r in db.execute('SELECT * FROM publish_recoveries WHERE task_id=? ORDER BY recovered_at', (task_id,))]
            return result

    @staticmethod
    def _decode(row):
        result = dict(row)
        result['plan'] = json.loads(result['plan'])
        result['evidence'] = json.loads(result['evidence']) if result['evidence'] else None
        return result
