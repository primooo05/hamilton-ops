"""
Gap tests for drivers/k6_driver.py

This file targets the 23 behavioral gaps identified after the initial
test_k6_driver.py review.

Gaps are numbered GAP-01 through GAP-23 to match the analysis document.

Strategy:
  - No real k6 process is spawned.
  - _run_subprocess is patched at the instance level (same convention as
    test_k6_driver.py) or replaced with a MagicMock.
  - Each test has ONE clear assertion focus; combined scenarios use
    descriptive names to explain the compound intent.
"""

import json
import subprocess
from pathlib import Path
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
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


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

    Contract: script_path must be stored as an absolute POSIX-safe path so
    the OS-level subprocess call never receives a relative path that would
    depend on the process working directory.
    """
    relative = "tests/scripts/load.js"
    driver = K6Driver(script_path=relative)
    assert driver.script_path.is_absolute(), (
        "script_path must be stored as an absolute path"
    )
    assert driver.script_path == Path(relative).resolve()


def test_init_defaults_thresholds_to_flight_thresholds():
    """
    GAP-02 | __init__: Default thresholds — thresholds=None → FlightThresholds().

    Contract: When the caller omits thresholds, the driver must use the
    project baseline (p95_ms=200, p99_ms=500, error_rate_percent=1.0).
    """
    driver = _make_driver()
    assert isinstance(driver.thresholds, FlightThresholds)
    assert driver.thresholds.p95_ms == 200
    assert driver.thresholds.p99_ms == 500
    assert driver.thresholds.error_rate_percent == 1.0


def test_init_default_target_is_localhost():
    """
    GAP-03 | __init__: Default target defaults to "http://localhost".

    Contract: The driver must default to localhost to prevent accidental DDoS
    if the caller forgets to specify a target.
    """
    driver = _make_driver()
    assert driver.target == "http://localhost"



def test_parse_metrics_file_empty_file_returns_zeroed_metrics(tmp_path):
    """
    GAP-04 | _parse_metrics_file: Empty file (0 bytes) → zeroed metrics.

    Contract: A file that exists but is empty must produce zeroed metrics —
    not raise.  The subprocess exit code surfaces the real failure.
    """
    out = tmp_path / "metrics.json"
    out.write_text("")

    driver = _make_driver()
    metrics = driver._parse_metrics_file(out)

    assert metrics == {"p95_ms": 0.0, "p99_ms": 0.0, "error_rate": 0.0}


def test_parse_metrics_file_missing_data_key_degrades_silently(tmp_path):
    """
    GAP-05 | _parse_metrics_file: Missing "data" key → silent degradation to 0.0.

    Contract: If a JSON line has a recognized metric name but no "data" key,
    the driver must not crash.  entry.get("data", {}) must absorb the absence.
    """
    out = tmp_path / "metrics.json"
    # Valid JSON but no "data" key at all
    out.write_text(json.dumps({"metric": "http_req_duration"}) + "\n")

    driver = _make_driver()
    metrics = driver._parse_metrics_file(out)

    assert metrics["p95_ms"] == pytest.approx(0.0)
    assert metrics["p99_ms"] == pytest.approx(0.0)


def test_parse_metrics_file_scalar_duration_value_is_skipped_by_isinstance_guard(tmp_path):
    """
    GAP-06 | _parse_metrics_file: Wrong value type for http_req_duration (scalar,
    not dict) → isinstance(value, dict) guard skips it, p95/p99 stay 0.

    Contract: The isinstance check is a real runtime guard, not dead code.
    If removed, this test would fail by crashing on value.get("p(95)").
    """
    out = tmp_path / "metrics.json"
    # value is a float scalar instead of a percentile dict
    out.write_text(
        json.dumps({"metric": "http_req_duration", "data": {"value": 42.0}}) + "\n"
    )

    driver = _make_driver()
    metrics = driver._parse_metrics_file(out)

    # Guard must have silently skipped — not crashed, not populated
    assert metrics["p95_ms"] == pytest.approx(0.0)
    assert metrics["p99_ms"] == pytest.approx(0.0)


def test_parse_metrics_file_dict_error_rate_value_is_skipped_by_isinstance_guard(tmp_path):
    """
    GAP-07 | _parse_metrics_file: Wrong value type for http_req_failed (dict, not
    number) → isinstance(value, (int, float)) guard skips it, error_rate stays 0.

    Contract: The isinstance check is a real runtime guard, not dead code.
    If removed, this test would fail by multiplying a dict by 100.
    """
    out = tmp_path / "metrics.json"
    # value is a dict instead of a rate fraction
    out.write_text(
        json.dumps({"metric": "http_req_failed", "data": {"value": {"rate": 0.5}}}) + "\n"
    )

    driver = _make_driver()
    metrics = driver._parse_metrics_file(out)

    assert metrics["error_rate"] == pytest.approx(0.0)


def test_parse_metrics_file_multiple_entries_last_wins(tmp_path):
    """
    GAP-08 | _parse_metrics_file: Multiple entries for same metric → last-wins.

    Contract: k6 streams metrics throughout a test run.  The final value of
    each metric must be the one used — matching k6's own summarisation semantics.
    """
    out = tmp_path / "metrics.json"
    lines = [
        json.dumps({"metric": "http_req_duration", "data": {"value": {"p(95)": 100.0, "p(99)": 200.0}}}),
        json.dumps({"metric": "http_req_duration", "data": {"value": {"p(95)": 350.0, "p(99)": 600.0}}}),
    ]
    out.write_text("\n".join(lines))

    driver = _make_driver()
    metrics = driver._parse_metrics_file(out)

    # The last entry must win
    assert metrics["p95_ms"] == pytest.approx(350.0)
    assert metrics["p99_ms"] == pytest.approx(600.0)


def test_parse_metrics_file_missing_p99_key_defaults_to_zero(tmp_path):
    """
    GAP-09 | _parse_metrics_file: Missing "p(99)" key in value dict → p99 = 0.0.

    Contract: value.get("p(99)", 0.0) must absorb a missing percentile key
    without crashing or producing an unspecified value.
    """
    out = tmp_path / "metrics.json"
    # Only p(95) is present; p(99) is absent
    out.write_text(
        json.dumps({"metric": "http_req_duration", "data": {"value": {"p(95)": 180.0}}}) + "\n"
    )

    driver = _make_driver()
    metrics = driver._parse_metrics_file(out)

    assert metrics["p95_ms"] == pytest.approx(180.0)
    assert metrics["p99_ms"] == pytest.approx(0.0)

def test_check_thresholds_raises_on_p99_breach_alone():
    """
    GAP-10 | _check_thresholds: P99 breach alone raises ThresholdExceededError.

    Contract: P99 is an independent kill-switch.  This is the only test that
    isolates P99 as the SOLE triggering violation (p95 and error_rate pass).
    """
    thresholds = FlightThresholds(p95_ms=200, p99_ms=500, error_rate_percent=1.0)
    driver = _make_driver(thresholds=thresholds)

    with pytest.raises(ThresholdExceededError) as exc_info:
        driver._check_thresholds({"p95_ms": 150.0, "p99_ms": 750.0, "error_rate": 0.5})

    assert "P99" in str(exc_info.value)
    assert "750.0" in str(exc_info.value)


def test_check_thresholds_context_contains_thresholds_dict():
    """
    GAP-11 | _check_thresholds: context["thresholds"] is populated with configured limits.

    Contract: The Supervisor needs the threshold values in the context to emit
    a structured telemetry diff (actual vs. limit).
    """
    thresholds = FlightThresholds(p95_ms=200, p99_ms=500, error_rate_percent=1.0)
    driver = _make_driver(thresholds=thresholds)

    with pytest.raises(ThresholdExceededError) as exc_info:
        driver._check_thresholds({"p95_ms": 999.0, "p99_ms": 100.0, "error_rate": 0.1})

    ctx = exc_info.value.context
    assert "thresholds" in ctx
    assert ctx["thresholds"]["p95_ms"] == 200
    assert ctx["thresholds"]["p99_ms"] == 500
    assert ctx["thresholds"]["error_rate_percent"] == 1.0


def test_check_thresholds_context_contains_violations_list():
    """
    GAP-12 | _check_thresholds: context["violations"] is a non-empty list.

    Contract: The Supervisor routes the signal based on the violations list.
    An empty list would make it impossible to determine why the alarm fired.
    """
    thresholds = FlightThresholds(p95_ms=200, p99_ms=500, error_rate_percent=1.0)
    driver = _make_driver(thresholds=thresholds)

    with pytest.raises(ThresholdExceededError) as exc_info:
        driver._check_thresholds({"p95_ms": 999.0, "p99_ms": 100.0, "error_rate": 0.1})

    ctx = exc_info.value.context
    assert "violations" in ctx
    assert isinstance(ctx["violations"], list)
    assert len(ctx["violations"]) >= 1

def test_map_exit_code_generic_includes_stderr_in_context():
    """
    GAP-13 | _map_exit_code: Generic case — context dict includes stderr key.

    Contract: The Supervisor logs context["stderr"] for post-mortem diagnosis.
    A missing key would cause a KeyError in the Supervisor's reporting layer.
    """
    with pytest.raises(HamiltonAlarm) as exc_info:
        K6Driver._map_exit_code(1, "some k6 error message")

    assert "stderr" in exc_info.value.context
    assert exc_info.value.context["stderr"] == "some k6 error message"


def test_map_exit_code_generic_includes_stderr_in_exception_message():
    """
    GAP-14 | _map_exit_code: Generic case — stderr content appears in the exception message.

    Contract: The human-readable exception message must embed the stderr text
    so operators can read it directly from logs without diving into context dicts.
    """
    stderr_text = "FATAL: script execution timeout"
    with pytest.raises(HamiltonAlarm) as exc_info:
        K6Driver._map_exit_code(1, stderr_text)

    assert stderr_text.strip() in str(exc_info.value)


def test_map_exit_code_zero_does_not_raise():
    """
    GAP-15 | _map_exit_code: Exit code 0 — must NOT raise HamiltonAlarm.

    Bug Fix Validation: Before the fix, code=0 fell through to the generic
    HamiltonAlarm branch producing "k6 exited with code 0: ..." which is
    semantically wrong.  The method must return None for code=0.
    """
    # Must not raise any exception
    result = K6Driver._map_exit_code(0, "")
    assert result is None


def test_check_health_whitespace_only_stdout_returns_unknown():
    """
    GAP-16 | check_health: Whitespace-only stdout → version_line="unknown", no IndexError.

    Bug Fix Validation: Before the fix, stdout=" \\n " passed the truthiness
    check, then .strip() produced "", then .splitlines() produced [], then
    [0] raised IndexError.  After the fix, an empty lines list falls back
    to "unknown".
    """
    driver = _make_driver()
    with patch("drivers.k6_driver.shutil.which", return_value="/usr/local/bin/k6"):
        # stdout is whitespace-only — the crash scenario
        driver._run_subprocess = MagicMock(
            return_value=_completed(returncode=0, stdout="   \n   ")
        )
        result = driver.check_health()

    assert result.success is True
    assert result.output["version"] == "unknown"


def test_check_health_version_failure_context_exit_code_is_correct():
    """
    GAP-17 | check_health: Version failure — context["exit_code"] carries the
    actual return code, not a hardcoded constant.

    Contract: The Supervisor uses context["exit_code"] to route the failure
    signal; an incorrect value would cause misrouting.
    """
    driver = _make_driver()
    with patch("drivers.k6_driver.shutil.which", return_value="/usr/local/bin/k6"):
        driver._run_subprocess = MagicMock(
            return_value=_completed(returncode=2, stderr="unexpected flag")
        )
        with pytest.raises(EnvError) as exc_info:
            driver.check_health()

    assert exc_info.value.context["exit_code"] == 2


def test_check_health_version_failure_stderr_in_error_message():
    """
    GAP-18 | check_health: Version failure — stderr content appears in error message.

    Contract: Operators reading raw logs must be able to see the k6 error
    text without having to inspect the context dict separately.
    """
    driver = _make_driver()
    with patch("drivers.k6_driver.shutil.which", return_value="/usr/local/bin/k6"):
        driver._run_subprocess = MagicMock(
            return_value=_completed(returncode=1, stderr="k6: command not recognized")
        )
        with pytest.raises(EnvError) as exc_info:
            driver.check_health()

    assert "k6: command not recognized" in str(exc_info.value)


def test_run_raises_hamilton_alarm_when_exit_nonzero_and_metrics_within_thresholds(tmp_path):
    """
    GAP-19 | run(): Non-zero exit + valid metrics within thresholds → HamiltonAlarm.

    Contract: Even when k6 produces valid, within-threshold metrics, a non-zero
    exit code must still surface as HamiltonAlarm.  The Supervisor must know
    the process crashed regardless of the metric snapshot.
    """
    driver = _make_driver(
        thresholds=FlightThresholds(p95_ms=200, p99_ms=500, error_rate_percent=1.0)
    )

    def fake_run(cmd):
        # Write valid, within-threshold metrics
        for i, arg in enumerate(cmd):
            if arg == "--out":
                json_path = Path(cmd[i + 1].replace("json=", ""))
                _write_k6_json(json_path, p95=100.0, p99=200.0, error_rate_fraction=0.001)
                break
        # But k6 crashed with exit code 1
        return _completed(returncode=1, stderr="script error")

    driver._run_subprocess = fake_run

    with pytest.raises(HamiltonAlarm):
        driver.run()


def test_run_raises_hamilton_alarm_not_threshold_error_when_both_conditions_met(tmp_path):
    """
    GAP-20 | run(): Exit code 0 ordering — HamiltonAlarm wins over ThresholdExceededError.

    Contract: When k6 exits non-zero AND metrics breach thresholds, HamiltonAlarm
    must be raised — NOT ThresholdExceededError.  This proves the exit-code check
    runs before _check_thresholds in the fixed implementation (Bug 3 fix).
    """
    driver = _make_driver(
        thresholds=FlightThresholds(p95_ms=200, p99_ms=500, error_rate_percent=1.0)
    )

    def fake_run(cmd):
        # Write BREACHING metrics
        for i, arg in enumerate(cmd):
            if arg == "--out":
                json_path = Path(cmd[i + 1].replace("json=", ""))
                _write_k6_json(json_path, p95=999.0, p99=1500.0, error_rate_fraction=0.10)
                break
        # AND k6 crashed
        return _completed(returncode=137, stderr="Killed")

    driver._run_subprocess = fake_run

    # Must raise HamiltonAlarm (crash signal), NOT ThresholdExceededError (metric signal)
    with pytest.raises(HamiltonAlarm):
        driver.run()

    # Confirm ThresholdExceededError is NOT raised instead
    # (ThresholdExceededError IS a subclass of HamiltonAlarm, so we must be explicit)
    driver._run_subprocess = fake_run
    try:
        driver.run()
    except ThresholdExceededError:
        pytest.fail(
            "ThresholdExceededError was raised — exit code check must run BEFORE "
            "_check_thresholds to prevent crash masking."
        )
    except HamiltonAlarm:
        pass  # Correct: crash signal wins


def test_run_success_result_contains_all_metric_keys(tmp_path):
    """
    GAP-21 | run(): Success result includes ALL metric keys: p95_ms, p99_ms, error_rate.

    Contract: The DriverResult output dict must contain all three keys so the
    Supervisor can log a complete telemetry snapshot without KeyError.
    The existing test only asserts p95_ms.
    """
    driver = _make_driver(
        thresholds=FlightThresholds(p95_ms=200, p99_ms=500, error_rate_percent=1.0)
    )

    def fake_run(cmd):
        for i, arg in enumerate(cmd):
            if arg == "--out":
                json_path = Path(cmd[i + 1].replace("json=", ""))
                _write_k6_json(json_path, p95=150.0, p99=300.0, error_rate_fraction=0.002)
                break
        return _completed(returncode=0)

    driver._run_subprocess = fake_run
    result = driver.run()

    assert result.success is True
    assert "p95_ms" in result.output,   "p95_ms must be in DriverResult output"
    assert "p99_ms" in result.output,   "p99_ms must be in DriverResult output"
    assert "error_rate" in result.output, "error_rate must be in DriverResult output"
    assert result.output["p99_ms"] == pytest.approx(300.0)
    assert result.output["error_rate"] == pytest.approx(0.2)


def test_build_command_script_path_is_last_element(tmp_path):
    """
    GAP-22 | _build_command: script_path is the last element in the command list.

    Contract: k6 requires the script as a positional argument AFTER all flags.
    If a flag appears after the script path, k6 treats it as another script.
    """
    driver = K6Driver(script_path="/scripts/load.js")
    cmd = driver._build_command(tmp_path / "out.json")

    assert cmd[-1] == str(driver.script_path), (
        "script_path must be the final positional argument in the k6 command"
    )


def test_run_subprocess_passes_restricted_env_with_target():
    """
    GAP-23 | _run_subprocess: passes env={"TARGET": self.target} to subprocess.run.

    Contract: The subprocess must receive ONLY TARGET in its environment
    (security: no inherited secrets from the host shell).  This verifies
    the actual subprocess.run call, not the wrapper method.
    """
    driver = _make_driver(target="http://staging.example.com")

    with patch("drivers.k6_driver.subprocess.run") as mock_run:
        mock_run.return_value = _completed(returncode=0, stdout="")
        driver._run_subprocess(["k6", "version"])

    # subprocess.run must have been called with the restricted env
    call_kwargs = mock_run.call_args.kwargs
    assert "env" in call_kwargs, "env must be passed as a keyword argument to subprocess.run"
    assert call_kwargs["env"] == {"TARGET": "http://staging.example.com"}, (
        "Only TARGET should be present in the subprocess environment"
    )
