"""
Gap tests for drivers/linter_driver.py

This file targets behavioral gaps, edge cases, and bug regressions identified
in the initial test_linter_driver.py review. It is intentionally separate from
the core contract tests.

Strategy:
  - No real linter process is spawned.
  - _run_subprocess is patched at the instance level (same convention as
    test_linter_driver.py and test_k6_driver_gaps.py).
  - Each test has ONE clear assertion focus, labelled with the gap/bug ID from
    the review document.

Gaps covered:
  B1  — Relative stage_path resolved to absolute
  B2  — tool_cmd=[] silently falls back to _DEFAULT_LINTER_CMD
  B3  — Defensive copy: mutating driver.tool_cmd does not affect _DEFAULT_LINTER_CMD
  B4  — _map_exit_code(0) does not raise QualityViolation (regression guard)
  B5  — stderr surfaced in EnvError and QualityViolation contexts
  B6  — check_health raises EnvError when --version returns non-zero

  Bug 1 — Whitespace-only version stdout falls back to "unknown" (no IndexError)
  Bug 2 — _map_exit_code(0) returns None (regression guard, same as B4)
  Bug 3 — Non-zero --version raises EnvError (regression guard, same as B6)
  Bug 4 — stderr in QualityViolation context (regression guard, same as B5)

  Section C — Full context assertions (exit_code, tool, output, stderr keys)
             and telemetry completeness for run()
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.exceptions import EnvError, QualityViolation
from drivers.linter_driver import LinterDriver, _DEFAULT_LINTER_CMD


def _completed(returncode=0, stdout="", stderr="") -> subprocess.CompletedProcess:
    """Build a fake CompletedProcess to inject into _run_subprocess."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _make_driver(stage="/tmp/stage", tool_cmd=None) -> LinterDriver:
    return LinterDriver(stage_path=stage, tool_cmd=tool_cmd)


def test_init_resolves_relative_stage_path_to_absolute():
    """
    GAP-B1 | __init__: Relative stage_path is stored as an absolute path.

    Contract: self.stage_path must always be absolute so that the OS-level
    subprocess call is never dependent on the process working directory.
    """
    relative = "staging/project"
    driver = LinterDriver(stage_path=relative)
    assert driver.stage_path.is_absolute(), (
        "stage_path must be stored as an absolute path after Path.resolve()"
    )
    assert driver.stage_path == Path(relative).resolve()


def test_init_empty_tool_cmd_falls_back_to_default():
    """
    GAP-B2 | __init__: tool_cmd=[] is falsy → silently falls back to flake8.

    Contract: An empty list passed as tool_cmd must be treated as "not set"
    and resolved to the project default, not result in a broken empty command.
    """
    driver = LinterDriver(stage_path="/tmp/stage", tool_cmd=[])
    # [] is falsy so `tool_cmd or list(...)` must kick in
    assert driver.tool_cmd == ["flake8"]


def test_init_empty_tool_cmd_command_starts_with_flake8():
    """
    GAP-B2 (command verification) | _build_command starts with flake8 after [] fallback.

    Contract: The assembled command's first element must be the resolved default
    binary, not an empty string.
    """
    driver = LinterDriver(stage_path="/tmp/stage", tool_cmd=[])
    cmd = driver._build_command()
    assert cmd[0] == "flake8"


def test_init_default_tool_cmd_is_a_defensive_copy():
    """
    GAP-B3 | __init__: Mutating driver.tool_cmd must not affect _DEFAULT_LINTER_CMD.

    Contract: list(_DEFAULT_LINTER_CMD) creates a new list. If the driver stored
    a reference to the module constant, a caller could corrupt global state.
    """
    original = list(_DEFAULT_LINTER_CMD)
    driver = _make_driver()
    driver.tool_cmd.append("--max-line-length=120")

    # _DEFAULT_LINTER_CMD must remain unchanged
    assert list(_DEFAULT_LINTER_CMD) == original, (
        "Mutating driver.tool_cmd must not affect the module-level _DEFAULT_LINTER_CMD"
    )


def test_init_explicit_tool_cmd_is_stored_as_copy():
    """
    GAP-B3 (explicit cmd) | __init__: Mutating the list passed to tool_cmd
    must not affect driver.tool_cmd.

    Contract: The driver stores its own copy of the provided list so that
    mutations by the caller after construction don't silently alter behaviour.
    """
    original = ["ruff", "check"]
    driver = LinterDriver(stage_path="/tmp/stage", tool_cmd=original)
    original.append("--extra-flag")

    # driver's internal copy must be unaffected
    assert "--extra-flag" not in driver.tool_cmd


