"""Source-evidence read-seam tests (B-T2A).

Three questions, kept apart because they fail for different reasons.

**Golden.** Do the accessors return the *values a record holds*? These cases are
hand-written documents, authored against the read model's own contract and not
produced by running it, so a change that quietly normalises, coerces or defaults
a value fails here even though every consumer would still appear to work.

**Refusal points.** Which accessor answers decide a refusal? Each case states the
fact that moved and shows the accessor reporting it, so the refusal a caller
reaches is traceable to a *read* rather than to an accident of the seam.

**Bridge.** Does the seam read a document the Twin really produced? The accepted
corpus is scanned and reconciled through the real :func:`hrca.twin.sync_twin`,
and every fact the seam exposes is compared against the record it came from.

The first two classes deliberately import no Twin module: a read model tested
only against its producer's output cannot show that it reads the *document*
rather than agreeing with the Twin by construction. The bridge test exists to
close the other half of that gap.
"""

from __future__ import annotations

import os
import unittest

from hrca import source_evidence, twin
from hrca.core import identity
from hrca.source import scanner

_HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.normpath(os.path.join(_HERE, "..", "fixtures"))
_PY_SUFFIXES = (".py", ".pyi")

# -- hand-written records --------------------------------------------------
#
# The shapes the seam reads, written out rather than produced. The file artifact
# carries a fingerprint and no locator; the symbol artifact carries a locator and
# no fingerprint. Neither asymmetry is an accident of the seam — it is the shape
# of the records, and the accessors report it rather than smoothing it over.

_FILE_ARTIFACT = {
    "record_type": "artifact",
    "id": "artifact:file:app/service.py",
    "kind": identity.ARTIFACT_FILE,
    "path": "app/service.py",
    "name": "service.py",
    "module": "app.service",
    "fingerprint": "ab" * 32,
    "syntax_status": "ok",
    "confidence": source_evidence.CONF_HIGH,
    "sync_state": "synchronized",
}

_SYMBOL_ARTIFACT = {
    "record_type": "artifact",
    "id": "artifact:class:app.service.Service",
    "kind": "class",
    "path": "app/service.py",
    "module": "app.service",
    "name": "Service",
    "locator": "app.service.Service",
    "confidence": source_evidence.CONF_HIGH,
    "sync_state": "synchronized",
}

_BASELINE = {
    "workspace_id": "ws:abc",
    "scan_generation": 7,
    "baseline_fingerprint": "cd" * 32,
}

_STORE = {
    "workspace_revision": dict(_BASELINE, sync_state="synchronized"),
    "artifacts": [
        _FILE_ARTIFACT,
        _SYMBOL_ARTIFACT,
        {"kind": "class"},
        {"id": ""},
        "not a record",
    ],
}


