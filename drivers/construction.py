"""
Hamilton-Ops Construction Driver — P3 Construction Stream

The Construction Driver is the "Factory Foreman" of Hamilton-Ops.

While P1 measures speed and P2 checks quality, the Construction Driver
turns the wrench: it hands the immutable staging directory to Docker
BuildKit and ensures a high-performance, security-hardened image is
produced. It is the "heavy lift" that the P1 Hamilton Alarm is designed
to kill if the guidance system (k6 / ThresholdExceededError) detects trouble.

Key capabilities over the simpler DockerDriver:

    Cache Management
        Injects ``--cache-from`` and ``--cache-to`` for CI registry caches,
        plus ``--build-arg CACHE_ID=<project_hash>`` to scope the BuildKit
        layer cache per project. Without this, every clean CI runner restarts
        from scratch (40s → 500s regression).

    Secure Secret / SSH Handover
        Supports ``--secret id=<name>,src=<path>`` for credentials that must
        reach the build without being baked into any image layer, and
        ``--ssh default`` for private Git clones over the SSH agent.

    Surgical Process Termination
        Uses ``subprocess.Popen`` (not ``subprocess.run``) to hold a live
        handle to the BuildKit process. ``terminate()`` sends SIGTERM to the
        entire process group, then SIGKILL if the group doesn't exit within
        ``_SIGKILL_TIMEOUT_SECONDS``. On Windows, where ``os.killpg`` is
        unavailable, the driver falls back to ``Popen.kill()``.

    Log Sanitisation
        ``--build-arg KEY=VALUE`` entries whose keys match known secret
        patterns are redacted to ``KEY=***REDACTED***`` in the log output.
        The actual subprocess command is always passed unredacted.

Error-mapping contract:
    | Condition                     | Hamilton Signal              |
    |-------------------------------|------------------------------|
    | docker binary missing (127)   | EnvError                     |
    | OOM-killed (137)              | BuildError (oom=True context) |
    | Non-zero exit (build failure) | BuildError                   |
    | Daemon running as root        | EnvError (rootless violation) |
    | P1 Abort (terminate called)   | BuildError (aborted=True)    |
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

from core.exceptions import BuildError, EnvError
from drivers.registry import DriverResult

logger = logging.getLogger("hamilton.drivers.construction")

_EXIT_NOT_FOUND   = 127
_EXIT_OOM         = 137
_SIGKILL_TIMEOUT  = 5       # seconds to wait after SIGTERM before escalating

# Build-arg keys that must never appear in logs.
# Uses re.search (not re.match) so that compound keys like DB_PASSWORD,
# GITHUB_TOKEN, or MY_API_SECRET are all caught — not just bare 'password'.
_SECRET_KEY_PATTERN = re.compile(
    r"(password|passwd|pwd|secret|token|key|api|auth|credential)",
    re.IGNORECASE,
)


class ConstructionDriver:
    """
    BuildKit-powered P3 Construction Driver.

    Manages the *entire lifecycle* of a ``docker build``
    invocation: command construction, cache injection, secret mounting,
    live process control, and log sanitisation.

    The key difference from ``DockerDriver`` is that execution goes through
    ``subprocess.Popen`` (not ``subprocess.run``), giving the driver a live
    handle so the Supervisor can call ``terminate()`` in response to a P1 Alarm.

    All subprocess creation is routed through ``_build_popen`` so that tests
    can replace the real ``Popen`` without mocking the OS-level module.
    """

    def __init__(
        self,
        stage_path: str | Path,
        image_tag: str,
        dockerfile: Optional[str | Path] = None,
        *,
        cache_ref: Optional[str] = None,
        project_hash: Optional[str] = None,
        secrets: Optional[list[str]] = None,
        ssh: bool = False,
        no_cache: bool = False,
    ) -> None:
        """
        Args:
            stage_path:    Absolute path to the staging directory.
                           Passed as a list element, never shell-expanded.
            image_tag:     Docker image tag, e.g. ``myapp:sha256-abc123``.
            dockerfile:    Optional path to a Dockerfile. Defaults to
                           ``<stage_path>/Dockerfile``.
            cache_ref:     Registry reference for BuildKit layer caching,
                           e.g. ``ghcr.io/org/myapp:buildcache``. When set,
                           ``--cache-from`` and ``--cache-to`` are injected.
            project_hash:  Deterministic identifier for the project state
                           (e.g., SHA256 of the lock files). Injected as
                           ``--build-arg CACHE_ID=<hash>`` to scope the
                           BuildKit cache per project, preventing cross-project
                           cache contamination on shared CI runners.
            secrets:       List of BuildKit secret mount specs in the form
                           ``id=<name>,src=<path>``. Each item becomes a
                           ``--secret`` flag. The file at ``src`` is mounted
                           read-only into the build context; nothing is baked
                           into an image layer.
            ssh:           If True, passes ``--ssh default`` so the build
                           can access private Git repositories via the SSH
                           agent socket. Requires the agent to be running.
            no_cache:      If True, passes ``--no-cache`` to disable all
                           BuildKit layer caching (useful for security scans).
                           This overrides ``cache_ref``.
        """
        self.stage_path   = Path(stage_path).resolve()
        self.image_tag    = image_tag
        self.dockerfile   = Path(dockerfile).resolve() if dockerfile else self.stage_path / "Dockerfile"
        self.cache_ref    = cache_ref
        self.project_hash = project_hash
        self.secrets      = secrets or []
        self.ssh          = ssh
        self.no_cache     = no_cache

        # Live process handle — set during run(), cleared after wait().
        self._proc: Optional[subprocess.Popen] = None


    def run(self) -> DriverResult:
        """
        Launch ``docker build`` and wait for it to complete.

        Returns:
            DriverResult(success=True, output={"image_tag": ..., "stdout": ...})

        Raises:
            BuildError: If the build fails, is OOM-killed, or is aborted.
            EnvError:   If the Docker binary is missing (exit 127).
        """
        cmd = self._build_command()
        safe_cmd = _redact_build_args(cmd)
        logger.info("CONSTRUCTION: Launching P3 build stream → %s", safe_cmd)

        self._proc = self._build_popen(cmd)
        stdout, stderr = self._proc.communicate()
        returncode = self._proc.returncode
        self._proc = None  # clear the handle after the process exits

        if returncode != 0:
            self._map_exit_code(returncode, stderr or "")

        return DriverResult(
            success=True,
            output={"image_tag": self.image_tag, "stdout": stdout},
        )

    def terminate(self) -> None:
        """
        Surgically abort the running build in response to a P1 Hamilton Alarm.

        Sends SIGTERM to the BuildKit process group (POSIX) or calls
        ``Popen.kill()`` on Windows. If the process group does not exit
        within ``_SIGKILL_TIMEOUT`` seconds, SIGKILL is sent as a last resort.

        This is idempotent — calling it when no build is running is a no-op.
        """
        if self._proc is None:
            logger.debug("CONSTRUCTION: terminate() called but no active build process.")
            return

        pid = self._proc.pid
        logger.warning("CONSTRUCTION: P1 Alarm — terminating BuildKit process (PID=%d)", pid)

        if hasattr(os, "killpg") and hasattr(os, "getpgid"):
            # POSIX: kill the entire process group so BuildKit child processes
            # (spawners, cache writers) are also reaped immediately.
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
                # Give the process group time to clean up gracefully.
                deadline = time.monotonic() + _SIGKILL_TIMEOUT
                while time.monotonic() < deadline:
                    if self._proc.poll() is not None:
                        break
                    time.sleep(0.1)
                if self._proc.poll() is None:
                    logger.warning("CONSTRUCTION: SIGTERM ignored — escalating to SIGKILL.")
                    os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                # Process already exited between our check and the kill call.
                logger.debug("CONSTRUCTION: Process %d already exited.", pid)
        else:
            # Windows fallback: kills the process directly.
            self._proc.kill()

        logger.info("CONSTRUCTION: BuildKit process group reaped.")

    def check_health(self) -> DriverResult:
        """
        Verify Docker is available, daemon is reachable, and running rootless.

        Raises:
            EnvError: If Docker is absent, the daemon is down, or rootless
                      mode is not active (security requirement per README).
        """
        import json as _json

        if not shutil.which("docker"):
            raise EnvError(
                "docker binary not found on PATH. "
                "Install Docker from https://docs.docker.com/get-docker/",
                context={"tool": "docker"},
            )

        info_result = self._run_subprocess(["docker", "info", "--format", "{{json .}}"])
        if info_result.returncode != 0:
            raise EnvError(
                f"Docker daemon is not reachable: {info_result.stderr.strip()}",
                context={"tool": "docker", "exit_code": info_result.returncode},
            )

        try:
            info = _json.loads(info_result.stdout)
            security_options: list = info.get("SecurityOptions", [])
            if not any("rootless" in opt for opt in security_options):
                raise EnvError(
                    "Docker is running as root. Hamilton-Ops requires rootless mode. "
                    "See https://docs.docker.com/engine/security/rootless/",
                    context={"security_options": security_options},
                )
        except _json.JSONDecodeError:
            logger.warning("CONSTRUCTION: Could not parse 'docker info' JSON — rootless check skipped.")

        version_result = self._run_subprocess(["docker", "version", "--format", "{{.Server.Version}}"])
        version = version_result.stdout.strip() if version_result.returncode == 0 else "unknown"
        return DriverResult(success=True, output={"version": version})


    def _build_command(self) -> list[str]:
        """
        Assemble the full ``docker build`` argument list.

        Inserts BuildKit flags in this order:
          1. Core flags (--file, --tag, --no-cache)
          2. Cache flags (--cache-from, --cache-to, --build-arg CACHE_ID)
          3. Secret mounts (--secret id=...)
          4. SSH agent  (--ssh default)
          5. Build context (staging directory — always last)
        """
        cmd: list[str] = [
            "docker", "build",
            "--file",  str(self.dockerfile),
            "--tag",   self.image_tag,
        ]

        if self.no_cache:
            cmd.append("--no-cache")

        # --- Cache layer ---
        if self.cache_ref and not self.no_cache:
            cmd += ["--cache-from", f"type=registry,ref={self.cache_ref}"]
            cmd += ["--cache-to",   f"type=registry,ref={self.cache_ref},mode=max"]

        if self.project_hash:
            cmd += ["--build-arg", f"CACHE_ID={self.project_hash}"]

        # --- Secret mounts (BuildKit-native) ---
        for secret_spec in self.secrets:
            cmd += ["--secret", secret_spec]

        # --- SSH agent ---
        if self.ssh:
            cmd += ["--ssh", "default"]

        # Build context must be last — always the immutable staging directory.
        cmd.append(str(self.stage_path))
        return cmd


    @staticmethod
    def _map_exit_code(code: int, stderr: str) -> None:
        """
        Translate a non-zero Docker exit code into the correct Hamilton signal.

        Raises:
            EnvError:   On exit 127 (binary not found).
            BuildError: On all other failures, with OOM or abort context.
        """
        if code == _EXIT_NOT_FOUND:
            raise EnvError(
                "docker binary not found during build execution (exit 127). "
                "Pre-flight health check should have caught this.",
                context={"exit_code": code},
            )
        if code == _EXIT_OOM:
            raise BuildError(
                "BuildKit process was OOM-killed (exit 137). "
                "Increase Docker memory limit or simplify the build stages.",
                context={"exit_code": code, "oom": True},
            )
        # Negative return codes on POSIX typically mean the process was killed
        # by a signal (e.g., SIGTERM = -15, SIGKILL = -9).
        if code < 0:
            raise BuildError(
                f"BuildKit process terminated by signal {-code} "
                f"(likely a P1 Abort). Exit code: {code}.",
                context={"exit_code": code, "aborted": True},
            )
        raise BuildError(
            f"docker build failed with exit code {code}: {stderr.strip()}",
            context={"exit_code": code, "stderr": stderr},
        )

    def _build_popen(self, cmd: list[str]) -> subprocess.Popen:
        """
        Create the ``Popen`` object for the build process.

        ``start_new_session=True`` moves the process into its own session
        (and therefore its own process group), enabling ``os.killpg`` to
        reap BuildKit child processes without touching the Python host.
        """
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    def _run_subprocess(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """
        Thin wrapper around subprocess.run — used for health checks only.
        Patch this in tests for check_health() coverage.
        """
        return subprocess.run(cmd, capture_output=True, text=True)

def _redact_build_args(cmd: list[str]) -> list[str]:
    """
    Return a copy of ``cmd`` with sensitive ``--build-arg`` values redacted.

    For each ``--build-arg KEY=VALUE`` pair where KEY matches a known secret
    pattern, VALUE is replaced with ``***REDACTED***``. The KEY itself is
    preserved so the operator knows which argument was sanitised.

    This function operates on the command *list* so no shell-string parsing
    is required — the same safety philosophy as the rest of the driver system.

    Example::

        _redact_build_args(["docker", "build", "--build-arg", "DB_PASSWORD=hunter2"])
        # → ["docker", "build", "--build-arg", "DB_PASSWORD=***REDACTED***"]
    """
    redacted = list(cmd)
    i = 0
    while i < len(redacted):
        if redacted[i] == "--build-arg" and i + 1 < len(redacted):
            arg_value = redacted[i + 1]
            if "=" in arg_value:
                key, _, value = arg_value.partition("=")
                if _SECRET_KEY_PATTERN.search(key):
                    redacted[i + 1] = f"{key}=***REDACTED***"
        i += 1
    return redacted
