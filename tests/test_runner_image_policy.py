"""The code-owned runner-image setup policy (P5.5a-r3c).

These tests are pure: no Docker, no network, no filesystem beyond reading the
repository's own Dockerfile. They pin the decisions a setup makes *before* it
touches anything — which base may be built from, which manifest an index may
offer, when an OnBuild value gates readiness — and they pin the agreement
between this policy and the constants the runner and the validation policy
already own, so a re-pin that misses one of them fails here rather than in a
quietly different artifact.
"""

from __future__ import annotations

import os
import unittest

from hrca.authoring import validation_policy
from hrca.execution import container_runner, runner_image_policy as policy

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(_HERE, ".."))

_OTHER_DIGEST = "sha256:" + "0" * 64


def _index(*manifests):
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "digest": policy.BASE_INDEX_DIGEST,
        "manifests": list(manifests),
    }


def _entry(architecture, digest, media_type="application/vnd.oci.image.manifest.v1+json",
           os_name="linux", annotations=None, variant=None):
    platform = {"os": os_name, "architecture": architecture}
    if variant is not None:
        platform["variant"] = variant
    entry = {"mediaType": media_type, "digest": digest, "platform": platform}
    if annotations is not None:
        entry["annotations"] = annotations
    return entry


class BaseReferenceTests(unittest.TestCase):
    def test_the_pinned_base_is_accepted(self):
        parts, reason = policy.verify_base_reference(policy.BASE_REFERENCE)
        self.assertIsNone(reason)
        self.assertEqual(policy.BASE_IMAGE, parts["name"])
        self.assertEqual(policy.BASE_INDEX_DIGEST, parts["digest"])
        self.assertEqual(
            "docker.io/library/python:3.12-slim", parts["canonical_name"]
        )

    def test_an_official_image_name_is_canonicalised_the_way_the_engine_does(self):
        # The engine resolves ``python`` to ``docker.io/library/python``; a
        # policy that did not would refuse the very reference it pins.
        expected = "docker.io/library/python:3.12-slim@%s" % policy.BASE_INDEX_DIGEST
        self.assertEqual(expected, policy.parse_reference(policy.BASE_REFERENCE)[0][
            "canonical_reference"
        ])

    def test_a_tag_only_reference_is_refused_as_mutable(self):
        parts, reason = policy.verify_base_reference(policy.BASE_IMAGE)
        self.assertIsNone(parts)
        self.assertEqual(policy.REASON_BASE_TAG_ONLY, reason)

    def test_a_foreign_digest_is_refused_as_a_mismatch(self):
        parts, reason = policy.verify_base_reference(
            policy.BASE_IMAGE + "@" + _OTHER_DIGEST
        )
        self.assertIsNone(parts)
        self.assertEqual(policy.REASON_BASE_MISMATCH, reason)

    def test_another_image_at_the_same_digest_is_refused_by_name(self):
        parts, reason = policy.verify_base_reference(
            "python:3.11-slim@" + policy.BASE_INDEX_DIGEST
        )
        self.assertIsNone(parts)
        self.assertEqual(policy.REASON_BASE_NAME, reason)

    def test_a_foreign_registry_is_refused(self):
        parts, reason = policy.verify_base_reference(
            "ghcr.io/library/python:3.12-slim@" + policy.BASE_INDEX_DIGEST
        )
        self.assertIsNone(parts)
        self.assertEqual(policy.REASON_REGISTRY_HOST, reason)

    def test_malformed_and_non_text_references_are_refused(self):
        for text, reason in (
            (None, policy.REASON_BASE_NOT_TEXT),
            (12, policy.REASON_BASE_NOT_TEXT),
            ("", policy.REASON_BASE_NOT_TEXT),
            ("   ", policy.REASON_BASE_NOT_TEXT),
            ("python:3.12-slim@latest", policy.REASON_BASE_NOT_DIGEST),
            ("python:3.12-slim@sha256:nothex", policy.REASON_BASE_NOT_DIGEST),
            ("@sha256:" + "0" * 64, policy.REASON_BASE_NOT_DIGEST),
        ):
            with self.subTest(text=text):
                parts, got = policy.verify_base_reference(text)
                self.assertIsNone(parts)
                self.assertEqual(reason, got)

    def test_a_sha256_short_digest_is_not_a_digest(self):
        self.assertFalse(policy.is_digest("sha256:" + "a" * 63))
        self.assertFalse(policy.is_digest("sha256:" + "A" * 64))
        self.assertFalse(policy.is_digest("sha512:" + "a" * 64))
        self.assertTrue(policy.is_digest("sha256:" + "a" * 64))


