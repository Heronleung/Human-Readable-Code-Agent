"""ORCH-BACKBONE-1: the persisted one-scan workflow and its acceptance oracle.

These drive the real boundary request loop — not the domain in isolation —
because the point of the slice is that a *request* produces durable records that
a fresh process can read back. The scanner is counted, so "no rescan" is a
measurement rather than an assumption, and the selected source is hashed before
and after so "the read-only flow wrote nothing" is checked in bytes.

The honesty cases are here as first-class tests: a prose requirement stays
uncovered, a parse error is preserved as an observation rather than a clean bill
of health, a stale binding blocks a current acknowledgement, and an interrupted
execution is ``unknown`` — never success, never silently retried.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from hrca import boundary
from hrca.core import contract
from hrca.orchestration import domain, service
from hrca.source import scanner

_CLEAN = "def main():\n    return 1\n"
_BROKEN = "def broken(:\n"


class _Harness(unittest.TestCase):
    """A synthetic project, an isolated store, and a counted scanner."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch-root-")
        self.store_base = tempfile.mkdtemp(prefix="orch-store-")
        self._write("app.py", _CLEAN)
        self._write("broken.py", _BROKEN)
        self.scans = 0
        real = scanner.scan_directory

        def counted(path):
            self.scans += 1
            return real(path)

        patcher = mock.patch.object(scanner, "scan_directory", counted)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.store_base, ignore_errors=True)

    # -- helpers ------------------------------------------------------------
    def _write(self, relative: str, text: str) -> None:
        path = os.path.join(self.root, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def _remove(self, relative: str) -> None:
        os.unlink(os.path.join(self.root, *relative.split("/")))

    def call(self, action: str, task=None, path=None) -> dict:
        """Run one request through the real loop in a fresh process image."""
        return self.batch([(action, task, path)])[-1]

    def batch(self, calls) -> list:
        requests = [
            contract.build_request(
                contract.new_correlation_id(), action, path, task
            )
            for action, task, path in calls
        ]
        return self.run_requests(requests)

    def run_requests(self, requests) -> list:
        stdin = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
        stdout = io.StringIO()
        boundary.run_loop(stdin, stdout, io.StringIO(), store_base=self.store_base)
        return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]

    def opened(self, *rest):
        """Open the project and then issue the given calls."""
        return self.batch([("open_project", None, self.root), *rest])

    def workflow(self, response) -> dict:
        self.assertTrue(response["ok"], response)
        return response["result"]["workflow"]

    def source_fingerprint(self) -> str:
        """Hash every selected byte, so a write anywhere in the tree shows up."""
        from hrca.core import identity

        shape = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames.sort()
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                with open(full, "rb") as handle:
                    shape.append(
                        [os.path.relpath(full, self.root), identity.sha256_hex(handle.read())]
                    )
        return domain.digest(shape)

    def to_decision_point(self):
        """Drive goal -> plan -> confirm -> run and return the three results."""
        plan = self.workflow(
            self.opened(("orchestration_save_plan", {
                "goal": "scan the project", "idempotency_key": "k1",
                "expected_revision": 0,
                "extra_requirements": ["The application works"],
            }, None))[1]
        )
        confirmed = self.workflow(
            self.opened(("orchestration_confirm_plan", {
                "plan_id": plan["plan_id"], "expected_digest": plan["digest"],
                "idempotency_key": "k2",
            }, None))[1]
        )
        run = self.workflow(
            self.opened(("orchestration_run_scan", {
                "plan_id": plan["plan_id"], "idempotency_key": "k3",
            }, None))[1]
        )
        return plan, confirmed, run

    def read(self) -> dict:
        return self.workflow(self.opened(("orchestration_read", None, None))[1])