def test_map_exit_code_zero_does_not_raise():
    """
    GAP-B4 / Bug 2 | _map_exit_code(0): Must return None, not raise QualityViolation.

    Regression guard: Before the fix, exit code 0 fell through directly to the
    QualityViolation branch, producing a spurious P2 signal on a clean run.
    """
    driver = _make_driver()
    # Must not raise any exception
    result = driver._map_exit_code(0, "", "")
    assert result is None


def test_map_exit_code_127_includes_stderr_in_env_error_context():
    """
    GAP-B5 / Bug 4 | _map_exit_code(127): stderr key must be present in EnvError context.

    Contract: The Supervisor logs context["stderr"] for post-mortem diagnosis.
    A missing key would cause a KeyError in the Supervisor's reporting layer.
    """
    driver = _make_driver()
    with pytest.raises(EnvError) as exc_info:
        driver._map_exit_code(127, "", "flake8: command not found")

    assert "stderr" in exc_info.value.context
    assert exc_info.value.context["stderr"] == "flake8: command not found"


def test_map_exit_code_1_includes_stderr_in_quality_violation_context():
    """
    GAP-B5 / Bug 4 | _map_exit_code(1): stderr key must be present in QualityViolation context.

    Contract: Some linters (e.g. eslint) write parse errors to stderr while
    still exiting non-zero. Discarding stderr hides these diagnostics from the
    Supervisor's structured log.
    """
    driver = _make_driver()
    stderr_text = "SyntaxError: Unexpected token at line 5"

    with pytest.raises(QualityViolation) as exc_info:
        driver._map_exit_code(1, "app.py:1:1: E302 error\n", stderr_text)

    assert "stderr" in exc_info.value.context
    assert exc_info.value.context["stderr"] == stderr_text


def test_check_health_raises_env_error_when_version_returns_nonzero():
    """
    GAP-B6 / Bug 3 | check_health(): Non-zero --version exit → EnvError.

    Regression guard: Before the fix, a non-zero --version silently returned
    a DriverResult with version="unknown", masking a broken binary from the
    Supervisor's pre-flight checks.
    """
    driver = _make_driver()
    with patch("drivers.linter_driver.shutil.which", return_value="/usr/bin/flake8"):
        driver._run_subprocess = MagicMock(
            return_value=_completed(returncode=1, stderr="flake8: illegal option --version")
        )
        with pytest.raises(EnvError) as exc_info:
            driver.check_health()

    assert exc_info.value.context["tool"] == "flake8"
    assert exc_info.value.context["exit_code"] == 1


def test_check_health_version_failure_stderr_in_error_message():
    """
    GAP-B6 (message) | check_health(): --version stderr appears in the EnvError message.

    Contract: Operators reading raw logs must be able to see the linter error
    text without inspecting the context dict separately.
    """
    driver = _make_driver()
    with patch("drivers.linter_driver.shutil.which", return_value="/usr/bin/flake8"):
        driver._run_subprocess = MagicMock(
            return_value=_completed(returncode=1, stderr="No module named flake8")
        )
        with pytest.raises(EnvError) as exc_info:
            driver.check_health()

    assert "No module named flake8" in str(exc_info.value)


def test_check_health_whitespace_only_version_stdout_returns_unknown():
    """
    Bug 1 | check_health(): Whitespace-only stdout → version="unknown", no IndexError.

    Regression guard: Before the fix, stdout="  \\n  " was truthy so the
    conditional `if completed.stdout` passed, but .strip().splitlines() returned
    [], and [][0] raised IndexError. The fix degrades gracefully to "unknown".
    """
    driver = _make_driver()
    with patch("drivers.linter_driver.shutil.which", return_value="/usr/bin/flake8"):
        driver._run_subprocess = MagicMock(
            return_value=_completed(returncode=0, stdout="   \n   ")
        )
        result = driver.check_health()

    assert result.success is True
    assert result.output["version"] == "unknown"


def test_check_health_empty_string_version_stdout_returns_unknown():
    """
    Bug 1 (empty string) | check_health(): stdout="" → version="unknown".

    Contract: An empty string must degrade to "unknown" without raising.
    """
    driver = _make_driver()
    with patch("drivers.linter_driver.shutil.which", return_value="/usr/bin/flake8"):
        driver._run_subprocess = MagicMock(
            return_value=_completed(returncode=0, stdout="")
        )
        result = driver.check_health()

    assert result.output["version"] == "unknown"


