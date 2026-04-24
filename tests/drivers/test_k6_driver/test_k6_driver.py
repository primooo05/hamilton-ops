"""
Contract tests for drivers/k6_driver.py

Strategy: Every test exercises the TRANSLATION LOGIC — command construction,
metric parsing, threshold checking, and exit-code mapping — without ever
spawning a real k6 process. ``_run_subprocess`` is patched at the driver
instance level so tests remain self-contained and fast.

Test categories:
  1. Command construction & path sanitization
  2. Telemetry parsing (pure function, no subprocess)
  3. Threshold checking (pure function)
  4. Exit-code → exception mapping
  5. Health check (binary detection)
  6. Integration: run() with mocked subprocess
"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.exceptions import EnvError, HamiltonAlarm, ThresholdExceededError
from core.priorities import FlightThresholds
from drivers.k6_driver import K6Driver


def _make_driver(script="tests/scripts/load.js", **kwargs) -> K6Driver:
    """Return a K6Driver with a fake script path (no filesystem access needed)."""
    return K6Driver(script_path=script, **kwargs)


def _completed(returncode=0, stdout="", stderr="") -> subprocess.CompletedProcess:
    """Build a fake CompletedProcess to inject into _run_subprocess."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _write_k6_json(path: Path, p95: float, p99: float, error_rate_fraction: float) -> None:
    """
    Write a minimal k6 --out json file with the given metric values.
    error_rate_fraction is 0.0–1.0 (k6's native format before × 100).
    """
    lines = [
        json.dumps({
            "metric": "http_req_duration",
            "data": {"value": {"p(95)": p95, "p(99)": p99}},
        }),
        json.dumps({
            "metric": "http_req_failed",
            "data": {"value": error_rate_fraction},
        }),
    ]
    path.write_text("\n".join(lines))


def test_build_command_contains_k6_run():
    """
    Contract: The assembled command list must begin with ['k6', 'run'].
    The Supervisor passes this directly to subprocess — no shell expansion.
    """
    driver = _make_driver(script="/scripts/load.js")
    cmd = driver._build_command(Path("/tmp/out.json"))
    assert cmd[:2] == ["k6", "run"]


def test_build_command_includes_json_out_flag(tmp_path):
    """
    Contract: ``--out json=<path>`` must be present so telemetry is captured.
    Without it, _parse_metrics_file has nothing to read.
    """
    out = tmp_path / "metrics.json"
    driver = _make_driver()
    cmd = driver._build_command(out)
    assert "--out" in cmd
    # The value immediately after --out must reference our temp file
    out_idx = cmd.index("--out")
    assert str(out) in cmd[out_idx + 1]


def test_build_command_path_with_spaces(tmp_path):
    """
    Contract: A script path containing spaces must appear as a single element
    in the command list — not split across multiple elements.
    Using a list (not a shell string) guarantees OS-level quoting.
    """
    spaced = tmp_path / "my scripts" / "load test.js"
    spaced.parent.mkdir(parents=True, exist_ok=True)
    spaced.touch()

    driver = _make_driver(script=str(spaced))
    cmd = driver._build_command(tmp_path / "out.json")

    # The full path must appear as one list element
    assert str(driver.script_path) in cmd
    # And must NOT be split by the space into separate elements
    split_parts = str(driver.script_path).split()
    assert not all(part in cmd for part in split_parts if len(split_parts) > 1)


def test_build_command_sets_target_env():
    """
    Contract: ``--env TARGET=<value>`` must appear in the command so k6
    scripts can reference $TARGET instead of hardcoding a URL.
    """
    driver = _make_driver(target="http://localhost:3000")
    cmd = driver._build_command(Path("/tmp/out.json"))
    assert "--env" in cmd
    env_idx = cmd.index("--env")
    assert "TARGET=http://localhost:3000" in cmd[env_idx + 1]

def test_parse_metrics_file_extracts_p95_and_error_rate(tmp_path):
    """
    Contract: _parse_metrics_file must correctly extract P95 latency and
    error rate from a k6 JSON output file — the core telemetry signal.
    """
    out = tmp_path / "metrics.json"
    _write_k6_json(out, p95=180.5, p99=320.0, error_rate_fraction=0.005)

    driver = _make_driver()
    metrics = driver._parse_metrics_file(out)

    assert metrics["p95_ms"] == pytest.approx(180.5)
    assert metrics["p99_ms"] == pytest.approx(320.0)
    # Error rate is converted from fraction to percentage
    assert metrics["error_rate"] == pytest.approx(0.5)


