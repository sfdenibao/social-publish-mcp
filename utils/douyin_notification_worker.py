"""Finite-scan notification watcher with durable, at-most-once submission.

The adapter exclusively owns DOM operations. No browser/profile or API client
is created here. Background scheduling reads persisted enabled accounts and
does not impose daily or per-round quantity limits on pending notifications.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import inspect
import re
import threading

from utils.comment_ledger import _validate_evidence
from utils.douyin_notification_store import NotificationStateError
from utils.glm_comment_reply import evaluate_decision, preflight_decision


_SESSION_ERRORS = frozenset({
    "LOGIN_REQUIRED", "LOGIN_EXPIRED", "NOT_LOGGED_IN", "AUTH_REQUIRED",
    "CAPTCHA", "CAPTCHA_REQUIRED", "VERIFY_REQUIRED", "VERIFICATION_REQUIRED",
    "ACCOUNT_MISMATCH", "ACCOUNT_NOT_READY", "INVALID_SESSION", "SESSION_EXPIRED",
    "DOUYIN_WEB_ACCOUNT_CHANGED", "DOUYIN_WEB_ACCOUNT_NOT_CONFIRMED",
    "DOUYIN_HANDLE_NOT_VISIBLE",
    "SESSION_FILE_MISSING", "LOGGED_OUT", "NEEDS_LOGIN", "CHALLENGE_REQUIRED",
    "DOUYIN_PUBLIC_IDENTITY_NOT_PINNED",
})
_TARGET_ERROR_PREFIXES = (
    "TARGET_", "COMMENT_TARGET_", "PUBLIC_COMMENT_TARGET_", "PARENT_COMMENT_",
    "REPLY_RECIPIENT_", "PREPARED_CONTEXT_",
)
_TARGET_ERRORS = frozenset({
    "NOTIFICATION_NOT_UNIQUE", "NOTIFICATION_NOT_FOUND", "NO_REPLYABLE_NOTIFICATION_TEXT",
    "REPLY_WITHOUT_SUBMIT_GUARD",
    "NOTIFICATION_NOT_VISIBLE", "PREPARED_NOTIFICATION_TARGET_CHANGED",
    "NOTIFICATION_SCOPE_EXCLUDED", "PREPARED_SCOPE_EXCLUDED", "PREPARED_SCOPE_UNKNOWN",
})
_TRANSIENT_DOM_ERRORS = frozenset({
    "NOTIFICATION_CONTEXT_TIMEOUTERROR", "NOTIFICATION_REPLY_TIMEOUTERROR",
    "NOTIFICATION_RUN_TIMEOUTERROR", "NOTIFICATION_RECHECK_TIMEOUTERROR",
})


def _code(value, fallback):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", value) else fallback


def _exception_code(exc, prefix):
    if isinstance(exc, RuntimeError) and len(exc.args) == 1:
        known = _code(exc.args[0], None)
        if known:
            return known
    # Never preserve exception messages containing public/raw response payloads
    # or request credentials. The exception type is enough for generic failure.
    return prefix + "_" + re.sub(r"[^A-Z0-9_]", "", type(exc).__name__.upper())[:40]


async def _invoke(function, *args, **kwargs):
    if inspect.iscoroutinefunction(function):
        return await function(*args, **kwargs)
    result = await asyncio.to_thread(function, *args, **kwargs)
    return await result if inspect.isawaitable(result) else result


class NotificationWorker:
    def __init__(self, store, adapter, model, *, tick_seconds=5):
        if type(tick_seconds) not in (int, float) or not 0.05 <= tick_seconds <= 60:
            raise ValueError("tick_seconds must be between 0.05 and 60")
        self.store, self.adapter, self.model = store, adapter, model
        self.tick_seconds = tick_seconds
        self._run_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread = None
        self._active_account = None
        self._last_tick = None
        self._last_error = None

    def _now(self):
        return self.store.clock().astimezone(timezone.utc)

    def _stopping(self):
        return self._stop_event.is_set() and threading.current_thread() is self._thread

    def status(self):
        with self._state_lock:
            return {"running": self._thread is not None and self._thread.is_alive(),
                    "stopping": self._stop_event.is_set(), "active_account": self._active_account,
                    "last_tick": self._last_tick, "last_error": self._last_error}

    def start(self):
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status()
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._thread_main,
                                            name="douyin-notifications", daemon=True)
            self._thread.start()
            return self.status()

    def stop(self, timeout=60):
        """Stop new background work without cancelling an in-flight send."""
        if type(timeout) not in (int, float) or not 0 <= timeout <= 60:
            raise ValueError("stop timeout must be between 0 and 60 seconds")
        self._stop_event.set()
        with self._state_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        return self.status()

    def _thread_main(self):
        try:
            asyncio.run(self._loop())
        except Exception as exc:
            with self._state_lock:
                self._last_error = _exception_code(exc, "NOTIFICATION_THREAD")

    async def _loop(self):
        while not self._stop_event.is_set():
            try:
                for settings in self.store.list_accounts(enabled_only=True):
                    if self._stop_event.is_set():
                        break
                    await self.run_once(settings["account_id"], send=True)
            except Exception as exc:
                with self._state_lock:
                    self._last_error = _exception_code(exc, "NOTIFICATION_TICK")
            await asyncio.to_thread(self._stop_event.wait, self.tick_seconds)

    def _enabled(self, account_id):
        return self.store.status(account_id)["settings"]["enabled"]

    def _pause(self, account_id, error):
        self.store.set_enabled(account_id, False)
        with self._state_lock:
            self._last_error = error

    @staticmethod
    def _evidence(result):
        if not isinstance(result, dict):
            return {"error": "INVALID_ADAPTER_RESULT"}
        evidence = result.get("evidence")
        if evidence is None:
            evidence = {key: result[key] for key in (
                "url", "observed_at", "screenshot_path", "screenshot", "reply_visible", "attempted",
            ) if key in result}
        try:
            _validate_evidence(evidence)
        except (ValueError, TypeError):
            return {"error": "INVALID_ADAPTER_EVIDENCE"}
        return evidence

    @staticmethod
    def _result_error(result):
        if not isinstance(result, dict):
            return "INVALID_ADAPTER_RESULT"
        status = result.get("status", result.get("state"))
        if status in {"BLOCKED", "ERROR", "FAILED", "BUSY", "LOGIN_REQUIRED", "CAPTCHA"}:
            return _code(result.get("error_code", result.get("error")),
                         status if status in _SESSION_ERRORS else "ADAPTER_BLOCKED")
        return None

    def _notice_failure(self, account_id, notice, error, *, evidence=None):
        """Handle one target failure while preserving any committed send guard."""
        key = notice["notification_key"]
        current = self.store.get(account_id, key)
        attempted = current["attempted_at"] is not None
        if attempted:
            self.store.finish(account_id, key, "UNKNOWN_RESULT", {**(evidence or {}), "error": error})
            state = "UNKNOWN_RESULT"
        elif error in _TRANSIENT_DOM_ERRORS:
            state = "RETRY"
        elif error in {"PROFILE_BUSY", "NOTIFICATIONS_PAUSED"}:
            # NEW/PREPARED stay retryable because no submit attempt took place.
            state = "BUSY" if error == "PROFILE_BUSY" else "PAUSED"
        elif error in {"NOTIFICATION_SCOPE_EXCLUDED", "PREPARED_SCOPE_EXCLUDED"}:
            self.store.finish(account_id, key, "SKIP", {**(evidence or {}), "error": error})
            state = "SKIP"
        else:
            self.store.finish(account_id, key, "HANDOFF", {**(evidence or {}), "error": error})
            state = "HANDOFF"
        target_failure = error in _TARGET_ERRORS or error.startswith(_TARGET_ERROR_PREFIXES)
        stop_round = not target_failure
        if error not in {"PROFILE_BUSY", "NOTIFICATIONS_PAUSED"} \
                and error not in _TRANSIENT_DOM_ERRORS and not target_failure:
            self._pause(account_id, error)
        return {"notification_key": key, "status": state, "error": error,
                "stop_round": stop_round}

    @staticmethod
    def _scope_decision(context):
        """Only explicit actual-DOM proof of an own-video root comment passes."""
        if not isinstance(context, dict):
            return {"action": "handoff", "reply": "", "reason": "notification_scope_unconfirmed"}
        if context.get("is_own_video") is False or context.get("is_top_level") is False \
                or (isinstance(context.get("work_type"), str) and context["work_type"]
                    and context["work_type"] != "video"):
            return {"action": "skip", "reply": "", "reason": "outside_own_video_top_level_scope"}
        if context.get("is_own_video") is not True or context.get("is_top_level") is not True \
                or context.get("work_type") != "video":
            return {"action": "handoff", "reply": "", "reason": "notification_scope_unconfirmed"}
        return None

    def _finish_scope(self, account_id, notice, decision, context):
        state = "SKIP" if decision["action"] == "skip" else "HANDOFF"
        self.store.finish(account_id, notice["notification_key"], state,
                          {"decision": decision, "context": context})
        return {"notification_key": notice["notification_key"], "status": state,
                "error": "NOTIFICATION_SCOPE_EXCLUDED" if state == "SKIP" else "NOTIFICATION_SCOPE_UNCONFIRMED"}

    async def _recheck(self, account_id, notice):
        # Legacy replies or unknown scope stay as historical UNKNOWN_RESULT.
        # Do not open their targets just to recheck an old, now excluded send.
        if notice["kind"] != "comment" or self._scope_decision(notice["context"]) is not None:
            return {"notification_key": notice["notification_key"], "status": "UNKNOWN_RESULT",
                    "error": "RECHECK_SCOPE_EXCLUDED", "recheck_only": True, "target_opened": False}
        method = getattr(self.adapter, "recheck", None)
        if not callable(method):
            return {"notification_key": notice["notification_key"], "status": "UNKNOWN_RESULT",
                    "error": "RECHECK_UNAVAILABLE", "recheck_only": True}
        try:
            context = await _invoke(self.adapter.read_context, account_id, notice)
            error = self._result_error(context)
            if error:
                return self._notice_failure(account_id, notice, error, evidence=self._evidence(context))
            _validate_evidence(context)
            scope_decision = self._scope_decision(context)
            if scope_decision is not None:
                self.store.finish(account_id, notice["notification_key"], "UNKNOWN_RESULT",
                                  {"decision": scope_decision, "context": context, "scope_excluded": True})
                return {"notification_key": notice["notification_key"], "status": "UNKNOWN_RESULT",
                        "error": "RECHECK_SCOPE_EXCLUDED", "recheck_only": True}
            result = await _invoke(method, account_id, notice, notice["reply"])
            error = self._result_error(result)
            if error:
                return self._notice_failure(account_id, notice, error, evidence=self._evidence(result))
            status = "VERIFIED" if result.get("status", result.get("state")) == "VERIFIED" else "UNKNOWN_RESULT"
            self.store.finish(account_id, notice["notification_key"], status, self._evidence(result))
            return {"notification_key": notice["notification_key"], "status": status, "recheck_only": True}
        except (NotImplementedError, AttributeError):
            return {"notification_key": notice["notification_key"], "status": "UNKNOWN_RESULT",
                    "error": "RECHECK_UNAVAILABLE", "recheck_only": True}
        except Exception as exc:
            return self._notice_failure(account_id, notice, _exception_code(exc, "NOTIFICATION_RECHECK"))

    async def _reply(self, account_id, notice):
        key = notice["notification_key"]

        def before_submit():
            persisted = self.store.get(account_id, key)
            if persisted["kind"] != "comment":
                raise NotificationStateError("NOTIFICATION_SCOPE_EXCLUDED")
            scope = self._scope_decision(persisted["context"])
            if scope is not None:
                raise NotificationStateError("PREPARED_SCOPE_EXCLUDED" if scope["action"] == "skip"
                                             else "PREPARED_SCOPE_UNKNOWN")
            return self.store.mark_attempted(account_id, key)

        try:
            result = await _invoke(self.adapter.reply, account_id, notice, notice["reply"],
                                   before_submit=before_submit)
            error = self._result_error(result)
            if error:
                return self._notice_failure(account_id, notice, error, evidence=self._evidence(result))
            current = self.store.get(account_id, key)
            status = result.get("status", result.get("state"))
            if current["attempted_at"] is not None:
                final = "VERIFIED" if status == "VERIFIED" else "UNKNOWN_RESULT"
                self.store.finish(account_id, key, final, self._evidence(result))
                return {"notification_key": key, "status": final}
            if status in {"SKIP", "ALREADY_PRESENT"}:
                self.store.finish(account_id, key, "SKIP", self._evidence(result))
                return {"notification_key": key, "status": "SKIP"}
            return self._notice_failure(account_id, notice, "REPLY_WITHOUT_SUBMIT_GUARD")
        except Exception as exc:
            return self._notice_failure(account_id, notice, _exception_code(exc, "NOTIFICATION_REPLY"))

    async def _process(self, account_id, notice):
        key = notice["notification_key"]
        if notice["kind"] != "comment":
            return self._notice_failure(account_id, notice, "NOTIFICATION_SCOPE_EXCLUDED")
        try:
            context = await _invoke(self.adapter.read_context, account_id, notice)
            error = self._result_error(context)
            if error:
                return self._notice_failure(account_id, notice, error, evidence=self._evidence(context))
            _validate_evidence(context)
            scope_decision = self._scope_decision(context)
            if scope_decision is not None:
                return self._finish_scope(account_id, notice, scope_decision, context)
            # Use actual target context, never a global example post. If the
            # adapter returns the observed comment text, enforce its identity.
            actual = context.get("comment")
            if isinstance(actual, dict):
                actual = actual.get("text")
            if actual is not None and (not isinstance(actual, str) or
                    " ".join(actual.split()) != " ".join(notice["text"].split())):
                return self._notice_failure(account_id, notice, "TARGET_COMMENT_MISMATCH")
        except Exception as exc:
            return self._notice_failure(account_id, notice, _exception_code(exc, "NOTIFICATION_CONTEXT"))

        decision = preflight_decision(notice["text"], context)
        if notice["state"] == "PREPARED":
            scope_decision = self._scope_decision(notice["context"])
            if scope_decision is not None:
                return self._finish_scope(account_id, notice, scope_decision, notice["context"])
            for field in ("comment", "parent_comment", "post_title", "post_description", "self_handle"):
                if field in notice["context"] and notice["context"][field] != context.get(field):
                    return self._notice_failure(account_id, notice, "PREPARED_CONTEXT_CHANGED")
            if decision is not None and decision["action"] != "reply":
                state = "SKIP" if decision["action"] == "skip" else "HANDOFF"
                self.store.finish(account_id, key, state, {"decision": decision, "context": context})
                return {"notification_key": key, "status": state}
        else:
            metadata = {"source": "local_preflight"}
            if decision is None:
                if not self._enabled(account_id) or self._stopping():
                    return {"notification_key": key, "status": "PAUSED", "stop_round": True}
                try:
                    model_status = await _invoke(self.model.status)
                    if not isinstance(model_status, dict) or model_status.get("ready") is not True:
                        error = _code(model_status.get("status") if isinstance(model_status, dict) else None,
                                      "MODEL_NOT_READY")
                        self._pause(account_id, error)
                        return {"notification_key": key, "status": "BLOCKED", "error": error, "stop_round": True}
                    generated = await _invoke(self.model.generate, notice["text"], context)
                    if not isinstance(generated, dict):
                        raise ValueError("invalid_model_result")
                    metadata = {field: generated[field] for field in (
                        "provider", "model", "usage", "request_id", "prompt_version", "error_code",
                    ) if field in generated}
                    _validate_evidence(metadata)
                    if generated.get("error_code"):
                        error = _code(generated["error_code"], "MODEL_REQUEST_FAILED")
                        self.store.finish(account_id, key, "HANDOFF", {"error": error, "model_meta": metadata})
                        self._pause(account_id, error)
                        return {"notification_key": key, "status": "HANDOFF", "error": error, "stop_round": True}
                    decision = evaluate_decision({field: generated.get(field) for field in (
                        "action", "reply", "reason",
                    )}, notice["text"], context)
                except Exception as exc:
                    error = _exception_code(exc, "NOTIFICATION_MODEL")
                    self.store.finish(account_id, key, "HANDOFF", {"error": error})
                    self._pause(account_id, error)
                    return {"notification_key": key, "status": "HANDOFF", "error": error, "stop_round": True}
            if decision["action"] != "reply":
                state = "SKIP" if decision["action"] == "skip" else "HANDOFF"
                self.store.finish(account_id, key, state, {
                    "decision": decision, "context": context, "model_meta": metadata,
                })
                return {"notification_key": key, "status": state}
            notice = self.store.prepare(account_id, key, decision["reply"],
                                        {**decision, "model_meta": metadata}, context)
        if not self._enabled(account_id) or self._stopping():
            return {"notification_key": key, "status": "PAUSED", "stop_round": True}
        return await self._reply(account_id, notice)

    async def run_once(self, account_id, send=False, force=False):
        if type(send) is not bool or type(force) is not bool:
            raise ValueError("send and force must be booleans")
        if not self._run_lock.acquire(blocking=False):
            return {"account_id": account_id, "status": "BUSY", "processed": []}
        result = {"account_id": account_id, "status": "IDLE", "processed": [], "error": None}
        record_run = False
        try:
            with self._state_lock:
                self._active_account = account_id
                self._last_tick = self._now().isoformat()
            settings = self.store.status(account_id)["settings"]
            if not force and settings["next_run"] and datetime.fromisoformat(settings["next_run"]) > self._now():
                result["status"] = "NOT_DUE"
                return result
            record_run = True
            scan = await _invoke(self.adapter.scan, account_id)
            error = self._result_error(scan)
            if error:
                raise RuntimeError(error)
            ingestion = self.store.ingest(account_id, scan.get("notices", []))
            result.update(counts=ingestion["counts"], scan_evidence=self._evidence(scan))
            if not send or not self._enabled(account_id):
                result["status"] = "SCANNED_READ_ONLY"
                return result

            for notice in self.store.unknown(account_id):
                await asyncio.sleep(0)
                if self._stopping() or not self._enabled(account_id):
                    result["status"] = "PAUSED"
                    return result
                if notice["kind"] != "comment":
                    # Preserve attempted history without opening reply threads
                    # or other users' works under the narrowed scope.
                    continue
                checked = await self._recheck(account_id, notice)
                result["processed"].append(checked)
                if checked.get("stop_round"):
                    stopped_status = "BUSY" if checked.get("error") == "PROFILE_BUSY" else (
                        "RETRY" if checked.get("error") in _TRANSIENT_DOM_ERRORS else "BLOCKED"
                    )
                    result.update(status=stopped_status, error=checked.get("error"))
                    return result

            queue = self.store.prepared(account_id) + self.store.pending(account_id)
            for notice in queue:
                # The queue is a finite snapshot, and local decisions yield so
                # a pause tool sharing this event loop can still take effect.
                await asyncio.sleep(0)
                if self._stopping() or not self._enabled(account_id):
                    result["status"] = "PAUSED"
                    break
                processed = await self._process(account_id, self.store.get(account_id, notice["notification_key"]))
                result["processed"].append(processed)
                if processed.get("stop_round"):
                    stopped_status = "BUSY" if processed.get("error") == "PROFILE_BUSY" else (
                        "PAUSED" if processed["status"] == "PAUSED" else (
                            "RETRY" if processed.get("error") in _TRANSIENT_DOM_ERRORS else "BLOCKED"
                        )
                    )
                    result.update(status=stopped_status, error=processed.get("error"))
                    break
            if result["status"] == "IDLE":
                result["status"] = "PROCESSED" if result["processed"] else "NO_PENDING_NOTIFICATIONS"
            return result
        except Exception as exc:
            error = _exception_code(exc, "NOTIFICATION_RUN")
            status = "BUSY" if error == "PROFILE_BUSY" else (
                "RETRY" if error in _TRANSIENT_DOM_ERRORS else "BLOCKED"
            )
            result.update(status=status, error=error)
            if error != "PROFILE_BUSY" and error not in _TRANSIENT_DOM_ERRORS:
                try:
                    if self._enabled(account_id):
                        self._pause(account_id, error)
                except KeyError:
                    pass
            return result
        finally:
            try:
                if record_run:
                    self.store.record_run(account_id, error=result.get("error"))
            finally:
                with self._state_lock:
                    self._active_account = None
                self._run_lock.release()
