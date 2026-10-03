from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from utils.comment_autoreply_store import (
    AutoreplyLimitError, AutoreplyStateError, CommentAutoreplyStore,
)


class CommentAutoreplyStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "autoreply.sqlite3"
        self.now = datetime(2026, 10, 3, 7, 0, tzinfo=timezone.utc)
        self.store = CommentAutoreplyStore(self.path, clock=lambda: self.now)
        self.job = self.store.upsert_job("account", "publish-task", "video")
        self.job_id = self.job["id"]

    @staticmethod
    def observed(key="one", **overrides):
        result = {
            "comment_key": key, "author": "访客", "text": "整理表格能帮忙吗？",
            "published_label": "刚刚", "fingerprint": f"fingerprint-{key}",
            "platform_cid": None, "thread_key": None, "is_own": False,
            "raw": {"visible": True},
        }
        result.update(overrides)
        return result

    def enabled(self):
        return self.store.set_enabled(self.job_id, True)

    def new_comment(self, key="one"):
        if not self.store.get_job(self.job_id)["baseline_complete"]:
            self.store.ingest(self.job_id, [])
        result = self.store.ingest(self.job_id, [self.observed(key)])
        return result["comments"][0]

    def prepared(self, key="one"):
        self.enabled()
        comment = self.new_comment(key)
        self.assertIsNotNone(self.store.claim(comment["id"]))
        self.store.reserve_model_call(self.job_id)
        self.store.record_decision(comment["id"], {
            "action": "reply", "reply": "可以先说说你现在怎么整理，一次大概要花多久？",
            "reason": "询问具体需求",
        }, {"model": "test-only", "latency_ms": 10})
        return self.store.prepare(comment["id"])

    def test_initial_baseline_and_own_never_enter_pending(self):
        self.enabled()
        result = self.store.ingest(self.job_id, [
            self.observed(), self.observed("own", author="包包", is_own=True),
        ])
        self.assertTrue(result["baseline"])
        self.assertEqual([c["state"] for c in result["comments"]], ["BASELINED", "OWN"])
        self.assertEqual(self.store.pending(self.job_id), [])
        for comment in result["comments"]:
            self.assertIsNone(self.store.claim(comment["id"]))
        new = self.store.ingest(self.job_id, [self.observed("later")])
        self.assertFalse(new["baseline"])
        self.assertEqual(new["comments"][0]["state"], "NEW")
        self.assertEqual(len(self.store.pending(self.job_id)), 1)

    def test_empty_first_scan_baselines_and_restart_preserves_enable(self):
        self.assertFalse(self.job["enabled"])
        self.enabled()
        self.store.ingest(self.job_id, [])
        reopened = CommentAutoreplyStore(self.path, clock=lambda: self.now)
        job = reopened.get_job(self.job_id)
        self.assertTrue(job["enabled"])
        self.assertTrue(job["baseline_complete"])
        updated = reopened.upsert_job("account", "publish-task", "video", daily_limit=3)
        self.assertEqual(updated["id"], self.job_id)
        self.assertTrue(updated["enabled"])
        self.assertEqual(updated["daily_limit"], 3)
        with self.assertRaises(AutoreplyStateError):
            reopened.upsert_job("account", "different-publish", "video")
        with self.assertRaises(AutoreplyStateError):
            reopened.upsert_job("other", "publish-task", "other", job_id=self.job_id)

    def test_paused_job_cannot_claim_spend_or_attempt(self):
        comment = self.new_comment()
        self.assertIsNone(self.store.claim(comment["id"]))
        with self.assertRaisesRegex(AutoreplyStateError, "JOB_PAUSED"):
            self.store.reserve_model_call(self.job_id)
        prepared = self.prepared("second")
        self.store.set_enabled(self.job_id, False)
        with self.assertRaisesRegex(AutoreplyStateError, "JOB_PAUSED"):
            self.store.mark_attempted(prepared["id"])
        self.assertIsNone(self.store.get_comment(prepared["id"])["attempted_at"])
        self.assertEqual(self.store.status(self.job_id)["usage"]["reply_attempts"], 0)

    def test_identity_is_immutable_and_failed_scan_is_atomic(self):
        self.store.ingest(self.job_id, [self.observed()])
        with self.assertRaisesRegex(AutoreplyStateError, "COMMENT_IDENTITY_CONFLICT"):
            self.store.ingest(self.job_id, [
                self.observed("new"), self.observed(text="changed"),
            ])
        self.assertEqual(len(self.store.history(self.job_id)), 1)
        later = self.store.ingest(self.job_id, [self.observed(published_label="1天前")])
        self.assertEqual(later["comments"][0]["published_label"], "刚刚")
        self.assertEqual(later["counts"]["existing"], 1)

    def test_parallel_claims_have_one_winner(self):
        self.enabled()
        comment = self.new_comment()
        barrier = threading.Barrier(6)

        def claim(_):
            other = CommentAutoreplyStore(self.path, clock=lambda: self.now)
            barrier.wait(timeout=10)
            return other.claim(comment["id"])

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(claim, range(6)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.store.get_comment(comment["id"])["state"], "GENERATING")

    def test_attempt_is_durable_at_most_once_including_failed_result(self):
        prepared = self.prepared()
        attempted = self.store.mark_attempted(prepared["id"])
        self.assertEqual(attempted["state"], "UNKNOWN_RESULT")
        reopened = CommentAutoreplyStore(self.path, clock=lambda: self.now)
        with self.assertRaisesRegex(AutoreplyStateError, "DUPLICATE_SUBMISSION_BLOCKED"):
            reopened.mark_attempted(prepared["id"])
        reopened.finish(prepared["id"], "FAILED", {"error": "verification timeout"})
        self.assertIsNone(reopened.claim(prepared["id"]))
        with self.assertRaises(AutoreplyStateError):
            reopened.prepare(prepared["id"])
        with self.assertRaises(AutoreplyStateError):
            reopened.mark_attempted(prepared["id"])
        self.assertEqual(reopened.status(self.job_id)["usage"]["reply_attempts"], 1)
        # A later independent read may verify the already attempted reply.
        reopened.finish(prepared["id"], "VERIFIED", {"reply_visible": True})
        with self.assertRaises(AutoreplyStateError):
            reopened.finish(prepared["id"], "FAILED", {"error": "later issue"})

    def test_concurrent_send_guard_allows_exactly_one_attempt(self):
        prepared = self.prepared()
        barrier = threading.Barrier(6)

        def attempt(_):
            barrier.wait(timeout=10)
            try:
                return self.store.mark_attempted(prepared["id"])
            except AutoreplyStateError:
                return None

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(attempt, range(6)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.store.status(self.job_id)["usage"]["reply_attempts"], 1)

    def test_daily_limits_count_unknown_and_reset_at_shanghai_midnight(self):
        self.store.upsert_job("account", "publish-task", "video", daily_limit=1,
                              max_model_calls_day=2, min_reply_interval=0)
        self.now = datetime(2026, 10, 3, 15, 59, 59, tzinfo=timezone.utc)
        first = self.prepared()
        second = self.prepared("second")
        self.store.mark_attempted(first["id"])
        with self.assertRaisesRegex(AutoreplyLimitError, "REPLY_DAILY_LIMIT"):
            self.store.mark_attempted(second["id"])
        with self.assertRaisesRegex(AutoreplyLimitError, "MODEL_DAILY_LIMIT"):
            self.store.reserve_model_call(self.job_id)
        self.assertEqual(self.store.status(self.job_id)["usage"], {
            "day": "2026-10-03", "model_calls": 2, "reply_attempts": 1,
        })
        self.now += timedelta(seconds=1)
        self.store.reserve_model_call(self.job_id)
        self.store.mark_attempted(second["id"])
        self.assertEqual(self.store.status(self.job_id)["usage"], {
            "day": "2026-10-04", "model_calls": 1, "reply_attempts": 1,
        })

    def test_concurrent_model_reservations_do_not_exceed_budget(self):
        self.store.upsert_job("account", "publish-task", "video", max_model_calls_day=2)
        self.enabled()
        barrier = threading.Barrier(6)

        def reserve(_):
            barrier.wait(timeout=10)
            try:
                return self.store.reserve_model_call(self.job_id)
            except AutoreplyLimitError:
                return None

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(reserve, range(6)))
        self.assertEqual(sum(result is not None for result in results), 2)
        self.assertEqual(self.store.status(self.job_id)["usage"]["model_calls"], 2)

    def test_minimum_reply_interval_uses_attempts_across_days(self):
        first = self.prepared()
        second = self.prepared("second")
        self.now = datetime(2026, 10, 3, 15, 59, 59, tzinfo=timezone.utc)
        self.store.mark_attempted(first["id"])
        self.now += timedelta(seconds=1)
        with self.assertRaisesRegex(AutoreplyLimitError, "REPLY_MIN_INTERVAL"):
            self.store.mark_attempted(second["id"])
        self.now += timedelta(seconds=59)
        self.store.mark_attempted(second["id"])

    def test_stale_generation_recovers_but_draft_and_attempt_do_not(self):
        self.enabled()
        generating = self.new_comment("generating")
        self.store.claim(generating["id"])
        prepared = self.prepared("prepared")
        attempted = self.prepared("attempted")
        self.store.mark_attempted(attempted["id"])
        self.now += timedelta(seconds=901)
        self.assertEqual(self.store.recover_stale_generating(self.job_id), 1)
        self.assertEqual(self.store.get_comment(generating["id"])["state"], "NEW")
        self.assertEqual(self.store.get_comment(prepared["id"])["state"], "PREPARED")
        self.assertEqual(self.store.get_comment(attempted["id"])["state"], "UNKNOWN_RESULT")

    def test_restart_lists_drafts_and_unknown_in_disjoint_queues(self):
        prepared = self.prepared("prepared")
        attempted = self.prepared("attempted")
        self.store.mark_attempted(attempted["id"])
        draft = self.new_comment("draft")
        self.store.claim(draft["id"])
        self.store.record_decision(draft["id"], {"action": "reply", "reply": "先聊聊具体麻烦。"})
        reopened = CommentAutoreplyStore(self.path, clock=lambda: self.now)
        self.assertEqual({item["id"] for item in reopened.ready(self.job_id)},
                         {prepared["id"], draft["id"]})
        self.assertEqual([item["id"] for item in reopened.pending_unknown(self.job_id)],
                         [attempted["id"]])
        self.assertEqual(reopened.pending(self.job_id), [])
        self.assertEqual(len(reopened.history(self.job_id, state="DRAFT")), 1)
        with self.assertRaises(ValueError):
            reopened.history(self.job_id, state="INVALID")

    def test_only_unused_generation_claim_can_be_released(self):
        self.enabled()
        comment = self.new_comment()
        self.store.claim(comment["id"])
        self.assertEqual(self.store.release_claim(comment["id"])["state"], "NEW")
        with self.assertRaises(AutoreplyStateError):
            self.store.release_claim(comment["id"])
        prepared = self.prepared("prepared")
        with self.assertRaises(AutoreplyStateError):
            self.store.release_claim(prepared["id"])
        self.store.mark_attempted(prepared["id"])
        with self.assertRaises(AutoreplyStateError):
            self.store.release_claim(prepared["id"])

    def test_skip_handoff_and_pre_attempt_failure_are_terminal_for_worker(self):
        self.enabled()
        for action, expected in (("skip", "SKIP"), ("handoff", "HANDOFF")):
            comment = self.new_comment(action)
            self.store.claim(comment["id"])
            result = self.store.record_decision(comment["id"], {
                "action": action, "reason": "test decision",
            })
            self.assertEqual(result["state"], expected)
            self.assertIsNone(self.store.claim(comment["id"]))
        failed = self.new_comment("failed")
        self.store.claim(failed["id"])
        self.store.finish(failed["id"], "FAILED", {"error": "model unavailable"})
        self.assertIsNone(self.store.claim(failed["id"]))
        with self.assertRaisesRegex(AutoreplyStateError, "COMMENT_NOT_ATTEMPTED"):
            self.store.finish(failed["id"], "VERIFIED", {"visible": True})

    def test_secrets_and_dynamic_execution_are_rejected(self):
        for config in (
            {"api_key": "secret"}, {"style": {"cookie": "secret"}},
            {"url": "https://example.test"}, {"javascript": "alert(1)"},
            {"style": "visit https://example.test"},
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.store.upsert_job("account", "publish-task", "video", config=config)
        with self.assertRaises(ValueError):
            self.store.ingest(self.job_id, [self.observed(raw={"token": "secret"})])
        prepared = self.prepared()
        with self.assertRaises(ValueError):
            self.store.finish(prepared["id"], "FAILED", {"authorization": "secret"})

    def test_run_schedule_status_history_and_validation(self):
        result = self.store.record_run(self.job_id, error="temporary network failure")
        self.assertEqual(datetime.fromisoformat(result["next_run"]),
                         self.now + timedelta(seconds=300))
        self.assertEqual(result["last_error"], "temporary network failure")
        self.assertEqual(self.store.event_history(self.job_id)[0]["kind"], "RUN_FINISHED")
        self.assertEqual(self.store.list_jobs(enabled_only=True), [])
        self.enabled()
        self.assertEqual(len(self.store.list_jobs(enabled_only=True)), 1)
        for bad in (-1, 0, 1.2, True):
            with self.assertRaises(ValueError):
                self.store.upsert_job("account", "publish-task", "video", daily_limit=bad)
        with self.assertRaises(ValueError):
            self.store.set_enabled(self.job_id, 1)
        with self.assertRaises(KeyError):
            self.store.get_comment("missing")

    def test_explicit_none_budgets_keep_daily_accounting_without_limits(self):
        configured = self.store.upsert_job(
            "account", "publish-task", "video", daily_limit=None,
            max_model_calls_day=None, min_reply_interval=0,
        )
        self.assertIsNone(configured["daily_limit"])
        self.assertIsNone(configured["max_model_calls_day"])
        first = self.prepared("first")
        self.store.mark_attempted(first["id"])
        self.now = datetime(2026, 10, 3, 15, 59, 59, tzinfo=timezone.utc)
        # Set a real accumulated counter in the temporary fixture, not a
        # pretend unlimited policy cap. None must bypass limit comparisons.
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("""
                UPDATE autoreply_usage SET model_calls = 6000, reply_attempts = 2000
                WHERE job_id = ? AND day = '2026-10-03'
            """, (self.job_id,))
            connection.commit()
        second = self.prepared("second")
        self.store.mark_attempted(second["id"])
        self.assertEqual(self.store.status(self.job_id)["usage"], {
            "day": "2026-10-03", "model_calls": 6001, "reply_attempts": 2001,
        })
        self.now += timedelta(seconds=1)
        third = self.prepared("third")
        self.store.mark_attempted(third["id"])
        self.assertEqual(self.store.status(self.job_id)["usage"], {
            "day": "2026-10-04", "model_calls": 1, "reply_attempts": 1,
        })

    def test_legacy_not_null_database_accepts_unlimited_without_losing_rows(self):
        # The old database schema uses NOT NULL integer budget columns. Seed a
        # normal old job and observation, reopen it, then configure None.
        self.store.upsert_job("account", "publish-task", "video", daily_limit=2,
                              max_model_calls_day=3)
        self.enabled()
        original = self.store.ingest(self.job_id, [self.observed()])["comments"][0]
        reopened = CommentAutoreplyStore(self.path, clock=lambda: self.now)
        self.assertEqual(reopened.get_job(self.job_id)["daily_limit"], 2)
        self.assertEqual(reopened.get_job(self.job_id)["max_model_calls_day"], 3)
        updated = reopened.upsert_job("account", "publish-task", "video",
                                      daily_limit=None, max_model_calls_day=None)
        self.assertTrue(updated["enabled"])
        self.assertTrue(updated["baseline_complete"])
        self.assertEqual(reopened.get_comment(original["id"])["state"], "BASELINED")
        with closing(sqlite3.connect(self.path)) as connection:
            columns = {row[1]: row for row in connection.execute("PRAGMA table_info(autoreply_jobs)")}
            self.assertEqual(columns["daily_limit"][3], 1)
            self.assertEqual(columns["max_model_calls_day"][3], 1)
            stored = connection.execute("""
                SELECT daily_limit, max_model_calls_day FROM autoreply_jobs WHERE id = ?
            """, (self.job_id,)).fetchone()
            self.assertEqual(stored, (0, 0))
        again = CommentAutoreplyStore(self.path, clock=lambda: self.now)
        self.assertIsNone(again.get_job(self.job_id)["daily_limit"])
        self.assertIsNone(again.status(self.job_id)["job"]["max_model_calls_day"])
        # Explicitly restoring numeric limits works on the same database.
        restored = again.upsert_job("account", "publish-task", "video",
                                    daily_limit=2, max_model_calls_day=3)
        self.assertEqual((restored["daily_limit"], restored["max_model_calls_day"]), (2, 3))

    def test_zero_is_not_public_unlimited_syntax(self):
        for field in ("daily_limit", "max_model_calls_day"):
            for value in (0, -1, True, 1.5, "None", []):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.store.upsert_job("account", "publish-task", "video", **{field: value})
            self.assertIsNone(self.store.upsert_job(
                "account", "publish-task", "video", **{field: None},
            )[field])


if __name__ == "__main__":
    unittest.main()