def test_parse_metrics_file_returns_zeros_when_file_missing(tmp_path):
    """
    Contract: If the metrics file doesn't exist (subprocess crashed before
    writing it), _parse_metrics_file must return zeroed metrics — not raise.
    The exit-code mapper will surface the real failure.
    """
    missing = tmp_path / "nonexistent.json"
    driver = _make_driver()
    metrics = driver._parse_metrics_file(missing)

    assert metrics == {"p95_ms": 0.0, "p99_ms": 0.0, "error_rate": 0.0}


def test_parse_metrics_file_tolerates_malformed_lines(tmp_path):
    """
    Contract: Corrupted or partial JSON lines must be skipped gracefully.
    The driver should not crash when k6 writes a partial line on OOM.
    """
    out = tmp_path / "metrics.json"
    out.write_text(
        '{"metric": "http_req_duration", "data": {"value": {"p(95)": 150.0, "p(99)": 200.0}}}\n'
        "CORRUPTED LINE\n"
        "{not json at all}\n"
    )
    driver = _make_driver()
    metrics = driver._parse_metrics_file(out)

    # p95 from the valid line must be captured; others default to 0
    assert metrics["p95_ms"] == pytest.approx(150.0)
    assert metrics["error_rate"] == pytest.approx(0.0)


def test_check_thresholds_raises_on_p95_breach():
    """
    Contract: _check_thresholds must raise ThresholdExceededError when
    P95 latency exceeds the configured threshold.
    This is the primary P1 kill-switch trigger.
    """
    thresholds = FlightThresholds(p95_ms=200, p99_ms=500, error_rate_percent=1.0)
    driver = _make_driver(thresholds=thresholds)

    with pytest.raises(ThresholdExceededError) as exc_info:
        driver._check_thresholds({"p95_ms": 250.0, "p99_ms": 300.0, "error_rate": 0.5})

    assert "P95" in str(exc_info.value)
    assert "250.0" in str(exc_info.value)


def test_check_thresholds_raises_on_error_rate_breach():
    """
    Contract: _check_thresholds must raise ThresholdExceededError when the
    error rate exceeds the configured percentage — even if latency is fine.
    """
    thresholds = FlightThresholds(p95_ms=200, p99_ms=500, error_rate_percent=1.0)
    driver = _make_driver(thresholds=thresholds)

    with pytest.raises(ThresholdExceededError) as exc_info:
        driver._check_thresholds({"p95_ms": 100.0, "p99_ms": 200.0, "error_rate": 2.5})

    assert "Error rate" in str(exc_info.value)


def test_check_thresholds_reports_all_violations_in_one_raise():
    """
    Contract: If both P95 and error rate breach, the single raised exception
    must document BOTH violations — the Supervisor needs the full picture.
    """
    thresholds = FlightThresholds(p95_ms=200, p99_ms=500, error_rate_percent=1.0)
    driver = _make_driver(thresholds=thresholds)

    with pytest.raises(ThresholdExceededError) as exc_info:
        driver._check_thresholds({"p95_ms": 999.0, "p99_ms": 100.0, "error_rate": 5.0})

    error_msg = str(exc_info.value)
    assert "P95" in error_msg
    assert "Error rate" in error_msg


def test_check_thresholds_does_not_raise_on_passing_metrics():
    """
    Contract: Metrics within all thresholds must not raise any exception.
    """
    thresholds = FlightThresholds(p95_ms=200, p99_ms=500, error_rate_percent=1.0)
    driver = _make_driver(thresholds=thresholds)

    # Must not raise
    driver._check_thresholds({"p95_ms": 180.0, "p99_ms": 400.0, "error_rate": 0.5})


def test_check_thresholds_context_contains_metric_values():
    """
    Contract: The ThresholdExceededError context dict must include the
    actual metric values so the Supervisor can emit a structured telemetry log.
    """
    thresholds = FlightThresholds(p95_ms=200, p99_ms=500, error_rate_percent=1.0)
    driver = _make_driver(thresholds=thresholds)

    with pytest.raises(ThresholdExceededError) as exc_info:
        driver._check_thresholds({"p95_ms": 999.0, "p99_ms": 100.0, "error_rate": 0.1})

    assert "metrics" in exc_info.value.context
    assert exc_info.value.context["metrics"]["p95_ms"] == 999.0

def test_map_exit_code_127_raises_hamilton_alarm():
    """
    Contract: Exit code 127 (command not found) must raise HamiltonAlarm —
    a P1 signal indicating the entire validation stream is unrecoverable.
    """
    with pytest.raises(HamiltonAlarm) as exc_info:
        K6Driver._map_exit_code(127, "")

    assert "127" in str(exc_info.value) or "not found" in str(exc_info.value).lower()
    assert exc_info.value.context["exit_code"] == 127


