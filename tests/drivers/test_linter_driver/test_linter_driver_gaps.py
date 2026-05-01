"""
Gap tests for drivers/linter_driver.py

This file targets behavioral gaps, edge cases, and bug regressions identified
in the initial test_linter_driver.py review. It is intentionally separate from
the core contract tests.

Strategy:
  - No real linter process is spawned.
  - _run_subprocess_async is patched at the instance level.
  - Each test has ONE clear assertion focus.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from core.exceptions import EnvError, QualityViolation
from drivers.linter_driver import LinterDriver, _DEFAULT_LINTER_CMD


def _make_driver(stage="/tmp/stage", tool_cmd=None) -> LinterDriver:
    return LinterDriver(stage_path=stage, tool_cmd=tool_cmd)


def test_init_resolves_relative_stage_path_to_absolute():
    """
    GAP-B1 | __init__: Relative stage_path resolved to absolute.
    """
    relative = "staging/project"
    driver = LinterDriver(stage_path=relative)
    assert driver.stage_path.is_absolute()
    assert driver.stage_path == Path(relative).resolve()


def test_init_empty_tool_cmd_falls_back_to_default():
    """
    GAP-B2 | __init__: tool_cmd=[] falls back to default.
    """
    driver = LinterDriver(stage_path="/tmp/stage", tool_cmd=[])
    assert driver.tool_cmd == ["flake8"]


def test_init_empty_tool_cmd_command_starts_with_flake8():
    """
    GAP-B2 | _build_command starts with flake8.
    """
    driver = LinterDriver(stage_path="/tmp/stage", tool_cmd=[])
    cmd = driver._build_command()
    assert cmd[0] == "flake8"


def test_init_default_tool_cmd_is_a_defensive_copy():
    """
    GAP-B3 | __init__: Mutating driver.tool_cmd does not affect _DEFAULT_LINTER_CMD.
    """
    original = list(_DEFAULT_LINTER_CMD)
    driver = _make_driver()
    driver.tool_cmd.append("--max-line-length=120")
    assert list(_DEFAULT_LINTER_CMD) == original


def test_init_explicit_tool_cmd_is_stored_as_copy():
    """
    GAP-B3 | __init__: Mutating caller's list does not affect driver.
    """
    original = ["ruff", "check"]
    driver = LinterDriver(stage_path="/tmp/stage", tool_cmd=original)
    original.append("--extra-flag")
    assert "--extra-flag" not in driver.tool_cmd


def test_map_exit_code_zero_does_not_raise():
    """
    GAP-B4 | _map_exit_code(0) returns None.
    """
    driver = _make_driver()
    assert driver._map_exit_code(0, "", "") is None


def test_map_exit_code_127_includes_stderr_in_env_error_context():
    """
    GAP-B5 | _map_exit_code(127) includes stderr.
    """
    driver = _make_driver()
    with pytest.raises(EnvError) as exc_info:
        driver._map_exit_code(127, "", "flake8: command not found")
    assert exc_info.value.context["stderr"] == "flake8: command not found"


def test_map_exit_code_1_includes_stderr_in_quality_violation_context():
    """
    GAP-B5 | _map_exit_code(1) includes stderr.
    """
    driver = _make_driver()
    stderr_text = "SyntaxError: Unexpected token"
    with pytest.raises(QualityViolation) as exc_info:
        driver._map_exit_code(1, "app.py:1:1: E302 error\n", stderr_text)
    assert exc_info.value.context["stderr"] == stderr_text


@pytest.mark.asyncio
async def test_check_health_raises_env_error_when_version_returns_nonzero():
    """
    GAP-B6 | check_health(): Non-zero --version → EnvError.
    """
    driver = _make_driver()
    with patch("drivers.linter_driver.shutil.which", return_value="/usr/bin/flake8"):
        driver._run_subprocess_async = AsyncMock(return_value=("", "illegal option", 1))
        with pytest.raises(EnvError) as exc_info:
            await driver.check_health()
    assert exc_info.value.context["exit_code"] == 1


@pytest.mark.asyncio
async def test_check_health_version_failure_stderr_in_error_message():
    """
    GAP-B6 | check_health(): stderr in exception message.
    """
    driver = _make_driver()
    with patch("drivers.linter_driver.shutil.which", return_value="/usr/bin/flake8"):
        driver._run_subprocess_async = AsyncMock(return_value=("", "No module named flake8", 1))
        with pytest.raises(EnvError) as exc_info:
            await driver.check_health()
    assert "No module named flake8" in str(exc_info.value)


@pytest.mark.asyncio
async def test_check_health_whitespace_only_version_stdout_returns_unknown():
    """
    Bug 1 | check_health(): Whitespace-only stdout → version="unknown".
    """
    driver = _make_driver()
    with patch("drivers.linter_driver.shutil.which", return_value="/usr/bin/flake8"):
        driver._run_subprocess_async = AsyncMock(return_value=("   \n   ", "", 0))
        result = await driver.check_health()
    assert result.output["version"] == "unknown"


@pytest.mark.asyncio
async def test_check_health_empty_string_version_stdout_returns_unknown():
    """
    Bug 1 | check_health(): stdout="" → version="unknown".
    """
    driver = _make_driver()
    with patch("drivers.linter_driver.shutil.which", return_value="/usr/bin/flake8"):
        driver._run_subprocess_async = AsyncMock(return_value=("", "", 0))
        result = await driver.check_health()
    assert result.output["version"] == "unknown"


@pytest.mark.asyncio
async def test_check_health_multiline_version_uses_first_line_only():
    """
    Bug 1 | check_health(): Multi-line stdout → first line only.
    """
    driver = _make_driver()
    with patch("drivers.linter_driver.shutil.which", return_value="/usr/bin/flake8"):
        stdout = "6.0.0 (mccabe: 0.7.0)\nusing CPython 3.11\n"
        driver._run_subprocess_async = AsyncMock(return_value=(stdout, "", 0))
        result = await driver.check_health()
    assert result.output["version"] == "6.0.0 (mccabe: 0.7.0)"


def test_map_exit_code_1_context_includes_exit_code_key():
    """
    Section C | _map_exit_code(1): exit_code in context.
    """
    driver = _make_driver()
    with pytest.raises(QualityViolation) as exc_info:
        driver._map_exit_code(1, "app.py:1:1: E302 error\n", "")
    assert exc_info.value.context["exit_code"] == 1


def test_map_exit_code_1_context_includes_tool_key():
    """
    Section C | _map_exit_code(1): tool key in context.
    """
    driver = _make_driver(tool_cmd=["ruff", "check"])
    with pytest.raises(QualityViolation) as exc_info:
        driver._map_exit_code(1, "app.py:1:1: E302 error\n", "")
    assert exc_info.value.context["tool"] == "ruff"


def test_map_exit_code_1_context_includes_output_key():
    """
    Section C | _map_exit_code(1): output key in context.
    """
    stdout = "app.py:1:1: E302 error\n"
    driver = _make_driver()
    with pytest.raises(QualityViolation) as exc_info:
        driver._map_exit_code(1, stdout, "")
    assert exc_info.value.context["output"] == stdout


@pytest.mark.asyncio
async def test_check_health_version_string_value_is_first_line():
    """
    Section C | check_health(): version value correct.
    """
    driver = _make_driver()
    with patch("drivers.linter_driver.shutil.which", return_value="/usr/bin/flake8"):
        driver._run_subprocess_async = AsyncMock(return_value=("6.0.0 (mccabe: 0.7.0)\n", "", 0))
        result = await driver.check_health()
    assert result.output["version"] == "6.0.0 (mccabe: 0.7.0)"


@pytest.mark.asyncio
async def test_run_success_output_includes_stdout_key():
    """
    Section C | run(): stdout in result.output.
    """
    stdout = "All checks passed.\n"
    driver = _make_driver()
    driver._run_subprocess_async = AsyncMock(return_value=(stdout, "", 0))
    result = await driver.run()
    assert result.output["stdout"] == stdout


@pytest.mark.asyncio
async def test_run_quality_violation_context_is_complete():
    """
    Section C | run(): QualityViolation context complete.
    """
    stdout = "app.py:1:1: E302 error\n"
    driver = _make_driver()
    driver._run_subprocess_async = AsyncMock(return_value=(stdout, "", 1))
    with pytest.raises(QualityViolation) as exc_info:
        await driver.run()
    ctx = exc_info.value.context
    assert ctx["violations"] == 1
    assert ctx["tool"] == "flake8"
    assert "stderr" in ctx
