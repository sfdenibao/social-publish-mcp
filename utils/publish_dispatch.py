"""Bounded, process-local work ownership; durable claims remain in SQLite."""
import asyncio
import os
import threading
from pathlib import Path


SCHEDULING = dict(scheduling_mode='parallel', scheduling_version=2)


def concurrency_limit(value=None):
    try:
        return max(1, min(8, int(value if value is not None else os.environ.get('SAU_PUBLISH_CONCURRENCY', '3'))))
    except (TypeError, ValueError):
        return 3


def resources(sessions, task):
    account_id = task['plan']['account_id']
    result = {'account:' + account_id}
    try:
        profile = sessions.account(account_id).get('profile')
        if profile:
            result.add('profile:' + str(Path(profile).resolve()).casefold())
    except Exception:
        # The pipeline reports configuration errors. Never merge unrelated accounts.
        pass
    return result


class PublishDispatch:
    def __init__(self, sessions, mutation_lock=None, max_parallel=None):
        self.sessions = sessions
        self.mutation_lock = mutation_lock or threading.Lock()
        self.max_parallel = concurrency_limit(max_parallel)
        self.active = {}
        self.last_error = None

    def reap(self):
        for task_id, entry in list(self.active.items()):
            future = entry['future']
            if future.done():
                if not future.cancelled():
                    error = future.exception()
                    if error:
                        self.last_error = type(error).__name__
                self.active.pop(task_id, None)

    def start(self, task, operation, *, claim=None, batch_id=None, kind='publish'):
        self.reap()
        required = resources(self.sessions, task)
        if (len(self.active) >= self.max_parallel or task['id'] in self.active
                or any(required & entry['resources'] for entry in self.active.values())):
            return False
        # Delete/recovery tools share this short lock. PREPARING/attempted state
        # then protects each task throughout browser I/O, without a global lock.
        if not self.mutation_lock.acquire(blocking=False):
            return False
        try:
            if claim is not None and not claim():
                return False
            self.active[task['id']] = dict(
                future=asyncio.create_task(operation()), resources=required,
                task_id=task['id'], batch_id=batch_id, kind=kind,
                account_id=task['plan']['account_id'], channel=task['plan']['channel'])
            return True
        finally:
            self.mutation_lock.release()

    async def drain(self):
        if self.active:
            await asyncio.gather(*(entry['future'] for entry in list(self.active.values())))
        self.reap()

    async def cancel(self):
        futures = [entry['future'] for entry in list(self.active.values())]
        for future in futures:
            future.cancel()
        await asyncio.gather(*futures, return_exceptions=True)
        self.reap()

    def status(self):
        return [{key: value for key, value in entry.items() if key not in {'future', 'resources'}}
                for entry in list(self.active.values()) if not entry['future'].done()]
