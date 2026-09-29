"""Tests for the relocated identity primitives (B1).

Two jobs. The first is ordinary: every digest, fingerprint and identifier in
:mod:`hrca.identity` is asserted against a value derived *outside* the module —
:mod:`hashlib` directly, or a literal golden — so the test would fail if a
relocated body changed by so much as an encoding call.

The second is the point of B1: ``hrca.twin`` re-exports these names rather than
reimplementing them, and these tests hold that. Every legacy ``twin.*`` path
still resolves, and resolves to the *same object* as its ``identity.*``
equivalent — so a stored identifier computed before the relocation still
matches one computed after it, and there is no second implementation to drift.
"""

from __future__ import annotations

import hashlib
import json
import os
import unittest

from hrca import twin
from hrca.core import identity

_HERE = os.path.dirname(os.path.abspath(__file__))
_NONASCII_FIXTURE = os.path.normpath(
    os.path.join(_HERE, "..", "fixtures", "nonascii", "traditional_chinese.txt")
)

# The names this package relocated, and the legacy path each must remain
# reachable through. Held as a literal so adding a symbol to identity.py
# without considering compatibility is not silently covered.
_MOVED = (
    "sha256_hex",
    "fingerprint_bytes",
    "fingerprint_source",
    "workspace_id_for",
    "file_artifact_id",
    "symbol_artifact_id",
    "baseline_fingerprint",
    "ARTIFACT_FILE",
)

# ``_portable`` is not part of the public Twin surface, but ``twin`` itself
# still uses it, so the re-export is load-bearing and is asserted here too.
_MOVED_PRIVATE = ("_portable",)


class Sha256Tests(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            identity.sha256_hex(b""),
        )

    def test_one_byte_input(self):
        self.assertEqual(hashlib.sha256(b"\x00").hexdigest(), identity.sha256_hex(b"\x00"))
        self.assertEqual(
            "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d",
            identity.sha256_hex(b"\x00"),
        )
        self.assertEqual(
            "4bf5122f344554c53bde2ebb8cd2b7e3d1600ad631c385a5d7cce23c7785459a",
            identity.sha256_hex(b"\x01"),
        )

    def test_payload_larger_than_one_mebibyte(self):
        data = bytes(range(256)) * 4096 + b"tail"
        self.assertGreater(len(data), 1024 * 1024)
        self.assertEqual(hashlib.sha256(data).hexdigest(), identity.sha256_hex(data))
        # A single flipped bit anywhere in a payload this size must show up.
        mutated = bytearray(data)
        mutated[-1] ^= 0x01
        self.assertNotEqual(
            identity.sha256_hex(data), identity.sha256_hex(bytes(mutated))
        )

    def test_equality_with_hashlib_over_a_range_of_inputs(self):
        for payload in (b"", b"a", b"abc", b"\x00\xff", b"x" * 1000):
            with self.subTest(payload=payload[:8]):
                self.assertEqual(
                    hashlib.sha256(payload).hexdigest(),
                    identity.sha256_hex(payload),
                )

    def test_the_digest_is_lowercase_hex_of_the_right_width(self):
        digest = identity.sha256_hex(b"anything")
        self.assertEqual(64, len(digest))
        self.assertEqual(digest.lower(), digest)
        self.assertTrue(all(char in "0123456789abcdef" for char in digest))


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_bytes_is_sha256_hex(self):
        for payload in (b"", b"a", b"the source of a module"):
            with self.subTest(payload=payload[:8]):
                self.assertEqual(
                    identity.sha256_hex(payload),
                    identity.fingerprint_bytes(payload),
                )

    def test_fingerprint_source_is_the_utf8_fingerprint_of_the_text(self):
        for text in ("", "a", "import os\n", "naïve"):
            with self.subTest(text=text):
                self.assertEqual(
                    identity.fingerprint_bytes(text.encode("utf-8")),
                    identity.fingerprint_source(text),
                )

    def test_fingerprint_source_distinguishes_text_from_its_repr(self):
        # The encoding is UTF-8, never ``ascii`` with escapes: a non-ASCII
        # source must fingerprint by its bytes, not by a printable rendering.
        text = "中文"
        self.assertEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(),
                         identity.fingerprint_source(text))
        self.assertNotEqual(
            identity.fingerprint_source(text),
            hashlib.sha256(text.encode("unicode_escape")).hexdigest(),
        )

    def test_non_ascii_fixture_fingerprints_by_its_bytes(self):
        if not os.path.isfile(_NONASCII_FIXTURE):
            self.skipTest("the non-ASCII fixture is not present")
        with open(_NONASCII_FIXTURE, "rb") as handle:
            data = handle.read()
        text = data.decode("utf-8")
        self.assertEqual(
            hashlib.sha256(data).hexdigest(), identity.fingerprint_source(text)
        )
        self.assertEqual(identity.fingerprint_bytes(data), identity.fingerprint_source(text))


