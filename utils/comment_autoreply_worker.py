"""Bounded background orchestration for the DOM comment adapter.

The adapter owns the browser. This worker only receives public observations,
calls the configured model, and coordinates the durable store. It never opens
account profiles or reads publishing databases. A send adapter MUST call the
synchronous before_submit callback immediately before its single submit action.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import inspect
import re
import threading

from utils.comment_autoreply_store import AutoreplyLimitError, AutoreplyStateError
from utils.comment_ledger import _validate_evidence
from utils.glm_comment_reply import evaluate_decision, preflight_decision


def _code(value, fallback):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", value) else fallback


def _exception_code(prefix, exc):
    # Exception messages may contain authorization headers, raw API bodies or
    # browser state. Persist only the exception class, never str(exc)/repr(exc).
    name = re.sub(r"[^A-Z0-9_]", "", type(exc).__name__.upper())[:40]
    return f"{prefix}_{name or 'ERROR'}"


def _profile_busy(exc):
    """Only the session lock's exact public error is a retryable contention."""
    return isinstance(exc, RuntimeError) and exc.args == ("PROFILE_BUSY",)


async def _invoke(function, *args, **kwargs):
    if inspect.iscoroutinefunction(function):
        return await function(*args, **kwargs)
    result = await asyncio.to_thread(function, *args, **kwargs)
    return await result if inspect.isawaitable(result) else result