class PlatformTests(unittest.TestCase):
    def test_the_engine_vocabulary_is_mapped_to_the_oci_one(self):
        self.assertEqual("amd64", policy.normalize_architecture("x86_64"))
        self.assertEqual("amd64", policy.normalize_architecture("amd64"))
        self.assertEqual("arm64", policy.normalize_architecture("aarch64"))
        self.assertEqual("arm64", policy.normalize_architecture("ARM64"))

    def test_an_unknown_architecture_is_refused_rather_than_guessed(self):
        for value in (None, "", "sparc", 7):
            with self.subTest(value=value):
                self.assertIsNone(policy.normalize_architecture(value))

    def test_exactly_one_platform_manifest_is_selected(self):
        manifest_digest = "sha256:" + "1" * 64
        selected, reason = policy.select_platform_manifest(
            _index(
                _entry("amd64", manifest_digest),
                _entry("arm64", "sha256:" + "2" * 64, variant="v8"),
            ),
            "linux",
            "amd64",
        )
        self.assertIsNone(reason)
        self.assertEqual(manifest_digest, selected["digest"])

    def test_an_attestation_entry_can_never_be_selected(self):
        # Attestations ride beside the real platform manifests and declare no
        # usable platform; the arm64 entry here is only an also-ran.
        selected, reason = policy.select_platform_manifest(
            _index(
                _entry(
                    "unknown",
                    "sha256:" + "3" * 64,
                    os_name="unknown",
                    annotations={
                        "vnd.docker.reference.type": policy.ATTESTATION_TYPE,
                        "vnd.docker.reference.digest": "sha256:" + "1" * 64,
                    },
                ),
                _entry("amd64", "sha256:" + "1" * 64),
            ),
            "linux",
            "amd64",
        )
        self.assertIsNone(reason)
        self.assertEqual("sha256:" + "1" * 64, selected["digest"])

    def test_an_index_without_the_platform_is_refused(self):
        selected, reason = policy.select_platform_manifest(
            _index(_entry("arm64", "sha256:" + "2" * 64, variant="v8")),
            "linux",
            "amd64",
        )
        self.assertIsNone(selected)
        self.assertEqual(policy.REASON_PLATFORM_MISSING, reason)

    def test_an_ambiguous_index_is_refused_rather_than_resolved_by_order(self):
        selected, reason = policy.select_platform_manifest(
            _index(
                _entry("amd64", "sha256:" + "1" * 64),
                _entry("amd64", "sha256:" + "2" * 64),
            ),
            "linux",
            "amd64",
        )
        self.assertIsNone(selected)
        self.assertEqual(policy.REASON_PLATFORM_AMBIGUOUS, reason)

    def test_an_entry_without_a_usable_digest_is_not_a_candidate(self):
        selected, reason = policy.select_platform_manifest(
            _index(_entry("amd64", "not-a-digest")), "linux", "amd64"
        )
        self.assertIsNone(selected)
        self.assertEqual(policy.REASON_PLATFORM_MISSING, reason)

    def test_a_non_index_document_is_refused(self):
        for value in (None, [], {}, {"manifests": "nope"}):
            with self.subTest(value=value):
                selected, reason = policy.select_platform_manifest(value, "linux", "amd64")
                self.assertIsNone(selected)
                self.assertEqual(policy.REASON_MANIFEST_NOT_MAPPING, reason)