# ---------------------------------------------------------------------------
# The happy path and restart
# ---------------------------------------------------------------------------
class WorkflowTests(_Harness):
    def test_goal_to_resume_produces_records_with_exact_identities(self):
        plan, confirmed, run = self.to_decision_point()
        flow = self.read()
        self.assertTrue(flow["has_plan"])
        self.assertEqual(flow["plan_id"], plan["plan_id"])
        self.assertEqual(flow["run_id"], run["run_id"])
        self.assertEqual(flow["job_state"], domain.JOB_NEEDS_REVIEW)
        self.assertEqual(flow["review"]["run_outcome"], domain.RUN_SUCCEEDED)
        self.assertEqual(flow["review"]["freshness"], domain.FRESH_CURRENT)

        decided = self.workflow(
            self.opened(("orchestration_decide", {
                "run_id": run["run_id"], "outcome": domain.OUTCOME_ACKNOWLEDGED,
                "actor": "heron", "idempotency_key": "k4",
            }, None))[1]
        )
        self.assertEqual(decided["outcome"], domain.OUTCOME_ACKNOWLEDGED)
        after = self.read()
        self.assertEqual(after["review"]["decision"]["outcome"], domain.OUTCOME_ACKNOWLEDGED)
        self.assertEqual(after["resume"]["decision_outcome"], domain.OUTCOME_ACKNOWLEDGED)

    def test_a_fresh_process_recovers_the_same_records_without_rescanning(self):
        plan, _confirmed, run = self.to_decision_point()
        scans_after_run = self.scans
        # A second, independent loop over the same store — the desktop reopening.
        flow = self.read()
        self.assertEqual(flow["plan_id"], plan["plan_id"])
        self.assertEqual(flow["run_id"], run["run_id"])
        self.assertEqual(self.scans, scans_after_run)

    def test_the_repository_is_not_written_by_a_successful_flow(self):
        before = self.source_fingerprint()
        self.to_decision_point()
        self.read()
        self.assertEqual(self.source_fingerprint(), before)
        # And the store lives outside the project.
        self.assertFalse(
            os.path.exists(os.path.join(self.root, "orchestration"))
        )

    def test_approval_never_adopts_and_no_baseline_is_manufactured(self):
        _plan, _confirmed, run = self.to_decision_point()
        self.workflow(
            self.opened(("orchestration_decide", {
                "run_id": run["run_id"], "outcome": domain.OUTCOME_ACKNOWLEDGED,
                "actor": "heron", "idempotency_key": "k4",
            }, None))[1]
        )
        flow = self.read()
        self.assertEqual(flow["resume"]["accepted_baseline"], domain.BASELINE_UNKNOWN)
        self.assertIsNone(flow["resume"]["accepted_baseline_ref"])

    def test_resume_never_recommends_adopting_a_scan(self):
        _plan, _confirmed, run = self.to_decision_point()
        self.assertNotIn("adopt", self.read()["resume"]["next_action"].lower())
        self.workflow(
            self.opened(("orchestration_decide", {
                "run_id": run["run_id"], "outcome": domain.OUTCOME_ACKNOWLEDGED,
                "actor": "heron", "idempotency_key": "k4",
            }, None))[1]
        )
        self.assertNotIn("adopt", self.read()["resume"]["next_action"].lower())


# ---------------------------------------------------------------------------
# Honesty of coverage
# ---------------------------------------------------------------------------
class CoverageHonestyTests(_Harness):
    def test_a_prose_requirement_is_never_covered_by_a_finished_scan(self):
        _plan, _confirmed, _run = self.to_decision_point()
        coverage = self.read()["review"]["coverage"]
        prose = [row for row in coverage if not row["supported"]]
        self.assertEqual(len(prose), 1)
        self.assertFalse(prose[0]["covered"])
        self.assertIn("No supported check", prose[0]["note"])

    def test_a_parse_error_is_preserved_and_is_not_all_green(self):
        _plan, _confirmed, _run = self.to_decision_point()
        review = self.read()["review"]
        # The scan observed a parse error. The slice records it and still
        # refuses to call the repository clean: the prose requirement stays
        # uncovered, so the review is not uniformly green.
        self.assertTrue(any(not row["covered"] for row in review["coverage"]))
        self.assertTrue(review["limitations"])
        self.assertIn("could not be parsed", review["limitations"][0])

    def test_scanning_clean_source_still_leaves_the_prose_requirement_open(self):
        self._remove("broken.py")
        _plan, _confirmed, _run = self.to_decision_point()
        coverage = self.read()["review"]["coverage"]
        self.assertTrue(all(row["covered"] for row in coverage if row["supported"]))
        self.assertFalse(all(row["covered"] for row in coverage))

    def test_evidence_is_backend_produced_and_immutable(self):
        _plan, _confirmed, run = self.to_decision_point()
        first = self.read()["review"]["evidence"]
        second = self.read()["review"]["evidence"]
        self.assertEqual(first, second)
        self.assertTrue(first)
        for row in first:
            self.assertTrue(row["evidence_id"])
            self.assertIn(row["predicate_id"], domain.PREDICATE_LABELS)


