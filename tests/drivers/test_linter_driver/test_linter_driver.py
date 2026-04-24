"""
Contract tests for drivers/linter_driver.py

Strategy: Test the translation logic (command construction, exit-code
mapping, violation counting) without running a real linter.

Test categories:
  1. Command construction & path sanitization
  2. Exit-code → exception mapping
  3. Violation counting from stdout
  4. Health check — binary detection
  5. Integration: run() with mocked subprocess
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.exceptions import EnvError, QualityViolation
from drivers.linter_driver import LinterDriver


def _completed(returncode=0, stdout="", stderr="") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _make_driver(stage="/tmp/stage", tool_cmd=None) -> LinterDriver:
    return LinterDriver(stage_path=stage, tool_cmd=tool_cmd)

def test_build_command_appends_stage_path():
    """
    Contract: _build_command must append the staging path as the final
    element so the linter scans the immutable snapshot, not the live tree.
    """
    driver = _make_driver(stage="/staging/project")
    cmd = driver._build_command()
    assert str(driver.stage_path) == cmd[-1]


def test_build_command_uses_configured_tool():
    """
    Contract: The first element of the command must be the configured
    linter binary — enabling tool-agnostic behaviour (flake8, ruff, eslint).
    """
    driver = _make_driver(tool_cmd=["ruff", "check"])
    cmd = driver._build_command()
    assert cmd[0] == "ruff"
    assert cmd[1] == "check"


def test_build_command_defaults_to_flake8():
    """
    Contract: When no tool_cmd is provided, the driver defaults to flake8.
    """
    driver = _make_driver()
    cmd = driver._build_command()
    assert cmd[0] == "flake8"


def test_build_command_path_with_spaces_is_single_element(tmp_path):
    """
    Contract: A staging path containing spaces must appear as a single
    list element — OS-level quoting via list, not shell=True.
    """
    spaced = tmp_path / "my staging area"
    spaced.mkdir()
    driver = LinterDriver(stage_path=str(spaced))
    cmd = driver._build_command()

    full_path = str(driver.stage_path)
    assert full_path in cmd

def test_map_exit_code_127_raises_env_error():
    """
    Contract: Exit 127 (linter binary not found) must raise EnvError —
    a pre-flight environment failure, not a code quality issue.
    """
    driver = _make_driver()
    with pytest.raises(EnvError) as exc_info:
        driver._map_exit_code(127, "", "")

    assert exc_info.value.context["exit_code"] == 127
    assert exc_info.value.context["tool"] == "flake8"


def test_map_exit_code_1_raises_quality_violation():
    """
    Contract: A non-zero exit from the linter must raise QualityViolation
    (P2), never a generic Python exception or BuildError.
    """
    stdout = "app.py:1:1: E302 expected 2 blank lines\napp.py:3:5: W291 trailing whitespace\n"
    driver = _make_driver()
    with pytest.raises(QualityViolation):
        driver._map_exit_code(1, stdout, "")


def test_quality_violation_is_not_a_build_error():
    """
    Contract: QualityViolation must be a distinct type from BuildError.
    The Supervisor routes P2 and P3 signals differently.
    """
    from core.exceptions import BuildError
    driver = _make_driver()
    with pytest.raises(QualityViolation) as exc_info:
        driver._map_exit_code(1, "app.py:1:1: E302 error\n", "")

    assert not isinstance(exc_info.value, BuildError)


def test_violation_count_is_correct_in_context():
    """
    Contract: The QualityViolation context must report the exact violation
    count so the Supervisor can emit a structured telemetry log.
    """
    # 3 non-empty lines = 3 violations
    stdout = (
        "app.py:1:1: E302 expected 2 blank lines\n"
        "app.py:5:1: W291 trailing whitespace\n"
        "utils.py:10:1: E501 line too long\n"
    )
    driver = _make_driver()
    with pytest.raises(QualityViolation) as exc_info:
        driver._map_exit_code(1, stdout, "")

    assert exc_info.value.context["violations"] == 3


def test_violation_count_ignores_empty_lines():
    """
    Contract: Empty lines in linter output must not inflate the violation
    count — the context dict must reflect real issues only.
    """
    stdout = "app.py:1:1: E302 error\n\n\n"
    driver = _make_driver()
    with pytest.raises(QualityViolation) as exc_info:
        driver._map_exit_code(1, stdout, "")

    assert exc_info.value.context["violations"] == 1


def test_check_health_raises_env_error_when_binary_missing():
    """
    Contract: check_health() must raise EnvError when the linter binary
    is not on PATH — enabling 'hamilton doctor' to report a missing tool.
    """
    driver = _make_driver()
    with patch("drivers.linter_driver.shutil.which", return_value=None):
        with pytest.raises(EnvError) as exc_info:
            driver.check_health()

    assert exc_info.value.context["tool"] == "flake8"


def test_check_health_returns_driver_result_on_success():
    """
    Contract: check_health() must return DriverResult(success=True) with
    a version key when the binary is found and responsive.
    """
    driver = _make_driver()
    with patch("drivers.linter_driver.shutil.which", return_value="/usr/bin/flake8"):
        driver._run_subprocess = MagicMock(
            return_value=_completed(returncode=0, stdout="6.0.0 (mccabe: 0.7.0)\n")
        )
        result = driver.check_health()

    assert result.success is True
    assert "version" in result.output


def test_check_health_uses_configured_binary():
    """
    Contract: check_health() must check for the configured binary name,
    not hardcoded 'flake8' — supporting any linter tool.
    """
    driver = _make_driver(tool_cmd=["ruff"])
    with patch("drivers.linter_driver.shutil.which", return_value=None) as mock_which:
        try:
            driver.check_health()
        except EnvError as exc:
            assert exc.context["tool"] == "ruff"
        mock_which.assert_called_with("ruff")



def test_run_returns_driver_result_on_clean_code():
    """
    Contract: run() must return DriverResult(success=True) when the linter
    exits 0 — indicating the staging area has no quality violations.
    """
    driver = _make_driver()
    driver._run_subprocess = MagicMock(return_value=_completed(returncode=0, stdout=""))
    result = driver.run()

    assert result.success is True
    assert result.output["violations"] == 0


def test_run_raises_quality_violation_on_linter_failure():
    """
    Contract: run() must raise QualityViolation (not return a failed result)
    when the linter reports issues — the P2 stream signals the Supervisor.
    """
    stdout = "app.py:1:1: E302 missing blank lines\n"
    driver = _make_driver()
    driver._run_subprocess = MagicMock(return_value=_completed(returncode=1, stdout=stdout))

    with pytest.raises(QualityViolation):
        driver.run()


def test_run_raises_env_error_on_exit_127():
    """
    Contract: run() must raise EnvError (not QualityViolation) on exit 127,
    clearly distinguishing an environment failure from a code quality issue.
    """
    driver = _make_driver()
    driver._run_subprocess = MagicMock(return_value=_completed(returncode=127, stderr="not found"))

    with pytest.raises(EnvError):
        driver.run()


@pytest.mark.parametrize("tool_cmd", [
    ["flake8"],
    ["ruff", "check"],
    ["eslint", "--ext", ".js"],
])
def test_run_works_with_different_linter_tools(tool_cmd):
    """
    Contract: The driver must be tool-agnostic — any configured linter command
    must produce a valid DriverResult on success without code changes.
    """
    driver = LinterDriver(stage_path="/tmp/stage", tool_cmd=tool_cmd)
    driver._run_subprocess = MagicMock(return_value=_completed(returncode=0, stdout=""))
    result = driver.run()

    assert result.success is True