class ManifestAndConfigTests(unittest.TestCase):
    def _manifest(self, **overrides):
        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": "sha256:" + "c" * 64,
                "size": 5664,
            },
            "layers": [{"digest": "sha256:" + "e" * 64}],
        }
        manifest.update(overrides)
        return manifest

    def test_the_config_digest_is_read_from_the_manifest(self):
        digest, reason = policy.config_digest_from_manifest(self._manifest())
        self.assertIsNone(reason)
        self.assertEqual("sha256:" + "c" * 64, digest)

    def test_a_manifest_that_is_not_an_image_manifest_is_refused(self):
        digest, reason = policy.config_digest_from_manifest(
            self._manifest(mediaType="application/vnd.oci.image.index.v1+json")
        )
        self.assertIsNone(digest)
        self.assertEqual(policy.REASON_MANIFEST_NOT_IMAGE, reason)

    def test_a_manifest_without_a_usable_config_digest_is_refused(self):
        for config in (None, {}, {"digest": "sha256:short"}, "nope"):
            with self.subTest(config=config):
                digest, reason = policy.config_digest_from_manifest(
                    self._manifest(config=config)
                )
                self.assertIsNone(digest)
                self.assertEqual(policy.REASON_CONFIG_DIGEST, reason)

    def test_the_layer_count_comes_from_the_manifest(self):
        self.assertEqual(1, policy.manifest_layer_count(self._manifest()))
        self.assertIsNone(policy.manifest_layer_count({"layers": "nope"}))

    def test_the_base_layer_identities_are_read_from_the_config(self):
        identifiers = ["sha256:" + "a" * 64, "sha256:" + "b" * 64]
        read, reason = policy.diff_ids({"rootfs": {"diff_ids": identifiers}})
        self.assertIsNone(reason)
        self.assertEqual(identifiers, read)

    def test_a_config_without_layer_identities_is_refused(self):
        for config in (None, {}, {"rootfs": {}}, {"rootfs": {"diff_ids": []}},
                       {"rootfs": {"diff_ids": ["nope"]}}):
            with self.subTest(config=config):
                read, reason = policy.diff_ids(config)
                self.assertIsNone(read)
                self.assertEqual(policy.REASON_CONFIG_UNREADABLE, reason)


class OnBuildTests(unittest.TestCase):
    def _config(self, onbuild=...):
        section = {} if onbuild is ... else {"OnBuild": onbuild}
        return {"config": section, "rootfs": {"diff_ids": ["sha256:" + "a" * 64]}}

    def test_an_absent_or_empty_value_is_clear(self):
        for value in (..., None, [], "", "   "):
            with self.subTest(value=value):
                self.assertIsNone(policy.onbuild_reason(self._config(value)))

    def test_a_non_empty_value_refuses(self):
        for value in (["RUN echo hi"], ["RUN a", "RUN b"], "RUN echo hi"):
            with self.subTest(value=value):
                self.assertEqual(
                    policy.REASON_ONBUILD_PRESENT, policy.onbuild_reason(self._config(value))
                )

    def test_an_unverifiable_value_refuses(self):
        for value in (42, {"a": 1}, [1, 2], True):
            with self.subTest(value=value):
                self.assertEqual(
                    policy.REASON_ONBUILD_UNVERIFIABLE,
                    policy.onbuild_reason(self._config(value)),
                )

    def test_a_document_without_a_config_section_is_unverifiable(self):
        for document in (None, [], {}, {"rootfs": {}}, {"config": "nope"}):
            with self.subTest(document=document):
                self.assertEqual(
                    policy.REASON_ONBUILD_UNVERIFIABLE, policy.onbuild_reason(document)
                )

    def test_the_local_inspect_shape_is_adapted_to_the_registry_shape(self):
        adapted = policy.local_config_shape(
            {
                "Config": {"OnBuild": ["RUN x"], "User": "65534:65534"},
                "RootFS": {"Layers": ["sha256:" + "a" * 64]},
            }
        )
        self.assertEqual(["RUN x"], adapted["config"]["OnBuild"])
        self.assertEqual(
            ["sha256:" + "a" * 64], adapted["rootfs"]["diff_ids"]
        )
        self.assertEqual(
            policy.REASON_ONBUILD_PRESENT, policy.onbuild_reason(adapted)
        )
        self.assertEqual({}, policy.local_config_shape(None))


