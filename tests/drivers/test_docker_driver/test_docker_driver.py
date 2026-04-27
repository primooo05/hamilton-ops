"""
Contract tests for drivers/docker_driver.py

Strategy: Test the translation logic (command construction, exit-code
mapping, rootless verification) without running Docker. The
``_run_subprocess`` method is replaced at the instance level.

Test categories:
  1. Command construction — core flags
  2. Exit-code → exception mapping (1, 127, 137)
  3. Health check — rootless mode enforcement
  4. Integration: run() with mocked subprocess
"""
import logging
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.exceptions import BuildError, EnvError
from drivers.docker_driver import DockerDriver

def _completed(returncode=0, stdout="", stderr="") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _make_driver(stage="/tmp/stage", tag="myapp:latest", **kwargs) -> DockerDriver:
    return DockerDriver(stage_path=stage, image_tag=tag, **kwargs)


def _docker_info_json(rootless: bool = True) -> str:
    """Return a fake ``docker info`` JSON response."""
    options = ["name=rootless"] if rootless else ["name=apparmor"]
    return json.dumps({"SecurityOptions": options, "ServerVersion": "24.0.0"})


def test_build_command_starts_with_docker_build():
    """
    Contract: The assembled command list must begin with ['docker', 'build'].
    """
    driver = _make_driver()
    cmd = driver._build_command()
    assert cmd[:2] == ["docker", "build"]


def test_build_command_includes_image_tag():
    """
    Contract: ``--tag <image_tag>`` must be present in the command.
    """
    driver = _make_driver(tag="myapp:sha256-abc123")
    cmd = driver._build_command()
    assert "--tag" in cmd
    assert "myapp:sha256-abc123" in cmd


def test_build_command_includes_no_cache_by_default():
    """
    Contract: ``--no-cache`` must be set by default to prevent BuildKit
    cache poisoning attacks.
    """
    driver = _make_driver()
    cmd = driver._build_command()
    assert "--no-cache" in cmd


def test_build_command_omits_no_cache_when_disabled():
    """
    Contract: Operators can opt out of --no-cache for development builds.
    The flag must be absent when no_cache=False.
    """
    driver = _make_driver(no_cache=False)
    cmd = driver._build_command()
    assert "--no-cache" not in cmd


def test_build_command_context_is_staging_path():
    """
    Contract: The build context (last CLI argument) must point to the
    staging directory — never the live source tree.
    """
    driver = DockerDriver(stage_path="/staging/my project", image_tag="app:v1")
    cmd = driver._build_command()
    # Last element of the docker build command is the build context
    assert str(driver.stage_path) == cmd[-1]


def test_build_command_path_with_spaces_is_single_element(tmp_path):
    """
    Contract: A staging path with spaces must appear as a single list element,
    not split across two elements. OS-level quoting via list (not shell=True).
    """
    spaced = tmp_path / "my staging dir"
    spaced.mkdir()
    driver = DockerDriver(stage_path=str(spaced), image_tag="app:v1")
    cmd = driver._build_command()

    # The full path must be one element in the list. If it were split by the 
    # shell, "my", "staging", and "dir" would appear as separate elements.
    full_path = str(driver.stage_path)
    assert full_path in cmd
    assert "my" not in cmd
    assert "staging" not in cmd


def test_map_exit_code_127_raises_env_error():
    """
    Contract: Exit 127 (binary not found) must raise EnvError — this is a
    pre-flight environment failure, not a build logic failure.
    """
    with pytest.raises(EnvError) as exc_info:
        DockerDriver._map_exit_code(127, "")

    assert exc_info.value.context["exit_code"] == 127


def test_map_exit_code_137_raises_build_error_with_oom_context():
    """
    Contract: Exit 137 (OOM-killed) must raise BuildError with oom=True
    in the context dict so the Supervisor can emit a specific log message.
    """
    with pytest.raises(BuildError) as exc_info:
        DockerDriver._map_exit_code(137, "Killed")

    assert exc_info.value.context["exit_code"] == 137
    assert exc_info.value.context.get("oom") is True


def test_map_exit_code_1_raises_generic_build_error():
    """
    Contract: A generic build failure (exit 1) must raise BuildError
    containing the stderr output for the operator's inspection.
    """
    with pytest.raises(BuildError) as exc_info:
        DockerDriver._map_exit_code(1, "COPY failed: file not found")

    assert exc_info.value.context["exit_code"] == 1
    assert "COPY failed" in exc_info.value.context["stderr"]


def test_check_health_raises_env_error_when_docker_missing():
    """
    Contract: check_health() must raise EnvError when the docker binary
    is not on PATH — before attempting any daemon communication.
    """
    driver = _make_driver()
    with patch("drivers.docker_driver.shutil.which", return_value=None):
        with pytest.raises(EnvError) as exc_info:
            driver.check_health()

    assert exc_info.value.context["tool"] == "docker"


def test_check_health_raises_env_error_when_daemon_unreachable():
    """
    Contract: If ``docker info`` returns non-zero, the daemon is down.
    check_health() must raise EnvError with a meaningful message.
    """
    driver = _make_driver()
    with patch("drivers.docker_driver.shutil.which", return_value="/usr/bin/docker"):
        driver._run_subprocess = MagicMock(
            return_value=_completed(returncode=1, stderr="Cannot connect to daemon")
        )
        with pytest.raises(EnvError) as exc_info:
            driver.check_health()

    assert "not reachable" in str(exc_info.value).lower()
    assert exc_info.value.context["tool"] == "docker"
    assert exc_info.value.context["exit_code"] == 1