class AccessorGoldenTests(unittest.TestCase):
    """Every accessor returns what the record holds, verbatim."""

    def test_the_workspace_baseline_is_returned_whole(self):
        self.assertEqual(_STORE["workspace_revision"],
                         source_evidence.workspace_revision(_STORE))

    def test_every_workspace_fact_is_returned(self):
        self.assertEqual("ws:abc", source_evidence.workspace_id(_STORE))
        self.assertEqual(7, source_evidence.scan_generation(_STORE))
        self.assertEqual("cd" * 32, source_evidence.baseline_fingerprint(_STORE))

    def test_every_file_artifact_fact_is_returned(self):
        artifact = _FILE_ARTIFACT
        self.assertEqual(artifact["id"], source_evidence.artifact_id(artifact))
        self.assertEqual(artifact["kind"], source_evidence.artifact_kind(artifact))
        self.assertEqual(artifact["path"], source_evidence.artifact_path(artifact))
        self.assertEqual(artifact["module"], source_evidence.artifact_module(artifact))
        self.assertEqual(artifact["name"], source_evidence.artifact_name(artifact))
        self.assertEqual(artifact["fingerprint"],
                         source_evidence.artifact_fingerprint(artifact))
        self.assertTrue(source_evidence.is_file_artifact(artifact))

    def test_every_symbol_artifact_fact_is_returned(self):
        artifact = _SYMBOL_ARTIFACT
        self.assertEqual(artifact["id"], source_evidence.artifact_id(artifact))
        self.assertEqual(artifact["kind"], source_evidence.artifact_kind(artifact))
        self.assertEqual(artifact["path"], source_evidence.artifact_path(artifact))
        self.assertEqual(artifact["module"], source_evidence.artifact_module(artifact))
        self.assertEqual(artifact["name"], source_evidence.artifact_name(artifact))
        self.assertEqual(artifact["locator"], source_evidence.artifact_locator(artifact))
        self.assertFalse(source_evidence.is_file_artifact(artifact))

    def test_the_two_artifact_shapes_are_not_smoothed_over(self):
        # A file artifact has no locator and a symbol artifact has no
        # fingerprint. The seam reports the absence; it never derives one from
        # the other, because a derived value would be a claim the record does
        # not make.
        self.assertIsNone(source_evidence.artifact_locator(_FILE_ARTIFACT))
        self.assertIsNone(source_evidence.artifact_fingerprint(_SYMBOL_ARTIFACT))

    def test_artifacts_keeps_only_records_carrying_an_identity(self):
        self.assertEqual(
            [_FILE_ARTIFACT, _SYMBOL_ARTIFACT], source_evidence.artifacts(_STORE)
        )

    def test_artifacts_preserves_document_order(self):
        reversed_store = {"artifacts": [_SYMBOL_ARTIFACT, _FILE_ARTIFACT]}
        self.assertEqual(
            [_SYMBOL_ARTIFACT, _FILE_ARTIFACT],
            source_evidence.artifacts(reversed_store),
        )

    def test_values_are_never_coerced(self):
        # Whatever a malformed document put there is what a caller compares
        # against, which is what keeps a refusal the same refusal it was before
        # the read moved behind this seam.
        store = {
            "workspace_revision": {
                "workspace_id": 5,
                "scan_generation": True,
                "baseline_fingerprint": ["x"],
            }
        }
        self.assertEqual(5, source_evidence.workspace_id(store))
        self.assertIs(True, source_evidence.scan_generation(store))
        self.assertEqual(["x"], source_evidence.baseline_fingerprint(store))

    def test_a_missing_fact_reads_as_absent_even_inside_a_present_baseline(self):
        store = {"workspace_revision": {"scan_generation": 7}}
        self.assertIsNotNone(source_evidence.workspace_revision(store))
        self.assertIsNone(source_evidence.workspace_id(store))
        self.assertIsNone(source_evidence.baseline_fingerprint(store))

    def test_a_document_with_no_baseline_has_no_baseline(self):
        for store in (
            {},
            {"workspace_revision": None},
            {"workspace_revision": "not a mapping"},
            {"workspace_revision": []},
            None,
            "not a mapping",
            [],
        ):
            with self.subTest(store=store):
                self.assertIsNone(source_evidence.workspace_revision(store))
                self.assertIsNone(source_evidence.workspace_id(store))
                self.assertIsNone(source_evidence.scan_generation(store))
                self.assertIsNone(source_evidence.baseline_fingerprint(store))

    def test_a_document_with_no_artifacts_has_no_artifacts(self):
        for store in ({}, {"artifacts": None}, {"artifacts": []}, None, "x", []):
            with self.subTest(store=store):
                self.assertEqual([], source_evidence.artifacts(store))

    def test_a_malformed_artifact_record_reads_as_absent(self):
        for artifact in (None, "x", 5, [], {}):
            with self.subTest(artifact=artifact):
                self.assertIsNone(source_evidence.artifact_id(artifact))
                self.assertIsNone(source_evidence.artifact_kind(artifact))
                self.assertIsNone(source_evidence.artifact_path(artifact))
                self.assertIsNone(source_evidence.artifact_module(artifact))
                self.assertIsNone(source_evidence.artifact_name(artifact))
                self.assertIsNone(source_evidence.artifact_locator(artifact))
                self.assertIsNone(source_evidence.artifact_fingerprint(artifact))
                self.assertFalse(source_evidence.is_file_artifact(artifact))

    def test_only_a_file_kind_is_a_file_artifact(self):
        for kind in ("class", "function", "method", None, 5, ""):
            with self.subTest(kind=kind):
                self.assertFalse(source_evidence.is_file_artifact({"kind": kind}))


