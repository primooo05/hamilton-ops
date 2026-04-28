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
    ``subprocess.run`` call through ``_run_subprocess`` so that tests can
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
                         May contain spaces — the driver ensures correct quoting.
            thresholds:  Frozen flight thresholds loaded from pyproject.toml.
                         Defaults to the spec baseline if not provided.
            target:      Injection point for the TARGET environment variable.
                         Defaults to localhost to prevent accidental DDoS.
        """
        self.script_path = Path(script_path).resolve()
        self.thresholds = thresholds or FlightThresholds()
        self.target = target

    def run(self) -> DriverResult:
        """
        Execute k6, parse telemetry, and return a DriverResult.

        Raises:
            ThresholdExceededError: If P95 or error rate breaches thresholds.
            HamiltonAlarm:          If k6 crashes or cannot be found.
        """
        with tempfile.TemporaryDirectory() as tmp:
            json_out = Path(tmp) / "metrics.json"
            cmd = self._build_command(json_out)

            logger.info("K6: Launching validation stream → %s", cmd)
            completed = self._run_subprocess(cmd)

            # Exit code is checked FIRST so a process crash is never masked by a coincident threshold breach.
            # If k6 crashed hard enough to produce bad/zero metrics, _check_thresholds would silently pass
            # (zeros are within thresholds), hiding the real failure from the Supervisor.  Crash → HamiltonAlarm must always win.
            if completed.returncode != 0:
                self._map_exit_code(completed.returncode, completed.stderr)

            # Only reached when k6 exited cleanly (returncode == 0).
            metrics = self._parse_metrics_file(json_out)
            self._check_thresholds(metrics)

        return DriverResult(
            success=True,
            output=metrics,
        )

    def check_health(self) -> DriverResult:
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
        completed = self._run_subprocess(["k6", "version"])
        if completed.returncode != 0:
            raise EnvError(
                f"k6 version check failed: {completed.stderr.strip()}",
                context={"tool": "k6", "exit_code": completed.returncode},
            )
        # strip() first, then splitlines(), so whitespace-only stdout
        # (e.g. "  \n  ") yields an empty list instead of a list with a blank
        # string — falling back to "unknown" rather than raising IndexError.
        stripped_stdout = completed.stdout.strip() if completed.stdout else ""
        lines = stripped_stdout.splitlines()
        version_line = lines[0] if lines else "unknown"
        return DriverResult(success=True, output={"version": version_line})

    def _build_command(self, json_out: Path) -> list[str]:
        """
        Assemble the k6 CLI argument list.

        Using a list (not a shell string) ensures the OS handles quoting —
        paths with spaces or special characters are passed safely without
        shell interpretation.
        """
        return [
            "k6", "run",
            "--out", f"json={json_out}",
            "--env", f"TARGET={self.target}",
            str(self.script_path),   # Path.resolve() gives an absolute POSIX-safe str
        ]

    def _parse_metrics_file(self, json_out: Path) -> dict:
        """
        Extract P95, P99, and error_rate from the k6 JSON metrics file.

        k6 ``--out json`` emits one JSON object per line.  We scan for the
        ``http_req_duration`` trend metric (which contains percentile data)
        and the ``http_req_failed`` rate metric.

        Returns a dict with keys: ``p95_ms``, ``p99_ms``, ``error_rate``.
        If the file does not exist or is empty, returns zeroed metrics
        (the subprocess exit code will surface the real failure).
        """
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
                    # k6 stores trend percentiles as nested keys
                    p95 = float(value.get("p(95)", 0.0))
                    p99 = float(value.get("p(99)", 0.0))

                elif metric_name == "http_req_failed" and isinstance(value, (int, float)):
                    # Rate metric: value is already a fraction (0.0–1.0)
                    error_rate = float(value) * 100.0

        return {"p95_ms": p95, "p99_ms": p99, "error_rate": error_rate}

    def _check_thresholds(self, metrics: dict) -> None:
        """
        Compare extracted metrics against FlightThresholds.

        Raises:
            ThresholdExceededError: If any metric breaches its threshold.
        """
        p95        = metrics.get("p95_ms", 0.0)
        p99        = metrics.get("p99_ms", 0.0)
        error_rate = metrics.get("error_rate", 0.0)

        violations: list[str] = []

        if p95 > self.thresholds.p95_ms:
            violations.append(
                f"P95 latency {p95:.1f}ms exceeds threshold {self.thresholds.p95_ms}ms"
            )
        if p99 > self.thresholds.p99_ms:
            violations.append(
                f"P99 latency {p99:.1f}ms exceeds threshold {self.thresholds.p99_ms}ms"
            )
        if error_rate > self.thresholds.error_rate_percent:
            violations.append(
                f"Error rate {error_rate:.2f}% exceeds threshold "
                f"{self.thresholds.error_rate_percent}%"
            )

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
        """
        Translate a non-zero k6 exit code into the correct Hamilton signal.

        The Supervisor must never see a raw integer — it must receive a
        structured exception it can route through the Priority state machine.

        Args:
            code:   The subprocess return code.
            stderr: The captured stderr text for diagnostic context.

        Returns:
            None if ``code`` is 0 (success — no alarm needed).

        Raises:
            HamiltonAlarm: For every non-zero code — the specific message
                           encodes the cause (missing binary, OOM, generic).
        """
        # Guard against callers passing code=0.  run() only calls
        # this method on non-zero exits, but the @staticmethod is publicly
        # accessible.  Without this guard, code=0 falls through to the generic
        # HamiltonAlarm which is semantically wrong (0 means success).
        if code == 0:
            return

        if code == _EXIT_NOT_FOUND:
            raise HamiltonAlarm(
                "k6 binary not found during execution (exit 127). "
                "Pre-flight health check should have caught this.",
                context={"exit_code": code},
            )
        if code == _EXIT_OOM:
            raise HamiltonAlarm(
                "k6 process was OOM-killed (exit 137). "
                "Reduce VU count or increase system memory.",
                context={"exit_code": code},
            )
        raise HamiltonAlarm(
            f"k6 exited with code {code}: {stderr.strip()}",
            context={"exit_code": code, "stderr": stderr},
        )

    def _run_subprocess(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """
        Thin wrapper around subprocess.run.

        Exists as a named method so tests can patch ``K6Driver._run_subprocess``
        without mocking the entire subprocess module.
        """
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env={"TARGET": self.target},  # restrict environment for security
        )
