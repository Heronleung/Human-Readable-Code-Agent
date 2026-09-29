"""Bounded runner-image setup adapter (P5.5a-r3c).

The one place that *prepares* the isolated runner image, and the one place that
observes what preparing it touched. It runs the Docker client only — never a
container, never a candidate, never the host Python.

What it does
------------

1. refuses a base that is not the pinned base, before dispatching anything;
2. resolves that base to one platform manifest and one config blob and reads
   the base's own layer identities;
3. verifies the repository's runner Dockerfile builds from exactly that base;
4. runs one bounded ``docker build`` of that Dockerfile;
5. reads the resulting image's identity, OnBuild state and layer lineage;
6. records what changed and what did not, and whether the artifact is ready to
   be planned against.

What it never does
------------------

It mounts nothing, executes nothing, and validates nothing. It creates no plan,
attempt or result, approves nothing and adopts nothing. The only file it writes
is one record inside a caller-supplied evidence base; the only durable change it
can cause is the local image the build produces.

The credential boundary is constructed, not merely asserted
-----------------------------------------------------------

Every Docker command runs with :data:`hrca.runner_image_policy.CLIENT_CONFIG_ENV`
pointing at a fresh empty directory created for this setup, and with every
credential-bearing ambient variable removed from the child environment. That is
what makes "no credential and no credential helper was available to this run" a
property of the run rather than a hope about the host: a client cannot read an
auth entry or invoke a helper that is not in the directory it is told to use. An
ambient credential helper is still *disclosed* — as a boolean, never by name and
never by reading it — because a reviewer is entitled to know it exists.

What this record is not
-----------------------

It is networked setup evidence. The build reaches the official registry, and no
destination, port, path or payload was captured: the client here is a Windows
binary and the daemon and BuildKit run in the Docker Desktop VM, so both sides
of that traffic are outside this process's view. The endpoint *classes* are
declared from the official client's documented behaviour, and the only host
names this record contains are ones the builder itself printed. Neither is a
proof of purpose, and neither is a zero-egress claim.

Timeouts
--------

``subprocess.run(timeout=...)`` raises :class:`subprocess.TimeoutExpired`, which
is not a :class:`TimeoutError`. Every bounded call here catches both names, so a
real timeout is reported as a bounded reason instead of escaping as an
exception.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import runner_image_policy as policy

RECORD_NAME = "hrca-runner-image-setup"
RECORD_SCHEMA_VERSION = "1.1.0"
# The record is named for its own content, so a later run with a different
# outcome writes a *second* file rather than replacing the first: evidence is
# append-only here for the same reason the validation contract makes it so.
RECORD_PREFIX = "setup-record-"
RECORD_SUFFIX = ".json"
RECORD_FILENAME_DIGEST_CHARS = 16

# Fixed bounds. No caller, fixture or prose value can tune them.
SETUP_TIMEOUT_SECONDS = 120.0
BUILD_TIMEOUT_SECONDS = 900.0
MAX_EVENT_LINES = 240
MAX_EVENT_LINE_CHARS = 200
MAX_SNAPSHOT_FILES = 512
# The container format deliberately omits ``.Status``: it is a human uptime
# string ("Up 59 minutes" becomes "Up About an hour"), so comparing it would
# report an environment change that no one made and make the record depend on
# the wall clock.
CONTAINER_FORMAT = "{{.ID}}|{{.Names}}|{{.Image}}"
IMAGE_FORMAT = "{{.ID}}|{{.Repository}}:{{.Tag}}"

_CLIENT_CONFIG_PREFIX = "hrca-setup-config-"
_HOST_IN_OUTPUT_RE = re.compile(
    r"\b([a-z0-9][a-z0-9.\-]*\.[a-z]{2,})(?=/[a-zA-Z0-9])"
)
# Context and Dockerfile transfer is the *client* handing bytes to the daemon.
# It is not registry acquisition, and calling it acquisition would tell a reader
# that layers came off the network when none did.
_CONTEXT_MARKERS = (
    "transferring dockerfile",
    "transferring context",
    "load build definition",
    "load build context",
    "load .dockerignore",
)
_ACQUISITION_MARKERS = (
    "extracting sha256:",
    "pulling",
    "downloading",
    "fetching",
)
_RESOLUTION_MARKERS = (
    "resolve image config",
    "load metadata",
    "resolve dockerfile",
    "resolve source metadata",
    "resolve docker.io",
)

# Bounded reasons. A caller value and an internal exception never reach these.
REASON_DOCKER_ABSENT = "the docker client could not be run"
REASON_DOCKER_FAILED = "a docker command failed"
REASON_DOCKER_TIMEOUT = "a docker command exceeded its bound"
REASON_DOCKER_OUTPUT = "a docker command returned output that could not be read"
REASON_DAEMON_UNREACHABLE = "the container daemon could not be reached"
REASON_CLIENT_ENV = "the isolated client configuration directory could not be created"
REASON_DOCKERFILE_UNREADABLE = "the runner Dockerfile could not be read"
REASON_BUILD_FAILED = "the image build did not succeed"
REASON_IMAGE_ABSENT = "the local runner image could not be read"
REASON_EVIDENCE_BASE = "the evidence base is not a directory"
REASON_EVIDENCE_WRITE = "the record could not be written"

OUTCOME_REFUSED = "refused"
OUTCOME_BUILT = "built"
OUTCOME_INSPECTED = "inspected"

_GROUP_NOTE = (
    "no container is dispatched, no candidate root is written, and nothing is "
    "staged or committed: each named candidate root is observed by content "
    "digest before and after, and the build context is the repository root"
)
_CONTEXT_NOTE = (
    "the build context is the repository root and the builder transfers the "
    "dockerfile and the files the Dockerfile copies; which lines it printed and "
    "how many bytes it moved are in this record's build events, so what left the "
    "tree is readable rather than assumed"
)
_GIT_NOTE = (
    "Git's view of an untracked file is its name alone, so this record does not "
    "and cannot verify the content of any untracked file"
)


def repository_root() -> str:
    """Return the repository root this module lives in."""
    # Three levels up: this module's package, ``hrca``, then ``src``.
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(os.path.dirname(here)))


def dumps(obj: Any) -> str:
    """Serialize a record canonically."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def dumps_pretty(obj: Any) -> str:
    """Serialize a record for review; the mapping is identical to :func:`dumps`."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, indent=2)


def record_filename(record: Dict[str, Any]) -> str:
    """Return the content-addressed filename for one record.

    Two runs that observed the same thing produce the same bytes and therefore
    the same name — writing one over the other is then a no-op. Two runs that
    observed different things produce different names, so neither can silently
    replace the other.
    """
    digest = hashlib.sha256(dumps(record).encode("utf-8")).hexdigest()
    return RECORD_PREFIX + digest[:RECORD_FILENAME_DIGEST_CHARS] + RECORD_SUFFIX


def _bounded(lines: List[str]) -> List[str]:
    """Return at most :data:`MAX_EVENT_LINES` lines, each at most the line bound."""
    return [line[:MAX_EVENT_LINE_CHARS] for line in lines[:MAX_EVENT_LINES]]


def classify_build_output(text: str) -> Dict[str, Any]:
    """Classify a build log into bounded events, without inventing any.

    The raw lines are kept (bounded in count and length) beside the coarse
    classification, so a reviewer reads the builder's own words rather than this
    function's summary of them. Context transfer, registry acquisition and cache
    hits are three separate answers: a build that moved 18 kB of source into the
    daemon and pulled no layer is a cache hit on layers, not an acquisition.
    """
    events: List[str] = []
    acquisition: List[str] = []
    cached: List[str] = []
    resolution: List[str] = []
    context: List[str] = []
    total = 0
    for raw in (text or "").splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        total += 1
        if len(events) < MAX_EVENT_LINES:
            events.append(line[:MAX_EVENT_LINE_CHARS])
        lowered = line.lower()
        if "cached" in lowered:
            cached.append(line[:MAX_EVENT_LINE_CHARS])
            continue
        if any(marker in lowered for marker in _ACQUISITION_MARKERS):
            acquisition.append(line[:MAX_EVENT_LINE_CHARS])
            continue
        if any(marker in lowered for marker in _CONTEXT_MARKERS):
            context.append(line[:MAX_EVENT_LINE_CHARS])
            continue
        if any(marker in lowered for marker in _RESOLUTION_MARKERS):
            resolution.append(line[:MAX_EVENT_LINE_CHARS])
    if not events:
        state = "unknown"
    elif acquisition and cached:
        state = "mixed"
    elif acquisition:
        state = "acquired"
    elif cached:
        state = "cache_hit"
    else:
        state = "unknown"
    return {
        "lines": len(events),
        "log_lines": total,
        "events": events,
        "cache_state": state,
        "cached_steps": _bounded(cached),
        "acquired_steps": _bounded(acquisition),
        "context_steps": _bounded(context),
        "resolution_steps": _bounded(resolution),
    }


def hosts_named_in_output(text: str) -> List[str]:
    """Return the host names an engine printed, sorted and de-duplicated.

    This reads the *builder's own output*. It is not a capture of destinations,
    and a host appearing here is not evidence that anything was sent to it.
    """
    found = set()
    for match in _HOST_IN_OUTPUT_RE.finditer(text or ""):
        found.add(match.group(1))
    return sorted(found)


class RunnerImageSetup:
    """Prepare and observe the isolated runner image, within fixed bounds."""

    def __init__(
        self,
        *,
        root: Optional[str] = None,
        docker: str = "docker",
        spawn: Callable[..., Any] = subprocess.run,
        call_timeout: float = SETUP_TIMEOUT_SECONDS,
        build_timeout: float = BUILD_TIMEOUT_SECONDS,
        environ: Optional[Dict[str, str]] = None,
    ) -> None:
        self.root = root or repository_root()
        self.docker = docker
        self._spawn = spawn
        self.call_timeout = call_timeout
        self.build_timeout = build_timeout
        self._environ = dict(os.environ if environ is None else environ)
        self._config_dir: Optional[str] = None
        self._config_dir_was_empty: Optional[bool] = None
        self._client_env: Optional[Dict[str, str]] = None
        self.dispatched: List[List[str]] = []

    # -- the client environment --------------------------------------------

    def client_env(self) -> Dict[str, str]:
        """Return the environment a Docker command runs under.

        The client configuration directory is created empty on first use and
        every credential-bearing ambient variable is dropped, so the run has no
        credential to present and no helper to consult. The mapping handed to
        the child is kept, so what was removed can be read back from the child's
        environment rather than asserted.
        """
        if self._config_dir is None:
            directory = tempfile.mkdtemp(prefix=_CLIENT_CONFIG_PREFIX)
            self._config_dir = directory
            self._config_dir_was_empty = not os.listdir(directory)
        env = dict(self._environ)
        env[policy.CLIENT_CONFIG_ENV] = self._config_dir
        for name in policy.CREDENTIAL_ENV_NAMES:
            env.pop(name, None)
        self._client_env = env
        return dict(env)

    def close(self) -> None:
        """Remove the empty client configuration directory this setup created."""
        if self._config_dir is not None:
            shutil.rmtree(self._config_dir, ignore_errors=True)
            self._config_dir = None

    # -- bounded dispatch --------------------------------------------------

    def _run(
        self, argv: Sequence[str], timeout: float
    ) -> Tuple[Optional[Any], Optional[str]]:
        """Run one bounded Docker client call, or return a bounded reason."""
        command = [self.docker] + list(argv)
        try:
            env = self.client_env()
        except OSError:
            # Creating the isolated directory is this setup's own step, and a
            # failure there must not be reported as an unreachable client.
            return None, REASON_CLIENT_ENV
        self.dispatched.append(list(argv))
        try:
            proc = self._spawn(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                cwd=self.root,
            )
        except subprocess.TimeoutExpired:
            return None, REASON_DOCKER_TIMEOUT
        except TimeoutError:
            return None, REASON_DOCKER_TIMEOUT
        except OSError:
            return None, REASON_DOCKER_ABSENT
        return proc, None

    def _json(
        self, argv: Sequence[str], timeout: Optional[float] = None
    ) -> Tuple[Optional[Any], Optional[str]]:
        """Run a Docker call that must print one JSON document."""
        proc, reason = self._run(argv, self.call_timeout if timeout is None else timeout)
        if reason is not None:
            return None, reason
        if proc.returncode != 0:
            return None, REASON_DOCKER_FAILED
        try:
            return json.loads(proc.stdout), None
        except (ValueError, TypeError):
            return None, REASON_DOCKER_OUTPUT

    def _lines(
        self, argv: Sequence[str], timeout: Optional[float] = None
    ) -> Tuple[Optional[List[str]], Optional[str]]:
        """Run a Docker call and return its non-empty output lines."""
        proc, reason = self._run(argv, self.call_timeout if timeout is None else timeout)
        if reason is not None:
            return None, reason
        if proc.returncode != 0:
            return None, REASON_DOCKER_FAILED
        text = (proc.stdout or "") + (proc.stderr or "")
        return [line for line in text.splitlines() if line.strip()], None

    def _git(self, argv: Sequence[str]) -> List[str]:
        """Run one read-only Git query; an unavailable Git yields no lines."""
        try:
            proc = self._spawn(
                ["git"] + list(argv),
                capture_output=True,
                text=True,
                timeout=self.call_timeout,
                cwd=self.root,
            )
        except (subprocess.TimeoutExpired, TimeoutError, OSError):
            return []
        if proc.returncode != 0:
            return []
        return [line for line in (proc.stdout or "").splitlines() if line.strip()]

    # -- identities --------------------------------------------------------

    def client_identity(self) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Return the client, daemon, BuildKit and builder identities."""
        proc, reason = self._run(["version", "--format", "{{json .}}"], self.call_timeout)
        if reason is not None:
            return None, reason
        if proc.returncode != 0:
            return None, REASON_DAEMON_UNREACHABLE
        try:
            data = json.loads(proc.stdout)
        except (ValueError, TypeError):
            return None, REASON_DOCKER_OUTPUT
        if not isinstance(data, dict):
            return None, REASON_DOCKER_OUTPUT
        client = data.get("Client")
        server = data.get("Server")
        if not isinstance(server, dict) or not server.get("Version"):
            return None, REASON_DAEMON_UNREACHABLE
        engine_architecture = server.get("Arch")
        architecture = policy.normalize_architecture(engine_architecture)
        if architecture is None:
            return None, policy.REASON_PLATFORM_UNKNOWN
        buildx_lines, _buildx_reason = self._lines(["buildx", "version"])
        builder = self._builder_identity()
        return {
            "client_version": client.get("Version") if isinstance(client, dict) else None,
            "server_version": server.get("Version"),
            "server_platform": _platform_name(server),
            "engine_os": server.get("Os"),
            "engine_architecture": engine_architecture,
            "architecture": architecture,
            "buildx_version": _buildx_version(
                buildx_lines[0] if buildx_lines else None
            ),
            "builder": builder.get("name"),
            "builder_driver": builder.get("driver"),
            "buildkit_version": builder.get("buildkit"),
        }, None

    def _builder_identity(self) -> Dict[str, Optional[str]]:
        """Return the current builder's name, driver and BuildKit version.

        The current builder is the one the tool itself marks, so this records
        what the build will actually use rather than a builder this module
        selected.
        """
        lines, reason = self._lines(["buildx", "ls"])
        if reason is not None or not lines:
            return {"name": None, "driver": None, "buildkit": None}
        name: Optional[str] = None
        driver: Optional[str] = None
        buildkit: Optional[str] = None
        for index, line in enumerate(lines):
            if line.lstrip().startswith("\\_"):
                continue
            if "*" in line and name is None and index + 1 < len(lines):
                tokens = line.split()
                if tokens:
                    name = tokens[0].rstrip("*")
                    driver = tokens[1] if len(tokens) > 1 else None
                    for token in lines[index + 1].split():
                        if token.startswith("v") and token[1:2].isdigit():
                            buildkit = token
                            break
        return {"name": name, "driver": driver, "buildkit": buildkit}

    def base_reference_parts(
        self, base_reference: Any
    ) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
        """Verify a requested base against the pinned one (``None`` = refused)."""
        return policy.verify_base_reference(base_reference)

    # -- base resolution ---------------------------------------------------

    def resolve_base(
        self, base_reference: str = policy.BASE_REFERENCE
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Bind the requested base to one platform manifest and one config.

        The chain is identity-only at every step: the index digest the engine
        resolves must be the digest that was requested, the platform manifest
        must be the single one the index offers for the engine's own platform,
        and the config digest is read from that manifest's own bytes.
        """
        parts, reason = self.base_reference_parts(base_reference)
        if reason is not None:
            return None, reason
        identity, reason = self.client_identity()
        if reason is not None:
            return None, reason
        index, reason = self._json(
            [
                "buildx",
                "imagetools",
                "inspect",
                parts["reference"],
                "--format",
                "{{json .Manifest}}",
            ]
        )
        if reason is not None:
            return None, reason
        if not isinstance(index, dict):
            return None, policy.REASON_MANIFEST_NOT_MAPPING
        index_digest = index.get("digest")
        if not policy.is_digest(index_digest):
            return None, policy.REASON_MANIFEST_NOT_MAPPING
        if index_digest != parts["digest"]:
            return None, policy.REASON_BASE_MISMATCH
        manifest, reason = policy.select_platform_manifest(
            index, identity["engine_os"] or policy.PLATFORM_OS, identity["architecture"]
        )
        if reason is not None:
            return None, reason
        manifest_reference = parts["canonical_name"] + "@" + manifest["digest"]
        raw, reason = self._json(
            ["buildx", "imagetools", "inspect", "--raw", manifest_reference]
        )
        if reason is not None:
            return None, reason
        config_digest, reason = policy.config_digest_from_manifest(raw)
        if reason is not None:
            return None, reason
        config, reason = self._json(
            [
                "buildx",
                "imagetools",
                "inspect",
                manifest_reference,
                "--format",
                "{{json .Image}}",
            ]
        )
        if reason is not None:
            return None, reason
        identifiers, reason = policy.diff_ids(config)
        if reason is not None:
            return None, reason
        return {
            "requested_reference": parts["reference"],
            "requested_canonical": parts["canonical_reference"],
            "index_digest": index_digest,
            "platform": {
                "os": identity["engine_os"],
                "architecture": identity["architecture"],
                "engine_architecture": identity["engine_architecture"],
            },
            "platform_manifest_digest": manifest["digest"],
            "platform_manifest_reference": manifest_reference,
            "config_digest": config_digest,
            "layer_diff_ids": identifiers,
            "layer_count": policy.manifest_layer_count(raw),
            "onbuild_reason": policy.onbuild_reason(config),
            "client": identity,
        }, None

    # -- the local artifact -------------------------------------------------

    def runner_identity(self) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Read the local runner image's identity, OnBuild state and layers.

        The reference is the code-owned tag, never a caller value, so no caller
        text can reach this argv.
        """
        proc, reason = self._run(
            ["image", "inspect", policy.RUNNER_IMAGE, "--format", "{{json .}}"],
            self.call_timeout,
        )
        if reason is not None:
            return None, reason
        if proc.returncode != 0:
            return None, REASON_IMAGE_ABSENT
        try:
            image = json.loads(proc.stdout)
        except (ValueError, TypeError):
            return None, REASON_DOCKER_OUTPUT
        if not isinstance(image, dict):
            return None, REASON_DOCKER_OUTPUT
        section = image.get("Config")
        section = section if isinstance(section, dict) else {}
        rootfs = image.get("RootFS")
        layers = rootfs.get("Layers") if isinstance(rootfs, dict) else None
        return {
            "reference": policy.RUNNER_IMAGE,
            "digest": image.get("Id"),
            "created": image.get("Created"),
            "user": section.get("User"),
            "working_dir": section.get("WorkingDir"),
            "layer_diff_ids": list(layers) if isinstance(layers, list) else [],
            "onbuild_reason": policy.onbuild_reason(policy.local_config_shape(image)),
            "matches_pinned": image.get("Id") == policy.RUNNER_IMAGE_DIGEST,
            "pinned_digest": policy.RUNNER_IMAGE_DIGEST,
            "repository_tags": sorted(image.get("RepoTags") or []),
        }, None

    def dockerfile_reason(self) -> Optional[str]:
        """Return a bounded reason unless the Dockerfile builds from the pin."""
        text = _read_dockerfile(self.root)
        if text is None:
            return REASON_DOCKERFILE_UNREADABLE
        return policy.dockerfile_reason(text)

    # -- observation --------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """Observe the state a setup must not change.

        Everything here is read-only. Untracked files — including any bundle in
        the working directory — appear only as the names Git reports; their
        content is never opened, hashed or staged.
        """
        containers, containers_reason = self._lines(
            ["ps", "-a", "--no-trunc", "--format", CONTAINER_FORMAT]
        )
        images, images_reason = self._lines(
            ["images", "--no-trunc", "--format", IMAGE_FORMAT]
        )
        readable = containers_reason is None and images_reason is None
        containers = sorted(containers or [])
        images = sorted(images or [])
        runner_suffix = "|" + policy.RUNNER_IMAGE
        return {
            "git_head": (self._git(["rev-parse", "HEAD"]) or [None])[0],
            "git_status": sorted(self._git(["status", "--porcelain"])),
            "git_diff_stat": sorted(self._git(["diff", "HEAD", "--stat"])),
            "candidate_roots": self._candidate_roots(),
            "containers": containers,
            "images": images,
            "images_excluding_runner": sorted(
                line for line in images if not line.endswith(runner_suffix)
            ),
            "readable": readable,
        }

    def _candidate_roots(self) -> Dict[str, Dict[str, Any]]:
        """Return a content observation per candidate root in the repository.

        The roots are named by this module and are repository fixtures. The
        digest is over file bytes, so an unchanged observation means no file
        beneath the root was written, moved or removed — and the file count and
        the truncation flag are recorded beside it, because a digest taken over
        a prefix would otherwise be indistinguishable from a digest of the whole.
        """
        observations: Dict[str, Dict[str, Any]] = {}
        for name in ("candidate_fixtures",):
            path = os.path.join(self.root, name)
            if not os.path.isdir(path):
                observations[name] = {"state": "absent"}
                continue
            accumulator = hashlib.sha256()
            count = 0
            truncated = False
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames.sort()
                for filename in sorted(filenames):
                    if count >= MAX_SNAPSHOT_FILES:
                        truncated = True
                        break
                    full = os.path.join(dirpath, filename)
                    relative = os.path.relpath(full, path)
                    try:
                        with open(full, "rb") as handle:
                            body = handle.read()
                    except OSError:
                        accumulator.update(b"unreadable")
                        continue
                    accumulator.update(relative.encode("utf-8"))
                    accumulator.update(body)
                    count += 1
                if truncated:
                    break
            observations[name] = {
                "state": "digested",
                "digest": "sha256:" + accumulator.hexdigest(),
                "files": count,
                "truncated": truncated,
            }
        return observations

    # -- credentials --------------------------------------------------------

    def credential_evidence(self) -> Dict[str, Any]:
        """Return what could be established about credentials, and what not.

        Nothing here reads, records or compares a credential *value*. The
        ambient configuration is inspected for the presence of credential keys
        and the number of stored entries (which must be zero); a credential
        helper configured there is disclosed as a boolean only.
        """
        ambient = [self._config_summary(path) for path in self._ambient_config_paths()]
        present_env = sorted(
            name for name in policy.CREDENTIAL_ENV_NAMES if name in self._environ
        )
        # Read back from the environment the children actually received, so the
        # claim and the behaviour cannot drift apart.
        child_env = self._client_env
        removed_env = sorted(
            name
            for name in policy.CREDENTIAL_ENV_NAMES
            if name in self._environ
            and (child_env is None or name not in child_env)
        )
        isolated = None
        if self._config_dir is not None:
            isolated = self._config_summary(
                os.path.join(self._config_dir, policy.CLIENT_CONFIG_FILENAME)
            )
            isolated["role"] = "the empty client configuration directory this setup created"
            isolated["empty_at_creation"] = self._config_dir_was_empty
            # The concrete path is machine state, not evidence, and recording it
            # would make an otherwise deterministic record differ per run.
            isolated.pop("path", None)
        if isolated is None:
            effective: Dict[str, Any] = {
                "auth_entries_available": None,
                "credential_helper_available": None,
                "ambient_client_config_used": False,
                "note": "no Docker command was dispatched, so no client environment was built",
            }
        else:
            effective = {
                "auth_entries_available": isolated["auths_entries"],
                "credential_helper_available": bool(
                    isolated["creds_store_configured"]
                    or isolated["creds_helpers_configured"]
                ),
                "ambient_client_config_used": False,
            }
        logins = [
            argv for argv in self.dispatched if "login" in [t.lower() for t in argv]
        ]
        return {
            "login_dispatched": bool(logins),
            "credential_env_present_in_ambient_environment": present_env,
            "credential_env_removed_from_child_environment": removed_env,
            "credential_env_names_dropped_unconditionally": sorted(
                policy.CREDENTIAL_ENV_NAMES
            ),
            "client_config_isolation": {
                "variable": policy.CLIENT_CONFIG_ENV,
                "isolated": self._config_dir is not None,
                "isolated_directory_empty_at_creation": self._config_dir_was_empty,
            },
            "effective_for_the_run": effective,
            "isolated_config": isolated,
            "ambient_configs": ambient,
            "limitations": [
                "a credential helper configured in the ambient client configuration is disclosed as a boolean and is never read, invoked or named here",
                "the ambient configurations checked are the ones this process can compute from DOCKER_CONFIG and the home directory; a client configuration outside those paths is not visible to it",
                "the *contents* of a stored auth entry are never read, so the count of entries is all this record states about them",
            ],
        }

    def _ambient_config_paths(self) -> List[str]:
        paths: List[str] = []
        configured = self._environ.get(policy.CLIENT_CONFIG_ENV)
        if configured:
            paths.append(os.path.join(configured, policy.CLIENT_CONFIG_FILENAME))
        home = self._environ.get("HOME")
        if home:
            paths.append(os.path.join(home, ".docker", policy.CLIENT_CONFIG_FILENAME))
        return paths

    def _config_summary(self, path: str) -> Dict[str, Any]:
        """Summarise one client configuration file without reading any value."""
        summary: Dict[str, Any] = {
            "path": path,
            "present": os.path.isfile(path),
            "credential_keys_present": [],
            "auths_entries": 0,
            "creds_store_configured": False,
            "creds_helpers_configured": False,
            "readable": False,
        }
        if not summary["present"]:
            return summary
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return summary
        if not isinstance(data, dict):
            return summary
        summary["readable"] = True
        summary["credential_keys_present"] = sorted(
            key for key in policy.CREDENTIAL_CONFIG_KEYS if key in data
        )
        auths = data.get("auths")
        if isinstance(auths, dict):
            summary["auths_entries"] = len(auths)
        summary["creds_store_configured"] = bool(data.get("credsStore"))
        helpers = data.get("credHelpers")
        summary["creds_helpers_configured"] = isinstance(helpers, dict) and bool(helpers)
        return summary

    # -- the setup ----------------------------------------------------------

    def build(
        self, base_reference: str = policy.BASE_REFERENCE
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """Prepare the runner image once, and return ``(record, reason)``.

        ``reason`` is not None only when the setup could not be carried out at
        all: a base that is not the pinned base, an unreachable daemon, a
        refused Dockerfile, a failed build or output that cannot be read. A
        refusal of *readiness* is not such a reason — the artifact exists, and
        the record says why it is not ready.
        """
        record = self._base_record(base_reference)
        try:
            _parts, reason = self.base_reference_parts(base_reference)
            if reason is not None:
                return self._refuse(record, reason), reason

            before = self.snapshot()
            record["non_mutation"]["before"] = before

            resolved, reason = self.resolve_base(base_reference)
            if reason is not None:
                return self._refuse(record, reason), reason
            record["base"] = resolved

            dockerfile_reason = self.dockerfile_reason()
            record["dockerfile"] = {
                "path": policy.DOCKERFILE_PATH,
                "builds_from_pinned_base": dockerfile_reason is None,
                "reason": dockerfile_reason,
            }
            if dockerfile_reason is not None:
                return self._refuse(record, dockerfile_reason), dockerfile_reason

            if resolved["onbuild_reason"] is not None:
                return self._refuse(record, resolved["onbuild_reason"]), resolved[
                    "onbuild_reason"
                ]

            proc, reason = self._run(
                ["build", "-f", policy.DOCKERFILE_PATH, "-t", policy.RUNNER_IMAGE, "."],
                self.build_timeout,
            )
            if reason is not None:
                return self._refuse(record, reason), reason
            output = (proc.stdout or "") + (proc.stderr or "")
            classified = classify_build_output(output)
            succeeded = proc.returncode == 0
            # The builder's own lines are evidence for cache and acquisition
            # answers, so they are kept when the build succeeded. A *failed*
            # build keeps only the bounded classification: a failure is
            # reported as a bounded reason, and a log is not a reason.
            record["build"] = {
                "dispatched": True,
                "reference": policy.RUNNER_IMAGE,
                "dockerfile": policy.DOCKERFILE_PATH,
                "exit_status": proc.returncode,
                "succeeded": succeeded,
                "cache_state": classified["cache_state"],
                "events": classified["events"] if succeeded else [],
                "log_lines": classified["log_lines"],
                "cached_steps": classified["cached_steps"] if succeeded else [],
                "acquired_steps": classified["acquired_steps"] if succeeded else [],
                "context_steps": classified["context_steps"] if succeeded else [],
                "resolution_steps": classified["resolution_steps"] if succeeded else [],
                "hosts_named_in_output": hosts_named_in_output(output) if succeeded else [],
            }
            if not succeeded:
                return self._refuse(record, REASON_BUILD_FAILED), REASON_BUILD_FAILED

            image, reason = self.runner_identity()
            if reason is not None:
                return self._refuse(record, reason), reason
            record["image"] = image

            after = self.snapshot()
            record["non_mutation"]["after"] = after
            record["non_mutation"]["unchanged"] = _compare_snapshots(before, after)
            record["non_mutation"]["all_unchanged"] = all(
                record["non_mutation"]["unchanged"].values()
            )
            record["credentials"] = self.credential_evidence()
            record["outcome"] = OUTCOME_BUILT
            record["readiness"] = self._readiness(
                resolved, image, dockerfile_reason, built=True
            )
            return self._finalize_contact(record), None
        finally:
            self.close()

    def inspect(
        self, base_reference: str = policy.BASE_REFERENCE
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """Observe without building: resolve the base and read the local image.

        The readiness reported here speaks about the image that is *already*
        present, not about a build this run performed, and the record says so.
        """
        record = self._base_record(base_reference)
        try:
            _parts, reason = self.base_reference_parts(base_reference)
            if reason is not None:
                return self._refuse(record, reason), reason
            resolved, reason = self.resolve_base(base_reference)
            if reason is not None:
                return self._refuse(record, reason), reason
            record["base"] = resolved
            dockerfile_reason = self.dockerfile_reason()
            record["dockerfile"] = {
                "path": policy.DOCKERFILE_PATH,
                "builds_from_pinned_base": dockerfile_reason is None,
                "reason": dockerfile_reason,
            }
            image, image_reason = self.runner_identity()
            record["image"] = image
            record["image_reason"] = image_reason
            record["build"] = {
                "dispatched": False,
                "note": "readiness speaks about the local image, not a build performed here",
            }
            record["non_mutation"]["notes"].append(
                "no before/after observation was taken: this mode dispatched no build"
            )
            record["credentials"] = self.credential_evidence()
            record["outcome"] = OUTCOME_INSPECTED
            record["readiness"] = self._readiness(
                resolved,
                image,
                dockerfile_reason,
                built=image_reason is None and dockerfile_reason is None,
            )
            return self._finalize_contact(record), None
        finally:
            self.close()

    # -- record assembly ----------------------------------------------------

    def _readiness(
        self,
        resolved: Dict[str, Any],
        image: Optional[Dict[str, Any]],
        dockerfile_reason: Optional[str],
        *,
        built: bool,
    ) -> Dict[str, Any]:
        state, reason = policy.readiness(
            base_identity_verified=bool(resolved.get("index_digest")),
            platform_manifest_resolved=bool(resolved.get("platform_manifest_digest")),
            config_resolved=bool(resolved.get("config_digest")),
            base_onbuild_reason=resolved["onbuild_reason"],
            dockerfile_verified=dockerfile_reason is None,
            build_succeeded=built,
            runner_recorded=image is not None,
            runner_onbuild_reason=(image or {}).get(
                "onbuild_reason", policy.REASON_ONBUILD_UNVERIFIABLE
            ),
            lineage_mismatch_reason=policy.lineage_reason(
                resolved.get("layer_diff_ids"), (image or {}).get("layer_diff_ids")
            ),
            runner_digest_matches_pin=bool((image or {}).get("matches_pinned")),
        )
        return {"state": state, "reason": reason}

    def _base_record(self, base_reference: Any) -> Dict[str, Any]:
        return {
            "record": RECORD_NAME,
            "schema_version": RECORD_SCHEMA_VERSION,
            "policy_version": policy.POLICY_VERSION,
            # The networked claim starts false and is set true only by
            # _finalize_contact, from what the run actually dispatched.
            "claim": {**policy.CLAIMS, policy.NETWORKED_CLAIM: False},
            "outcome": OUTCOME_REFUSED,
            "refusal": None,
            "requested_base": base_reference if isinstance(base_reference, str) else None,
            "pinned_base": policy.BASE_REFERENCE,
            "pinned_runner_image": policy.RUNNER_IMAGE,
            "pinned_runner_digest": policy.RUNNER_IMAGE_DIGEST,
            "base": None,
            "dockerfile": None,
            "build": {"dispatched": False},
            "image": None,
            "contact": {
                # Filled in from what actually happened, never seeded from the
                # full list of classes a complete run could reach.
                "observed_classes": [],
                "declared_endpoint_classes": list(policy.DECLARED_ENDPOINT_CLASSES),
                "unobserved": list(policy.CONTACT_UNOBSERVED),
                "dispatched_operations": [],
                "hosts_named_in_build_output": [],
                "host_names_observed": False,
                "unexpected_hits": [],
            },
            "credentials": None,
            "non_mutation": {
                "before": None,
                "after": None,
                "unchanged": {},
                "all_unchanged": False,
                "notes": [_GROUP_NOTE, _CONTEXT_NOTE, _GIT_NOTE],
            },
            "readiness": {"state": policy.NOT_READY, "reason": None},
            "limitations": list(policy.LIMITATIONS),
        }

    def contacted_a_registry(self) -> bool:
        """Return True only if this run dispatched a registry-reaching operation.

        The setup's only registry-reaching operations are a resolution and a
        build. Nothing else it dispatches leaves the machine, so this is a
        statement about what was *attempted* — not about what any request was
        for, and not about which destinations a daemon reached.
        """
        return any(
            _operation(argv) in policy.NETWORK_OPERATIONS for argv in self.dispatched
        )

    def _finalize_contact(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Record what was dispatched, and the contact classes that followed."""
        build = record.get("build") or {}
        classes: List[str] = []
        if record.get("base") is not None:
            classes.extend(policy.CONTACT_RESOLUTION_CLASSES)
        if build.get("acquired_steps"):
            classes.append(policy.CONTACT_LAYER_ACQUISITION)
        elif build.get("cached_steps"):
            classes.append(policy.CONTACT_LAYER_CACHE_LOOKUP)
        hosts = build.get("hosts_named_in_output") or []
        record["contact"]["observed_classes"] = classes
        record["contact"]["dispatched_operations"] = sorted(
            {_operation(argv) for argv in self.dispatched}
        )
        record["contact"]["hosts_named_in_build_output"] = hosts
        record["contact"]["host_names_observed"] = bool(hosts)
        record["contact"]["unexpected_hits"] = unexpected_hits(record)
        # Set from what the run did, never assumed: a refused run that never
        # reached the registry must not describe itself as networked evidence.
        record["claim"][policy.NETWORKED_CLAIM] = self.contacted_a_registry()
        return record

    def _refuse(self, record: Dict[str, Any], reason: str) -> Dict[str, Any]:
        """Finish a refused record with the evidence that still exists."""
        record["outcome"] = OUTCOME_REFUSED
        record["refusal"] = reason
        record["credentials"] = self.credential_evidence()
        before = record["non_mutation"]["before"]
        after = record["non_mutation"]["after"]
        if before is not None and after is not None:
            record["non_mutation"]["unchanged"] = _compare_snapshots(before, after)
            record["non_mutation"]["all_unchanged"] = all(
                record["non_mutation"]["unchanged"].values()
            )
        elif before is not None:
            record["non_mutation"]["notes"].append(
                "the setup refused before it observed an after state"
            )
        else:
            record["non_mutation"]["notes"].append(
                "the setup refused before it observed anything, so no before or after state exists"
            )
        record["readiness"] = {"state": policy.NOT_READY, "reason": reason}
        return self._finalize_contact(record)


def unexpected_hits(record: Dict[str, Any]) -> List[str]:
    """Return host names an engine printed that are outside the allowed set.

    This is a name check on output the engine produced itself. It is not a
    destination check, and an empty list is not a claim that nothing was
    contacted — the record says separately whether any host name was read at all.
    """
    hits = set()
    for host in (record.get("build") or {}).get("hosts_named_in_output") or []:
        if host not in policy.ALLOWED_REGISTRY_HOSTS:
            hits.add(host)
    requested = (record.get("base") or {}).get("requested_canonical")
    if isinstance(requested, str) and "/" in requested:
        registry = requested.split("/", 1)[0]
        if registry not in policy.ALLOWED_REGISTRY_HOSTS:
            hits.add(registry)
    return sorted(hits)


def _operation(argv: Sequence[str]) -> str:
    """Return a bounded name for one dispatched client operation."""
    if not argv:
        return "unknown"
    if len(argv) > 2 and argv[0] == "buildx" and argv[1] == "imagetools":
        return "buildx imagetools inspect"
    if argv[0] == "image":
        return "image inspect"
    if argv[0] == "buildx":
        return "buildx " + str(argv[1])
    return str(argv[0])


def _platform_name(server: Dict[str, Any]) -> Optional[str]:
    """Return the daemon's own platform name, bounded, or ``None``."""
    platform = server.get("Platform")
    if not isinstance(platform, dict):
        return None
    name = platform.get("Name")
    return name[:MAX_EVENT_LINE_CHARS] if isinstance(name, str) else None


def _buildx_version(line: Optional[str]) -> Optional[str]:
    """Return the version token from a ``docker buildx version`` line."""
    if not line:
        return None
    for token in line.split():
        if token.startswith("v") and token[1:2].isdigit():
            return token
    return None


def _read_dockerfile(root: str) -> Optional[str]:
    try:
        with open(
            os.path.join(root, policy.DOCKERFILE_PATH), "r", encoding="utf-8"
        ) as handle:
            return handle.read()
    except OSError:
        return None


def _compare_snapshots(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, bool]:
    """Return, per observed group, whether the group is identical."""
    compared = (
        "git_head",
        "git_status",
        "git_diff_stat",
        "candidate_roots",
        "containers",
        "images_excluding_runner",
    )
    return {key: before.get(key) == after.get(key) for key in compared}


def write_record(
    record: Dict[str, Any], evidence_base: str
) -> Tuple[Optional[str], Optional[str]]:
    """Write the record into an evidence base; return ``(path, reason)``.

    This is the only write in this module, and it happens only beneath a
    directory the caller supplied that already exists. The filename is derived
    from the record's own bytes, so a run that observed something different
    writes a second file instead of replacing the first.
    """
    if not os.path.isdir(evidence_base):
        return None, REASON_EVIDENCE_BASE
    path = os.path.join(evidence_base, record_filename(record))
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(dumps_pretty(record) + "\n")
    except OSError:
        return None, REASON_EVIDENCE_WRITE
    return path, None


__all__ = [
    "RECORD_NAME",
    "RECORD_SCHEMA_VERSION",
    "RECORD_PREFIX",
    "RECORD_SUFFIX",
    "SETUP_TIMEOUT_SECONDS",
    "BUILD_TIMEOUT_SECONDS",
    "MAX_EVENT_LINES",
    "OUTCOME_REFUSED",
    "OUTCOME_BUILT",
    "OUTCOME_INSPECTED",
    "REASON_DOCKER_ABSENT",
    "REASON_DOCKER_FAILED",
    "REASON_DOCKER_TIMEOUT",
    "REASON_DOCKER_OUTPUT",
    "REASON_DAEMON_UNREACHABLE",
    "REASON_CLIENT_ENV",
    "REASON_DOCKERFILE_UNREADABLE",
    "REASON_BUILD_FAILED",
    "REASON_IMAGE_ABSENT",
    "REASON_EVIDENCE_BASE",
    "REASON_EVIDENCE_WRITE",
    "repository_root",
    "dumps",
    "dumps_pretty",
    "record_filename",
    "classify_build_output",
    "hosts_named_in_output",
    "unexpected_hits",
    "RunnerImageSetup",
    "write_record",
]
