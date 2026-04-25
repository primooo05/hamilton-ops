"""
Hamilton-Ops Docker Driver — P3 Construction Stream

Responsibility: Translate a high-level "Build Image" command from the
Supervisor into a concrete ``docker build`` invocation against the
immutable staging directory, then map exit codes to structured signals.

Security notes:
    - Build context always points to the STAGING directory, never the
      live source tree (prevents dirty-cache states and race conditions).
    - ``--no-cache`` is enforced by default to prevent cache poisoning.
    - The Docker daemon must be running in rootless mode; ``check_health``
      verifies this before any build is attempted.

Error-mapping contract:
    | Condition                    | Hamilton Signal              |
    |------------------------------|------------------------------|
    | docker binary missing (127)  | EnvError                     |
    | OOM-killed (137)             | BuildError (oom=True context)|
    | Non-zero exit (build failure)| BuildError                   |
    | Daemon running as root        | EnvError (rootless violation)|
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from core.exceptions import BuildError, EnvError
from drivers.registry import DriverResult

logger = logging.getLogger("hamilton.drivers.docker")

_EXIT_NOT_FOUND = 127
_EXIT_OOM       = 137

class DockerDriver:
    """
    Stateless translation layer for the Docker BuildKit engine (P3 Construction).

    Subprocess execution is routed through ``_run_subprocess`` so that tests
    can inject controlled ``CompletedProcess`` objects without spawning Docker.
    """

    def __init__(
        self,
        stage_path: str | Path,
        image_tag: str,
        dockerfile: Optional[str | Path] = None,
        no_cache: bool = True,
    ) -> None:
        """
        Args:
            stage_path:  Path to the immutable staging directory
                         produced by ``StagingContext``.  May contain spaces.
            image_tag:   Docker image tag, e.g. ``myapp:sha256-abc123``.
            dockerfile:  Path to the Dockerfile.  Defaults to
                         ``<stage_path>/Dockerfile``.
            no_cache:    Enforce ``--no-cache`` to prevent BuildKit cache
                         poisoning attacks.  Defaults to True.
        """
        self.stage_path = Path(stage_path).resolve()
        self.image_tag  = image_tag
        self.dockerfile = Path(dockerfile).resolve() if dockerfile else self.stage_path / "Dockerfile"
        self.no_cache   = no_cache

    def run(self) -> DriverResult:
        """
        Execute ``docker build`` against the staging directory.

        Raises:
            BuildError: If the build fails for any reason.
            EnvError:   If the Docker binary is missing (exit 127).
        """
        cmd = self._build_command()
        logger.info("DOCKER: Launching build stream → %s", cmd)
        completed = self._run_subprocess(cmd)

        if completed.returncode != 0:
            self._map_exit_code(completed.returncode, completed.stderr)

        return DriverResult(
            success=True,
            output={"image_tag": self.image_tag, "stdout": completed.stdout},
        )

    def check_health(self) -> DriverResult:
        """
        Verify Docker is available, daemon is reachable, and running rootless.

        Raises:
            EnvError: If Docker is absent, the daemon is down, or rootless
                      mode is not active (security requirement per README).
        """
        if not shutil.which("docker"):
            raise EnvError(
                "docker binary not found on PATH. "
                "Install Docker from https://docs.docker.com/get-docker/",
                context={"tool": "docker"},
            )

        # ``docker info`` fails if the daemon is not running
        info_result = self._run_subprocess(["docker", "info", "--format", "{{json .}}"])
        if info_result.returncode != 0:
            raise EnvError(
                f"Docker daemon is not reachable: {info_result.stderr.strip()}",
                context={"tool": "docker", "exit_code": info_result.returncode},
            )

        # Rootless mode guard: the SecurityOptions list must contain "rootless"
        import json as _json
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
            # Non-fatal: if we can't parse the JSON we skip the rootless check
            # rather than blocking a valid environment.
            logger.warning("DOCKER: Could not parse 'docker info' JSON — rootless check skipped.")

        version_result = self._run_subprocess(["docker", "version", "--format", "{{.Server.Version}}"])
        version = version_result.stdout.strip() if version_result.returncode == 0 else "unknown"
        return DriverResult(success=True, output={"version": version})


    def _build_command(self) -> list[str]:
        """
        Assemble the ``docker build`` argument list.

        The build context is the staging directory (not the live source).
        Using a list prevents any shell interpretation of path characters.
        """
        cmd = [
            "docker", "build",
            "--file", str(self.dockerfile),
            "--tag",  self.image_tag,
        ]
        if self.no_cache:
            cmd.append("--no-cache")

        # Build context last — always the immutable staging directory.
        cmd.append(str(self.stage_path))
        return cmd

    @staticmethod
    def _map_exit_code(code: int, stderr: str) -> None:
        """
        Translate a non-zero Docker exit code into the correct Hamilton signal.

        Raises:
            AssertionError: If called with code=0 (logic error).
            EnvError:       On exit 127 (binary not found).
            BuildError:     On all other failures, with OOM context when exit 137.
        """
        assert code != 0, "_map_exit_code should never be called for a successful exit (code 0)"
        if code == _EXIT_NOT_FOUND:
            raise EnvError(
                "docker binary not found during execution (exit 127). "
                "Pre-flight health check should have caught this.",
                context={"exit_code": code},
            )
        if code == _EXIT_OOM:
            raise BuildError(
                "Docker build process was OOM-killed (exit 137). "
                "Increase Docker memory limit or simplify the build.",
                context={"exit_code": code, "oom": True},
            )
        err_msg = stderr.strip() if stderr else "No error output captured"
        raise BuildError(
            f"docker build failed with exit code {code}: {err_msg}",
            context={"exit_code": code, "stderr": stderr or ""},
        )

    def _run_subprocess(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """
        Thin wrapper around subprocess.run — patch this in tests.
        """
        return subprocess.run(cmd, capture_output=True, text=True)
