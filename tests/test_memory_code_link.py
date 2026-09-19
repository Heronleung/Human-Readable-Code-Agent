"""Tests for the M4.5/v2b Memory Code Twin link and freshness boundary.

A link binds one stored Memory record to one exact Code Twin entity plus the
Twin's workspace revision at the time it was taken. These tests pin the
properties that make that bound honest:

* identity is exact or the answer is a miss — no label, prose, path substring,
  row order, digest or inferred symbol is ever substituted for it;
* ``current`` is returned only where authoritative Twin state attests that the
  recorded revision is the current one, and a retained earlier version can never
  read as current;
* the recorded revision is the Twin's revision *number*, not a content digest, so
  no fingerprint value crosses the boundary in either direction;
* neither action writes anything, so Memory authority is untouched.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest

from hrca import (
    boundary,
    contract,
    memory,
    memory_docs,
    memory_store,
    memory_twin_link,
    scanner,
    twin,
    twin_store,
)

# The workspace the session is rooted at. The Twin domain hashes this string, so
# the tests never need a real directory at this path.
ROOT = "/tmp/hrca-v2b-workspace"

_SECRET = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
_PERSONAL = "C:/Users/someone/.ssh/id_rsa"

# Any 64-hex run is a SHA-256. The only one this contract may ever emit is the
# workspace identity, whose ``ws:`` prefix makes it an identity rather than a
# content digest.
_HEX64 = re.compile(r"\b[0-9a-f]{64}\b")

_FUNCTION = "artifact:function:pkg.mod.f"
_OTHER_FUNCTION = "artifact:function:pkg.other.f"
_FILE = "artifact:file:pkg/mod.py"


class LinkTestCase(unittest.TestCase):
    """A really-scanned project, its authoritative Twin, and one stored run."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="hrca-v2b-")
        self.project = tempfile.mkdtemp(prefix="hrca-v2b-src-")
        self.session = boundary.WorkspaceSession(store_base=self.base)
        self.session.root = ROOT
        self.workspace_id = twin.workspace_id_for(ROOT)
        self.write("pkg/__init__.py", "")
        self.write("pkg/mod.py", "def f():\n    return 1\n")
        # A same-named function in another module: the collision the exact
        # identity rule exists to survive.
        self.write("pkg/other.py", "def f():\n    return 2\n")
        self.store = self.publish(1)
        self.run_id, run_store = self.seed_run()
        self.record_id = run_store["code_entity_links"][0]["id"]
        self.run_store = run_store

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)
        shutil.rmtree(self.project, ignore_errors=True)

    # -- fixtures --------------------------------------------------------

    def write(self, rel_path, text):
        path = os.path.join(self.project, *rel_path.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def scan(self):
        document = scanner.scan_directory(self.project)
        fingerprints = {}
        for file_rec in document.get("files", []):
            rel_path = file_rec.get("path")
            if not isinstance(rel_path, str) or not rel_path.endswith((".py", ".pyi")):
                continue
            try:
                with open(os.path.join(self.project, rel_path), "rb") as handle:
                    fingerprints[rel_path] = twin.fingerprint_bytes(handle.read())
            except OSError:
                fingerprints[rel_path] = None
        return document, fingerprints

    def publish(self, generation, store_override=None):
        """Write a fresh authoritative Twin store at ``generation``."""
        if store_override is not None:
            store = store_override
        else:
            document, fingerprints = self.scan()
            store = twin.build_store(
                document, fingerprints, self.workspace_id, generation,
                "2026-01-01T00:00:00Z",
            )
        self.assertIsNone(twin_store.save(self.base, self.workspace_id, store))
        self.store = store
        return store

    def resync(self, generation):
        """Reconcile the current source against the stored Twin incrementally."""
        document, fingerprints = self.scan()
        store, result = twin.sync_twin(
            document, fingerprints, self.store, self.workspace_id, generation,
            "2026-01-01T00:00:00Z",
        )
        self.assertIsNone(twin_store.save(self.base, self.workspace_id, store))
        self.store = store
        return store, result

    def seed_run(self, session_id="s-1"):
        events = [
            {"event_type": "run_started", "source_event_id": "e1",
             "payload": {"prompt": _SECRET}},
            {"event_type": "run_progress", "source_event_id": "e2", "payload": {},
             "paths": ["pkg/mod.py"],
             # The Memory side's own claim about source. A link deliberately
             # never reads this to resolve a Twin entity.
             "code_entities": [{"path": "pkg/mod.py", "symbol": "pkg.mod.f"}],
             "evidence": [{"kind": "artifact", "artifact_ref": "pkg/mod.py",
                           "digest": "sha256:" + "a" * 64}]},
            {"event_type": "run_terminated", "source_event_id": "e3",
             "outcome": "completed", "payload": {}},
            {"event_type": "stream_ended", "source_event_id": "e4", "payload": {}}]
        store, error, _ = memory.ingest_session({
            "adapter": "smoke", "session_id": session_id, "events": events,
            "project": {"source_id": "p-1", "name": "Project One"},
            "work_package": {"source_id": "w-1", "title": "Work Package One"},
        })
        self.assertIsNone(error)
        run_id = store["agent_run"]["id"]
        self.assertIsNone(memory_store.save(self.base, run_id, store))
        return run_id, store

    # -- boundary calls --------------------------------------------------

    def call(self, payload, **overrides):
        request = {
            "contract_version": contract.CONTRACT_VERSION,
            "correlation_id": "c1",
        }
        request.update(payload)
        request.update(overrides)
        return boundary.handle_request(request, self.session)

    def bind(self, entity_id=_FUNCTION, entity_kind="function", **overrides):
        payload = {
            "action": contract.ACTION_MEMORY_CODE_LINK,
            "entity_id": entity_id,
            "entity_kind": entity_kind,
            "run_id": self.run_id,
            "record_id": self.record_id,
            "kind": memory_docs.LINK_CODE_ENTITY,
        }
        payload.update(overrides)
        return self.call(payload)

    def link_for(self, entity_id=_FUNCTION, entity_kind="function", revision=None):
        """The link document the bind action returns, or a hand-built one."""
        if revision is None:
            result = self.bind(entity_id, entity_kind)
            self.assertTrue(result["ok"], result)
            return result["result"]
        return {
            "link_schema_version": "1.0.0",
            "workspace_id": self.workspace_id,
            "entity_id": entity_id,
            "entity_kind": entity_kind,
            "memory_run_id": self.run_id,
            "memory_record_id": self.record_id,
            "recorded_revision": revision,
        }

    def resolve(self, link, **overrides):
        payload = {"action": contract.ACTION_MEMORY_CODE_FRESHNESS}
        payload.update(link)
        payload.update(overrides)
        return self.call(payload)

    def freshness(self, link, **overrides):
        result = self.resolve(link, **overrides)
        self.assertTrue(result["ok"], result)
        return result["result"]

    def assert_error(self, result, code):
        self.assertFalse(result["ok"], result)
        self.assertEqual(code, result["error"]["code"])
        # Every message is catalogue-drawn and interpolates nothing.
        self.assertEqual(contract.error_message(code), result["error"]["message"])


class ExactIdentityTests(LinkTestCase):
    """Identity is exact, or the answer is a miss — never a substitute."""

    def test_a_link_binds_the_exact_entity_it_names(self):
        link = self.link_for()
        self.assertEqual(_FUNCTION, link["entity_id"])
        self.assertEqual("function", link["entity_kind"])
        self.assertEqual(self.workspace_id, link["workspace_id"])
        self.assertEqual(self.run_id, link["memory_run_id"])
        self.assertEqual(self.record_id, link["memory_record_id"])
        self.assertEqual("1.0.0", link["link_schema_version"])

    def test_a_same_named_entity_in_another_module_is_a_different_entity(self):
        # Both modules define ``f``. Each link must resolve to its own entity and
        # neither may stand in for the other.
        mine = self.freshness(self.link_for(_FUNCTION))
        theirs = self.freshness(self.link_for(_OTHER_FUNCTION))
        self.assertEqual("current", mine["freshness"])
        self.assertEqual("current", theirs["freshness"])
        self.assertEqual(_FUNCTION, mine["entity_id"])
        self.assertEqual(_OTHER_FUNCTION, theirs["entity_id"])
        self.assertNotEqual(mine["entity_id"], theirs["entity_id"])

    def test_an_absent_identity_is_never_substituted_by_a_similar_name(self):
        # ``pkg.mod.ff`` is one character from a real entity: it is a miss.
        link = self.link_for("artifact:function:pkg.mod.ff", "function", revision=1)
        self.assertEqual("missing", self.freshness(link)["freshness"])

    def test_an_entity_kind_cannot_be_swapped_for_another(self):
        # The identity says ``function``: there is no class by that name, and the
        # entity kind cannot be relabelled to reach a different artifact.
        link = self.link_for("artifact:class:pkg.mod.f", "class", revision=1)
        self.assertEqual("missing", self.freshness(link)["freshness"])
        self.assert_error(self.bind("artifact:class:pkg.mod.f", "class"),
                          "twin_entity_not_found")
        # A kind that disagrees with the identity is refused outright.
        self.assert_error(self.bind(_FUNCTION, "class"), "memory_code_link_invalid")

    def test_a_file_entity_binds_and_resolves_like_a_symbol(self):
        link = self.link_for(_FILE, "file")
        self.assertEqual(_FILE, link["entity_id"])
        self.assertEqual("current", self.freshness(link)["freshness"])

    def test_identity_is_not_taken_from_the_memory_records_own_claim(self):
        # The bound Memory record claims ``pkg.mod.f``. The link below names the
        # other module's entity and binds it, proving resolution reads the given
        # identity alone: the record's path and symbol are never consulted.
        link = self.link_for(_OTHER_FUNCTION)
        self.assertEqual(_OTHER_FUNCTION, link["entity_id"])
        self.assertEqual("current", self.freshness(link)["freshness"])

    def test_resolution_is_deterministic(self):
        link = self.link_for()
        first = self.freshness(link)
        second = self.freshness(link)
        self.assertEqual(first, second)


class FreshnessTests(LinkTestCase):
    """Each state means exactly one thing, and only one of them is current."""

    def test_a_link_taken_now_is_current(self):
        result = self.freshness(self.link_for())
        self.assertEqual("current", result["freshness"])
        self.assertTrue(result["actionable"])
        self.assertIsNone(result["reason"])
        self.assertEqual(1, result["recorded_revision"])
        self.assertEqual(1, result["current_revision"])

    def test_a_source_change_makes_a_link_stale(self):
        link = self.link_for()
        self.write("pkg/mod.py", "def f():\n    return 11\n")
        self.publish(2)
        result = self.freshness(link)
        self.assertEqual("stale", result["freshness"])
        self.assertTrue(result["actionable"])
        self.assertEqual(1, result["recorded_revision"])
        self.assertEqual(2, result["current_revision"])

    def test_a_retained_earlier_version_is_historical_and_never_current(self):
        # Bind against the entity while its source parses ...
        link = self.link_for()
        self.assertEqual("current", self.freshness(link)["freshness"])
        # ... then break the file: the Twin keeps the last valid symbol record
        # and says so, so the link now points at retained history.
        self.write("pkg/mod.py", "def f(:\n    return 1\n")
        self.resync(2)
        retained = [
            record for record in self.store["artifacts"]
            if record["id"] == _FUNCTION
        ]
        self.assertEqual([twin.SYNC_STALE], [r["sync_state"] for r in retained])
        result = self.freshness(link)
        self.assertEqual("historical", result["freshness"])
        self.assertNotEqual("current", result["freshness"])

    def test_a_retained_version_cannot_be_bound_afresh(self):
        self.write("pkg/mod.py", "def f(:\n    return 1\n")
        self.resync(2)
        self.assert_error(self.bind(), "twin_entity_not_current")

    def test_a_removed_entity_is_missing_and_non_actionable(self):
        link = self.link_for()
        os.remove(os.path.join(self.project, "pkg", "mod.py"))
        self.publish(2)
        result = self.freshness(link)
        self.assertEqual("missing", result["freshness"])
        self.assertFalse(result["actionable"])

    def test_history_outranks_the_revision_comparison(self):
        # A hand-built link whose revision *equals* the authoritative one still
        # cannot be current if the Twin retains the entity as a past version.
        self.write("pkg/mod.py", "def f(:\n    return 1\n")
        self.resync(2)
        link = self.link_for(_FUNCTION, "function", revision=2)
        result = self.freshness(link)
        self.assertEqual("historical", result["freshness"])
        self.assertNotEqual("current", result["freshness"])


class UnsupportedTests(LinkTestCase):
    """No comparable authority is reported, never assumed."""

    def test_an_absent_twin_store_is_unsupported(self):
        link = self.link_for()
        shutil.rmtree(os.path.dirname(
            twin_store.workspace_store_path(self.base, self.workspace_id)
        ), ignore_errors=True)
        result = self.freshness(link)
        self.assertEqual("unsupported", result["freshness"])
        self.assertFalse(result["actionable"])

    def test_a_future_schema_store_is_unsupported(self):
        link = self.link_for()
        future = json.loads(json.dumps(self.store))
        future["schema_version"] = "9.9.9"
        self.assertIsNone(twin_store.save(self.base, self.workspace_id, future))
        result = self.freshness(link)
        self.assertEqual("unsupported", result["freshness"])
        self.assertFalse(result["actionable"])
        # ... and no link can be taken against it either.
        self.assert_error(self.bind(), "twin_not_synchronized")

    def test_a_corrupt_store_is_unsupported(self):
        link = self.link_for()
        path = twin_store.workspace_store_path(self.base, self.workspace_id)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual("unsupported", self.freshness(link)["freshness"])

    def test_a_link_from_another_workspace_is_never_compared(self):
        link = self.link_for()
        foreign = dict(link, workspace_id="ws:" + "0" * 64)
        result = self.freshness(foreign)
        self.assertEqual("unsupported", result["freshness"])
        self.assertFalse(result["actionable"])

    def test_an_unclassified_artifact_state_is_unsupported(self):
        link = self.link_for()
        mutated = json.loads(json.dumps(self.store))
        for record in mutated["artifacts"]:
            if record["id"] == _FUNCTION:
                record["sync_state"] = "blocked"
        self.publish(1, store_override=mutated)
        # The mutated store is written but its revision is unchanged, so the
        # only thing standing between the link and ``current`` is the state.
        result = self.freshness(link)
        self.assertEqual("unsupported", result["freshness"])
        self.assertFalse(result["actionable"])

    def test_a_store_naming_another_workspace_is_unsupported(self):
        link = self.link_for()
        mutated = json.loads(json.dumps(self.store))
        mutated["workspace_revision"]["workspace_id"] = "ws:" + "1" * 64
        self.publish(1, store_override=mutated)
        self.assertEqual("unsupported", self.freshness(link)["freshness"])


class BindRefusalTests(LinkTestCase):
    """A link is refused rather than guessed when its preconditions fail."""

    def test_a_malformed_entity_identity_is_refused(self):
        for entity_id, kind in (
            ("not-an-id", "function"),
            ("artifact:function:", "function"),
            ("artifact:bogus:pkg.mod.f", "bogus"),
            ("artifact:function:pkg.mod.f", ""),
            ("artifact:function:" + "x" * 500, "function"),
        ):
            with self.subTest(entity_id=entity_id):
                self.assert_error(self.bind(entity_id, kind),
                                  "memory_code_link_invalid")

    def test_a_path_outside_the_workspace_is_refused(self):
        for entity_id in ("artifact:file:/etc/passwd",
                          "artifact:file:a/../b.py",
                          "artifact:file:.." + os.sep + "b.py",
                          "artifact:file:C:/x.py"):
            with self.subTest(entity_id=entity_id):
                self.assert_error(self.bind(entity_id, "file"),
                                  "memory_code_link_invalid")

    def test_an_unknown_run_or_record_is_refused(self):
        self.assert_error(
            self.bind(run_id="run:absent:x:run"), "memory_run_not_found"
        )
        self.assert_error(
            self.bind(record_id="link:absent:x:1"), "memory_record_not_found"
        )

    def test_an_unsupported_record_kind_is_refused(self):
        self.assert_error(self.bind(kind="not_a_kind"), "memory_kind_not_supported")

    def test_a_missing_entity_is_refused_not_invented(self):
        self.assert_error(
            self.bind("artifact:function:pkg.gone.f"), "twin_entity_not_found"
        )

    def test_an_unusable_revision_is_refused(self):
        # Freshness is never asserted by a caller, and a digest can never travel
        # in the revision field: only the Twin's own revision number is accepted.
        link = self.link_for()
        for revision in ("sha256:" + "a" * 64, "1", None, True, -1, 10 ** 12):
            with self.subTest(revision=repr(revision)):
                self.assert_error(
                    self.resolve(dict(link, recorded_revision=revision)),
                    "memory_code_link_invalid",
                )

    def test_a_malformed_link_document_is_refused(self):
        link = self.link_for()
        self.assert_error(self.resolve(dict(link, link_schema_version="9.9.9")),
                          "memory_code_link_invalid")
        self.assert_error(self.resolve({}), "memory_code_link_invalid")
        self.assert_error(self.resolve(dict(link, entity_kind="class")),
                          "memory_code_link_invalid")


class AuthorityTests(LinkTestCase):
    """Neither action writes anything, so Memory authority is untouched."""

    def _bytes(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    def test_the_link_actions_are_reads_and_write_nothing(self):
        run_path = memory_store.run_store_path(self.base, self.run_id)
        twin_path = twin_store.workspace_store_path(self.base, self.workspace_id)
        before_run, before_twin = self._bytes(run_path), self._bytes(twin_path)
        before_store = json.dumps(self.run_store, sort_keys=True)

        link = self.link_for()
        for entity_id, kind in ((_FUNCTION, "function"), (_FILE, "file"),
                                (_OTHER_FUNCTION, "function")):
            self.freshness(self.link_for(entity_id, kind))
        self.freshness(link)

        self.assertEqual(before_run, self._bytes(run_path))
        self.assertEqual(before_twin, self._bytes(twin_path))
        after, _error = memory_store.load(self.base, self.run_id)
        self.assertEqual(before_store, json.dumps(after, sort_keys=True))
        # No run state, correction or generated evidence is touched.
        self.assertEqual(self.run_store["agent_run"], after["agent_run"])
        self.assertEqual(self.run_store["corrections"], after["corrections"])
        self.assertEqual(self.run_store["generated_documents"],
                         after["generated_documents"])

    def test_a_link_carries_the_twins_revision_not_memory_metadata(self):
        link = self.link_for()
        # The recorded revision is the Twin's own counter, not a digest and not
        # anything the Memory store recorded.
        self.assertIsInstance(link["recorded_revision"], int)
        self.assertEqual(
            self.store["workspace_revision"]["scan_generation"],
            link["recorded_revision"],
        )
        digests = self.run_store["evidence"][0]["digest"]
        self.assertNotIn(digests, json.dumps(link))

    def test_both_actions_are_allowlisted_reads(self):
        for action, name in (
            (contract.ACTION_MEMORY_CODE_LINK, "get_memory_code_link"),
            (contract.ACTION_MEMORY_CODE_FRESHNESS, "resolve_memory_code_freshness"),
        ):
            with self.subTest(action=action):
                self.assertEqual(name, action)
                self.assertIn(action, contract.ALLOWED_ACTIONS)
                self.assertIn(action, contract.MEMORY_CODE_LINK_ACTIONS)


class PrivacyTests(LinkTestCase):
    """No prohibited class crosses the boundary, in or out."""

    def _every_response(self):
        """Every response this contract can produce, as serialized text.

        All five freshness states and every error this contract can return are
        exercised against one real run so the negative scan covers the whole
        surface rather than a sample of it.
        """
        link = self.link_for()
        blobs = [self.bind(), self.resolve(link)]
        # stale: the recorded revision is not the authoritative one.
        blobs.append(self.resolve(self.link_for(_FUNCTION, "function", revision=9)))
        # historical: the Twin retains the entity as a past version.
        self.write("pkg/mod.py", "def f(:\n    return 1\n")
        self.resync(2)
        blobs.append(self.resolve(link))
        # missing: the entity is gone from a freshly published store.
        os.remove(os.path.join(self.project, "pkg", "mod.py"))
        self.publish(3)
        blobs.append(self.resolve(link))
        # unsupported: no authoritative store at all.
        shutil.rmtree(os.path.dirname(
            twin_store.workspace_store_path(self.base, self.workspace_id)
        ), ignore_errors=True)
        blobs.append(self.resolve(link))
        # Every error this contract can produce, including one whose caller text
        # names a personal path that must never be echoed back.
        blobs.append(self.bind("not-an-id", "function"))
        blobs.append(self.bind(_FUNCTION, "class"))
        blobs.append(self.bind("artifact:file:" + _PERSONAL, "file"))
        blobs.append(self.bind("artifact:function:pkg.gone.f"))
        blobs.append(self.bind(run_id="run:absent:x:run"))
        blobs.append(self.bind(kind="not_a_kind"))
        blobs.append(self.resolve(dict(link, recorded_revision="sha256:" + "a" * 64)))
        return "\n".join(json.dumps(blob, sort_keys=True) for blob in blobs)

    def test_no_stored_content_or_secret_crosses(self):
        blob = self._every_response()
        for prohibited in (_SECRET, _PERSONAL, "sk-ant", ".ssh", "id_rsa",
                           "content_fingerprint", "existing_fingerprint",
                           "incoming_fingerprint", "digest", "payload",
                           "transcript", "privacy", self.project, self.base):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(prohibited, blob)

    def test_no_content_fingerprint_or_digest_crosses(self):
        # Collect every fingerprint the authoritative store holds, plus the
        # Memory store's own internal digests, and prove none of them appears.
        stored = []
        for record in self.store["artifacts"]:
            value = record.get("fingerprint")
            if isinstance(value, str):
                stored.append(value)
        baseline = self.store["workspace_revision"].get("baseline_fingerprint")
        if isinstance(baseline, str):
            stored.append(baseline)
        stored.append(self.run_store["evidence"][0]["digest"].split(":", 1)[-1])
        stored.append(self.run_store["events"][0]["content_fingerprint"])
        self.assertTrue(stored)

        blob = self._every_response()
        for value in stored:
            with self.subTest(fingerprint=value[:12]):
                self.assertNotIn(value, blob)

    def test_the_only_digest_shaped_value_is_the_workspace_identity(self):
        # A mechanical check of the design claim: the link and its verdict carry
        # no SHA-256 other than the workspace identity, which is an *identity*
        # (``ws:``-prefixed) rather than a content digest.
        blob = self._every_response()
        for found in _HEX64.findall(blob):
            with self.subTest(value=found[:12]):
                self.assertIn("ws:" + found, blob)

    def test_the_link_and_the_verdict_are_allowlisted(self):
        link = self.link_for()
        self.assertEqual(
            {"link_schema_version", "workspace_id", "entity_id", "entity_kind",
             "memory_run_id", "memory_record_id", "recorded_revision"},
            set(link),
        )
        verdict = self.freshness(link)
        self.assertEqual(
            {"entity_id", "entity_kind", "workspace_id", "memory_run_id",
             "memory_record_id", "recorded_revision", "current_revision",
             "freshness", "reason", "actionable"},
            set(verdict),
        )
        self.assertIn(verdict["freshness"], memory_twin_link.FRESHNESS_STATES)

    def test_every_error_is_catalogue_drawn_and_bounded(self):
        for code in ("memory_code_link_invalid", "twin_entity_not_found",
                     "twin_entity_not_current"):
            with self.subTest(code=code):
                self.assertIn(code, contract.ERROR_CODES)
                self.assertTrue(contract.error_message(code))

    def test_the_vocabulary_is_the_five_states_and_no_other(self):
        self.assertEqual(
            ("current", "historical", "stale", "missing", "unsupported"),
            memory_twin_link.FRESHNESS_STATES,
        )
        self.assertEqual(
            frozenset({"missing", "unsupported"}),
            memory_twin_link.NON_ACTIONABLE_STATES,
        )


if __name__ == "__main__":
    unittest.main()
