"""Durable state for a bounded, single-work comment auto-reply job.

This module never opens a browser or reads publishing/account databases. A new
job is disabled. Its first successful complete scan is always a baseline; only
subsequent observations can become reply candidates. Call ``mark_attempted``
immediately before the external send action. Once that call commits, no API in
this store can make the comment sendable again, even after an uncertain result.
All times are UTC ISO strings; daily budgets use the Asia/Shanghai calendar.
An explicit None daily budget is unlimited. SQLite stores that as zero so old
NOT NULL databases need no table rebuild; public APIs never expose the sentinel.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from utils.comment_ledger import _validate_evidence, _validate_string


SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
STATES = frozenset({
    "NEW", "BASELINED", "OWN", "SKIP", "HANDOFF", "GENERATING", "DRAFT",
    "PREPARED", "UNKNOWN_RESULT", "VERIFIED", "FAILED",
})


class AutoreplyStateError(RuntimeError):
    """A transition would break a job's identity or submission guarantee."""


class AutoreplyLimitError(AutoreplyStateError):
    """A durable daily budget or minimum send interval was reached."""


def _text(value, name, maximum=1000, optional=False):
    if optional and value is None:
        return None
    return _validate_string(value, name, max_length=maximum)


def _integer(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _budget(value, name):
    """Encode an optional positive SQLite integer; zero is internal-only."""
    if value is None:
        return 0
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be None or a positive integer")
    if value > 9223372036854775807:
        raise ValueError(f"{name} must fit a SQLite integer")
    return value


def _config_json(config):
    encoded = _validate_evidence(config)

    def inspect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                if any(part in normalized for part in (
                    "script", "javascript", "eval", "executable", "command",
                    "endpoint", "baseurl", "webhook", "browserurl", "targeturl",
                )) or normalized in {"url", "uri"}:
                    raise ValueError("job config cannot specify URLs or executable code")
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)
        elif isinstance(value, str) and re.search(
            r"(?:https?://|javascript:|<script\b)", value, re.IGNORECASE
        ):
            raise ValueError("job config cannot specify URLs or executable code")

    inspect(config)
    return encoded