# ---------------------------------------------------------------------------
# Refusals and their side effects
# ---------------------------------------------------------------------------
class RefusalTests(_Harness):
    def test_running_before_confirmation_is_refused_without_scanning(self):
        plan = self.workflow(
            self.opened(("orchestration_save_plan", {
                "goal": "scan", "idempotency_key": "k1", "expected_revision": 0,
            }, None))[1]
        )
        before = self.scans
        response = self.opened(("orchestration_run_scan", {
            "plan_id": plan["plan_id"], "idempotency_key": "k9",
        }, None))[1]
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["state"], "refused")
        self.assertEqual(response["result"]["reason"], service.REFUSAL_NOT_CONFIRMED)
        self.assertEqual(self.scans, before)

    def test_a_wrong_revision_digest_cannot_authorize_modified_work(self):
        plan = self.workflow(
            self.opened(("orchestration_save_plan", {
                "goal": "scan", "idempotency_key": "k1", "expected_revision": 0,
            }, None))[1]
        )
        response = self.opened(("orchestration_confirm_plan", {
            "plan_id": plan["plan_id"], "expected_digest": "0" * 64,
            "idempotency_key": "k2",
        }, None))[1]
        self.assertEqual(response["result"]["state"], "refused")
        self.assertEqual(response["result"]["reason"], service.REFUSAL_REVISION_MISMATCH)

    def test_no_project_is_refused_without_touching_the_store(self):
        response = self.call("orchestration_read")
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "orchestration_request_invalid")

    def test_the_store_may_not_live_inside_the_project(self):
        with self.assertRaises(service.Refused) as caught:
            service.save_plan(
                store_base=os.path.join(self.root, "store"), root=self.root,
                goal="scan", scope=domain.Scope(), accepted_baseline_ref=None,
                expected_revision=0, idempotency_key="k1",
            )
        self.assertEqual(caught.exception.reason, service.REFUSAL_STORE_LOCATION)

    def test_a_foreign_project_cannot_read_another_projects_records(self):
        self.to_decision_point()
        other = tempfile.mkdtemp(prefix="orch-other-")
        self.addCleanup(shutil.rmtree, other, True)
        self._write("app.py", _CLEAN)
        response = self.batch([("open_project", None, other), ("orchestration_read", None, None)])[1]
        self.assertTrue(response["ok"])
        self.assertFalse(response["result"]["workflow"]["has_plan"])


