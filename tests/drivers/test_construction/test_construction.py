"""
Contract tests for drivers/construction.py

Strategy: Test the translation logic (command construction, cache injection,
secret/SSH flags, log redaction) and process lifecycle (terminate/signal)
without launching a real Docker daemon. ``_build_popen`` is replaced at the
instance level with an ``AsyncMockProcess`` / async factory.

Test categories:
  1. Command construction — core flags
  2. Cache Mount Integrity — --cache-from / --cache-to / --build-arg CACHE_ID
  3. Secret Leakage in Build Args — log redaction via _redact_build_args
  4. BuildKit Secret & SSH Handover — --secret and --ssh flags
  5. Surgical Interrupt (Signal Handling) — terminate() process group kill
  6. Exit-code → exception mapping
  7. Integration: run() with mocked async Popen
  8. Health Check — check_health() core paths
  9. _build_command() — --file flag and custom dockerfile
"""

import json
import os
import signal
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from core.exceptions import BuildError, EnvError
from drivers.construction import ConstructionDriver, _redact_build_args


def _make_driver(stage="/tmp/stage", tag="myapp:v1", **kwargs) -> ConstructionDriver:
    return ConstructionDriver(stage_path=stage, image_tag=tag, **kwargs)


class AsyncMockProcess:
    """
    Lightweight fake for asyncio.subprocess.Process.

    Supports async iteration of stdout lines, returncode, pid, kill().
    ``pid`` is fixed to 9999 so tests can assert on it without spawning
    a real process.
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


def test_build_command_starts_with_docker_build():
    """
    Contract: The assembled command must begin with ['docker', 'build'].
    """
    cmd = _make_driver()._build_command()
    assert cmd[:2] == ["docker", "build"]


def test_build_command_includes_tag():
    """
    Contract: --tag must be present so the image can be referenced by name
    in subsequent audit and deployment steps.
    """
    cmd = _make_driver(tag="myapp:sha256-abc123")._build_command()
    assert "--tag" in cmd
    assert "myapp:sha256-abc123" in cmd


def test_build_command_context_is_staging_path_last_element():
    """
    Contract: The build context (last element) must be the staging directory —
    never the live source tree.
    """
    driver = _make_driver(stage="/staging/project")
    cmd = driver._build_command()
    assert str(driver.stage_path) == cmd[-1]


def test_build_command_no_cache_flag_present_when_enabled():
    """
    Contract: --no-cache must appear when no_cache=True.
    """
    cmd = _make_driver(no_cache=True)._build_command()
    assert "--no-cache" in cmd


def test_build_command_no_cache_flag_absent_when_disabled():
    """
    Contract: --no-cache must NOT appear when no_cache=False (default).
    """
    cmd = _make_driver(no_cache=False)._build_command()
    assert "--no-cache" not in cmd


def test_build_command_path_with_spaces_is_single_element(tmp_path):
    """
    Contract: A staging path with spaces must appear as one list element —
    OS-level quoting via list, not shell=True.
    """
    spaced = tmp_path / "my staging dir"
    spaced.mkdir()
    driver = ConstructionDriver(stage_path=str(spaced), image_tag="app:v1")
    cmd = driver._build_command()
    assert str(driver.stage_path) in cmd


def test_cache_from_injected_when_cache_ref_set():
    """
    Contract: --cache-from must appear in the command when cache_ref is provided.
    Without it, CI clean runners rebuild every layer from scratch.
    """
    driver = _make_driver(cache_ref="ghcr.io/org/app:cache")
    cmd = driver._build_command()
    assert "--cache-from" in cmd
    assert any("ghcr.io/org/app:cache" in arg for arg in cmd)


def test_cache_to_injected_when_cache_ref_set():
    """
    Contract: --cache-to must also appear so newly built layers are exported
    back to the registry for the next CI run.
    """
    driver = _make_driver(cache_ref="ghcr.io/org/app:cache")
    cmd = driver._build_command()
    assert "--cache-to" in cmd
    assert any("mode=max" in arg for arg in cmd)


def test_build_arg_cache_id_injected_when_project_hash_set():
    """
    Contract: --build-arg CACHE_ID=<hash> must be injected to scope the
    BuildKit layer cache per project — preventing cross-project contamination
    on shared CI runners.
    """
    driver = _make_driver(project_hash="abc123def456")
    cmd = driver._build_command()
    assert "--build-arg" in cmd
    arg_idx = cmd.index("--build-arg")
    assert cmd[arg_idx + 1] == "CACHE_ID=abc123def456"


def test_cache_flags_absent_when_cache_ref_not_set():
    """
    Contract: --cache-from and --cache-to must NOT appear when no cache_ref
    is configured — local builds should not reference a non-existent registry.
    """
    cmd = _make_driver()._build_command()
    assert "--cache-from" not in cmd
    assert "--cache-to" not in cmd


def test_cache_flags_suppressed_by_no_cache():
    """
    Contract: When no_cache=True, --cache-from and --cache-to must be
    suppressed even if a cache_ref is provided — no_cache wins.
    """
    driver = _make_driver(cache_ref="ghcr.io/org/app:cache", no_cache=True)
    cmd = driver._build_command()
    assert "--cache-from" not in cmd
    assert "--cache-to" not in cmd


def test_redact_build_args_redacts_password():
    """
    Contract: A --build-arg whose key contains 'password' must have its
    value replaced with ***REDACTED*** in the log-safe copy.
    """
    cmd = ["docker", "build", "--build-arg", "DB_PASSWORD=hunter2", "/stage"]
    safe = _redact_build_args(cmd)
    assert "DB_PASSWORD=***REDACTED***" in safe
    assert "hunter2" not in safe


def test_redact_build_args_redacts_secret():
    """
    Contract: Keys containing 'secret' must also be redacted.
    """
    cmd = ["docker", "build", "--build-arg", "API_SECRET=abc123", "/stage"]
    safe = _redact_build_args(cmd)
    assert "API_SECRET=***REDACTED***" in safe


def test_redact_build_args_redacts_token():
    """
    Contract: Keys containing 'token' must be redacted to prevent
    OAuth / Bearer tokens from appearing in CI logs.
    """
    cmd = ["docker", "build", "--build-arg", "GITHUB_TOKEN=ghp_abcdef", "/stage"]
    safe = _redact_build_args(cmd)
    assert "GITHUB_TOKEN=***REDACTED***" in safe


def test_redact_build_args_preserves_non_secret_args():
    """
    Contract: Non-sensitive --build-arg entries (e.g., NODE_ENV, PORT)
    must pass through unmodified — we must not break build configuration.
    """
    cmd = ["docker", "build", "--build-arg", "NODE_ENV=production", "/stage"]
    safe = _redact_build_args(cmd)
    assert "NODE_ENV=production" in safe


def test_redact_build_args_is_case_insensitive():
    """
    Contract: Redaction must be case-insensitive — PASSWORD, password, and
    Password are all sensitive.
    """
    for key in ["PASSWORD", "password", "Password"]:
        cmd = ["--build-arg", f"{key}=secret"]
        safe = _redact_build_args(cmd)
        assert f"{key}=***REDACTED***" in safe, f"Expected {key} to be redacted"


def test_redact_build_args_does_not_mutate_original():
    """
    Contract: _redact_build_args must return a NEW list. The original command
    passed to the subprocess must remain unmodified.
    """
    original = ["docker", "build", "--build-arg", "DB_PASSWORD=hunter2"]
    safe = _redact_build_args(original)
    assert original[3] == "DB_PASSWORD=hunter2"  # original unchanged
    assert safe[3] == "DB_PASSWORD=***REDACTED***"


@pytest.mark.parametrize("key,value", [
    ("DB_PASSWORD", "hunter2"),
    ("API_SECRET", "abc123"),
    ("GITHUB_TOKEN", "ghp_abc"),
    ("AUTH_KEY", "xyz789"),
])
def test_redact_build_args_covers_all_secret_key_patterns(key, value):
    """
    Contract: All secret key patterns recognised by the driver must be
    consistently redacted in a parametrized pass.
    """
    cmd = ["docker", "build", "--build-arg", f"{key}={value}", "/stage"]
    safe = _redact_build_args(cmd)
    assert value not in safe


def test_secret_flag_injected_for_each_secret_spec():
    """
    Contract: Each entry in ``secrets`` must produce one ``--secret``
    flag in the command. This is the BuildKit-native secure mount method —
    no credential is baked into a layer.
    """
    driver = _make_driver(secrets=["id=npmrc,src=/home/user/.npmrc", "id=ghtoken,src=/tmp/token"])
    cmd = driver._build_command()
    secret_args = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--secret"]
    assert "id=npmrc,src=/home/user/.npmrc" in secret_args
    assert "id=ghtoken,src=/tmp/token" in secret_args


def test_ssh_flag_injected_when_ssh_enabled():
    """
    Contract: ``--ssh default`` must appear in the command when ssh=True,
    enabling private Git clones without baking credentials into the image.
    """
    driver = _make_driver(ssh=True)
    cmd = driver._build_command()
    assert "--ssh" in cmd
    ssh_idx = cmd.index("--ssh")
    assert cmd[ssh_idx + 1] == "default"


def test_ssh_flag_absent_when_ssh_disabled():
    """
    Contract: ``--ssh`` must NOT appear when ssh=False (default).
    """
    driver = _make_driver(ssh=False)
    cmd = driver._build_command()
    assert "--ssh" not in cmd


def test_secret_uses_mount_syntax_not_env():
    """
    Contract: The driver must use BuildKit's ``--secret id=...`` syntax,
    NOT ``--build-arg`` or ``--env`` for credentials. This verifies we are
    using the modern, secure mounting method rather than the insecure
    "bake into layer" method.
    """
    driver = _make_driver(secrets=["id=mytoken,src=/tmp/token"])
    cmd = driver._build_command()
    # --secret must be present, not --build-arg with the secret
    assert "--secret" in cmd
    # The secret spec must not appear as a --build-arg value
    build_arg_values = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--build-arg"]
    assert not any("mytoken" in v for v in build_arg_values)


@pytest.mark.asyncio
async def test_terminate_calls_killpg_on_posix():
    """
    Contract: terminate() must call os.killpg with SIGTERM when the OS
    supports process groups (POSIX). This kills the entire BuildKit tree,
    not just the parent docker process.

    We replace the entire `os` reference inside the module with a stub that
    has killpg and getpgid — avoiding AttributeError on Windows where these
    functions genuinely don't exist.
    """
    import drivers.construction as construction_module

    driver = _make_driver()
    mock_proc = AsyncMockProcess(pid=1234)
    mock_proc.returncode = -15  # simulate process exiting after SIGTERM
    driver._proc = mock_proc

    # Stub os with killpg and getpgid present
    mock_os = MagicMock(spec=os)
    mock_os.getpgid = MagicMock(return_value=5678)
    mock_os.killpg  = MagicMock()

    with patch.object(construction_module, "os", mock_os):
        await driver.terminate()

    mock_os.getpgid.assert_called_with(1234)
    # SIGTERM must be sent to the process GROUP (5678), not just the PID
    mock_os.killpg.assert_any_call(5678, signal.SIGTERM)


@pytest.mark.asyncio
async def test_terminate_is_noop_when_no_active_build():
    """
    Contract: terminate() must not raise when called before run() or after
    the build has already completed. This makes it safe to call from the
    Supervisor's cleanup handler unconditionally.
    """
    driver = _make_driver()
    assert driver._proc is None
    await driver.terminate()  # must not raise


@pytest.mark.asyncio
async def test_terminate_kills_process_on_windows_fallback():
    """
    Contract: On Windows (where os.killpg is absent), terminate() must
    fall back to Popen.kill() to abort the build process.
    """
    driver = _make_driver()
    mock_proc = AsyncMockProcess(pid=4321)
    driver._proc = mock_proc

    # Simulate a Windows environment where os.killpg does not exist
    with patch("drivers.construction.hasattr", return_value=False):
        await driver.terminate()

    assert mock_proc.killed is True


def test_map_exit_code_127_raises_env_error():
    """
    Contract: Exit 127 (docker binary not found) must raise EnvError —
    an environment problem, not a build logic failure.
    """
    with pytest.raises(EnvError) as exc_info:
        ConstructionDriver._map_exit_code(127, "")
    assert exc_info.value.context["exit_code"] == 127


def test_map_exit_code_137_raises_build_error_with_oom():
    """
    Contract: Exit 137 (OOM-killed) must raise BuildError with oom=True
    so the Supervisor can emit a specific memory-pressure log message.
    """
    with pytest.raises(BuildError) as exc_info:
        ConstructionDriver._map_exit_code(137, "Killed")
    assert exc_info.value.context["oom"] is True


def test_map_exit_code_negative_raises_build_error_with_aborted():
    """
    Contract: A negative exit code (POSIX signal kill) must raise BuildError
    with aborted=True — signalling a P1-triggered abort, not a build failure.
    """
    with pytest.raises(BuildError) as exc_info:
        ConstructionDriver._map_exit_code(-15, "")  # -15 = SIGTERM
    assert exc_info.value.context["aborted"] is True


def test_map_exit_code_1_raises_generic_build_error():
    """
    Contract: A generic build failure (exit 1) must raise BuildError
    containing the stderr for the operator's inspection.
    """
    with pytest.raises(BuildError) as exc_info:
        ConstructionDriver._map_exit_code(1, "COPY failed: no such file")
    assert "COPY failed" in exc_info.value.context["stderr"]



@pytest.mark.asyncio
async def test_run_returns_driver_result_on_success(tmp_path):
    """
    Contract: run() must return DriverResult(success=True) with image_tag
    in the output when docker build exits 0.
    """
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n")

    driver = ConstructionDriver(stage_path=str(tmp_path), image_tag="myapp:v1.0.0")
    driver._build_popen = _make_async_popen(returncode=0, stdout_lines=["Successfully built abc123"])
    result = await driver.run()

    assert result.success is True
    assert result.output["image_tag"] == "myapp:v1.0.0"


@pytest.mark.asyncio
async def test_run_raises_build_error_on_nonzero_exit(tmp_path):
    """
    Contract: run() must raise BuildError (not return a failed DriverResult)
    when docker build exits non-zero — the P3 stream is aborted.
    """
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n")

    driver = ConstructionDriver(stage_path=str(tmp_path), image_tag="app:v1")
    driver._build_popen = _make_async_popen(returncode=1, stdout_lines=["COPY failed: no such file"])
    with pytest.raises(BuildError):
        await driver.run()


@pytest.mark.asyncio
async def test_run_raises_build_error_with_oom_context_on_exit_137(tmp_path):
    """
    Contract: run() must raise BuildError with oom=True when OOM-killed.
    """
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n")

    driver = ConstructionDriver(stage_path=str(tmp_path), image_tag="app:v1")
    driver._build_popen = _make_async_popen(returncode=137)
    with pytest.raises(BuildError) as exc_info:
        await driver.run()
    assert exc_info.value.context["oom"] is True


@pytest.mark.asyncio
async def test_run_raises_build_error_with_aborted_on_sigterm(tmp_path):
    """
    Contract: run() must raise BuildError with aborted=True when the process
    is killed by a signal (negative exit code — P1 Abort scenario).
    """
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n")

    driver = ConstructionDriver(stage_path=str(tmp_path), image_tag="app:v1")
    driver._build_popen = _make_async_popen(returncode=-15)
    with pytest.raises(BuildError) as exc_info:
        await driver.run()
    assert exc_info.value.context["aborted"] is True


@pytest.mark.asyncio
async def test_run_passes_full_command_to_popen_not_redacted(tmp_path):
    """
    Contract: run() must pass the REAL command (with secrets) to _build_popen
    — only the logged representation is redacted. The build itself needs the
    actual values.
    """
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n")

    driver = ConstructionDriver(stage_path=str(tmp_path), image_tag="app:v1", project_hash="abc123")
    captured_cmds: list[list[str]] = []

    async def fake_popen(cmd):
        captured_cmds.append(cmd)
        return AsyncMockProcess(returncode=0)

    driver._build_popen = fake_popen
    await driver.run()

    # The real CACHE_ID value must reach Popen
    full_cmd = captured_cmds[0]
    assert "CACHE_ID=abc123" in full_cmd

def _completed(returncode=0, stdout="", stderr="") -> subprocess.CompletedProcess:
    """Build a fake CompletedProcess for _run_subprocess stubs."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _docker_info_json(rootless: bool = True) -> str:
    """Return a minimal ``docker info`` JSON payload for health-check tests."""
    options = ["name=rootless"] if rootless else ["name=apparmor"]
    return json.dumps({"SecurityOptions": options, "ServerVersion": "24.0.0"})


