import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from utils.owned_video_reply_runner import OwnedVideoReplyRunError, OwnedVideoReplyRunner
from utils.owned_video_reply_store import OwnedVideoReplyStore


def comment(cid="one", **changes):
    return {"comment_id": cid, "author": "测试访客", "text": "表格整理能帮忙吗？",
            "profile_path": "/user/test-person", "is_author": False, **changes}


class FakeAdapter:
    def __init__(self):
        self.comments = [comment(), comment("two")]
        self.complete = True
        self.overrides = {}
        self.context_calls = []
        self.reply_calls = []
        self.sent = []
        self.rechecks = []
        self.reply_error = None
        self.after_send_error = None
        self.reply_status = "VERIFIED"
        self.recheck_status = "VERIFIED"
        self.call_guard = True
        self.started = None
        self.release = None

    async def read_video(self, account, video):
        if self.started is not None:
            self.started.set()
            await self.release.wait()
        return {"account_id": account, "video_id": video, "complete": self.complete,
                "card": {"position": 4, "title": "冻结的第四条视频", "video_id": video, "work_type": "video"},
                "comments": list(self.comments), "screenshot_path": "test/video.png"}

    async def read_context(self, account, notice):
        self.context_calls.append(notice)
        cid = notice["raw"]["comment_id"]
        return {"comment": notice["text"], "video_id": notice["raw"]["video_id"],
                "comment_id": cid, "root_id": cid, "is_own_video": True, "is_top_level": True,
                "work_type": "video", "post_title": "实际视频标题", "post_description": "实际作品说明",
                "parent_comment": "", "already_replied": False, "is_author": False,
                **self.overrides.get(cid, {})}

    async def reply(self, account, notice, text, *, before_submit):
        self.reply_calls.append(notice)
        if self.reply_error:
            raise self.reply_error
        if self.reply_status not in {"SKIP", "ALREADY_PRESENT"} and self.call_guard:
            before_submit()
            self.sent.append((notice["raw"]["comment_id"], text))
        if self.after_send_error:
            raise self.after_send_error
        return {"status": self.reply_status, "screenshot_path": "test/reply.png"}

    async def recheck(self, account, notice, text):
        self.rechecks.append(notice)
        return {"status": self.recheck_status, "screenshot_path": "test/recheck.png"}


class FakeModel:
    def __init__(self):
        self.calls = []
        self.ready = True
        self.result = {"action": "reply", "reply": "最想先解决表格里的哪一步？", "reason": "了解实际需求",
                       "provider": "test", "model": "fake", "usage": {"total_tokens": 10}}

    def status(self):
        return {"ready": self.ready, "status": "READY" if self.ready else "MISSING_API_KEY"}

    def generate(self, text, context):
        self.calls.append((text, context))
        return dict(self.result)


class OwnedVideoReplyRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "owned.sqlite3"
        self.store = OwnedVideoReplyStore(self.path)
        self.adapter, self.model = FakeAdapter(), FakeModel()
        self.runner = OwnedVideoReplyRunner(self.store, self.adapter, self.model)
        self.account, self.video = "account", "7683842660402072859"

    async def draft(self):
        return await self.runner.prepare(self.account, self.video)

    async def test_prepare_freezes_video_and_drafts_without_sending(self):
        prepared = await self.draft()
        self.assertEqual(prepared["status"], "PREPARED")
        self.assertEqual(prepared["work_position"], 4)
        self.assertEqual([item["state"] for item in prepared["comments"]], ["PREPARED", "PREPARED"])
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(len(self.model.calls), 2)
        self.assertTrue(all(call[1]["post_title"] == "实际视频标题" for call in self.model.calls))
        mapped = self.adapter.context_calls[0]
        self.assertEqual(mapped["notification_key"], f"owned:{self.video}:one")
        self.assertEqual(mapped["source_profile_path"], "/user/test-person")
        self.assertEqual(mapped["raw"], {"video_id": self.video, "comment_id": "one"})
        self.assertEqual(self.runner.get_run(prepared["id"])["comments"][0]["reply"], self.model.result["reply"])

    async def test_incomplete_scan_creates_no_run(self):
        self.adapter.complete = False
        with self.assertRaisesRegex(OwnedVideoReplyRunError, "INCOMPLETE_VIDEO_COMMENT_SCAN"):
            await self.draft()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM owned_video_runs").fetchone()[0], 0)
        self.assertEqual(self.model.calls, [])
        self.assertFalse(self.runner.status()["busy"])

    async def test_submit_wrong_video_does_not_touch_dom_or_send(self):
        prepared = await self.draft()
        with self.assertRaisesRegex(OwnedVideoReplyRunError, "EXPECTED_VIDEO_ID_MISMATCH"):
            await self.runner.submit(prepared["id"], "another-video")
        self.assertEqual(self.adapter.reply_calls, [])
        self.assertEqual(self.adapter.rechecks, [])

    async def test_submit_sends_each_frozen_comment_once_and_no_model_calls(self):
        prepared = await self.draft()
        submitted = await self.runner.submit(prepared["id"], self.video)
        self.assertEqual(submitted["status"], "COMPLETED")
        self.assertEqual([item["state"] for item in submitted["comments"]], ["VERIFIED", "VERIFIED"])
        self.assertEqual([item[0] for item in self.adapter.sent], ["one", "two"])
        await self.runner.submit(prepared["id"], self.video)
        self.assertEqual(len(self.adapter.sent), 2)
        self.assertEqual(len(self.model.calls), 2)

    async def test_unknown_is_only_rechecked_even_from_a_duplicate_run(self):
        prepared = await self.draft()
        self.adapter.reply_status = "UNKNOWN_RESULT"
        await self.runner.submit(prepared["id"], self.video)
        self.assertEqual(len(self.adapter.sent), 2)
        duplicate = await self.draft()
        self.assertEqual([item["state"] for item in duplicate["comments"]], ["UNKNOWN_RESULT", "UNKNOWN_RESULT"])
        result = await self.runner.submit(duplicate["id"], self.video)
        self.assertTrue(all(item["recheck_only"] for item in result["results"]))
        self.assertEqual(len(self.adapter.rechecks), 2)
        self.assertEqual(len(self.adapter.sent), 2)
        self.assertEqual(len(self.model.calls), 2)

    async def test_unattempted_error_preserves_prepared_for_explicit_retry(self):
        prepared = await self.draft()
        self.adapter.reply_error = RuntimeError("PROFILE_BUSY")
        result = await self.runner.submit(prepared["id"], self.video)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual([item["state"] for item in result["comments"]], ["PREPARED", "PREPARED"])
        self.assertEqual(self.adapter.sent, [])
        self.adapter.reply_error = None
        await self.runner.submit(prepared["id"], self.video)
        self.assertEqual(len(self.adapter.sent), 2)
        self.assertEqual(len(self.model.calls), 2)

    async def test_error_after_guard_is_unknown_and_never_resends(self):
        prepared = await self.draft()
        self.adapter.after_send_error = RuntimeError("Authorization: Bearer secret-do-not-save")
        result = await self.runner.submit(prepared["id"], self.video)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual([item["state"] for item in result["comments"]], ["UNKNOWN_RESULT", "UNKNOWN_RESULT"])
        self.assertNotIn("secret-do-not-save", str(result))
        self.adapter.after_send_error = None
        await self.runner.submit(prepared["id"], self.video)
        self.assertEqual(len(self.adapter.sent), 2)
        self.assertEqual(len(self.adapter.rechecks), 2)

    async def test_scope_and_actual_identity_fail_closed_before_model(self):
        self.adapter.comments = [comment("other"), comment("nested"), comment("missing"), comment("wrong-id")]
        self.adapter.overrides = {"other": {"is_own_video": False}, "nested": {"is_top_level": False},
                                  "missing": {"work_type": None}, "wrong-id": {"video_id": "different"}}
        result = await self.draft()
        self.assertEqual([item["state"] for item in result["comments"]], ["SKIP", "SKIP", "HANDOFF", "HANDOFF"])
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.adapter.sent, [])

    async def test_self_already_replied_skip_and_model_handoff(self):
        self.adapter.comments = [comment("own", is_author=True), comment("answered"), comment("handoff")]
        self.adapter.overrides["answered"] = {"already_replied": True}
        self.model.result.update(action="handoff", reply="", reason="需要本人判断")
        result = await self.draft()
        self.assertEqual([item["state"] for item in result["comments"]], ["SKIP", "SKIP", "HANDOFF"])
        self.assertEqual(len(self.model.calls), 1)
        await self.runner.submit(result["id"], self.video)
        self.assertEqual(self.adapter.reply_calls, [])

    async def test_model_unavailable_leaves_new_for_repaired_prepare(self):
        self.model.ready = False
        first = await self.draft()
        self.assertEqual(first["status"], "PARTIAL")
        self.assertEqual([item["state"] for item in first["comments"]], ["NEW", "NEW"])
        self.model.ready = True
        second = await self.draft()
        self.assertEqual(second["status"], "PREPARED")
        self.assertEqual(len(self.model.calls), 2)

    async def test_adapter_already_present_skips_without_attempt(self):
        prepared = await self.draft()
        self.adapter.reply_status = "ALREADY_PRESENT"
        result = await self.runner.submit(prepared["id"], self.video)
        self.assertEqual([item["state"] for item in result["comments"]], ["SKIP", "SKIP"])
        self.assertTrue(all(item["attempted_at"] is None for item in result["comments"]))
        self.assertEqual(self.adapter.sent, [])

    async def test_success_claim_without_guard_is_not_recorded_as_sent(self):
        prepared = await self.draft()
        self.adapter.call_guard = False
        result = await self.runner.submit(prepared["id"], self.video)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual([item["state"] for item in result["comments"]], ["PREPARED", "PREPARED"])
        self.assertEqual(self.adapter.sent, [])

    async def test_prepare_and_submit_share_a_nonblocking_lock(self):
        self.adapter.started, self.adapter.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.draft())
        await self.adapter.started.wait()
        self.assertTrue(self.runner.status()["busy"])
        with self.assertRaisesRegex(OwnedVideoReplyRunError, "OWNED_REPLY_RUNNER_BUSY"):
            await self.runner.submit("not-yet-created", self.video)
        with self.assertRaisesRegex(OwnedVideoReplyRunError, "OWNED_REPLY_RUNNER_BUSY"):
            await self.draft()
        self.adapter.release.set()
        await task
        self.assertFalse(self.runner.status()["busy"])


if __name__ == "__main__":
    unittest.main()