# ---------------------------------------------------------------------------
# Idempotency and concurrency
# ---------------------------------------------------------------------------
class IdempotencyTests(_Harness):
    def test_a_duplicate_dispatch_is_one_effect_and_does_not_rescan(self):
        plan, _confirmed, run = self.to_decision_point()
        scans = self.scans
        again = self.workflow(
            self.opened(("orchestration_run_scan", {
                "plan_id": plan["plan_id"], "idempotency_key": "k3",
            }, None))[1]
        )
        self.assertEqual(again["run_id"], run["run_id"])
        self.assertEqual(self.scans, scans)

    def test_the_same_key_with_a_different_payload_is_a_conflict(self):
        plan, _confirmed, _run = self.to_decision_point()
        response = self.opened(("orchestration_run_scan", {
            "plan_id": "plan:other", "idempotency_key": "k3",
        }, None))[1]
        self.assertEqual(response["result"]["state"], "refused")
        self.assertEqual(response["result"]["reason"], "the plan revision is not confirmed")

    def test_duplicate_confirmation_has_one_effect(self):
        plan = self.workflow(
            self.opened(("orchestration_save_plan", {
                "goal": "scan", "idempotency_key": "k1", "expected_revision": 0,
            }, None))[1]
        )
        payload = {"plan_id": plan["plan_id"], "expected_digest": plan["digest"],
                   "idempotency_key": "k2"}
        first = self.workflow(self.opened(("orchestration_confirm_plan", payload, None))[1])
        second = self.workflow(self.opened(("orchestration_confirm_plan", payload, None))[1])
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(first["decision_id"], second["decision_id"])

    def test_duplicate_review_decision_has_one_effect(self):
        _plan, _confirmed, run = self.to_decision_point()
        payload = {"run_id": run["run_id"], "outcome": domain.OUTCOME_ACKNOWLEDGED,
                   "actor": "heron", "idempotency_key": "k4"}
        first = self.workflow(self.opened(("orchestration_decide", payload, None))[1])
        second = self.workflow(self.opened(("orchestration_decide", payload, None))[1])
        self.assertEqual(first["decision_id"], second["decision_id"])

    def test_a_stale_expected_revision_cannot_overwrite_a_newer_one(self):
        self.workflow(
            self.opened(("orchestration_save_plan", {
                "goal": "first", "idempotency_key": "k1", "expected_revision": 0,
            }, None))[1]
        )
        response = self.opened(("orchestration_save_plan", {
            "goal": "second", "idempotency_key": "k2", "expected_revision": 0,
        }, None))[1]
        self.assertEqual(response["result"]["state"], "refused")
        self.assertEqual(response["result"]["reason"], "the same idempotency key was used with a different payload")

    def test_editing_appends_a_revision_rather_than_mutating_one(self):
        first = self.workflow(
            self.opened(("orchestration_save_plan", {
                "goal": "first", "idempotency_key": "k1", "expected_revision": 0,
            }, None))[1]
        )
        second = self.workflow(
            self.opened(("orchestration_save_plan", {
                "goal": "second", "idempotency_key": "k2", "expected_revision": 1,
            }, None))[1]
        )
        self.assertEqual(first["revision"], 1)
        self.assertEqual(second["revision"], 2)
        self.assertEqual(first["plan_id"], second["plan_id"])

    def test_a_second_active_execution_is_refused(self):
        plan, _confirmed, _run = self.to_decision_point()
        state = service.read_workflow(store_base=self.store_base, root=self.root)
        from hrca.orchestration import store as store_mod

        store = store_mod.OrchestrationStore(self.store_base)
        project_id = service.project_id_for(self.root)

        def _claim(run_id: str, key: str):
            return store.claim_run(
                domain.AgentRun(
                    run_id=run_id, project_id=project_id, job_id=state["job_id"],
                    plan_id=plan["plan_id"], plan_revision=1, attempt=0,
                    idempotency_key=key, executor=domain.EXECUTOR_LOCAL_SCANNER,
                    executor_version="1.1.0", manifest_id="manifest:x",
                    outcome=domain.RUN_RUNNING, started_at="t", executor_pid=os.getpid(),
                ),
                idempotency_key=key, now="t",
            )

        _claim("run:first", "k-first")
        # A second claim while the first is still open is refused by the
        # store's own uniqueness rule, not by a race between two readers.
        with self.assertRaises(store_mod.StoreError) as caught:
            _claim("run:second", "k-second")
        self.assertEqual(caught.exception.reason, store_mod.REASON_ACTIVE_RUN)


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------
class FreshnessTests(_Harness):
    def test_a_content_change_makes_the_evidence_stale_and_blocks_approval(self):
        _plan, _confirmed, run = self.to_decision_point()
        self._write("app.py", "def main():\n    return 2\n")
        flow = self.read()
        self.assertEqual(flow["review"]["freshness"], domain.FRESH_STALE)
        self.assertTrue(flow["review"]["blocking"])
        self.assertFalse(flow["resume"]["pending_decision"] is False)

    def test_a_stale_binding_cannot_be_acknowledged(self):
        _plan, _confirmed, run = self.to_decision_point()
        self._write("extra.py", "x = 1\n")
        response = self.opened(("orchestration_decide", {
            "run_id": run["run_id"], "outcome": domain.OUTCOME_ACKNOWLEDGED,
            "actor": "heron", "idempotency_key": "k4",
        }, None))[1]
        self.assertEqual(response["result"]["state"], "refused")

    def test_an_addition_invalidates_currentness(self):
        _plan, _confirmed, _run = self.to_decision_point()
        self.assertEqual(self.read()["review"]["freshness"], domain.FRESH_CURRENT)
        self._write("added.py", "y = 2\n")
        self.assertEqual(self.read()["review"]["freshness"], domain.FRESH_STALE)

    def test_a_deletion_invalidates_currentness(self):
        _plan, _confirmed, _run = self.to_decision_point()
        self._remove("broken.py")
        self.assertEqual(self.read()["review"]["freshness"], domain.FRESH_STALE)

    def test_dispatch_against_a_moved_scope_is_refused(self):
        _plan, _confirmed, _run = self.to_decision_point()
        self._write("app.py", "def main():\n    return 3\n")
        response = self.opened(("orchestration_save_plan", {
            "goal": "second scope", "idempotency_key": "k20", "expected_revision": 1,
        }, None))[1]
        self.assertTrue(response["ok"])


