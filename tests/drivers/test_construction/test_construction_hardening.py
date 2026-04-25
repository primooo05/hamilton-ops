"""
Gap tests for the hardened drivers/construction.py

Covers the six features added in the hardening pass:

  Feature 1 & 2 — Asyncio Integration
      run() is async and non-blocking; terminate() awaits the grace period
      without holding the event loop.

  Feature 3 — Stream Log Sanitisation
      _redact_line() strips secrets from individual build output lines.

  Feature 4 — Resource Guardrails
      --memory and --cpu-period / --cpu-quota flags appear in the command
      with correct values derived from memory_gb and cpu_count.

  Feature 5 — Dockerfile Existence Validation
      run() raises EnvError before launching Docker if the Dockerfile
      is missing — clean error instead of a cryptic daemon message.

  Feature 6 — Artifact Path Handoff
      DriverResult.output["artifact_path"] carries the expected binary
      location so the Supervisor can forward it directly to AuditChain.

Strategy:
  - All async tests use pytest-asyncio with asyncio_mode="auto".
  - The asyncio Process is replaced at the instance level via _build_popen
    override, keeping OS-subprocess isolation intact.
  - No mocking of the asyncio module itself — we use a lightweight
    AsyncMockProcess that mimics asyncio.subprocess.Process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.exceptions import BuildError, EnvError
from drivers.construction import (
    ConstructionDriver,
    _redact_build_args,
    _redact_line,
)



def _make_driver(stage="/tmp/stage", tag="myapp:v1", **kwargs) -> ConstructionDriver:
    return ConstructionDriver(stage_path=stage, image_tag=tag, **kwargs)


class AsyncMockProcess:
    """
    Minimal fake for asyncio.subprocess.Process.

    Provides:
      - pid (fixed)
      - returncode (set after wait)
      - stdout (async iterable of encoded lines)
      - kill() / terminate()
      - wait() (coroutine)
    """

    def __init__(self, returncode: int = 0, stdout_lines: list[str] | None = None, pid: int = 9999):
        self.pid = pid
        self.returncode = returncode
        self._stdout_lines = stdout_lines or []
        self.killed = False
        self.terminated = False

        # stdout is an async iterable that yields encoded lines
        async def _async_gen():
            for line in self._stdout_lines:
                yield (line + "\n").encode()

        self.stdout = _async_gen()

    async def wait(self):
        return self.returncode

    def kill(self):
        self.killed = True

    def terminate(self):
        self.terminated = True


def _make_async_popen(returncode: int = 0, stdout_lines: list[str] | None = None, pid: int = 9999):
    """Return an async factory that produces an AsyncMockProcess."""
    async def _factory(cmd):
        return AsyncMockProcess(returncode=returncode, stdout_lines=stdout_lines, pid=pid)
    return _factory



class TestResourceGuardrails:
    """--memory and --cpu-period/quota must appear in the build command."""

    def test_memory_flag_injected_with_default(self):
        """
        Contract: --memory 4g must appear by default (Pillar E guardrail).
        """
        driver = _make_driver()
        cmd = driver._build_command()
        assert "--memory" in cmd
        mem_idx = cmd.index("--memory")
        assert cmd[mem_idx + 1] == "4g"

    def test_memory_flag_uses_configured_value(self):
        """
        Contract: --memory uses the memory_gb value passed at construction time.
        """
        driver = _make_driver(memory_gb=8)
        cmd = driver._build_command()
        mem_idx = cmd.index("--memory")
        assert cmd[mem_idx + 1] == "8g"

    def test_cpu_period_injected(self):
        """
        Contract: --cpu-period 100000 must always be present alongside --cpu-quota.
        """
        driver = _make_driver(cpu_count=4)
        cmd = driver._build_command()
        assert "--cpu-period" in cmd
        period_idx = cmd.index("--cpu-period")
        assert cmd[period_idx + 1] == "100000"

    def test_cpu_quota_equals_cpu_count_times_period(self):
        """
        Contract: --cpu-quota = cpu_count * 100000. For cpu_count=4 → 400000.
        """
        driver = _make_driver(cpu_count=4)
        cmd = driver._build_command()
        quota_idx = cmd.index("--cpu-quota")
        assert cmd[quota_idx + 1] == "400000"

    def test_cpu_count_auto_detected_when_not_provided(self):
        """
        Contract: When cpu_count is not specified, os.cpu_count() is used.
        The quota value must be a positive multiple of 100000.
        """
        driver = _make_driver()
        cmd = driver._build_command()
        quota_idx = cmd.index("--cpu-quota")
        quota = int(cmd[quota_idx + 1])
        assert quota > 0
        assert quota % 100000 == 0

    def test_cpu_falls_back_to_one_when_os_cpu_count_returns_none(self):
        """
        Contract: If os.cpu_count() returns None (rare container environments),
        the driver defaults to 1 core (quota = 100000).
        """
        with patch("drivers.construction.os.cpu_count", return_value=None):
            driver = _make_driver()
        cmd = driver._build_command()
        quota_idx = cmd.index("--cpu-quota")
        # cpu_count should have fallen back to 1 → quota = 100000
        assert cmd[quota_idx + 1] == "100000"

    def test_resource_flags_appear_before_context_path(self):
        """
        Contract: Resource flags must appear before the build context argument
        (the staging directory) which must always be the last element.
        """
        driver = _make_driver(cpu_count=2)
        cmd = driver._build_command()
        context = cmd[-1]
        mem_idx = cmd.index("--memory")
        cpu_idx = cmd.index("--cpu-period")
        # Both resource flags must precede the context path
        assert mem_idx < len(cmd) - 1
        assert cpu_idx < len(cmd) - 1
        assert cmd[-1] == context


class TestDockerfileValidation:
    """run() must raise EnvError before launch if Dockerfile is missing."""

    @pytest.mark.asyncio
    async def test_run_raises_env_error_when_dockerfile_missing(self, tmp_path):
        """
        Contract: If the Dockerfile does not exist, run() must raise EnvError
        with context["dockerfile"] set, before any subprocess is launched.
        """
        # tmp_path exists but contains no Dockerfile
        driver = ConstructionDriver(stage_path=str(tmp_path), image_tag="app:v1")
        # Guarantee the Dockerfile is absent
        assert not driver.dockerfile.exists()

        popen_called = False

        async def _spy_popen(cmd):
            nonlocal popen_called
            popen_called = True
            return AsyncMockProcess(returncode=0)

        driver._build_popen = _spy_popen

        with pytest.raises(EnvError) as exc_info:
            await driver.run()

        assert "dockerfile" in exc_info.value.context
        assert not popen_called, "_build_popen must NOT be called when Dockerfile is missing"

    @pytest.mark.asyncio
    async def test_run_proceeds_when_dockerfile_exists(self, tmp_path):
        """
        Contract: When the Dockerfile is present, run() must NOT raise EnvError
        for a missing file — it should proceed to launch the build.
        """
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM scratch\n")

        driver = ConstructionDriver(stage_path=str(tmp_path), image_tag="app:v1")
        driver._build_popen = _make_async_popen(returncode=0, stdout_lines=["Step 1/1 done"])

        result = await driver.run()
        assert result.success is True

    @pytest.mark.asyncio
    async def test_env_error_includes_dockerfile_path_in_context(self, tmp_path):
        """
        Contract: The EnvError context must contain the absolute Dockerfile path
        so operators can find the missing file immediately.
        """
        driver = ConstructionDriver(stage_path=str(tmp_path), image_tag="app:v1")
        driver._build_popen = _make_async_popen(returncode=0)

        with pytest.raises(EnvError) as exc_info:
            await driver.run()

        assert str(driver.dockerfile) in exc_info.value.context["dockerfile"]


class TestStreamSanitisation:
    """_redact_line() must redact secrets from individual output lines."""

    def test_redact_line_strips_password_assignment(self):
        """
        Contract: A line containing KEY=VALUE where KEY matches the secret
        pattern must have VALUE replaced with ***REDACTED***.
        """
        line = "Step 3: setting DB_PASSWORD=hunter2 in environment"
        result = _redact_line(line)
        assert "hunter2" not in result
        assert "DB_PASSWORD=***REDACTED***" in result

    def test_redact_line_strips_token_assignment(self):
        """
        Contract: Lines containing TOKEN=VALUE are redacted.
        """
        line = "Fetching: GITHUB_TOKEN=ghp_abc123"
        result = _redact_line(line)
        assert "ghp_abc123" not in result

    def test_redact_line_strips_api_key_colon_syntax(self):
        """
        Contract: KEY: VALUE (colon-separated) variants must also be redacted
        since some tools print environment variables with colons.
        """
        line = "api_key: s3cr3t_value"
        result = _redact_line(line)
        assert "s3cr3t_value" not in result

    def test_redact_line_preserves_non_secret_lines(self):
        """
        Contract: Build output lines that don't match secret patterns must
        pass through completely unchanged.
        """
        line = "Step 2/5: RUN apt-get install -y curl"
        result = _redact_line(line)
        assert result == line

    def test_redact_line_is_idempotent(self):
        """
        Contract: Running _redact_line() twice on an already-redacted line
        must not change the result (no double-redaction artifacts).
        """
        line = "DB_PASSWORD=hunter2"
        first = _redact_line(line)
        second = _redact_line(first)
        assert first == second

    @pytest.mark.asyncio
    async def test_build_stdout_is_sanitised_before_logging(self, tmp_path, caplog):
        """
        Contract: Secrets that appear in build stdout must be redacted before
        they reach the log sink. The raw secret must never appear in the log.
        """
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM scratch\n")

        driver = ConstructionDriver(stage_path=str(tmp_path), image_tag="app:v1")
        driver._build_popen = _make_async_popen(
            returncode=0,
            stdout_lines=["DB_PASSWORD=hunter2 echoed by RUN printenv"],
        )

        with caplog.at_level(logging.DEBUG, logger="hamilton.drivers.construction"):
            result = await driver.run()

        # Secret must not appear raw in any log record
        for record in caplog.records:
            assert "hunter2" not in record.message

        # The redacted form must appear in DriverResult stdout
        assert "hunter2" not in result.output["stdout"]
        assert "***REDACTED***" in result.output["stdout"]


class TestArtifactHandoff:
    """DriverResult must include artifact_path after a successful build."""

    @pytest.mark.asyncio
    async def test_result_includes_artifact_path_from_subpath(self, tmp_path):
        """
        Contract: When artifact_subpath is configured, DriverResult.output
        must include "artifact_path" pointing to stage_path / artifact_subpath.
        """
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM scratch\n")

        driver = ConstructionDriver(
            stage_path=str(tmp_path),
            image_tag="app:v1",
            artifact_subpath="dist/app",
        )
        driver._build_popen = _make_async_popen(returncode=0)

        result = await driver.run()
        expected = str(tmp_path / "dist/app")
        assert result.output["artifact_path"] == expected

    @pytest.mark.asyncio
    async def test_result_falls_back_to_stage_root_when_no_subpath(self, tmp_path):
        """
        Contract: When artifact_subpath is not set, artifact_path falls back
        to the stage_path root so the Supervisor always has a valid path.
        """
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM scratch\n")

        driver = ConstructionDriver(stage_path=str(tmp_path), image_tag="app:v1")
        driver._build_popen = _make_async_popen(returncode=0)

        result = await driver.run()
        assert result.output["artifact_path"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_result_always_contains_image_tag(self, tmp_path):
        """
        Contract: image_tag must always be present in DriverResult.output
        regardless of whether artifact_subpath is set.
        """
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM scratch\n")

        driver = ConstructionDriver(stage_path=str(tmp_path), image_tag="myapp:sha256-abc")
        driver._build_popen = _make_async_popen(returncode=0)

        result = await driver.run()
        assert result.output["image_tag"] == "myapp:sha256-abc"
        assert "artifact_path" in result.output


class TestAsyncioIntegration:
    """run() and terminate() must be event-loop safe."""

    @pytest.mark.asyncio
    async def test_run_is_coroutine(self, tmp_path):
        """
        Contract: ConstructionDriver.run must be a coroutine function so
        the TaskGroup can await it without asyncio.to_thread wrapping.
        """
        import inspect
        assert inspect.iscoroutinefunction(ConstructionDriver.run)

    @pytest.mark.asyncio
    async def test_terminate_is_coroutine(self):
        """
        Contract: ConstructionDriver.terminate must be a coroutine function
        so the kill handler can await it.
        """
        import inspect
        assert inspect.iscoroutinefunction(ConstructionDriver.terminate)

    @pytest.mark.asyncio
    async def test_terminate_clears_proc_reference_on_posix(self):
        """
        Contract: After terminate() runs, _proc must be set to None so
        subsequent calls are no-ops (idempotency).
        """
        import drivers.construction as construction_module

        driver = _make_driver()
        proc = AsyncMockProcess(pid=1234)
        proc.returncode = -15  # simulate already dead after SIGTERM
        driver._proc = proc

        mock_os = MagicMock(spec=os)
        mock_os.getpgid = MagicMock(return_value=5678)
        mock_os.killpg = MagicMock()

        with patch.object(construction_module, "os", mock_os):
            await driver.terminate()

        assert driver._proc is None

    @pytest.mark.asyncio
    async def test_terminate_is_noop_when_no_proc(self):
        """
        Contract: terminate() must not raise when _proc is None (called before
        run() or after the build completed naturally).
        """
        driver = _make_driver()
        assert driver._proc is None
        await driver.terminate()  # must not raise

    @pytest.mark.asyncio
    async def test_run_raises_build_error_on_nonzero_exit(self, tmp_path):
        """
        Contract (async): run() must raise BuildError when docker exits non-zero.
        """
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM scratch\n")

        driver = ConstructionDriver(stage_path=str(tmp_path), image_tag="app:v1")
        driver._build_popen = _make_async_popen(
            returncode=1,
            stdout_lines=["COPY failed: no such file"],
        )

        with pytest.raises(BuildError):
            await driver.run()

    @pytest.mark.asyncio
    async def test_run_raises_env_error_on_exit_127(self, tmp_path):
        """
        Contract (async): run() must raise EnvError when docker exits 127
        (binary-not-found) during the build execution.
        """
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM scratch\n")

        driver = ConstructionDriver(stage_path=str(tmp_path), image_tag="app:v1")
        driver._build_popen = _make_async_popen(returncode=127)

        with pytest.raises(EnvError):
            await driver.run()

    @pytest.mark.asyncio
    async def test_run_raises_build_error_with_oom_context(self, tmp_path):
        """
        Contract (async): exit 137 (OOM-killed) must yield BuildError with
        context["oom"] = True.
        """
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM scratch\n")

        driver = ConstructionDriver(stage_path=str(tmp_path), image_tag="app:v1")
        driver._build_popen = _make_async_popen(returncode=137)

        with pytest.raises(BuildError) as exc_info:
            await driver.run()

        assert exc_info.value.context["oom"] is True

    @pytest.mark.asyncio
    async def test_proc_cleared_after_successful_build(self, tmp_path):
        """
        Contract: _proc must be None after run() completes successfully so
        terminate() is a no-op in post-flight cleanup.
        """
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM scratch\n")

        driver = ConstructionDriver(stage_path=str(tmp_path), image_tag="app:v1")
        driver._build_popen = _make_async_popen(returncode=0)

        await driver.run()
        assert driver._proc is None