class DockerfileTests(unittest.TestCase):
    def test_the_repository_dockerfile_builds_from_the_pinned_base(self):
        # The artifact definition and the code-owned pin have to agree; this is
        # where a Dockerfile edit that drifts from the pin shows up.
        with open(
            os.path.join(REPO, policy.DOCKERFILE_PATH), "r", encoding="utf-8"
        ) as handle:
            text = handle.read()
        self.assertIsNone(policy.dockerfile_reason(text))
        references, reason = policy.from_instruction(text)
        self.assertIsNone(reason)
        self.assertEqual([policy.BASE_REFERENCE], references)

    def test_another_base_is_refused(self):
        text = "FROM python:3.12-slim@%s\n" % _OTHER_DIGEST
        self.assertEqual(policy.REASON_DOCKERFILE_FROM, policy.dockerfile_reason(text))

    def test_a_tag_only_base_is_refused(self):
        self.assertEqual(
            policy.REASON_DOCKERFILE_FROM, policy.dockerfile_reason("FROM python:3.12-slim\n")
        )

    def test_more_than_one_base_is_refused(self):
        text = "FROM %s\nFROM %s\n" % (policy.BASE_REFERENCE, policy.BASE_REFERENCE)
        self.assertEqual(
            policy.REASON_DOCKERFILE_FROM_MULTIPLE, policy.dockerfile_reason(text)
        )

    def test_comments_flags_and_stage_names_are_tolerated(self):
        text = (
            "# a comment mentioning FROM elsewhere:latest\n"
            "\n"
            "   from --platform=$BUILDPLATFORM %s AS base\n" % policy.BASE_REFERENCE
        )
        references, reason = policy.from_instruction(text)
        self.assertIsNone(reason)
        self.assertEqual([policy.BASE_REFERENCE], references)

    def test_a_malformed_from_is_refused(self):
        for text in ("FROM\n", "FROM\n", "FROM x y\n", "FROM --platform=\n", "LABEL a=b\n"):
            with self.subTest(text=text):
                references, reason = policy.from_instruction(text)
                self.assertIsNone(references)
                self.assertEqual(policy.REASON_DOCKERFILE_FROM, reason)

    def test_a_non_text_dockerfile_is_refused(self):
        references, reason = policy.from_instruction(None)
        self.assertIsNone(references)
        self.assertEqual(policy.REASON_DOCKERFILE_UNREADABLE, reason)


class LineageTests(unittest.TestCase):
    BASE = ["sha256:" + "a" * 64, "sha256:" + "b" * 64]

    def test_an_image_that_starts_with_the_base_layers_is_bound(self):
        self.assertIsNone(
            policy.lineage_reason(self.BASE, self.BASE + ["sha256:" + "c" * 64])
        )
        self.assertIsNone(policy.lineage_reason(self.BASE, self.BASE))

    def test_a_different_or_shorter_layer_list_is_not_bound(self):
        cases = (
            (self.BASE, list(reversed(self.BASE)) + ["sha256:" + "c" * 64]),
            (self.BASE, ["sha256:" + "z" * 64, "sha256:" + "b" * 64]),
            (self.BASE, [self.BASE[0]]),
            (self.BASE, []),
            (None, self.BASE),
            (self.BASE, None),
            ([], self.BASE),
        )
        for base, image in cases:
            with self.subTest(image=image):
                self.assertEqual(
                    policy.REASON_LINEAGE_MISMATCH, policy.lineage_reason(base, image)
                )


