"""Code-owned runner-image setup policy (P5.5a-r3c).

The single place that decides **which** base the isolated runner image may be
built from, **how** a resolved manifest is bound to a local image identity, and
**when** the result is fit to be planned against. Nothing here is derived from a
caller, a candidate, a fixture or prose.

Why a setup policy exists at all
--------------------------------

P5.5a-r2 built ``hrca-runner:v1`` once and pinned the digest it produced. That
worked, but the *path* to that image — which base, resolved how, bound to what —
lived only in a commit message. This module is that path as code, so the next
rebuild is a reviewable artifact rather than a recollection.

The three identities it binds
-----------------------------

1. **the requested base** — ``python:3.12-slim@sha256:78387...``. The name is a
   convenience; the digest is the identity. A reference that carries only a tag
   is refused *by name* as mutable, because the same tag may point at different
   content tomorrow and an image built from it is an image nobody reviewed.
2. **the resolved platform manifest and config** — the index at that digest is
   resolved to exactly one image manifest for the engine's own platform, and
   that manifest names exactly one config blob. Exactly one: an index offering
   two candidates for one platform is ambiguous, and ambiguity is refused rather
   than resolved by order or by preference.
3. **the final local image** — the digest the rebuild actually produced, which
   has to equal :data:`RUNNER_IMAGE_DIGEST` before anything is ready to be
   planned against.

The base image's layer identities are carried through as well, so the built
image can be shown to *descend from* the resolved base rather than merely be
built near it.

The normal coding-agent boundary, not an exact-purpose proof
------------------------------------------------------------

An earlier revision of this work required proof of the *purpose* of every
daemon-side request. That proof is not obtainable on this host, and the
requirement was replaced by the ordinary coding-agent boundary: one pinned base,
anonymous access, the minimum official registry contact the engine needs, and an
honest disclosure of what was and was not observed. This module therefore never
claims zero egress, never treats a domain list as proof of intent, and separates
what the setup *observed* (the operations it dispatched and their outcomes) from
what it *declares* (the endpoint classes the official client documents).

OnBuild, and why it gates readiness
-----------------------------------

An ``OnBuild`` trigger in an image is a command that image asks a *child* build
to run. The accepted runner definition declares none, so a non-empty value means
the image being prepared is not the image this policy describes. An unreadable
or unverifiable value is treated identically: absence of evidence is not
evidence of absence, and readiness is refused either way.

Purity
------

Standard library only, no filesystem, no network, no process. It imports
nothing from this package, so it can neither dispatch anything nor read
anything. The values it shares with the runner adapter and the validation
policy are restated here as literals and asserted equal by the tests, so a
drift is a failing test rather than a quiet disagreement.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

POLICY_VERSION = "1.0.0"

# -- the base, by immutable identity ---------------------------------------

BASE_IMAGE = "python:3.12-slim"
BASE_INDEX_DIGEST = (
    "sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"
)
BASE_REFERENCE = BASE_IMAGE + "@" + BASE_INDEX_DIGEST

# -- the final local image, by mutable tag *and* immutable digest ----------
#
# The tag is what a rebuild produces; the digest is what a later run must
# observe. Only the digest is authority; the tag is a name for humans.
RUNNER_IMAGE = "hrca-runner:v1"
RUNNER_IMAGE_DIGEST = (
    "sha256:0809a47a00fcce555500b02d6645b68a565ad2a8299416bd9aa02f459ebaf258"
)

# The runner definition that must name exactly this base, repository-relative.
DOCKERFILE_PATH = "packaging/runner/Dockerfile"

# -- platform --------------------------------------------------------------

PLATFORM_OS = "linux"

# The engine reports its own architecture vocabulary (``x86_64``); a manifest
# index uses the OCI one (``amd64``). The mapping is code-owned and bounded: an
# architecture this map does not know is refused rather than guessed at.
ARCHITECTURE_ALIASES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}

IMAGE_MANIFEST_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)

# A buildx attestation rides in an index beside the real platform manifests and
# declares no usable platform, so it can never be the manifest a build used.
ATTESTATION_REFERENCE_TYPE = "vnd.docker.reference.type"
ATTESTATION_TYPE = "attestation-manifest"

# -- registry identity -----------------------------------------------------
#
# What is checked here is the *resolved reference*, never a network destination:
# a reference the engine resolves to a registry outside this set is refused
# before anything is dispatched. That is a name check with a stop condition, not
# a claim about which host any request reached.
ALLOWED_REGISTRY_HOSTS = (
    "docker.io",
    "registry-1.docker.io",
    "index.docker.io",
    "auth.docker.io",
    "production.cloudflare.docker.com",
)
DOCKER_HUB_HOST = "docker.io"
OFFICIAL_REPOSITORY_PREFIX = "library/"

# -- credentials -----------------------------------------------------------
#
# Values are never read, recorded or compared: only the *presence* of a
# credential-bearing ambient value, and only its name.
CREDENTIAL_ENV_NAMES = (
    "DOCKER_AUTH_CONFIG",
    "DOCKER_USERNAME",
    "DOCKER_PASSWORD",
    "DOCKER_TOKEN",
    "DOCKER_PAT",
    "REGISTRY_AUTH_FILE",
    "REGISTRY_USERNAME",
    "REGISTRY_PASSWORD",
)
CREDENTIAL_CONFIG_KEYS = ("auths", "credsStore", "credHelpers")
CLIENT_CONFIG_FILENAME = "config.json"
# The client's own configuration directory, redirected so the setup runs with no
# auth entry and no credential helper available to it at all.
CLIENT_CONFIG_ENV = "DOCKER_CONFIG"

# -- bounded reasons -------------------------------------------------------
#
# Every refusal below is one code-owned sentence. A caller value is never
# echoed, and no internal exception text is ever surfaced.

REASON_BASE_NOT_TEXT = "the base reference is not text"
REASON_BASE_TAG_ONLY = "the base reference carries no digest, so it is mutable"
REASON_BASE_NOT_DIGEST = "the base reference carries no usable sha256 digest"
REASON_BASE_NAME = "the base reference names an image other than the pinned base"
REASON_BASE_MISMATCH = "the base reference digest is not the pinned base digest"
REASON_REGISTRY_HOST = "the base reference names a registry outside the allowed set"
REASON_MANIFEST_NOT_MAPPING = "the resolved manifest is not a mapping"
REASON_PLATFORM_UNKNOWN = "the engine architecture is not one this policy can map"
REASON_PLATFORM_MISSING = "the index has no image manifest for the engine platform"
REASON_PLATFORM_AMBIGUOUS = "the index has more than one image manifest for the engine platform"
REASON_MANIFEST_NOT_IMAGE = "the resolved manifest is not an image manifest"
REASON_CONFIG_DIGEST = "the image manifest carries no usable config digest"
REASON_CONFIG_UNREADABLE = "the base config carries no usable layer identities"
REASON_ONBUILD_PRESENT = "the image declares OnBuild triggers"
REASON_ONBUILD_UNVERIFIABLE = "the image's OnBuild state could not be read"
REASON_DOCKERFILE_UNREADABLE = "the runner Dockerfile could not be read"
REASON_DOCKERFILE_FROM = "the runner Dockerfile does not build from the pinned base"
REASON_DOCKERFILE_FROM_MULTIPLE = "the runner Dockerfile names more than one base"
REASON_LINEAGE_MISMATCH = "the built image does not descend from the resolved base"
REASON_READY_BASE = "the pinned base identity was not verified"
REASON_READY_PLATFORM = "no platform manifest was resolved for the engine"
REASON_READY_CONFIG = "no base config was resolved"
REASON_READY_DOCKERFILE = "the runner Dockerfile was not verified against the pinned base"
REASON_READY_BUILD = "no image build succeeded"
REASON_READY_IMAGE = "no local runner image identity was recorded"
REASON_READY_DIGEST = "the recorded runner image digest is not the pinned digest"

# -- readiness -------------------------------------------------------------
#
# Two states, and only two. Readiness is a gate in front of *planning*: it says
# the artifact a later run would be bound to is the artifact this policy
# describes. It is not a validation result and it creates none.

READY = "ready_for_validation_planning"
NOT_READY = "not_ready"

# What this artifact is not, always and regardless of what a run did. The false
# entries are the point.
CLAIMS = {
    "zero_egress_proof": False,
    "exact_purpose_proof": False,
    "candidate_validation_success": False,
    "candidate_executed": False,
    "candidate_mounted": False,
    "plan_created": False,
    "attempt_created": False,
    "result_created": False,
    "approved": False,
    "adopted": False,
    "applied": False,
    "source_modified": False,
    "remote_pushed": False,
}

# The one claim that depends on what a run actually did. The setup's only
# registry-reaching operations are these two, so the adapter sets this from what
# it dispatched: a run that refused before reaching the registry — an unreachable
# daemon, a base that is not the pinned base — must not describe itself as
# networked setup evidence. Claiming the category on a run that made no contact
# would be exactly the kind of stronger-than-observed claim this record exists to
# avoid.
NETWORKED_CLAIM = "networked_setup_evidence"
NETWORK_OPERATIONS = (
    "buildx imagetools inspect",
    "build",
)

# The contact classes a setup run can observe, because they name operations it
# dispatched itself and whose outcomes it read back. The record carries the ones
# that actually happened: a run that dispatched nothing, or that built entirely
# from cache, must not borrow a class it never reached.
CONTACT_INDEX_RESOLUTION = "base index manifest resolution"
CONTACT_PLATFORM_RESOLUTION = "base platform manifest resolution"
CONTACT_CONFIG_RESOLUTION = "base image config resolution"
CONTACT_LAYER_ACQUISITION = "base layer acquisition at build time"
CONTACT_LAYER_CACHE_LOOKUP = "base layer cache lookup at build time"

# The three resolutions a successful base resolution performs, in the order it
# performs them.
CONTACT_RESOLUTION_CLASSES = (
    CONTACT_INDEX_RESOLUTION,
    CONTACT_PLATFORM_RESOLUTION,
    CONTACT_CONFIG_RESOLUTION,
)

# The endpoint classes the official client's documented behaviour uses for those
# operations. Declared, not observed: no destination was captured here.
DECLARED_ENDPOINT_CLASSES = (
    "the Docker Hub registry manifest API for the pinned base index, its platform manifest and its config blob",
    "the Docker Hub token endpoint the official client uses to obtain an anonymous pull token",
    "the Docker Hub content endpoints that serve manifest and blob bytes, including the redirect target the registry names for blob delivery",
)

# What this setup cannot see, stated where a future reader will look for it.
CONTACT_UNOBSERVED = (
    "request destinations, ports, paths, query strings and payloads",
    "whether any request carried a token, and whether the client consulted a credential helper",
    "which declared endpoint class served any individual artifact",
    "whether the engine also contacted an endpoint outside the declared classes",
)

LIMITATIONS = (
    "this is networked setup evidence: the build reached the official registry over the network, and nothing here is a zero-egress proof",
    "the endpoint classes are declared from the official client's documented behaviour; no destination was observed, so they do not prove the purpose of any request",
    "the docker client on this host is a Windows binary and the daemon and BuildKit run in the Docker Desktop VM, so client-side and daemon-side traffic are outside this setup's observation",
    "the credential-free property is constructed rather than merely observed: every command runs with DOCKER_CONFIG pointing at a fresh empty directory, so the client has no stored auth entry and no credential helper configured for the run",
    "a credential helper configured in the ambient client configuration is disclosed as a boolean and is never read, invoked or named by this setup",
    "the client configuration checked for credentials is the one this process can compute from DOCKER_CONFIG or the home directory; a platform-specific client configuration outside that path is not visible to it",
    "no candidate was mounted, executed or validated: preparing an image is not a validation result, and this setup creates no plan, attempt or result",
    "a rebuild may produce a different image digest than the pinned one; until the pin is updated the artifact is not ready, and readiness says so rather than re-pinning itself",
    "the record is deterministic for one environment, not across environments: fields that describe the environment — a client configuration path, Git's view of the working tree — differ when the environment does",
    "the builder's own cache is not an image and does not appear in the image list, so a change to it is outside what these before/after observations cover",
)

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_LEADING_FLAG_RE = re.compile(r"^--[A-Za-z0-9][A-Za-z0-9_.-]*(=[^\s]*)?$")


def is_digest(value: Any) -> bool:
    """Return True when ``value`` is a ``sha256:`` digest and nothing else."""
    return isinstance(value, str) and bool(_DIGEST_RE.match(value))


def split_name(name: str) -> Tuple[str, str, str]:
    """Split ``name`` into ``(registry, repository, tag)``.

    Docker Hub's official images are named without a repository prefix, so
    ``python`` is normalised to registry ``docker.io`` and repository
    ``library/python`` — the same canonical name the engine itself reports.
    """
    remainder = name
    registry = DOCKER_HUB_HOST
    head, slash, tail = remainder.partition("/")
    if slash and ("." in head or ":" in head or head == "localhost"):
        registry = head
        remainder = tail
    repository, tag_sep, tag = remainder.partition(":")
    if registry == DOCKER_HUB_HOST and slash == "":
        repository = OFFICIAL_REPOSITORY_PREFIX + repository
    return registry, repository, tag if tag_sep else ""


def parse_reference(text: Any) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """Return ``(parts, reason)`` for one image reference.

    A reference without ``@`` is refused as mutable rather than upgraded: the
    tag it carries may point at different content on the next pull, so it cannot
    identify a base. Everything else that cannot be read as *name*@*sha256
    digest* is refused too, and no part of the caller's text is echoed back.
    """
    if not isinstance(text, str):
        return None, REASON_BASE_NOT_TEXT
    candidate = text.strip()
    if not candidate:
        return None, REASON_BASE_NOT_TEXT
    name_part, separator, digest = candidate.partition("@")
    if not separator:
        return None, REASON_BASE_TAG_ONLY
    if not name_part or not is_digest(digest):
        return None, REASON_BASE_NOT_DIGEST
    registry, repository, tag = split_name(name_part)
    if not repository or not all(
        segment and all(ch.isalnum() or ch in "._-" for ch in segment)
        for segment in repository.split("/")
    ):
        return None, REASON_BASE_NAME
    canonical = registry + "/" + repository + (":" + tag if tag else "")
    return {
        "reference": candidate,
        "name": name_part,
        "registry": registry,
        "repository": repository,
        "tag": tag,
        "digest": digest,
        "canonical_name": canonical,
        "canonical_reference": canonical + "@" + digest,
    }, None


def verify_base_reference(
    text: Any, expected: str = BASE_REFERENCE
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """Return ``(parts, reason)`` for a requested base against the pinned one.

    The check is exact in both directions: the name has to be the pinned base's
    canonical name and the digest has to be the pinned digest. A reference that
    satisfies only one of them is refused, so neither a re-pointed tag nor a
    same-named different image can be substituted quietly.
    """
    parts, reason = parse_reference(text)
    if reason is not None:
        return None, reason
    if parts["registry"] not in ALLOWED_REGISTRY_HOSTS:
        return None, REASON_REGISTRY_HOST
    expected_parts, expected_reason = parse_reference(expected)
    if expected_reason is not None:  # pragma: no cover - the pin is code-owned
        return None, expected_reason
    if parts["canonical_name"] != expected_parts["canonical_name"]:
        return None, REASON_BASE_NAME
    if parts["digest"] != expected_parts["digest"]:
        return None, REASON_BASE_MISMATCH
    return parts, None


def normalize_architecture(value: Any) -> Optional[str]:
    """Map an engine architecture to its OCI name, or ``None`` if unknown."""
    if not isinstance(value, str):
        return None
    return ARCHITECTURE_ALIASES.get(value.strip().lower())


def select_platform_manifest(
    index: Any, os_name: str, architecture: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return the one image manifest an index offers for a platform.

    Attestation entries are skipped by their declared reference type and by
    their platform, index entries are skipped by media type, and an entry
    without a usable digest cannot be a candidate. If that leaves exactly one
    match it is returned; zero and more-than-one are both refusals.
    """
    if not isinstance(index, dict):
        return None, REASON_MANIFEST_NOT_MAPPING
    manifests = index.get("manifests")
    if not isinstance(manifests, list):
        return None, REASON_MANIFEST_NOT_MAPPING
    matches: List[Dict[str, Any]] = []
    for entry in manifests:
        if not isinstance(entry, dict):
            continue
        if entry.get("mediaType") not in IMAGE_MANIFEST_TYPES:
            continue
        annotations = entry.get("annotations")
        if isinstance(annotations, dict):
            if annotations.get(ATTESTATION_REFERENCE_TYPE) == ATTESTATION_TYPE:
                continue
        platform = entry.get("platform")
        if not isinstance(platform, dict):
            continue
        if platform.get("os") != os_name:
            continue
        if platform.get("architecture") != architecture:
            continue
        if not is_digest(entry.get("digest")):
            continue
        matches.append(entry)
    if not matches:
        return None, REASON_PLATFORM_MISSING
    if len(matches) > 1:
        return None, REASON_PLATFORM_AMBIGUOUS
    return dict(matches[0]), None


