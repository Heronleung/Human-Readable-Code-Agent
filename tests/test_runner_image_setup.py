"""The bounded runner-image setup adapter (P5.5a-r3c).

Every test here injects a stand-in for the Docker client and for Git, so the
suite is offline, dispatches no container and reads no registry. The live setup
is exercised separately, once, by the operator command the evidence record
points at — this module pins what the setup *would* do: which identity it binds,
which refusal it returns, and what it must never dispatch.

The load-bearing assertions are the negative ones: a refused base dispatches
nothing at all, a build is one fixed argv, no credential or helper is available
to the run, and the record says in its own words that it is networked setup
evidence rather than a zero-egress proof.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from hrca import runner_image_policy as policy
from hrca import runner_image_setup as setup

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(_HERE, ".."))

_OTHER_DIGEST = "sha256:" + "0" * 64
_MANIFEST_DIGEST = "sha256:" + "1" * 64
_CONFIG_DIGEST = "sha256:" + "c" * 64
_BASE_LAYERS = ["sha256:" + "a" * 64, "sha256:" + "b" * 64]
_EXTRA_LAYER = "sha256:" + "d" * 64
_IMAGE_DIGEST = policy.RUNNER_IMAGE_DIGEST
_BUILD_ARGV = [
    "docker",
    "build",
    "-f",
    policy.DOCKERFILE_PATH,
    "-t",
    policy.RUNNER_IMAGE,
    ".",
]

_FORBIDDEN_TOKENS = (
    "run",
    "exec",
    "cp",
    "login",
    "logout",
    "push",
    "pull",
    "save",
    "load",
    "--privileged",
    "--network",
    "--mount",
    "-v",
    "--volume",
    "--user",
    "--device",
    "docker.sock",
)


def _version_document(architecture="amd64"):
    return {
        "Client": {"Version": "28.4.0"},
        "Server": {
            "Version": "28.4.0",
            "Os": "linux",
            "Arch": architecture,
            "Platform": {"Name": "Docker Desktop 4.46.0"},
        },
    }


def _index_document(digest=_MANIFEST_DIGEST):
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "digest": policy.BASE_INDEX_DIGEST,
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": digest,
                "platform": {"os": "linux", "architecture": "amd64"},
            },
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:" + "9" * 64,
                "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"},
            },
        ],
    }


def _manifest_document(config_digest=_CONFIG_DIGEST):
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": config_digest,
            "size": 5664,
        },
        "layers": [{"digest": layer} for layer in _BASE_LAYERS],
    }


def _config_document(onbuild=..., layers=None):
    section = {} if onbuild is ... else {"OnBuild": onbuild}
    return {
        "created": "2026-09-01T00:12:40Z",
        "architecture": "amd64",
        "os": "linux",
        "config": section,
        "rootfs": {"diff_ids": list(_BASE_LAYERS if layers is None else layers)},
    }


def _image_document(
    digest=_IMAGE_DIGEST, onbuild=None, layers=None, reference=policy.RUNNER_IMAGE
):
    return {
        "Id": digest,
        "Created": "2026-09-20T12:51:51Z",
        "Config": {
            "OnBuild": onbuild,
            "User": "65534:65534",
            "WorkingDir": "/app",
            "Cmd": ["python3"],
        },
        "RootFS": {"Layers": list(_BASE_LAYERS + [_EXTRA_LAYER] if layers is None else layers)},
        "RepoTags": [reference],
    }


class FakeEngine:
    """A bounded stand-in for the Docker client and Git.

    It answers exactly the calls the setup makes and records every one of them,
    including the environment each Docker command would have run under, so a
    test can assert on what the setup asked for rather than on what it returned.
    """

    def __init__(
        self,
        *,
        index=None,
        manifest=None,
        config=None,
        image=None,
        version=None,
        build_stdout="#1 DONE 0.1s\n#2 CACHED\n",
        build_stderr="",
        build_rc=0,
        containers=(),
        images=(),
        git_status=(),
        git_head="deadbeef",
        raise_for=None,
        raise_exc=None,
    ):
        self.index = _index_document() if index is None else index
        self.manifest = _manifest_document() if manifest is None else manifest
        self.config = _config_document() if config is None else config
        self.image = _image_document() if image is None else image
        self.version = _version_document() if version is None else version
        self.build_stdout = build_stdout
        self.build_stderr = build_stderr
        self.build_rc = build_rc
        self.containers = list(containers)
        self.images = list(images)
        self.git_status = list(git_status)
        self.git_head = git_head
        self.raise_for = raise_for
        self.raise_exc = raise_exc
        self.calls = []
        self.envs = []
        self.config_dir_existed = []
        self.config_dir_empty = []

    # -- the stand-in itself ------------------------------------------------

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if self.raise_for is not None and argv[1:2] == [self.raise_for] and argv[0] == "docker":
            raise self.raise_exc
        if argv[0] == "git":
            return self._git(argv)
        self._record_env(kwargs.get("env") or {})
        return self._docker(argv[1:])

    def _record_env(self, env):
        directory = env.get(policy.CLIENT_CONFIG_ENV)
        self.envs.append(env)
        self.config_dir_existed.append(bool(directory) and os.path.isdir(directory))
        self.config_dir_empty.append(
            bool(directory) and os.path.isdir(directory) and not os.listdir(directory)
        )

    def _proc(self, argv, rc, stdout, stderr=""):
        return subprocess.CompletedProcess(argv, rc, stdout, stderr)

    def _git(self, argv):
        if argv[1] == "rev-parse":
            return self._proc(argv, 0, self.git_head + "\n")
        if argv[1] == "status":
            return self._proc(argv, 0, "".join(line + "\n" for line in self.git_status))
        if argv[1] == "diff":
            return self._proc(argv, 0, "")
        return self._proc(argv, 1, "")

    def _docker(self, argv):
        if argv[:1] == ["version"]:
            return self._proc(argv, 0, json.dumps(self.version))
        if argv[:2] == ["buildx", "version"]:
            return self._proc(argv, 0, "github.com/docker/buildx v0.28.0-desktop.1 abc\n")
        if argv[:2] == ["buildx", "ls"]:
            return self._proc(
                argv,
                0,
                "NAME/NODE   DRIVER/ENDPOINT   STATUS    BUILDKIT   PLATFORMS\n"
                "default*    docker\n"
                " \\_ default  \\_ default       running   v0.24.0    linux/amd64\n",
            )
        if argv[:1] == ["build"]:
            return self._proc(argv, self.build_rc, self.build_stdout, self.build_stderr)
        if argv[:2] == ["buildx", "imagetools"]:
            return self._proc(argv, 0, json.dumps(self._imagetools(argv)))
        if argv[:2] == ["image", "inspect"]:
            return self._proc(argv, 0, json.dumps(self.image))
        if argv[:1] == ["ps"]:
            return self._proc(argv, 0, "".join(line + "\n" for line in self.containers))
        if argv[:1] == ["images"]:
            return self._proc(argv, 0, "".join(line + "\n" for line in self.images))
        return self._proc(argv, 1, "", "no such call")

    def _imagetools(self, argv):
        if "--raw" in argv:
            return self.manifest
        if "{{json .Manifest}}" in argv:
            return self.index
        return self.config

    # -- assertions helpers -------------------------------------------------

    def docker_calls(self):
        return [argv for argv in self.calls if argv and argv[0] == "docker"]

    def builds(self):
        return [argv for argv in self.docker_calls() if argv[1:2] == ["build"]]

    def dispatched_text(self):
        return " ".join(" ".join(argv) for argv in self.docker_calls())


class _Root:
    """A temporary repository root whose Dockerfile a test controls."""

    def __init__(self, dockerfile=None):
        self.path = tempfile.mkdtemp(prefix="hrca-setup-test-")
        target = os.path.join(self.path, policy.DOCKERFILE_PATH)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(
                "FROM %s\n" % policy.BASE_REFERENCE if dockerfile is None else dockerfile
            )

    def close(self):
        shutil.rmtree(self.path, ignore_errors=True)


class SetupTestCase(unittest.TestCase):
    def setUp(self):
        self.root = _Root()
        self.addCleanup(self.root.close)
        self.environ = {"HOME": tempfile.mkdtemp(prefix="hrca-home-"), "PATH": "/usr/bin"}
        self.addCleanup(shutil.rmtree, self.environ["HOME"], True)

    def engine(self, **kwargs):
        return FakeEngine(**kwargs)

    def run_setup(self, engine, base=policy.BASE_REFERENCE):
        return setup.RunnerImageSetup(
            root=self.root.path, spawn=engine, environ=self.environ
        ).build(base)

    def run_inspect(self, engine, base=policy.BASE_REFERENCE):
        return setup.RunnerImageSetup(
            root=self.root.path, spawn=engine, environ=self.environ
        ).inspect(base)


class RefusalTests(SetupTestCase):
    def test_a_tag_only_base_dispatches_nothing_at_all(self):
        engine = self.engine()
        record, error = self.run_setup(engine, policy.BASE_IMAGE)
        self.assertEqual(policy.REASON_BASE_TAG_ONLY, error)
        self.assertEqual(setup.OUTCOME_REFUSED, record["outcome"])
        self.assertEqual(policy.REASON_BASE_TAG_ONLY, record["refusal"])
        self.assertEqual([], engine.calls)
        self.assertEqual(policy.NOT_READY, record["readiness"]["state"])

    def test_a_mismatched_digest_dispatches_nothing_at_all(self):
        engine = self.engine()
        record, error = self.run_setup(engine, policy.BASE_IMAGE + "@" + _OTHER_DIGEST)
        self.assertEqual(policy.REASON_BASE_MISMATCH, error)
        self.assertEqual([], engine.calls)

    def test_a_foreign_registry_dispatches_nothing_at_all(self):
        engine = self.engine()
        record, error = self.run_setup(
            engine, "ghcr.io/library/python:3.12-slim@" + policy.BASE_INDEX_DIGEST
        )
        self.assertEqual(policy.REASON_REGISTRY_HOST, error)
        self.assertEqual([], engine.calls)

    def test_a_dockerfile_that_names_another_base_refuses_before_the_build(self):
        root = _Root(dockerfile="FROM python:3.12-slim@%s\n" % _OTHER_DIGEST)
        self.addCleanup(root.close)
        engine = self.engine()
        record, error = setup.RunnerImageSetup(
            root=root.path, spawn=engine, environ=self.environ
        ).build(policy.BASE_REFERENCE)
        self.assertEqual(policy.REASON_DOCKERFILE_FROM, error)
        self.assertEqual([], engine.builds())
        self.assertIs(False, record["dockerfile"]["builds_from_pinned_base"])


class OnBuildGateTests(SetupTestCase):
    def test_a_base_with_onbuild_triggers_refuses_before_the_build(self):
        engine = self.engine(config=_config_document(onbuild=["RUN echo hi"]))
        record, error = self.run_setup(engine)
        self.assertEqual(policy.REASON_ONBUILD_PRESENT, error)
        self.assertEqual([], engine.builds(), "an OnBuild base must not be built from")
        self.assertEqual(setup.OUTCOME_REFUSED, record["outcome"])

    def test_an_unverifiable_base_onbuild_refuses(self):
        config = _config_document()
        config["config"] = "not a mapping"
        engine = self.engine(config=config)
        record, error = self.run_setup(engine)
        self.assertEqual(policy.REASON_ONBUILD_UNVERIFIABLE, error)
        self.assertEqual([], engine.builds())

    def test_a_non_empty_image_onbuild_builds_but_is_not_ready(self):
        engine = self.engine(image=_image_document(onbuild=["RUN echo hi"]))
        record, error = self.run_setup(engine)
        self.assertIsNone(error)
        self.assertEqual(setup.OUTCOME_BUILT, record["outcome"])
        self.assertEqual(policy.NOT_READY, record["readiness"]["state"])
        self.assertEqual(
            policy.REASON_ONBUILD_PRESENT, record["readiness"]["reason"]
        )


class BuildTests(SetupTestCase):
    def test_the_build_is_dispatched_once_with_one_fixed_argv(self):
        engine = self.engine()
        record, error = self.run_setup(engine)
        self.assertIsNone(error)
        self.assertEqual(setup.OUTCOME_BUILT, record["outcome"])
        self.assertEqual([_BUILD_ARGV], engine.builds())
        self.assertEqual(1, len(engine.builds()))

    def test_the_setup_never_dispatches_a_container_or_a_candidate(self):
        engine = self.engine()
        self.run_setup(engine)
        dispatched = engine.docker_calls()
        self.assertTrue(dispatched)
        for argv in dispatched:
            with self.subTest(argv=argv):
                self.assertNotEqual("run", argv[1])
                self.assertNotIn("exec", argv)
                self.assertNotIn("login", argv)
                self.assertNotIn("push", argv)
        text = " " + engine.dispatched_text() + " "
        for token in _FORBIDDEN_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(" %s " % token, text)

    def test_the_record_binds_every_identity_by_digest(self):
        engine = self.engine()
        record, _error = self.run_setup(engine)
        base = record["base"]
        self.assertEqual(policy.BASE_REFERENCE, record["requested_base"])
        self.assertEqual(policy.BASE_INDEX_DIGEST, base["index_digest"])
        self.assertEqual(_MANIFEST_DIGEST, base["platform_manifest_digest"])
        self.assertEqual(_CONFIG_DIGEST, base["config_digest"])
        self.assertEqual(_BASE_LAYERS, base["layer_diff_ids"])
        self.assertEqual(2, base["layer_count"])
        self.assertEqual(
            "docker.io/library/python:3.12-slim", base["requested_canonical"].split("@")[0]
        )
        self.assertEqual("linux", base["platform"]["os"])
        self.assertEqual("amd64", base["platform"]["architecture"])
        self.assertIsNone(base["onbuild_reason"])
        self.assertEqual(policy.READY, record["readiness"]["state"])
        self.assertIsNone(record["readiness"]["reason"])

    def test_the_engine_identities_are_recorded(self):
        engine = self.engine()
        record, _error = self.run_setup(engine)
        client = record["base"]["client"]
        self.assertEqual("28.4.0", client["client_version"])
        self.assertEqual("28.4.0", client["server_version"])
        self.assertEqual("Docker Desktop 4.46.0", client["server_platform"])
        self.assertEqual("amd64", client["engine_architecture"])
        self.assertEqual("amd64", client["architecture"])
        self.assertEqual("v0.28.0-desktop.1", client["buildx_version"])
        self.assertEqual("default", client["builder"])
        self.assertEqual("docker", client["builder_driver"])
        self.assertEqual("v0.24.0", client["buildkit_version"])

    def test_the_image_identity_is_recorded_and_matched_against_the_pin(self):
        engine = self.engine()
        record, _error = self.run_setup(engine)
        image = record["image"]
        self.assertEqual(_IMAGE_DIGEST, image["digest"])
        self.assertEqual(_IMAGE_DIGEST, image["pinned_digest"])
        self.assertIs(True, image["matches_pinned"])
        self.assertEqual("65534:65534", image["user"])
        self.assertEqual("/app", image["working_dir"])
        self.assertEqual([policy.RUNNER_IMAGE], image["repository_tags"])

    def test_a_rebuilt_image_with_a_different_digest_is_not_ready(self):
        other = "sha256:" + "f" * 64
        engine = self.engine(image=_image_document(digest=other))
        record, error = self.run_setup(engine)
        self.assertIsNone(error)
        self.assertEqual(setup.OUTCOME_BUILT, record["outcome"])
        self.assertIs(False, record["image"]["matches_pinned"])
        self.assertEqual(policy.NOT_READY, record["readiness"]["state"])
        self.assertEqual(policy.REASON_READY_DIGEST, record["readiness"]["reason"])

    def test_an_image_that_does_not_descend_from_the_base_is_not_ready(self):
        engine = self.engine(image=_image_document(layers=[_EXTRA_LAYER]))
        record, _error = self.run_setup(engine)
        self.assertEqual(policy.NOT_READY, record["readiness"]["state"])
        self.assertEqual(policy.REASON_LINEAGE_MISMATCH, record["readiness"]["reason"])

    def test_a_failed_build_is_a_bounded_refusal_without_echoing_output(self):
        # A failure is reported as a bounded reason: the builder's lines are
        # evidence when a build succeeds and are not kept when it does not, so
        # a log can never become the shape a refusal takes.
        engine = self.engine(build_rc=1, build_stderr="ERROR: secret-looking text\n")
        record, error = self.run_setup(engine)
        self.assertEqual(setup.REASON_BUILD_FAILED, error)
        self.assertEqual(setup.OUTCOME_REFUSED, record["outcome"])
        self.assertIs(False, record["build"]["succeeded"])
        self.assertEqual(1, record["build"]["exit_status"])
        self.assertEqual([], record["build"]["events"])
        self.assertEqual([], record["build"]["cached_steps"])
        self.assertNotIn("secret-looking", setup.dumps(record))
        self.assertEqual(policy.NOT_READY, record["readiness"]["state"])

    def test_a_successful_build_keeps_the_builder_lines_as_evidence(self):
        engine = self.engine(build_stdout="#1 CACHED\n#2 extracting sha256:abc done\n")
        record, _error = self.run_setup(engine)
        self.assertTrue(record["build"]["succeeded"])
        self.assertIn("#1 CACHED", record["build"]["events"])

    def test_a_foreign_platform_refuses_before_the_build(self):
        engine = self.engine(version=_version_document(architecture="sparc"))
        record, error = self.run_setup(engine)
        self.assertEqual(policy.REASON_PLATFORM_UNKNOWN, error)
        self.assertEqual([], engine.builds())

    def test_an_index_without_the_engine_platform_refuses(self):
        index = _index_document()
        index["manifests"] = [index["manifests"][1]]
        engine = self.engine(index=index)
        record, error = self.run_setup(engine)
        self.assertEqual(policy.REASON_PLATFORM_MISSING, error)
        self.assertEqual([], engine.builds())

    def test_an_index_that_resolves_to_a_different_digest_refuses(self):
        engine = self.engine(index=dict(_index_document(), digest=_OTHER_DIGEST))
        record, error = self.run_setup(engine)
        self.assertEqual(policy.REASON_BASE_MISMATCH, error)
        self.assertEqual([], engine.builds())


class SpellingTests(SetupTestCase):
    """The pinned base by any of its legal spellings."""

    CANONICAL = (
        "docker.io/library/python:3.12-slim@" + policy.BASE_INDEX_DIGEST
    )

    def test_the_canonical_spelling_is_accepted_by_the_policy(self):
        parts, reason = policy.verify_base_reference(self.CANONICAL)
        self.assertIsNone(reason)
        self.assertEqual(policy.BASE_INDEX_DIGEST, parts["digest"])

    def test_a_canonical_base_is_not_refused_by_the_dockerfile_gate(self):
        # The record publishes this spelling as ``requested_canonical``; if an
        # operator passes it back, the Dockerfile check must compare identities
        # rather than spellings, or the setup records the false claim that the
        # reviewed Dockerfile builds from something else.
        engine = self.engine()
        record, error = self.run_setup(engine, self.CANONICAL)
        self.assertIsNone(error)
        self.assertEqual(setup.OUTCOME_BUILT, record["outcome"])
        self.assertIs(True, record["dockerfile"]["builds_from_pinned_base"])
        self.assertEqual(policy.READY, record["readiness"]["state"])

    def test_the_canonical_spelling_also_survives_the_inspect_path(self):
        engine = self.engine()
        record, error = self.run_inspect(engine, self.CANONICAL)
        self.assertIsNone(error)
        self.assertIs(True, record["dockerfile"]["builds_from_pinned_base"])
        self.assertEqual(policy.READY, record["readiness"]["state"])

    def test_the_dockerfile_is_compared_by_identity_in_the_policy(self):
        text = "FROM %s\n" % self.CANONICAL
        self.assertIsNone(policy.dockerfile_reason(text, policy.BASE_REFERENCE))
        self.assertIsNone(policy.dockerfile_reason(text, self.CANONICAL))
        other = "FROM python:3.12-slim@%s\n" % _OTHER_DIGEST
        self.assertEqual(
            policy.REASON_DOCKERFILE_FROM,
            policy.dockerfile_reason(other, self.CANONICAL),
        )


class ContactClassTests(SetupTestCase):
    def test_context_transfer_is_not_counted_as_registry_acquisition(self):
        # "transferring context" is the client handing bytes to the daemon. A
        # build that pulled no layer must not read as an acquisition.
        engine = self.engine(
            build_stdout="#1 transferring dockerfile: 1.33kB done\n"
            "#4 transferring context: 18.08kB done\n"
            "#6 CACHED\n#7 CACHED\n"
        )
        record, _error = self.run_setup(engine)
        build = record["build"]
        self.assertEqual([], build["acquired_steps"])
        self.assertTrue(build["context_steps"])
        self.assertTrue(build["cached_steps"])
        self.assertEqual("cache_hit", build["cache_state"])
        self.assertIn(
            policy.CONTACT_LAYER_CACHE_LOOKUP, record["contact"]["observed_classes"]
        )
        self.assertNotIn(
            policy.CONTACT_LAYER_ACQUISITION, record["contact"]["observed_classes"]
        )

    def test_a_real_layer_pull_is_reported_as_acquisition(self):
        engine = self.engine(
            build_stdout="#5 extracting sha256:abc 0.1s done\n#5 DONE 0.1s\n"
        )
        record, _error = self.run_setup(engine)
        self.assertEqual("acquired", record["build"]["cache_state"])
        self.assertTrue(record["build"]["acquired_steps"])
        self.assertIn(
            policy.CONTACT_LAYER_ACQUISITION, record["contact"]["observed_classes"]
        )

    def test_a_refusal_before_any_dispatch_claims_no_contact_class(self):
        engine = self.engine()
        record, _error = self.run_setup(engine, policy.BASE_IMAGE)
        self.assertEqual([], record["contact"]["observed_classes"])
        self.assertEqual([], record["contact"]["dispatched_operations"])
        self.assertIs(False, record["contact"]["host_names_observed"])

    def test_inspect_claims_resolution_but_no_layer_class(self):
        engine = self.engine()
        record, _error = self.run_inspect(engine)
        classes = record["contact"]["observed_classes"]
        self.assertEqual(list(policy.CONTACT_RESOLUTION_CLASSES), classes)
        self.assertNotIn(policy.CONTACT_LAYER_CACHE_LOOKUP, classes)
        self.assertEqual([], record["contact"]["hosts_named_in_build_output"])
        self.assertIs(False, record["contact"]["host_names_observed"])

    def test_the_comparison_never_asks_for_container_uptime(self):
        # ``.Status`` is a human uptime string, so comparing it would report a
        # change that no one made and make the record clock-dependent.
        self.assertNotIn("Status", setup.CONTAINER_FORMAT)

    def test_a_similarly_named_image_is_not_excluded_from_the_comparison(self):
        engine = self.engine(
            images=("sha256:aaaa|hrca-runner:v10", "sha256:bbbb|other:x")
        )
        record, _error = self.run_setup(engine)
        self.assertEqual(
            ["sha256:aaaa|hrca-runner:v10", "sha256:bbbb|other:x"],
            record["non_mutation"]["before"]["images_excluding_runner"],
        )

    def test_candidate_roots_record_what_the_digest_covers(self):
        engine = self.engine()
        record, _error = self.run_setup(engine)
        roots = record["non_mutation"]["before"]["candidate_roots"]
        self.assertEqual(["candidate_fixtures"], sorted(roots))
        # This root has no fixture corpus, so the honest answer is "absent".
        self.assertEqual({"state": "absent"}, roots["candidate_fixtures"])

    def test_a_digested_candidate_root_records_its_coverage(self):
        corpus = os.path.join(self.root.path, "candidate_fixtures", "files")
        os.makedirs(corpus)
        with open(os.path.join(corpus, "example.py"), "w", encoding="utf-8") as handle:
            handle.write("VALUE = 1\n")
        record, _error = self.run_setup(self.engine())
        observation = record["non_mutation"]["before"]["candidate_roots"][
            "candidate_fixtures"
        ]
        self.assertEqual("digested", observation["state"])
        self.assertEqual(1, observation["files"])
        self.assertIs(False, observation["truncated"])
        self.assertTrue(observation["digest"].startswith("sha256:"))

    def test_an_unreadable_candidate_root_is_reported_as_truncated(self):
        corpus = os.path.join(self.root.path, "candidate_fixtures", "files")
        os.makedirs(corpus)
        for index in range(6):
            with open(
                os.path.join(corpus, "f%d.py" % index), "w", encoding="utf-8"
            ) as handle:
                handle.write("VALUE = %d\n" % index)
        original = setup.MAX_SNAPSHOT_FILES
        try:
            setup.MAX_SNAPSHOT_FILES = 2
            record, _error = self.run_setup(self.engine())
        finally:
            setup.MAX_SNAPSHOT_FILES = original
        observation = record["non_mutation"]["before"]["candidate_roots"][
            "candidate_fixtures"
        ]
        self.assertIs(True, observation["truncated"])
        self.assertEqual(2, observation["files"])


class RobustnessTests(SetupTestCase):
    def test_a_non_mapping_config_or_rootfs_does_not_crash_the_reader(self):
        engine = self.engine(image={"Id": _IMAGE_DIGEST, "Config": "nope", "RootFS": "nope"})
        record, error = self.run_setup(engine)
        self.assertIsNone(error)
        self.assertEqual(setup.OUTCOME_BUILT, record["outcome"])
        self.assertIsNone(record["image"]["user"])
        self.assertEqual([], record["image"]["layer_diff_ids"])
        self.assertEqual(policy.NOT_READY, record["readiness"]["state"])
        # An unreadable config is an unverifiable OnBuild state, and that gate
        # comes before lineage: absence of evidence is not evidence of absence.
        self.assertEqual(
            policy.REASON_ONBUILD_UNVERIFIABLE, record["readiness"]["reason"]
        )

    def test_a_non_mapping_client_document_does_not_crash_the_client_read(self):
        engine = self.engine(version={"Client": "nope", "Server": {"Version": "28.4.0",
                                                                 "Os": "linux",
                                                                 "Arch": "amd64"}})
        record, error = self.run_setup(engine)
        self.assertIsNone(error)
        self.assertIsNone(record["base"]["client"]["client_version"])

    def test_an_unreachable_daemon_is_named_rather_than_generic(self):
        class Down(FakeEngine):
            def _docker(self, argv):
                if argv[:1] == ["version"]:
                    return self._proc(argv, 1, "", "Cannot connect to the Docker daemon")
                return super()._docker(argv)

        record, error = self.run_setup(Down())
        self.assertEqual(setup.REASON_DAEMON_UNREACHABLE, error)
        self.assertEqual(
            setup.REASON_DAEMON_UNREACHABLE, record["readiness"]["reason"]
        )

    def test_a_client_environment_failure_is_named_rather_than_a_missing_client(self):
        import unittest.mock as mock

        engine = self.engine()
        runner = setup.RunnerImageSetup(
            root=self.root.path, spawn=engine, environ=self.environ
        )
        with mock.patch.object(
            setup.tempfile, "mkdtemp", side_effect=OSError("no space")
        ):
            _record, error = runner.build(policy.BASE_REFERENCE)
        self.assertEqual(setup.REASON_CLIENT_ENV, error)
        # The observation the setup takes before resolving is Git, not Docker:
        # no client call was made, and none was recorded as dispatched.
        self.assertEqual([], engine.docker_calls())
        self.assertEqual([], runner.dispatched)

    def test_the_command_list_is_bounded_against_a_repetitive_log(self):
        text = "".join(
            "#%d extracting sha256:abc done\n" % index
            for index in range(setup.MAX_EVENT_LINES + 40)
        )
        classified = setup.classify_build_output(text)
        self.assertEqual(setup.MAX_EVENT_LINES, len(classified["acquired_steps"]))
        self.assertEqual(setup.MAX_EVENT_LINES, len(classified["events"]))
        self.assertEqual(setup.MAX_EVENT_LINES + 40, classified["log_lines"])


class TimeoutTests(SetupTestCase):
    def test_a_real_subprocess_timeout_is_a_bounded_reason(self):
        engine = self.engine(
            raise_for="version",
            raise_exc=subprocess.TimeoutExpired(cmd="docker", timeout=1.0),
        )
        record, error = self.run_setup(engine)
        self.assertEqual(setup.REASON_DOCKER_TIMEOUT, error)
        self.assertEqual(setup.OUTCOME_REFUSED, record["outcome"])

    def test_a_literal_timeout_error_is_a_bounded_reason(self):
        # Both names have to be caught: subprocess.TimeoutExpired is not a
        # TimeoutError, and catching only one leaves the other escaping.
        engine = self.engine(raise_for="version", raise_exc=TimeoutError())
        record, error = self.run_setup(engine)
        self.assertEqual(setup.REASON_DOCKER_TIMEOUT, error)

    def test_a_missing_client_is_a_bounded_reason(self):
        engine = self.engine(raise_for="version", raise_exc=FileNotFoundError())
        record, error = self.run_setup(engine)
        self.assertEqual(setup.REASON_DOCKER_ABSENT, error)


class CredentialTests(SetupTestCase):
    def test_no_credential_is_available_to_any_dispatched_command(self):
        self.environ.update(
            {"DOCKER_TOKEN": "not-a-real-token", "DOCKER_PASSWORD": "x",
             "REGISTRY_AUTH_FILE": "/somewhere"}
        )
        engine = self.engine()
        record, _error = self.run_setup(engine)
        self.assertTrue(engine.envs, "no Docker command was run")
        for env in engine.envs:
            with self.subTest(env=env.get(policy.CLIENT_CONFIG_ENV)):
                self.assertNotEqual(
                    self.environ.get(policy.CLIENT_CONFIG_ENV),
                    env.get(policy.CLIENT_CONFIG_ENV),
                )
                for name in policy.CREDENTIAL_ENV_NAMES:
                    self.assertNotIn(name, env)
        self.assertTrue(
            all(engine.config_dir_existed), "a command ran without a client config directory"
        )
        self.assertTrue(
            all(engine.config_dir_empty), "the client config directory was not empty"
        )

    def test_the_record_states_that_no_credential_or_helper_was_available(self):
        engine = self.engine()
        record, _error = self.run_setup(engine)
        credentials = record["credentials"]
        self.assertIs(False, credentials["login_dispatched"])
        self.assertEqual(
            {
                "auth_entries_available": 0,
                "credential_helper_available": False,
                "ambient_client_config_used": False,
            },
            credentials["effective_for_the_run"],
        )
        self.assertEqual(
            {"variable": "DOCKER_CONFIG", "isolated": True,
             "isolated_directory_empty_at_creation": True},
            credentials["client_config_isolation"],
        )
        self.assertEqual([], credentials["credential_env_present_in_ambient_environment"])
        self.assertEqual(
            [], credentials["credential_env_removed_from_child_environment"]
        )
        self.assertEqual(
            sorted(policy.CREDENTIAL_ENV_NAMES),
            credentials["credential_env_names_dropped_unconditionally"],
        )
        self.assertIs(
            True, credentials["client_config_isolation"]["isolated_directory_empty_at_creation"]
        )

    def test_the_removed_names_are_read_back_from_the_child_environment(self):
        # The field is derived from the mapping the children received, not from
        # the ambient dict, so it cannot stay true if the removal stops working.
        self.environ.update({"DOCKER_TOKEN": "x", "REGISTRY_PASSWORD": "y"})
        engine = self.engine()
        record, _error = self.run_setup(engine)
        credentials = record["credentials"]
        self.assertEqual(
            ["DOCKER_TOKEN", "REGISTRY_PASSWORD"],
            credentials["credential_env_present_in_ambient_environment"],
        )
        self.assertEqual(
            ["DOCKER_TOKEN", "REGISTRY_PASSWORD"],
            credentials["credential_env_removed_from_child_environment"],
        )
        for env in engine.envs:
            self.assertNotIn("DOCKER_TOKEN", env)
            self.assertNotIn("REGISTRY_PASSWORD", env)

    def test_a_run_that_dispatched_nothing_claims_no_effective_state(self):
        engine = self.engine()
        record, _error = self.run_setup(engine, policy.BASE_IMAGE)
        effective = record["credentials"]["effective_for_the_run"]
        self.assertIsNone(effective["auth_entries_available"])
        self.assertIsNone(effective["credential_helper_available"])
        self.assertIn("no Docker command was dispatched", effective["note"])
        isolation = record["credentials"]["client_config_isolation"]
        self.assertIs(False, isolation["isolated"])
        self.assertIsNone(isolation["isolated_directory_empty_at_creation"])

    def test_the_ambient_configuration_is_summarised_without_any_value(self):
        home = self.environ["HOME"]
        os.makedirs(os.path.join(home, ".docker"), exist_ok=True)
        ambient = {
            "auths": {"https://index.docker.io/v1/": {"auth": "c2VjcmV0"}},
            "credsStore": "some-helper",
        }
        with open(os.path.join(home, ".docker", "config.json"), "w", encoding="utf-8") as fh:
            json.dump(ambient, fh)
        engine = self.engine()
        record, _error = self.run_setup(engine)
        summary = record["credentials"]["ambient_configs"][0]
        self.assertIs(True, summary["present"])
        self.assertIs(True, summary["readable"])
        self.assertEqual(1, summary["auths_entries"])
        self.assertIs(True, summary["creds_store_configured"])
        self.assertEqual(["auths", "credsStore"], summary["credential_keys_present"])
        # The value never reaches the record, and the helper is never named.
        self.assertNotIn("c2VjcmV0", setup.dumps(record))
        self.assertNotIn("some-helper", setup.dumps(record))

    def test_the_client_config_directory_is_removed_afterwards(self):
        engine = self.engine()
        self.run_setup(engine)
        for env in engine.envs:
            directory = env.get(policy.CLIENT_CONFIG_ENV)
            with self.subTest(directory=directory):
                self.assertFalse(os.path.exists(directory), "the directory was left behind")

    def test_a_login_would_be_visible_in_the_record(self):
        # The dispatch list is the evidence: a login cannot be hidden by
        # reporting success, because the record names every operation run.
        engine = self.engine()
        runner = setup.RunnerImageSetup(
            root=self.root.path, spawn=engine, environ=self.environ
        )
        runner.dispatched.append(["login", "-u", "someone"])
        evidence = runner.credential_evidence()
        self.assertIs(True, evidence["login_dispatched"])


class NonMutationTests(SetupTestCase):
    IMAGES = (
        "sha256:aaaa|some-other-image:latest",
        "sha256:bbbb|hrca-runner:v1",
    )

    def test_an_unchanged_environment_is_recorded_as_unchanged(self):
        engine = self.engine(images=self.IMAGES, containers=("abc|hrca-run-1|img|Exited",))
        record, _error = self.run_setup(engine)
        unchanged = record["non_mutation"]["unchanged"]
        self.assertEqual(
            {
                "git_head",
                "git_status",
                "git_diff_stat",
                "candidate_roots",
                "containers",
                "images_excluding_runner",
            },
            set(unchanged),
        )
        self.assertTrue(all(unchanged.values()), unchanged)
        self.assertIs(True, record["non_mutation"]["all_unchanged"])

    def test_a_changed_container_makes_the_record_say_so(self):
        class Changing(FakeEngine):
            def _docker(self, argv):
                if argv[:1] == ["ps"]:
                    self.ps_calls = getattr(self, "ps_calls", 0) + 1
                    if self.ps_calls > 1:
                        return self._proc(argv, 0, "abc|hrca-run-2|img|Up\n")
                return super()._docker(argv)

        engine = Changing(containers=("abc|hrca-run-1|img|Exited",))
        record, _error = self.run_setup(engine)
        self.assertIs(False, record["non_mutation"]["unchanged"]["containers"])
        self.assertIs(False, record["non_mutation"]["all_unchanged"])

    def test_the_runner_image_itself_is_excluded_from_the_comparison(self):
        class Rebuilt(FakeEngine):
            def _docker(self, argv):
                if argv[:1] == ["images"]:
                    self.image_calls = getattr(self, "image_calls", 0) + 1
                    if self.image_calls > 1:
                        return self._proc(
                            argv, 0, "sha256:cccc|hrca-runner:v1\nsha256:aaaa|other:x\n"
                        )
                return super()._docker(argv)

        engine = Rebuilt(images=("sha256:aaaa|other:x", "sha256:bbbb|hrca-runner:v1"))
        record, _error = self.run_setup(engine)
        self.assertIs(True, record["non_mutation"]["all_unchanged"])
        self.assertEqual(
            ["sha256:aaaa|other:x"],
            record["non_mutation"]["after"]["images_excluding_runner"],
        )
        self.assertEqual(2, len(record["non_mutation"]["before"]["images"]))

    def test_the_notes_say_an_untracked_file_is_never_read(self):
        engine = self.engine()
        record, _error = self.run_setup(engine)
        joined = " ".join(record["non_mutation"]["notes"]).lower()
        self.assertIn("untracked", joined)
        self.assertIn("does not and cannot verify", joined)
        # The note must not claim a property the code does not establish: the
        # candidate roots *are* read (to digest them), and the build context
        # *is* the repository root, so the note says exactly that.
        self.assertIn("observed by content digest", joined)
        self.assertIn("build context is the repository root", joined)
        self.assertNotIn("never read, hashed or staged", joined)


class ContactTests(SetupTestCase):
    def test_the_record_lists_the_operations_it_dispatched(self):
        engine = self.engine()
        record, _error = self.run_setup(engine)
        operations = record["contact"]["dispatched_operations"]
        self.assertIn("buildx imagetools inspect", operations)
        self.assertIn("image inspect", operations)
        self.assertIn("version", operations)
        self.assertIn("build", operations)

    def test_the_hosts_the_builder_named_are_recorded_and_checked(self):
        engine = self.engine(
            build_stdout="#3 [1/4] FROM docker.io/library/python:3.12-slim@"
            + policy.BASE_INDEX_DIGEST
            + "\n#3 CACHED\n"
        )
        record, _error = self.run_setup(engine)
        self.assertEqual(["docker.io"], record["contact"]["hosts_named_in_build_output"])
        self.assertEqual([], record["contact"]["unexpected_hits"])

    def test_a_host_outside_the_allowed_set_is_flagged(self):
        engine = self.engine(
            build_stdout="#3 [1/4] FROM ghcr.io/other/base:latest\n#3 DONE 0.2s\n"
        )
        record, _error = self.run_setup(engine)
        self.assertEqual(["ghcr.io"], record["contact"]["unexpected_hits"])

    def test_the_record_says_what_was_not_observed(self):
        engine = self.engine()
        record, _error = self.run_setup(engine)
        contact = record["contact"]
        self.assertTrue(contact["observed_classes"])
        self.assertTrue(contact["declared_endpoint_classes"])
        self.assertTrue(contact["unobserved"])
        joined = " ".join(contact["unobserved"]).lower()
        self.assertIn("destinations", joined)

    def test_cache_and_acquisition_are_classified_from_the_build_log(self):
        engine = self.engine(
            build_stdout="#2 resolve image config for docker-image://x done\n"
            "#2 DONE 0.4s\n#3 extracting sha256:abc 0.1s done\n#3 DONE 0.2s\n"
            "#4 CACHED\n"
        )
        record, _error = self.run_setup(engine)
        build = record["build"]
        self.assertEqual("mixed", build["cache_state"])
        self.assertTrue(build["cached_steps"])
        self.assertTrue(build["acquired_steps"])
        self.assertTrue(build["resolution_steps"])


class ClassifyTests(unittest.TestCase):
    def test_the_states_are_named_honestly(self):
        cases = (
            ("", "unknown"),
            ("#4 CACHED\n", "cache_hit"),
            ("#3 extracting sha256:abc done\n", "acquired"),
            ("#4 CACHED\n#5 extracting sha256:abc done\n", "mixed"),
            ("#1 DONE 0.1s\n", "unknown"),
        )
        for text, state in cases:
            with self.subTest(text=text):
                self.assertEqual(state, setup.classify_build_output(text)["cache_state"])

    def test_the_raw_lines_are_kept_but_bounded(self):
        text = "".join("#%d line\n" % index for index in range(setup.MAX_EVENT_LINES + 50))
        classified = setup.classify_build_output(text)
        self.assertEqual(setup.MAX_EVENT_LINES, len(classified["events"]))
        self.assertEqual(setup.MAX_EVENT_LINES, classified["lines"])

    def test_a_very_long_line_is_truncated(self):
        classified = setup.classify_build_output("x" * 5000)
        self.assertEqual(setup.MAX_EVENT_LINE_CHARS, len(classified["events"][0]))

    def test_host_extraction_is_about_names_not_destinations(self):
        self.assertEqual(
            ["docker.io"],
            setup.hosts_named_in_output("FROM docker.io/library/python:3.12-slim"),
        )
        self.assertEqual(
            ["production.cloudflare.docker.com"],
            setup.hosts_named_in_output("downloading production.cloudflare.docker.com/blob"),
        )
        self.assertEqual([], setup.hosts_named_in_output("python:3.12-slim 3.12/slim"))


class RecordShapeTests(SetupTestCase):
    def test_the_record_says_it_is_not_a_zero_egress_or_purpose_proof(self):
        engine = self.engine()
        record, _error = self.run_setup(engine)
        self.assertIs(True, record["claim"]["networked_setup_evidence"])
        self.assertIs(False, record["claim"]["zero_egress_proof"])
        self.assertIs(False, record["claim"]["exact_purpose_proof"])
        self.assertIs(False, record["claim"]["candidate_validation_success"])
        self.assertIs(False, record["claim"]["candidate_executed"])
        self.assertIs(False, record["claim"]["attempt_created"])
        joined = " ".join(record["limitations"]).lower()
        self.assertIn("zero-egress", joined)

    def test_the_record_is_deterministic_for_the_same_environment(self):
        first, _ = self.run_setup(self.engine())
        second, _ = self.run_setup(self.engine())
        self.assertEqual(setup.dumps(first), setup.dumps(second))

    def test_the_record_is_canonical_json(self):
        engine = self.engine()
        record, _error = self.run_setup(engine)
        text = setup.dumps(record)
        self.assertEqual(text, json.dumps(record, ensure_ascii=True, sort_keys=True,
                                          separators=(",", ":")))
        self.assertEqual(record, json.loads(text))
        self.assertEqual(record, json.loads(setup.dumps_pretty(record)))

    def test_inspect_dispatches_no_build_and_says_so(self):
        engine = self.engine()
        record, error = self.run_inspect(engine)
        self.assertIsNone(error)
        self.assertEqual(setup.OUTCOME_INSPECTED, record["outcome"])
        self.assertEqual([], engine.builds())
        self.assertIs(False, record["build"]["dispatched"])
        self.assertEqual(policy.READY, record["readiness"]["state"])

    def test_inspect_is_not_ready_when_the_local_image_does_not_match_the_pin(self):
        engine = self.engine(image=_image_document(digest="sha256:" + "f" * 64))
        record, error = self.run_inspect(engine)
        self.assertIsNone(error)
        self.assertEqual(policy.NOT_READY, record["readiness"]["state"])
        self.assertEqual(policy.REASON_READY_DIGEST, record["readiness"]["reason"])


class WriteRecordTests(SetupTestCase):
    def test_the_record_is_written_only_into_an_existing_directory(self):
        engine = self.engine()
        record, _error = self.run_setup(engine)
        missing = os.path.join(self.root.path, "not-there")
        path, reason = setup.write_record(record, missing)
        self.assertIsNone(path)
        self.assertEqual(setup.REASON_EVIDENCE_BASE, reason)
        self.assertFalse(os.path.exists(missing))

        base = os.path.join(self.root.path, "evidence")
        os.makedirs(base)
        path, reason = setup.write_record(record, base)
        self.assertIsNone(reason)
        expected_name = setup.record_filename(record)
        self.assertEqual(os.path.join(base, expected_name), path)
        with open(path, "r", encoding="utf-8") as handle:
            written = json.load(handle)
        self.assertEqual(record, written)
        self.assertEqual(sorted(os.listdir(base)), [expected_name])

    def test_a_different_record_is_a_second_file_not_a_replacement(self):
        # Evidence is append-only: a run that observed something else must not
        # be able to overwrite the record of a run that observed this.
        engine = self.engine()
        first, _error = self.run_setup(engine)
        base = os.path.join(self.root.path, "evidence")
        os.makedirs(base)
        first_path, _reason = setup.write_record(first, base)

        second, _error = self.run_setup(
            self.engine(image=_image_document(digest="sha256:" + "f" * 64))
        )
        self.assertNotEqual(setup.dumps(first), setup.dumps(second))
        second_path, reason = setup.write_record(second, base)
        self.assertIsNone(reason)
        self.assertNotEqual(first_path, second_path)
        self.assertEqual(2, len(os.listdir(base)))

        # Writing the same record twice is a no-op rather than a third file.
        again, reason = setup.write_record(first, base)
        self.assertIsNone(reason)
        self.assertEqual(first_path, again)
        self.assertEqual(2, len(os.listdir(base)))


class CliTests(SetupTestCase):
    def test_the_cli_reports_readiness_and_writes_the_record(self):
        from hrca import runner_image_setup_cli as cli

        base = os.path.join(self.root.path, "evidence")
        os.makedirs(base)
        argv = ["build", "--base", policy.BASE_REFERENCE, "--evidence-base", base]
        original = setup.RunnerImageSetup
        try:
            setup.RunnerImageSetup = lambda *a, **k: original(
                root=self.root.path, spawn=self.engine(), environ=self.environ
            )
            with _CapturedStreams() as streams:
                code = cli.main(argv)
        finally:
            setup.RunnerImageSetup = original
        self.assertEqual(cli.EXIT_READY, code)
        # The command reports where it wrote, and reports no refusal.
        self.assertIn("record written to", " ".join(streams.errors))
        self.assertNotIn("refused", " ".join(streams.errors))
        self.assertNotIn("not ready", " ".join(streams.errors))
        payload = json.loads(streams.outputs[0])
        self.assertEqual(policy.READY, payload["readiness"]["state"])
        self.assertTrue(
            os.path.isfile(os.path.join(base, setup.record_filename(payload)))
        )

    def test_a_usage_error_is_not_reported_as_not_ready(self):
        from hrca import runner_image_setup_cli as cli

        with _CapturedStreams():
            code = cli.main(["nonsense"])
        self.assertEqual(cli.EXIT_USAGE, code)
        self.assertNotEqual(cli.EXIT_NOT_READY, code)

    def test_the_cli_exits_non_zero_on_a_refused_base(self):
        from hrca import runner_image_setup_cli as cli

        original = setup.RunnerImageSetup
        try:
            setup.RunnerImageSetup = lambda *a, **k: original(
                root=self.root.path, spawn=self.engine(), environ=self.environ
            )
            with _CapturedStreams() as streams:
                code = cli.main(["inspect", "--base", policy.BASE_IMAGE])
        finally:
            setup.RunnerImageSetup = original
        self.assertEqual(cli.EXIT_NOT_READY, code)
        self.assertIn("refused", " ".join(streams.errors))


class _CapturedStreams:
    """Capture the CLI's two streams without leaving the terminal changed."""

    def __enter__(self):
        import io
        import sys

        self._stdout = sys.stdout
        self._stderr = sys.stderr
        self.out = io.StringIO()
        self.err = io.StringIO()
        sys.stdout = self.out
        sys.stderr = self.err
        self.outputs = []
        self.errors = []
        return self

    def __exit__(self, *exc_info):
        import sys

        sys.stdout = self._stdout
        sys.stderr = self._stderr
        self.outputs = self.out.getvalue().splitlines()
        self.errors = [line for line in self.err.getvalue().splitlines() if line.strip()]
        return False


if __name__ == "__main__":
    unittest.main()