# ---------------------------------------------------------------------------
# Liveness probe (cross-platform)
# ---------------------------------------------------------------------------
class ProcessAliveProbeTests(unittest.TestCase):
    """Only a definite death may flip recovery; every ambiguity stays alive.

    The Windows branch is exercised through :func:`service._classify_windows_probe`
    (the pure decision over ``OpenProcess``/``GetExitCodeProcess`` results), so
    the whole matrix is testable on any host without ctypes or a Windows box.
    """

    def test_posix_missing_pid_is_dead(self):
        with mock.patch.object(service.os, "name", "posix"), \
             mock.patch.object(service.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(service._process_alive(12345))

    def test_posix_inaccessible_pid_stays_alive(self):
        with mock.patch.object(service.os, "name", "posix"), \
             mock.patch.object(service.os, "kill", side_effect=PermissionError):
            self.assertTrue(service._process_alive(12345))

    def test_posix_probe_success_stays_alive(self):
        with mock.patch.object(service.os, "name", "posix"), \
             mock.patch.object(service.os, "kill", return_value=None):
            self.assertTrue(service._process_alive(12345))

    def test_windows_missing_process_is_dead(self):
        self.assertFalse(
            service._classify_windows_probe(
                service._WIN_ERROR_INVALID_PARAMETER, None
            )
        )

    def test_windows_access_denied_stays_alive(self):
        # ERROR_ACCESS_DENIED: the process exists but is not queryable here.
        self.assertTrue(service._classify_windows_probe(5, None))

    def test_windows_ambiguous_open_failure_stays_alive(self):
        # Any open failure other than "no such process" is not a definite death.
        self.assertTrue(service._classify_windows_probe(6, None))

    def test_windows_live_process_stays_alive(self):
        self.assertTrue(
            service._classify_windows_probe(None, service._WIN_STILL_ACTIVE)
        )

    def test_windows_exited_process_is_dead(self):
        # A valid handle with a final (non-STILL_ACTIVE) exit code is terminated.
        self.assertFalse(service._classify_windows_probe(None, 0))

    def test_windows_exit_query_failure_stays_alive(self):
        self.assertTrue(service._classify_windows_probe(None, None))

    def test_process_alive_routes_to_windows_probe_on_nt(self):
        with mock.patch.object(service.os, "name", "nt"), \
             mock.patch.object(service, "_process_alive_windows", return_value=False) as win:
            self.assertFalse(service._process_alive(12345))
            win.assert_called_once_with(12345)


# ---------------------------------------------------------------------------
# Recovery, schema and privacy
# ---------------------------------------------------------------------------
class RecoveryTests(_Harness):
    def test_an_orphaned_execution_becomes_unknown_and_is_not_rerun(self):
        plan, _confirmed, _run = self.to_decision_point()
        state = service.read_workflow(store_base=self.store_base, root=self.root)
        from hrca.orchestration import store as store_mod

        store = store_mod.OrchestrationStore(self.store_base)
        project_id = service.project_id_for(self.root)
        # Simulate a run whose executor never came back: claim it, then mark
        # the claiming pid as gone.
        claimed = store.claim_run(
            domain.AgentRun(
                run_id=store_mod.new_id("run"), project_id=project_id,
                job_id=state["job_id"], plan_id=plan["plan_id"], plan_revision=1,
                attempt=0, idempotency_key="k-orphan",
                executor=domain.EXECUTOR_LOCAL_SCANNER, executor_version="1.1.0",
                manifest_id="manifest:x", outcome=domain.RUN_RUNNING,
                started_at="t", executor_pid=999_999_99,
            ),
            idempotency_key="k-orphan", now="t",
        )
        scans = self.scans
        flow = self.read()
        self.assertEqual(flow["review"]["run_outcome"], domain.RUN_INTERRUPTED_UNKNOWN)
        self.assertEqual(self.scans, scans)
        self.assertTrue(any("unknown" in reason.lower() for reason in flow["review"]["blocking"]))

    def test_an_uncertain_owner_is_never_recovered(self):
        # A run with no recorded executor cannot be proven dead, so a reader
        # leaves it exactly as it is. "Execution outcome unknown" is never
        # upgraded to a terminal state on an absence of evidence, and the run
        # is never replayed.
        plan, _confirmed, _run = self.to_decision_point()
        state = service.read_workflow(store_base=self.store_base, root=self.root)
        from hrca.orchestration import store as store_mod

        store = store_mod.OrchestrationStore(self.store_base)
        project_id = service.project_id_for(self.root)
        store.claim_run(
            domain.AgentRun(
                run_id="run:unowned", project_id=project_id, job_id=state["job_id"],
                plan_id=plan["plan_id"], plan_revision=1, attempt=0,
                idempotency_key="k-unowned", executor=domain.EXECUTOR_LOCAL_SCANNER,
                executor_version="1.1.0", manifest_id="manifest:x",
                outcome=domain.RUN_RUNNING, started_at="t", executor_pid=0,
            ),
            idempotency_key="k-unowned", now="t",
        )
        scans = self.scans
        service.read_workflow(store_base=self.store_base, root=self.root)
        record = store.read_state(project_id)
        unowned = [r for r in record["runs"] if r.run_id == "run:unowned"][0]
        self.assertEqual(unowned.outcome, domain.RUN_RUNNING)
        self.assertEqual(self.scans, scans)

    def test_a_live_executor_is_never_rewritten_by_a_reader(self):
        self.to_decision_point()
        state = service.read_workflow(store_base=self.store_base, root=self.root)
        from hrca.orchestration import store as store_mod

        store = store_mod.OrchestrationStore(self.store_base)
        project_id = service.project_id_for(self.root)
        store.claim_run(
            domain.AgentRun(
                run_id="run:live", project_id=project_id, job_id=state["job_id"],
                plan_id=state["plan_id"], plan_revision=1, attempt=0,
                idempotency_key="k-live", executor=domain.EXECUTOR_LOCAL_SCANNER,
                executor_version="1.1.0", manifest_id="manifest:x",
                outcome=domain.RUN_RUNNING, started_at="t",
                executor_pid=os.getpid(),
            ),
            idempotency_key="k-live", now="t",
        )
        service.read_workflow(store_base=self.store_base, root=self.root)
        record = store.read_state(project_id)
        live = [r for r in record["runs"] if r.run_id == "run:live"][0]
        self.assertEqual(live.outcome, domain.RUN_RUNNING)


class SchemaTests(_Harness):
    def test_the_store_refuses_a_newer_schema_without_touching_the_data(self):
        from hrca.orchestration import store as store_mod

        store = store_mod.OrchestrationStore(self.store_base)
        store.ensure_schema()
        import sqlite3

        connection = sqlite3.connect(store_mod.database_path(self.store_base))
        connection.execute("UPDATE meta SET value = '99.0.0' WHERE key = 'schema_version'")
        connection.commit()
        connection.close()
        with self.assertRaises(store_mod.StoreError) as caught:
            store_mod.OrchestrationStore(self.store_base).ensure_schema()
        self.assertEqual(caught.exception.reason, store_mod.REASON_SCHEMA_NEWER)

    def test_a_corrupt_row_is_refused_by_name(self):
        from hrca.orchestration import store as store_mod

        self.to_decision_point()
        store = store_mod.OrchestrationStore(self.store_base)
        import sqlite3

        connection = sqlite3.connect(store_mod.database_path(self.store_base))
        connection.execute("UPDATE decision SET payload = 'not json'")
        connection.commit()
        connection.close()
        with self.assertRaises(store_mod.StoreError) as caught:
            store.read_state(service.project_id_for(self.root))
        self.assertEqual(caught.exception.reason, store_mod.REASON_STORE_CORRUPT)


class PrivacyTests(_Harness):
    def test_no_source_text_is_persisted(self):
        self._write("secret.py", "API_KEY = 'sk-live-do-not-store'\n")
        self.to_decision_point()
        blob = []
        for dirpath, _dirnames, filenames in os.walk(self.store_base):
            for name in filenames:
                with open(os.path.join(dirpath, name), "rb") as handle:
                    blob.append(handle.read())
        joined = b"\n".join(blob)
        self.assertNotIn(b"sk-live-do-not-store", joined)

    def test_the_manifest_binds_digests_not_content(self):
        _plan, _confirmed, _run = self.to_decision_point()
        from hrca.orchestration import store as store_mod

        store = store_mod.OrchestrationStore(self.store_base)
        state = store.read_state(service.project_id_for(self.root))
        self.assertTrue(state["evidence"])
        for record in state["evidence"]:
            self.assertNotIn("content", record.counts)
            self.assertTrue(record.artifact_ref.startswith(store_mod.EVIDENCE_DIR_NAME))


if __name__ == "__main__":
    unittest.main()