def config_digest_from_manifest(
    manifest: Any,
) -> Tuple[Optional[str], Optional[str]]:
    """Return the config blob digest an image manifest names."""
    if not isinstance(manifest, dict):
        return None, REASON_MANIFEST_NOT_MAPPING
    if manifest.get("mediaType") not in IMAGE_MANIFEST_TYPES:
        return None, REASON_MANIFEST_NOT_IMAGE
    config = manifest.get("config")
    if not isinstance(config, dict) or not is_digest(config.get("digest")):
        return None, REASON_CONFIG_DIGEST
    return config["digest"], None


def manifest_layer_count(manifest: Any) -> Optional[int]:
    """Return the number of layers an image manifest declares, or ``None``."""
    if not isinstance(manifest, dict):
        return None
    layers = manifest.get("layers")
    if not isinstance(layers, list):
        return None
    return len(layers)


def local_config_shape(image: Any) -> Dict[str, Any]:
    """Adapt a local ``docker image inspect`` document to the registry shape.

    A registry config blob names its sections ``config`` and ``rootfs``; the
    local inspect document names the same things ``Config`` and ``RootFS``. One
    shape is the adapter's, one is the contract's, and mapping between them in
    a single named place keeps the checks above written against one of them
    rather than against whichever document happened to be in hand.
    """
    if not isinstance(image, dict):
        return {}
    rootfs = image.get("RootFS")
    return {
        "config": image.get("Config"),
        "rootfs": {
            "diff_ids": (rootfs or {}).get("Layers") if isinstance(rootfs, dict) else None,
        },
    }