class RefusalPointTests(unittest.TestCase):
    """The accessors a binding refusal is made of, one fact at a time."""

    def _store(self, **baseline):
        return {"workspace_revision": dict(_BASELINE, **baseline)}

    def test_a_differing_workspace_identity_is_the_fact_that_moved(self):
        store = self._store(workspace_id="ws:elsewhere")
        self.assertNotEqual(_BASELINE["workspace_id"],
                            source_evidence.workspace_id(store))
        self.assertEqual(_BASELINE["scan_generation"],
                         source_evidence.scan_generation(store))
        self.assertEqual(_BASELINE["baseline_fingerprint"],
                         source_evidence.baseline_fingerprint(store))

    def test_a_differing_scan_generation_is_the_fact_that_moved(self):
        store = self._store(scan_generation=8)
        self.assertEqual(_BASELINE["workspace_id"],
                         source_evidence.workspace_id(store))
        self.assertNotEqual(_BASELINE["scan_generation"],
                            source_evidence.scan_generation(store))
        self.assertEqual(_BASELINE["baseline_fingerprint"],
                         source_evidence.baseline_fingerprint(store))

    def test_a_differing_baseline_fingerprint_is_the_fact_that_moved(self):
        store = self._store(baseline_fingerprint="00" * 32)
        self.assertEqual(_BASELINE["workspace_id"],
                         source_evidence.workspace_id(store))
        self.assertEqual(_BASELINE["scan_generation"],
                         source_evidence.scan_generation(store))
        self.assertNotEqual(_BASELINE["baseline_fingerprint"],
                            source_evidence.baseline_fingerprint(store))

    def test_an_absent_baseline_is_absence_rather_than_a_mismatch(self):
        # A document that cannot say which revision it is is not evidence for a
        # *different* workspace; it is no evidence. This is the read that tells
        # those two answers apart, and it is why the seam exposes the record
        # itself and not only its facts.
        for store in ({}, {"workspace_revision": None}, {"workspace_revision": "x"}):
            with self.subTest(store=store):
                self.assertIsNone(source_evidence.workspace_revision(store))

    def test_the_facts_of_a_present_baseline_are_never_absent_together(self):
        store = self._store()
        self.assertIsNotNone(source_evidence.workspace_revision(store))
        self.assertIsNotNone(source_evidence.workspace_id(store))
        self.assertIsNotNone(source_evidence.scan_generation(store))
        self.assertIsNotNone(source_evidence.baseline_fingerprint(store))


def _fingerprints(root, doc):
    """Return ``{path: content fingerprint}`` for every supported corpus file."""
    out = {}
    for record in doc["files"]:
        path = record["path"]
        if not path.endswith(_PY_SUFFIXES):
            continue
        try:
            with open(os.path.join(root, path), "rb") as fh:
                out[path] = identity.fingerprint_bytes(fh.read())
        except OSError:  # pragma: no cover - the corpus is readable
            out[path] = None
    return out


def _sync(previous):
    """Reconcile the frozen corpus through the real ``twin.sync_twin``."""
    doc = scanner.scan_directory(FIXTURES)
    return twin.sync_twin(
        doc,
        _fingerprints(FIXTURES, doc),
        previous,
        twin.workspace_id_for(doc["root"]),
        1,
        "T",
    )