def test_map_exit_code_137_raises_hamilton_alarm_with_oom_message():
    """
    Contract: Exit code 137 (OOM-killed) must raise HamiltonAlarm with a
    message directing the operator to reduce VU count or add memory.
    """
    with pytest.raises(HamiltonAlarm) as exc_info:
        K6Driver._map_exit_code(137, "")

    assert exc_info.value.context["exit_code"] == 137
    assert "137" in str(exc_info.value) or "oom" in str(exc_info.value).lower()


def test_map_exit_code_generic_non_zero_raises_hamilton_alarm():
    """
    Contract: Any other non-zero exit code must also raise HamiltonAlarm —
    the Supervisor should never receive a generic Exception from a driver.
    """
    with pytest.raises(HamiltonAlarm) as exc_info:
        K6Driver._map_exit_code(1, "threshold check failed")

    assert exc_info.value.context["exit_code"] == 1


def test_check_health_raises_env_error_when_k6_missing():
    """
    Contract: check_health() must raise EnvError (not HamiltonAlarm) when
    k6 is absent — this is a pre-flight environment failure, not a P1 alarm.
    """
    driver = _make_driver()
    # Patch shutil.which to simulate k6 not being on PATH
    with patch("drivers.k6_driver.shutil.which", return_value=None):
        with pytest.raises(EnvError) as exc_info:
            driver.check_health()

    assert exc_info.value.context["tool"] == "k6"


def test_check_health_raises_env_error_when_version_fails():
    """
    Contract: If k6 is on PATH but ``k6 version`` returns non-zero,
    check_health() must still raise EnvError.
    """
    driver = _make_driver()
    with patch("drivers.k6_driver.shutil.which", return_value="/usr/local/bin/k6"):
        driver._run_subprocess = MagicMock(return_value=_completed(returncode=1, stderr="error"))
        with pytest.raises(EnvError):
            driver.check_health()


def test_check_health_returns_driver_result_on_success():
    """
    Contract: A successful health check must return DriverResult(success=True)
    containing the version string — used by 'hamilton doctor' reporting.
    """
    driver = _make_driver()
    with patch("drivers.k6_driver.shutil.which", return_value="/usr/local/bin/k6"):
        driver._run_subprocess = MagicMock(
            return_value=_completed(returncode=0, stdout="k6 v0.49.0 (go1.21.0)")
        )
        result = driver.check_health()

    assert isinstance(result.output, dict)
    assert "version" in result.output
    

def test_run_returns_driver_result_on_success(tmp_path):
    """
    Contract: run() must return DriverResult(success=True) when k6 exits 0
    and all metrics are within thresholds.
    The subprocess is mocked — we inject the metrics file ourselves.
    """
    driver = _make_driver(thresholds=FlightThresholds(p95_ms=200, p99_ms=500, error_rate_percent=1.0))

    def fake_run(cmd):
        # Locate the json output path from the command and write fake metrics
        for i, arg in enumerate(cmd):
            if arg == "--out":
                json_path = Path(cmd[i + 1].replace("json=", ""))
                _write_k6_json(json_path, p95=150.0, p99=300.0, error_rate_fraction=0.001)
                break
        return _completed(returncode=0)

    driver._run_subprocess = fake_run
    result = driver.run()

    assert result.success is True
    assert result.output["p95_ms"] == pytest.approx(150.0)


def test_run_raises_threshold_exceeded_error_on_high_p95(tmp_path):
    """
    Contract: run() must raise ThresholdExceededError (not return a DriverResult)
    when k6 reports a P95 above the configured threshold.
    """
    driver = _make_driver(thresholds=FlightThresholds(p95_ms=200, p99_ms=500, error_rate_percent=1.0))

    def fake_run(cmd):
        for i, arg in enumerate(cmd):
            if arg == "--out":
                json_path = Path(cmd[i + 1].replace("json=", ""))
                _write_k6_json(json_path, p95=999.0, p99=1500.0, error_rate_fraction=0.0)
                break
        return _completed(returncode=0)

    driver._run_subprocess = fake_run

    with pytest.raises(ThresholdExceededError):
        driver.run()


def test_run_raises_hamilton_alarm_on_nonzero_exit(tmp_path):
    """
    Contract: run() must raise HamiltonAlarm when k6 crashes (non-zero exit)
    and no metrics were written — the validation stream is unrecoverable.
    """
    driver = _make_driver()

    def fake_run(cmd):
        # Do not write any metrics file — simulate a hard crash
        return _completed(returncode=137, stderr="Killed")

    driver._run_subprocess = fake_run

    with pytest.raises(HamiltonAlarm):
        driver.run()
