import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import tempfile
import time
import unittest

from utils.douyin_notification_store import NotificationStore
from utils.douyin_notification_worker import NotificationWorker


def notice(key="one", **changes):
    value = {"notification_key": key, "author": "访客", "text": "想整理我工作里的表格",
             "kind": "comment", "display_time": "昨天", "source_profile_path": "/user/test-person",
             "cover_path": "/cover/work", "raw": {"dom_index": 0}}
    value.update(changes)
    return value


class FakeAdapter:
    def __init__(self):
        self.notices = []
        self.scan_calls = 0
        self.context_calls = []
        self.reply_calls = []
        self.sent = []
        self.rechecks = []
        self.scan_error = None
        self.context_errors = {}
        self.reply_error = None
        self.reply_status = "VERIFIED"
        self.after_send_error = None
        self.before_send = None
        self.recheck_status = "VERIFIED"
        self.recheck_error = None
        self.context_overrides = {}
        self.scan_started = None
        self.scan_release = None

    async def scan(self, account):
        self.scan_calls += 1
        if self.scan_error:
            raise self.scan_error
        if self.scan_started is not None:
            self.scan_started.set()
            await self.scan_release.wait()
        return {"notices": list(self.notices), "scope": "visible_notification_page",
                "evidence": {"screenshot_path": "test/notification-scan.png"}}

    async def read_context(self, account, observed):
        key = observed["notification_key"]
        self.context_calls.append(key)
        if key in self.context_errors:
            raise self.context_errors[key]
        return {"comment": observed["text"], "parent_comment": "这个具体麻烦是什么？",
                "post_title": f"实际作品-{key}", "post_description": "来源于这条通知打开的作品",
                "self_handle": "test-self", "is_own_video": True, "is_top_level": True,
                "work_type": "video", **self.context_overrides.get(key, {})}

    async def reply(self, account, observed, text, *, before_submit):
        key = observed["notification_key"]
        self.reply_calls.append(key)
        if self.reply_error:
            raise self.reply_error
        if self.before_send:
            self.before_send()
        before_submit()
        self.sent.append((key, text))
        if self.after_send_error:
            raise self.after_send_error
        return {"status": self.reply_status, "evidence": {"screenshot_path": "test/notification-reply.png"}}

    async def recheck(self, account, observed, text):
        self.rechecks.append(observed["notification_key"])
        if self.recheck_error:
            raise self.recheck_error
        return {"status": self.recheck_status, "evidence": {"read_only": True}}


class FakeModel:
    def __init__(self):
        self.ready = True
        self.calls = []
        self.status_calls = 0
        self.error = None
        self.result = {"action": "reply", "reply": "表格里最花时间的是哪一步？", "reason": "了解具体需求",
                       "model": "test-model", "provider": "test", "usage": {"total_tokens": 15}}

    def status(self):
        self.status_calls += 1
        return {"ready": self.ready, "status": "READY" if self.ready else "MISSING_API_KEY"}

    def generate(self, text, context):
        self.calls.append((text, context))
        if self.error:
            raise self.error
        return dict(self.result)


class NotificationWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.now = datetime(2026, 10, 3, 7, tzinfo=timezone.utc)
        self.path = Path(temporary.name) / "notification-test.sqlite3"
        self.store = NotificationStore(self.path, clock=lambda: self.now)
        self.account = "test-account"
        self.store.configure(self.account)
        self.adapter, self.model = FakeAdapter(), FakeModel()
        self.worker = NotificationWorker(self.store, self.adapter, self.model, tick_seconds=0.05)
        self.addCleanup(lambda: self.worker.stop(timeout=2))

    async def send(self):
        self.store.set_enabled(self.account, True)
        return await self.worker.run_once(self.account, send=True, force=True)

    async def test_read_only_registers_visible_comments_without_baseline_or_send(self):
        self.adapter.notices = [notice(), notice("like", kind="likes")]
        result = await self.worker.run_once(self.account, force=True)
        self.assertEqual(result["status"], "SCANNED_READ_ONLY")
        self.assertEqual(self.store.get(self.account, "one")["state"], "NEW")
        self.assertEqual(self.store.get(self.account, "like")["state"], "SKIP")
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.adapter.context_calls, [])
        self.assertEqual(self.adapter.sent, [])

    async def test_first_send_uses_actual_context_and_deduplicates_later_scans(self):
        self.adapter.notices = [notice(), notice("second")]
        result = await self.send()
        self.assertEqual(result["status"], "PROCESSED")
        self.assertEqual([row[0] for row in self.adapter.sent], ["one", "second"])
        self.assertEqual([call[1]["post_title"] for call in self.model.calls],
                         ["实际作品-one", "实际作品-second"])
        await self.worker.run_once(self.account, send=True, force=True)
        self.assertEqual(len(self.adapter.sent), 2)
        self.assertEqual(len(self.model.calls), 2)
        self.assertEqual(self.store.get(self.account, "one")["context"]["post_title"], "实际作品-one")

    async def test_disabled_cannot_be_bypassed_by_send_or_force(self):
        self.adapter.notices = [notice()]
        result = await self.worker.run_once(self.account, send=True, force=True)
        self.assertEqual(result["status"], "SCANNED_READ_ONLY")
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.adapter.sent, [])

    async def test_target_failure_handoffs_one_and_continues_next(self):
        self.adapter.notices = [notice("bad"), notice("good")]
        self.adapter.context_errors["bad"] = RuntimeError("PUBLIC_COMMENT_TARGET_NOT_UNIQUE")
        result = await self.send()
        self.assertEqual([row["status"] for row in result["processed"]], ["HANDOFF", "VERIFIED"])
        self.assertEqual([row[0] for row in self.adapter.sent], ["good"])
        self.assertTrue(self.store.status(self.account)["settings"]["enabled"])

    async def test_actual_comment_mismatch_does_not_send_to_wrong_target(self):
        self.adapter.notices = [notice("bad"), notice("good")]
        self.adapter.context_overrides["bad"] = {"comment": "另一个人的内容"}
        result = await self.send()
        self.assertEqual(result["processed"][0]["error"], "TARGET_COMMENT_MISMATCH")
        self.assertEqual([row[0] for row in self.adapter.sent], ["good"])

    async def test_profile_busy_scan_preserves_settings_for_retry(self):
        self.adapter.scan_error = RuntimeError("PROFILE_BUSY")
        result = await self.send()
        self.assertEqual(result["status"], "BUSY")
        settings = self.store.status(self.account)["settings"]
        self.assertTrue(settings["enabled"])
        self.assertEqual(datetime.fromisoformat(settings["next_run"]), self.now + timedelta(seconds=300))
        self.adapter.scan_error = None
        self.adapter.notices = [notice()]
        await self.worker.run_once(self.account, send=True, force=True)
        self.assertEqual(len(self.adapter.sent), 1)

    async def test_profile_busy_reply_keeps_prepared_without_regenerating(self):
        self.adapter.notices = [notice()]
        self.adapter.reply_error = RuntimeError("PROFILE_BUSY")
        first = await self.send()
        self.assertEqual(first["status"], "BUSY")
        self.assertEqual(self.store.get(self.account, "one")["state"], "PREPARED")
        self.assertTrue(self.store.status(self.account)["settings"]["enabled"])
        self.adapter.reply_error = None
        self.adapter.notices = [notice(display_time="前天", raw={"dom_index": 7})]
        await self.worker.run_once(self.account, send=True, force=True)
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.store.get(self.account, "one")["raw"]["dom_index"], 7)

    async def test_transient_dom_timeouts_preserve_retry_state_and_send_guard(self):
        self.adapter.notices = [notice()]
        self.adapter.scan_error = TimeoutError("temporary navigation timeout")
        scanned = await self.send()
        self.assertEqual(scanned["status"], "RETRY")
        self.assertTrue(self.store.status(self.account)["settings"]["enabled"])
        self.assertEqual(self.store.pending(self.account), [])
        self.adapter.scan_error = None
        self.adapter.context_errors["one"] = TimeoutError("temporary context timeout")
        contextual = await self.worker.run_once(self.account, send=True, force=True)
        self.assertEqual(contextual["status"], "RETRY")
        self.assertEqual(self.store.get(self.account, "one")["state"], "NEW")
        self.adapter.context_errors.clear()
        self.adapter.reply_error = TimeoutError("temporary pre-submit timeout")
        prepared = await self.worker.run_once(self.account, send=True, force=True)
        self.assertEqual(prepared["status"], "RETRY")
        self.assertEqual(self.store.get(self.account, "one")["state"], "PREPARED")
        self.assertEqual(len(self.model.calls), 1)
        self.adapter.reply_error = None
        self.adapter.after_send_error = TimeoutError("verification timed out after submit")
        attempted = await self.worker.run_once(self.account, send=True, force=True)
        self.assertEqual(attempted["status"], "RETRY")
        self.assertEqual(self.store.get(self.account, "one")["state"], "UNKNOWN_RESULT")
        self.assertEqual(len(self.adapter.sent), 1)
        self.adapter.after_send_error = None
        self.adapter.recheck_error = TimeoutError("temporary read-only check timeout")
        checked = await self.worker.run_once(self.account, send=True, force=True)
        self.assertEqual(checked["status"], "RETRY")
        self.assertEqual(self.store.get(self.account, "one")["state"], "UNKNOWN_RESULT")
        settings = self.store.status(self.account)["settings"]
        self.assertTrue(settings["enabled"])
        self.assertEqual(datetime.fromisoformat(settings["next_run"]), self.now + timedelta(seconds=300))
        self.adapter.recheck_error = None
        await self.worker.run_once(self.account, send=True, force=True)
        self.assertEqual(self.store.get(self.account, "one")["state"], "VERIFIED")
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(len(self.model.calls), 1)

    async def test_unknown_is_only_rechecked_and_never_resent(self):
        self.adapter.notices = [notice()]
        self.adapter.reply_status = "UNKNOWN_RESULT"
        await self.send()
        self.assertEqual(self.store.get(self.account, "one")["state"], "UNKNOWN_RESULT")
        await self.worker.run_once(self.account, send=True, force=True)
        self.assertEqual(self.adapter.rechecks, ["one"])
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.store.get(self.account, "one")["state"], "VERIFIED")

    async def test_missing_recheck_adapter_keeps_unknown_without_resend(self):
        self.adapter.notices = [notice()]
        self.adapter.reply_status = "UNKNOWN_RESULT"
        await self.send()
        self.adapter.recheck = None
        result = await self.worker.run_once(self.account, send=True, force=True)
        self.assertEqual(result["processed"][0]["error"], "RECHECK_UNAVAILABLE")
        self.assertEqual(self.store.get(self.account, "one")["state"], "UNKNOWN_RESULT")
        self.assertEqual(len(self.adapter.sent), 1)

    async def test_exception_after_attempt_retains_guard_and_sanitizes_error(self):
        self.adapter.notices = [notice()]
        self.adapter.after_send_error = RuntimeError("Authorization: Bearer never-log-this")
        result = await self.send()
        self.assertEqual(result["processed"][0]["status"], "UNKNOWN_RESULT")
        self.assertFalse(self.store.status(self.account)["settings"]["enabled"])
        self.assertNotIn("never-log-this", str(self.store.history(self.account)))
        self.adapter.after_send_error = None
        await self.send()
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(self.adapter.rechecks, ["one"])

    async def test_pause_before_submit_preserves_draft_and_blocks_click(self):
        self.adapter.notices = [notice()]
        self.adapter.before_send = lambda: self.store.set_enabled(self.account, False)
        result = await self.send()
        self.assertEqual(result["status"], "PAUSED")
        self.assertEqual(result["processed"][0]["error"], "NOTIFICATIONS_PAUSED")
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(self.store.get(self.account, "one")["state"], "PREPARED")

    async def test_missing_model_and_model_error_pause_before_sending(self):
        self.adapter.notices = [notice()]
        self.model.ready = False
        result = await self.send()
        self.assertEqual(result["error"], "MISSING_API_KEY")
        self.assertEqual(self.store.get(self.account, "one")["state"], "NEW")
        self.model.ready = True
        self.model.result.update(action="handoff", reply="", error_code="RATE_LIMITED")
        result = await self.send()
        self.assertEqual(result["processed"][0]["error"], "RATE_LIMITED")
        self.assertFalse(self.store.status(self.account)["settings"]["enabled"])
        self.assertEqual(self.adapter.sent, [])

    async def test_model_handoff_and_local_skip_record_actual_context(self):
        self.adapter.notices = [notice("ad", text="刷粉返佣"), notice("handoff")]
        self.model.result.update(action="handoff", reply="", reason="需要用户本人判断")
        result = await self.send()
        self.assertEqual([row["status"] for row in result["processed"]], ["SKIP", "HANDOFF"])
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.store.get(self.account, "handoff")["evidence"]["context"]["post_title"],
                         "实际作品-handoff")

    async def test_no_daily_or_round_quantity_limit_and_pause_during_iteration(self):
        self.adapter.notices = [notice(str(index)) for index in range(45)]
        result = await self.send()
        self.assertEqual(len(result["processed"]), 45)
        self.assertEqual(len(self.adapter.sent), 45)
        self.assertEqual(self.adapter.scan_calls, 1)
        self.adapter.notices.extend(notice(str(index)) for index in range(45, 50))

        def pause_next():
            if len(self.adapter.sent) == 47:
                self.store.set_enabled(self.account, False)

        self.adapter.before_send = pause_next
        await self.worker.run_once(self.account, send=True, force=True)
        self.assertEqual(len(self.adapter.sent), 47)
        self.assertEqual(len(self.store.prepared(self.account)), 1)
        self.assertEqual(len(self.store.pending(self.account)), 2)

    async def test_global_lock_blocks_overlapping_runs_even_different_accounts(self):
        self.adapter.notices = [notice()]
        self.store.set_enabled(self.account, True)
        self.store.configure("other-account")
        self.adapter.scan_started, self.adapter.scan_release = asyncio.Event(), asyncio.Event()
        first = asyncio.create_task(self.worker.run_once(self.account, send=True, force=True))
        await self.adapter.scan_started.wait()
        second = await self.worker.run_once("other-account", send=True, force=True)
        self.assertEqual(second["status"], "BUSY")
        self.adapter.scan_release.set()
        await first
        self.assertEqual(len(self.adapter.sent), 1)

    async def test_login_failure_stops_other_targets_and_pauses(self):
        self.adapter.notices = [notice("first"), notice("second")]
        self.adapter.context_errors["first"] = RuntimeError("LOGIN_REQUIRED")
        result = await self.send()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(self.store.status(self.account)["settings"]["enabled"])
        self.assertEqual(self.store.get(self.account, "second")["state"], "NEW")

    async def test_prepared_context_change_handoffs_without_reusing_stale_reply(self):
        self.adapter.notices = [notice()]
        self.adapter.reply_error = RuntimeError("PROFILE_BUSY")
        await self.send()
        self.adapter.reply_error = None
        self.adapter.context_overrides["one"] = {"parent_comment": "已变成另一个问题"}
        result = await self.worker.run_once(self.account, send=True, force=True)
        self.assertEqual(result["processed"][0]["error"], "PREPARED_CONTEXT_CHANGED")
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(len(self.model.calls), 1)

    async def test_background_restores_enabled_accounts_and_respects_due_time(self):
        self.adapter.notices = [notice()]
        self.store.set_enabled(self.account, True)
        self.assertTrue(self.worker.start()["running"])
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            rows = self.store.history(self.account)
            if rows and rows[0]["state"] == "VERIFIED":
                break
            await asyncio.sleep(0.02)
        stopped = await asyncio.to_thread(self.worker.stop, 2)
        self.assertFalse(stopped["running"])
        self.assertTrue(self.store.status(self.account)["settings"]["enabled"])
        self.assertEqual(len(self.adapter.sent), 1)
        result = await self.worker.run_once(self.account, send=True)
        self.assertEqual(result["status"], "NOT_DUE")

    async def test_old_reply_new_prepared_and_unknown_never_open_or_send(self):
        self.store.set_enabled(self.account, True)
        self.store.ingest(self.account, [notice(key) for key in ("new", "prepared", "unknown")])
        context = {"is_own_video": True, "is_top_level": True, "work_type": "video"}
        self.store.prepare(self.account, "prepared", "之前的草稿", context=context)
        self.store.prepare(self.account, "unknown", "之前的回复", context=context)
        self.store.mark_attempted(self.account, "unknown")
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE douyin_notifications SET kind = 'reply'")
            connection.commit()
        # Keep old rows outside the new visible scan to also test worker-side
        # eligibility, independently of ingest converting visible rows to SKIP.
        self.adapter.notices = []
        result = await self.worker.run_once(self.account, send=True, force=True)
        self.assertEqual([row["status"] for row in result["processed"]], ["SKIP", "SKIP"])
        self.assertEqual(self.store.get(self.account, "new")["state"], "SKIP")
        self.assertEqual(self.store.get(self.account, "prepared")["state"], "SKIP")
        self.assertEqual(self.store.get(self.account, "unknown")["state"], "UNKNOWN_RESULT")
        self.assertEqual(self.adapter.context_calls, [])
        self.assertEqual(self.adapter.rechecks, [])
        self.assertEqual(self.adapter.reply_calls, [])
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.model.status_calls, 0)

    async def test_scope_false_or_non_video_skips_without_model_calls(self):
        self.adapter.notices = [notice("other-owner"), notice("nested"), notice("image")]
        self.adapter.context_overrides = {
            "other-owner": {"is_own_video": False}, "nested": {"is_top_level": False},
            "image": {"work_type": "image"},
        }
        result = await self.send()
        self.assertEqual([row["status"] for row in result["processed"]], ["SKIP", "SKIP", "SKIP"])
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.model.status_calls, 0)
        self.assertEqual(self.adapter.sent, [])

    async def test_scope_missing_or_truthy_non_bool_handoffs_without_model_calls(self):
        self.adapter.notices = [notice("owner-unknown"), notice("depth-unknown"),
                                notice("type-unknown"), notice("truthy")]
        self.adapter.context_overrides = {
            "owner-unknown": {"is_own_video": None}, "depth-unknown": {"is_top_level": None},
            "type-unknown": {"work_type": None}, "truthy": {"is_own_video": 1},
        }
        result = await self.send()
        self.assertTrue(all(row["status"] == "HANDOFF" for row in result["processed"]))
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.model.status_calls, 0)
        self.assertEqual(self.adapter.sent, [])

    async def test_submit_callback_rechecks_persisted_scope_before_guard(self):
        self.adapter.notices = [notice()]

        def revoke_saved_scope():
            with closing(sqlite3.connect(self.path)) as connection:
                context = self.store.get(self.account, "one")["context"]
                context["is_top_level"] = False
                connection.execute("UPDATE douyin_notifications SET context_json = ? WHERE notification_key = 'one'",
                                   (json.dumps(context, ensure_ascii=False),))
                connection.commit()

        self.adapter.before_send = revoke_saved_scope
        result = await self.send()
        self.assertEqual(result["processed"][0]["status"], "SKIP")
        self.assertEqual(self.adapter.sent, [])
        self.assertIsNone(self.store.get(self.account, "one")["attempted_at"])

    async def test_unknown_without_saved_scope_is_preserved_without_target_open(self):
        self.store.set_enabled(self.account, True)
        self.store.ingest(self.account, [notice()])
        self.store.prepare(self.account, "one", "旧回复", context={})
        self.store.mark_attempted(self.account, "one")
        await self.worker.run_once(self.account, send=True, force=True)
        self.assertEqual(self.store.get(self.account, "one")["state"], "UNKNOWN_RESULT")
        self.assertEqual(self.adapter.context_calls, [])
        self.assertEqual(self.adapter.rechecks, [])
        self.assertEqual(self.adapter.sent, [])


if __name__ == "__main__":
    unittest.main()
