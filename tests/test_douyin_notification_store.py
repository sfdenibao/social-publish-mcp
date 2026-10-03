from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from utils.douyin_notification_store import NotificationStateError, NotificationStore


class NotificationStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "notifications.sqlite3"
        self.now = datetime(2026, 10, 3, 7, tzinfo=timezone.utc)
        self.store = NotificationStore(self.path, clock=lambda: self.now)
        self.account = "test-account"
        self.store.configure(self.account)

    @staticmethod
    def notice(key="one", **changes):
        value = {
            "notification_key": key, "author": "测试访客", "text": "整理表格可以帮忙吗？",
            "kind": "comment", "display_time": "昨天", "source_profile_path": "/user/public-person",
            "cover_path": "/cover/public-work", "raw": {"click_index": 0, "visible": True},
        }
        value.update(changes)
        return value

    def prepare(self, key="one"):
        self.store.ingest(self.account, [self.notice(key)])
        return self.store.prepare(self.account, key, "先说说现在怎么整理表格？",
                                  {"action": "reply", "reason": "需求澄清"},
                                  {"post_title": "用AI帮忙解决具体的小问题"})

    def test_initial_visible_only_comments_are_new_replies_and_others_skipped(self):
        result = self.store.ingest(self.account, [
            self.notice("comment"), self.notice("reply", kind="reply"),
            self.notice("likes", kind="likes", text=""),
            self.notice("follow", kind="follow", text="关注了你"),
            self.notice("private", kind="message", text="not a public interaction"),
        ])
        self.assertEqual(result["counts"], {"NEW": 1, "SKIP": 4, "existing": 0})
        self.assertEqual({n["notification_key"] for n in self.store.pending(self.account)},
                         {"comment"})
        self.assertEqual(self.store.status(self.account)["counts"]["SKIP"], 4)

    def test_duplicate_ingest_updates_index_and_relative_time_not_identity(self):
        original = self.store.ingest(self.account, [self.notice()])["notices"][0]
        self.now += timedelta(days=1)
        result = self.store.ingest(self.account, [self.notice(
            display_time="前天", raw={"click_index": 7, "visible": True},
        )])
        current = result["notices"][0]
        self.assertEqual(result["counts"]["existing"], 1)
        self.assertEqual(current["raw"]["click_index"], 7)
        self.assertEqual(current["display_time"], "前天")
        self.assertEqual(current["first_seen"], original["first_seen"])
        self.assertNotEqual(current["last_seen"], original["last_seen"])
        self.assertEqual(len(self.store.pending(self.account)), 1)

    def test_immutable_identity_conflicts_rollback_entire_scan(self):
        self.store.ingest(self.account, [self.notice()])
        for field, value in (("author", "其他访客"), ("text", "不同内容"), ("kind", "reply"),
                             ("source_profile_path", "/user/other"), ("cover_path", "/cover/other")):
            with self.subTest(field=field), self.assertRaisesRegex(
                NotificationStateError, "NOTIFICATION_IDENTITY_CONFLICT"
            ):
                self.store.ingest(self.account, [self.notice("new"), self.notice(**{field: value})])
            self.assertEqual(len(self.store.pending(self.account)), 1)

    def test_account_isolation_and_settings_persist_across_restart(self):
        self.store.configure("other-account")
        self.store.ingest(self.account, [self.notice()])
        self.store.ingest("other-account", [self.notice(author="另一个账号的访客")])
        self.store.set_enabled(self.account, True)
        reopened = NotificationStore(self.path, clock=lambda: self.now)
        self.assertTrue(reopened.status(self.account)["settings"]["enabled"])
        self.assertFalse(reopened.status("other-account")["settings"]["enabled"])
        self.assertEqual(reopened.get(self.account, "one")["author"], "测试访客")
        self.assertEqual(reopened.get("other-account", "one")["author"], "另一个账号的访客")
        changed = reopened.configure(self.account, interval_seconds=600)
        self.assertTrue(changed["enabled"])
        self.assertEqual(changed["interval_seconds"], 600)

    def test_prepare_is_idempotent_for_same_draft_conflicts_on_changes(self):
        draft = self.prepare()
        repeated = self.store.prepare(self.account, "one", draft["reply"],
                                      draft["decision"], draft["context"])
        self.assertEqual(draft, repeated)
        with self.assertRaisesRegex(NotificationStateError, "PREPARED_PAYLOAD_CONFLICT"):
            self.store.prepare(self.account, "one", "另一个回复")
        self.assertEqual(self.store.pending(self.account), [])
        self.assertEqual(len(self.store.prepared(self.account)), 1)

    def test_pause_blocks_attempt_without_losing_prepared_draft(self):
        self.prepare()
        with self.assertRaisesRegex(NotificationStateError, "NOTIFICATIONS_PAUSED"):
            self.store.mark_attempted(self.account, "one")
        self.assertIsNone(self.store.get(self.account, "one")["attempted_at"])
        self.store.set_enabled(self.account, True)
        self.store.set_enabled(self.account, False)
        with self.assertRaises(NotificationStateError):
            self.store.mark_attempted(self.account, "one")
        self.assertEqual(self.store.get(self.account, "one")["state"], "PREPARED")

    def test_unknown_survives_restart_and_cannot_be_reprepared_or_resent(self):
        self.prepare()
        self.store.set_enabled(self.account, True)
        attempted = self.store.mark_attempted(self.account, "one")
        self.assertEqual(attempted["state"], "UNKNOWN_RESULT")
        reopened = NotificationStore(self.path, clock=lambda: self.now)
        for action in (
            lambda: reopened.mark_attempted(self.account, "one"),
            lambda: reopened.prepare(self.account, "one", "different reply"),
            lambda: reopened.finish(self.account, "one", "HANDOFF", {}),
            lambda: reopened.finish(self.account, "one", "SKIP", {}),
        ):
            with self.assertRaises(NotificationStateError):
                action()
        self.assertEqual(len(reopened.unknown(self.account)), 1)
        self.assertEqual(reopened.prepared(self.account), [])
        result = reopened.finish(self.account, "one", "VERIFIED", {"reply_visible": True})
        self.assertEqual(result["attempted_at"], attempted["attempted_at"])
        self.assertEqual(reopened.unknown(self.account), [])

    def test_concurrent_submission_guard_has_exactly_one_winner(self):
        self.prepare()
        self.store.set_enabled(self.account, True)
        barrier = threading.Barrier(8)

        def attempt(_):
            other = NotificationStore(self.path, clock=lambda: self.now)
            barrier.wait(timeout=10)
            try:
                return other.mark_attempted(self.account, "one")
            except NotificationStateError:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(8)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.store.get(self.account, "one")["state"], "UNKNOWN_RESULT")

    def test_terminal_states_cannot_be_downgraded_or_sent(self):
        self.store.ingest(self.account, [self.notice("skip", kind="follow"), self.notice("handoff")])
        self.store.finish(self.account, "handoff", "HANDOFF", {"reason": "needs_owner"})
        self.prepare("verified")
        self.store.set_enabled(self.account, True)
        self.store.mark_attempted(self.account, "verified")
        self.store.finish(self.account, "verified", "VERIFIED", {})
        for key in ("skip", "handoff", "verified"):
            with self.subTest(key=key), self.assertRaises(NotificationStateError):
                self.store.prepare(self.account, key, "不能发这条")
            with self.subTest(key=key), self.assertRaises(NotificationStateError):
                self.store.finish(self.account, key, "UNKNOWN_RESULT", {})
        self.store.finish(self.account, "verified", "VERIFIED", {"checked_again": True})

    def test_cannot_claim_verified_or_unknown_without_attempt(self):
        self.prepare()
        for state in ("VERIFIED", "UNKNOWN_RESULT"):
            with self.assertRaisesRegex(NotificationStateError, "NOTIFICATION_NOT_ATTEMPTED"):
                self.store.finish(self.account, "one", state, {})
        with self.assertRaises(ValueError):
            self.store.finish(self.account, "one", "NEW", {})

    def test_pending_has_no_implicit_quantity_cap(self):
        self.store.ingest(self.account, [self.notice(str(index)) for index in range(125)])
        self.assertEqual(len(self.store.pending(self.account)), 125)
        self.assertEqual(len(self.store.history(self.account, limit=120)), 120)

    def test_safe_public_data_and_secret_rejection(self):
        notice = self.notice(text="表格怎么做？\n谢谢❤️‍🔥")
        self.store.ingest(self.account, [notice])
        self.assertEqual(self.store.get(self.account, "one")["text"], notice["text"])
        for raw in ({"cookie": "x"}, {"headers": {"Authorization": "x"}},
                    {"url": "https://example.test/?access_token=secret"}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.store.ingest(self.account, [self.notice("unsafe", raw=raw)])
        with self.assertRaises(ValueError):
            self.store.prepare(self.account, "one", "正常回复", context={"api_key": "secret"})
        with self.assertRaises(ValueError):
            self.store.finish(self.account, "one", "HANDOFF", {"error": "Bearer secret-value"})
        with self.assertRaises(ValueError):
            self.store.prepare(self.account, "one", "正常回复", decision={"action": "handoff"})

    def test_kind_and_profile_aliases_have_stable_normalized_identity(self):
        first = self.notice(kind="like")
        first["sourceprofile_path"] = first.pop("source_profile_path")
        self.store.ingest(self.account, [first])
        second = self.store.ingest(self.account, [self.notice(kind="likes")])
        self.assertEqual(second["counts"]["existing"], 1)
        self.assertEqual(second["notices"][0]["kind"], "likes")

    def test_schedule_is_durable_and_inputs_are_checked(self):
        result = self.store.record_run(self.account, error="PROFILE_BUSY")
        self.assertEqual(datetime.fromisoformat(result["next_run"]), self.now + timedelta(seconds=300))
        self.assertEqual(result["last_error"], "PROFILE_BUSY")
        future = self.now + timedelta(seconds=900)
        result = self.store.record_run(self.account, next_run=future.isoformat())
        self.assertEqual(datetime.fromisoformat(result["next_run"]), future)
        self.assertIsNone(result["last_error"])
        for value in (0, -1, True, 0.5):
            with self.assertRaises(ValueError):
                self.store.configure(self.account, interval_seconds=value)
        with self.assertRaises(ValueError):
            self.store.set_enabled(self.account, 1)
        with self.assertRaises(ValueError):
            self.store.history(self.account, state="INVALID")
        with self.assertRaises(KeyError):
            self.store.get("other-account", "one")

    def test_list_accounts_preserves_enabled_filter_and_restart(self):
        self.store.configure("other-account")
        self.store.set_enabled(self.account, True)
        reopened = NotificationStore(self.path, clock=lambda: self.now)
        self.assertEqual(len(reopened.list_accounts()), 2)
        self.assertEqual([row["account_id"] for row in reopened.list_accounts(enabled_only=True)],
                         [self.account])
        with self.assertRaises(ValueError):
            reopened.list_accounts(enabled_only=1)

    def test_old_reply_states_are_excluded_without_downgrading_unknown(self):
        self.store.set_enabled(self.account, True)
        self.store.ingest(self.account, [self.notice("new")])
        draft = self.prepare("prepared")
        self.prepare("unknown")
        self.store.mark_attempted(self.account, "unknown")
        # This temporary fixture represents rows created by the earlier policy.
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE douyin_notifications SET kind = 'reply'")
            connection.commit()
        with self.assertRaisesRegex(NotificationStateError, "NOTIFICATION_SCOPE_EXCLUDED"):
            self.store.prepare(self.account, "new", "不要发这个")
        with self.assertRaisesRegex(NotificationStateError, "NOTIFICATION_SCOPE_EXCLUDED"):
            self.store.mark_attempted(self.account, "prepared")
        self.store.ingest(self.account, [self.notice(key, kind="reply")
                                         for key in ("new", "prepared", "unknown")])
        self.assertEqual(self.store.get(self.account, "new")["state"], "SKIP")
        self.assertEqual(self.store.get(self.account, "prepared")["state"], "SKIP")
        self.assertEqual(self.store.get(self.account, "prepared")["decision"], draft["decision"])
        self.assertEqual(self.store.get(self.account, "unknown")["state"], "UNKNOWN_RESULT")
        self.assertEqual(self.store.pending(self.account), [])


if __name__ == "__main__":
    unittest.main()
