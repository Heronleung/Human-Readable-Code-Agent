"""Isolated container-runner adapter (P4.3).

The single, backend-owned place that *executes* an app package. It runs the
trusted handler inside a controlled Linux container (Docker), never on the
host Python. Every control here is code-owned and reviewed — none is derived
from package or model input.

Hard controls enforced by the generated ``docker run`` command (and asserted by
tests):

* **fixed image** — :data:`RUNNER_IMAGE`, built once and reviewed;
* **no network** — ``--network none``;
* **non-root** — ``--user 65534:65534``;
* **least privilege** — ``--security-opt no-new-privileges`` and
  ``--cap-drop ALL``;
* **read-only rootfs** — ``--read-only`` plus a bounded ``--tmpfs /tmp``;
* **bounded resources** — ``--memory``/``--memory-swap``/``--cpus``/
  ``--pids-limit``/``--stop-timeout``;
* **process-tree cleanup** — ``--init`` (PID 1 reaping), ``--rm``, and an
  explicit ``docker kill``/``docker rm -f`` on timeout;
* **staged input only** — one fresh input directory is bind-mounted read-only;
* **validated output collection** — one fresh output directory is bind-mounted
  and only the expected, size-bounded ``output.json`` is read back;
* **no host secrets/mounts** — the home directory, credentials, the full
  repository, and the Docker socket are never mounted.

Fail-closed: :meth:`preflight` reports unavailable when the ``docker`` client or
daemon is absent/unreachable (and blocked when the reviewed image is missing),
and :meth:`run` never falls back to host Python.

The candidate-validation path
----------------------------

:meth:`ContainerRunner.run_candidate` is a second, separate path. It exists
because the package path can only call three baked-in business-rule handlers and
has no way to look at a mount at all. The candidate path adds exactly one
more bind mount — the candidate root's ``files`` directory, read-only, at the
fixed ``/candidate`` — and one literal product-owned entrypoint,
:data:`CANDIDATE_ENTRYPOINT`, which compiles the declared files and never
imports or runs them.

Two things about it are deliberately stricter than the package path:

* it refuses unless the local image's immutable **ID** equals
  :data:`RUNNER_IMAGE_DIGEST`. A tag can be moved to different content under the
  same name, so a run bound only to a tag could report evidence about an image
  nobody reviewed;
* it mounts the candidate root's ``files`` directory rather than the root
  itself. The root is created ``0700`` and the container runs as 65534, so
  mounting the root would mean widening its mode — a mutation of the thing being
  validated. ``files`` is already traversable, so the candidate is not touched
  at all, and the container never sees the manifest or the review envelope
  beside it.

The timeout lifecycle
---------------------

``subprocess.run(timeout=...)`` raises :class:`subprocess.TimeoutExpired`, which
is a :class:`subprocess.SubprocessError` and **not** a :class:`TimeoutError`.
Every bounded client call below therefore catches both names. Catching only
``TimeoutError`` meant a real timeout escaped each of these handlers, and the
consequences were not merely a missing state token: :meth:`ContainerRunner._kill`
and the ``finally`` cleanup were skipped with it, so the container was never
killed or removed, the staged input directory — which holds the staged payload —
was left on disk, and the caller saw an exception instead of a bounded outcome.

A real timeout now runs the whole lifecycle exactly once: the bounded timeout
token, one kill/removal attempt, one staged-root cleanup. The token reports
whether that lifecycle *completed*: ``timeout`` when the kill, the removal and
the cleanup all ran, and ``runner_failed`` when any of them did not, so a
half-finished cleanup is never reported as a clean timeout. A client that
answers non-zero without raising counts as answered, because a container that
has already exited is a benign outcome and this bounded repair cannot tell it
apart from a refusal.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import app_package

# Code-owned runtime profile. Never user-configurable; never from a package.
RUNNER_IMAGE = "hrca-runner:v1"
RUNNER_USER = "65534:65534"
RUNNER_NETWORK = "none"
RUNNER_MEMORY = "64m"
RUNNER_CPUS = "0.5"
RUNNER_PIDS_LIMIT = "64"
RUNNER_TMPFS = "/tmp:rw,size=16m,mode=1777"
RUNNER_STOP_TIMEOUT = "2"
RUNNER_TIMEOUT_SECONDS = 10.0
RUNNER_MAX_OUTPUT_BYTES = 64 * 1024

_INPUT_FILENAME = "input.json"
_OUTPUT_FILENAME = "output.json"
_INPUT_DIR = "/in"
_OUTPUT_DIR = "/out"
_ENTRYPOINT = ["python", "/app/runner_main.py", "/in/input.json", "/out/output.json"]

# -- the candidate-validation path (P5.5a-r2) ------------------------------
#
# A second, separate path. It exists so that a P5.4 candidate can be exercised
# *without* the product-package handler registry, which can only ever call the
# three baked-in business-rule handlers and has no way to look at a mount.
#
# The image reference below is pinned by the manifest digest the image was
# actually built from, not by the floating ``hrca-runner:v1`` tag. A tag can be
# moved to different content under the same name, which would let a run report
# evidence about an image nobody reviewed; ``run_candidate`` reads the local
# image's immutable ID and refuses unless it equals this value exactly.
RUNNER_IMAGE_DIGEST = "sha256:0ae0f7f5c31a4378a03f35c158d7c07989bcd3f1fcc64148e914ef363cbf2c48"

# The one fixed in-image location a candidate root is mounted at, read-only.
_CANDIDATE_DIR = "/candidate"
CANDIDATE_ENTRYPOINT_ID = "runner_syntax"
CANDIDATE_ENTRYPOINT = [
    "python",
    "/app/runner_syntax.py",
    "/in/input.json",
    "/out/output.json",
]
CANDIDATE_INPUT_KEY = "files"
_MAX_DECLARED_FILES = 64

# The P5.4 candidate-root shape, restated here as literals rather than imported:
# the runner is a low-level adapter and must not depend on the candidate
# contract. A root that does not have this exact shape is refused before any
# host path becomes a mount.
_MANIFEST_NAME = "candidate.json"
_FILES_DIR = "files"
_CANDIDATE_ROOT_PREFIX = "candidate-"

# What is mounted is the candidate root's ``files`` directory — the exact
# directory the P5.4 manifest enumerates — not the root itself.
#
# The root is created 0700 by ``tempfile.mkdtemp`` and the container runs as
# 65534, so mounting the root would require widening its mode: a mutation of the
# candidate. ``files`` and everything below it are already traversable, so
# mounting it needs no change to the candidate at all — not one byte, not one
# bit — and it exposes strictly less: the container sees the candidate's content
# and never the manifest or the review envelope beside it. The mount source is
# still *restricted to* a verified candidate root and is checked for
# containment before it becomes a mount.
CANDIDATE_CONTENT_DIR = "files"

# Bounded refusal tokens for the candidate path. They are distinct from the
# package path's result tokens because they are refusals to dispatch at all.
REASON_DIGEST_ABSENT = "image_digest_absent"
REASON_DIGEST_MISMATCH = "image_digest_mismatch"
REASON_CANDIDATE_ROOT_INVALID = "candidate_root_invalid"
REASON_DECLARED_FILES_INVALID = "declared_files_invalid"

# Bounded preflight reasons.
PREFLIGHT_AVAILABLE = "available"
PREFLIGHT_RUNTIME_UNAVAILABLE = "runtime_unavailable"
PREFLIGHT_RUNTIME_BLOCKED = "runtime_blocked"


def _default_which(name: str) -> Optional[str]:
    return shutil.which(name)


class ContainerRunner:
    """The Docker-based isolated runner.

    ``spawn`` and ``which`` are injectable for deterministic tests; the defaults
    are the real ``subprocess.run`` and ``shutil.which``.
    """

    def __init__(
        self,
        *,
        image: str = RUNNER_IMAGE,
        timeout: float = RUNNER_TIMEOUT_SECONDS,
        max_output_bytes: int = RUNNER_MAX_OUTPUT_BYTES,
        spawn: Callable[..., Any] = subprocess.run,
        which: Callable[[str], Optional[str]] = _default_which,
    ) -> None:
        self._image = image
        self._timeout = timeout
        self._max_output_bytes = max_output_bytes
        self._spawn = spawn
        self._which = which

    # -- preflight -------------------------------------------------------

    def preflight(self) -> Dict[str, Any]:
        """Return a fail-closed availability/isolation preflight result.

        ``available`` is True only when the ``docker`` client is present, the
        daemon is reachable, and the reviewed image exists. Any failure returns
        ``available: False`` with a bounded ``reason`` and a per-check summary.
        """
        docker = self._which("docker")
        checks = {"docker": docker is not None, "daemon": False, "image": False}
        if docker is None:
            return {
                "available": False,
                "reason": PREFLIGHT_RUNTIME_UNAVAILABLE,
                "checks": checks,
            }
        try:
            info = self._spawn([docker, "info"], capture_output=True, timeout=5.0)
            checks["daemon"] = getattr(info, "returncode", None) == 0
        except (OSError, TimeoutError, subprocess.TimeoutExpired):
            # A hanging client is an unreachable daemon, not an exception: a
            # real ``subprocess.run`` timeout raises ``TimeoutExpired``, which
            # neither ``OSError`` nor ``TimeoutError`` catches.
            checks["daemon"] = False
        if not checks["daemon"]:
            return {
                "available": False,
                "reason": PREFLIGHT_RUNTIME_UNAVAILABLE,
                "checks": checks,
            }
        try:
            inspect = self._spawn(
                [docker, "image", "inspect", self._image],
                capture_output=True,
                timeout=5.0,
            )
            checks["image"] = getattr(inspect, "returncode", None) == 0
        except (OSError, TimeoutError, subprocess.TimeoutExpired):
            checks["image"] = False
        if not checks["image"]:
            return {
                "available": False,
                "reason": PREFLIGHT_RUNTIME_BLOCKED,
                "checks": checks,
            }
        return {"available": True, "reason": None, "checks": checks}

    # -- command construction --------------------------------------------

    def build_command(
        self,
        *,
        handler: str,
        input_payload: Dict[str, Any],
        container_name: str,
        input_dir: str,
        output_dir: str,
    ) -> List[str]:
        """Return the exact hardened ``docker run`` argv for one package run.

        This is the reviewed control surface: every flag is fixed here, and no
        host home, credential, repository or socket path appears in it.
        """
        docker = self._which("docker") or "docker"
        return [
            docker,
            "run",
            "--name", container_name,
            "--rm",
            "--init",
            "--network", RUNNER_NETWORK,
            "--user", RUNNER_USER,
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            "--read-only",
            "--memory", RUNNER_MEMORY,
            "--memory-swap", RUNNER_MEMORY,
            "--cpus", RUNNER_CPUS,
            "--pids-limit", RUNNER_PIDS_LIMIT,
            "--tmpfs", RUNNER_TMPFS,
            "--stop-timeout", RUNNER_STOP_TIMEOUT,
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "HOME=/tmp",
            "--mount", f"type=bind,src={input_dir},dst={_INPUT_DIR},readonly",
            "--mount", f"type=bind,src={output_dir},dst={_OUTPUT_DIR}",
            self._image,
            *_ENTRYPOINT,
        ]

    # -- execution -------------------------------------------------------

    def run(
        self,
        *,
        handler: str,
        input_payload: Dict[str, Any],
        parameters: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Stage the input, run once inside the container, collect the output.

        ``parameters`` is an optional, already-validated mapping of code-owned
        rule parameters (resolved from a rule delta) passed to the handler.
        Returns ``(result, error)`` where exactly one is ``None``. ``result`` is
        the raw ``result`` mapping from the runner output (validated against the
        package result schema by the broker); ``error`` is a normalized state
        token (timeout / output_invalid / input_invalid / runner_failed).

        ``timeout`` means the whole timeout lifecycle ran: the kill, the removal
        and the staged-root cleanup. If any of those failed, ``runner_failed`` is
        returned instead, because reporting ``timeout`` would claim a clean stop
        that did not happen.
        """
        if not isinstance(handler, str) or not handler:
            return None, app_package.STATE_OUTPUT_INVALID

        input_dir = tempfile.mkdtemp(prefix="hrca-in-")
        output_dir = tempfile.mkdtemp(prefix="hrca-out-")
        container_name = "hrca-run-" + uuid.uuid4().hex
        # ``None`` until the timeout path cleans up and observes the result, so
        # the ``finally`` below can tell whether the staged roots are still its
        # to remove. Every path cleans up exactly once.
        cleaned: Optional[bool] = None
        try:
            self._stage_input(input_dir, handler, input_payload, parameters)
            # The staged input directory must be traversable by the container's
            # non-root user (``tempfile.mkdtemp`` creates a 0700 directory, which
            # ``nobody`` could not read). The staged input file is world-readable;
            # widening only the fresh, dedicated input directory never exposes the
            # host.
            os.chmod(input_dir, 0o755)
            # The output directory must be writable by the non-root container
            # user; it is a fresh, dedicated directory only.
            os.chmod(output_dir, 0o777)
            argv = self.build_command(
                handler=handler,
                input_payload=input_payload,
                container_name=container_name,
                input_dir=input_dir,
                output_dir=output_dir,
            )
            try:
                proc = self._spawn(argv, capture_output=True, timeout=self._timeout)
            except (TimeoutError, subprocess.TimeoutExpired):
                killed = self._kill(container_name)
                cleaned = self._cleanup(input_dir, output_dir)
                if killed and cleaned:
                    return None, app_package.STATE_TIMEOUT
                # The timeout fired, but the lifecycle did not finish. Saying
                # ``timeout`` here would report a clean stop that did not
                # happen, so the louder existing token is returned instead.
                return None, app_package.STATE_RUNNER_FAILED
            except OSError:
                return None, app_package.STATE_RUNNER_FAILED

            if getattr(proc, "returncode", 1) != 0:
                return None, app_package.STATE_RUNNER_FAILED

            output = self._read_output(output_dir)
            if output is None:
                return None, app_package.STATE_OUTPUT_INVALID
            if "error" in output and "result" not in output:
                return None, app_package.STATE_INPUT_INVALID
            result = output.get("result")
            if not isinstance(result, dict):
                return None, app_package.STATE_OUTPUT_INVALID
            return result, None
        finally:
            # The timeout path has already cleaned up and observed the result;
            # every other path reaches this safety net exactly once.
            if cleaned is None:
                self._cleanup(input_dir, output_dir)

    # -- the candidate-validation path -----------------------------------

    def image_digest(self) -> Optional[str]:
        """Return the local image's immutable ID, or ``None`` if unreadable.

        The ID is content-addressed, so it is the same value however the tag
        moves. ``None`` covers a missing client, a hung client, a non-zero exit
        and an image that is not in the local store — all of which must refuse
        rather than dispatch.
        """
        docker = self._which("docker") or "docker"
        try:
            proc = self._spawn(
                [docker, "image", "inspect", self._image, "--format", "{{.Id}}"],
                capture_output=True,
                timeout=5.0,
            )
        except (OSError, TimeoutError, subprocess.TimeoutExpired):
            return None
        if getattr(proc, "returncode", 1) != 0:
            return None
        raw = getattr(proc, "stdout", b"") or b""
        if not isinstance(raw, (bytes, bytearray)):
            raw = str(raw).encode("utf-8", "replace")
        text = bytes(raw).decode("utf-8", "replace").strip()
        return text or None

    def build_candidate_command(
        self,
        *,
        container_name: str,
        input_dir: str,
        output_dir: str,
        candidate_dir: str,
    ) -> List[str]:
        """Return the hardened ``docker run`` argv for one candidate syntax check.

        Exactly three mounts, in a fixed order: the staged input (read-only),
        the staged output, and the candidate root (read-only). Nothing else is
        mounted, and the entrypoint is a module constant rather than a handler
        name — no plan, candidate or prose value reaches any element here.
        """
        docker = self._which("docker") or "docker"
        return [
            docker,
            "run",
            "--name", container_name,
            "--rm",
            "--init",
            "--network", RUNNER_NETWORK,
            "--user", RUNNER_USER,
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            "--read-only",
            "--memory", RUNNER_MEMORY,
            "--memory-swap", RUNNER_MEMORY,
            "--cpus", RUNNER_CPUS,
            "--pids-limit", RUNNER_PIDS_LIMIT,
            "--tmpfs", RUNNER_TMPFS,
            "--stop-timeout", RUNNER_STOP_TIMEOUT,
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "HOME=/tmp",
            "--mount", f"type=bind,src={input_dir},dst={_INPUT_DIR},readonly",
            "--mount", f"type=bind,src={output_dir},dst={_OUTPUT_DIR}",
            "--mount", f"type=bind,src={candidate_dir},dst={_CANDIDATE_DIR},readonly",
            self._image,
            *CANDIDATE_ENTRYPOINT,
        ]

    @staticmethod
    def _candidate_mount_source(candidate_dir: Any) -> Optional[str]:
        """Return the host directory to mount, or ``None`` to refuse.

        The structural check is deliberately narrow: an absolute directory that
        is not a link, named as a P5.4 candidate root, holding the manifest and
        the ``files`` directory and nothing else at the top level, with that
        ``files`` directory resolving inside it. The full identity check is the
        caller's, against the manifest and the review envelope; this is the last
        line before a host path becomes a mount.
        """
        if not isinstance(candidate_dir, str) or not candidate_dir:
            return None
        if not os.path.isabs(candidate_dir) or os.path.islink(candidate_dir):
            return None
        if os.path.realpath(candidate_dir) != candidate_dir:
            return None
        if not os.path.isdir(candidate_dir):
            return None
        if not os.path.basename(candidate_dir).startswith(_CANDIDATE_ROOT_PREFIX):
            return None
        try:
            entries = sorted(os.listdir(candidate_dir))
        except OSError:
            return None
        if entries != sorted([_MANIFEST_NAME, _FILES_DIR]):
            return None

        content = os.path.join(candidate_dir, _FILES_DIR)
        real = os.path.realpath(content)
        if os.path.islink(content) or not os.path.isdir(content):
            return None
        # Containment, checked on the resolved paths: the mount source must be
        # inside the root that was verified, never a link out of it.
        if not real.startswith(candidate_dir.rstrip(os.sep) + os.sep):
            return None
        return content

    @staticmethod
    def _declared_files_usable(declared_files: Any) -> bool:
        """Return whether every declared path is a safe relative source path."""
        if not isinstance(declared_files, list) or not declared_files:
            return False
        if len(declared_files) > _MAX_DECLARED_FILES:
            return False
        for item in declared_files:
            if not isinstance(item, str) or not item or len(item) > 512:
                return False
            if item.startswith("/") or "\\" in item or ":" in item:
                return False
            if any(ord(char) < 32 or ord(char) == 127 for char in item):
                return False
            if any(part in ("", ".", "..") for part in item.split("/")):
                return False
        return True

    def run_candidate(
        self,
        *,
        candidate_dir: Any,
        declared_files: Any,
        expected_digest: Any,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Syntax-check the declared candidate files inside the container.

        Returns ``(result, error)`` with exactly one ``None``, the same shape as
        :meth:`run`. ``error`` is either an existing run token (timeout /
        runner_failed / output_invalid / input_invalid) or one of the bounded
        candidate-path refusals, which mean nothing was dispatched.

        The candidate root is only ever read, and it is never passed to
        :meth:`_cleanup`: this method stages its own two directories and cleans
        up exactly those.
        """
        mount_source = self._candidate_mount_source(candidate_dir)
        if mount_source is None:
            return None, REASON_CANDIDATE_ROOT_INVALID
        if not self._declared_files_usable(declared_files):
            return None, REASON_DECLARED_FILES_INVALID
        if not isinstance(expected_digest, str) or not expected_digest:
            return None, REASON_DIGEST_ABSENT
        actual = self.image_digest()
        if not actual:
            return None, REASON_DIGEST_ABSENT
        if actual != expected_digest:
            return None, REASON_DIGEST_MISMATCH

        input_dir = tempfile.mkdtemp(prefix="hrca-in-")
        output_dir = tempfile.mkdtemp(prefix="hrca-out-")
        container_name = "hrca-run-" + uuid.uuid4().hex
        cleaned: Optional[bool] = None
        try:
            self._stage_input(
                input_dir,
                CANDIDATE_ENTRYPOINT_ID,
                {CANDIDATE_INPUT_KEY: sorted(set(declared_files))},
            )
            # Only the two directories this call made, and only enough for the
            # non-root container user to traverse them. The candidate is not
            # touched at all: the mount source is already traversable.
            os.chmod(input_dir, 0o755)
            os.chmod(output_dir, 0o777)
            argv = self.build_candidate_command(
                container_name=container_name,
                input_dir=input_dir,
                output_dir=output_dir,
                candidate_dir=mount_source,
            )
            try:
                proc = self._spawn(argv, capture_output=True, timeout=self._timeout)
            except (TimeoutError, subprocess.TimeoutExpired):
                killed = self._kill(container_name)
                cleaned = self._cleanup(input_dir, output_dir)
                if killed and cleaned:
                    return None, app_package.STATE_TIMEOUT
                return None, app_package.STATE_RUNNER_FAILED
            except OSError:
                return None, app_package.STATE_RUNNER_FAILED

            if getattr(proc, "returncode", 1) != 0:
                return None, app_package.STATE_RUNNER_FAILED

            output = self._read_output(output_dir)
            if output is None:
                return None, app_package.STATE_OUTPUT_INVALID
            if "error" in output and "result" not in output:
                return None, app_package.STATE_INPUT_INVALID
            result = output.get("result")
            if not isinstance(result, dict):
                return None, app_package.STATE_OUTPUT_INVALID
            return result, None
        finally:
            if cleaned is None:
                self._cleanup(input_dir, output_dir)

    # -- helpers ---------------------------------------------------------

    def _stage_input(
        self,
        input_dir: str,
        handler: str,
        input_payload: Dict[str, Any],
        parameters: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload = {"handler": handler, "input": input_payload}
        if parameters is not None:
            payload["parameters"] = parameters
        path = os.path.join(input_dir, _INPUT_FILENAME)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=True, separators=(",", ":"))

    def _read_output(self, output_dir: str) -> Optional[Dict[str, Any]]:
        path = os.path.join(output_dir, _OUTPUT_FILENAME)
        try:
            if os.path.getsize(path) > self._max_output_bytes:
                return None
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read(self._max_output_bytes + 1)
        except OSError:
            return None
        if len(raw.encode("utf-8")) > self._max_output_bytes:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def _kill(self, container_name: str) -> bool:
        """Attempt the kill and the removal; return whether both were answered.

        A client that raises — including a real ``subprocess.TimeoutExpired``,
        which ``TimeoutError`` alone does not catch — is a failure, and the
        caller must not report a clean timeout for it.
        """
        docker = self._which("docker") or "docker"
        answered = True
        try:
            self._spawn([docker, "kill", container_name], capture_output=True, timeout=5.0)
        except (OSError, TimeoutError, subprocess.TimeoutExpired):
            answered = False
        try:
            self._spawn([docker, "rm", "-f", container_name], capture_output=True, timeout=5.0)
        except (OSError, TimeoutError, subprocess.TimeoutExpired):
            answered = False
        return answered

    def _cleanup(self, input_dir: str, output_dir: str) -> bool:
        """Remove the two staged roots; return whether both are gone.

        Only the two directories this call created are ever passed in — no
        caller-supplied path reaches here — so a failure leaves an unrelated
        root entirely alone. A root that is already absent counts as removed,
        which is what makes a repeated call harmless.
        """
        removed = True
        for path in (input_dir, output_dir):
            try:
                if os.path.exists(path):
                    shutil.rmtree(path)
            except OSError:
                removed = False
        return removed


__all__ = [
    "RUNNER_IMAGE",
    "RUNNER_IMAGE_DIGEST",
    "RUNNER_USER",
    "RUNNER_NETWORK",
    "RUNNER_MEMORY",
    "RUNNER_CPUS",
    "RUNNER_PIDS_LIMIT",
    "RUNNER_TIMEOUT_SECONDS",
    "RUNNER_MAX_OUTPUT_BYTES",
    "CANDIDATE_ENTRYPOINT",
    "CANDIDATE_ENTRYPOINT_ID",
    "CANDIDATE_INPUT_KEY",

    "REASON_DIGEST_ABSENT",
    "REASON_DIGEST_MISMATCH",
    "REASON_CANDIDATE_ROOT_INVALID",
    "REASON_DECLARED_FILES_INVALID",
    "PREFLIGHT_AVAILABLE",
    "PREFLIGHT_RUNTIME_UNAVAILABLE",
    "PREFLIGHT_RUNTIME_BLOCKED",
    "ContainerRunner",
]
