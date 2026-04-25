"""
Gap tests for drivers/construction.py

This file targets behavioral gaps, edge cases, and safety contracts identified 
in the construction driver. It is intentionally separate from the core 
contract tests (test_construction.py).

Strategy:
  - Test surgical interrupt escalation (SIGTERM -> SIGKILL).
  - Test race conditions in process management.
  - Test regex edge cases for build-arg redaction.
  - Test state cleanup and idempotency.
  - Test graceful degradation in health checks.
"""

import json
import logging
import os
import signal
import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from core.exceptions import BuildError, EnvError
from drivers.construction import ConstructionDriver, _redact_build_args


def _make_driver(stage="/tmp/stage", tag="myapp:v1", **kwargs) -> ConstructionDriver:
    return ConstructionDriver(stage_path=stage, image_tag=tag, **kwargs)


class AsyncMockProcess:
    """
    Lightweight fake for asyncio.subprocess.Process.
    """
    def __init__(self, returncode=0, stdout_lines=None, pid=9999):
        self.returncode = returncode
        self._stdout_lines = stdout_lines or []
        self.pid = pid
        self.killed = False

        async def _async_gen():
            for line in self._stdout_lines:
                yield (line + "\n").encode()

        self.stdout = _async_gen()

    async def wait(self):
        return self.returncode

    def kill(self):
        self.killed = True


def _make_async_popen(returncode=0, stdout_lines=None, pid=9999):
    """Return an async factory that yields an AsyncMockProcess."""
    async def _factory(cmd):
        return AsyncMockProcess(returncode=returncode, stdout_lines=stdout_lines, pid=pid)
    return _factory


