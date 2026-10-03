"""Delete local, provably unsubmitted work; never remove remote publications."""
import hashlib
import json
import uuid
from utils.publish_ledger import now


def deletion_receipt(ledger, target_id):
    """Read only: a missing task is never converted into a successful deletion."""
    with ledger.connect() as db:
        row = db.execute('''SELECT d.receipt FROM publish_deletions d
            JOIN publish_deletion_aliases a ON a.deletion_id=d.id WHERE a.target_id=?''', (target_id,)).fetchone()
        return json.loads(row['receipt']) if row else None


def delete_publish(ledger, target_id):
    if not target_id or not target_id.strip():
        raise ValueError('TARGET_ID_REQUIRED')
    with ledger.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        prior = db.execute('''SELECT d.receipt FROM publish_deletions d
            JOIN publish_deletion_aliases a ON a.deletion_id=d.id WHERE a.target_id=?''', (target_id,)).fetchone()
        if prior:
            return json.loads(prior['receipt'])
        batch = db.execute('SELECT * FROM publish_batches WHERE id=?', (target_id,)).fetchone()
        task = db.execute('SELECT * FROM publish_attempts WHERE id=?', (target_id,)).fetchone()
        plan = db.execute('SELECT * FROM frozen_plans WHERE id=?', (target_id,)).fetchone()
        if not any((batch, task, plan)):
            return dict(deleted=False, state='NOT_FOUND', target_id=target_id,
                        deleted_task_ids=[], deleted_batch_ids=[], deleted_plan_ids=[])
        if task:
            if db.execute('SELECT 1 FROM batch_tasks WHERE task_id=?', (target_id,)).fetchone():
                raise ValueError('USE_BATCH_ID_FOR_DELETE')
            tasks = [task]
        elif batch:
            tasks = db.execute('SELECT p.*,t.owned,t.position FROM publish_attempts p JOIN batch_tasks t ON p.id=t.task_id WHERE t.batch_id=? ORDER BY t.position', (target_id,)).fetchall()
            if batch['state'] in {'RUNNING', 'PREFLIGHT'}:
                raise ValueError('TASK_BUSY')
            if batch['state'] not in {'FAILED','BLOCKED','WAITING_FOR_LOGIN','PREFLIGHT_PENDING'}:
                raise ValueError('SUBMITTED_TASK_DELETE_FORBIDDEN')
            try:
                frozen = json.loads(batch['plan'])
                items = frozen.get('items', [frozen])
                if not tasks or len(tasks)!=len(items) or [r['position'] for r in tasks]!=list(range(len(items))):
                    raise ValueError('INCOMPLETE_BATCH_DELETE_FORBIDDEN')
                for row,item in zip(tasks,items):
                    if not row['owned'] or json.loads(row['plan'])!=item:
                        raise ValueError('TASK_REFERENCED_BY_OTHER_BATCH')
            except (TypeError, AttributeError, json.JSONDecodeError) as exc:
                raise ValueError('INCOMPLETE_BATCH_DELETE_FORBIDDEN') from exc
        else:
            tasks = []
        for row in tasks:
            if (row['attempted_at'] is not None or row['evidence'] is not None
                    or db.execute('SELECT 1 FROM publish_recoveries WHERE task_id=?', (row['id'],)).fetchone()):
                raise ValueError('SUBMITTED_TASK_DELETE_FORBIDDEN')
            if row['state'] == 'PREPARING':
                raise ValueError('TASK_BUSY')
            if row['state'] not in {'QUEUED','FAILED','WAITING_FOR_LOGIN','WAITING_FOR_ACCOUNT'}:
                raise ValueError('SUBMITTED_TASK_DELETE_FORBIDDEN')
            if db.execute("""SELECT 1 FROM task_events WHERE task_id IN (?,?) AND
                    (stage='SUBMIT_CLICK' OR event IN ('SUBMIT_CLICK','PUBLISHED','SUBMITTED','PENDING_REVIEW','UNKNOWN_RESULT'))""",
                    (row['id'], target_id)).fetchone():
                raise ValueError('SUBMITTED_TASK_DELETE_FORBIDDEN')
            runtime=db.execute('SELECT error FROM task_runtime WHERE task_id=?',(row['id'],)).fetchone()
            try:
                error=json.loads(runtime['error']) if runtime and runtime['error'] else {}
                if error.get('code') in {'PUBLISHED','SUBMITTED','PENDING_REVIEW','UNKNOWN_RESULT'}:
                    raise ValueError('SUBMITTED_TASK_DELETE_FORBIDDEN')
            except (TypeError, AttributeError, json.JSONDecodeError) as exc:
                raise ValueError('INCOMPLETE_BATCH_DELETE_FORBIDDEN') from exc
            if db.execute('SELECT 1 FROM batch_tasks WHERE task_id=? AND batch_id!=?', (row['id'], target_id)).fetchone():
                raise ValueError('TASK_REFERENCED_BY_OTHER_BATCH')
        def normalized(body):
            value=json.loads(body)
            items=value.get('items', [value])
            return sorted(json.dumps({k:v for k,v in item.items() if k!='plan_id'}, sort_keys=True) for item in items)
        body = (batch or task or plan)['plan' if batch or task else 'body']
        matching = [r['id'] for r in db.execute('SELECT * FROM frozen_plans') if normalized(r['body'])==normalized(body)]
        if plan:
            for table in ('publish_attempts', 'publish_batches'):
                if any(normalized(r['plan'])==normalized(body) for r in db.execute('SELECT plan FROM '+table)):
                    raise ValueError('PLAN_HAS_TASK_USE_TASK_OR_BATCH_ID')
        # The actual deleted records and keys/events are archived atomically with
        # deletion. This is a receipt of an observed safe operation, never a
        # reconstruction from NOT_FOUND after an old unrecorded deletion.
        snapshot = dict(target_id=target_id, batch=dict(batch) if batch else None,
                        tasks=[dict(row) for row in tasks], plans=[], batch_keys=[], publish_keys=[],
                        batch_tasks=[], task_runtime=[], task_events=[])
        for pid in matching:
            snapshot['plans'].append(dict(db.execute('SELECT * FROM frozen_plans WHERE id=?',(pid,)).fetchone()))
        if batch:
            snapshot['batch_keys']=[dict(row) for row in db.execute('SELECT * FROM batch_keys WHERE batch_id=?',(target_id,))]
            snapshot['batch_tasks']=[dict(row) for row in db.execute('SELECT * FROM batch_tasks WHERE batch_id=?',(target_id,))]
        for row in tasks:
            snapshot['publish_keys'].extend(dict(r) for r in db.execute('SELECT * FROM publish_keys WHERE task_id=?',(row['id'],)))
        for identity in dict.fromkeys([target_id,*[row['id'] for row in tasks]]):
            for table in ('task_runtime','task_events'):
                snapshot[table].extend(dict(r) for r in db.execute('SELECT * FROM '+table+' WHERE task_id=?',(identity,)))
        encoded=json.dumps(snapshot,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        deletion_id=str(uuid.uuid4());deleted_at=now()
        receipt=dict(deleted=True, state='DELETED', target_id=target_id,
                     deleted_task_ids=[r['id'] for r in tasks], deleted_batch_ids=[target_id] if batch else [],
                     deleted_plan_ids=matching, remote_content_deleted=False, deletion_id=deletion_id,
                     deleted_at=deleted_at, mode='never_submitted',
                     snapshot_sha256=hashlib.sha256(encoded.encode('utf-8')).hexdigest())
        for row in tasks:
            for table, key in [('publish_keys','task_id'), ('task_runtime','task_id'), ('task_events','task_id'), ('batch_tasks','task_id'), ('publish_attempts','id')]:
                db.execute(f'DELETE FROM {table} WHERE {key}=?', (row['id'],))
        if batch:
            for table,key in [('batch_keys','batch_id'), ('batch_tasks','batch_id'), ('task_runtime','task_id'), ('task_events','task_id'), ('publish_batches','id')]:
                db.execute(f'DELETE FROM {table} WHERE {key}=?', (target_id,))
        for pid in matching:
            db.execute('DELETE FROM frozen_plans WHERE id=?', (pid,))
        db.execute('INSERT INTO publish_deletions VALUES (?,?,?,?,?)',
                   (deletion_id,target_id,deleted_at,json.dumps(receipt,ensure_ascii=False),encoded))
        aliases={target_id,*receipt['deleted_task_ids'],*receipt['deleted_batch_ids'],*receipt['deleted_plan_ids']}
        for alias in aliases:
            db.execute('INSERT INTO publish_deletion_aliases VALUES (?,?)',(alias,deletion_id))
        return receipt
