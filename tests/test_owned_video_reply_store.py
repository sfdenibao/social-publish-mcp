from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
import unittest

from utils.owned_video_reply_store import OwnedVideoReplyStateError, OwnedVideoReplyStore


class OwnedVideoReplyStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "owned-video-replies.sqlite3"
        self.now = datetime(2026, 10, 3, 7, tzinfo=timezone.utc)
        self.store = OwnedVideoReplyStore(self.path, clock=lambda: self.now)

    @staticmethod
    def comment(comment_id="first", **changes):
        return {"comment_id": comment_id, "author": "测试访客", "text": "表格能帮忙整理吗？",
                "profile_path": "/user/test-person", "target_ids": {"root_id": comment_id},
                "raw": {"index": 0}, **changes}

    def run_for(self, *, comments=None, account="account", video="video"):
        return self.store.create_run(account, video, 4, "第四条视频",
                                     comments if comments is not None else [self.comment()])

    def prepare(self, run_id, comment_id="first"):
        return self.store.prepare(run_id, comment_id, "现在整理表格最花时间的是哪一步？",
                                  {"action": "reply", "reason": "了解具体情况"},
                                  {"is_own_video": True, "is_top_level": True, "work_type": "video"})

    def test_run_freezes_target_snapshot_and_keeps_order(self):
        comments = [self.comment("second"), self.comment("first")]
        run = self.run_for(comments=comments)
        comments[0]["raw"]["index"] = 99
        comments[0]["author"] = "被后来修改的作者"
        reopened = self.store.get_run(run["id"])
        self.assertEqual(reopened["work_position"], 4)
        self.assertEqual(reopened["title"], "第四条视频")
        self.assertEqual([item["comment_id"] for item in reopened["comments"]], ["second", "first"])
        self.assertEqual(reopened["comments"][0]["raw"]["index"], 0)
        self.assertEqual(reopened["comments"][0]["author"], "测试访客")
        self.assertEqual([item["state"] for item in reopened["comments"]], ["NEW", "NEW"])

    def test_new_run_may_have_new_dom_index_without_changing_original_snapshot(self):
        first = self.run_for()
        second = self.run_for(comments=[self.comment(raw={"index": 8})])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.store.get_item(first["id"], "first")["raw"]["index"], 0)
        self.assertEqual(self.store.get_item(second["id"], "first")["raw"]["index"], 8)

    def test_target_identity_changes_for_same_key_are_rejected(self):
        self.run_for()
        for change in ({"author": "其他人"}, {"text": "其他内容"},
                       {"profile_path": "/user/other"}, {"target_ids": {"root_id": "other"}}):
            with self.subTest(change=change), self.assertRaisesRegex(
                OwnedVideoReplyStateError, "COMMENT_IDENTITY_CONFLICT"
            ):
                self.run_for(comments=[self.comment(**change)])

    def test_run_membership_cannot_be_bypassed_by_an_existing_other_comment(self):
        first = self.run_for()
        self.run_for(comments=[self.comment("other")])
        with self.assertRaises(KeyError):
            self.store.prepare(first["id"], "other", "不能发送到未冻结的目标")
        with self.assertRaises(KeyError):
            self.store.mark_attempted(first["id"], "other")
        with self.assertRaises(KeyError):
            self.store.finish(first["id"], "other", "SKIP", {})

    def test_prepare_is_idempotent_and_draft_change_cannot_race_other_run(self):
        first = self.run_for()
        draft = self.prepare(first["id"])
        second = self.run_for()
        reused = self.prepare(second["id"])
        self.assertEqual(reused["reply"], draft["reply"])
        self.assertEqual(reused["prepared_run_id"], first["id"])
        with self.assertRaisesRegex(OwnedVideoReplyStateError, "PREPARED_PAYLOAD_CONFLICT"):
            self.store.prepare(second["id"], "first", "换个文字试试")

    def test_attempt_survives_restart_and_new_run_cannot_resend_or_change_reply(self):
        first = self.run_for()
        self.prepare(first["id"])
        attempted = self.store.mark_attempted(first["id"], "first")
        self.assertEqual(attempted["state"], "UNKNOWN_RESULT")
        self.assertEqual(attempted["attempted_run_id"], first["id"])
        reopened = OwnedVideoReplyStore(self.path, clock=lambda: self.now)
        second = reopened.create_run("account", "video", 4, "同一视频", [self.comment()])
        self.assertEqual(second["comments"][0]["state"], "UNKNOWN_RESULT")
        for action in (
            lambda: reopened.prepare(second["id"], "first", "不同的回复"),
            lambda: reopened.mark_attempted(second["id"], "first"),
            lambda: reopened.skip(second["id"], "first", "不能跳过发送记录"),
            lambda: reopened.finish(second["id"], "first", "HANDOFF", {}),
        ):
            with self.assertRaises(OwnedVideoReplyStateError):
                action()
        verified = reopened.finish(second["id"], "first", "VERIFIED", {"reply_visible": True})
        self.assertEqual(verified["attempted_at"], attempted["attempted_at"])
        self.assertEqual(reopened.get_item(first["id"], "first")["state"], "VERIFIED")
        with self.assertRaises(OwnedVideoReplyStateError):
            reopened.finish(first["id"], "first", "UNKNOWN_RESULT", {})

    def test_concurrent_attempts_across_runs_have_exactly_one_winner(self):
        runs = [self.run_for() for _ in range(8)]
        self.prepare(runs[0]["id"])
        barrier = threading.Barrier(8)

        def attempt(run):
            other = OwnedVideoReplyStore(self.path, clock=lambda: self.now)
            barrier.wait(timeout=10)
            try:
                return other.mark_attempted(run["id"], "first")
            except OwnedVideoReplyStateError:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, runs))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertTrue(all(self.store.get_item(run["id"], "first")["state"] == "UNKNOWN_RESULT" for run in runs))

    def test_account_and_video_are_separate_guards(self):
        runs = [self.run_for(), self.run_for(account="other"), self.run_for(video="other")]
        for run in runs:
            self.prepare(run["id"])
            self.store.mark_attempted(run["id"], "first")
        self.assertEqual(len({self.store.get_item(run["id"], "first")["attempted_run_id"] for run in runs}), 3)

    def test_skip_handoff_and_verified_cannot_be_reopened(self):
        run = self.run_for(comments=[self.comment("skip"), self.comment("handoff"), self.comment("verified")])
        self.store.skip(run["id"], "skip", "已有作者回复")
        self.store.finish(run["id"], "handoff", "HANDOFF", {"error": "AMBIGUOUS_TARGET"})
        self.prepare(run["id"], "verified")
        self.store.mark_attempted(run["id"], "verified")
        self.store.finish(run["id"], "verified", "VERIFIED", {})
        for comment_id in ("skip", "handoff", "verified"):
            with self.assertRaises(OwnedVideoReplyStateError):
                self.prepare(run["id"], comment_id)
        self.assertEqual(self.store.get_item(run["id"], "skip")["evidence"]["reason"], "已有作者回复")

    def test_cannot_verify_without_attempt_and_no_global_enabled_dependency(self):
        run = self.run_for()
        with self.assertRaises(OwnedVideoReplyStateError):
            self.store.mark_attempted(run["id"], "first")
        with self.assertRaisesRegex(OwnedVideoReplyStateError, "COMMENT_NOT_ATTEMPTED"):
            self.store.finish(run["id"], "first", "VERIFIED", {})
        self.prepare(run["id"])
        result = self.store.mark_attempted(run["id"], "first")
        self.assertIsNotNone(result["attempted_at"])
        self.assertEqual([path.name for path in self.path.parent.iterdir()], ["owned-video-replies.sqlite3"])

    def test_credential_fields_and_invalid_frozen_input_are_rejected(self):
        with self.assertRaises(ValueError):
            self.run_for(comments=[self.comment(raw={"cookie": "secret"})])
        with self.assertRaises(ValueError):
            self.run_for(comments=[self.comment(), self.comment()])
        with self.assertRaises(ValueError):
            self.store.create_run("account", "video", True, "title", [])
        run = self.run_for()
        with self.assertRaises(ValueError):
            self.store.prepare(run["id"], "first", "回复", context={"api_key": "secret"})
        with self.assertRaises(ValueError):
            self.store.finish(run["id"], "first", "HANDOFF", {"error": "Bearer secret-value"})
        with self.assertRaises(ValueError):
            self.store.prepare(run["id"], "first", "回复", decision={"action": "handoff"})

    def test_aliases_are_supported_without_allowing_conflicting_identity(self):
        original = self.comment()
        original["comment_key"] = original.pop("comment_id")
        original["source_profile_path"] = original.pop("profile_path")
        run = self.run_for(comments=[original])
        self.assertEqual(run["comments"][0]["comment_id"], "first")
        self.assertEqual(run["comments"][0]["profile_path"], "/user/test-person")
        with self.assertRaises(ValueError):
            self.run_for(comments=[self.comment(comment_key="conflicting")])


if __name__ == "__main__":
    unittest.main()