def diff_ids(config: Any) -> Tuple[Optional[List[str]], Optional[str]]:
    """Return the base config's layer identities (``rootfs.diff_ids``)."""
    if not isinstance(config, dict):
        return None, REASON_CONFIG_UNREADABLE
    rootfs = config.get("rootfs")
    if not isinstance(rootfs, dict):
        return None, REASON_CONFIG_UNREADABLE
    identifiers = rootfs.get("diff_ids")
    if not isinstance(identifiers, list) or not identifiers:
        return None, REASON_CONFIG_UNREADABLE
    if not all(is_digest(item) for item in identifiers):
        return None, REASON_CONFIG_UNREADABLE
    return list(identifiers), None


def onbuild_reason(config: Any) -> Optional[str]:
    """Return a bounded reason unless the image's OnBuild state is clear.

    ``None`` — the key absent, a null value, an empty string or an empty list —
    is clear. A non-empty string or list is :data:`REASON_ONBUILD_PRESENT`. Any
    other shape, or a config that carries no ``config`` section at all, is
    :data:`REASON_ONBUILD_UNVERIFIABLE`.
    """
    if not isinstance(config, dict):
        return REASON_ONBUILD_UNVERIFIABLE
    section = config.get("config")
    if not isinstance(section, dict):
        return REASON_ONBUILD_UNVERIFIABLE
    if "OnBuild" not in section:
        return None
    value = section["OnBuild"]
    if value is None:
        return None
    if isinstance(value, str):
        return REASON_ONBUILD_PRESENT if value.strip() else None
    if isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            return REASON_ONBUILD_UNVERIFIABLE
        return REASON_ONBUILD_PRESENT if value else None
    return REASON_ONBUILD_UNVERIFIABLE


