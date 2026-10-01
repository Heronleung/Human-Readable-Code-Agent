"""The sole owner of the orchestration store.

A dedicated SQLite database under the backend-owned app-data namespace, in its
own ``orchestration/`` directory beside — never inside — any selected
repository. Nothing else in the package opens it, and the desktop never reaches
it: the only path in is through the boundary.

What the store is responsible for, and why it is a transaction and not a
convention:

* **One active execution per project.** A partial unique index on
  ``agent_run(project_id) WHERE outcome = 'running'`` makes a second concurrent
  claim fail in the database rather than in a race between two readers.
* **Idempotency bound to the payload.** ``idempotency(project_id, key)`` stores
  the digest of the payload that produced the result. The same key with the
  same payload returns the stored result; the same key with a different payload
  is a conflict. The correlation id is not used for this — it matches a
  response to a request, and a retried request has a new one.
* **Terminal facts published together.** A run's outcome and its evidence rows
  are written in one transaction, and the bounded artifact is renamed into
  place *before* that transaction commits, so a committed evidence row can
  never point at a partial artifact.
* **No destructive migration.** An unreadable, malformed or newer schema is
  refused with a bounded reason and the existing data is left untouched.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import uuid
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import domain

#: The orchestration schema version, owned independently of every other store.
ORCH_SCHEMA_VERSION = "1.0.0"

ORCH_DIR_NAME = "orchestration"
DATABASE_FILENAME = "orchestration.db"
EVIDENCE_DIR_NAME = "evidence"

_TMP_PREFIX = ".orch-"
_TMP_SUFFIX = ".tmp"

#: Bounded reasons a caller can act on. Never interpolate caller text.
REASON_SCHEMA_NEWER = "orchestration schema is newer than supported"
REASON_SCHEMA_MALFORMED = "orchestration schema is malformed"
REASON_STORE_UNAVAILABLE = "orchestration store is unavailable"
REASON_STORE_CORRUPT = "orchestration store record is corrupt"
REASON_STORE_LOCATION = "the orchestration store may not live inside the project"
REASON_CONFLICT = "the same idempotency key was used with a different payload"
REASON_ACTIVE_RUN = "an execution is already active for this project"


class StoreError(Exception):
    """A bounded store failure. ``reason`` is safe to surface verbatim."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


