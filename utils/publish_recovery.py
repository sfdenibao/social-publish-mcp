"""Explicit, audited one-shot recovery; uncertainty is never called failure."""
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from utils.publish_ledger import now


def recover_uncertain(ledger, batches, reconcile, *, task_id, expected_attempted_at,
                      request_id, reason, acknowledge_duplicate_risk):
    if acknowledge_duplicate_risk is not True or not reason.strip() or not request_id.strip():
        raise ValueError('EXPLICIT_RECOVERY_ACKNOWLEDGEMENT_REQUIRED')
    payload = dict(task_id=task_id, expected_attempted_at=expected_attempted_at,
                   request_id=request_id, reason=reason, acknowledge_duplicate_risk=True)
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    with ledger.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS publish_recoveries (
            request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, task_id TEXT NOT NULL,
            recovered_at TEXT NOT NULL, previous_attempt TEXT NOT NULL,
            observation TEXT NOT NULL, authorization TEXT NOT NULL)''')
        prior = db.execute('SELECT * FROM publish_recoveries WHERE request_id=?', (request_id,)).fetchone()
        owner = db.execute('SELECT batch_id FROM batch_tasks WHERE task_id=? AND owned=1', (task_id,)).fetchone()
        if prior and prior['fingerprint'] != fingerprint:
            raise ValueError('IDEMPOTENCY_CONFLICT')
        if not owner:
            raise ValueError('OWNED_BATCH_TASK_REQUIRED')
        batch_id = owner['batch_id']
        if prior:
            return batches.get(batch_id)
    task = ledger.get(task_id)
    if task['state'] != 'UNKNOWN_RESULT' or task['attempted_at'] != expected_attempted_at or not expected_attempted_at:
        raise ValueError('STALE_OR_INELIGIBLE_RECOVERY')
    if (datetime.now(timezone.utc) - datetime.fromisoformat(expected_attempted_at)).total_seconds() < 300:
        raise ValueError('RECENT_SUBMISSION_MUST_SETTLE')
    # Reconcile immediately before granting a new attempt. Login/busy/parser
    # errors are not absence evidence and cannot authorize recovery.
    try:
        asyncio.run(reconcile(task))
    except RuntimeError as exc:
        if str(exc) != 'UNKNOWN_RESULT':
            raise
    current = ledger.get(task_id)
    if current['state'] != 'UNKNOWN_RESULT':
        return batches.get(batch_id)
    observation_path = Path(ledger.path).parent / (task_id + '-management-observation.json')
    observation = json.loads(observation_path.read_text(encoding='utf-8'))
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(observation['observed_at'])).total_seconds()
    if (observation['task_id'] != task_id or observation['attempted_at'] != expected_attempted_at
            or not 0 <= age <= 120 or not observation.get('cards')):
        raise ValueError('FRESH_MANAGEMENT_OBSERVATION_REQUIRED')
    title = task['plan']['title'].strip()
    if any(title in str(card.get('title') or '') for card in observation['cards']):
        raise ValueError('POSSIBLE_EXISTING_WORK_REQUIRES_RECONCILIATION')
    # This is a risk-acknowledged retry, NOT a claim the first request failed.
    # The original attempt and its uncertainty survive in an immutable journal.
    with ledger.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM publish_attempts WHERE id=?', (task_id,)).fetchone()
        if row['state'] != 'UNKNOWN_RESULT' or row['attempted_at'] != expected_attempted_at:
            raise ValueError('STALE_OR_INELIGIBLE_RECOVERY')
        db.execute('INSERT INTO publish_recoveries VALUES (?,?,?,?,?,?,?)', (
            request_id, fingerprint, task_id, now(), json.dumps(dict(row)),
            json.dumps(observation, ensure_ascii=False), json.dumps(payload, ensure_ascii=False)))
        db.execute("UPDATE publish_attempts SET state='QUEUED',attempted_at=NULL,evidence=NULL WHERE id=?", (task_id,))
        db.execute('UPDATE task_runtime SET next_check=0,check_count=0,error=NULL WHERE task_id=?', (task_id,))
        batch = db.execute('SELECT checks FROM publish_batches WHERE id=?', (batch_id,)).fetchone()
        checks = [check for check in json.loads(batch['checks']) if check.get('task_id') != task_id
                  and not (check.get('task_id') is None
                           and check.get('account_id') == task['plan']['account_id']
                           and check.get('channel') == task['plan']['channel'])]
        active = db.execute("""SELECT 1 FROM batch_tasks b JOIN publish_attempts p ON p.id=b.task_id
            WHERE b.batch_id=? AND p.state='PREPARING'""", (batch_id,)).fetchone()
        db.execute('UPDATE publish_batches SET state=?,checks=?,error=NULL,next_check=0 WHERE id=?',
                   ('RUNNING' if active else 'PREFLIGHT_PENDING', json.dumps(checks, ensure_ascii=False), batch_id))
        db.execute('INSERT INTO task_events(task_id,at,stage,event) VALUES (?,?,?,?)',
                   (task_id, now(), 'RECOVERY', 'EXPLICIT_ONE_SHOT_RETRY:' + request_id))
    return batches.get(batch_id)
