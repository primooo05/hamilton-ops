"""
Hamilton-Ops Linter Driver — P2 Quality Stream

Responsibility: Execute a configured linter against the staging directory
and map its exit code to the correct Hamilton P2 signal.

Design notes:
    - Tool-agnostic: the linter command is configurable (default: flake8).
      This allows the driver to wrap flake8 for Python projects, eslint for
      JavaScript, or any other linter without changing core logic.
    - In --strict mode, the Supervisor may escalate a QualityViolation to a
      P1 Alarm. This decision lives in the Supervisor, not here.

Error-mapping contract:
    | Condition                    | Hamilton Signal     |
    |------------------------------|---------------------|
    | Linter binary missing (127)  | EnvError            |
    | Non-zero exit (style issues) | QualityViolation    |
    | Zero exit                    | DriverResult(ok)    |
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from core.exceptions import EnvError, QualityViolation
from drivers.registry import DriverResult

logger = logging.getLogger("hamilton.drivers.linter")

_EXIT_NOT_FOUND = 127

# Default linter: flake8. Swap to ["eslint", "--ext", ".js", ".ts"]
# for JavaScript projects via config.
_DEFAULT_LINTER_CMD = ["flake8"]


class LinterDriver:
    """
    Stateless translation layer for code quality tools (P2 Quality).

    ``tool_cmd`` is the base command list — the target path is appended at
    runtime so the driver remains decoupled from the tool's invocation style.
    """

    def __init__(
        self,
        stage_path: str | Path,
        tool_cmd: Optional[list[str]] = None,
    ) -> None:
        """
        Args:
            stage_path: Absolute path to the immutable staging directory.
                        The linter runs against this snapshot, not the live tree.
            tool_cmd:   Base linter command, e.g. ``["flake8"]`` or
                        ``["eslint", "--ext", ".js"]``.
                        Defaults to ``["flake8"]``.
        """
        self.stage_path = Path(stage_path).resolve()
        self.tool_cmd   = tool_cmd or list(_DEFAULT_LINTER_CMD)

    def run(self) -> DriverResult:
        """
        Execute the linter against the staging directory.

        Raises:
            QualityViolation: If the linter reports any issues (non-zero exit).
            EnvError:         If the linter binary is not found (exit 127).
        """
        cmd = self._build_command()
        logger.info("LINTER: Launching quality stream → %s", cmd)
        completed = self._run_subprocess(cmd)

        if completed.returncode != 0:
            self._map_exit_code(completed.returncode, completed.stdout, completed.stderr)

        return DriverResult(
            success=True,
            output={"stdout": completed.stdout, "violations": 0},
        )

    def check_health(self) -> DriverResult:
        """
        Verify that the configured linter binary is available on PATH.

        Raises:
            EnvError: If the binary is not found.
        """
        binary = self.tool_cmd[0]
        if not shutil.which(binary):
            raise EnvError(
                f"Linter binary '{binary}' not found on PATH. "
                f"Install it or update the linter tool_cmd configuration.",
                context={"tool": binary},
            )
        # Run with --version (works for flake8, eslint, ruff, etc.)
        completed = self._run_subprocess([binary, "--version"])
        version = completed.stdout.strip().splitlines()[0] if completed.stdout else "unknown"
        return DriverResult(success=True, output={"version": version})

    def _build_command(self) -> list[str]:
        """
        Append the staging path to the base linter command.

        The stage_path may contain spaces — passing it as a list element
        (not a shell string) ensures the OS handles it safely.
        """
        return [*self.tool_cmd, str(self.stage_path)]

    def _map_exit_code(self, code: int, stdout: str, stderr: str) -> None:
        """
        Translate a non-zero linter exit code into the correct Hamilton signal.

        Raises:
            EnvError:        On exit 127 (binary not found at runtime).
            QualityViolation: On all other non-zero exits (hygiene issues).
        """
        if code == _EXIT_NOT_FOUND:
            raise EnvError(
                f"Linter binary '{self.tool_cmd[0]}' not found during execution (exit 127). "
                "Pre-flight health check should have caught this.",
                context={"exit_code": code, "tool": self.tool_cmd[0]},
            )
        # Count violations from stdout (linters typically emit one line per issue)
        violation_count = len([l for l in stdout.splitlines() if l.strip()])
        raise QualityViolation(
            f"Linter detected {violation_count} violation(s) in staging area. "
            "Run with --strict to escalate to P1.",
            context={
                "exit_code": code,
                "tool": self.tool_cmd[0],
                "violations": violation_count,
                "output": stdout,
            },
        )

    def _run_subprocess(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """
        Thin wrapper around subprocess.run — patch this in tests.
        """
        return subprocess.run(cmd, capture_output=True, text=True)
