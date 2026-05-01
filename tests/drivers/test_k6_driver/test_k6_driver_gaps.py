"""
Gap tests for drivers/k6_driver.py

This file targets the 23 behavioral gaps identified after the initial
test_k6_driver.py review.

Gaps are numbered GAP-01 through GAP-23 to match the analysis document.

Strategy:
  - No real k6 process is spawned.
  - _run_subprocess_async is patched at the instance level.
  - Each test has ONE clear assertion focus; combined scenarios use
    descriptive names to explain the compound intent.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from core.exceptions import EnvError, HamiltonAlarm, ThresholdExceededError
from core.priorities import FlightThresholds
from drivers.k6_driver import K6Driver


def _make_driver(script="tests/scripts/load.js", **kwargs) -> K6Driver:
    """Return a K6Driver with a fake script path (no filesystem access needed)."""
    return K6Driver(script_path=script, **kwargs)


def _completed_async(returncode=0, stdout="", stderr="") -> tuple[str, str, int]:
    """Build a fake return value for _run_subprocess_async."""
    return (stdout, stderr, returncode)


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


def test_init_resolves_relative_path_to_absolute(tmp_path):
    """
    GAP-01 | __init__: Path resolution — relative → absolute via Path.resolve().
    """
    relative = "tests/scripts/load.js"
    driver = K6Driver(script_path=relative)
    assert driver.script_path.is_absolute()
    assert driver.script_path == Path(relative).resolve()


def test_init_defaults_thresholds_to_flight_thresholds():
    """
    GAP-02 | __init__: Default thresholds — thresholds=None → FlightThresholds().
    """
    driver = _make_driver()
    assert isinstance(driver.thresholds, FlightThresholds)
    assert driver.thresholds.p95_ms == 200


def test_init_default_target_is_localhost():
    """
    GAP-03 | __init__: Default target defaults to "http://localhost".
    """
    driver = _make_driver()
    assert driver.target == "http://localhost"


def test_parse_metrics_file_empty_file_returns_zeroed_metrics(tmp_path):
    """
    GAP-04 | _parse_metrics_file: Empty file (0 bytes) → zeroed metrics.
    """
    out = tmp_path / "metrics.json"
    out.write_text("")
    driver = _make_driver()
    metrics = driver._parse_metrics_file(out)
    assert metrics == {"p95_ms": 0.0, "p99_ms": 0.0, "error_rate": 0.0}


def test_parse_metrics_file_missing_data_key_degrades_silently(tmp_path):
    """
    GAP-05 | _parse_metrics_file: Missing "data" key → silent degradation to 0.0.
    """
    out = tmp_path / "metrics.json"
    out.write_text(json.dumps({"metric": "http_req_duration"}) + "\n")
    driver = _make_driver()
    metrics = driver._parse_metrics_file(out)
    assert metrics["p95_ms"] == pytest.approx(0.0)


def test_parse_metrics_file_scalar_duration_value_is_skipped_by_isinstance_guard(tmp_path):
    """
    GAP-06 | _parse_metrics_file: Wrong value type for http_req_duration (scalar).
    """
    out = tmp_path / "metrics.json"
    out.write_text(json.dumps({"metric": "http_req_duration", "data": {"value": 42.0}}) + "\n")
    driver = _make_driver()
    metrics = driver._parse_metrics_file(out)
    assert metrics["p95_ms"] == pytest.approx(0.0)


def test_parse_metrics_file_dict_error_rate_value_is_skipped_by_isinstance_guard(tmp_path):
    """
    GAP-07 | _parse_metrics_file: Wrong value type for http_req_failed (dict).
    """
    out = tmp_path / "metrics.json"
    out.write_text(json.dumps({"metric": "http_req_failed", "data": {"value": {"rate": 0.5}}}) + "\n")
    driver = _make_driver()
    metrics = driver._parse_metrics_file(out)
    assert metrics["error_rate"] == pytest.approx(0.0)


def test_parse_metrics_file_multiple_entries_last_wins(tmp_path):
    """
    GAP-08 | _parse_metrics_file: Multiple entries for same metric → last-wins.
    """
    out = tmp_path / "metrics.json"
    lines = [
        json.dumps({"metric": "http_req_duration", "data": {"value": {"p(95)": 100.0, "p(99)": 200.0}}}),
        json.dumps({"metric": "http_req_duration", "data": {"value": {"p(95)": 350.0, "p(99)": 600.0}}}),
    ]
    out.write_text("\n".join(lines))
    driver = _make_driver()
    metrics = driver._parse_metrics_file(out)
    assert metrics["p95_ms"] == pytest.approx(350.0)


def test_parse_metrics_file_missing_p99_key_defaults_to_zero(tmp_path):
    """
    GAP-09 | _parse_metrics_file: Missing "p(99)" key in value dict → p99 = 0.0.
    """
    out = tmp_path / "metrics.json"
    out.write_text(json.dumps({"metric": "http_req_duration", "data": {"value": {"p(95)": 180.0}}}) + "\n")
    driver = _make_driver()
    metrics = driver._parse_metrics_file(out)
    assert metrics["p99_ms"] == pytest.approx(0.0)


def test_check_thresholds_raises_on_p99_breach_alone():
    """
    GAP-10 | _check_thresholds: P99 breach alone raises ThresholdExceededError.
    """
    thresholds = FlightThresholds(p95_ms=200, p99_ms=500, error_rate_percent=1.0)
    driver = _make_driver(thresholds=thresholds)
    with pytest.raises(ThresholdExceededError) as exc_info:
        driver._check_thresholds({"p95_ms": 150.0, "p99_ms": 750.0, "error_rate": 0.5})
    assert "P99" in str(exc_info.value)


def test_check_thresholds_context_contains_thresholds_dict():
    """
    GAP-11 | _check_thresholds: context["thresholds"] is populated.
    """
    thresholds = FlightThresholds(p95_ms=200, p99_ms=500, error_rate_percent=1.0)
    driver = _make_driver(thresholds=thresholds)
    with pytest.raises(ThresholdExceededError) as exc_info:
        driver._check_thresholds({"p95_ms": 999.0, "p99_ms": 100.0, "error_rate": 0.1})
    assert "thresholds" in exc_info.value.context


def test_check_thresholds_context_contains_violations_list():
    """
    GAP-12 | _check_thresholds: context["violations"] is a non-empty list.
    """
    thresholds = FlightThresholds(p95_ms=200, p99_ms=500, error_rate_percent=1.0)
    driver = _make_driver(thresholds=thresholds)
    with pytest.raises(ThresholdExceededError) as exc_info:
        driver._check_thresholds({"p95_ms": 999.0, "p99_ms": 100.0, "error_rate": 0.1})
    assert isinstance(exc_info.value.context["violations"], list)


def test_map_exit_code_generic_includes_stderr_in_context():
    """
    GAP-13 | _map_exit_code: Generic case — context dict includes stderr key.
    """
    with pytest.raises(HamiltonAlarm) as exc_info:
        K6Driver._map_exit_code(1, "some k6 error message")
    assert exc_info.value.context["stderr"] == "some k6 error message"


def test_map_exit_code_generic_includes_stderr_in_exception_message():
    """
    GAP-14 | _map_exit_code: Generic case — stderr in exception message.
    """
    stderr_text = "FATAL: script execution timeout"
    with pytest.raises(HamiltonAlarm) as exc_info:
        K6Driver._map_exit_code(1, stderr_text)
    assert stderr_text.strip() in str(exc_info.value)


def test_map_exit_code_zero_does_not_raise():
    """
    GAP-15 | _map_exit_code: Exit code 0 — must NOT raise HamiltonAlarm.
    """
    assert K6Driver._map_exit_code(0, "") is None


@pytest.mark.asyncio
async def test_check_health_whitespace_only_stdout_returns_unknown():
    """
    GAP-16 | check_health: Whitespace-only stdout → version_line="unknown".
    """
    driver = _make_driver()
    with patch("drivers.k6_driver.shutil.which", return_value="/usr/local/bin/k6"):
        driver._run_subprocess_async = AsyncMock(return_value=("   \n   ", "", 0))
        result = await driver.check_health()
    assert result.output["version"] == "unknown"


@pytest.mark.asyncio
async def test_check_health_version_failure_context_exit_code_is_correct():
    """
    GAP-17 | check_health: Version failure — context["exit_code"] carries return code.
    """
    driver = _make_driver()
    with patch("drivers.k6_driver.shutil.which", return_value="/usr/local/bin/k6"):
        driver._run_subprocess_async = AsyncMock(return_value=("", "unexpected flag", 2))
        with pytest.raises(EnvError) as exc_info:
            await driver.check_health()
    assert exc_info.value.context["exit_code"] == 2


@pytest.mark.asyncio
async def test_check_health_version_failure_stderr_in_error_message():
    """
    GAP-18 | check_health: Version failure — stderr content appears in error message.
    """
    driver = _make_driver()
    with patch("drivers.k6_driver.shutil.which", return_value="/usr/local/bin/k6"):
        driver._run_subprocess_async = AsyncMock(return_value=("", "k6: command not recognized", 1))
        with pytest.raises(EnvError) as exc_info:
            await driver.check_health()
    assert "k6: command not recognized" in str(exc_info.value)


@pytest.mark.asyncio
async def test_run_raises_hamilton_alarm_when_exit_nonzero_and_metrics_within_thresholds(tmp_path):
    """
    GAP-19 | run(): Non-zero exit + valid metrics → HamiltonAlarm.
    """
    driver = _make_driver()
    async def fake_run(cmd, env=None):
        for i, arg in enumerate(cmd):
            if arg == "--out":
                json_path = Path(cmd[i + 1].replace("json=", ""))
                _write_k6_json(json_path, p95=100.0, p99=200.0, error_rate_fraction=0.001)
                break
        return ("", "script error", 1)
    driver._run_subprocess_async = fake_run
    with pytest.raises(HamiltonAlarm):
        await driver.run()


@pytest.mark.asyncio
async def test_run_raises_hamilton_alarm_not_threshold_error_when_both_conditions_met(tmp_path):
    """
    GAP-20 | run(): Exit code 0 ordering — HamiltonAlarm wins.
    """
    driver = _make_driver()
    async def fake_run(cmd, env=None):
        for i, arg in enumerate(cmd):
            if arg == "--out":
                json_path = Path(cmd[i + 1].replace("json=", ""))
                _write_k6_json(json_path, p95=999.0, p99=1500.0, error_rate_fraction=0.10)
                break
        return ("", "Killed", 137)
    driver._run_subprocess_async = fake_run
    with pytest.raises(HamiltonAlarm) as exc_info:
        await driver.run()
    # Ensure it's NOT ThresholdExceededError
    assert not isinstance(exc_info.value, ThresholdExceededError)


@pytest.mark.asyncio
async def test_run_success_result_contains_all_metric_keys(tmp_path):
    """
    GAP-21 | run(): Success result includes ALL metric keys.
    """
    driver = _make_driver()
    async def fake_run(cmd, env=None):
        for i, arg in enumerate(cmd):
            if arg == "--out":
                json_path = Path(cmd[i + 1].replace("json=", ""))
                _write_k6_json(json_path, p95=150.0, p99=300.0, error_rate_fraction=0.002)
                break
        return ("", "", 0)
    driver._run_subprocess_async = fake_run
    result = await driver.run()
    assert "p95_ms" in result.output
    assert "p99_ms" in result.output
    assert "error_rate" in result.output


def test_build_command_script_path_is_last_element(tmp_path):
    """
    GAP-22 | _build_command: script_path is the last element.
    """
    driver = K6Driver(script_path="/scripts/load.js")
    cmd = driver._build_command(tmp_path / "out.json")
    assert cmd[-1] == str(driver.script_path)


@pytest.mark.asyncio
async def test_run_subprocess_passes_restricted_env_with_target():
    """
    GAP-23 | _run_subprocess_async: passes env={"TARGET": self.target} to asyncio.create_subprocess_exec.
    """
    driver = _make_driver(target="http://staging.example.com")
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0
        mock_exec.return_value = mock_proc
        
        await driver._run_subprocess_async(["k6", "version"], env={"TARGET": driver.target})

    call_kwargs = mock_exec.call_args.kwargs
    assert call_kwargs["env"] == {"TARGET": "http://staging.example.com"}
