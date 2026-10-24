"""The agent-session store: the sole owner of agent-loop metadata (AGENT-LOOP-1).

The bounded agent loop needs durable, honest state of its own: which project a
session belongs to, the goal, each turn's identity and chosen action, the
retrieved observations a turn carried forward, and the persisted budget that
survives a reopen. None of that belongs in the orchestration store, whose schema
has no migration path and whose records are authoritative for the scan workflow.

So this module owns a **separate namespace** — ``<app-data>/agent/agent.db`` —
with its own schema version and its own refusal behavior. It references
orchestration records (a plan, a job, a run, an evidence row) **by exact,
project-bound id only**; it copies no authoritative record and is never a second
owner of the scan workflow.

Durability is implemented, not declared:

* a turn's intent is written **before** any adapter invocation or effect, and its
  terminal result is written **before** the response is returned;
* idempotency is key plus canonical payload digest; an identical replay returns
  the stored result with no new call and no new effect, a changed payload is a
  conflict, and a concurrent duplicate cannot claim the same turn twice;
* an attempt recorded as started but never finished stays ``unknown`` — it is
  never retried and never rewritten into a success.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..core.identity import sha256_hex
from . import budget as budget_mod

#: The busy wait a caller that sets no deadline keeps — the value this store
#: has always used.
DEFAULT_LOCK_WAIT_SECONDS = budget_mod.STORE_WAIT_CAP_SECONDS

#: The agent-session schema version, owned independently of every other store.
AGENT_SCHEMA_VERSION = "1.0.0"

AGENT_DIR_NAME = "agent"
DATABASE_FILENAME = "agent.db"

#: Bounded reasons a caller can act on. Never interpolate caller text.
REASON_SCHEMA_NEWER = "agent schema is newer than supported"
REASON_SCHEMA_MALFORMED = "agent schema is malformed"
REASON_STORE_UNAVAILABLE = "agent store is unavailable"
REASON_STORE_LOCATION = "the agent store may not live inside the project"
REASON_CONFLICT = "the same idempotency key was used with a different payload"

#: A turn that was durably started but whose terminal result was never written.
TURN_RUNNING = "running"
TURN_SETTLED = "settled"
TURN_UNKNOWN = "unknown"


class StoreError(Exception):
    """A bounded store failure. ``reason`` is safe to surface verbatim."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_CREATE_STATEMENTS: Tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS session (
        project_id TEXT PRIMARY KEY,
        goal       TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS turn (
        project_id      TEXT NOT NULL,
        turn_id         TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        state           TEXT NOT NULL,
        choice          TEXT NOT NULL,
        payload         TEXT NOT NULL,
        PRIMARY KEY (project_id, turn_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS step (
        project_id TEXT NOT NULL,
        step_id    TEXT NOT NULL,
        turn_id    TEXT NOT NULL,
        ordinal    INTEGER NOT NULL,
        kind       TEXT NOT NULL,
        payload    TEXT NOT NULL,
        PRIMARY KEY (project_id, step_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS idempotency (
        project_id     TEXT NOT NULL,
        key            TEXT NOT NULL,
        payload_digest TEXT NOT NULL,
        result         TEXT NOT NULL,
        created_at     TEXT NOT NULL,
        PRIMARY KEY (project_id, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS budget (
        project_id  TEXT PRIMARY KEY,
        model_calls INTEGER NOT NULL,
        steps       INTEGER NOT NULL,
        reads       INTEGER NOT NULL
    )
    """,
)


def canonical_bytes(payload: Any) -> bytes:
    """Return the canonical JSON encoding used for every digest here."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def payload_digest(payload: Any) -> str:
    """Return the SHA-256 of the canonical encoding of ``payload``."""
    return sha256_hex(canonical_bytes(payload))


def new_id(prefix: str) -> str:
    """Return a stable opaque identity with a readable prefix."""
    return f"{prefix}:{uuid.uuid4().hex}"


def store_dir(base_dir: str) -> str:
    """Return the agent namespace under an app-data base."""
    return os.path.join(base_dir, AGENT_DIR_NAME)


def database_path(base_dir: str) -> str:
    """Return the agent database path under an app-data base."""
    return os.path.join(store_dir(base_dir), DATABASE_FILENAME)


def assert_outside_project(base_dir: str, root: Optional[str]) -> None:
    """Refuse an agent store that would live inside the selected source."""
    if not root:
        return
    base = os.path.realpath(os.path.abspath(base_dir))
    project = os.path.realpath(os.path.abspath(root))
    if base == project or base.startswith(project + os.sep):
        raise StoreError(REASON_STORE_LOCATION)


def _rollback(connection: sqlite3.Connection) -> None:
    """Roll back if a transaction is still open.

    A contended ``BEGIN IMMEDIATE`` fails before a transaction exists, so the
    ``ROLLBACK`` a handler runs would itself raise — turning a bounded store
    fault into an unhandled error. Undoing nothing is not an error.
    """
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


class AgentSessionStore:
    """A thin, transactional owner of the agent namespace."""

    def __init__(self, base_dir: str, deadline: Optional[float] = None) -> None:
        self.base_dir = base_dir
        self._db_path = database_path(base_dir)
        #: Optional monotonic deadline. When set, every connection's SQLite busy
        #: wait is the *remaining* allowance rather than the store default, and
        #: it shrinks as the turn spends time — successive operations share one
        #: budget instead of each getting a fresh full wait.
        self.deadline = deadline

    def set_deadline(self, deadline: Optional[float]) -> None:
        """Bind this store to the turn's single monotonic deadline."""
        self.deadline = deadline

    def _lock_wait(self) -> float:
        """Return the seconds this connection may wait on a lock."""
        if self.deadline is None:
            return DEFAULT_LOCK_WAIT_SECONDS
        return budget_mod.store_wait_allowance(self.deadline, time.monotonic())

    def _refresh_lock_wait(self, connection: sqlite3.Connection) -> float:
        """Re-apply the remaining allowance to an open connection's busy wait.

        The SQLite busy timeout is a connection setting fixed at ``connect``, so
        it is only *current* at connect time. Re-issuing it at a blocking-phase
        boundary makes the phase that actually blocks — the terminal commit,
        which is the one that follows all of a turn's work — wait on what is
        left now instead of on what was left when the connection opened.

        It never grants a fresh allowance: the value is the same remaining-time
        function, evaluated again. ``0`` is a real answer and disables the busy
        handler, so a turn with nothing left fails immediately rather than
        blocking past its deadline.
        """
        allowance = self._lock_wait()
        try:
            connection.execute("PRAGMA busy_timeout = %d" % int(allowance * 1000.0))
        except sqlite3.Error:
            # A connection that cannot take the pragma keeps the timeout it was
            # opened with; that is a bounded wait too, so this is not a fault.
            pass
        return allowance

    # -- connection ---------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        try:
            os.makedirs(store_dir(self.base_dir), exist_ok=True)
            connection = sqlite3.connect(
                self._db_path, timeout=self._lock_wait(), isolation_level=None
            )
        except (OSError, sqlite3.Error) as error:
            raise StoreError(REASON_STORE_UNAVAILABLE) from error
        connection.row_factory = sqlite3.Row
        return connection

    def ensure_schema(self) -> None:
        """Create the schema, or verify the version already recorded."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                for statement in _CREATE_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                    (AGENT_SCHEMA_VERSION,),
                )
                connection.execute("COMMIT")
                return
            found = str(row["value"])
            if not found or found != AGENT_SCHEMA_VERSION:
                raise StoreError(
                    REASON_SCHEMA_NEWER
                    if _newer(found)
                    else REASON_SCHEMA_MALFORMED
                )
            connection.execute("COMMIT")
        except StoreError:
            _rollback(connection)
            raise
        except sqlite3.Error as error:
            _rollback(connection)
            raise StoreError(REASON_STORE_UNAVAILABLE) from error
        finally:
            connection.close()

    # -- session ------------------------------------------------------------
    def ensure_session(self, project_id: str, goal: str, created_at: str) -> None:
        """Record the session's goal once; a repeat is a no-op."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO session (project_id, goal, created_at) VALUES (?, ?, ?)",
                (project_id, goal, created_at),
            )
            connection.execute(
                "INSERT OR IGNORE INTO budget (project_id, model_calls, steps, reads) "
                "VALUES (?, 0, 0, 0)",
                (project_id,),
            )
            connection.execute("COMMIT")
        except sqlite3.Error as error:
            _rollback(connection)
            raise StoreError(REASON_STORE_UNAVAILABLE) from error
        finally:
            connection.close()

    # -- idempotency --------------------------------------------------------
    def claim(
        self, project_id: str, key: str, digest: str, created_at: str
    ) -> Tuple[str, Optional[str]]:
        """Reserve ``key`` for a turn, or report what already holds it.

        Returns ``("claimed", None)`` when this caller may proceed,
        ``("replay", stored_result_json)`` when an identical request already
        settled, and ``("pending", None)`` when an identical request is durably
        in flight (never claimed twice). A different payload for a used key is a
        conflict.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_digest, result FROM idempotency WHERE project_id = ? AND key = ?",
                (project_id, key),
            ).fetchone()
            if row is not None:
                if str(row["payload_digest"]) != digest:
                    # Raise with the transaction still open so the handler's
                    # rollback is valid; committing first would make ROLLBACK
                    # itself throw and turn a bounded conflict into a fault.
                    raise StoreError(REASON_CONFLICT)
                connection.execute("COMMIT")
                result = str(row["result"])
                return ("replay", result) if result else ("pending", None)
            connection.execute(
                "INSERT INTO idempotency (project_id, key, payload_digest, result, created_at) "
                "VALUES (?, ?, ?, '', ?)",
                (project_id, key, digest, created_at),
            )
            connection.execute("COMMIT")
            return "claimed", None
        except StoreError:
            _rollback(connection)
            raise
        except sqlite3.Error as error:
            _rollback(connection)
            raise StoreError(REASON_STORE_UNAVAILABLE) from error
        finally:
            connection.close()

    # -- turns and steps ----------------------------------------------------
    def open_turn(
        self, project_id: str, turn_id: str, key: str, payload: Mapping[str, Any]
    ) -> None:
        """Durably record that a turn has started, before any effect."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO turn (project_id, turn_id, idempotency_key, state, choice, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, turn_id, key, TURN_RUNNING, "", _dumps(payload)),
            )
            connection.execute("COMMIT")
        except sqlite3.Error as error:
            _rollback(connection)
            raise StoreError(REASON_STORE_UNAVAILABLE) from error
        finally:
            connection.close()

    def close_turn(
        self,
        project_id: str,
        turn_id: str,
        *,
        choice: str,
        payload: Mapping[str, Any],
        steps: List[Mapping[str, Any]],
        budget_delta: Mapping[str, int],
        idempotency_key: str,
        result_json: str,
    ) -> None:
        """Commit a turn's terminal state, its steps, its budget AND its
        idempotency result in **one** transaction.

        The last two are the point: a response can be lost after the work is
        done, and the durable fact must not be. Committing the result beside the
        turn means a replay of the same key returns the recorded success without
        re-running anything, while a turn whose terminal commit never landed
        stays ``running`` and reads ``unknown``.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE turn SET state = ?, choice = ?, payload = ? "
                "WHERE project_id = ? AND turn_id = ?",
                (TURN_SETTLED, choice, _dumps(payload), project_id, turn_id),
            )
            for ordinal, step in enumerate(steps):
                connection.execute(
                    "INSERT INTO step (project_id, step_id, turn_id, ordinal, kind, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        project_id,
                        new_id("step"),
                        turn_id,
                        ordinal,
                        str(step.get("kind") or ""),
                        _dumps(step),
                    ),
                )
            connection.execute(
                "UPDATE budget SET model_calls = model_calls + ?, steps = steps + ?, "
                "reads = reads + ? WHERE project_id = ?",
                (
                    int(budget_delta.get("model_calls", 0)),
                    int(budget_delta.get("steps", 0)),
                    int(budget_delta.get("reads", 0)),
                    project_id,
                ),
            )
            connection.execute(
                "UPDATE idempotency SET result = ? WHERE project_id = ? AND key = ?",
                (result_json, project_id, idempotency_key),
            )
            # The terminal phase is the only one that follows all of the turn's
            # work, so its busy wait is re-derived here rather than inherited
            # from the connect that preceded BEGIN.
            self._refresh_lock_wait(connection)
            connection.execute("COMMIT")
        except sqlite3.Error as error:
            _rollback(connection)
            raise StoreError(REASON_STORE_UNAVAILABLE) from error
        finally:
            connection.close()

    # -- reads --------------------------------------------------------------
    def read_state(self, project_id: str) -> Dict[str, Any]:
        """Return the project's session state, turns, steps and budget.

        A turn left ``running`` is reported ``unknown``: read never rewrites it
        and never resumes it.
        """
        self.ensure_schema()
        connection = self._connect()
        try:
            session = connection.execute(
                "SELECT goal, created_at FROM session WHERE project_id = ?", (project_id,)
            ).fetchone()
            budget = connection.execute(
                "SELECT model_calls, steps, reads FROM budget WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            turns = connection.execute(
                "SELECT turn_id, idempotency_key, state, choice, payload FROM turn "
                "WHERE project_id = ? ORDER BY rowid",
                (project_id,),
            ).fetchall()
            steps = connection.execute(
                "SELECT turn_id, ordinal, kind, payload FROM step WHERE project_id = ? "
                "ORDER BY turn_id, ordinal",
                (project_id,),
            ).fetchall()
        finally:
            connection.close()

        steps_by_turn: Dict[str, List[Dict[str, Any]]] = {}
        for row in steps:
            steps_by_turn.setdefault(str(row["turn_id"]), []).append(
                {"ordinal": int(row["ordinal"]), "kind": str(row["kind"]), **_loads(row["payload"])}
            )
        return {
            "has_session": session is not None,
            "goal": str(session["goal"]) if session is not None else "",
            "budget": {
                "model_calls": int(budget["model_calls"]) if budget else 0,
                "steps": int(budget["steps"]) if budget else 0,
                "reads": int(budget["reads"]) if budget else 0,
            },
            "turns": [
                {
                    "turn_id": str(row["turn_id"]),
                    "state": TURN_UNKNOWN if str(row["state"]) == TURN_RUNNING else str(row["state"]),
                    # The bounded outcome a caller cares about: the model's
                    # recorded turn state, or `unknown` for a turn whose
                    # terminal result was never written.
                    "outcome": (
                        TURN_UNKNOWN
                        if str(row["state"]) == TURN_RUNNING
                        else str(_loads(row["payload"]).get("state") or "")
                    ),
                    "choice": str(row["choice"]),
                    "payload": _loads(row["payload"]),
                    "steps": steps_by_turn.get(str(row["turn_id"]), []),
                }
                for row in turns
            ],
        }


def _newer(found: str) -> bool:
    try:
        return tuple(int(part) for part in found.split(".")) > tuple(
            int(part) for part in AGENT_SCHEMA_VERSION.split(".")
        )
    except (ValueError, TypeError):
        return False


def _dumps(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _loads(text: Any) -> Dict[str, Any]:
    try:
        value = json.loads(str(text))
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


__all__ = [
    "AGENT_SCHEMA_VERSION",
    "AGENT_DIR_NAME",
    "DATABASE_FILENAME",
    "REASON_SCHEMA_NEWER",
    "REASON_SCHEMA_MALFORMED",
    "REASON_STORE_UNAVAILABLE",
    "REASON_STORE_LOCATION",
    "REASON_CONFLICT",
    "TURN_RUNNING",
    "TURN_SETTLED",
    "TURN_UNKNOWN",
    "StoreError",
    "AgentSessionStore",
    "canonical_bytes",
    "payload_digest",
    "new_id",
    "store_dir",
    "database_path",
    "assert_outside_project",
]
