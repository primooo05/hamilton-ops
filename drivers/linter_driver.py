"""
Hamilton-Ops Linter Driver — P2 Quality Stream

Responsibility: Execute a configured linter against the staging directory
and map its exit code to the correct Hamilton P2 signal.
"""

from __future__ import annotations

import logging
import shutil
import asyncio
from pathlib import Path
from typing import Optional

from core.exceptions import EnvError, QualityViolation
from drivers.registry import DriverResult

logger = logging.getLogger("hamilton.drivers.linter")

_EXIT_NOT_FOUND = 127
_DEFAULT_LINTER_CMD = ["flake8"]


class LinterDriver:
    """
    Stateless translation layer for code quality tools (P2 Quality).
    """

    def __init__(
        self,
        stage_path: str | Path,
        tool_cmd: Optional[list[str]] = None,
    ) -> None:
        """
        Args:
            stage_path: Absolute path to the immutable staging directory.
            tool_cmd:   Base linter command, e.g. ``["flake8"]``.
        """
        self.stage_path = Path(stage_path).resolve() if stage_path else None
        self.tool_cmd   = list(tool_cmd) if tool_cmd else list(_DEFAULT_LINTER_CMD)

    async def run(self) -> DriverResult:
        """
        Execute the linter asynchronously against the staging directory.

        Raises:
            QualityViolation: If the linter reports any issues (non-zero exit).
            EnvError:         If the linter binary is not found.
        """
        if not self.stage_path:
            raise EnvError("LinterDriver.run() called without a valid stage_path.")

        cmd = self._build_command()
        logger.info("LINTER: Launching quality stream → %s", cmd)
        
        try:
            stdout, stderr, returncode = await self._run_subprocess_async(cmd)
        except FileNotFoundError:
            binary = self.tool_cmd[0]
            raise EnvError(
                f"Linter binary '{binary}' not found on PATH. "
                f"Install it or update the linter tool_cmd configuration.",
                context={"tool": binary},
            )

        if returncode != 0:
            self._map_exit_code(returncode, stdout, stderr)

        return DriverResult(
            success=True,
            output={"stdout": stdout, "violations": 0},
        )

    async def check_health(self) -> DriverResult:
        """
        Verify that the configured linter binary is available on PATH.

        Raises:
            EnvError: If the binary is not found or --version returns non-zero.
        """
        binary = self.tool_cmd[0]
        if not shutil.which(binary):
            raise EnvError(
                f"Linter binary '{binary}' not found on PATH.",
                context={"tool": binary},
            )
        
        try:
            stdout, stderr, returncode = await self._run_subprocess_async([binary, "--version"])
        except FileNotFoundError:
            raise EnvError(
                f"Linter binary '{binary}' not found on PATH.",
                context={"tool": binary},
            )

        if returncode != 0:
            raise EnvError(
                f"Linter binary '{binary}' --version check failed (exit {returncode}): {stderr.strip()}",
                context={"tool": binary, "exit_code": returncode},
            )

        stripped = stdout.strip() if stdout else ""
        lines = stripped.splitlines()
        version = lines[0] if lines else "unknown"
        return DriverResult(success=True, output={"version": version})

    async def _run_subprocess_async(self, cmd: list[str]) -> tuple[str, str, int]:
        """
        Asynchronous wrapper for subprocess execution.
        Returns (stdout, stderr, returncode).
        """
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await proc.communicate()
            return (
                stdout_bytes.decode("utf-8", errors="replace"),
                stderr_bytes.decode("utf-8", errors="replace"),
                proc.returncode or 0,
            )
        except asyncio.CancelledError:
            # Hardening for Windows: ensure process is reaped on cancellation
            if proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
            raise

    def _build_command(self) -> list[str]:
        """Append the staging path to the base linter command."""
        return [*self.tool_cmd, str(self.stage_path)]

    def _map_exit_code(self, code: int, stdout: str, stderr: str) -> None:
        """Translate a non-zero linter exit code into the correct Hamilton signal."""
        if code == 0:
            return
        if code == _EXIT_NOT_FOUND:
            raise EnvError(
                f"Linter binary '{self.tool_cmd[0]}' not found during execution.",
                context={"exit_code": code, "tool": self.tool_cmd[0], "stderr": stderr},
            )
        violation_count = len([l for l in stdout.splitlines() if l.strip()])
        raise QualityViolation(
            f"Linter detected {violation_count} violation(s) in staging area.",
            context={
                "exit_code": code,
                "tool": self.tool_cmd[0],
                "violations": violation_count,
                "output": stdout,
                "stderr": stderr,
            },
        )