class TwinBridgeTests(unittest.TestCase):
    """The seam reads a document the Twin really produced."""

    @classmethod
    def setUpClass(cls):
        cls.store, cls.result = _sync(None)

    def test_the_bridge_reads_a_real_reconciliation(self):
        self.assertEqual(twin.SYNC_SYNCHRONIZED, self.result["state"])
        self.assertEqual(twin.TWIN_SCHEMA_VERSION, self.store["schema_version"])

    def test_every_artifact_fact_matches_the_record_it_came_from(self):
        records = source_evidence.artifacts(self.store)
        self.assertEqual(len(self.store["artifacts"]), len(records))
        for record in records:
            with self.subTest(artifact=record["id"]):
                self.assertEqual(record["id"], source_evidence.artifact_id(record))
                self.assertEqual(record["kind"], source_evidence.artifact_kind(record))
                self.assertEqual(record.get("path"),
                                 source_evidence.artifact_path(record))
                self.assertEqual(record.get("module"),
                                 source_evidence.artifact_module(record))
                self.assertEqual(record.get("name"),
                                 source_evidence.artifact_name(record))
                self.assertEqual(record.get("locator"),
                                 source_evidence.artifact_locator(record))
                self.assertEqual(record.get("fingerprint"),
                                 source_evidence.artifact_fingerprint(record))

    def test_the_baseline_facts_match_the_record_they_came_from(self):
        revision = self.store["workspace_revision"]
        self.assertEqual(revision, source_evidence.workspace_revision(self.store))
        self.assertEqual(revision["workspace_id"],
                         source_evidence.workspace_id(self.store))
        self.assertEqual(revision["scan_generation"],
                         source_evidence.scan_generation(self.store))
        self.assertEqual(revision["baseline_fingerprint"],
                         source_evidence.baseline_fingerprint(self.store))

    def test_the_corpus_presents_both_artifact_shapes(self):
        records = source_evidence.artifacts(self.store)
        files = [r for r in records if source_evidence.is_file_artifact(r)]
        symbols = [r for r in records if not source_evidence.is_file_artifact(r)]
        self.assertTrue(files, "the corpus holds no file artifact")
        self.assertTrue(symbols, "the corpus holds no symbol artifact")
        for record in files:
            self.assertIsInstance(source_evidence.artifact_fingerprint(record), str)
            self.assertIsNone(source_evidence.artifact_locator(record))
        for record in symbols:
            self.assertIsInstance(source_evidence.artifact_locator(record), str)
            self.assertIsNone(source_evidence.artifact_fingerprint(record))

    def test_a_reconciled_store_reads_the_same_way_as_a_first_sync(self):
        reconciled, result = _sync(self.store)
        self.assertEqual(twin.SYNC_NO_CHANGE, result["state"])
        self.assertEqual(
            source_evidence.workspace_revision(self.store),
            source_evidence.workspace_revision(reconciled),
        )
        self.assertEqual(
            [r["id"] for r in source_evidence.artifacts(self.store)],
            [r["id"] for r in source_evidence.artifacts(reconciled)],
        )

    def test_the_confidence_vocabulary_is_one_object_across_both_modules(self):
        self.assertIs(twin.CONF_HIGH, source_evidence.CONF_HIGH)
        self.assertIs(twin.CONF_LOW, source_evidence.CONF_LOW)
        self.assertEqual("high", source_evidence.CONF_HIGH)
        self.assertEqual("low", source_evidence.CONF_LOW)

    def test_the_confidence_vocabulary_matches_what_the_records_carry(self):
        levels = set()
        for field in ("artifacts",):
            for record in self.store[field]:
                if "confidence" in record:
                    levels.add(record["confidence"])
        self.assertTrue(levels <= {source_evidence.CONF_HIGH, source_evidence.CONF_LOW})


if __name__ == "__main__":
    unittest.main()
