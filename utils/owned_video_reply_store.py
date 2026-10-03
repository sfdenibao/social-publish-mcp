"""Frozen one-off own-video runs with cross-run at-most-once reply guards.

Pass a persistent path, normally runtime/owned-video-replies.sqlite3. This store
has no scheduler or enabled flag and never accesses notification configuration.
Each run freezes the selected video's observed comment targets. A shared guard
per account/video/comment survives duplicate runs, process restarts, changed
reply text and an uncertain send outcome. Commit mark_attempted before clicking.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from utils.comment_ledger import _validate_evidence
from utils.douyin_notification_store import _identifier, _text


class OwnedVideoReplyStateError(RuntimeError):
    """A frozen target or shared submission guard would be contradicted."""


class OwnedVideoReplyStore:
    def __init__(self, path, *, clock=None):
        if str(path) == ":memory:":
            raise ValueError("OwnedVideoReplyStore requires a persistent database path")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with self._transaction() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS owned_video_runs (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    work_position INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS owned_video_comment_guards (
                    account_id TEXT NOT NULL,
                    video_id TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    identity_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'NEW','PREPARED','SKIP','HANDOFF','UNKNOWN_RESULT','VERIFIED'
                    )),
                    reply TEXT,
                    decision_json TEXT NOT NULL DEFAULT '{}',
                    context_json TEXT NOT NULL DEFAULT '{}',
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    prepared_run_id TEXT REFERENCES owned_video_runs(id),
                    attempted_run_id TEXT REFERENCES owned_video_runs(id),
                    attempted_at TEXT,
                    error TEXT,
                    PRIMARY KEY(account_id, video_id, comment_id)
                );
                CREATE TABLE IF NOT EXISTS owned_video_run_items (
                    run_id TEXT NOT NULL REFERENCES owned_video_runs(id),
                    comment_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    PRIMARY KEY(run_id, comment_id),
                    UNIQUE(run_id, ordinal)
                );
            """)

    def _now(self):
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
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
    def _run(connection, run_id):
        row = connection.execute("SELECT * FROM owned_video_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return dict(row)

    @staticmethod
    def _item(connection, run_id, comment_id):
        run = OwnedVideoReplyStore._run(connection, run_id)
        frozen = connection.execute("""
            SELECT snapshot_json, ordinal FROM owned_video_run_items
            WHERE run_id = ? AND comment_id = ?
        """, (run_id, comment_id)).fetchone()
        if frozen is None:
            raise KeyError((run_id, comment_id))
        row = connection.execute("""
            SELECT * FROM owned_video_comment_guards
            WHERE account_id = ? AND video_id = ? AND comment_id = ?
        """, (run["account_id"], run["video_id"], comment_id)).fetchone()
        guard = dict(row)
        item = json.loads(frozen["snapshot_json"])
        # Mutable results come exclusively from the shared guard, never from
        # similarly named fields in a supplied public DOM observation.
        for name in ("account_id", "video_id", "comment_id", "state", "reply",
                     "created_at", "updated_at", "prepared_run_id", "attempted_run_id",
                     "attempted_at", "error"):
            item[name] = guard[name]
        for name in ("decision", "context", "evidence"):
            item[name] = json.loads(guard[f"{name}_json"])
        item["run_id"] = run_id
        item["ordinal"] = frozen["ordinal"]
        return item

    @staticmethod
    def _items(connection, run_id):
        OwnedVideoReplyStore._run(connection, run_id)
        rows = connection.execute("""
            SELECT comment_id FROM owned_video_run_items WHERE run_id = ? ORDER BY ordinal
        """, (run_id,)).fetchall()
        return [OwnedVideoReplyStore._item(connection, run_id, row["comment_id"]) for row in rows]

    @staticmethod
    def _observation(comment):
        if not isinstance(comment, dict):
            raise ValueError("comment must be a public DOM observation object")
        comment_id = comment.get("comment_id", comment.get("comment_key"))
        _identifier(comment_id, "comment_id")
        if "comment_id" in comment and "comment_key" in comment and comment["comment_key"] != comment_id:
            raise ValueError("comment_id and comment_key conflict")
        author = _text(comment.get("author"), "author", 300)
        text = _text(comment.get("text"), "text", 10000, empty=True)
        profile = comment.get("profile_path", comment.get("source_profile_path"))
        _identifier(profile, "profile_path", optional=True)
        if "profile_path" in comment and "source_profile_path" in comment \
                and comment["source_profile_path"] != profile:
            raise ValueError("profile path aliases conflict")
        target_ids = comment.get("target_ids", {})
        _validate_evidence(target_ids)
        identity = {"author": author, "text": text, "profile_path": profile,
                    "target_ids": target_ids}
        for name in ("platform_cid", "parent_comment_id", "root_comment_id", "thread_key"):
            if name in comment:
                identity[name] = _identifier(comment[name], name, optional=True)
        snapshot = {**comment, "comment_id": comment_id, "author": author,
                    "text": text, "profile_path": profile, "target_ids": target_ids}
        return comment_id, _validate_evidence(identity), _validate_evidence(snapshot)

    def create_run(self, account_id, video_id, work_position, title, comments):
        """Freeze this exact observed work/target set without enabling a worker."""
        _identifier(account_id, "account_id")
        _identifier(video_id, "video_id")
        if type(work_position) is not int or not 1 <= work_position <= 2147483647:
            raise ValueError("work_position must be a positive 32-bit integer")
        _text(title, "title", 10000, empty=True)
        observations = [self._observation(comment) for comment in comments]
        if len({comment[0] for comment in observations}) != len(observations):
            raise ValueError("duplicate comment ids in a frozen run")
        run_id, now = uuid4().hex, self._now()
        with self._transaction() as connection:
            connection.execute("""
                INSERT INTO owned_video_runs (id, account_id, video_id, work_position, title, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (run_id, account_id, video_id, work_position, title, now))
            for ordinal, (comment_id, identity_json, snapshot_json) in enumerate(observations):
                existing = connection.execute("""
                    SELECT identity_json FROM owned_video_comment_guards
                    WHERE account_id = ? AND video_id = ? AND comment_id = ?
                """, (account_id, video_id, comment_id)).fetchone()
                if existing is not None:
                    if json.loads(existing["identity_json"]) != json.loads(identity_json):
                        raise OwnedVideoReplyStateError("COMMENT_IDENTITY_CONFLICT")
                else:
                    connection.execute("""
                        INSERT INTO owned_video_comment_guards
                            (account_id, video_id, comment_id, identity_json, state, created_at, updated_at)
                        VALUES (?, ?, ?, ?, 'NEW', ?, ?)
                    """, (account_id, video_id, comment_id, identity_json, now, now))
                connection.execute("""
                    INSERT INTO owned_video_run_items (run_id, comment_id, ordinal, snapshot_json)
                    VALUES (?, ?, ?, ?)
                """, (run_id, comment_id, ordinal, snapshot_json))
            return {**self._run(connection, run_id), "comments": self._items(connection, run_id)}

    def get_run(self, run_id):
        with self._connection() as connection:
            return {**self._run(connection, run_id), "comments": self._items(connection, run_id)}

    def list_items(self, run_id):
        with self._connection() as connection:
            return self._items(connection, run_id)

    def get_item(self, run_id, comment_id):
        with self._connection() as connection:
            return self._item(connection, run_id, comment_id)

    def prepare(self, run_id, comment_id, text, decision=None, context=None):
        _text(text, "reply", 200)
        decision_json = _validate_evidence({} if decision is None else decision)
        context_json = _validate_evidence({} if context is None else context)
        if decision is not None and decision.get("action", "reply") != "reply":
            raise ValueError("only reply decisions can be prepared")
        now = self._now()
        with self._transaction() as connection:
            item = self._item(connection, run_id, comment_id)
            if item["attempted_at"] is not None:
                raise OwnedVideoReplyStateError("DUPLICATE_SUBMISSION_BLOCKED")
            if item["state"] == "PREPARED":
                if item["reply"] != text or item["decision"] != json.loads(decision_json) \
                        or item["context"] != json.loads(context_json):
                    raise OwnedVideoReplyStateError("PREPARED_PAYLOAD_CONFLICT")
                return item
            if item["state"] != "NEW":
                raise OwnedVideoReplyStateError("COMMENT_NOT_NEW")
            connection.execute("""
                UPDATE owned_video_comment_guards SET state = 'PREPARED', reply = ?,
                    decision_json = ?, context_json = ?, prepared_run_id = ?, updated_at = ?
                WHERE account_id = ? AND video_id = ? AND comment_id = ?
            """, (text, decision_json, context_json, run_id, now,
                  item["account_id"], item["video_id"], comment_id))
            return self._item(connection, run_id, comment_id)

    def mark_attempted(self, run_id, comment_id):
        """Commit the shared UNKNOWN guard before the one external send action."""
        now = self._now()
        with self._transaction() as connection:
            item = self._item(connection, run_id, comment_id)
            if item["attempted_at"] is not None or item["state"] != "PREPARED":
                raise OwnedVideoReplyStateError("DUPLICATE_SUBMISSION_BLOCKED")
            connection.execute("""
                UPDATE owned_video_comment_guards SET state = 'UNKNOWN_RESULT',
                    attempted_at = ?, attempted_run_id = ?, updated_at = ?
                WHERE account_id = ? AND video_id = ? AND comment_id = ?
            """, (now, run_id, now, item["account_id"], item["video_id"], comment_id))
            return self._item(connection, run_id, comment_id)

    def finish(self, run_id, comment_id, state, evidence):
        if state not in {"SKIP", "HANDOFF", "UNKNOWN_RESULT", "VERIFIED"}:
            raise ValueError("unsupported owned-video result state")
        evidence_json = _validate_evidence(evidence)
        now = self._now()
        with self._transaction() as connection:
            item = self._item(connection, run_id, comment_id)
            if item["state"] in {"SKIP", "HANDOFF", "VERIFIED"} and state != item["state"]:
                raise OwnedVideoReplyStateError("TERMINAL_COMMENT_STATE")
            attempted = item["attempted_at"] is not None
            if state in {"VERIFIED", "UNKNOWN_RESULT"} and not attempted:
                raise OwnedVideoReplyStateError("COMMENT_NOT_ATTEMPTED")
            if state in {"SKIP", "HANDOFF"} and attempted:
                raise OwnedVideoReplyStateError("COMMENT_ALREADY_ATTEMPTED")
            error = evidence.get("error")
            if not isinstance(error, str):
                error = None
            connection.execute("""
                UPDATE owned_video_comment_guards SET state = ?, evidence_json = ?,
                    error = ?, updated_at = ?
                WHERE account_id = ? AND video_id = ? AND comment_id = ?
            """, (state, evidence_json, error, now, item["account_id"], item["video_id"], comment_id))
            return self._item(connection, run_id, comment_id)

    def skip(self, run_id, comment_id, reason="not_eligible", evidence=None):
        _text(reason, "reason", 1000)
        details = {} if evidence is None else evidence
        _validate_evidence(details)
        return self.finish(run_id, comment_id, "SKIP", {**details, "reason": reason})