class CommentAutoreplyStore:
    def __init__(self, path, *, clock=None):
        if str(path) == ":memory:":
            raise ValueError("CommentAutoreplyStore requires a persistent database path")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with self._transaction() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS autoreply_jobs (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    publish_task_id TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
                    baseline_complete INTEGER NOT NULL DEFAULT 0,
                    interval_seconds INTEGER NOT NULL,
                    min_reply_interval INTEGER NOT NULL,
                    daily_limit INTEGER NOT NULL,
                    max_model_calls_day INTEGER NOT NULL,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_run TEXT,
                    next_run TEXT,
                    last_error TEXT,
                    UNIQUE(account_id, video_id)
                );
                CREATE TABLE IF NOT EXISTS autoreply_inbox (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES autoreply_jobs(id),
                    account_id TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    comment_key TEXT NOT NULL,
                    author TEXT NOT NULL,
                    text TEXT NOT NULL,
                    published_label TEXT,
                    fingerprint TEXT,
                    platform_cid TEXT,
                    thread_key TEXT,
                    is_own INTEGER NOT NULL CHECK(is_own IN (0,1)),
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    state TEXT NOT NULL CHECK(state IN (
                        'NEW','BASELINED','OWN','SKIP','HANDOFF','GENERATING',
                        'DRAFT','PREPARED','UNKNOWN_RESULT','VERIFIED','FAILED'
                    )),
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    claimed_at TEXT,
                    attempted_at TEXT,
                    reply TEXT,
                    decision_json TEXT NOT NULL DEFAULT '{}',
                    model_meta_json TEXT NOT NULL DEFAULT '{}',
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    UNIQUE(account_id, video_id, comment_key)
                );
                CREATE INDEX IF NOT EXISTS autoreply_pending
                    ON autoreply_inbox(job_id, state, first_seen);
                CREATE TABLE IF NOT EXISTS autoreply_usage (
                    job_id TEXT NOT NULL REFERENCES autoreply_jobs(id),
                    day TEXT NOT NULL,
                    model_calls INTEGER NOT NULL DEFAULT 0,
                    reply_attempts INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(job_id, day)
                );
                CREATE TABLE IF NOT EXISTS autoreply_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES autoreply_jobs(id),
                    comment_id TEXT,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
            """)

    def _now_dt(self):
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc)

    @staticmethod
    def _iso(now):
        return now.astimezone(timezone.utc).isoformat(timespec="microseconds")

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _job(connection, job_id):
        row = connection.execute(
            "SELECT * FROM autoreply_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        job = dict(row)
        job["enabled"] = bool(job["enabled"])
        job["baseline_complete"] = bool(job["baseline_complete"])
        for field in ("daily_limit", "max_model_calls_day"):
            if job[field] == 0:
                job[field] = None
        job["config"] = json.loads(job.pop("config_json"))
        return job

    @staticmethod
    def _comment(connection, comment_id):
        row = connection.execute(
            "SELECT * FROM autoreply_inbox WHERE id = ?", (comment_id,)
        ).fetchone()
        if row is None:
            raise KeyError(comment_id)
        return CommentAutoreplyStore._decode_comment(row)

    @staticmethod
    def _decode_comment(row):
        comment = dict(row)
        comment["is_own"] = bool(comment["is_own"])
        for name in ("raw", "decision", "model_meta", "evidence"):
            comment[name] = json.loads(comment.pop(f"{name}_json"))
        return comment

    @staticmethod
    def _event(connection, job_id, kind, now, comment_id=None, details=None):
        connection.execute("""
            INSERT INTO autoreply_events
                (job_id, comment_id, kind, created_at, details_json)
            VALUES (?, ?, ?, ?, ?)
        """, (job_id, comment_id, kind, now, _validate_evidence(details or {})))

    def upsert_job(self, account_id, publish_task_id, video_id, *, job_id=None,
                   interval_seconds=300, min_reply_interval=60, daily_limit=20,
                   max_model_calls_day=40, config=None):
        """Bind a work; explicit None budgets mean unlimited, never enable it."""
        _text(account_id, "account_id", 200)
        _text(publish_task_id, "publish_task_id", 200)
        _text(video_id, "video_id", 200)
        if job_id is not None:
            _text(job_id, "job_id", 200)
        _integer(interval_seconds, "interval_seconds", 30, 86400)
        _integer(min_reply_interval, "min_reply_interval", 0, 86400)
        daily_limit = _budget(daily_limit, "daily_limit")
        max_model_calls_day = _budget(max_model_calls_day, "max_model_calls_day")
        encoded = _config_json(config or {})
        now = self._iso(self._now_dt())
        with self._transaction() as connection:
            existing = connection.execute("""
                SELECT id FROM autoreply_jobs WHERE account_id = ? AND video_id = ?
            """, (account_id, video_id)).fetchone()
            if existing:
                if job_id is not None and job_id != existing["id"]:
                    raise AutoreplyStateError("JOB_IDENTITY_CONFLICT")
                job_id = existing["id"]
            elif job_id is not None:
                existing = connection.execute(
                    "SELECT id FROM autoreply_jobs WHERE id = ?", (job_id,)
                ).fetchone()
            if existing:
                job = self._job(connection, job_id)
                if (job["account_id"], job["publish_task_id"], job["video_id"]) != (
                    account_id, publish_task_id, video_id
                ):
                    raise AutoreplyStateError("JOB_IDENTITY_CONFLICT")
                connection.execute("""
                    UPDATE autoreply_jobs SET interval_seconds = ?,
                        min_reply_interval = ?, daily_limit = ?,
                        max_model_calls_day = ?, config_json = ?, updated_at = ?
                    WHERE id = ?
                """, (interval_seconds, min_reply_interval, daily_limit,
                      max_model_calls_day, encoded, now, job_id))
            else:
                job_id = job_id or uuid4().hex
                connection.execute("""
                    INSERT INTO autoreply_jobs (id, account_id, publish_task_id,
                        video_id, interval_seconds, min_reply_interval,
                        daily_limit, max_model_calls_day, config_json,
                        created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (job_id, account_id, publish_task_id, video_id,
                      interval_seconds, min_reply_interval, daily_limit,
                      max_model_calls_day, encoded, now, now))
            self._event(connection, job_id, "JOB_CONFIGURED", now)
            return self._job(connection, job_id)

    def set_enabled(self, job_id, enabled):
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        now = self._iso(self._now_dt())
        with self._transaction() as connection:
            self._job(connection, job_id)
            connection.execute("""
                UPDATE autoreply_jobs SET enabled = ?, updated_at = ?,
                    next_run = CASE WHEN ? THEN ? ELSE next_run END
                WHERE id = ?
            """, (int(enabled), now, int(enabled), now, job_id))
            self._event(connection, job_id, "ENABLED" if enabled else "PAUSED", now)
            return self._job(connection, job_id)

    def get_job(self, job_id):
        with self._connection() as connection:
            return self._job(connection, job_id)

    def list_jobs(self, *, enabled_only=False):
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id FROM autoreply_jobs "
                + ("WHERE enabled = 1 " if enabled_only else "")
                + "ORDER BY created_at, id"
            ).fetchall()
            return [self._job(connection, row["id"]) for row in rows]

    @staticmethod
    def _observation(comment):
        if not isinstance(comment, dict):
            raise ValueError("each comment must be a dict of public DOM fields")
        observation = {}
        for key, maximum in (("comment_key", 500), ("author", 300), ("text", 10000)):
            observation[key] = _text(comment.get(key), key, maximum)
        for key, maximum in (("published_label", 300), ("fingerprint", 1000),
                             ("platform_cid", 300), ("thread_key", 500)):
            observation[key] = _text(comment.get(key), key, maximum, optional=True)
        is_own = comment.get("is_own", False)
        if not isinstance(is_own, bool):
            raise ValueError("is_own must be a boolean")
        observation["is_own"] = is_own
        observation["raw_json"] = _validate_evidence(comment.get("raw", {}))
        _validate_evidence({k: v for k, v in observation.items() if k != "raw_json"})
        return observation

    def ingest(self, job_id, comments, *, baseline=False):
        """Ingest one COMPLETE scan; callers must not baseline a partial page."""
        if not isinstance(baseline, bool):
            raise ValueError("baseline must be a boolean")
        observations = [self._observation(comment) for comment in comments]
        now = self._iso(self._now_dt())
        counts = {"NEW": 0, "BASELINED": 0, "OWN": 0, "existing": 0}
        result_ids = []
        with self._transaction() as connection:
            job = self._job(connection, job_id)
            use_baseline = baseline or not job["baseline_complete"]
            for observed in observations:
                existing = connection.execute("""
                    SELECT id FROM autoreply_inbox
                    WHERE account_id = ? AND video_id = ? AND comment_key = ?
                """, (job["account_id"], job["video_id"], observed["comment_key"])).fetchone()
                if existing:
                    stored = self._comment(connection, existing["id"])
                    if any(stored[field] != observed[field] for field in (
                        "author", "text", "thread_key", "platform_cid", "is_own"
                    )):
                        raise AutoreplyStateError("COMMENT_IDENTITY_CONFLICT")
                    connection.execute(
                        "UPDATE autoreply_inbox SET last_seen = ? WHERE id = ?",
                        (now, stored["id"]),
                    )
                    counts["existing"] += 1
                    result_ids.append(stored["id"])
                    continue
                state = "OWN" if observed["is_own"] else (
                    "BASELINED" if use_baseline else "NEW"
                )
                comment_id = uuid4().hex
                connection.execute("""
                    INSERT INTO autoreply_inbox (id, job_id, account_id, video_id,
                        comment_key, author, text, published_label, fingerprint,
                        platform_cid, thread_key, is_own, raw_json, state,
                        first_seen, last_seen, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (comment_id, job_id, job["account_id"], job["video_id"],
                      observed["comment_key"], observed["author"], observed["text"],
                      observed["published_label"], observed["fingerprint"],
                      observed["platform_cid"], observed["thread_key"],
                      int(observed["is_own"]), observed["raw_json"], state, now, now, now))
                counts[state] += 1
                result_ids.append(comment_id)
            connection.execute("""
                UPDATE autoreply_jobs SET baseline_complete = 1, updated_at = ?
                WHERE id = ?
            """, (now, job_id))
            self._event(connection, job_id, "INGESTED", now,
                        details={"baseline": use_baseline, "counts": counts})
            return {"baseline": use_baseline, "counts": counts,
                    "comments": [self._comment(connection, cid) for cid in result_ids]}

    def get_comment(self, comment_id):
        with self._connection() as connection:
            return self._comment(connection, comment_id)

    def pending(self, job_id, limit=20):
        _integer(limit, "limit", 1, 1000)
        with self._connection() as connection:
            self._job(connection, job_id)
            rows = connection.execute("""
                SELECT * FROM autoreply_inbox
                WHERE job_id = ? AND state = 'NEW' AND attempted_at IS NULL
                ORDER BY first_seen, rowid LIMIT ?
            """, (job_id, limit)).fetchall()
            return [self._decode_comment(row) for row in rows]

    def ready(self, job_id, limit=20):
        """Return unsent drafts/prepared rows for recovery without regeneration."""
        _integer(limit, "limit", 1, 1000)
        with self._connection() as connection:
            self._job(connection, job_id)
            rows = connection.execute("""
                SELECT * FROM autoreply_inbox
                WHERE job_id = ? AND state IN ('DRAFT', 'PREPARED')
                    AND attempted_at IS NULL AND reply IS NOT NULL
                ORDER BY first_seen, rowid LIMIT ?
            """, (job_id, limit)).fetchall()
            return [self._decode_comment(row) for row in rows]

    def pending_unknown(self, job_id, limit=20):
        """Return uncertain sends exclusively for read-only verification."""
        return self.history(job_id, limit=limit, state="UNKNOWN_RESULT")

    def claim(self, comment_id):
        """Return the claimed row, or None when another worker/paused job wins."""
        now = self._iso(self._now_dt())
        with self._transaction() as connection:
            comment = self._comment(connection, comment_id)
            job = self._job(connection, comment["job_id"])
            if not job["enabled"] or comment["state"] != "NEW" \
                    or comment["attempted_at"] is not None or comment["reply"] is not None:
                return None
            connection.execute("""
                UPDATE autoreply_inbox SET state = 'GENERATING', claimed_at = ?,
                    updated_at = ? WHERE id = ?
            """, (now, now, comment_id))
            self._event(connection, job["id"], "CLAIMED", now, comment_id)
            return self._comment(connection, comment_id)

    def release_claim(self, comment_id):
        """Return an unused generation claim to NEW after a pause or budget stop."""
        now = self._iso(self._now_dt())
        with self._transaction() as connection:
            comment = self._comment(connection, comment_id)
            if comment["state"] != "GENERATING" or comment["reply"] is not None \
                    or comment["attempted_at"] is not None:
                raise AutoreplyStateError("CLAIM_NOT_RELEASABLE")
            connection.execute("""
                UPDATE autoreply_inbox SET state = 'NEW', claimed_at = NULL,
                    updated_at = ? WHERE id = ?
            """, (now, comment_id))
            self._event(connection, comment["job_id"], "CLAIM_RELEASED", now, comment_id)
            return self._comment(connection, comment_id)

    @staticmethod
    def _usage(connection, job_id, day):
        row = connection.execute("""
            SELECT model_calls, reply_attempts FROM autoreply_usage
            WHERE job_id = ? AND day = ?
        """, (job_id, day)).fetchone()
        return {"day": day, "model_calls": row["model_calls"] if row else 0,
                "reply_attempts": row["reply_attempts"] if row else 0}

    def reserve_model_call(self, job_id):
        """Reserve before the request; failed/uncertain requests still cost budget."""
        now_dt = self._now_dt()
        now = self._iso(now_dt)
        day = now_dt.astimezone(SHANGHAI).date().isoformat()
        with self._transaction() as connection:
            job = self._job(connection, job_id)
            if not job["enabled"]:
                raise AutoreplyStateError("JOB_PAUSED")
            usage = self._usage(connection, job_id, day)
            if job["max_model_calls_day"] is not None \
                    and usage["model_calls"] >= job["max_model_calls_day"]:
                raise AutoreplyLimitError("MODEL_DAILY_LIMIT")
            connection.execute("""
                INSERT INTO autoreply_usage(job_id, day, model_calls)
                VALUES (?, ?, 1) ON CONFLICT(job_id, day)
                DO UPDATE SET model_calls = model_calls + 1
            """, (job_id, day))
            self._event(connection, job_id, "MODEL_RESERVED", now)
            return self._usage(connection, job_id, day)

    def record_decision(self, comment_id, decision, model_meta=None):
        encoded = _validate_evidence(decision)
        metadata = _validate_evidence(model_meta or {})
        action = decision.get("action")
        if action not in {"reply", "skip", "handoff"}:
            raise ValueError("decision.action must be reply, skip, or handoff")
        reply = _text(decision.get("reply"), "reply", 200) if action == "reply" else None
        state = {"reply": "DRAFT", "skip": "SKIP", "handoff": "HANDOFF"}[action]
        now = self._iso(self._now_dt())
        with self._transaction() as connection:
            comment = self._comment(connection, comment_id)
            if comment["state"] != "GENERATING" or comment["attempted_at"] is not None:
                raise AutoreplyStateError("COMMENT_NOT_GENERATING")
            connection.execute("""
                UPDATE autoreply_inbox SET state = ?, reply = ?, decision_json = ?,
                    model_meta_json = ?, updated_at = ?, error = NULL WHERE id = ?
            """, (state, reply, encoded, metadata, now, comment_id))
            self._event(connection, comment["job_id"], "DECIDED", now, comment_id,
                        {"action": action})
            return self._comment(connection, comment_id)

    def prepare(self, comment_id):
        now = self._iso(self._now_dt())
        with self._transaction() as connection:
            comment = self._comment(connection, comment_id)
            job = self._job(connection, comment["job_id"])
            if not job["enabled"]:
                raise AutoreplyStateError("JOB_PAUSED")
            if comment["state"] != "DRAFT" or comment["attempted_at"] is not None \
                    or not comment["reply"]:
                raise AutoreplyStateError("COMMENT_NOT_DRAFT")
            connection.execute("""
                UPDATE autoreply_inbox SET state = 'PREPARED', updated_at = ?
                WHERE id = ?
            """, (now, comment_id))
            self._event(connection, comment["job_id"], "PREPARED", now, comment_id)
            return self._comment(connection, comment_id)

    def mark_attempted(self, comment_id):
        """Commit UNKNOWN_RESULT and count this send before touching the UI."""
        now_dt = self._now_dt()
        now = self._iso(now_dt)
        day = now_dt.astimezone(SHANGHAI).date().isoformat()
        with self._transaction() as connection:
            comment = self._comment(connection, comment_id)
            job = self._job(connection, comment["job_id"])
            if not job["enabled"]:
                raise AutoreplyStateError("JOB_PAUSED")
            if comment["state"] != "PREPARED" or comment["attempted_at"] is not None:
                raise AutoreplyStateError("DUPLICATE_SUBMISSION_BLOCKED")
            usage = self._usage(connection, job["id"], day)
            if job["daily_limit"] is not None and usage["reply_attempts"] >= job["daily_limit"]:
                raise AutoreplyLimitError("REPLY_DAILY_LIMIT")
            latest = connection.execute("""
                SELECT MAX(attempted_at) AS attempted_at FROM autoreply_inbox
                WHERE job_id = ? AND attempted_at IS NOT NULL
            """, (job["id"],)).fetchone()["attempted_at"]
            if latest is not None and (now_dt - datetime.fromisoformat(latest)).total_seconds() \
                    < job["min_reply_interval"]:
                raise AutoreplyLimitError("REPLY_MIN_INTERVAL")
            connection.execute("""
                UPDATE autoreply_inbox SET state = 'UNKNOWN_RESULT',
                    attempted_at = ?, updated_at = ? WHERE id = ?
            """, (now, now, comment_id))
            connection.execute("""
                INSERT INTO autoreply_usage(job_id, day, reply_attempts)
                VALUES (?, ?, 1) ON CONFLICT(job_id, day)
                DO UPDATE SET reply_attempts = reply_attempts + 1
            """, (job["id"], day))
            self._event(connection, job["id"], "ATTEMPTED", now, comment_id)
            return self._comment(connection, comment_id)

    def finish(self, comment_id, state, evidence):
        if state not in {"VERIFIED", "UNKNOWN_RESULT", "FAILED", "SKIP", "HANDOFF"}:
            raise ValueError("unsupported finish state")
        encoded = _validate_evidence(evidence)
        now = self._iso(self._now_dt())
        with self._transaction() as connection:
            comment = self._comment(connection, comment_id)
            attempted = comment["attempted_at"] is not None
            if comment["state"] in {"VERIFIED", "OWN", "BASELINED", "SKIP", "HANDOFF"} \
                    and state != comment["state"]:
                raise AutoreplyStateError("TERMINAL_COMMENT_STATE")
            if state in {"VERIFIED", "UNKNOWN_RESULT"} and not attempted:
                raise AutoreplyStateError("COMMENT_NOT_ATTEMPTED")
            if attempted and state in {"SKIP", "HANDOFF"}:
                raise AutoreplyStateError("COMMENT_ALREADY_ATTEMPTED")
            error = evidence.get("error")
            if not isinstance(error, str):
                error = None
            connection.execute("""
                UPDATE autoreply_inbox SET state = ?, evidence_json = ?,
                    error = ?, updated_at = ? WHERE id = ?
            """, (state, encoded, error, now, comment_id))
            self._event(connection, comment["job_id"], "FINISHED", now,
                        comment_id, {"state": state})
            return self._comment(connection, comment_id)

    def recover_stale_generating(self, job_id, stale_seconds=900):
        _integer(stale_seconds, "stale_seconds", 1, 86400 * 30)
        now_dt = self._now_dt()
        now = self._iso(now_dt)
        cutoff = self._iso(now_dt - timedelta(seconds=stale_seconds))
        with self._transaction() as connection:
            self._job(connection, job_id)
            cursor = connection.execute("""
                UPDATE autoreply_inbox SET state = 'NEW', claimed_at = NULL,
                    updated_at = ?
                WHERE job_id = ? AND state = 'GENERATING' AND attempted_at IS NULL
                    AND reply IS NULL AND claimed_at <= ?
            """, (now, job_id, cutoff))
            self._event(connection, job_id, "RECOVERED", now,
                        details={"count": cursor.rowcount})
            return cursor.rowcount

    def record_run(self, job_id, error=None, next_run=None):
        if error is not None:
            _text(error, "error", 4000)
            _validate_evidence({"error": error})
        now_dt = self._now_dt()
        now = self._iso(now_dt)
        if next_run is not None:
            if isinstance(next_run, str):
                next_run = datetime.fromisoformat(next_run)
            if not isinstance(next_run, datetime) or next_run.tzinfo is None:
                raise ValueError("next_run must be an aware datetime or ISO string")
        with self._transaction() as connection:
            job = self._job(connection, job_id)
            next_at = self._iso(next_run or (
                now_dt + timedelta(seconds=job["interval_seconds"])
            ))
            connection.execute("""
                UPDATE autoreply_jobs SET last_run = ?, next_run = ?,
                    last_error = ?, updated_at = ? WHERE id = ?
            """, (now, next_at, error, now, job_id))
            self._event(connection, job_id, "RUN_FINISHED", now,
                        details={"error": error, "next_run": next_at})
            return self._job(connection, job_id)

    def status(self, job_id):
        day = self._now_dt().astimezone(SHANGHAI).date().isoformat()
        with self._connection() as connection:
            job = self._job(connection, job_id)
            rows = connection.execute("""
                SELECT state, COUNT(*) AS n FROM autoreply_inbox
                WHERE job_id = ? GROUP BY state
            """, (job_id,)).fetchall()
            counts = {state: 0 for state in sorted(STATES)}
            counts.update({row["state"]: row["n"] for row in rows})
            return {"job": job, "counts": counts,
                    "usage": self._usage(connection, job_id, day)}

    def history(self, job_id, limit=100, *, state=None):
        _integer(limit, "limit", 1, 1000)
        if state is not None and state not in STATES:
            raise ValueError("unsupported comment state")
        with self._connection() as connection:
            self._job(connection, job_id)
            query = "SELECT * FROM autoreply_inbox WHERE job_id = ?"
            parameters = [job_id]
            if state is not None:
                query += " AND state = ?"
                parameters.append(state)
            query += " ORDER BY updated_at DESC, rowid DESC LIMIT ?"
            parameters.append(limit)
            rows = connection.execute(query, parameters).fetchall()
            return [self._decode_comment(row) for row in rows]

    def event_history(self, job_id, limit=100):
        _integer(limit, "limit", 1, 1000)
        with self._connection() as connection:
            self._job(connection, job_id)
            rows = connection.execute("""
                SELECT * FROM autoreply_events WHERE job_id = ?
                ORDER BY id DESC LIMIT ?
            """, (job_id, limit)).fetchall()
            events = []
            for row in rows:
                event = dict(row)
                event["details"] = json.loads(event.pop("details_json"))
                events.append(event)
            return events