def test_check_health_raises_env_error_when_not_rootless():
    """
    Contract: Hamilton-Ops requires rootless Docker (README security requirement).
    check_health() must raise EnvError when the daemon is running as root.
    Only enforced on non-Windows hosts — Docker Desktop on Windows never
    reports "rootless" in SecurityOptions even when backed by WSL2.
    """
    driver = _make_driver()

    def fake_run(cmd):
        if "info" in cmd:
            return _completed(returncode=0, stdout=_docker_info_json(rootless=False))
        return _completed(returncode=0, stdout="24.0.0")

    with patch("drivers.docker_driver.shutil.which", return_value="/usr/bin/docker"):
        with patch("drivers.docker_driver.platform.system", return_value="Linux"):
            driver._run_subprocess = fake_run
            with pytest.raises(EnvError) as exc_info:
                driver.check_health()

    assert "rootless" in str(exc_info.value).lower()
    assert "name=apparmor" in exc_info.value.context["security_options"]


def test_check_health_skips_rootless_on_windows():
    """
    Contract: On Windows, Docker Desktop uses WSL2 as its backend and does NOT
    report "rootless" in SecurityOptions on the Windows-side socket — even though
    the underlying engine is secure. check_health() must pass without raising
    EnvError when platform.system() == "Windows".
    """
    driver = _make_driver()

    def fake_run(cmd):
        if "info" in cmd:
            # Windows Docker Desktop — no "rootless" in SecurityOptions
            return _completed(returncode=0, stdout=_docker_info_json(rootless=False))
        return _completed(returncode=0, stdout="24.0.0")

    with patch("drivers.docker_driver.shutil.which", return_value="/usr/bin/docker"):
        with patch("drivers.docker_driver.platform.system", return_value="Windows"):
            driver._run_subprocess = fake_run
            # Must NOT raise — the check is intentionally skipped on Windows
            result = driver.check_health()

    assert result.success is True


def test_check_health_passes_when_rootless():
    """
    Contract: check_health() must return DriverResult(success=True) when
    Docker is installed, the daemon is up, and running in rootless mode.
    """
    driver = _make_driver()

    def fake_run(cmd):
        if "info" in cmd:
            return _completed(returncode=0, stdout=_docker_info_json(rootless=True))
        return _completed(returncode=0, stdout="24.0.0")

    with patch("drivers.docker_driver.shutil.which", return_value="/usr/bin/docker"):
        driver._run_subprocess = fake_run
        result = driver.check_health()

    assert result.success is True
    assert "version" in result.output
    

def test_run_returns_driver_result_on_success():
    """
    Contract: run() must return DriverResult(success=True) with image_tag
    in the output when docker build exits 0.
    """
    driver = _make_driver(tag="myapp:v1.0.0")
    driver._run_subprocess = MagicMock(
        return_value=_completed(returncode=0, stdout="Successfully built abc123\n")
    )
    result = driver.run()

    assert result.success is True
    assert result.output["image_tag"] == "myapp:v1.0.0"


def test_run_raises_build_error_on_nonzero_exit():
    """
    Contract: run() must raise BuildError (not return a failed DriverResult)
    when docker build exits non-zero — the P3 stream is aborted.
    """
    driver = _make_driver()
    driver._run_subprocess = MagicMock(
        return_value=_completed(returncode=1, stderr="COPY failed: no such file")
    )
    with pytest.raises(BuildError):
        driver.run()


def test_run_raises_env_error_on_exit_127():
    """
    Contract: run() must raise EnvError (not BuildError) specifically for
    exit 127 — signalling the environment is broken, not the Dockerfile.
    """
    driver = _make_driver()
    driver._run_subprocess = MagicMock(
        return_value=_completed(returncode=127, stderr="docker: command not found")
    )
    with pytest.raises(EnvError):
        driver.run()


def test_run_raises_build_error_with_oom_context_on_exit_137():
    """
    Contract: run() must raise a BuildError with oom=True in its context
    when the process is OOM-killed, so the Supervisor logs the right cause.
    """
    driver = _make_driver()
    driver._run_subprocess = MagicMock(
        return_value=_completed(returncode=137, stderr="Killed")
    )
    with pytest.raises(BuildError) as exc_info:
        driver.run()

    assert exc_info.value.context.get("oom") is True

def test_build_command_includes_file_flag_and_custom_path(tmp_path):
    """
    Contract: _build_command() must include --file and handle custom paths.
    """
    custom_df = tmp_path / "CustomDockerfile"
    driver = _make_driver(stage=str(tmp_path), dockerfile=str(custom_df))
    cmd = driver._build_command()

    assert "--file" in cmd
    idx = cmd.index("--file")
    assert cmd[idx + 1] == str(custom_df.resolve())


def test_build_command_flag_ordering():
    """
    Contract: Verify the exact ordering of flags in _build_command().
    """
    driver = _make_driver(no_cache=True)
    cmd = driver._build_command()
    # Expected: docker, build, --file, <file>, --tag, <tag>, --no-cache, <context>
    assert cmd[0] == "docker"
    assert cmd[1] == "build"
    assert cmd[2] == "--file"
    assert cmd[4] == "--tag"
    assert "--no-cache" in cmd
    assert cmd[-1] == str(driver.stage_path)


def test_run_logs_unredacted_command(caplog):
    """
    Contract: DockerDriver logs the unredacted command. This is an intentional
    design decision for the base driver as it does not handle secrets (unlike
    ConstructionDriver). This test pins that behavior to prevent accidental
    leaks if secret support is added later.
    """
    import logging
    driver = _make_driver(tag="myapp:v1")
    driver._run_subprocess = MagicMock(return_value=_completed(returncode=0))

    with caplog.at_level(logging.INFO, logger="hamilton.drivers.docker"):
        driver.run()

    # Verify the log message contains the unredacted image tag
    assert any("myapp:v1" in record.message for record in caplog.records)
    assert any("Launching build stream" in record.message for record in caplog.records)
