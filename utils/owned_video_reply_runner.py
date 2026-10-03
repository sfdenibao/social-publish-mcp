"""One-off frozen-video draft/submit operations; no scheduler or enabled flags."""

import asyncio
import inspect
import re
import threading

from utils.comment_ledger import _validate_evidence
from utils.glm_comment_reply import evaluate_decision, preflight_decision


class OwnedVideoReplyRunError(RuntimeError):
    pass


async def _invoke(function, *args, **kwargs):
    if inspect.iscoroutinefunction(function):
        return await function(*args, **kwargs)
    value = await asyncio.to_thread(function, *args, **kwargs)
    return await value if inspect.isawaitable(value) else value


def _error(exc):
    if isinstance(exc, RuntimeError) and len(exc.args) == 1 and isinstance(exc.args[0], str) \
            and re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", exc.args[0]):
        return exc.args[0]
    return "OWNED_REPLY_" + re.sub(r"[^A-Z0-9_]", "", type(exc).__name__.upper())[:40]


def _safe_code(value, fallback):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", value) else fallback


class OwnedVideoReplyRunner:
    def __init__(self, store, adapter, model):
        self.store, self.adapter, self.model = store, adapter, model
        self._lock = threading.Lock()

    def status(self):
        return {"busy": self._lock.locked()}

    def get_run(self, run_id):
        return self.store.get_run(run_id)

    @staticmethod
    def _notice(run, item):
        return {"notification_key": "owned:" + run["video_id"] + ":" + item["comment_id"],
                "kind": "comment", "author": item["author"], "text": item["text"],
                "source_profile_path": item["profile_path"],
                "raw": {"video_id": run["video_id"], "comment_id": item["comment_id"]},
                "context": item["context"]}

    @staticmethod
    def _scope(context):
        if not isinstance(context, dict):
            return "HANDOFF"
        if context.get("is_own_video") is False or context.get("is_top_level") is False \
                or (isinstance(context.get("work_type"), str) and context["work_type"]
                    and context["work_type"] != "video"):
            return "SKIP"
        if context.get("is_own_video") is not True or context.get("is_top_level") is not True \
                or context.get("work_type") != "video":
            return "HANDOFF"
        return None

    @staticmethod
    def _bound(context, run, item):
        if context.get("video_id") != run["video_id"] \
                or context.get("comment_id") != item["comment_id"] \
                or context.get("root_id") != item["comment_id"]:
            return False
        actual = context.get("comment")
        return isinstance(actual, str) and " ".join(actual.split()) == " ".join(item["text"].split())

    @staticmethod
    def _evidence(result):
        if not isinstance(result, dict):
            return {"error": "INVALID_ADAPTER_RESULT"}
        evidence = result.get("evidence")
        if evidence is None:
            evidence = {key: result[key] for key in (
                "screenshot_path", "observed_at", "url", "matches", "reason", "scope",
            ) if key in result}
        try:
            _validate_evidence(evidence)
        except (ValueError, TypeError):
            return {"error": "INVALID_ADAPTER_EVIDENCE"}
        return evidence

    async def prepare(self, account_id, video_id):
        if not self._lock.acquire(blocking=False):
            raise OwnedVideoReplyRunError("OWNED_REPLY_RUNNER_BUSY")
        try:
            scan = await _invoke(self.adapter.read_video, account_id, video_id)
            if not isinstance(scan, dict) or scan.get("complete") is not True:
                raise OwnedVideoReplyRunError("INCOMPLETE_VIDEO_COMMENT_SCAN")
            card = scan.get("card", {})
            if not isinstance(card, dict) or card.get("video_id") != video_id \
                    or card.get("work_type") != "video" \
                    or scan.get("video_id", video_id) != video_id \
                    or scan.get("account_id", account_id) != account_id:
                raise OwnedVideoReplyRunError("OWNED_VIDEO_SCAN_IDENTITY_MISMATCH")
            run = self.store.create_run(account_id, video_id, card["position"], card["title"], scan["comments"])
            errors = []
            for item in self.store.list_items(run["id"]):
                await asyncio.sleep(0)
                if item["state"] != "NEW":
                    continue
                if item.get("is_author") is True:
                    self.store.skip(run["id"], item["comment_id"], "OWN_COMMENT")
                    continue
                try:
                    context = await _invoke(self.adapter.read_context, account_id, self._notice(run, item))
                    _validate_evidence(context)
                    scope = self._scope(context)
                    if scope is not None:
                        self.store.finish(run["id"], item["comment_id"], scope,
                                          {"error": "OWN_VIDEO_TOP_LEVEL_SCOPE_NOT_CONFIRMED", "context": context})
                        continue
                    if not self._bound(context, run, item):
                        self.store.finish(run["id"], item["comment_id"], "HANDOFF",
                                          {"error": "FROZEN_COMMENT_CONTEXT_MISMATCH", "context": context})
                        continue
                    decision = preflight_decision(item["text"], context)
                    metadata = {"source": "local_preflight"}
                    if decision is None:
                        configured = await _invoke(self.model.status)
                        if not isinstance(configured, dict) or configured.get("ready") is not True:
                            errors.append({"comment_id": item["comment_id"], "error": _safe_code(
                                configured.get("status") if isinstance(configured, dict) else None, "MODEL_NOT_READY")})
                            break
                        generated = await _invoke(self.model.generate, item["text"], context)
                        if not isinstance(generated, dict):
                            raise OwnedVideoReplyRunError("INVALID_MODEL_RESULT")
                        if generated.get("error_code"):
                            errors.append({"comment_id": item["comment_id"],
                                           "error": _safe_code(generated["error_code"], "MODEL_REQUEST_FAILED")})
                            break
                        metadata = {field: generated[field] for field in (
                            "provider", "model", "usage", "request_id", "prompt_version",
                        ) if field in generated}
                        _validate_evidence(metadata)
                        decision = evaluate_decision({field: generated.get(field) for field in (
                            "action", "reply", "reason",
                        )}, item["text"], context)
                    if decision["action"] != "reply":
                        state = "SKIP" if decision["action"] == "skip" else "HANDOFF"
                        self.store.finish(run["id"], item["comment_id"], state,
                                          {"decision": decision, "context": context, "model_meta": metadata})
                    else:
                        self.store.prepare(run["id"], item["comment_id"], decision["reply"],
                                           {**decision, "model_meta": metadata}, context)
                except Exception as exc:
                    # No send occurred. Leave NEW available for an explicit
                    # repaired retry instead of making a transient failure final.
                    errors.append({"comment_id": item["comment_id"], "error": _error(exc)})
            return {**self.store.get_run(run["id"]), "status": "PARTIAL" if errors else "PREPARED",
                    "errors": errors, "scan_evidence": self._evidence(scan)}
        finally:
            self._lock.release()

    async def submit(self, run_id, expected_video_id):
        if not self._lock.acquire(blocking=False):
            raise OwnedVideoReplyRunError("OWNED_REPLY_RUNNER_BUSY")
        try:
            run = self.store.get_run(run_id)
            if run["video_id"] != expected_video_id:
                raise OwnedVideoReplyRunError("EXPECTED_VIDEO_ID_MISMATCH")
            errors, results = [], []
            for item in self.store.list_items(run_id):
                await asyncio.sleep(0)
                key = item["comment_id"]
                if item["state"] not in {"PREPARED", "UNKNOWN_RESULT"}:
                    results.append({"comment_id": key, "status": item["state"]})
                    continue
                recheck_only = item["state"] == "UNKNOWN_RESULT"
                notice = self._notice(run, item)
                try:
                    if self._scope(item["context"]) is not None or not self._bound(item["context"], run, item):
                        raise OwnedVideoReplyRunError("PREPARED_TARGET_SCOPE_NOT_CONFIRMED")
                    if recheck_only:
                        method = getattr(self.adapter, "recheck", None)
                        if not callable(method):
                            raise OwnedVideoReplyRunError("RECHECK_UNAVAILABLE")
                        response = await _invoke(method, run["account_id"], notice, item["reply"])
                    else:
                        def before_submit(comment_id=key):
                            persisted = self.store.get_item(run_id, comment_id)
                            if self._scope(persisted["context"]) is not None \
                                    or not self._bound(persisted["context"], run, persisted):
                                raise OwnedVideoReplyRunError("PREPARED_TARGET_SCOPE_NOT_CONFIRMED")
                            return self.store.mark_attempted(run_id, comment_id)

                        response = await _invoke(self.adapter.reply, run["account_id"], notice,
                                                 item["reply"], before_submit=before_submit)
                    if not isinstance(response, dict):
                        raise OwnedVideoReplyRunError("INVALID_ADAPTER_RESULT")
                    observed = response.get("status", response.get("state"))
                    current = self.store.get_item(run_id, key)
                    evidence = self._evidence(response)
                    if current["attempted_at"] is not None:
                        final = "VERIFIED" if observed == "VERIFIED" else "UNKNOWN_RESULT"
                        self.store.finish(run_id, key, final, evidence)
                        results.append({"comment_id": key, "status": final, "recheck_only": recheck_only})
                    elif observed in {"SKIP", "ALREADY_PRESENT"}:
                        self.store.skip(run_id, key, response.get("reason") or observed, evidence)
                        results.append({"comment_id": key, "status": "SKIP"})
                    else:
                        raise OwnedVideoReplyRunError(_safe_code(response.get("error_code"),
                                                                "ADAPTER_RESULT_WITHOUT_ATTEMPT"))
                except Exception as exc:
                    error = _error(exc)
                    current = self.store.get_item(run_id, key)
                    if current["attempted_at"] is not None:
                        self.store.finish(run_id, key, "UNKNOWN_RESULT", {"error": error})
                    # Unattempted PREPARED remains unchanged for repair/retry.
                    errors.append({"comment_id": key, "error": error, "state": current["state"]})
                    results.append({"comment_id": key, "status": current["state"], "error": error,
                                    "recheck_only": recheck_only})
            return {**self.store.get_run(run_id), "status": "PARTIAL" if errors else "COMPLETED",
                    "results": results, "errors": errors}
        finally:
            self._lock.release()