#: The schema, one statement per entry. Executed with ``connection.execute``
#: inside an explicit transaction rather than with ``executescript``, because
#: ``executescript`` commits any open transaction before it runs.
_CREATE_STATEMENTS: Tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS plan_revision (
        project_id  TEXT NOT NULL,
        plan_id     TEXT NOT NULL,
        revision    INTEGER NOT NULL,
        digest      TEXT NOT NULL,
        phase       TEXT NOT NULL,
        payload     TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (project_id, plan_id, revision)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job (
        project_id    TEXT NOT NULL,
        job_id        TEXT NOT NULL,
        plan_id       TEXT NOT NULL,
        plan_revision INTEGER NOT NULL,
        state         TEXT NOT NULL,
        payload       TEXT NOT NULL,
        PRIMARY KEY (project_id, job_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_run (
        project_id      TEXT NOT NULL,
        run_id          TEXT NOT NULL,
        job_id          TEXT NOT NULL,
        attempt         INTEGER NOT NULL,
        outcome         TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        manifest_id     TEXT NOT NULL,
        payload         TEXT NOT NULL,
        started_at      TEXT NOT NULL,
        ended_at        TEXT,
        PRIMARY KEY (project_id, run_id)
    )
    """,
    # One active execution per project, enforced by the database.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS agent_run_one_active
        ON agent_run (project_id) WHERE outcome = 'running'
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence (
        project_id   TEXT NOT NULL,
        evidence_id  TEXT NOT NULL,
        run_id       TEXT NOT NULL,
        plan_id      TEXT NOT NULL,
        job_id       TEXT NOT NULL,
        predicate_id TEXT NOT NULL,
        result       TEXT NOT NULL,
        payload      TEXT NOT NULL,
        PRIMARY KEY (project_id, evidence_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decision (
        project_id      TEXT NOT NULL,
        decision_id     TEXT NOT NULL,
        kind            TEXT NOT NULL,
        outcome         TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        payload         TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        PRIMARY KEY (project_id, decision_id)
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
)


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------
def store_dir(base_dir: str) -> str:
    """Return the orchestration namespace under an app-data base."""
    return os.path.join(base_dir, ORCH_DIR_NAME)


def database_path(base_dir: str) -> str:
    """Return the orchestration database path under an app-data base."""
    return os.path.join(store_dir(base_dir), DATABASE_FILENAME)


def evidence_dir(base_dir: str) -> str:
    """Return the bounded-artifact directory under the same namespace."""
    return os.path.join(store_dir(base_dir), EVIDENCE_DIR_NAME)


def assert_outside_project(base_dir: str, root: Optional[str]) -> None:
    """Refuse a store that would live inside the selected source or its Git metadata.

    The slice may write app-owned metadata; it may never write near the source
    it is reading, and a store inside the repository would also be picked up by
    the next scan as project content.
    """
    if not root:
        return
    base = os.path.realpath(os.path.abspath(base_dir))
    project = os.path.realpath(os.path.abspath(root))
    if base == project or base.startswith(project + os.sep):
        raise StoreError(REASON_STORE_LOCATION)


def new_id(prefix: str) -> str:
    """Return a stable opaque identity with a readable prefix."""
    return f"{prefix}:{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
class OrchestrationStore:
    """A thin, transactional owner of the orchestration namespace."""

    def __init__(self, base_dir: str) -> None:
        self.base_dir = base_dir
        self._db_path = database_path(base_dir)

    # -- connection ---------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        try:
            os.makedirs(store_dir(self.base_dir), exist_ok=True)
            connection = sqlite3.connect(self._db_path, timeout=10.0, isolation_level=None)
        except (OSError, sqlite3.Error) as error:
            raise StoreError(REASON_STORE_UNAVAILABLE) from error
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def ensure_schema(self) -> None:
        """Create the schema, or verify the version that is already there."""
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
                    (ORCH_SCHEMA_VERSION,),
                )
                connection.execute("COMMIT")
                return
            found = str(row["value"])
            if not found:
                raise StoreError(REASON_SCHEMA_MALFORMED)
            if _version_tuple(found) > _version_tuple(ORCH_SCHEMA_VERSION):
                raise StoreError(REASON_SCHEMA_NEWER)
            if _version_tuple(found) < _version_tuple(ORCH_SCHEMA_VERSION):
                raise StoreError(REASON_SCHEMA_MALFORMED)
            connection.execute("COMMIT")
        except StoreError:
            connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as error:
            connection.execute("ROLLBACK")
            raise StoreError(REASON_STORE_UNAVAILABLE) from error
        finally:
            connection.close()

    # -- idempotency --------------------------------------------------------
    def _replay(
        self, connection: sqlite3.Connection, project_id: str, key: str, payload_digest: str
    ) -> Optional[Any]:
        row = connection.execute(
            "SELECT payload_digest, result FROM idempotency WHERE project_id = ? AND key = ?",
            (project_id, key),
        ).fetchone()
        if row is None:
            return None
        if str(row["payload_digest"]) != payload_digest:
            raise StoreError(REASON_CONFLICT)
        return json.loads(row["result"])

    @staticmethod
    def _remember(
        connection: sqlite3.Connection,
        project_id: str,
        key: str,
        payload_digest: str,
        result: Any,
        created_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO idempotency (project_id, key, payload_digest, result, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                project_id,
                key,
                payload_digest,
                json.dumps(result, sort_keys=True, ensure_ascii=True),
                created_at,
            ),
        )

    # -- writes -------------------------------------------------------------
    def save_revision(
        self,
        revision: domain.PlanRevision,
        *,
        expected_revision: Optional[int],
        idempotency_key: str,
        now: str,
    ) -> Dict[str, Any]:
        """Append a plan revision under optimistic concurrency.

        ``expected_revision`` is the revision the caller believes is current:
        ``0`` means "no revision yet". A mismatch is a conflict, so an edit
        written against a stale read can never overwrite a newer one.
        """
        payload = revision.as_payload()
        payload_digest = domain.digest({"op": "save_revision", "payload": payload})
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replayed = self._replay(connection, revision.project_id, idempotency_key, payload_digest)
            if replayed is not None:
                connection.execute("COMMIT")
                return replayed

            row = connection.execute(
                "SELECT MAX(revision) AS latest FROM plan_revision "
                "WHERE project_id = ? AND plan_id = ?",
                (revision.project_id, revision.plan_id),
            ).fetchone()
            latest = int(row["latest"]) if row and row["latest"] is not None else 0
            if latest != int(expected_revision or 0):
                raise StoreError(REASON_CONFLICT)
            if revision.revision != latest + 1:
                raise StoreError(REASON_CONFLICT)

            stored = revision.with_digest()
            connection.execute(
                "INSERT INTO plan_revision "
                "(project_id, plan_id, revision, digest, phase, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    stored.project_id,
                    stored.plan_id,
                    stored.revision,
                    stored.digest,
                    stored.phase,
                    json.dumps(stored.as_payload(), sort_keys=True, ensure_ascii=True),
                    now,
                ),
            )
            result = {"revision": stored.revision, "digest": stored.digest, "phase": stored.phase}
            self._remember(
                connection, stored.project_id, idempotency_key, payload_digest, result, now
            )
            connection.execute("COMMIT")
            return result
        except StoreError:
            connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as error:
            connection.execute("ROLLBACK")
            raise StoreError(REASON_STORE_UNAVAILABLE) from error
        finally:
            connection.close()

    def list_revisions(self, project_id: str, plan_id: str) -> List[domain.PlanRevision]:
        """Return every stored revision of a plan, oldest first."""
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT payload, phase FROM plan_revision "
                "WHERE project_id = ? AND plan_id = ? ORDER BY revision",
                (project_id, plan_id),
            ).fetchall()
        except sqlite3.Error as error:
            raise StoreError(REASON_STORE_UNAVAILABLE) from error
        finally:
            connection.close()
        return [_revision_from_row(row) for row in rows]

    def latest_revision(self, project_id: str) -> Optional[domain.PlanRevision]:
        """Return the newest revision for a project, or ``None``."""
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT payload, phase FROM plan_revision WHERE project_id = ? "
                "ORDER BY plan_id DESC, revision DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        except sqlite3.Error as error:
            raise StoreError(REASON_STORE_UNAVAILABLE) from error
        finally:
            connection.close()
        return _revision_from_row(row) if row else None

    def confirm_revision(
        self,
        revision: domain.PlanRevision,
        job: domain.JobRecord,
        *,
        expected_digest: str,
        idempotency_key: str,
        now: str,
        decision_id: str,
    ) -> Dict[str, Any]:
        """Confirm one exact revision and create its job, atomically.

        The stored digest must match the digest the developer confirmed, so a
        confirmation can never slide onto a revision edited in between.
        """
        payload_digest = domain.digest(
            {"op": "confirm", "plan_id": revision.plan_id, "digest": expected_digest}
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replayed = self._replay(connection, revision.project_id, idempotency_key, payload_digest)
            if replayed is not None:
                connection.execute("COMMIT")
                return replayed

            row = connection.execute(
                "SELECT digest, phase FROM plan_revision "
                "WHERE project_id = ? AND plan_id = ? AND revision = ?",
                (revision.project_id, revision.plan_id, revision.revision),
            ).fetchone()
            if row is None:
                raise StoreError(REASON_STORE_CORRUPT)
            if str(row["digest"]) != expected_digest:
                raise StoreError(REASON_CONFLICT)
            if str(row["phase"]) != domain.PLAN_DRAFT:
                raise StoreError(REASON_CONFLICT)

            database_payload = revision.with_phase(domain.PLAN_CONFIRMED).as_payload()
            connection.execute(
                "UPDATE plan_revision SET phase = ?, payload = ? "
                "WHERE project_id = ? AND plan_id = ? AND revision = ?",
                (
                    domain.PLAN_CONFIRMED,
                    json.dumps(
                        {**database_payload, "phase": domain.PLAN_CONFIRMED},
                        sort_keys=True,
                        ensure_ascii=True,
                    ),
                    revision.project_id,
                    revision.plan_id,
                    revision.revision,
                ),
            )
            connection.execute(
                "INSERT INTO job (project_id, job_id, plan_id, plan_revision, state, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    job.project_id,
                    job.job_id,
                    job.plan_id,
                    job.plan_revision,
                    job.state,
                    json.dumps(job.as_payload(), sort_keys=True, ensure_ascii=True),
                ),
            )
            decision = domain.DecisionRecord(
                decision_id=decision_id,
                project_id=revision.project_id,
                kind=domain.DECISION_PLAN_CONFIRMATION,
                outcome="confirmed",
                actor="local developer",
                target_digest=expected_digest,
                idempotency_key=idempotency_key,
                created_at=now,
            )
            connection.execute(
                "INSERT INTO decision "
                "(project_id, decision_id, kind, outcome, idempotency_key, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    decision.project_id,
                    decision.decision_id,
                    decision.kind,
                    decision.outcome,
                    decision.idempotency_key,
                    json.dumps(decision.as_payload(), sort_keys=True, ensure_ascii=True),
                    now,
                ),
            )
            result = {
                "revision": revision.revision,
                "digest": expected_digest,
                "job_id": job.job_id,
                "decision_id": decision_id,
            }
            self._remember(
                connection, revision.project_id, idempotency_key, payload_digest, result, now
            )
            connection.execute("COMMIT")
            return result
        except StoreError:
            connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as error:
            connection.execute("ROLLBACK")
            raise StoreError(REASON_STORE_UNAVAILABLE) from error
        finally:
            connection.close()

    def claim_run(
        self,
        run: domain.AgentRun,
        *,
        idempotency_key: str,
        now: str,
    ) -> Dict[str, Any]:
        """Atomically claim the one active execution for a project."""
        payload_digest = domain.digest(
            {
                "op": "claim_run",
                "job_id": run.job_id,
                "plan_id": run.plan_id,
                "revision": run.plan_revision,
                "manifest_id": run.manifest_id,
            }
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replayed = self._replay(connection, run.project_id, idempotency_key, payload_digest)
            if replayed is not None:
                connection.execute("COMMIT")
                return replayed

            active = connection.execute(
                "SELECT run_id FROM agent_run WHERE project_id = ? AND outcome = 'running'",
                (run.project_id,),
            ).fetchone()
            if active is not None:
                raise StoreError(REASON_ACTIVE_RUN)

            row = connection.execute(
                "SELECT MAX(attempt) AS latest FROM agent_run WHERE project_id = ? AND job_id = ?",
                (run.project_id, run.job_id),
            ).fetchone()
            attempt = (int(row["latest"]) if row and row["latest"] is not None else 0) + 1

            stored = domain.AgentRun(
                run_id=run.run_id,
                project_id=run.project_id,
                job_id=run.job_id,
                plan_id=run.plan_id,
                plan_revision=run.plan_revision,
                attempt=attempt,
                idempotency_key=idempotency_key,
                executor=run.executor,
                executor_version=run.executor_version,
                manifest_id=run.manifest_id,
                outcome=domain.RUN_RUNNING,
                started_at=run.started_at,
                # The claiming process, so recovery can establish that it is
                # gone before it touches an unfinished run.
                executor_pid=run.executor_pid,
            )
            connection.execute(
                "INSERT INTO agent_run "
                "(project_id, run_id, job_id, attempt, outcome, idempotency_key, manifest_id, "
                " payload, started_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    stored.project_id,
                    stored.run_id,
                    stored.job_id,
                    stored.attempt,
                    stored.outcome,
                    stored.idempotency_key,
                    stored.manifest_id,
                    json.dumps(stored.as_payload(), sort_keys=True, ensure_ascii=True),
                    stored.started_at,
                ),
            )
            connection.execute(
                "UPDATE job SET state = ? WHERE project_id = ? AND job_id = ?",
                (domain.JOB_RUNNING, stored.project_id, stored.job_id),
            )
            result = {"run_id": stored.run_id, "attempt": attempt, "outcome": domain.RUN_RUNNING}
            self._remember(
                connection, stored.project_id, idempotency_key, payload_digest, result, now
            )
            connection.execute("COMMIT")
            return result
        except StoreError:
            connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as error:
            connection.execute("ROLLBACK")
            raise StoreError(REASON_STORE_UNAVAILABLE) from error
        finally:
            connection.close()

    def publish_terminal(
        self,
        run_id: str,
        project_id: str,
        *,
        outcome: str,
        reason: str,
        evidence: Sequence[domain.EvidenceRecord],
        artifacts: Sequence[Tuple[str, bytes]],
        job_state: str,
        now: str,
    ) -> Dict[str, Any]:
        """Publish a terminal outcome and its evidence in one transaction.

        Artifacts are renamed into place first, so a committed evidence row
        always has a complete artifact behind it.
        """
        if outcome not in domain.TERMINAL_RUN_OUTCOMES:
            raise StoreError(REASON_STORE_CORRUPT)

        written: List[str] = []
        os.makedirs(evidence_dir(self.base_dir), exist_ok=True)
        for name, payload in artifacts:
            written.append(self._write_artifact(name, payload))

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload, outcome FROM agent_run WHERE project_id = ? AND run_id = ?",
                (project_id, run_id),
            ).fetchone()
            if row is None:
                raise StoreError(REASON_STORE_CORRUPT)
            if str(row["outcome"]) != domain.RUN_RUNNING:
                raise StoreError(REASON_CONFLICT)

            stored_run = json.loads(str(row["payload"]))
            stored_run.update({"outcome": outcome, "reason": reason, "ended_at": now})
            connection.execute(
                "UPDATE agent_run SET outcome = ?, payload = ?, ended_at = ? "
                "WHERE project_id = ? AND run_id = ?",
                (
                    outcome,
                    json.dumps(stored_run, sort_keys=True, ensure_ascii=True),
                    now,
                    project_id,
                    run_id,
                ),
            )
            for record in evidence:
                connection.execute(
                    "INSERT INTO evidence "
                    "(project_id, evidence_id, run_id, plan_id, job_id, predicate_id, result, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.project_id,
                        record.evidence_id,
                        record.run_id,
                        record.plan_id,
                        record.job_id,
                        record.predicate_id,
                        record.result,
                        json.dumps(record.as_payload(), sort_keys=True, ensure_ascii=True),
                    ),
                )
            connection.execute(
                "UPDATE job SET state = ? WHERE project_id = ? AND job_id = ?",
                (job_state, project_id, stored_run["job_id"]),
            )
            connection.execute("COMMIT")
        except StoreError:
            connection.execute("ROLLBACK")
            for path in written:
                _discard(path)
            raise
        except sqlite3.Error as error:
            connection.execute("ROLLBACK")
            for path in written:
                _discard(path)
            raise StoreError(REASON_STORE_UNAVAILABLE) from error
        finally:
            connection.close()
        return {"run_id": run_id, "outcome": outcome, "evidence_ids": [e.evidence_id for e in evidence]}

    def append_decision(self, decision: domain.DecisionRecord) -> Dict[str, Any]:
        """Append one human decision, idempotently."""
        payload_digest = domain.digest({"op": "decision", "payload": decision.as_payload()})
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replayed = self._replay(
                connection, decision.project_id, decision.idempotency_key, payload_digest
            )
            if replayed is not None:
                connection.execute("COMMIT")
                return replayed

            connection.execute(
                "INSERT INTO decision "
                "(project_id, decision_id, kind, outcome, idempotency_key, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    decision.project_id,
                    decision.decision_id,
                    decision.kind,
                    decision.outcome,
                    decision.idempotency_key,
                    json.dumps(decision.as_payload(), sort_keys=True, ensure_ascii=True),
                    decision.created_at,
                ),
            )
            # A scan-review decision moves the job to reviewed. A confirmation
            # decision does not: it targets the plan, not a run.
            job_id = (
                _job_of_run(connection, decision.project_id, decision.run_id)
                if decision.kind == domain.DECISION_SCAN_REVIEW and decision.run_id
                else ""
            )
            if job_id:
                connection.execute(
                    "UPDATE job SET state = ? WHERE project_id = ? AND job_id = ?",
                    (domain.JOB_REVIEWED, decision.project_id, job_id),
                )
            result = {"decision_id": decision.decision_id, "outcome": decision.outcome}
            self._remember(
                connection,
                decision.project_id,
                decision.idempotency_key,
                payload_digest,
                result,
                decision.created_at,
            )
            connection.execute("COMMIT")
            return result
        except StoreError:
            connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as error:
            connection.execute("ROLLBACK")
            raise StoreError(REASON_STORE_UNAVAILABLE) from error
        finally:
            connection.close()

    # -- reads --------------------------------------------------------------
    def run_by_idempotency_key(
        self, project_id: str, idempotency_key: str
    ) -> Optional[domain.AgentRun]:
        """Return the run a dispatch key already claimed, or ``None``.

        This is what makes a repeated dispatch one effect: the caller learns
        which execution it already started before it reaches the scanner.
        """
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT payload FROM agent_run WHERE project_id = ? AND idempotency_key = ? "
                "ORDER BY rowid LIMIT 1",
                (project_id, idempotency_key),
            ).fetchone()
        except sqlite3.Error as error:
            raise StoreError(REASON_STORE_UNAVAILABLE) from error
        finally:
            connection.close()
        return _run_from_payload(_decode(row)) if row else None

    def read_state(self, project_id: str) -> Dict[str, Any]:
        """Return every persisted record for a project, bounded and sanitized."""
        connection = self._connect()
        try:
            revisions = connection.execute(
                "SELECT payload, phase FROM plan_revision WHERE project_id = ? "
                "ORDER BY plan_id, revision",
                (project_id,),
            ).fetchall()
            job_row = connection.execute(
                "SELECT payload, state FROM job WHERE project_id = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            run_rows = connection.execute(
                "SELECT payload FROM agent_run WHERE project_id = ? ORDER BY rowid",
                (project_id,),
            ).fetchall()
            evidence_rows = connection.execute(
                "SELECT payload FROM evidence WHERE project_id = ? ORDER BY rowid",
                (project_id,),
            ).fetchall()
            decision_rows = connection.execute(
                "SELECT payload FROM decision WHERE project_id = ? ORDER BY rowid",
                (project_id,),
            ).fetchall()
        except sqlite3.Error as error:
            raise StoreError(REASON_STORE_UNAVAILABLE) from error
        finally:
            connection.close()

        return {
            "project_id": project_id,
            "revisions": [_revision_from_row(row) for row in revisions],
            "job": _job_from_row(job_row) if job_row else None,
            "runs": [_run_from_payload(_decode(row)) for row in run_rows],
            "evidence": [_evidence_from_payload(_decode(row)) for row in evidence_rows],
            "decisions": [_decision_from_payload(_decode(row)) for row in decision_rows],
        }

    # -- artifacts ----------------------------------------------------------
    def _write_artifact(self, name: str, payload: bytes) -> str:
        """Write one bounded artifact atomically and return its relative ref."""
        target_dir = evidence_dir(self.base_dir)
        path = os.path.join(target_dir, name)
        handle, temporary = tempfile.mkstemp(prefix=_TMP_PREFIX, suffix=_TMP_SUFFIX, dir=target_dir)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as error:
            _discard(temporary)
            raise StoreError(REASON_STORE_UNAVAILABLE) from error
        return os.path.join(EVIDENCE_DIR_NAME, name)

    def read_artifact(self, artifact_ref: str) -> Optional[bytes]:
        """Return a bounded artifact's bytes, or ``None`` when it is absent."""
        relative = str(artifact_ref).replace("\\", "/")
        if relative.startswith("/") or ".." in relative.split("/"):
            return None
        path = os.path.join(store_dir(self.base_dir), *relative.split("/"))
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except OSError:
            return None


def _discard(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _job_of_run(connection: sqlite3.Connection, project_id: str, run_id: str) -> str:
    row = connection.execute(
        "SELECT job_id FROM agent_run WHERE project_id = ? AND run_id = ?",
        (project_id, run_id),
    ).fetchone()
    return str(row["job_id"]) if row else ""


def _decode(row: sqlite3.Row) -> Mapping[str, Any]:
    """Decode one stored JSON payload, refusing a corrupt row by name."""
    try:
        return json.loads(str(row["payload"]))
    except (ValueError, TypeError) as error:
        raise StoreError(REASON_STORE_CORRUPT) from error


def _version_tuple(version: str) -> Tuple[int, ...]:
    parts: List[int] = []
    for chunk in str(version).split("."):
        try:
            parts.append(int(chunk))
        except (TypeError, ValueError):
            continue
    return tuple(parts)


def _revision_from_row(row: sqlite3.Row) -> domain.PlanRevision:
    try:
        payload = json.loads(str(row["payload"]))
    except (ValueError, TypeError) as error:
        raise StoreError(REASON_STORE_CORRUPT) from error
    return _revision_from_payload(payload, phase=str(row["phase"]))


def _revision_from_payload(payload: Mapping[str, Any], *, phase: str = "") -> domain.PlanRevision:
    job_spec = domain.JobSpec.from_payload(payload.get("job_spec") or {})
    return domain.PlanRevision(
        plan_id=str(payload.get("plan_id", "")),
        project_id=str(payload.get("project_id", "")),
        revision=int(payload.get("revision") or 0),
        goal=str(payload.get("goal", "")),
        provenance=str(payload.get("provenance", "")),
        scope=domain.Scope.from_payload(payload.get("scope") or {}),
        manifest_id=str(payload.get("manifest_id", "")),
        criteria=tuple(
            domain.Criterion.from_payload(item) for item in payload.get("criteria", ())
        ),
        job_spec=job_spec,
        phase=phase or str(payload.get("phase", domain.PLAN_DRAFT)),
        accepted_baseline_ref=payload.get("accepted_baseline_ref"),
        created_at=str(payload.get("created_at", "")),
    )


def _job_from_row(row: sqlite3.Row) -> domain.JobRecord:
    try:
        payload = json.loads(str(row["payload"]))
    except (ValueError, TypeError) as error:
        raise StoreError(REASON_STORE_CORRUPT) from error
    return _job_from_payload(payload, state=str(row["state"]))


def _job_from_payload(payload: Mapping[str, Any], *, state: str = "") -> domain.JobRecord:
    return domain.JobRecord(
        job_id=str(payload.get("job_id", "")),
        project_id=str(payload.get("project_id", "")),
        plan_id=str(payload.get("plan_id", "")),
        plan_revision=int(payload.get("plan_revision") or 0),
        capability=str(payload.get("capability", "")),
        executor=str(payload.get("executor", "")),
        scope=domain.Scope.from_payload(payload.get("scope") or {}),
        manifest_id=str(payload.get("manifest_id", "")),
        criterion_ids=tuple(str(c) for c in payload.get("criterion_ids", ())),
        state=state or str(payload.get("state", domain.JOB_READY)),
        reason=str(payload.get("reason", "")),
    )


def _run_from_payload(payload: Mapping[str, Any]) -> domain.AgentRun:
    return domain.AgentRun(
        run_id=str(payload.get("run_id", "")),
        project_id=str(payload.get("project_id", "")),
        job_id=str(payload.get("job_id", "")),
        plan_id=str(payload.get("plan_id", "")),
        plan_revision=int(payload.get("plan_revision") or 0),
        attempt=int(payload.get("attempt") or 0),
        idempotency_key=str(payload.get("idempotency_key", "")),
        executor=str(payload.get("executor", "")),
        executor_version=str(payload.get("executor_version", "")),
        manifest_id=str(payload.get("manifest_id", "")),
        outcome=str(payload.get("outcome", domain.RUN_RUNNING)),
        reason=str(payload.get("reason", "")),
        started_at=str(payload.get("started_at", "")),
        ended_at=str(payload.get("ended_at", "")),
        executor_pid=int(payload.get("executor_pid") or 0),
    )


def _evidence_from_payload(payload: Mapping[str, Any]) -> domain.EvidenceRecord:
    return domain.EvidenceRecord(
        evidence_id=str(payload.get("evidence_id", "")),
        project_id=str(payload.get("project_id", "")),
        plan_id=str(payload.get("plan_id", "")),
        job_id=str(payload.get("job_id", "")),
        run_id=str(payload.get("run_id", "")),
        kind=str(payload.get("kind", "")),
        predicate_id=str(payload.get("predicate_id", "")),
        result=str(payload.get("result", "")),
        manifest_id=str(payload.get("manifest_id", "")),
        limitation=str(payload.get("limitation", "")),
        counts=dict(payload.get("counts") or {}),
        grammar=dict(payload.get("grammar") or {}),
        scanner_schema=str(payload.get("scanner_schema", "")),
        artifact_ref=str(payload.get("artifact_ref", "")),
        artifact_digest=str(payload.get("artifact_digest", "")),
        created_at=str(payload.get("created_at", "")),
    )


def _decision_from_payload(payload: Mapping[str, Any]) -> domain.DecisionRecord:
    return domain.DecisionRecord(
        decision_id=str(payload.get("decision_id", "")),
        project_id=str(payload.get("project_id", "")),
        kind=str(payload.get("kind", "")),
        outcome=str(payload.get("outcome", "")),
        actor=str(payload.get("actor", "")),
        target_digest=str(payload.get("target_digest", "")),
        idempotency_key=str(payload.get("idempotency_key", "")),
        run_id=str(payload.get("run_id", "")),
        evidence_set_digest=str(payload.get("evidence_set_digest", "")),
        reason=str(payload.get("reason", "")),
        supersedes=str(payload.get("supersedes", "")),
        created_at=str(payload.get("created_at", "")),
    )


__all__ = [
    "ORCH_SCHEMA_VERSION",
    "ORCH_DIR_NAME",
    "DATABASE_FILENAME",
    "REASON_SCHEMA_NEWER",
    "REASON_SCHEMA_MALFORMED",
    "REASON_STORE_UNAVAILABLE",
    "REASON_STORE_CORRUPT",
    "REASON_STORE_LOCATION",
    "REASON_CONFLICT",
    "REASON_ACTIVE_RUN",
    "StoreError",
    "OrchestrationStore",
    "store_dir",
    "database_path",
    "evidence_dir",
    "assert_outside_project",
    "new_id",
]