class CommentAutoreplyWorker:
    def __init__(self, store, dom, model, context_factory, *, max_per_run=3,
                 tick_seconds=5):
        if max_per_run is not None and (type(max_per_run) is not int or max_per_run <= 0):
            raise ValueError("max_per_run must be None or a positive integer")
        if type(tick_seconds) not in (int, float) or not 0.05 <= tick_seconds <= 60:
            raise ValueError("tick_seconds must be between 0.05 and 60")
        self.store, self.dom, self.model = store, dom, model
        self.context_factory = context_factory
        self.max_per_run, self.tick_seconds = max_per_run, tick_seconds
        self._stop_event = threading.Event()
        self._thread = None
        self._state_lock = threading.RLock()
        self._job_locks = {}
        self._active_jobs = set()
        self._last_tick = None
        self._last_error = None

    def _now(self):
        return self.store.clock().astimezone(timezone.utc)

    def _background_stopping(self):
        return self._stop_event.is_set() and threading.current_thread() is self._thread

    def _round_full(self, result):
        return self.max_per_run is not None and len(result["processed"]) >= self.max_per_run

    def status(self):
        with self._state_lock:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "stopping": self._stop_event.is_set(),
                "last_tick": self._last_tick,
                "active_jobs": sorted(self._active_jobs),
                "last_error": self._last_error,
                "max_per_run": self.max_per_run,
            }

    def start(self):
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status()
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._thread_main, name="comment-autoreply", daemon=True,
            )
            self._thread.start()
            return self.status()

    def stop(self, timeout=60):
        """Stop scheduling; let an in-flight model/send operation finish safely."""
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
                self._last_error = _exception_code("WORKER_ERROR", exc)

    async def _loop(self):
        while not self._stop_event.is_set():
            with self._state_lock:
                self._last_tick = self._now().isoformat()
            try:
                for job in self.store.list_jobs(enabled_only=True):
                    if self._stop_event.is_set():
                        break
                    await self.run_once(job["id"], send=True, force=False)
            except Exception as exc:
                with self._state_lock:
                    self._last_error = _exception_code("WORKER_TICK_ERROR", exc)
            await asyncio.to_thread(self._stop_event.wait, self.tick_seconds)

    def _pause(self, job_id, code):
        self.store.set_enabled(job_id, False)
        with self._state_lock:
            self._last_error = code

    @staticmethod
    def _evidence(result):
        evidence = result.get("evidence", {}) if isinstance(result, dict) else {}
        try:
            _validate_evidence(evidence)
        except (ValueError, TypeError):
            return {"error": "INVALID_EVIDENCE"}
        return evidence

    @staticmethod
    def _observation(comment):
        if not isinstance(comment, dict):
            raise ValueError("invalid_observation")
        result = {name: comment[name] for name in (
            "comment_key", "author", "text", "published_label", "fingerprint",
            "platform_cid", "thread_key", "is_own",
        ) if name in comment}
        raw = dict(comment.get("raw", {}))
        for name in ("ambiguous", "already_replied"):
            if name in comment:
                if type(comment[name]) is not bool:
                    raise ValueError("invalid_observation_flag")
                raw[name] = comment[name]
            if name in raw and type(raw[name]) is not bool:
                raise ValueError("invalid_observation_flag")
        result["raw"] = raw
        return result

    @staticmethod
    def _input(comment, observation):
        raw = observation.get("raw", {})
        return {
            "text": comment["text"],
            "is_author": comment["is_own"],
            "duplicate": False,
            "already_replied": raw.get("already_replied", False),
        }

    @staticmethod
    def _metadata(result):
        metadata = {key: result.get(key) for key in (
            "provider", "model", "request_id", "prompt_version", "usage", "error_code",
        ) if key in result}
        _validate_evidence(metadata)
        return metadata

    async def _send(self, job, comment, observation, *, recheck_only=False):
        comment_id = comment["id"]

        def before_submit():
            if recheck_only:
                raise AutoreplyStateError("READ_ONLY_VERIFICATION")
            # This is also the final enabled/limit check immediately before UI
            # submission; no caller may suppress this exception and then send.
            return self.store.mark_attempted(comment_id)

        try:
            result = await _invoke(
                self.dom.reply, job["publish_task_id"], job["video_id"],
                observation, comment["reply"], before_submit,
                recheck_only=recheck_only,
            )
        except AutoreplyLimitError as exc:
            return {"id": comment_id, "state": "LIMITED",
                    "error": _code(str(exc), "REPLY_LIMIT")}
        except AutoreplyStateError as exc:
            current = self.store.get_comment(comment_id)
            if current["attempted_at"] is not None:
                self.store.finish(comment_id, "UNKNOWN_RESULT", {"error": "SEND_GUARD_STOPPED"})
                return {"id": comment_id, "state": "UNKNOWN_RESULT"}
            return {"id": comment_id, "state": "BLOCKED",
                    "error": _code(str(exc), "SEND_GUARD_STOPPED")}
        except Exception as exc:
            current = self.store.get_comment(comment_id)
            if _profile_busy(exc):
                if current["attempted_at"] is not None:
                    self.store.finish(comment_id, "UNKNOWN_RESULT", {"error": "PROFILE_BUSY"})
                    return {"id": comment_id, "state": "UNKNOWN_RESULT", "error": "PROFILE_BUSY"}
                return {"id": comment_id, "state": "LIMITED", "error": "PROFILE_BUSY"}
            error = _exception_code("DOM_REPLY_ERROR", exc)
            state = "UNKNOWN_RESULT" if current["attempted_at"] is not None else "FAILED"
            self.store.finish(comment_id, state, {"error": error})
            self._pause(job["id"], error)
            return {"id": comment_id, "state": state, "error": error, "paused": True}

        result = result if isinstance(result, dict) else {}
        evidence = self._evidence(result)
        state = result.get("state")
        current = self.store.get_comment(comment_id)
        attempted = current["attempted_at"] is not None
        if state == "BLOCKED":
            error = _code(result.get("error_code"), "DOM_REPLY_BLOCKED")
            if error == "PROFILE_BUSY":
                if attempted:
                    self.store.finish(comment_id, "UNKNOWN_RESULT", {**evidence, "error": error})
                    return {"id": comment_id, "state": "UNKNOWN_RESULT", "error": error}
                return {"id": comment_id, "state": "LIMITED", "error": error}
            if not attempted and error in {
                "REPLY_MIN_INTERVAL", "REPLY_DAILY_LIMIT", "JOB_PAUSED",
            }:
                # The DOM adapter may report a rejected pre-submit callback as
                # BLOCKED instead of propagating its exception. Keep the exact
                # generated draft; a later permitted tick can still submit it.
                return {"id": comment_id,
                        "state": "BLOCKED" if error == "JOB_PAUSED" else "LIMITED",
                        "error": error}
            self.store.finish(comment_id, "UNKNOWN_RESULT" if attempted else "HANDOFF",
                              {**evidence, "error": error})
            self._pause(job["id"], error)
            return {"id": comment_id, "state": "BLOCKED", "error": error, "paused": True}
        if attempted:
            final = "VERIFIED" if state == "VERIFIED" else "UNKNOWN_RESULT"
            self.store.finish(comment_id, final, evidence)
            return {"id": comment_id, "state": final, "recheck_only": recheck_only}
        if state == "SKIP":
            self.store.finish(comment_id, "SKIP", evidence)
            return {"id": comment_id, "state": "SKIP"}
        # VERIFIED without a persisted callback cannot be called a successful
        # send. An adapter that discovers an existing reply must return SKIP.
        self.store.finish(comment_id, "HANDOFF", {"error": "DOM_RESULT_WITHOUT_ATTEMPT"})
        return {"id": comment_id, "state": "HANDOFF", "error": "DOM_RESULT_WITHOUT_ATTEMPT"}

    async def run_once(self, job_id, send=True, force=False):
        if type(send) is not bool or type(force) is not bool:
            raise ValueError("send and force must be booleans")
        with self._state_lock:
            lock = self._job_locks.setdefault(job_id, threading.Lock())
        if not lock.acquire(blocking=False):
            return {"job_id": job_id, "state": "BUSY", "processed": []}
        record_run = False
        result = {"job_id": job_id, "state": "IDLE", "processed": [], "error": None}
        try:
            with self._state_lock:
                self._active_jobs.add(job_id)
                self._last_tick = self._now().isoformat()
            job = self.store.get_job(job_id)
            if not force and job["next_run"] and datetime.fromisoformat(job["next_run"]) > self._now():
                result["state"] = "NOT_DUE"
                return result
            record_run = True
            scan = await _invoke(self.dom.scan, job["publish_task_id"], job["video_id"], max_scrolls=5)
            if not isinstance(scan, dict):
                raise ValueError("invalid_scan")
            if scan.get("state") in {"BLOCKED", "ERROR", "FAILED", "LOGIN_REQUIRED", "CAPTCHA"}:
                error = _code(scan.get("error_code"), "DOM_SCAN_BLOCKED")
                if error == "PROFILE_BUSY":
                    result.update(state="BUSY", error=error)
                    return result
                if job["enabled"]:
                    self._pause(job_id, error)
                result.update(state="BLOCKED", error=error)
                return result
            complete = scan.get("complete") is True
            result["scan_complete"] = complete
            result["scan_evidence"] = self._evidence(scan)
            if not job["baseline_complete"] and not complete:
                result.update(state="BASELINE_INCOMPLETE", baseline=False)
                return result
            observations = [self._observation(comment) for comment in scan.get("comments", [])]
            observed_by_key = {comment["comment_key"]: comment for comment in observations}
            ingestion = self.store.ingest(job_id, observations, baseline=not job["baseline_complete"])
            result.update(counts=ingestion["counts"], baseline=ingestion["baseline"])
            if ingestion["baseline"]:
                result["state"] = "BASELINED"
                return result
            job = self.store.get_job(job_id)
            if not send or not job["enabled"]:
                result["state"] = "READ_ONLY"
                return result

            # This scan is a finite snapshot (the DOM scan stays bounded). Use
            # every observed row rather than applying an implicit SQL LIMIT 100
            # when the caller explicitly disables per-round quantity limits.
            observed_rows = {comment["id"]: comment for comment in ingestion["comments"]}
            unknown = sorted(
                (comment for comment in observed_rows.values() if comment["state"] == "UNKNOWN_RESULT"),
                key=lambda item: item["updated_at"],
            )
            # With a numeric round cap, one read-only recheck reserves room for
            # new work. Unlimited mode checks the whole finite observed set.
            if self.max_per_run is not None:
                unknown = unknown[:1]
            for comment in unknown:
                await asyncio.sleep(0)
                if self._background_stopping() or not self.store.get_job(job_id)["enabled"]:
                    break
                observation = observed_by_key.get(comment["comment_key"])
                if observation is not None:
                    check = await self._send(job, comment, observation, recheck_only=True)
                    result["processed"].append(check)
                    if check.get("paused"):
                        result.update(state="BLOCKED", error=check.get("error"))
                        return result

            if self._round_full(result):
                result["state"] = "PROCESSED"
                return result
            model_status = await _invoke(self.model.status)
            if not isinstance(model_status, dict) or model_status.get("ready") is not True:
                error = _code(model_status.get("status") if isinstance(model_status, dict) else None,
                              "MODEL_NOT_READY")
                self._pause(job_id, error)
                result.update(state="BLOCKED", error=error)
                return result
            context = await _invoke(self.context_factory, job)
            if not isinstance(context, dict):
                raise ValueError("invalid_public_context")
            _validate_evidence(context)
            self.store.recover_stale_generating(job_id)
            current_rows = [self.store.get_comment(comment_id) for comment_id in observed_rows]
            queue = [comment for comment in current_rows if comment["state"] in {"DRAFT", "PREPARED"}
                     and comment["attempted_at"] is None]
            queue += [comment for comment in current_rows if comment["state"] == "NEW"
                      and comment["attempted_at"] is None]
            for queued in queue:
                # Even local skip/handoff decisions must yield so a pause tool
                # sharing this event loop can stop an unlimited finite round.
                await asyncio.sleep(0)
                if self._round_full(result) or self._background_stopping():
                    break
                job = self.store.get_job(job_id)
                if not job["enabled"]:
                    break
                usage = self.store.status(job_id)["usage"]
                if job["daily_limit"] is not None and usage["reply_attempts"] >= job["daily_limit"]:
                    result.update(state="LIMITED", error="REPLY_DAILY_LIMIT")
                    break
                comment = self.store.get_comment(queued["id"])
                observation = observed_by_key.get(comment["comment_key"])
                if observation is None:
                    # A partial scan is not evidence that an offscreen target
                    # disappeared. Keep it queued until a later observed scan.
                    continue
                raw = observation.get("raw", {})
                if raw.get("ambiguous") is True:
                    self.store.finish(comment["id"], "HANDOFF", {"error": "AMBIGUOUS_COMMENT_TARGET"})
                    result["processed"].append({"id": comment["id"], "state": "HANDOFF"})
                    continue
                if comment["is_own"] or raw.get("already_replied") is True:
                    self.store.finish(comment["id"], "SKIP", {"reason": "already_handled"})
                    result["processed"].append({"id": comment["id"], "state": "SKIP"})
                    continue
                if comment["state"] == "NEW":
                    model_input = self._input(comment, observation)
                    decision = preflight_decision(model_input, context)
                    if decision is None and job["max_model_calls_day"] is not None \
                            and usage["model_calls"] >= job["max_model_calls_day"]:
                        result.update(state="LIMITED", error="MODEL_DAILY_LIMIT")
                        break
                    comment = self.store.claim(comment["id"])
                    if comment is None:
                        continue
                    metadata = {"source": "local_preflight"}
                    if decision is None:
                        try:
                            self.store.reserve_model_call(job_id)
                        except (AutoreplyLimitError, AutoreplyStateError) as exc:
                            self.store.release_claim(comment["id"])
                            result.update(state="LIMITED", error=_code(str(exc), "MODEL_RESERVATION_BLOCKED"))
                            break
                        try:
                            generated = await _invoke(self.model.generate, model_input, context)
                            if not isinstance(generated, dict):
                                raise ValueError("invalid_model_result")
                            metadata = self._metadata(generated)
                            decision = evaluate_decision({key: generated.get(key) for key in (
                                "action", "reply", "reason",
                            )}, model_input, context)
                            error = generated.get("error_code")
                            if error:
                                error = _code(error, "MODEL_REQUEST_FAILED")
                                self.store.record_decision(comment["id"], {
                                    "action": "handoff", "reply": "", "reason": "model_request_failed",
                                }, metadata)
                                self._pause(job_id, error)
                                result["processed"].append({"id": comment["id"], "state": "HANDOFF", "error": error})
                                result.update(state="BLOCKED", error=error)
                                break
                        except Exception as exc:
                            error = _exception_code("MODEL_ERROR", exc)
                            self.store.finish(comment["id"], "FAILED", {"error": error})
                            self._pause(job_id, error)
                            result["processed"].append({"id": comment["id"], "state": "FAILED", "error": error})
                            result.update(state="BLOCKED", error=error)
                            break
                    comment = self.store.record_decision(comment["id"], decision, metadata)
                    if comment["state"] != "DRAFT":
                        result["processed"].append({"id": comment["id"], "state": comment["state"]})
                        continue
                if not self.store.get_job(job_id)["enabled"] or self._background_stopping():
                    break
                if comment["state"] == "DRAFT":
                    comment = self.store.prepare(comment["id"])
                sent = await self._send(job, comment, observation)
                result["processed"].append(sent)
                if sent.get("paused") or sent["state"] in {"BLOCKED", "LIMITED"}:
                    result.update(state=sent["state"], error=sent.get("error"))
                    break
            if result["state"] == "IDLE":
                result["state"] = "PROCESSED" if result["processed"] else "NO_NEW_COMMENTS"
            return result
        except Exception as exc:
            if _profile_busy(exc):
                result.update(state="BUSY", error="PROFILE_BUSY")
                return result
            error = _exception_code("WORKER_RUN_ERROR", exc)
            # Public input/DOM/model contract failures stop the job. Never log
            # raw input or exception messages while recording this condition.
            try:
                if self.store.get_job(job_id)["enabled"]:
                    self._pause(job_id, error)
            except KeyError:
                pass
            with self._state_lock:
                self._last_error = error
            result.update(state="BLOCKED", error=error)
            return result
        finally:
            try:
                if record_run:
                    self.store.record_run(job_id, error=result.get("error"))
            finally:
                with self._state_lock:
                    self._active_jobs.discard(job_id)
                lock.release()