def _completed(returncode=0, stdout="", stderr="") -> subprocess.CompletedProcess:
    """Build a fake CompletedProcess for _run_subprocess stubs."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _docker_info_json(rootless: bool = True) -> str:
    """Return a minimal ``docker info`` JSON payload for health-check tests."""
    options = ["name=rootless"] if rootless else ["name=apparmor"]
    return json.dumps({"SecurityOptions": options, "ServerVersion": "24.0.0"})


@pytest.mark.asyncio
async def test_terminate_escalates_to_sigkill_when_sigterm_ignored():
    """
    GAP | terminate(): Escalation to SIGKILL when SIGTERM is ignored.

    Contract: If the process group does not exit within _SIGKILL_TIMEOUT
    seconds after SIGTERM, terminate() MUST escalate to SIGKILL.
    """
    import drivers.construction as construction_module

    driver = _make_driver()
    mock_proc = AsyncMockProcess(pid=1234)
    # returncode stays None — process never exits after SIGTERM
    mock_proc.returncode = None
    driver._proc = mock_proc

    mock_os = MagicMock(spec=os)
    mock_os.getpgid = MagicMock(return_value=5678)
    mock_os.killpg = MagicMock()

    with patch.object(construction_module, "os", mock_os), \
         patch("drivers.construction.time.monotonic", side_effect=[0.0, 999.0, 999.0]):
        # Stub asyncio.sleep as a no-op coroutine so the grace period passes instantly.
        async def _instant_sleep(_s):
            return
        with patch.object(construction_module.asyncio, "sleep", _instant_sleep):
            await driver.terminate()

    # SIGTERM first
    assert mock_os.killpg.call_args_list[0] == call(5678, signal.SIGTERM)
    # SIGKILL escalation (9)
    assert mock_os.killpg.call_args_list[1] == call(5678, 9)


@pytest.mark.asyncio
async def test_terminate_does_not_escalate_to_sigkill_when_process_exits_cleanly():
    """
    GAP | terminate(): No SIGKILL if process exits after SIGTERM.

    Contract: If the process exits during the polling loop, terminate() must 
    NOT send SIGKILL to avoid hitting recycled PIDs.
    """
    import drivers.construction as construction_module

    driver = _make_driver()
    mock_proc = AsyncMockProcess(pid=1234)
    mock_proc.returncode = -15  # already exited
    driver._proc = mock_proc

    mock_os = MagicMock(spec=os)
    mock_os.getpgid = MagicMock(return_value=5678)
    mock_os.killpg = MagicMock()

    with patch.object(construction_module, "os", mock_os), \
         patch("drivers.construction.time.monotonic", return_value=0.0):
        await driver.terminate()

    for c in mock_os.killpg.call_args_list:
        assert c != call(5678, 9)


@pytest.mark.asyncio
async def test_terminate_handles_process_lookup_error_race_condition():
    """
    GAP | terminate(): Handles ProcessLookupError (TOCTOU race).

    Contract: If the process exits between getpgid() and killpg(), 
    terminate() must silently absorb the error.
    """
    import drivers.construction as construction_module

    driver = _make_driver()
    mock_proc = AsyncMockProcess(pid=1234)
    driver._proc = mock_proc

    mock_os = MagicMock(spec=os)
    mock_os.getpgid = MagicMock(return_value=5678)
    mock_os.killpg = MagicMock(side_effect=ProcessLookupError)

    with patch.object(construction_module, "os", mock_os):
        await driver.terminate()  # must not raise


@pytest.mark.parametrize("key,value", [
    ("DB_PASSWD", "letmein"),
    ("DB_PWD", "letmein"),
    ("MY_CREDENTIAL", "s3cr3t"),
])
def test_redact_build_args_covers_standalone_secret_patterns(key, value):
    """
    GAP | _redact_build_args: Standalone patterns (passwd, pwd, credential).
    """
    cmd = ["docker", "build", "--build-arg", f"{key}={value}", "/stage"]
    safe = _redact_build_args(cmd)
    assert value not in safe
    assert f"{key}=***REDACTED***" in safe


def test_redact_build_args_no_redaction_when_build_arg_has_no_equals_sign():
    """
    GAP | _redact_build_args: No '=' in --build-arg value.
    """
    cmd = ["docker", "build", "--build-arg", "KEY_NO_VALUE", "/stage"]
    safe = _redact_build_args(cmd)
    assert "KEY_NO_VALUE" in safe


def test_redact_build_args_trailing_build_arg_at_end_of_list():
    """
    GAP | _redact_build_args: --build-arg at end of list (boundary).
    """
    cmd = ["docker", "build", "--build-arg"]
    safe = _redact_build_args(cmd)
    assert safe == cmd


def test_redact_build_args_value_with_multiple_equals_signs():
    """
    GAP | _redact_build_args: Values containing '='.
    """
    cmd = ["docker", "build", "--build-arg", "DB_PASSWORD=postgres://host?pass=abc", "/stage"]
    safe = _redact_build_args(cmd)
    assert "DB_PASSWORD=***REDACTED***" in safe
    assert "postgres://host?pass=abc" not in safe



@pytest.mark.asyncio
async def test_terminate_clears_proc_reference_is_handled_by_run(tmp_path):
    """
    GAP | run(): _proc is cleared after successful build.
    """
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n")

    driver = ConstructionDriver(stage_path=str(tmp_path), image_tag="myapp:v1")
    driver._build_popen = _make_async_popen(returncode=0)
    await driver.run()
    assert driver._proc is None


@pytest.mark.asyncio
async def test_run_clears_proc_reference_after_build_failure(tmp_path):
    """
    GAP | run(): _proc is cleared after failed build.
    """
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n")

    driver = ConstructionDriver(stage_path=str(tmp_path), image_tag="myapp:v1")
    driver._build_popen = _make_async_popen(returncode=1, stdout_lines=["COPY failed"])

    with pytest.raises(BuildError):
        await driver.run()

    assert driver._proc is None


def test_check_health_logs_warning_and_continues_on_malformed_json(caplog):
    """
    GAP | check_health(): Degradation on malformed 'docker info' JSON.
    """
    driver = _make_driver()

    def fake_run(cmd):
        if "info" in cmd:
            return _completed(returncode=0, stdout="NOT_JSON{{{")
        return _completed(returncode=0, stdout="24.0.0")

    with patch("drivers.construction.shutil.which", return_value="/usr/bin/docker"):
        driver._run_subprocess = fake_run
        with caplog.at_level(logging.WARNING, logger="hamilton.drivers.construction"):
            result = driver.check_health()

    assert result.success is True
    assert any("rootless check skipped" in record.message for record in caplog.records)


def test_check_health_reports_unknown_version_when_version_cmd_fails():
    """
    GAP | check_health(): Degradation when 'docker version' fails.
    """
    driver = _make_driver()

    def fake_run(cmd):
        if "info" in cmd:
            return _completed(returncode=0, stdout=_docker_info_json(rootless=True))
        return _completed(returncode=1, stderr="version command failed")

    with patch("drivers.construction.shutil.which", return_value="/usr/bin/docker"):
        driver._run_subprocess = fake_run
        result = driver.check_health()

    assert result.success is True
    assert result.output["version"] == "unknown"