class ReadinessTests(unittest.TestCase):
    READY_FACTS = dict(
        base_identity_verified=True,
        platform_manifest_resolved=True,
        config_resolved=True,
        base_onbuild_reason=None,
        dockerfile_verified=True,
        build_succeeded=True,
        runner_recorded=True,
        runner_onbuild_reason=None,
        lineage_mismatch_reason=None,
        runner_digest_matches_pin=True,
    )

    def test_every_gate_satisfied_is_ready(self):
        state, reason = policy.readiness(**self.READY_FACTS)
        self.assertEqual(policy.READY, state)
        self.assertIsNone(reason)

    def test_every_gate_refuses_in_its_own_words(self):
        expectations = (
            ("base_identity_verified", False, policy.REASON_READY_BASE),
            ("platform_manifest_resolved", False, policy.REASON_READY_PLATFORM),
            ("config_resolved", False, policy.REASON_READY_CONFIG),
            ("dockerfile_verified", False, policy.REASON_READY_DOCKERFILE),
            ("build_succeeded", False, policy.REASON_READY_BUILD),
            ("runner_recorded", False, policy.REASON_READY_IMAGE),
            ("runner_digest_matches_pin", False, policy.REASON_READY_DIGEST),
        )
        for key, value, reason in expectations:
            with self.subTest(gate=key):
                facts = dict(self.READY_FACTS)
                facts[key] = value
                state, got = policy.readiness(**facts)
                self.assertEqual(policy.NOT_READY, state)
                self.assertEqual(reason, got)

    def test_an_onbuild_or_lineage_reason_is_passed_through(self):
        facts = dict(self.READY_FACTS, base_onbuild_reason=policy.REASON_ONBUILD_PRESENT)
        self.assertEqual(
            (policy.NOT_READY, policy.REASON_ONBUILD_PRESENT), policy.readiness(**facts)
        )
        facts = dict(self.READY_FACTS, runner_onbuild_reason=policy.REASON_ONBUILD_UNVERIFIABLE)
        self.assertEqual(
            (policy.NOT_READY, policy.REASON_ONBUILD_UNVERIFIABLE),
            policy.readiness(**facts),
        )
        facts = dict(self.READY_FACTS, lineage_mismatch_reason=policy.REASON_LINEAGE_MISMATCH)
        self.assertEqual(
            (policy.NOT_READY, policy.REASON_LINEAGE_MISMATCH), policy.readiness(**facts)
        )

    def test_the_defaults_are_all_not_ready(self):
        state, reason = policy.readiness()
        self.assertEqual(policy.NOT_READY, state)
        self.assertEqual(policy.REASON_READY_BASE, reason)


class PinAgreementTests(unittest.TestCase):
    """One pin, three owners: a re-pin that misses one of them fails here."""

    def test_the_runner_image_tag_is_the_same_everywhere(self):
        self.assertEqual(container_runner.RUNNER_IMAGE, policy.RUNNER_IMAGE)
        self.assertEqual(validation_policy.CANDIDATE_IMAGE, policy.RUNNER_IMAGE)

    def test_the_runner_image_digest_is_the_same_everywhere(self):
        self.assertEqual(
            container_runner.RUNNER_IMAGE_DIGEST, policy.RUNNER_IMAGE_DIGEST
        )
        self.assertEqual(
            validation_policy.CANDIDATE_IMAGE_DIGEST, policy.RUNNER_IMAGE_DIGEST
        )

    def test_the_base_is_pinned_by_digest_and_named_by_tag(self):
        self.assertEqual(
            "python:3.12-slim", policy.BASE_IMAGE
        )
        self.assertTrue(policy.is_digest(policy.BASE_INDEX_DIGEST))
        self.assertEqual(
            policy.BASE_IMAGE + "@" + policy.BASE_INDEX_DIGEST, policy.BASE_REFERENCE
        )

    def test_the_pin_is_a_digest_and_never_a_tag_alone(self):
        self.assertTrue(policy.is_digest(policy.RUNNER_IMAGE_DIGEST))

    def test_the_claims_state_what_this_setup_is_not(self):
        for key in (
            "zero_egress_proof",
            "exact_purpose_proof",
            "candidate_validation_success",
            "candidate_executed",
            "candidate_mounted",
            "plan_created",
            "attempt_created",
            "result_created",
            "approved",
            "adopted",
            "applied",
            "source_modified",
            "remote_pushed",
        ):
            with self.subTest(claim=key):
                self.assertIs(False, policy.CLAIMS[key])

    def test_the_networked_claim_is_not_a_constant(self):
        # It is the one claim that depends on what a run did, so it must not be
        # baked into the fixed map the record starts from.
        self.assertNotIn(policy.NETWORKED_CLAIM, policy.CLAIMS)
        self.assertEqual("networked_setup_evidence", policy.NETWORKED_CLAIM)
        self.assertEqual(
            ("buildx imagetools inspect", "build"), policy.NETWORK_OPERATIONS
        )

    def test_the_limitations_say_the_record_is_not_a_zero_egress_proof(self):
        joined = " ".join(policy.LIMITATIONS).lower()
        self.assertIn("zero-egress", joined)
        self.assertIn("purpose", joined)
        self.assertTrue(policy.CONTACT_UNOBSERVED)
        self.assertTrue(policy.DECLARED_ENDPOINT_CLASSES)


if __name__ == "__main__":
    unittest.main()
