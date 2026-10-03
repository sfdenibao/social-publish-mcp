"""Real asyncio overlap and durable queue tests; browser operations are inert fixtures."""
import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tests import test_publish_batches as batch_fixtures
from utils.publish_batches import ORDER
from utils.publish_dispatch import PublishDispatch
from utils.publish_delete import delete_publish
from utils.publish_worker import PublishWorker


@pytest.fixture
def env():
    case = batch_fixtures.BatchTests()
    case.setUp()
    with case.ledger.connect() as db:
        db.execute('CREATE TABLE frozen_plans(id TEXT PRIMARY KEY,body TEXT NOT NULL)')
    case.sessions.accounts = lambda: []
    case.sessions.account = lambda account: dict(channel=account, profile=str(case.root / account))
    yield case
    case.doCleanups()


def worker(env, execute=None, reconcile=None, limit=3):
    result = PublishWorker(env.ledger, execute or env.execute,
        reconcile or env.batches.reconcile, env.sessions, max_parallel=limit)
    result.batches = env.batches
    return result


def test_slow_preflight_does_not_delay_other_platform_uploads(env):
    async def scenario():
        batch = env.submit()
        slow, release, other = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def preflight(account):
            if account == ORDER[0]:
                slow.set()
                await release.wait()
            return dict(status='READY', publish_input_verified=True)
        async def execute(task):
            await env.execute(task)
            if task['plan']['channel'] == ORDER[1]:
                other.set()
        env.sessions.preflight = preflight
        w = worker(env, execute)
        await w.tick(wait=False)
        await asyncio.wait_for(slow.wait(), 2)
        await asyncio.wait_for(other.wait(), 2)
        assert not release.is_set()
        assert env.batches.get(batch['id'])['progress']['published'] >= 1
        release.set()
        await w.dispatch.drain()
        assert env.batches.get(batch['id'])['state'] == 'PUBLISHED'
    asyncio.run(scenario())


def test_slow_upload_does_not_delay_next_platform_and_global_lock_is_free(env):
    async def scenario():
        batch = env.submit()
        entered, release, other = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def execute(task):
            if task['plan']['channel'] == ORDER[0]:
                entered.set()
                await release.wait()
            await env.execute(task)
            if task['plan']['channel'] == ORDER[1]:
                other.set()
        w = worker(env, execute)
        await w.tick(wait=False)
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(other.wait(), 2)
        assert w.mutation_lock.acquire(blocking=False)
        w.mutation_lock.release()
        with pytest.raises(ValueError, match='TASK_BUSY|SUBMITTED_TASK_DELETE_FORBIDDEN'):
            delete_publish(env.ledger, batch['id'])
        with pytest.raises(ValueError, match='BATCH_BUSY'):
            env.batches.resume(batch['id'])
        release.set()
        await w.dispatch.drain()
        assert len(env.clicks) == 3
    asyncio.run(scenario())


def test_old_unknown_reconciliation_and_new_sibling_run_concurrently_after_restart(env):
    async def scenario():
        batch = env.submit()
        first = batch['tasks'][0]
        env.batches._claim(batch['id'], first)
        env.batches._record_check(batch['id'], first, dict(account_id=ORDER[0], channel=ORDER[0],
            status='READY', publish_input_verified=True))
        env.ledger.before_click(first['id'])
        attempted = env.ledger.get(first['id'])['attempted_at']
        env.batches.update(batch['id'], 'WAITING_RESULT', seconds=5)
        env.ledger.recover(); env.batches.recover()
        checking, release, uploaded = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def reconcile(task):
            checking.set()
            await release.wait()
            raise RuntimeError('UNKNOWN_RESULT')
        async def execute(task):
            assert task['id'] != first['id']
            await env.execute(task)
            uploaded.set()
        w = worker(env, execute, reconcile)
        await w.tick(wait=False)
        await asyncio.wait_for(checking.wait(), 2)
        await asyncio.wait_for(uploaded.wait(), 2)
        assert not release.is_set()
        release.set(); await w.dispatch.drain()
        await w.tick()
        assert env.ledger.get(first['id'])['attempted_at'] == attempted
        assert env.batches.get(batch['id'])['progress']['published'] == 2
        assert set(env.clicks) == set(ORDER[1:])
    asyncio.run(scenario())


def test_slow_result_check_does_not_block_another_result_check(env):
    async def scenario():
        tasks = [env.ledger.submit(str(index), item) for index, item in enumerate(env.items)]
        for task in tasks:
            env.ledger.before_click(task['id'])
        release, completed = asyncio.Event(), asyncio.Event()
        async def reconcile(task):
            if task['id'] == tasks[0]['id']:
                await release.wait()
            else:
                env.publish(task); completed.set()
        w = worker(env, reconcile=reconcile)
        await w.tick(wait=False)
        await asyncio.wait_for(completed.wait(), 2)
        assert not release.is_set()
        release.set(); await w.dispatch.drain()
        assert all(env.ledger.get(task['id'])['state'] == 'PUBLISHED' for task in tasks[1:])
    asyncio.run(scenario())