def test_check_health_raises_env_error_when_docker_missing():
    """
    Contract: check_health() must raise EnvError immediately when 'docker'
    is absent from PATH — before attempting any daemon communication.

    Mocks shutil.which at the module level (not os.environ) because the
    driver delegates binary discovery entirely to shutil.which.
    """
    driver = _make_driver()
    with patch("drivers.construction.shutil.which", return_value=None):
        with pytest.raises(EnvError) as exc_info:
            driver.check_health()

    assert exc_info.value.context["tool"] == "docker"


def test_check_health_raises_env_error_when_daemon_unreachable():
    """
    Contract: If ``docker info`` exits non-zero, the daemon is down.
    check_health() must raise EnvError with a 'not reachable' message —
    not a BuildError, because this is a pre-flight environment failure.

    _run_subprocess is replaced at the instance level (not patched globally)
    because the driver already provides this seam for testing.
    """
    driver = _make_driver()
    with patch("drivers.construction.shutil.which", return_value="/usr/bin/docker"):
        driver._run_subprocess = MagicMock(
            return_value=_completed(returncode=1, stderr="Cannot connect to Docker daemon")
        )
        with pytest.raises(EnvError) as exc_info:
            driver.check_health()

    assert "not reachable" in str(exc_info.value).lower()


def test_check_health_raises_env_error_when_not_rootless():
    """
    Contract: Hamilton-Ops is a SECURITY REQUIREMENT — it must refuse to
    operate when Docker runs as root (README rootless mandate).
    check_health() must raise EnvError when 'rootless' is absent from
    SecurityOptions, even when the daemon is reachable.

    This is the most critical branch: a silent pass here would allow
    a root-mode Docker to build production images without detection.
    """
    driver = _make_driver()

    def fake_run(cmd):
        if "info" in cmd:
            # Non-rootless payload — apparmor only, no rootless entry
            return _completed(returncode=0, stdout=_docker_info_json(rootless=False))
        return _completed(returncode=0, stdout="24.0.0")

    with patch("drivers.construction.shutil.which", return_value="/usr/bin/docker"):
        driver._run_subprocess = fake_run
        with pytest.raises(EnvError) as exc_info:
            driver.check_health()

    assert "rootless" in str(exc_info.value).lower()


