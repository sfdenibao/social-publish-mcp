import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import time
import unittest

from utils.comment_autoreply_store import CommentAutoreplyStore
from utils.comment_autoreply_store import AutoreplyStateError
from utils.comment_autoreply_worker import CommentAutoreplyWorker


def observation(key, text="我想整理工作表格", **raw):
    return {
        "comment_key": key, "author": "测试访客", "text": text,
        "published_label": "刚刚", "fingerprint": key, "platform_cid": None,
        "thread_key": None, "is_own": False, "raw": raw,
    }


class FakeDOM:
    def __init__(self):
        self.comments = []
        self.complete = True
        self.scan_calls = 0
        self.reply_calls = []
        self.send_calls = []
        self.send_state = "VERIFIED"
        self.recheck_state = "UNKNOWN_RESULT"
        self.scan_state = None
        self.scan_started = None
        self.scan_release = None
        self.before_send = None
        self.raise_after_send = False
        self.return_guard_errors = False
        self.scan_error = None
        self.reply_error = None
        self.reply_blocked_code = None
        self.after_send_error = None

    async def scan(self, publish_task_id, video_id, max_scrolls=5):
        self.scan_calls += 1
        if self.scan_error:
            raise self.scan_error
        if self.scan_started is not None:
            self.scan_started.set()
            await self.scan_release.wait()
        if self.scan_state:
            return {"state": self.scan_state, "error_code": "LOGIN_REQUIRED"}
        return {"comments": list(self.comments), "complete": self.complete,
                "evidence": {"screenshot": "test/scan.png"}}

    async def reply(self, publish_task_id, video_id, observed, reply_text,
                    before_submit, recheck_only=False):
        self.reply_calls.append((observed["comment_key"], reply_text, recheck_only))
        if self.reply_error:
            raise self.reply_error
        if self.reply_blocked_code:
            return {"state": "BLOCKED", "error_code": self.reply_blocked_code,
                    "evidence": {"attempted": False}}
        if recheck_only:
            return {"state": self.recheck_state, "evidence": {"checked": True}}
        if self.before_send:
            self.before_send()
        if self.send_state == "BLOCKED":
            return {"state": "BLOCKED", "error_code": "CAPTCHA", "evidence": {}}
        try:
            before_submit()
        except AutoreplyStateError as exc:
            if self.return_guard_errors:
                return {"state": "BLOCKED", "error_code": str(exc),
                        "evidence": {"attempted": False}}
            raise
        self.send_calls.append(observed["comment_key"])
        if self.after_send_error:
            raise self.after_send_error
        if self.raise_after_send:
            raise RuntimeError("Authorization: Bearer secret-must-not-be-saved")
        return {"state": self.send_state, "evidence": {"screenshot": "test/reply.png"}}


class FakeModel:
    def __init__(self):
        self.ready = True
        self.calls = []
        self.generated = {
            "action": "reply", "reply": "先说说表格怎么整理，一次大概要花多久？",
            "reason": "了解具体需求", "model": "test-model", "usage": {},
        }
        self.error = None

    def status(self):
        return {"ready": self.ready, "status": "READY" if self.ready else "MISSING_API_KEY"}

    def generate(self, comment, context):
        self.calls.append((comment, context))
        if self.error:
            raise self.error
        return dict(self.generated)


class CommentAutoreplyWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.now = datetime(2026, 10, 3, 7, tzinfo=timezone.utc)
        self.path = Path(self.temporary.name) / "test.sqlite3"
        self.store = CommentAutoreplyStore(self.path, clock=lambda: self.now)
        self.job = self.store.upsert_job("account", "task", "video", min_reply_interval=0)
        self.job_id = self.job["id"]
        self.dom, self.model = FakeDOM(), FakeModel()
        self.worker = CommentAutoreplyWorker(
            self.store, self.dom, self.model,
            lambda job: {"post_title": "用AI帮忙解决具体的小问题","is_own_video":True,"is_top_level":True,"work_type":"video"},
        )
        self.addCleanup(lambda: self.worker.stop(timeout=2))

    async def baseline(self):
        self.store.set_enabled(self.job_id, True)
        return await self.worker.run_once(self.job_id, force=True)

    async def test_first_complete_scan_only_baselines_history(self):
        self.dom.comments = [observation("old")]
        result = await self.baseline()
        self.assertEqual(result["state"], "BASELINED")
        self.assertEqual(self.store.history(self.job_id)[0]["state"], "BASELINED")
        self.assertEqual(self.dom.send_calls, [])
        self.assertEqual(self.model.calls, [])
        self.dom.comments.append(observation("new"))
        sent = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(sent["processed"][0]["state"], "VERIFIED")
        self.assertEqual(self.dom.send_calls, ["new"])
        await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(self.dom.send_calls, ["new"])
        self.assertEqual(len(self.model.calls), 1)

    async def test_incomplete_initial_scan_does_not_create_baseline(self):
        self.dom.complete = False
        self.dom.comments = [observation("old")]
        result = await self.baseline()
        self.assertEqual(result["state"], "BASELINE_INCOMPLETE")
        self.assertFalse(self.store.get_job(self.job_id)["baseline_complete"])
        self.assertEqual(self.store.history(self.job_id), [])
        self.dom.complete = True
        self.dom.comments.append(observation("also-old"))
        await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(self.store.status(self.job_id)["counts"]["BASELINED"], 2)

    async def test_own_ambiguous_already_replied_and_local_preflight_skip(self):
        await self.baseline()
        own = observation("own")
        own["is_own"] = True
        self.dom.comments = [own, observation("ambiguous", ambiguous=True),
                             observation("answered", already_replied=True),
                             observation("advertisement", "刷粉返佣")]
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(len(result["processed"]), 3)
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.dom.send_calls, [])
        counts = self.store.status(self.job_id)["counts"]
        self.assertEqual((counts["OWN"], counts["HANDOFF"], counts["SKIP"]), (1, 1, 2))

    async def test_unknown_result_is_only_rechecked_never_resent(self):
        await self.baseline()
        self.dom.comments = [observation("new")]
        self.dom.send_state = "UNKNOWN_RESULT"
        await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(self.dom.send_calls, ["new"])
        await self.worker.run_once(self.job_id, force=True)
        self.dom.recheck_state = "VERIFIED"
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(result["processed"][0]["state"], "VERIFIED")
        self.assertEqual(self.dom.send_calls, ["new"])
        self.assertEqual([call[2] for call in self.dom.reply_calls], [False, True, True])
        self.assertEqual(self.store.status(self.job_id)["usage"]["reply_attempts"], 1)

    async def test_force_cannot_bypass_disabled_or_read_only(self):
        await self.baseline()
        self.store.set_enabled(self.job_id, False)
        self.dom.comments = [observation("new")]
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(result["state"], "READ_ONLY")
        self.assertEqual(self.model.calls, [])
        self.store.set_enabled(self.job_id, True)
        result = await self.worker.run_once(self.job_id, send=False, force=True)
        self.assertEqual(result["state"], "READ_ONLY")
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.dom.send_calls, [])

    async def test_pause_immediately_before_click_prevents_send(self):
        await self.baseline()
        self.dom.comments = [observation("new")]
        self.dom.before_send = lambda: self.store.set_enabled(self.job_id, False)
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(result["state"], "BLOCKED")
        self.assertEqual(result["error"], "JOB_PAUSED")
        self.assertEqual(self.dom.send_calls, [])
        self.assertEqual(self.store.ready(self.job_id)[0]["state"], "PREPARED")

    async def test_missing_model_configuration_pauses_without_api_call(self):
        await self.baseline()
        self.model.ready = False
        self.dom.comments = [observation("new")]
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(result["error"], "MISSING_API_KEY")
        self.assertFalse(self.store.get_job(self.job_id)["enabled"])
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.dom.send_calls, [])

    async def test_model_handoff_and_error_never_send(self):
        await self.baseline()
        self.dom.comments = [observation("new")]
        self.model.generated.update(action="handoff", reply="", reason="需要人判断")
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(result["processed"][0]["state"], "HANDOFF")
        self.assertTrue(self.store.get_job(self.job_id)["enabled"])
        self.dom.comments.append(observation("failed"))
        self.model.generated["error_code"] = "RATE_LIMITED"
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(result["error"], "RATE_LIMITED")
        self.assertFalse(self.store.get_job(self.job_id)["enabled"])
        self.assertEqual(self.dom.send_calls, [])

    async def test_model_exception_does_not_leak_message(self):
        await self.baseline()
        self.dom.comments = [observation("new")]
        self.model.error = RuntimeError("api_key=secret_do_not_log")
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(result["error"], "MODEL_ERROR_RUNTIMEERROR")
        self.assertNotIn("secret_do_not_log", str(self.store.history(self.job_id)))
        self.assertNotIn("secret_do_not_log", str(self.worker.status()))

    async def test_daily_send_limit_stops_extra_model_spending(self):
        self.store.upsert_job("account", "task", "video", daily_limit=1, min_reply_interval=0)
        await self.baseline()
        self.dom.comments = [observation("first"), observation("second")]
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(result["error"], "REPLY_DAILY_LIMIT")
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.dom.send_calls, ["first"])
        self.assertEqual(len(self.store.pending(self.job_id)), 1)

    async def test_model_budget_preserves_new_comment_for_later(self):
        self.store.upsert_job("account", "task", "video", max_model_calls_day=1, min_reply_interval=0)
        await self.baseline()
        self.store.reserve_model_call(self.job_id)
        self.dom.comments = [observation("new")]
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(result["error"], "MODEL_DAILY_LIMIT")
        self.assertEqual(len(self.store.pending(self.job_id)), 1)
        self.assertEqual(self.model.calls, [])

    async def test_dom_returned_limit_and_pause_preserve_prepared_draft(self):
        self.store.upsert_job("account", "task", "video", min_reply_interval=60)
        await self.baseline()
        self.dom.return_guard_errors = True
        self.dom.comments = [observation("first"), observation("second")]
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(result["error"], "REPLY_MIN_INTERVAL")
        self.assertTrue(self.store.get_job(self.job_id)["enabled"])
        prepared = self.store.ready(self.job_id)
        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0]["state"], "PREPARED")
        self.now += timedelta(seconds=60)
        self.dom.before_send = lambda: self.store.set_enabled(self.job_id, False)
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(result["error"], "JOB_PAUSED")
        self.assertEqual(self.store.ready(self.job_id)[0]["id"], prepared[0]["id"])
        self.assertEqual(self.dom.send_calls, ["first"])

    async def test_concurrent_run_returns_busy_and_sends_once(self):
        await self.baseline()
        self.dom.comments = [observation("new")]
        self.dom.scan_started, self.dom.scan_release = asyncio.Event(), asyncio.Event()
        first = asyncio.create_task(self.worker.run_once(self.job_id, force=True))
        await self.dom.scan_started.wait()
        second = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(second["state"], "BUSY")
        self.dom.scan_release.set()
        await first
        self.assertEqual(self.dom.send_calls, ["new"])

    async def test_draft_recovery_does_not_regenerate(self):
        await self.baseline()
        self.dom.comments = [observation("new")]
        row = self.store.ingest(self.job_id, self.dom.comments)["comments"][0]
        self.store.claim(row["id"])
        self.store.record_decision(row["id"], {
            "action": "reply", "reply": "你现在整理表格最花时间的是哪一步？", "reason": "需求澄清",
        })
        await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(self.dom.send_calls, ["new"])
        self.assertEqual(self.model.calls, [])

    async def test_scan_login_failure_and_post_click_exception_pause(self):
        await self.baseline()
        self.dom.scan_state = "BLOCKED"
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(result["error"], "LOGIN_REQUIRED")
        self.assertFalse(self.store.get_job(self.job_id)["enabled"])
        self.store.set_enabled(self.job_id, True)
        self.dom.scan_state = None
        self.dom.comments = [observation("new")]
        self.dom.raise_after_send = True
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(result["processed"][0]["state"], "UNKNOWN_RESULT")
        self.assertFalse(self.store.get_job(self.job_id)["enabled"])
        self.assertNotIn("secret-must-not-be-saved", str(result))
        self.assertEqual(self.dom.send_calls, ["new"])

    async def test_exact_profile_busy_scan_keeps_enabled_and_retries_next_scan(self):
        self.store.set_enabled(self.job_id, True)
        self.dom.scan_error = RuntimeError("PROFILE_BUSY")
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual((result["state"], result["error"]), ("BUSY", "PROFILE_BUSY"))
        job = self.store.get_job(self.job_id)
        self.assertTrue(job["enabled"])
        self.assertFalse(job["baseline_complete"])
        self.assertEqual(datetime.fromisoformat(job["next_run"]), self.now + timedelta(seconds=300))
        self.dom.scan_error = None
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(result["state"], "BASELINED")

    async def test_exact_profile_busy_reply_preserves_prepared_then_sends_once(self):
        await self.baseline()
        self.dom.comments = [observation("new")]
        self.dom.reply_error = RuntimeError("PROFILE_BUSY")
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual((result["state"], result["error"]), ("LIMITED", "PROFILE_BUSY"))
        self.assertTrue(self.store.get_job(self.job_id)["enabled"])
        self.assertEqual(self.store.ready(self.job_id)[0]["state"], "PREPARED")
        self.assertEqual(self.dom.send_calls, [])
        self.dom.reply_error = None
        self.dom.reply_blocked_code = "PROFILE_BUSY"
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(result["state"], "LIMITED")
        self.assertEqual(self.store.ready(self.job_id)[0]["state"], "PREPARED")
        self.dom.reply_blocked_code = None
        await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(self.dom.send_calls, ["new"])
        self.assertEqual(len(self.model.calls), 1)

    async def test_profile_busy_after_attempt_stays_unknown_and_is_never_resent(self):
        await self.baseline()
        self.dom.comments = [observation("new")]
        self.dom.after_send_error = RuntimeError("PROFILE_BUSY")
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(result["processed"][0]["state"], "UNKNOWN_RESULT")
        self.assertTrue(self.store.get_job(self.job_id)["enabled"])
        self.dom.after_send_error = None
        self.dom.recheck_state = "VERIFIED"
        await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(self.dom.send_calls, ["new"])
        self.assertEqual(self.dom.reply_calls[-1][2], True)

    async def test_unrelated_lock_message_does_not_get_profile_busy_retry(self):
        self.store.set_enabled(self.job_id, True)
        self.dom.scan_error = RuntimeError("PROFILE_BUSY: unknown extra details")
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(result["state"], "BLOCKED")
        self.assertFalse(self.store.get_job(self.job_id)["enabled"])

    async def test_per_round_limit_and_due_time(self):
        await self.baseline()
        self.dom.comments = [observation(str(i)) for i in range(5)]
        skipped = await self.worker.run_once(self.job_id)
        self.assertEqual(skipped["state"], "NOT_DUE")
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(len(result["processed"]), 3)
        self.assertEqual(len(self.dom.send_calls), 3)
        self.assertEqual(len(self.store.pending(self.job_id)), 2)

    async def test_background_start_stop_keeps_job_enabled_for_restart(self):
        self.store.set_enabled(self.job_id, True)
        status = self.worker.start()
        self.assertTrue(status["running"])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.store.get_job(self.job_id)["baseline_complete"]:
            await asyncio.sleep(0.02)
        result = await asyncio.to_thread(self.worker.stop, 2)
        self.assertFalse(result["running"])
        self.assertTrue(self.store.get_job(self.job_id)["enabled"])
        self.assertTrue(self.store.get_job(self.job_id)["baseline_complete"])
        self.assertEqual(self.dom.send_calls, [])
        # Stopping the scheduler does not override an explicitly requested
        # one-off execution when the persisted job is still enabled.
        self.dom.comments = [observation("manual")]
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(result["processed"][0]["state"], "VERIFIED")

    async def test_unlimited_processes_finite_scan_beyond_old_caps_once(self):
        self.store.upsert_job("account", "task", "video", daily_limit=None,
                              max_model_calls_day=None, min_reply_interval=0)
        self.worker.max_per_run = None
        await self.baseline()
        self.dom.comments = [observation(str(index)) for index in range(45)]
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(len(result["processed"]), 45)
        self.assertEqual(len(self.dom.send_calls), 45)
        self.assertEqual(len(self.model.calls), 45)
        self.assertEqual(self.dom.scan_calls, 2)
        await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(len(self.dom.send_calls), 45)
        self.assertEqual(len(self.model.calls), 45)

    async def test_unlimited_has_no_hidden_hundred_row_queue_limit(self):
        self.store.upsert_job("account", "task", "video", daily_limit=None,
                              max_model_calls_day=None, min_reply_interval=0)
        self.worker.max_per_run = None
        await self.baseline()
        self.dom.comments = [observation(str(index), "刷粉返佣") for index in range(105)]
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(len(result["processed"]), 105)
        self.assertTrue(all(item["state"] == "SKIP" for item in result["processed"]))
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.dom.send_calls, [])
        self.assertEqual(self.dom.scan_calls, 2)

    async def test_pause_during_unlimited_round_prevents_further_sends(self):
        self.store.upsert_job("account", "task", "video", daily_limit=None,
                              max_model_calls_day=None, min_reply_interval=0)
        self.worker.max_per_run = None
        await self.baseline()
        self.dom.comments = [observation(str(index)) for index in range(8)]

        def pause_third_send():
            if len(self.dom.send_calls) == 2:
                self.store.set_enabled(self.job_id, False)

        self.dom.before_send = pause_third_send
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(result["error"], "JOB_PAUSED")
        self.assertEqual(len(self.dom.send_calls), 2)
        self.assertFalse(self.store.get_job(self.job_id)["enabled"])
        self.assertEqual(self.store.ready(self.job_id)[0]["state"], "PREPARED")
        self.assertEqual(len(self.store.pending(self.job_id)), 5)

    async def test_unlimited_rechecks_all_observed_unknowns_without_resending(self):
        self.store.upsert_job("account", "task", "video", daily_limit=None,
                              max_model_calls_day=None, min_reply_interval=0)
        self.worker.max_per_run = None
        await self.baseline()
        self.dom.send_state = "UNKNOWN_RESULT"
        self.dom.comments = [observation(str(index)) for index in range(4)]
        await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(len(self.dom.send_calls), 4)
        self.dom.recheck_state = "VERIFIED"
        result = await self.worker.run_once(self.job_id, force=True)
        self.assertEqual(len(result["processed"]), 4)
        self.assertTrue(all(item["recheck_only"] for item in result["processed"]))
        self.assertEqual(len(self.dom.send_calls), 4)
        self.assertEqual(len(self.model.calls), 4)
        self.assertEqual(self.store.status(self.job_id)["counts"]["VERIFIED"], 4)

    async def test_round_limit_accepts_none_or_positive_integer_not_zero(self):
        for value in (None, 1, 30):
            worker = CommentAutoreplyWorker(self.store, self.dom, self.model,
                                            lambda job: {}, max_per_run=value)
            self.assertEqual(worker.max_per_run, value)
            self.assertEqual(worker.status()["max_per_run"], value)
        for value in (0, -1, True, 1.5, "None"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CommentAutoreplyWorker(self.store, self.dom, self.model,
                                       lambda job: {}, max_per_run=value)


if __name__ == "__main__":
    unittest.main()
