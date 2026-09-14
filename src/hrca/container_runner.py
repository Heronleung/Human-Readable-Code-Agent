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
        except (OSError, TimeoutError):
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
        except (OSError, TimeoutError):
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
        """
        if not isinstance(handler, str) or not handler:
            return None, app_package.STATE_OUTPUT_INVALID

        input_dir = tempfile.mkdtemp(prefix="hrca-in-")
        output_dir = tempfile.mkdtemp(prefix="hrca-out-")
        container_name = "hrca-run-" + uuid.uuid4().hex
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
            except TimeoutError:
                self._kill(container_name)
                return None, app_package.STATE_TIMEOUT
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

    def _kill(self, container_name: str) -> None:
        docker = self._which("docker") or "docker"
        try:
            self._spawn([docker, "kill", container_name], capture_output=True, timeout=5.0)
        except (OSError, TimeoutError):
            pass
        try:
            self._spawn([docker, "rm", "-f", container_name], capture_output=True, timeout=5.0)
        except (OSError, TimeoutError):
            pass

    def _cleanup(self, input_dir: str, output_dir: str) -> None:
        for path in (input_dir, output_dir):
            try:
                shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass


__all__ = [
    "RUNNER_IMAGE",
    "RUNNER_USER",
    "RUNNER_NETWORK",
    "RUNNER_MEMORY",
    "RUNNER_CPUS",
    "RUNNER_PIDS_LIMIT",
    "RUNNER_TIMEOUT_SECONDS",
    "RUNNER_MAX_OUTPUT_BYTES",
    "PREFLIGHT_AVAILABLE",
    "PREFLIGHT_RUNTIME_UNAVAILABLE",
    "PREFLIGHT_RUNTIME_BLOCKED",
    "ContainerRunner",
]