class IdentifierTests(unittest.TestCase):
    def test_workspace_id_golden_value(self):
        self.assertEqual(
            "ws:816fc349d3faebf805d1bed70fce7e14754cad5251c77dda31c414ee961a0bdd",
            identity.workspace_id_for("/repo"),
        )

    def test_workspace_id_is_the_ws_prefix_over_the_utf8_digest(self):
        for root in ("/repo", "/home/heron/projects/x", "C:/repo", "/"):
            with self.subTest(root=root):
                self.assertEqual(
                    "ws:" + hashlib.sha256(root.encode("utf-8")).hexdigest(),
                    identity.workspace_id_for(root),
                )

    def test_workspace_id_prefix_is_exact(self):
        self.assertTrue(identity.workspace_id_for("/repo").startswith("ws:"))

    def test_file_artifact_id_golden_value_posix(self):
        self.assertEqual(
            "artifact:file:pkg/mod.py", identity.file_artifact_id("pkg/mod.py")
        )

    def test_file_artifact_id_folds_a_windows_path_to_forward_slashes(self):
        # The one platform-dependent behaviour in the moved code: a stored
        # identifier must not depend on which separator the caller wrote.
        self.assertEqual(
            "artifact:file:C:/a/b.py", identity.file_artifact_id("C:\\a\\b.py")
        )
        self.assertEqual(
            identity.file_artifact_id("C:/a/b.py"),
            identity.file_artifact_id("C:\\a\\b.py"),
        )

    def test_file_artifact_id_prefix_is_exact(self):
        self.assertTrue(
            identity.file_artifact_id("a.py").startswith("artifact:file:")
        )

    def test_symbol_artifact_id_golden_value(self):
        self.assertEqual(
            "artifact:function:pkg.mod.Func",
            identity.symbol_artifact_id("pkg.mod.Func", "function"),
        )

    def test_symbol_artifact_id_carries_the_kind_verbatim(self):
        self.assertEqual(
            "artifact:method:pkg.mod.Cls.m",
            identity.symbol_artifact_id("pkg.mod.Cls.m", "method"),
        )

    def test_artifact_file_constant_is_byte_preserved(self):
        self.assertEqual("file", identity.ARTIFACT_FILE)


