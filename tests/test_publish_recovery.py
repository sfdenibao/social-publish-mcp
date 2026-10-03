import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from utils.publish_batches import PublishBatches
from utils.publish_delete import delete_publish
from utils.publish_ledger import PublishLedger, now
from utils.publish_recovery import recover_uncertain


@pytest.fixture
def recovery(tmp_path):
    ledger = PublishLedger(tmp_path / 'ledger.db')
    with ledger.connect() as db:
        db.execute('CREATE TABLE frozen_plans(id TEXT PRIMARY KEY, body TEXT)')
    plan = dict(channel='wechat_channels', account_id='wechat', title='cat', description='',
                tags=[], declaration='', media_path='fixture.mp4', media_sha256='fixture')
    batches = PublishBatches(ledger, SimpleNamespace(), None)
    batch = batches.submit('original', dict(items=[plan]))
    task_id = batch['tasks'][0]['id']
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with ledger.connect() as db:
        db.execute("UPDATE publish_attempts SET state='UNKNOWN_RESULT',attempted_at=? WHERE id=?", (stamp, task_id))
        db.execute("UPDATE publish_batches SET state='WAITING_RESULT'")
    observed = dict(task_id=task_id, attempted_at=stamp, observed_at=now(), cards=[dict(title='other work')])
    async def reconcile(task):
        (tmp_path / (task_id + '-management-observation.json')).write_text(json.dumps(observed), encoding='utf-8')
        raise RuntimeError('UNKNOWN_RESULT')
    args = dict(task_id=task_id, expected_attempted_at=stamp, request_id='one',
                reason='Explicit user-authorized one-shot recovery', acknowledge_duplicate_risk=True)
    return ledger, batches, reconcile, args, observed


def run(w, **changes):
    ledger, batches, reconcile, args, _ = w
    return recover_uncertain(ledger, batches, reconcile, **(args | changes))


def test_retry_archives_unknown_and_cannot_be_replayed_or_deleted(recovery):
    ledger, _, _, args, _ = recovery
    result = run(recovery)
    assert result['state'] == 'PREFLIGHT_PENDING' and result['checks'] == []
    task = result['tasks'][0]
    assert task['state'] == 'QUEUED' and task['attempted_at'] is None
    assert task['previous_attempts'][0]['attempted_at'] == args['expected_attempted_at']
    with ledger.connect() as db:
        previous = json.loads(db.execute('SELECT previous_attempt FROM publish_recoveries').fetchone()[0])
        assert previous['state'] == 'UNKNOWN_RESULT' and previous['attempted_at'] == args['expected_attempted_at']
        db.execute("UPDATE publish_attempts SET state='UNKNOWN_RESULT',attempted_at=?", (now(),))
    replay = run(recovery)
    assert replay['tasks'][0]['state'] == 'UNKNOWN_RESULT'  # request replay cannot issue a third click
    with pytest.raises(ValueError, match='SUBMITTED_TASK_DELETE_FORBIDDEN'):
        delete_publish(ledger, result['id'])
    with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
        run(recovery, reason='changed authorization')


@pytest.mark.parametrize('changes,error', [
    ({'acknowledge_duplicate_risk': False}, 'EXPLICIT_RECOVERY'),
    ({'reason': ''}, 'EXPLICIT_RECOVERY'),
    ({'expected_attempted_at': 'stale'}, 'STALE_OR_INELIGIBLE'),
])
def test_no_ack_or_stale_card_has_no_side_effect(recovery, changes, error):
    with pytest.raises(ValueError, match=error):
        run(recovery, **changes)
    assert recovery[0].get(recovery[3]['task_id'])['state'] == 'UNKNOWN_RESULT'


@pytest.mark.parametrize('state', ['PUBLISHED', 'PENDING_REVIEW', 'FAILED'])
def test_known_platform_result_never_retries(recovery, state):
    with recovery[0].connect() as db:
        db.execute('UPDATE publish_attempts SET state=?', (state,))
    with pytest.raises(ValueError, match='STALE_OR_INELIGIBLE'):
        run(recovery)


