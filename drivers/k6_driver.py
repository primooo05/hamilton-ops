"""
Hamilton-Ops k6 Driver — P1 Validation Stream

Responsibility: Translate a high-level "Run Validation" command from the
Supervisor into a concrete ``k6 run`` invocation, then parse the raw output
into structured telemetry the Supervisor can act on.

Why JSON output?
    k6 supports ``--out json=<file>`` which dumps every metric as a
    newline-delimited JSON stream.  This is far more reliable to parse
    than the human-readable terminal output, which changes across k6
    versions and is locale-sensitive.

Error-mapping contract:
    | Condition                    | Hamilton Signal            |
    |------------------------------|----------------------------|
    | p95 > threshold              | ThresholdExceededError     |
    | error_rate > threshold       | ThresholdExceededError     |
    | k6 binary missing (exit 127) | HamiltonAlarm              |
    | OOM kill (exit 137)          | HamiltonAlarm              |
    | Any other non-zero exit      | HamiltonAlarm              |
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import asyncio
from pathlib import Path
from typing import Optional

from core.exceptions import EnvError, HamiltonAlarm, ThresholdExceededError
from core.priorities import FlightThresholds
from drivers.registry import DriverResult

logger = logging.getLogger("hamilton.drivers.k6")

# Exit codes with specific semantics that must not be treated as generic failures.
_EXIT_NOT_FOUND = 127   # shell: command not found (k6 binary missing)
_EXIT_OOM       = 137   # Linux OOM killer or ``kill -9``


class K6Driver:
    """
    Stateless translation layer for the k6 load-testing tool (P1 Validation).

    All methods that construct or run subprocesses delegate the actual
    subprocess execution through ``_run_subprocess_async`` so that tests can
    patch a single injection point without needing to mock deep internals.
    """

    def __init__(
        self,
        script_path: str | Path,
        thresholds: Optional[FlightThresholds] = None,
        target: str = "http://localhost",
    ) -> None:
        """
        Args:
            script_path: Absolute path to the k6 JavaScript test script.
            thresholds:  Frozen flight thresholds loaded from pyproject.toml.
            target:      Injection point for the TARGET environment variable.
        """
        self.script_path = Path(script_path).resolve()
        self.thresholds = thresholds or FlightThresholds()
        self.target = target

    async def run(self) -> DriverResult:
        """
        Execute k6 asynchronously, parse telemetry, and return a DriverResult.

        Raises:
            ThresholdExceededError: If P95 or error rate breaches thresholds.
            HamiltonAlarm:          If k6 crashes or cannot be found.
            EnvError:               If the k6 binary is missing (especially on Windows).
        """
        with tempfile.TemporaryDirectory() as tmp:
            json_out = Path(tmp) / "metrics.json"
            cmd = self._build_command(json_out)

            logger.info("K6: Launching validation stream → %s", cmd)
            
            try:
                stdout, stderr, returncode = await self._run_subprocess_async(
                    cmd, env={"TARGET": self.target}
                )
            except FileNotFoundError:
                raise EnvError(
                    "k6 binary not found on PATH. "
                    "Install k6 from https://grafana.com/docs/k6/latest/set-up/install-k6/",
                    context={"tool": "k6"},
                )

            # Exit code is checked FIRST.
            if returncode != 0:
                self._map_exit_code(returncode, stderr)

            metrics = self._parse_metrics_file(json_out)
            try:
                self._check_thresholds(metrics)
            except ThresholdExceededError as e:
                # If we have a total failure, attach stderr to the exception context
                # so the user can see WHY k6 couldn't connect.
                if metrics.get("error_rate", 0.0) >= 100.0:
                    e.context["k6_stderr"] = stderr
                    logger.error("K6: Total validation failure. Diagnostics:\n%s", stderr)
                raise

        return DriverResult(
            success=True,
            output=metrics,
        )

    async def check_health(self) -> DriverResult:
        """
        Verify that k6 is installed and reachable on PATH.

        Raises:
            EnvError: If ``k6 version`` fails or k6 is not found.
        """
        if not shutil.which("k6"):
            raise EnvError(
                "k6 binary not found on PATH. "
                "Install k6 from https://grafana.com/docs/k6/latest/set-up/install-k6/",
                context={"tool": "k6"},
            )
        
        try:
            stdout, stderr, returncode = await self._run_subprocess_async(["k6", "version"])
        except FileNotFoundError:
            raise EnvError(
                "k6 binary not found on PATH.",
                context={"tool": "k6"},
            )

        if returncode != 0:
            raise EnvError(
                f"k6 version check failed: {stderr.strip()}",
                context={"tool": "k6", "exit_code": returncode},
            )
            
        stripped_stdout = stdout.strip() if stdout else ""
        lines = stripped_stdout.splitlines()
        version_line = lines[0] if lines else "unknown"
        return DriverResult(success=True, output={"version": version_line})

    async def _run_subprocess_async(
        self, cmd: list[str], env: Optional[dict] = None
    ) -> tuple[str, str, int]:
        """
        Asynchronous wrapper for subprocess execution.
        Returns (stdout, stderr, returncode).
        """
        import os
        merged_env = {**os.environ, **(env or {})}
        
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
        )
        try:
            stdout_bytes, stderr_bytes = await proc.communicate()
            return (
                stdout_bytes.decode("utf-8", errors="replace"),
                stderr_bytes.decode("utf-8", errors="replace"),
                proc.returncode or 0,
            )
        except asyncio.CancelledError:
            if proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
            raise

    def _build_command(self, json_out: Path) -> list[str]:
        """Assemble the k6 CLI argument list."""
        return [
            "k6", "run",
            "--out", f"json={json_out}",
            "--env", f"TARGET={self.target}",
            str(self.script_path),
        ]

    def _parse_metrics_file(self, json_out: Path) -> dict:
        """Extract P95, P99, and error_rate from the k6 JSON metrics file."""
        if not json_out.exists():
            logger.warning("K6: Metrics file not found — subprocess may have crashed.")
            return {"p95_ms": 0.0, "p99_ms": 0.0, "error_rate": 0.0}

        p95 = p99 = error_rate = 0.0
        with json_out.open() as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                metric_name = entry.get("metric")
                data        = entry.get("data", {})
                value       = data.get("value", {})

                if metric_name == "http_req_duration" and isinstance(value, dict):
                    p95 = float(value.get("p(95)", 0.0))
                    p99 = float(value.get("p(99)", 0.0))
                elif metric_name == "http_req_failed" and isinstance(value, (int, float)):
                    error_rate = float(value) * 100.0

        return {"p95_ms": p95, "p99_ms": p99, "error_rate": error_rate}

    def _check_thresholds(self, metrics: dict) -> None:
        """Compare extracted metrics against FlightThresholds."""
        p95        = metrics.get("p95_ms", 0.0)
        p99        = metrics.get("p99_ms", 0.0)
        error_rate = metrics.get("error_rate", 0.0)

        violations: list[str] = []
        if p95 > self.thresholds.p95_ms:
            violations.append(f"P95 latency {p95:.1f}ms exceeds threshold {self.thresholds.p95_ms}ms")
        if p99 > self.thresholds.p99_ms:
            violations.append(f"P99 latency {p99:.1f}ms exceeds threshold {self.thresholds.p99_ms}ms")
        if error_rate > self.thresholds.error_rate_percent:
            suffix = ""
            if error_rate >= 100.0:
                suffix = f" (Target '{self.target}' may be unreachable)"
            violations.append(f"Error rate {error_rate:.2f}% exceeds threshold {self.thresholds.error_rate_percent}%{suffix}")

        if violations:
            raise ThresholdExceededError(
                f"P1 threshold breach: {'; '.join(violations)}",
                context={
                    "metrics": metrics,
                    "thresholds": {
                        "p95_ms": self.thresholds.p95_ms,
                        "p99_ms": self.thresholds.p99_ms,
                        "error_rate_percent": self.thresholds.error_rate_percent,
                    },
                    "violations": violations,
                },
            )

    @staticmethod
    def _map_exit_code(code: int, stderr: str) -> None:
        """Translate a non-zero k6 exit code into the correct Hamilton signal."""
        if code == 0:
            return
        if code == _EXIT_NOT_FOUND:
            raise HamiltonAlarm(
                "k6 binary not found during execution (exit 127).",
                context={"exit_code": code},
            )
        if code == _EXIT_OOM:
            raise HamiltonAlarm(
                "k6 process was OOM-killed (exit 137).",
                context={"exit_code": code},
            )
        raise HamiltonAlarm(
            f"k6 exited with code {code}: {stderr.strip()}",
            context={"exit_code": code, "stderr": stderr},
        )