class BaselineFingerprintTests(unittest.TestCase):
    _FINGERPRINTS = {"a.py": "x", "b.py": "y"}

    def test_golden_value(self):
        self.assertEqual(
            "1f604113c429a99fa93a91ba2b9c6843e95b403d9165c955b5404ceaf0786a8b",
            identity.baseline_fingerprint(self._FINGERPRINTS),
        )

    def test_it_is_the_digest_of_the_sorted_canonical_pairs(self):
        # Derived independently of the implementation, so a change to the
        # canonical form is caught even if the golden is updated with it.
        pairs = sorted(self._FINGERPRINTS.items())
        canonical = json.dumps(
            pairs, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        self.assertEqual(
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            identity.baseline_fingerprint(self._FINGERPRINTS),
        )

    def test_input_order_does_not_matter(self):
        reversed_order = dict(reversed(list(self._FINGERPRINTS.items())))
        self.assertEqual(
            identity.baseline_fingerprint(self._FINGERPRINTS),
            identity.baseline_fingerprint(reversed_order),
        )

    def test_changed_content_changes_the_fingerprint(self):
        self.assertNotEqual(
            identity.baseline_fingerprint(self._FINGERPRINTS),
            identity.baseline_fingerprint({**self._FINGERPRINTS, "a.py": "changed"}),
        )

    def test_an_addition_changes_the_fingerprint(self):
        self.assertNotEqual(
            identity.baseline_fingerprint(self._FINGERPRINTS),
            identity.baseline_fingerprint({**self._FINGERPRINTS, "c.py": "z"}),
        )

    def test_a_removal_changes_the_fingerprint(self):
        self.assertNotEqual(
            identity.baseline_fingerprint(self._FINGERPRINTS),
            identity.baseline_fingerprint({"a.py": "x"}),
        )

    def test_a_null_fingerprint_is_part_of_the_baseline(self):
        # ``None`` means "present but unreadable", which is not the same as
        # absent: the baseline must distinguish them.
        self.assertNotEqual(
            identity.baseline_fingerprint({"a.py": None}),
            identity.baseline_fingerprint({}),
        )
        self.assertNotEqual(
            identity.baseline_fingerprint({"a.py": None}),
            identity.baseline_fingerprint({"a.py": "x"}),
        )

    def test_an_empty_baseline_is_stable(self):
        self.assertEqual(
            identity.baseline_fingerprint({}), identity.baseline_fingerprint({})
        )


class TwinCompatibilityTests(unittest.TestCase):
    """Every legacy path resolves, and to the relocated object itself."""

    def test_every_moved_symbol_is_reachable_through_twin(self):
        for name in _MOVED + _MOVED_PRIVATE:
            with self.subTest(name=name):
                self.assertTrue(hasattr(twin, name), name)

    def test_every_moved_symbol_is_the_same_object_not_a_copy(self):
        for name in _MOVED + _MOVED_PRIVATE:
            with self.subTest(name=name):
                self.assertIs(getattr(identity, name), getattr(twin, name))

    def test_the_legacy_import_forms_still_resolve(self):
        # ``from hrca.twin import sha256_hex`` is the form two modules used
        # before B1; it must keep working.
        from hrca.twin import (  # noqa: F401
            ARTIFACT_FILE,
            baseline_fingerprint,
            file_artifact_id,
            fingerprint_bytes,
            fingerprint_source,
            sha256_hex,
            symbol_artifact_id,
            workspace_id_for,
        )

        self.assertIs(sha256_hex, identity.sha256_hex)
        self.assertIs(ARTIFACT_FILE, identity.ARTIFACT_FILE)

    def test_a_legacy_call_returns_the_identity_value(self):
        self.assertEqual(
            identity.sha256_hex(b"abc"), twin.sha256_hex(b"abc")
        )
        self.assertEqual(
            identity.file_artifact_id("C:\\a\\b.py"),
            twin.file_artifact_id("C:\\a\\b.py"),
        )
        self.assertEqual(
            identity.baseline_fingerprint({"a.py": "x"}),
            twin.baseline_fingerprint({"a.py": "x"}),
        )

    def test_the_twin_retains_its_own_vocabulary(self):
        # The other half of the seam: what was deliberately *not* moved must
        # still be Twin's, or B1 took more than it declared.
        for name in (
            "CONF_HIGH",
            "CONF_LOW",
            "ARTIFACT_CLASS",
            "ARTIFACT_FUNCTION",
            "ARTIFACT_METHOD",
            "ARTIFACT_KINDS",
            "TWIN_SCHEMA_VERSION",
            "MIGRATIONS",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(twin, name), name)
                self.assertFalse(hasattr(identity, name), name)

    def test_artifact_file_is_in_the_twin_kind_set(self):
        self.assertIn(twin.ARTIFACT_FILE, twin.ARTIFACT_KINDS)


if __name__ == "__main__":
    unittest.main()
