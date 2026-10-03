from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from utils.comment_ledger import (
    CommentLedger, CommentStateError, IdempotencyConflictError,
)


class CommentLedgerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "comments.sqlite3"
        self.ledger = CommentLedger(self.path)
        self.payload = ("test-account", "test-work", "回复来自codex")

    def prepare(self, key="test-key"):
        return self.ledger.prepare(*self.payload, key)

    def test_new_task_contains_public_contract(self):
        task = self.prepare()
        self.assertEqual(set(task), {
            "id", "account_id", "work_id", "text", "state", "created_at",
            "attempted_at", "evidence", "error", "reused",
        })
        self.assertEqual(task["state"], "PREPARED")
        self.assertIsNone(task["attempted_at"])
        self.assertEqual(task["evidence"], {})
        self.assertFalse(task["reused"])

    def test_repeated_key_reuses_task_and_changed_payload_conflicts(self):
        first = self.prepare()
        self.assertEqual(self.prepare()["id"], first["id"])
        self.assertTrue(self.prepare()["reused"])
        for payload in (("other", *self.payload[1:]),
                        (self.payload[0], "other", self.payload[2]),
                        (*self.payload[:2], "different")):
            with self.subTest(payload=payload):
                with self.assertRaises(IdempotencyConflictError):
                    self.ledger.prepare(*payload, "test-key")

    def test_different_key_same_payload_is_bound_alias_after_restart(self):
        first = self.prepare("first")
        second = self.prepare("second")
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["reused"])
        reopened = CommentLedger(self.path)
        with self.assertRaises(IdempotencyConflictError):
            reopened.prepare(*self.payload[:2], "different", "second")

    def test_different_account_or_work_creates_distinct_task(self):
        first = self.prepare()
        second = self.ledger.prepare("other", *self.payload[1:], "second")
        third = self.ledger.prepare(self.payload[0], "other", self.payload[2], "third")
        self.assertEqual(len({first["id"], second["id"], third["id"]}), 3)

    def test_parallel_prepares_with_distinct_keys_create_one_task(self):
        barrier = threading.Barrier(8)

        def prepare(index):
            ledger = CommentLedger(self.path)
            barrier.wait(timeout=10)
            return ledger.prepare(*self.payload, f"key-{index}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            tasks = list(pool.map(prepare, range(8)))
        self.assertEqual(len({task["id"] for task in tasks}), 1)
        self.assertEqual(sum(not task["reused"] for task in tasks), 1)
        for index in range(8):
            with self.assertRaises(IdempotencyConflictError):
                self.ledger.prepare(*self.payload[:2], "different", f"key-{index}")

    def test_parallel_same_key_different_payload_has_one_winner(self):
        barrier = threading.Barrier(6)

        def prepare(index):
            barrier.wait(timeout=10)
            try:
                return self.ledger.prepare(*self.payload[:2], f"text-{index}", "same")
            except IdempotencyConflictError:
                return None

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(prepare, range(6)))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_only_one_worker_can_mark_attempted(self):
        task = self.prepare()
        barrier = threading.Barrier(8)

        def claim(_):
            barrier.wait(timeout=10)
            try:
                return CommentLedger(self.path).mark_attempted(task["id"])
            except CommentStateError:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(claim, range(8)))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["state"], "UNKNOWN_RESULT")
        self.assertIsNotNone(winners[0]["attempted_at"])

    def test_crash_after_guard_blocks_click_after_restart_and_key_change(self):
        task = self.prepare()
        attempted = self.ledger.mark_attempted(task["id"])
        reopened = CommentLedger(self.path)
        reused = reopened.prepare(*self.payload, "brand-new-key")
        self.assertEqual(reused["id"], task["id"])
        self.assertEqual(reused["state"], "UNKNOWN_RESULT")
        self.assertEqual(reused["attempted_at"], attempted["attempted_at"])
        with self.assertRaises(CommentStateError):
            reopened.mark_attempted(reused["id"])

    def test_failure_before_attempt_can_retry_but_prepare_preserves_failure(self):
        task = self.prepare()
        failed = self.ledger.fail_before_attempt(task["id"], "target unavailable")
        self.assertEqual(failed["state"], "FAILED")
        self.assertIsNone(failed["attempted_at"])
        reopened = CommentLedger(self.path)
        same = reopened.prepare(*self.payload, "retry-key")
        self.assertEqual(same["state"], "FAILED")
        self.assertEqual(same["error"], "target unavailable")
        attempted = reopened.mark_attempted(same["id"])
        self.assertEqual(attempted["state"], "UNKNOWN_RESULT")
        self.assertIsNone(attempted["error"])

    def test_failed_or_blocked_result_after_attempt_never_allows_retry(self):
        for index, state in enumerate(("FAILED", "BLOCKED", "UNKNOWN_RESULT")):
            with self.subTest(state=state):
                task = self.ledger.prepare(*self.payload[:2], f"test-{index}", str(index))
                attempted = self.ledger.mark_attempted(task["id"])
                result = self.ledger.record(task["id"], state, {"error": "uncertain"})
                self.assertEqual(result["attempted_at"], attempted["attempted_at"])
                with self.assertRaises(CommentStateError):
                    self.ledger.mark_attempted(task["id"])
                with self.assertRaises(CommentStateError):
                    self.ledger.fail_before_attempt(task["id"], "pretend pre-click")

    def test_already_present_without_click_permanently_blocks_submission(self):
        task = self.prepare()
        evidence = {"matched_text": self.payload[2], "screenshot": "public.png"}
        existing = self.ledger.record(task["id"], "ALREADY_PRESENT", evidence)
        self.assertIsNone(existing["attempted_at"])
        reopened = CommentLedger(self.path)
        same = reopened.prepare(*self.payload, "fresh-key")
        self.assertEqual(same["state"], "ALREADY_PRESENT")
        self.assertEqual(same["evidence"], evidence)
        with self.assertRaises(CommentStateError):
            reopened.mark_attempted(task["id"])
        with self.assertRaises(CommentStateError):
            reopened.fail_before_attempt(task["id"], "retry")

    def test_verified_requires_attempt_and_terminal_result_cannot_be_downgraded(self):
        task = self.prepare()
        for state in ("VERIFIED", "UNKNOWN_RESULT", "BLOCKED", "FAILED"):
            with self.assertRaises(CommentStateError):
                self.ledger.record(task["id"], state, {})
        self.ledger.mark_attempted(task["id"])
        evidence = {"comment_text": self.payload[2], "dom_match": True}
        self.ledger.record(task["id"], "VERIFIED", evidence)
        self.assertEqual(self.prepare("new-key")["evidence"], evidence)
        self.assertEqual(self.ledger.record(task["id"], "VERIFIED", evidence)["state"],
                         "VERIFIED")
        for state in ("UNKNOWN_RESULT", "FAILED", "BLOCKED", "ALREADY_PRESENT"):
            with self.assertRaises(CommentStateError):
                self.ledger.record(task["id"], state, {})

    def test_late_pre_attempt_failure_cannot_erase_attempt(self):
        task = self.prepare()
        self.ledger.mark_attempted(task["id"])
        with self.assertRaises(CommentStateError):
            self.ledger.fail_before_attempt(task["id"], "late exception")
        self.assertIsNotNone(self.ledger.get(task["id"])["attempted_at"])

    def test_text_length_and_control_characters_are_validated(self):
        for invalid in ("", "x" * 201, "a\nb", "a\tb", "a\x00b", "a\x7fb",
                        "a\x85b", "a\u202eb", "a\ud800b", "a\u2028b", None):
            with self.subTest(invalid=repr(invalid)):
                with self.assertRaises(ValueError):
                    self.ledger.prepare(*self.payload[:2], invalid, "invalid")
        self.ledger.prepare(*self.payload[:2], "x", "min")
        self.ledger.prepare(*self.payload[:2], "字" * 200, "max")

    def test_evidence_is_json_object_and_rejects_credentials(self):
        task = self.prepare()
        self.ledger.mark_attempted(task["id"])
        invalid_evidence = [
            [], "text", None, {1: "value"}, {"value": object()},
            {"value": float("nan")}, {"value": float("inf")},
            {"cookie": "secret"}, {"data": [{"access_token": "secret"}]},
            {"Authorization": "redacted"}, {"note": "Bearer abc123"},
            {"url": "https://user:password@example.test/"},
            {"url": "https://example.test/?access_token=secret"},
        ]
        for invalid in invalid_evidence:
            with self.subTest(evidence=repr(invalid)):
                with self.assertRaises(ValueError):
                    self.ledger.record(task["id"], "VERIFIED", invalid)
        self.assertEqual(self.ledger.get(task["id"])["state"], "UNKNOWN_RESULT")
        evidence = {"url": "https://example.test/video/123", "matches": [True, 1, None]}
        self.assertEqual(self.ledger.record(task["id"], "VERIFIED", evidence)["evidence"],
                         evidence)

    def test_missing_tasks_and_unknown_states_fail_explicitly(self):
        for operation in (
            lambda: self.ledger.get("missing"),
            lambda: self.ledger.mark_attempted("missing"),
            lambda: self.ledger.record("missing", "ALREADY_PRESENT", {}),
            lambda: self.ledger.fail_before_attempt("missing", "error"),
        ):
            with self.assertRaises(KeyError):
                operation()
        task = self.prepare()
        with self.assertRaises(ValueError):
            self.ledger.record(task["id"], "PUBLISHED", {})


if __name__ == "__main__":
    unittest.main()
