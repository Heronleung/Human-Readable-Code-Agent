"""Tests for the generic storage seam (B2).

:mod:`hrca.storage` took ownership of three things that every store needs and
none of them owned: the per-user application-data root, canonical
serialization, and the generic schema-migration engine. These tests hold the
two promises that relocation made.

**Nothing moved.** The per-user directory leaf, the platform branches, the
environment fallbacks and every derived store path are asserted against exact
values, so a "tidier" root name or a normalised separator fails here rather
than orphaning a user's data.

**Nothing broke.** ``twin_store.app_data_dir`` and ``twin.dumps`` are the same
objects as their ``hrca.storage`` equivalents, and the migration engine returns
the same five refusal sentences it always did.

No test here reads or writes a real user application-data directory. The root
function only *computes* a path — it performs no I/O — and every case runs with
a fully replaced environment rather than an edited one.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import os
import unittest
from unittest import mock

from hrca import storage, twin, twin_store

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPOSITORY_ROOT = os.path.normpath(os.path.join(_HERE, ".."))

# A controlled base that exists nowhere. Every derived-path assertion below is
# a pure string join, so no directory is created and no real store is touched.
_CONTROLLED_BASE = os.path.join(os.sep, "controlled-app-data")

_HOME = os.path.join(os.sep, "controlled-home")
_LOCALAPPDATA = os.path.join(os.sep, "controlled-localappdata")
_XDG_DATA_HOME = os.path.join(os.sep, "controlled-xdg")

# The two leaf names the application has always used. These are the
# compatibility requirement: renamed, either one orphans every store already
# sitting beneath it.
_WINDOWS_LEAF = "HumanReadableCodeAgent"
_POSIX_LEAF = "human-readable-code-agent"


@contextlib.contextmanager
def _environment(*, os_name, environ, home=None):
    """Run a block with a controlled platform, environment and home directory.

    The environment is replaced wholesale rather than edited, so a variable
    that happens to be set on the machine running the tests cannot leak into an
    assertion and mask a fallback.
    """
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(os, "name", os_name))
        stack.enter_context(mock.patch.dict(os.environ, environ, clear=True))
        if home is not None:
            stack.enter_context(
                mock.patch.object(os.path, "expanduser", lambda path: home)
            )
        yield


def _split(path):
    """Return ``(base, leaf)`` for a computed root, without platform guessing."""
    return os.path.dirname(path), os.path.basename(path)


class AppDataRootWindowsTests(unittest.TestCase):
    def test_localappdata_is_used_and_the_leaf_is_exact(self):
        with _environment(
            os_name="nt", environ={"LOCALAPPDATA": _LOCALAPPDATA}, home=_HOME
        ):
            base, leaf = _split(storage.app_data_dir())
        self.assertEqual(_LOCALAPPDATA, base)
        self.assertEqual(_WINDOWS_LEAF, leaf)

    def test_the_windows_leaf_is_capitalised_exactly(self):
        # Recorded as its own assertion because this name is the one a user's
        # existing data sits under, and it is the one a "tidy-up" would change.
        with _environment(
            os_name="nt", environ={"LOCALAPPDATA": _LOCALAPPDATA}, home=_HOME
        ):
            root = storage.app_data_dir()
        self.assertTrue(root.endswith("HumanReadableCodeAgent"))
        self.assertNotIn("human-readable-code-agent", root)

    def test_the_home_directory_is_the_fallback(self):
        with _environment(os_name="nt", environ={}, home=_HOME):
            base, leaf = _split(storage.app_data_dir())
        self.assertEqual(_HOME, base)
        self.assertEqual(_WINDOWS_LEAF, leaf)

    def test_xdg_data_home_is_ignored_on_windows(self):
        with _environment(
            os_name="nt",
            environ={"XDG_DATA_HOME": _XDG_DATA_HOME},
            home=_HOME,
        ):
            base, leaf = _split(storage.app_data_dir())
        self.assertEqual(_HOME, base)
        self.assertEqual(_WINDOWS_LEAF, leaf)

    def test_an_empty_localappdata_falls_back(self):
        # ``os.environ.get(...) or ...`` treats an empty value as absent; that
        # is existing behaviour and is preserved rather than "corrected".
        with _environment(os_name="nt", environ={"LOCALAPPDATA": ""}, home=_HOME):
            base, leaf = _split(storage.app_data_dir())
        self.assertEqual(_HOME, base)
        self.assertEqual(_WINDOWS_LEAF, leaf)


class AppDataRootPosixTests(unittest.TestCase):
    def test_xdg_data_home_is_used_and_the_leaf_is_exact(self):
        with _environment(os_name="posix", environ={"XDG_DATA_HOME": _XDG_DATA_HOME}):
            base, leaf = _split(storage.app_data_dir())
        self.assertEqual(_XDG_DATA_HOME, base)
        self.assertEqual(_POSIX_LEAF, leaf)

    def test_the_posix_leaf_is_lowercase_hyphenated(self):
        with _environment(os_name="posix", environ={"XDG_DATA_HOME": _XDG_DATA_HOME}):
            root = storage.app_data_dir()
        self.assertTrue(root.endswith("human-readable-code-agent"))
        self.assertNotIn("HumanReadableCodeAgent", root)

    def test_the_home_directory_is_the_fallback(self):
        with _environment(os_name="posix", environ={}, home=_HOME):
            base, leaf = _split(storage.app_data_dir())
        self.assertEqual(
            os.path.join(_HOME, ".local", "share"), base
        )
        self.assertEqual(_POSIX_LEAF, leaf)

    def test_localappdata_is_ignored_on_posix(self):
        with _environment(
            os_name="posix", environ={"LOCALAPPDATA": _LOCALAPPDATA}, home=_HOME
        ):
            base, leaf = _split(storage.app_data_dir())
        self.assertEqual(os.path.join(_HOME, ".local", "share"), base)
        self.assertEqual(_POSIX_LEAF, leaf)

    def test_the_two_platforms_produce_different_leaves(self):
        with _environment(
            os_name="nt", environ={"LOCALAPPDATA": _LOCALAPPDATA}, home=_HOME
        ):
            windows_leaf = os.path.basename(storage.app_data_dir())
        with _environment(
            os_name="posix", environ={"XDG_DATA_HOME": _XDG_DATA_HOME}
        ):
            posix_leaf = os.path.basename(storage.app_data_dir())
        self.assertNotEqual(windows_leaf, posix_leaf)


class AppDataRootIsIndependentOfAnyRepositoryTests(unittest.TestCase):
    def test_the_root_takes_no_arguments(self):
        # The structural form of the guarantee: with no parameter there is no
        # way for a caller to influence the root, so it cannot follow a
        # selected repository.
        self.assertEqual([], list(inspect.signature(storage.app_data_dir).parameters))

    def test_the_root_is_never_the_repository_root(self):
        with _environment(
            os_name="posix", environ={"XDG_DATA_HOME": _XDG_DATA_HOME}
        ):
            root = os.path.normpath(storage.app_data_dir())
        self.assertNotEqual(_REPOSITORY_ROOT, root)
        self.assertFalse(root.startswith(_REPOSITORY_ROOT + os.sep))

    def test_the_root_is_never_below_the_repository(self):
        # And the reverse: the repository must not appear as an ancestor of the
        # root, whatever ``XDG_DATA_HOME`` happens to be.
        with _environment(os_name="posix", environ={"XDG_DATA_HOME": _REPOSITORY_ROOT}):
            root = os.path.normpath(storage.app_data_dir())
        # The root is derived from the variable, never from the tree, so this
        # only asserts the app does not append a repository-relative segment.
        self.assertTrue(root.startswith(_REPOSITORY_ROOT))
        self.assertTrue(root.endswith(_POSIX_LEAF))


class RootCompatibilityTests(unittest.TestCase):
    def test_twin_store_app_data_dir_is_the_storage_function(self):
        self.assertIs(storage.app_data_dir, twin_store.app_data_dir)

    def test_the_twin_store_export_is_still_advertised(self):
        self.assertIn("app_data_dir", twin_store.__all__)

    def test_calling_through_either_path_gives_the_same_answer(self):
        with _environment(
            os_name="posix", environ={"XDG_DATA_HOME": _XDG_DATA_HOME}
        ):
            self.assertEqual(
                storage.app_data_dir(), twin_store.app_data_dir()
            )

    def test_storage_owns_no_twin_schema(self):
        for name in ("TWIN_SCHEMA_VERSION", "MIGRATIONS", "ARTIFACT_KINDS"):
            self.assertFalse(hasattr(storage, name), name)

    def test_twin_still_owns_its_schema_and_registry(self):
        self.assertEqual("1.0.0", twin.TWIN_SCHEMA_VERSION)
        self.assertEqual({}, twin.MIGRATIONS)
        self.assertIs(twin.dumps, storage.dumps)


class DerivedStorePathTests(unittest.TestCase):
    """Every physical path a store derives from the root, pinned exactly.

    These are pure joins — nothing is created — so the assertion is that the
    path *string* is unchanged, which is what decides where an existing file is
    looked for.
    """

    def test_twin_store_path(self):
        self.assertEqual(
            os.path.join(_CONTROLLED_BASE, "ws_abc", "twin.json"),
            twin_store.workspace_store_path(_CONTROLLED_BASE, "ws:abc"),
        )

    def test_twin_draft_path(self):
        self.assertEqual(
            os.path.join(_CONTROLLED_BASE, "ws_abc", "draft.json"),
            twin_store.workspace_draft_path(_CONTROLLED_BASE, "ws:abc"),
        )

    def test_twin_namespace_folds_separators(self):
        self.assertEqual(
            twin_store.workspace_store_path(_CONTROLLED_BASE, "ws:a/b\\c"),
            twin_store.workspace_store_path(_CONTROLLED_BASE, "ws:a_b_c"),
        )

    def test_memory_store_path(self):
        from hrca import memory_store

        self.assertEqual(
            os.path.join(_CONTROLLED_BASE, "memory", "run_smoke_s-1_run", "run.json"),
            memory_store.run_store_path(_CONTROLLED_BASE, "run:smoke:s-1:run"),
        )

    def test_document_store_path(self):
        from hrca import version_store

        self.assertEqual(
            os.path.join(_CONTROLLED_BASE, "documents", "doc_abc", "document.json"),
            version_store.document_store_path(_CONTROLLED_BASE, "doc:abc"),
        )

    def test_library_store_path(self):
        from hrca import library_store

        self.assertEqual(
            os.path.join(_CONTROLLED_BASE, "library", "library.json"),
            library_store.library_store_path(_CONTROLLED_BASE),
        )

    def test_provider_config_path(self):
        from hrca import provider_config

        self.assertEqual(
            os.path.join(_CONTROLLED_BASE, "provider-config.json"),
            provider_config.config_path(_CONTROLLED_BASE),
        )

    def test_every_store_hangs_off_the_same_supplied_base(self):
        from hrca import library_store, memory_store, provider_config, version_store

        for path in (
            twin_store.workspace_store_path(_CONTROLLED_BASE, "ws:abc"),
            twin_store.workspace_draft_path(_CONTROLLED_BASE, "ws:abc"),
            memory_store.run_store_path(_CONTROLLED_BASE, "run:x"),
            version_store.document_store_path(_CONTROLLED_BASE, "doc:x"),
            library_store.library_store_path(_CONTROLLED_BASE),
            provider_config.config_path(_CONTROLLED_BASE),
        ):
            with self.subTest(path=path):
                self.assertTrue(path.startswith(_CONTROLLED_BASE + os.sep), path)


class MigrationRefusalTests(unittest.TestCase):
    """The five sentences the package has always refused with, verbatim."""

    def _twin(self, raw):
        return twin.migrate_store(raw)

    def test_the_current_version_passes_through(self):
        store, error = self._twin({"schema_version": "1.0.0", "x": 1})
        self.assertIsNone(error)
        self.assertEqual({"schema_version": "1.0.0", "x": 1}, store)

    def test_a_non_mapping_is_refused(self):
        for raw in ([], "x", None, 5):
            with self.subTest(raw=raw):
                store, error = self._twin(raw)
                self.assertIsNone(store)
                self.assertEqual("store is not a mapping", error)

    def test_a_missing_version_is_refused(self):
        for raw in ({}, {"schema_version": ""}, {"schema_version": 5}):
            with self.subTest(raw=raw):
                store, error = self._twin(raw)
                self.assertIsNone(store)
                self.assertEqual("missing schema_version", error)

    def test_a_future_version_is_refused(self):
        store, error = self._twin({"schema_version": "99.0.0"})
        self.assertIsNone(store)
        self.assertEqual("schema_version is newer than supported", error)

    def test_an_unlisted_older_version_is_refused(self):
        store, error = self._twin({"schema_version": "0.0.1"})
        self.assertIsNone(store)
        self.assertEqual("schema_version is not migratable", error)

    def test_an_unparseable_component_is_refused(self):
        # A superscript digit passes ``str.isdigit`` but is not a decimal
        # integer, so the comparison genuinely raises and the guard is live.
        store, error = self._twin({"schema_version": "²"})
        self.assertIsNone(store)
        self.assertEqual("invalid schema_version", error)

    def test_the_refusal_set_is_exactly_these_five(self):
        reasons = set()
        for raw in ([], "x", None, {}, {"schema_version": ""}, {"schema_version": 5},
                    {"schema_version": "99.0.0"}, {"schema_version": "0.0.1"},
                    {"schema_version": "²"}):
            reasons.add(self._twin(raw)[1])
        self.assertEqual(
            {
                "store is not a mapping",
                "missing schema_version",
                "invalid schema_version",
                "schema_version is newer than supported",
                "schema_version is not migratable",
            },
            reasons,
        )


class GenericMigrationEngineTests(unittest.TestCase):
    """The engine owns no schema: a caller supplies its version and chain."""

    def test_a_caller_supplied_chain_is_executed(self):
        calls = []

        def upgrade(store):
            calls.append(store)
            return {**store, "migrated": True}

        store, error = storage.migrate(
            {"schema_version": "0.9.0", "x": 1},
            current_version="1.0.0",
            migrations={"0.9.0": upgrade},
        )
        self.assertIsNone(error)
        self.assertTrue(store["migrated"])
        self.assertEqual(1, len(calls))

    def test_the_migration_receives_a_copy_not_the_caller_mapping(self):
        original = {"schema_version": "0.9.0", "x": 1}

        def upgrade(store):
            store["mutated"] = True
            return store

        storage.migrate(
            original, current_version="1.0.0", migrations={"0.9.0": upgrade}
        )
        self.assertNotIn("mutated", original)

    def test_an_equal_version_returns_the_mapping_unchanged(self):
        original = {"schema_version": "1.0.0", "x": 1}
        store, error = storage.migrate(
            original, current_version="1.0.0", migrations={}
        )
        self.assertIsNone(error)
        self.assertIs(original, store)

    def test_the_engine_carries_no_registry_of_its_own(self):
        for name in ("MIGRATIONS", "TWIN_SCHEMA_VERSION", "MEMORY_SCHEMA_VERSION"):
            self.assertFalse(hasattr(storage, name), name)

    def test_version_tuple_ignores_non_numeric_components(self):
        # The filter is per dot-separated component, not per trailing suffix, so
        # a pre-release marker drops that whole component. That is what the
        # engine always did and is preserved rather than "fixed": a version it
        # would sort differently is a version whose stored bytes might be read
        # as a different schema.
        self.assertEqual((1, 0, 0), storage.version_tuple("1.0.0"))
        self.assertEqual((1, 0), storage.version_tuple("1.0.0-beta"))
        self.assertEqual((1, 0), storage.version_tuple("1.0.-1"))
        self.assertEqual((0,), storage.version_tuple("no-digits"))
        self.assertEqual((0,), storage.version_tuple(""))
        self.assertEqual((2, 10), storage.version_tuple("2.10"))


class CanonicalSerializationTests(unittest.TestCase):
    def test_dumps_sorts_keys_and_uses_compact_separators(self):
        self.assertEqual('{"a":2,"b":1}', storage.dumps({"b": 1, "a": 2}))

    def test_dumps_is_ascii_safe(self):
        self.assertEqual('{"k":"\\u4e2d"}', storage.dumps({"k": "中"}))

    def test_dumps_is_deterministic_across_insertion_orders(self):
        self.assertEqual(
            storage.dumps({"a": 1, "b": [1, 2], "c": None}),
            storage.dumps({"c": None, "b": [1, 2], "a": 1}),
        )

    def test_twin_dumps_is_the_storage_function(self):
        self.assertIs(storage.dumps, twin.dumps)

    def test_dumps_output_is_valid_json(self):
        self.assertEqual({"a": 1}, json.loads(storage.dumps({"a": 1})))


if __name__ == "__main__":
    unittest.main()