def test_matching_work_and_stale_observation_block_recovery(recovery):
    recovery[4]['cards'] = [dict(title='cat')]
    with pytest.raises(ValueError, match='POSSIBLE_EXISTING_WORK'):
        run(recovery)
    recovery[4]['cards'] = [dict(title='other')]
    recovery[4]['observed_at'] = '2020-01-01T00:00:00+00:00'
    with pytest.raises(ValueError, match='FRESH_MANAGEMENT'):
        run(recovery)


def test_login_error_does_not_grant_retry(recovery):
    async def failed(task):
        raise RuntimeError('LOGIN_REQUIRED')
    ledger, batches, _, args, _ = recovery
    with pytest.raises(RuntimeError, match='LOGIN_REQUIRED'):
        recover_uncertain(ledger, batches, failed, **args)
    assert ledger.get(args['task_id'])['attempted_at'] == args['expected_attempted_at']


def test_explicit_recovery_preserves_active_sibling_preflight_and_click_gate(recovery):
    ledger, batches, _, args, _ = recovery
    sibling = ledger.submit('sibling', dict(channel='douyin', account_id='douyin', title='other',
        declaration='', media_path='fixture.mp4', media_sha256='different'))
    with ledger.connect() as db:
        batch_id = db.execute('SELECT batch_id FROM batch_tasks WHERE task_id=?', (args['task_id'],)).fetchone()[0]
        body = json.loads(db.execute('SELECT plan FROM publish_batches WHERE id=?', (batch_id,)).fetchone()[0])
        body['items'].append(sibling['plan'])
        db.execute('UPDATE publish_batches SET plan=? WHERE id=?', (json.dumps(body), batch_id))
        db.execute('INSERT INTO batch_tasks VALUES (?,?,?,?)', (batch_id, 1, sibling['id'], 1))
    assert batches._claim(batch_id, sibling)
    batches._record_check(batch_id, sibling, dict(account_id='douyin', channel='douyin',
        status='READY', publish_input_verified=True))
    before = batches.get(batch_id)['checks']
    result = run(recovery)
    assert result['state'] == 'RUNNING'
    assert result['checks'] == before
    assert ledger.get(sibling['id'])['state'] == 'PREPARING'
    ledger.before_click(sibling['id'])
    assert ledger.get(sibling['id'])['attempted_at'] is not None


def test_fastmcp_async_dispatch_preserves_recovery_lock_and_event_loop(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    script = r'''
import asyncio,json
from datetime import datetime,timedelta,timezone
import publish_mcp as s
plan=dict(channel='wechat_channels',account_id='fixture',title='cat',description='',tags=[],declaration='',media_path='fixture',media_sha256='fixture')
b=s.batches.submit('fixture',dict(items=[plan]));task=b['tasks'][0]['id']
stamp=(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
with s.ledger.connect() as db:
 db.execute("UPDATE publish_attempts SET state='UNKNOWN_RESULT',attempted_at=?",(stamp,))
 db.execute("UPDATE publish_batches SET state='WAITING_RESULT'")
async def fake(t):
 assert s.worker_runtime.mutation_lock.locked()
 (s.RUNTIME/(task+'-management-observation.json')).write_text(json.dumps(dict(task_id=task,attempted_at=stamp,observed_at=datetime.now(timezone.utc).isoformat(),cards=[dict(title='other')])))
 raise RuntimeError('UNKNOWN_RESULT')
s.reconcile=fake
async def main():
 result=await s.mcp.call_tool('recover_uncertain_publish',dict(task_id=task,expected_attempted_at=stamp,request_id='one',reason='fixture',acknowledge_duplicate_risk=True))
 assert s.ledger.get(task)['state']=='QUEUED',str(result)
 assert not s.worker_runtime.mutation_lock.locked()
 print('PASS')
asyncio.run(main())
'''
    result = subprocess.run([sys.executable, '-c', script],
        cwd=Path(__file__).resolve().parents[1], env={**os.environ, 'SAU_RUNTIME_DIR': str(tmp_path)},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    assert 'PASS' in result.stdout