def from_instruction(text: Any) -> Tuple[Optional[List[str]], Optional[str]]:
    """Return ``(references, reason)`` for the ``FROM`` lines of a Dockerfile.

    Comments, blank lines and leading build flags are skipped; a ``FROM`` whose
    remaining tokens are not ``<reference> [AS <name>]`` is refused rather than
    partially read. More than one ``FROM`` is refused by name, because a
    multi-stage file could hide the base this policy is supposed to pin.
    """
    if not isinstance(text, str):
        return None, REASON_DOCKERFILE_UNREADABLE
    references: List[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line[:4].lower() != "from":
            continue
        rest = line[4:]
        if rest[:1] not in (" ", "\t"):
            continue
        tokens = rest.split()
        while tokens and _LEADING_FLAG_RE.match(tokens[0]):
            tokens.pop(0)
        if not tokens:
            return None, REASON_DOCKERFILE_FROM
        if len(tokens) > 1 and tokens[1].lower() != "as":
            return None, REASON_DOCKERFILE_FROM
        references.append(tokens[0])
    if not references:
        return None, REASON_DOCKERFILE_FROM
    if len(references) > 1:
        return None, REASON_DOCKERFILE_FROM_MULTIPLE
    return references, None


def dockerfile_reason(
    text: Any, expected: str = BASE_REFERENCE
) -> Optional[str]:
    """Return a bounded reason unless the Dockerfile builds from the pinned base.

    The comparison is by *identity*, not by spelling: the ``FROM`` reference and
    the expected one are each parsed, and their canonical names and digests must
    agree. Comparing the raw strings instead would refuse the very reference
    this policy publishes — ``docker.io/library/python:3.12-slim@sha256:…`` is
    the pinned base written the way the engine writes it — and would record the
    false claim that the reviewed Dockerfile builds from something else.
    """
    references, reason = from_instruction(text)
    if reason is not None:
        return reason
    parsed, parsed_reason = parse_reference(references[0])
    if parsed_reason is not None:
        return REASON_DOCKERFILE_FROM
    expected_parts, expected_reason = parse_reference(expected)
    if expected_reason is not None:  # pragma: no cover - the pin is code-owned
        return expected_reason
    if parsed["canonical_name"] != expected_parts["canonical_name"]:
        return REASON_DOCKERFILE_FROM
    if parsed["digest"] != expected_parts["digest"]:
        return REASON_DOCKERFILE_FROM
    return None


def lineage_reason(
    base_ids: Any, image_ids: Any
) -> Optional[str]:
    """Return a bounded reason unless the image descends from the base layers.

    An image built ``FROM`` a base begins with that base's layer identities in
    order. The comparison is a prefix comparison of exact digests, so it binds
    the built image to the *resolved* base rather than to a tag, a name or a
    build log line.
    """
    if not isinstance(base_ids, list) or not base_ids:
        return REASON_LINEAGE_MISMATCH
    if not isinstance(image_ids, list) or len(image_ids) < len(base_ids):
        return REASON_LINEAGE_MISMATCH
    if list(image_ids[: len(base_ids)]) != list(base_ids):
        return REASON_LINEAGE_MISMATCH
    return None


def readiness(
    *,
    base_identity_verified: bool = False,
    platform_manifest_resolved: bool = False,
    config_resolved: bool = False,
    base_onbuild_reason: Optional[str] = REASON_ONBUILD_UNVERIFIABLE,
    dockerfile_verified: bool = False,
    build_succeeded: bool = False,
    runner_recorded: bool = False,
    runner_onbuild_reason: Optional[str] = REASON_ONBUILD_UNVERIFIABLE,
    lineage_mismatch_reason: Optional[str] = REASON_LINEAGE_MISMATCH,
    runner_digest_matches_pin: bool = False,
) -> Tuple[str, Optional[str]]:
    """Return ``(state, reason)`` for the artifact the setup prepared.

    The gates are ordered from the most fundamental identity to the least, and
    the first one that fails supplies the bounded reason. An OnBuild or lineage
    reason is passed through as given, so a refusal names its own cause instead
    of a generic "not ready". The lineage parameter is named for the *reason*
    rather than for the function below it, so neither shadows the other.
    """
    if not base_identity_verified:
        return NOT_READY, REASON_READY_BASE
    if not platform_manifest_resolved:
        return NOT_READY, REASON_READY_PLATFORM
    if not config_resolved:
        return NOT_READY, REASON_READY_CONFIG
    if base_onbuild_reason is not None:
        return NOT_READY, base_onbuild_reason
    if not dockerfile_verified:
        return NOT_READY, REASON_READY_DOCKERFILE
    if not build_succeeded:
        return NOT_READY, REASON_READY_BUILD
    if not runner_recorded:
        return NOT_READY, REASON_READY_IMAGE
    if runner_onbuild_reason is not None:
        return NOT_READY, runner_onbuild_reason
    if lineage_mismatch_reason is not None:
        return NOT_READY, lineage_mismatch_reason
    if not runner_digest_matches_pin:
        return NOT_READY, REASON_READY_DIGEST
    return READY, None


__all__ = [
    "POLICY_VERSION",
    "BASE_IMAGE",
    "BASE_INDEX_DIGEST",
    "BASE_REFERENCE",
    "RUNNER_IMAGE",
    "RUNNER_IMAGE_DIGEST",
    "DOCKERFILE_PATH",
    "PLATFORM_OS",
    "ARCHITECTURE_ALIASES",
    "IMAGE_MANIFEST_TYPES",
    "ATTESTATION_REFERENCE_TYPE",
    "ATTESTATION_TYPE",
    "ALLOWED_REGISTRY_HOSTS",
    "DOCKER_HUB_HOST",
    "OFFICIAL_REPOSITORY_PREFIX",
    "CREDENTIAL_ENV_NAMES",
    "CREDENTIAL_CONFIG_KEYS",
    "CLIENT_CONFIG_FILENAME",
    "CLIENT_CONFIG_ENV",
    "CLAIMS",
    "NETWORKED_CLAIM",
    "NETWORK_OPERATIONS",
    "CONTACT_INDEX_RESOLUTION",
    "CONTACT_PLATFORM_RESOLUTION",
    "CONTACT_CONFIG_RESOLUTION",
    "CONTACT_LAYER_ACQUISITION",
    "CONTACT_LAYER_CACHE_LOOKUP",
    "CONTACT_RESOLUTION_CLASSES",
    "DECLARED_ENDPOINT_CLASSES",
    "CONTACT_UNOBSERVED",
    "LIMITATIONS",
    "READY",
    "NOT_READY",
    "REASON_BASE_NOT_TEXT",
    "REASON_BASE_TAG_ONLY",
    "REASON_BASE_NOT_DIGEST",
    "REASON_BASE_NAME",
    "REASON_BASE_MISMATCH",
    "REASON_REGISTRY_HOST",
    "REASON_MANIFEST_NOT_MAPPING",
    "REASON_PLATFORM_UNKNOWN",
    "REASON_PLATFORM_MISSING",
    "REASON_PLATFORM_AMBIGUOUS",
    "REASON_MANIFEST_NOT_IMAGE",
    "REASON_CONFIG_DIGEST",
    "REASON_CONFIG_UNREADABLE",
    "REASON_ONBUILD_PRESENT",
    "REASON_ONBUILD_UNVERIFIABLE",
    "REASON_DOCKERFILE_UNREADABLE",
    "REASON_DOCKERFILE_FROM",
    "REASON_DOCKERFILE_FROM_MULTIPLE",
    "REASON_LINEAGE_MISMATCH",
    "REASON_READY_BASE",
    "REASON_READY_PLATFORM",
    "REASON_READY_CONFIG",
    "REASON_READY_DOCKERFILE",
    "REASON_READY_BUILD",
    "REASON_READY_IMAGE",
    "REASON_READY_DIGEST",
    "is_digest",
    "split_name",
    "parse_reference",
    "verify_base_reference",
    "normalize_architecture",
    "select_platform_manifest",
    "config_digest_from_manifest",
    "manifest_layer_count",
    "local_config_shape",
    "diff_ids",
    "onbuild_reason",
    "from_instruction",
    "dockerfile_reason",
    "lineage_reason",
    "readiness",
]
