"""
Gap tests for drivers/docker_driver.py

This file targets behavioral gaps, edge cases, and graceful degradation 
scenarios identified in the docker driver. It is intentionally separate 
from the core contract tests (test_docker_driver.py).

Strategy:
  - Test graceful degradation in health checks (JSON failure, version fallback).
  - Test edge cases in exit code mapping (negative signals, null stderr).
  - Test telemetry completeness (stdout inclusion).
  - Test constructor path resolution.
  - Test internal implementation contracts (exact command formatting).
"""

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.exceptions import BuildError, EnvError
from drivers.docker_driver import DockerDriver


def _completed(returncode=0, stdout="", stderr="") -> subprocess.CompletedProcess:
    """Build a fake CompletedProcess for stubs."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _make_driver(stage="/tmp/stage", tag="myapp:latest", **kwargs) -> DockerDriver:
    """Return a DockerDriver with a fake stage path."""
    return DockerDriver(stage_path=stage, image_tag=tag, **kwargs)


def _docker_info_json(rootless: bool = True) -> str:
    """Return a fake ``docker info`` JSON response."""
    options = ["name=rootless"] if rootless else ["name=apparmor"]
    return json.dumps({"SecurityOptions": options, "ServerVersion": "24.0.0"})



def test_check_health_logs_warning_and_skips_rootless_on_json_failure(caplog):
    """
    GAP | check_health(): Degradation on malformed 'docker info' JSON.

    Contract: If ``docker info`` returns invalid JSON, check_health() must
    log a warning and skip the rootless check instead of failing.
    """
    driver = _make_driver()

    def fake_run(cmd):
        if "info" in cmd:
            return _completed(returncode=0, stdout="INVALID JSON")
        return _completed(returncode=0, stdout="24.0.0")

    with patch("drivers.docker_driver.shutil.which", return_value="/usr/bin/docker"):
        driver._run_subprocess = fake_run
        with caplog.at_level(logging.WARNING, logger="hamilton.drivers.docker"):
            result = driver.check_health()

    assert result.success is True
    assert "could not parse" in caplog.text.lower()


def test_check_health_uses_unknown_version_when_version_cmd_fails():
    """
    GAP | check_health(): Degradation when 'docker version' fails.

    Contract: If ``docker version`` fails, check_health() should report
    the version as "unknown" rather than raising EnvError.
    """
    driver = _make_driver()

    def fake_run(cmd):
        if "info" in cmd:
            return _completed(returncode=0, stdout=_docker_info_json(rootless=True))
        return _completed(returncode=1, stderr="version failed")

    with patch("drivers.docker_driver.shutil.which", return_value="/usr/bin/docker"):
        driver._run_subprocess = fake_run
        result = driver.check_health()

    assert result.success is True
    assert result.output["version"] == "unknown"


def test_check_health_raises_env_error_on_empty_security_options():
    """
    GAP | check_health(): Edge case — SecurityOptions is an empty list.

    Contract: If SecurityOptions is empty, the rootless check should fail
    with an explicit EnvError.
    """
    driver = _make_driver()

    def fake_run(cmd):
        if "info" in cmd:
            return _completed(returncode=0, stdout=json.dumps({"SecurityOptions": []}))
        return _completed(returncode=0, stdout="24.0.0")

    with patch("drivers.docker_driver.shutil.which", return_value="/usr/bin/docker"):
        driver._run_subprocess = fake_run
        with pytest.raises(EnvError) as exc_info:
            driver.check_health()

    assert "rootless" in str(exc_info.value).lower()
    assert exc_info.value.context["security_options"] == []


def test_check_health_exact_commands_passed_to_subprocess():
    """
    GAP | check_health(): Internal contract — Exact CLI arguments.

    Contract: Verify the exact commands passed to _run_subprocess in check_health().
    Ensures no breaking changes in how we query the daemon.
    """
    driver = _make_driver()
    captured_cmds = []

    def fake_run(cmd):
        captured_cmds.append(cmd)
        if "info" in cmd:
            return _completed(returncode=0, stdout=_docker_info_json(rootless=True))
        return _completed(returncode=0, stdout="24.0.0")

    with patch("drivers.docker_driver.shutil.which", return_value="/usr/bin/docker"):
        driver._run_subprocess = fake_run
        driver.check_health()

    assert captured_cmds[0] == ["docker", "info", "--format", "{{json .}}"]
    assert captured_cmds[1] == ["docker", "version", "--format", "{{.Server.Version}}"]


def test_map_exit_code_negative_raises_generic_build_error():
    """
    GAP | _map_exit_code: Signal-killed process (negative exit code).

    Contract: Negative exit codes should be mapped to a generic BuildError.
    """
    with pytest.raises(BuildError) as exc_info:
        DockerDriver._map_exit_code(-9, "")

    assert exc_info.value.context["exit_code"] == -9


def test_map_exit_code_handles_none_stderr():
    """
    GAP | _map_exit_code: Null-safety for stderr.

    Contract: _map_exit_code must not crash if stderr is None.
    """
    with pytest.raises(BuildError) as exc_info:
        DockerDriver._map_exit_code(1, None)

    assert "No error output captured" in str(exc_info.value)
    assert exc_info.value.context["stderr"] == ""


def test_map_exit_code_precondition_asserts_on_success_code():
    """
    GAP | _map_exit_code: Precondition guard.

    Contract: _map_exit_code must NOT be called for exit code 0; it should 
    assert if it is.
    """
    with pytest.raises(AssertionError) as exc_info:
        DockerDriver._map_exit_code(0, "")

    assert "should never be called for a successful exit" in str(exc_info.value)


def test_run_result_includes_stdout():
    """
    GAP | run(): Telemetry completeness — stdout inclusion.

    Contract: run() result must include the full stdout from the process.
    """
    driver = _make_driver()
    stdout_content = "Build output line 1\nBuild output line 2"
    driver._run_subprocess = MagicMock(
        return_value=_completed(returncode=0, stdout=stdout_content)
    )
    result = driver.run()

    assert result.output["stdout"] == stdout_content


def test_init_resolves_default_dockerfile():
    """
    GAP | __init__: Path resolution contract for default Dockerfile.

    Contract: When dockerfile=None, it should resolve to stage_path / "Dockerfile".
    """
    stage = Path("/tmp/stage")
    driver = DockerDriver(stage_path=stage, image_tag="app:v1")
    assert driver.dockerfile == (stage / "Dockerfile").resolve()
