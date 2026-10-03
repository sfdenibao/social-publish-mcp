"""Persistent, at-most-once submission guard for one comment on one work.

Call ``mark_attempted`` immediately before the external submit click, and click
only if it returns successfully. Its committed UNKNOWN_RESULT survives a crash:
an uncertain attempt is never made retryable, even with a new idempotency key.
This module does not read account configuration or operate a browser.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4


class IdempotencyConflictError(ValueError):
    """An existing idempotency key was supplied with a different payload."""


class CommentStateError(RuntimeError):
    """The requested transition could allow a duplicate or contradict history."""


_RESULT_STATES = frozenset(
    {"VERIFIED", "UNKNOWN_RESULT", "BLOCKED", "FAILED", "ALREADY_PRESENT"}
)
_TERMINAL_STATES = frozenset({"VERIFIED", "ALREADY_PRESENT"})
_SECRET_KEYS = frozenset(
    {
        "authorization", "proxyauthorization", "cookie", "cookies", "setcookie",
        "password", "passwd", "pwd", "secret", "clientsecret", "credential",
        "credentials", "token", "accesstoken", "refreshtoken", "idtoken",
        "apikey", "privatekey", "sessionid", "sessionkey", "csrftoken",
        "xcsrftoken", "storagestate", "localstorage", "sessionstorage",
    }
)
_SECRET_VALUE = re.compile(
    r"(?:\bBearer\s+\S+|\bBasic\s+[A-Za-z0-9+/=]{8,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b)",
    re.IGNORECASE,
)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _validate_string(value, name, max_length=None):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{name} must contain 1..{max_length} characters")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
           for char in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


def _is_secret_key(key):
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in _SECRET_KEYS or normalized.endswith(
        ("accesstoken", "refreshtoken", "clientsecret", "apikey", "password")
    )


def _validate_evidence(evidence):
    """Accept JSON objects, rejecting common credential fields/signatures.

    Callers must supply only public DOM observations, URLs, and evidence paths;
    arbitrary opaque secrets cannot be identified by their value alone.
    """
    if not isinstance(evidence, dict):
        raise ValueError("evidence must be a JSON object")

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ValueError("evidence object keys must be strings")
                if _is_secret_key(key):
                    raise ValueError("evidence must not contain credentials")
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            if _SECRET_VALUE.search(value):
                raise ValueError("evidence must not contain credentials")
            if value.startswith(("http://", "https://")):
                parsed = urlsplit(value)
                if parsed.username is not None or parsed.password is not None:
                    raise ValueError("evidence URLs must not contain credentials")
                if any(_is_secret_key(key) for key, _ in parse_qsl(parsed.query)):
                    raise ValueError("evidence URLs must not contain credentials")
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise ValueError("evidence must contain only JSON values")

    try:
        visit(evidence)
        return json.dumps(evidence, ensure_ascii=False, allow_nan=False)
    except (TypeError, RecursionError) as exc:
        raise ValueError("evidence must contain only finite, acyclic JSON values") from exc


class CommentLedger:
    """A standalone SQLite ledger; no production publishing tables are used.

    All returned task dictionaries contain ``reused``. It is True only when
    ``prepare`` returns an existing task; other operations return False.
    Unknown task ids raise KeyError. Invalid inputs raise ValueError; forbidden
    state transitions raise CommentStateError (a RuntimeError).
    """

    def __init__(self, path):
        if str(path) == ":memory:":
            raise ValueError("CommentLedger requires a persistent database path")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS comment_tasks (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    work_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN (
                        'PREPARED', 'VERIFIED', 'UNKNOWN_RESULT', 'BLOCKED',
                        'FAILED', 'ALREADY_PRESENT'
                    )),
                    created_at TEXT NOT NULL,
                    attempted_at TEXT,
                    evidence TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    UNIQUE (account_id, work_id, text)
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS comment_idempotency_keys (
                    idempotency_key TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES comment_tasks(id)
                )
            """)

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
    def _task(connection, comment_task_id, reused=False):
        row = connection.execute(
            "SELECT * FROM comment_tasks WHERE id = ?", (comment_task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(comment_task_id)
        task = dict(row)
        task["evidence"] = json.loads(task["evidence"])
        task["reused"] = reused
        return task

    def prepare(self, account_id, work_id, text, idempotency_key):
        """Create or reuse a task; every supplied key remains bound to it."""
        _validate_string(account_id, "account_id")
        _validate_string(work_id, "work_id")
        _validate_string(text, "text", max_length=200)
        _validate_string(idempotency_key, "idempotency_key")
        payload = (account_id, work_id, text)
        with self._transaction() as connection:
            keyed = connection.execute("""
                SELECT task_id FROM comment_idempotency_keys
                WHERE idempotency_key = ?
            """, (idempotency_key,)).fetchone()
            if keyed is not None:
                task = self._task(connection, keyed["task_id"], reused=True)
                if (task["account_id"], task["work_id"], task["text"]) != payload:
                    raise IdempotencyConflictError("IDEMPOTENCY_CONFLICT")
                return task

            existing = connection.execute("""
                SELECT id FROM comment_tasks
                WHERE account_id = ? AND work_id = ? AND text = ?
            """, payload).fetchone()
            reused = existing is not None
            task_id = existing["id"] if reused else uuid4().hex
            if not reused:
                connection.execute("""
                    INSERT INTO comment_tasks
                        (id, account_id, work_id, text, state, created_at)
                    VALUES (?, ?, ?, ?, 'PREPARED', ?)
                """, (task_id, *payload, _now()))
            connection.execute("""
                INSERT INTO comment_idempotency_keys (idempotency_key, task_id)
                VALUES (?, ?)
            """, (idempotency_key, task_id))
            return self._task(connection, task_id, reused=reused)

    def get(self, comment_task_id):
        with self._connection() as connection:
            return self._task(connection, comment_task_id)

    def mark_attempted(self, comment_task_id):
        """Commit the submission guard BEFORE clicking; never retry a click."""
        with self._transaction() as connection:
            task = self._task(connection, comment_task_id)
            if task["attempted_at"] is not None or task["state"] not in {
                "PREPARED", "FAILED"
            }:
                raise CommentStateError("DUPLICATE_SUBMISSION_BLOCKED")
            connection.execute("""
                UPDATE comment_tasks
                SET state = 'UNKNOWN_RESULT', attempted_at = ?, error = NULL
                WHERE id = ? AND attempted_at IS NULL
                    AND state IN ('PREPARED', 'FAILED')
            """, (_now(), comment_task_id))
            return self._task(connection, comment_task_id)

    def record(self, comment_task_id, state, evidence):
        """Record observed results without ever reopening the submission guard.

        Only ALREADY_PRESENT can be recorded before an attempt. VERIFIED and
        ALREADY_PRESENT are terminal; repeating the same terminal state may
        refresh its evidence, but it cannot be changed to a different state.
        """
        if not isinstance(state, str) or state not in _RESULT_STATES:
            raise ValueError("unsupported comment result state")
        encoded = _validate_evidence(evidence)
        with self._transaction() as connection:
            task = self._task(connection, comment_task_id)
            if task["attempted_at"] is None and state != "ALREADY_PRESENT":
                raise CommentStateError("COMMENT_NOT_ATTEMPTED")
            if task["state"] in _TERMINAL_STATES and task["state"] != state:
                raise CommentStateError("TERMINAL_COMMENT_STATE")
            error = evidence.get("error")
            if not isinstance(error, str):
                error = None
            connection.execute("""
                UPDATE comment_tasks SET state = ?, evidence = ?, error = ?
                WHERE id = ?
            """, (state, encoded, error, comment_task_id))
            return self._task(connection, comment_task_id)

    def fail_before_attempt(self, comment_task_id, error):
        """A proven pre-click failure is retryable; attempted tasks never are."""
        _validate_string(error, "error")
        _validate_evidence({"error": error})
        with self._transaction() as connection:
            task = self._task(connection, comment_task_id)
            if task["attempted_at"] is not None or task["state"] not in {
                "PREPARED", "FAILED"
            }:
                raise CommentStateError("PRE_ATTEMPT_FAILURE_NOT_ALLOWED")
            connection.execute("""
                UPDATE comment_tasks SET state = 'FAILED', error = ? WHERE id = ?
            """, (error, comment_task_id))
            return self._task(connection, comment_task_id)
