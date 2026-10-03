"""Durable at-most-once state for currently observed Douyin interactions.

This module does not read browser profiles, operate the DOM or call a model.
The watcher controls the bounded notification scan. Only visible comment
notifications enter NEW; replies, likes, follows and unknown kinds enter SKIP.
Call mark_attempted immediately before the one external submit action. A saved
UNKNOWN_RESULT can only be verified by reading; it is never made sendable again.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import unicodedata

from utils.comment_ledger import _validate_evidence, _validate_string


STATES = frozenset({"NEW", "SKIP", "HANDOFF", "PREPARED", "UNKNOWN_RESULT", "VERIFIED"})
TERMINAL_STATES = frozenset({"SKIP", "HANDOFF", "VERIFIED"})


class NotificationStateError(RuntimeError):
    """A transition conflicts with immutable identity or at-most-once sending."""


def _text(value, name, maximum=1000, *, empty=False):
    if not isinstance(value, str) or (not value and not empty) or len(value) > maximum:
        raise ValueError(f"{name} must be a string of at most {maximum} characters")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
           and char not in "\n\t\u200d" for char in value):
        raise ValueError(f"{name} contains invalid control characters")
    _validate_evidence({"value": value})
    return value


def _identifier(value, name, *, optional=False):
    if optional and value is None:
        return None
    _validate_string(value, name, max_length=4000)
    _validate_evidence({"value": value})
    return value


class NotificationStore:
    def __init__(self, path, *, clock=None):
        if str(path) == ":memory:":
            raise ValueError("NotificationStore requires a persistent database path")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with self._transaction() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS douyin_notification_settings (
                    account_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
                    interval_seconds INTEGER NOT NULL DEFAULT 300,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_run TEXT,
                    next_run TEXT,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS douyin_notifications (
                    account_id TEXT NOT NULL REFERENCES douyin_notification_settings(account_id),
                    notification_key TEXT NOT NULL,
                    author TEXT NOT NULL,
                    text TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    display_time TEXT,
                    source_profile_path TEXT,
                    cover_path TEXT,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    state TEXT NOT NULL CHECK(state IN (
                        'NEW','SKIP','HANDOFF','PREPARED','UNKNOWN_RESULT','VERIFIED'
                    )),
                    reply TEXT,
                    decision_json TEXT NOT NULL DEFAULT '{}',
                    context_json TEXT NOT NULL DEFAULT '{}',
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    attempted_at TEXT,
                    error TEXT,
                    PRIMARY KEY(account_id, notification_key)
                );
                CREATE INDEX IF NOT EXISTS douyin_notifications_pending
                    ON douyin_notifications(account_id, state, first_seen);
            """)

    def _now(self):
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
    def _settings(connection, account_id):
        row = connection.execute(
            "SELECT * FROM douyin_notification_settings WHERE account_id = ?", (account_id,)
        ).fetchone()
        if row is None:
            raise KeyError(account_id)
        settings = dict(row)
        settings["enabled"] = bool(settings["enabled"])
        return settings

    @staticmethod
    def _decode(row):
        notice = dict(row)
        for name in ("raw", "decision", "context", "evidence"):
            notice[name] = json.loads(notice.pop(f"{name}_json"))
        return notice

    @staticmethod
    def _notice(connection, account_id, notification_key):
        row = connection.execute("""
            SELECT * FROM douyin_notifications WHERE account_id = ? AND notification_key = ?
        """, (account_id, notification_key)).fetchone()
        if row is None:
            raise KeyError((account_id, notification_key))
        return NotificationStore._decode(row)

    def configure(self, account_id, *, interval_seconds=300):
        """Create disabled settings, or update the interval preserving enabled."""
        _identifier(account_id, "account_id")
        if type(interval_seconds) is not int or not 1 <= interval_seconds <= 2147483647:
            raise ValueError("interval_seconds must be a positive 32-bit integer")
        now = self._iso(self._now())
        with self._transaction() as connection:
            connection.execute("""
                INSERT INTO douyin_notification_settings
                    (account_id, interval_seconds, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    interval_seconds = excluded.interval_seconds,
                    updated_at = excluded.updated_at
            """, (account_id, interval_seconds, now, now))
            return self._settings(connection, account_id)

    def set_enabled(self, account_id, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        now = self._iso(self._now())
        with self._transaction() as connection:
            self._settings(connection, account_id)
            connection.execute("""
                UPDATE douyin_notification_settings SET enabled = ?, updated_at = ?,
                    next_run = CASE WHEN ? THEN ? ELSE next_run END
                WHERE account_id = ?
            """, (int(enabled), now, int(enabled), now, account_id))
            return self._settings(connection, account_id)

    def status(self, account_id):
        with self._connection() as connection:
            settings = self._settings(connection, account_id)
            counts = {state: 0 for state in sorted(STATES)}
            rows = connection.execute("""
                SELECT state, COUNT(*) AS n FROM douyin_notifications
                WHERE account_id = ? GROUP BY state
            """, (account_id,)).fetchall()
            counts.update({row["state"]: row["n"] for row in rows})
            return {"settings": settings, "counts": counts}

    def list_accounts(self, *, enabled_only=False):
        """Return configured public settings, optionally only enabled accounts."""
        if type(enabled_only) is not bool:
            raise ValueError("enabled_only must be a boolean")
        with self._connection() as connection:
            query = "SELECT account_id FROM douyin_notification_settings"
            if enabled_only:
                query += " WHERE enabled = 1"
            query += " ORDER BY created_at, account_id"
            return [self._settings(connection, row["account_id"])
                    for row in connection.execute(query).fetchall()]

    @staticmethod
    def _observation(notice):
        if not isinstance(notice, dict):
            raise ValueError("notice must be a public observation object")
        key = _identifier(notice.get("notification_key"), "notification_key")
        author = _text(notice.get("author"), "author", 300)
        text = _text(notice.get("text", ""), "text", 10000, empty=True)
        kind = _identifier(notice.get("kind"), "kind").lower()
        if kind == "like":
            kind = "likes"
        profile_path = notice.get("source_profile_path", notice.get("sourceprofile_path"))
        _identifier(profile_path, "source_profile_path", optional=True)
        cover_path = _identifier(notice.get("cover_path"), "cover_path", optional=True)
        display_time = notice.get("display_time")
        if display_time is not None:
            _text(display_time, "display_time", 300, empty=True)
        raw = _validate_evidence(notice.get("raw", {}))
        return {"notification_key": key, "author": author, "text": text, "kind": kind,
                "display_time": display_time, "source_profile_path": profile_path,
                "cover_path": cover_path, "raw_json": raw}

    def ingest(self, account_id, notices):
        """Register only the finite visible scan supplied by the caller.

        Relative display times and safe click indices may change. Content and
        stable source/cover identifiers remain bound to their initial key.
        """
        observations = [self._observation(notice) for notice in notices]
        counts = {"NEW": 0, "SKIP": 0, "existing": 0}
        keys = []
        now = self._iso(self._now())
        with self._transaction() as connection:
            self._settings(connection, account_id)
            for notice in observations:
                key = notice["notification_key"]
                existing = connection.execute("""
                    SELECT * FROM douyin_notifications
                    WHERE account_id = ? AND notification_key = ?
                """, (account_id, key)).fetchone()
                if existing is not None:
                    if any(existing[name] != notice[name] for name in (
                        "author", "text", "kind", "source_profile_path", "cover_path",
                    )):
                        raise NotificationStateError("NOTIFICATION_IDENTITY_CONFLICT")
                    connection.execute("""
                        UPDATE douyin_notifications SET display_time = ?, raw_json = ?,
                            last_seen = ? WHERE account_id = ? AND notification_key = ?
                    """, (notice["display_time"], notice["raw_json"], now, account_id, key))
                    if notice["kind"] != "comment" and existing["state"] in {"NEW", "PREPARED"} \
                            and existing["attempted_at"] is None:
                        connection.execute("""
                            UPDATE douyin_notifications SET state = 'SKIP', evidence_json = ?,
                                updated_at = ? WHERE account_id = ? AND notification_key = ?
                        """, (_validate_evidence({**json.loads(existing["evidence_json"]),
                                                   "scope_action": "skip", "scope_reason": "outside_top_level_comment_scope"}),
                              now, account_id, key))
                    counts["existing"] += 1
                else:
                    state = "NEW" if notice["kind"] == "comment" else "SKIP"
                    decision = {} if state == "NEW" else {
                        "action": "skip", "reason": "non_comment_notification",
                    }
                    connection.execute("""
                        INSERT INTO douyin_notifications (account_id, notification_key,
                            author, text, kind, display_time, source_profile_path, cover_path,
                            raw_json, state, decision_json, first_seen, last_seen, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (account_id, key, notice["author"], notice["text"], notice["kind"],
                          notice["display_time"], notice["source_profile_path"], notice["cover_path"],
                          notice["raw_json"], state, _validate_evidence(decision), now, now, now))
                    counts[state] += 1
                keys.append(key)
            return {"counts": counts,
                    "notices": [self._notice(connection, account_id, key) for key in keys]}

    def get(self, account_id, notification_key):
        with self._connection() as connection:
            return self._notice(connection, account_id, notification_key)

    def _by_state(self, account_id, state):
        with self._connection() as connection:
            self._settings(connection, account_id)
            rows = connection.execute("""
                SELECT * FROM douyin_notifications WHERE account_id = ? AND state = ?
                ORDER BY first_seen, rowid
            """, (account_id, state)).fetchall()
            return [self._decode(row) for row in rows]

    def pending(self, account_id):
        """Return all durable NEW records; no daily or per-round quantity cap."""
        return self._by_state(account_id, "NEW")

    def prepared(self, account_id):
        return self._by_state(account_id, "PREPARED")

    def unknown(self, account_id):
        """These rows are only eligible for read-only result verification."""
        return self._by_state(account_id, "UNKNOWN_RESULT")

    def prepare(self, account_id, notification_key, reply, decision=None, context=None):
        _text(reply, "reply", 200)
        encoded_decision = _validate_evidence({} if decision is None else decision)
        if decision is not None and decision.get("action", "reply") != "reply":
            raise ValueError("only reply decisions can be prepared")
        encoded_context = _validate_evidence({} if context is None else context)
        now = self._iso(self._now())
        with self._transaction() as connection:
            notice = self._notice(connection, account_id, notification_key)
            if notice["kind"] != "comment":
                raise NotificationStateError("NOTIFICATION_SCOPE_EXCLUDED")
            if notice["attempted_at"] is not None:
                raise NotificationStateError("DUPLICATE_SUBMISSION_BLOCKED")
            if notice["state"] == "PREPARED":
                if notice["reply"] != reply or notice["decision"] != json.loads(encoded_decision) \
                        or notice["context"] != json.loads(encoded_context):
                    raise NotificationStateError("PREPARED_PAYLOAD_CONFLICT")
                return notice
            if notice["state"] != "NEW":
                raise NotificationStateError("NOTIFICATION_NOT_NEW")
            connection.execute("""
                UPDATE douyin_notifications SET state = 'PREPARED', reply = ?,
                    decision_json = ?, context_json = ?, updated_at = ?
                WHERE account_id = ? AND notification_key = ?
            """, (reply, encoded_decision, encoded_context, now, account_id, notification_key))
            return self._notice(connection, account_id, notification_key)

    def mark_attempted(self, account_id, notification_key):
        """Commit UNKNOWN_RESULT before submit; no API can reopen this guard."""
        now = self._iso(self._now())
        with self._transaction() as connection:
            settings = self._settings(connection, account_id)
            if not settings["enabled"]:
                raise NotificationStateError("NOTIFICATIONS_PAUSED")
            notice = self._notice(connection, account_id, notification_key)
            if notice["kind"] != "comment":
                raise NotificationStateError("NOTIFICATION_SCOPE_EXCLUDED")
            if notice["attempted_at"] is not None or notice["state"] != "PREPARED":
                raise NotificationStateError("DUPLICATE_SUBMISSION_BLOCKED")
            connection.execute("""
                UPDATE douyin_notifications SET state = 'UNKNOWN_RESULT',
                    attempted_at = ?, updated_at = ?
                WHERE account_id = ? AND notification_key = ?
            """, (now, now, account_id, notification_key))
            return self._notice(connection, account_id, notification_key)

    def finish(self, account_id, notification_key, state, evidence):
        if state not in {"SKIP", "HANDOFF", "UNKNOWN_RESULT", "VERIFIED"}:
            raise ValueError("unsupported notification result state")
        encoded = _validate_evidence(evidence)
        now = self._iso(self._now())
        with self._transaction() as connection:
            notice = self._notice(connection, account_id, notification_key)
            if notice["state"] in TERMINAL_STATES and state != notice["state"]:
                raise NotificationStateError("TERMINAL_NOTIFICATION_STATE")
            attempted = notice["attempted_at"] is not None
            if state in {"VERIFIED", "UNKNOWN_RESULT"} and not attempted:
                raise NotificationStateError("NOTIFICATION_NOT_ATTEMPTED")
            if state in {"SKIP", "HANDOFF"} and attempted:
                raise NotificationStateError("NOTIFICATION_ALREADY_ATTEMPTED")
            error = evidence.get("error")
            if not isinstance(error, str):
                error = None
            connection.execute("""
                UPDATE douyin_notifications SET state = ?, evidence_json = ?, error = ?,
                    updated_at = ? WHERE account_id = ? AND notification_key = ?
            """, (state, encoded, error, now, account_id, notification_key))
            return self._notice(connection, account_id, notification_key)

    def record_run(self, account_id, error=None, next_run=None):
        if error is not None:
            _text(error, "error", 4000)
        now = self._now()
        if next_run is not None:
            if isinstance(next_run, str):
                next_run = datetime.fromisoformat(next_run)
            if not isinstance(next_run, datetime) or next_run.tzinfo is None:
                raise ValueError("next_run must be an aware datetime or ISO string")
        with self._transaction() as connection:
            settings = self._settings(connection, account_id)
            next_at = self._iso(next_run or (now + timedelta(seconds=settings["interval_seconds"])))
            connection.execute("""
                UPDATE douyin_notification_settings SET last_run = ?, next_run = ?,
                    last_error = ?, updated_at = ? WHERE account_id = ?
            """, (self._iso(now), next_at, error, self._iso(now), account_id))
            return self._settings(connection, account_id)

    def history(self, account_id, limit=100, *, state=None):
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if state is not None and state not in STATES:
            raise ValueError("unsupported notification state")
        with self._connection() as connection:
            self._settings(connection, account_id)
            query = "SELECT * FROM douyin_notifications WHERE account_id = ?"
            parameters = [account_id]
            if state is not None:
                query += " AND state = ?"
                parameters.append(state)
            query += " ORDER BY updated_at DESC, rowid DESC LIMIT ?"
            parameters.append(limit)
            return [self._decode(row) for row in connection.execute(query, parameters).fetchall()]