def test_check_health_returns_driver_result_on_success():
    """
    Contract: check_health() must return DriverResult(success=True) with
    a 'version' key in output when Docker is installed, daemon is up,
    and running in rootless mode.
    """
    driver = _make_driver()

    def fake_run(cmd):
        if "info" in cmd:
            return _completed(returncode=0, stdout=_docker_info_json(rootless=True))
        # Version check returns a plain version string
        return _completed(returncode=0, stdout="26.1.4\n")

    with patch("drivers.construction.shutil.which", return_value="/usr/bin/docker"):
        driver._run_subprocess = fake_run
        result = driver.check_health()

    assert result.success is True
    assert result.output["version"] == "26.1.4"


def test_build_command_includes_file_flag_pointing_to_default_dockerfile(tmp_path):
    """
    Contract: _build_command() must always include ``--file`` so BuildKit
    uses the controlled Dockerfile in the staging area — not a random
    Dockerfile that happens to be in the working directory.

    Default: <stage_path>/Dockerfile
    """
    driver = ConstructionDriver(stage_path=str(tmp_path), image_tag="app:v1")
    cmd = driver._build_command()

    assert "--file" in cmd
    file_idx = cmd.index("--file")
    # The default dockerfile must resolve to <stage_path>/Dockerfile
    assert cmd[file_idx + 1] == str(tmp_path / "Dockerfile")


def test_build_command_uses_custom_dockerfile_when_provided(tmp_path):
    """
    Contract: When a custom dockerfile path is passed to the constructor,
    _build_command() must use THAT path in the --file argument, not the
    default <stage_path>/Dockerfile.

    This enables multi-Dockerfile projects (e.g., Dockerfile.prod vs
    Dockerfile.dev) without changing the staging directory layout.
    """
    custom_df = tmp_path / "Dockerfile.prod"
    custom_df.touch()  # file must exist for Path.resolve() to be deterministic
    driver = ConstructionDriver(
        stage_path=str(tmp_path),
        image_tag="app:v1",
        dockerfile=str(custom_df),
    )
    cmd = driver._build_command()

    assert "--file" in cmd
    file_idx = cmd.index("--file")
    assert cmd[file_idx + 1] == str(custom_df.resolve())