def test_finished_slot_is_refilled_while_another_upload_is_still_slow(env):
    async def scenario():
        env.submit(env.items[:2])
        release, first_started, third_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def execute(task):
            if task['plan']['channel'] == ORDER[0]:
                first_started.set(); await release.wait()
            await env.execute(task)
            if task['plan']['channel'] == ORDER[2]:
                third_finished.set()
        w = worker(env, execute, limit=2)
        await w.tick(wait=False)
        await asyncio.wait_for(first_started.wait(), 2)
        for _ in range(100):
            if ORDER[1] in env.clicks:
                break
            await asyncio.sleep(.01)
        assert ORDER[1] in env.clicks
        env.submit([env.items[2]], key='later')
        await w.tick(wait=False)
        await asyncio.wait_for(third_finished.wait(), 2)
        assert not release.is_set()
        release.set(); await w.dispatch.drain()
    asyncio.run(scenario())


@pytest.mark.parametrize('same_account', [True, False])
def test_same_account_or_profile_never_overlaps_and_does_not_block_other_profile(env, same_account):
    async def scenario():
        if same_account:
            second = dict(env.items[0], media_sha256='other-media', title='second')
        else:
            second = env.items[1]
            env.sessions.account = lambda account: dict(channel=account,
                profile=str(env.root / ('shared' if account in ORDER[:2] else account)))
        tasks = [env.ledger.submit('first', env.items[0]), env.ledger.submit('second', second),
                 env.ledger.submit('third', env.items[2])]
        release, first_started, third_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        started = []
        async def execute(task):
            started.append(task['id'])
            if task['id'] == tasks[0]['id']:
                first_started.set(); await release.wait()
            await env.execute(task)
            if task['id'] == tasks[2]['id']:
                third_finished.set()
        w = worker(env, execute)
        await w.tick(wait=False)
        await asyncio.wait_for(first_started.wait(), 2)
        await asyncio.wait_for(third_finished.wait(), 2)
        assert tasks[1]['id'] not in started
        assert env.ledger.get(tasks[1]['id'])['state'] == 'QUEUED'
        release.set(); await w.dispatch.drain()
        await w.tick()
        assert started.count(tasks[1]['id']) == 1
    asyncio.run(scenario())


def test_global_concurrency_limit_is_respected(env):
    async def scenario():
        env.submit()
        release = asyncio.Event()
        active = peak = 0
        async def execute(task):
            nonlocal active, peak
            active += 1; peak = max(peak, active)
            try:
                await release.wait(); await env.execute(task)
            finally:
                active -= 1
        w = worker(env, execute, limit=2)
        await w.tick(wait=False)
        for _ in range(100):
            if active == 2: break
            await asyncio.sleep(.01)
        assert active == 2 and len(w.dispatch.active) == 2
        release.set(); await w.dispatch.drain(); await w.tick()
        assert peak == 2 and len(env.clicks) == 3
    asyncio.run(scenario())


def test_durable_click_guard_uses_only_this_target_and_rejects_second_click(env):
    batch = env.submit()
    second = batch['tasks'][1]
    env.batches._claim(batch['id'], second)
    env.batches._record_check(batch['id'], second, dict(account_id=ORDER[1], channel=ORDER[1],
        status='READY', publish_input_verified=True))
    with pytest.raises(RuntimeError, match='BATCH_PREFLIGHT_REQUIRED'):
        env.ledger.before_click(batch['tasks'][0]['id'])
    env.ledger.before_click(second['id'])
    with pytest.raises(RuntimeError, match='DUPLICATE_SUBMISSION_BLOCKED'):
        env.ledger.before_click(second['id'])


def test_atomic_click_commit_allows_only_one_concurrent_caller(env):
    from concurrent.futures import ThreadPoolExecutor
    task = env.ledger.submit('single', env.items[0])
    barrier = threading.Barrier(4)
    def click():
        barrier.wait()
        try:
            env.ledger.before_click(task['id']); return 'clicked'
        except RuntimeError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(lambda _: click(), range(4)))
    assert result.count('clicked') == 1
    assert result.count('DUPLICATE_SUBMISSION_BLOCKED') == 3


def test_two_scheduler_instances_cannot_claim_same_target(env):
    async def scenario():
        env.submit()
        execute = AsyncMock(side_effect=env.execute)
        await asyncio.gather(worker(env, execute).tick(), worker(env, execute).tick())
        assert execute.await_count == 3
        assert len(env.clicks) == 3
    asyncio.run(scenario())


def test_reference_failure_can_be_resumed_and_interrupted_reference_recovers(env):
    async def scenario():
        old = env.ledger.submit('old', env.items[0])
        env.ledger.before_click(old['id']); env.publish(old)
        batch = env.submit([env.items[0]])
        original = env.batches.reconcile
        env.batches.reconcile = AsyncMock(side_effect=RuntimeError('UNKNOWN_RESULT'))
        await env.tick()
        assert env.batches.get(batch['id'])['state'] == 'BLOCKED'
        env.batches.resume(batch['id'])
        task = batch['tasks'][0]
        env.batches._claim_reference(batch['id'], task)
        env.batches.refresh(batch['id'])
        env.batches.recover()
        env.batches.reconcile = original
        await env.tick()
        assert env.batches.get(batch['id'])['state'] == 'PUBLISHED'
        assert env.clicks == []
    asyncio.run(scenario())


def test_unchanged_waiting_result_does_not_flood_audit_events(env):
    async def scenario():
        env.submit([env.items[0]])
        async def uncertain(task):
            env.ledger.before_click(task['id']); raise TimeoutError()
        await env.tick(uncertain)
        with env.ledger.connect() as db:
            before = db.execute('SELECT count(*) FROM task_events').fetchone()[0]
        for _ in range(5):
            await env.tick()
        with env.ledger.connect() as db:
            assert db.execute('SELECT count(*) FROM task_events').fetchone()[0] == before
    asyncio.run(scenario())