def test_check_health_multiline_version_uses_first_line_only():
    """
    Bug 1 (multiline) | check_health(): Multi-line stdout → only the first line is used.

    Contract: Some linters print extra info after the version. The driver must
    return just the first line (the version identifier).
    """
    driver = _make_driver()
    with patch("drivers.linter_driver.shutil.which", return_value="/usr/bin/flake8"):
        driver._run_subprocess = MagicMock(
            return_value=_completed(
                returncode=0,
                stdout="6.0.0 (mccabe: 0.7.0, pycodestyle: 2.10.0, pyflakes: 3.0.1)\nusing CPython 3.11\n"
            )
        )
        result = driver.check_health()

    assert result.output["version"] == "6.0.0 (mccabe: 0.7.0, pycodestyle: 2.10.0, pyflakes: 3.0.1)"


def test_map_exit_code_1_context_includes_exit_code_key():
    """
    Section C | _map_exit_code(1): context["exit_code"] is present and correct.

    The existing tests only check context["violations"]. This verifies the
    exit_code key is also populated so the Supervisor can correlate signals.
    """
    driver = _make_driver()
    with pytest.raises(QualityViolation) as exc_info:
        driver._map_exit_code(1, "app.py:1:1: E302 error\n", "")

    assert exc_info.value.context["exit_code"] == 1


def test_map_exit_code_1_context_includes_tool_key():
    """
    Section C | _map_exit_code(1): context["tool"] names the configured binary.

    Contract: The Supervisor needs to know which tool produced the violations
    to emit a structured log referencing the correct linter.
    """
    driver = _make_driver(tool_cmd=["ruff", "check"])
    with pytest.raises(QualityViolation) as exc_info:
        driver._map_exit_code(1, "app.py:1:1: E302 error\n", "")

    assert exc_info.value.context["tool"] == "ruff"


def test_map_exit_code_1_context_includes_output_key():
    """
    Section C | _map_exit_code(1): context["output"] contains the raw linter stdout.

    Contract: The Supervisor stores context["output"] in the telemetry log so
    developers can read the full linter report without re-running the tool.
    """
    stdout = "app.py:1:1: E302 expected 2 blank lines\n"
    driver = _make_driver()
    with pytest.raises(QualityViolation) as exc_info:
        driver._map_exit_code(1, stdout, "")

    assert exc_info.value.context["output"] == stdout


def test_check_health_version_string_value_is_first_line():
    """
    Section C | check_health(): result.output["version"] is the actual version string.

    The existing test only checks `"version" in result.output`. This verifies
    the actual value so a regression (e.g. storing the wrong line) is caught.
    """
    driver = _make_driver()
    with patch("drivers.linter_driver.shutil.which", return_value="/usr/bin/flake8"):
        driver._run_subprocess = MagicMock(
            return_value=_completed(returncode=0, stdout="6.0.0 (mccabe: 0.7.0)\n")
        )
        result = driver.check_health()

    assert result.output["version"] == "6.0.0 (mccabe: 0.7.0)"


def test_run_success_output_includes_stdout_key():
    """
    Section C | run(): result.output["stdout"] is present and contains linter stdout.

    The existing test only checks result.output["violations"]. The source also
    sets output["stdout"] — this verifies it is correctly propagated.
    """
    expected_stdout = "All checks passed.\n"
    driver = _make_driver()
    driver._run_subprocess = MagicMock(
        return_value=_completed(returncode=0, stdout=expected_stdout)
    )
    result = driver.run()

    assert "stdout" in result.output
    assert result.output["stdout"] == expected_stdout


def test_run_quality_violation_context_is_complete():
    """
    Section C | run(): QualityViolation raised from run() carries a complete context dict.

    The existing test only asserts the exception type. This verifies the full
    context structure so the Supervisor is guaranteed a complete telemetry payload.
    """
    stdout = "app.py:1:1: E302 error\napp.py:2:1: W291 trailing whitespace\n"
    driver = _make_driver()
    driver._run_subprocess = MagicMock(
        return_value=_completed(returncode=1, stdout=stdout, stderr="")
    )

    with pytest.raises(QualityViolation) as exc_info:
        driver.run()

    ctx = exc_info.value.context
    assert ctx["violations"] == 2
    assert ctx["exit_code"] == 1
    assert ctx["tool"] == "flake8"
    assert ctx["output"] == stdout
    assert "stderr" in ctx
