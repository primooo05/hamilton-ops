"""
Hamilton-Ops Construction Driver — P3 Construction Stream

The Construction Driver is the "Factory Foreman" of Hamilton-Ops.

While P1 measures speed and P2 checks quality, the Construction Driver
turns the wrench: it hands the immutable staging directory to Docker
BuildKit and ensures a high-performance, security-hardened image is
produced. It is the "heavy lift" that the P1 Hamilton Alarm is designed
to kill if the guidance system (k6 / ThresholdExceededError) detects trouble.

Key capabilities over the simpler DockerDriver:

    Cache Management
        Injects ``--cache-from`` and ``--cache-to`` for CI registry caches,
        plus ``--build-arg CACHE_ID=<project_hash>`` to scope the BuildKit
        layer cache per project. Without this, every clean CI runner restarts
        from scratch (40s → 500s regression).

    Secure Secret / SSH Handover
        Supports ``--secret id=<name>,src=<path>`` for credentials that must
        reach the build without being baked into any image layer, and
        ``--ssh default`` for private Git clones over the SSH agent.

    Surgical Process Termination
        Uses ``asyncio.create_subprocess_exec`` to hold a live async handle to
        the BuildKit process. ``terminate()`` sends SIGTERM to the entire
        process group, then SIGKILL if the group doesn't exit within
        ``_SIGKILL_TIMEOUT_SECONDS``. On Windows, where ``os.killpg`` is
        unavailable, the driver falls back to ``Process.kill()``.

        IMPORTANT — asyncio safety: Because run() is async and uses the asyncio
        subprocess API, it does NOT block the event loop. The Supervisor's
        TaskGroup can cancel P3 mid-flight without the communicate() call
        freezing. terminate() is similarly async-safe.

    Log Sanitisation (command + stream)
        ``--build-arg KEY=VALUE`` entries whose keys match known secret patterns
        are redacted to ``KEY=***REDACTED***`` in the logged command.
        Additionally, every stdout/stderr line produced by the build process is
        passed through ``_redact_line()`` before being emitted to the log, so
        that a ``RUN echo $SECRET`` in the Dockerfile cannot leak values.

    Resource Guardrails (Pillar E)
        Injects ``--memory`` and CPU quota flags to prevent a runaway build
        from degrading the host. Defaults are 4 GB RAM and all logical cores.
        Both are configurable at construction time.

    Dockerfile Existence Validation
        The driver verifies that the target Dockerfile exists before launching
        the build. Missing Dockerfile → clean EnvError, not a cryptic Docker
        error message.

    Artifact Handoff to Audit Chain
        After a successful build, ``DriverResult.output`` includes an
        ``artifact_path`` key pointing to the expected binary location inside
        the staging directory. The Supervisor passes this directly to
        ``AuditChain.run()`` to close the handoff contract between P3 and the
        post-flight audit step.

    start_new_session vs os.setsid
        The driver uses ``start_new_session=True`` (equivalent to
        ``preexec_fn=os.setsid``) to move the subprocess into its own process
        group. This is a Python-idiomatic wrapper around the same POSIX
        ``setsid(2)`` syscall, so ``os.getpgid(pid)`` in ``terminate()``
        always returns a distinct PGID for targeted ``killpg`` calls.

Error-mapping contract:
    | Condition                     | Hamilton Signal              |
    |-------------------------------|------------------------------|
    | docker binary missing (127)   | EnvError                     |
    | OOM-killed (137)              | BuildError (oom=True context) |
    | Non-zero exit (build failure) | BuildError                   |
    | Daemon running as root        | EnvError (rootless violation) |
    | Dockerfile not found          | EnvError (before launch)     |
    | P1 Abort (terminate called)   | BuildError (aborted=True)    |
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

from core.exceptions import BuildError, EnvError
from drivers.registry import DriverResult

logger = logging.getLogger("hamilton.drivers.construction")

_EXIT_NOT_FOUND   = 127
_EXIT_OOM         = 137
_SIGKILL_TIMEOUT  = 5       # seconds to wait after SIGTERM before escalating

# Build-arg keys that must never appear in logs.
# Uses re.search (not re.match) so that compound keys like DB_PASSWORD,
# GITHUB_TOKEN, or MY_API_SECRET are all caught — not just bare 'password'.
_SECRET_KEY_PATTERN = re.compile(
    r"(password|passwd|pwd|secret|token|key|api|auth|credential)",
    re.IGNORECASE,
)

# Pattern for redacting secrets that appear inline in build output.
# Matches common KEY=VALUE patterns in Docker RUN output or env-dump commands.
_SECRET_LINE_PATTERN = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|api[_\-]?key|auth|credential)\s*[=:]\s*\S+",
)

# ---------------------------------------------------------------------------
# Package-manager install commands that must appear AFTER dependency manifests
# are COPYed in a Dockerfile. Detecting ``COPY .`` before these commands means
# every source-code change busts the expensive install layer.
# ---------------------------------------------------------------------------
_INSTALL_COMMANDS: frozenset[str] = frozenset([
    "npm install",
    "npm ci",
    "yarn install",
    "yarn",
    "pnpm install",
    "pip install",
    "pip3 install",
    "poetry install",
    "cargo build",
    "cargo fetch",
    "mvn install",
    "mvn package",
    "gradle build",
    "go mod download",
    "bundle install",
])


class DockerfileAnalyzer:
    """
    Static pre-build Dockerfile layer analyser.

    Parses the Dockerfile *before* handing off to Docker so that common
    caching anti-patterns are caught as a clean EnvError rather than a
    silent cache miss that inflates CI build time from ~40 s to ~500 s.

    Anti-pattern detected — ``COPY .`` before dependency install
    ---------------------------------------------------------------
    When a Dockerfile copies the entire source tree (``COPY . .``,
    ``COPY . /app``, etc.) before running a package-manager install
    command, every single source-code change invalidates the dependency
    install layer, forcing a full reinstall on every commit.

    Correct pattern::

        COPY package*.json ./       ← copy manifests only
        RUN npm ci                  ← install deps (cached layer)
        COPY . .                    ← copy source (busts ONLY the upper layers)

    The analyser raises EnvError with the offending line numbers so the
    developer can fix the Dockerfile before wasting CI minutes.

    Limitations:
        - Only the outermost ``FROM … AS`` stage is analysed (multi-stage
          Dockerfiles have many stages; future work can extend per-stage).
        - ``COPY --from=…`` (cross-stage copies) are ignored — they do not
          represent the live source tree.
        - ``ADD`` is not flagged: it is rarely used for source trees, and
          its URL-download form would produce false positives.
        - Comments and blank lines are stripped before analysis.
    """

    def __init__(self, dockerfile_path: Path) -> None:
        """
        Args:
            dockerfile_path: Absolute path to the Dockerfile to analyse.
        """
        self._path = dockerfile_path

    def analyze(self) -> None:
        """
        Perform static layer analysis on the Dockerfile.

        Raises:
            EnvError: If a ``COPY .`` instruction precedes a package-manager
                      install command in the same build stage.
        """
        try:
            lines = self._path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            # File access error — let the existing Dockerfile-existence guard
            # in run() surface a cleaner message; just log here.
            logger.debug("ANALYZER: Could not read Dockerfile at %s: %s", self._path, exc)
            return

        violations = self._detect_copy_before_install(lines)
        if violations:
            detail = "; ".join(
                f"line {lineno}: '{snippet}'" for lineno, snippet in violations
            )
            raise EnvError(
                f"Dockerfile layer cache anti-pattern detected in '{self._path}'. "
                f"A 'COPY .' instruction appears before a package-manager install command. "
                f"This busts the dependency cache on every source change. "
                f"Move 'COPY .' AFTER the install step. "
                f"Offending location(s): {detail}",
                context={
                    "dockerfile": str(self._path),
                    "violations": [(n, s) for n, s in violations],
                },
            )

        logger.debug("ANALYZER: Dockerfile %s passed layer-cache analysis.", self._path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _detect_copy_before_install(self, lines: list[str]) -> list[tuple[int, str]]:
        """
        Scan logical Dockerfile instructions for ``COPY .`` before install.

        Returns a list of (line_number, snippet) pairs for each COPY . that
        precedes an install command in the same stage. Empty list = clean.

        Implementation notes:
            - Line numbers are 1-indexed to match editor/IDE conventions.
            - Continuation lines (trailing backslash) are joined so that a
              multi-line RUN command is analysed as a single unit.
            - ``COPY --from=…`` (cross-stage) is explicitly excluded.
            - Resets ``pending_copy_dots`` on each new ``FROM`` so that
              multi-stage builds are evaluated per-stage.
        """
        # Join continuation lines into logical instructions.
        logical: list[tuple[int, str]] = []  # (first_line_no, instruction)
        buf: list[str] = []
        start_lineno = 1

        for lineno, raw in enumerate(lines, start=1):
            stripped = raw.strip()
            # Skip comments and blanks.
            if not stripped or stripped.startswith("#"):
                continue
            if not buf:
                start_lineno = lineno
            buf.append(stripped.rstrip("\\"))
            if not stripped.endswith("\\"):
                logical.append((start_lineno, " ".join(buf).strip()))
                buf = []

        if buf:
            # Unterminated continuation at EOF — flush anyway.
            logical.append((start_lineno, " ".join(buf).strip()))

        violations: list[tuple[int, str]] = []
        # Track line numbers of COPY . instructions seen in the current stage
        # that have NOT yet been followed by an install command.
        pending_copy_dots: list[tuple[int, str]] = []

        for lineno, instruction in logical:
            upper = instruction.upper()

            if upper.startswith("FROM "):
                # New stage — reset pending tracker.
                pending_copy_dots = []
                continue

            if self._is_copy_dot(instruction):
                pending_copy_dots.append((lineno, instruction))
                continue

            if pending_copy_dots and self._is_install_command(instruction):
                # Install command found AFTER a COPY . — this is the violation.
                violations.extend(pending_copy_dots)
                # Reset so subsequent install calls don't re-flag the same COPY.
                pending_copy_dots = []

        return violations

    @staticmethod
    def _is_copy_dot(instruction: str) -> bool:
        """
        Return True if ``instruction`` is a broad source-tree COPY.

        Patterns matched:
            COPY . .
            COPY . /app
            COPY . ./
            COPY . /

        Patterns NOT matched:
            COPY --from=builder /app/dist /app/dist   (cross-stage, ignored)
            COPY package.json ./                       (specific file, fine)
            COPY src/ /app/src                         (specific dir, fine)
        """
        # Normalise whitespace for reliable splitting.
        parts = instruction.split()
        if len(parts) < 3:
            return False
        if parts[0].upper() != "COPY":
            return False
        # Skip cross-stage copies entirely — ``--from=`` as the first real arg.
        if parts[1].startswith("--"):
            return False
        # The source argument is parts[1]; flag it if it is exactly ".".
        return parts[1] == "."

    @staticmethod
    def _is_install_command(instruction: str) -> bool:
        """
        Return True if ``instruction`` contains a known package-manager install.

        Matches are substring-based (using ``in``) so that flags and
        subcommands (e.g., ``npm ci --legacy-peer-deps``) are still detected.
        """
        # Strip the leading RUN keyword if present so we check the command body.
        body = instruction
        if body.upper().startswith("RUN "):
            body = body[4:].strip()

        lower_body = body.lower()
        return any(cmd in lower_body for cmd in _INSTALL_COMMANDS)



class ConstructionDriver:
    """
    BuildKit-powered P3 Construction Driver.

    Manages the *entire lifecycle* of a ``docker build``
    invocation: command construction, cache injection, secret mounting,
    live process control, stream log sanitisation, and resource guardrails.

    The key difference from ``DockerDriver`` is that execution goes through
    ``asyncio.create_subprocess_exec`` (not ``subprocess.run``), giving the
    driver a non-blocking async handle so the Supervisor's TaskGroup can
    cancel P3 mid-flight without freezing the event loop. ``terminate()``
    is likewise async so it can be awaited from within the TaskGroup's
    cancellation handler.

    All subprocess creation is routed through ``_build_popen`` so that tests
    can replace the real subprocess factory without mocking OS-level modules.
    """

    def __init__(
        self,
        stage_path: str | Path,
        image_tag: str,
        dockerfile: Optional[str | Path] = None,
        *,
        cache_ref: Optional[str] = None,
        project_hash: Optional[str] = None,
        secrets: Optional[list[str]] = None,
        ssh: bool = False,
        no_cache: bool = False,
        memory_gb: int = 4,
        cpu_count: Optional[int] = None,
        artifact_subpath: Optional[str] = None,
    ) -> None:
        """
        Args:
            stage_path:       Absolute path to the staging directory.
                              Passed as a list element, never shell-expanded.
            image_tag:        Docker image tag, e.g. ``myapp:sha256-abc123``.
            dockerfile:       Optional path to a Dockerfile. Defaults to
                              ``<stage_path>/Dockerfile``.
            cache_ref:        Registry reference for BuildKit layer caching,
                              e.g. ``ghcr.io/org/myapp:buildcache``. When set,
                              ``--cache-from`` and ``--cache-to`` are injected.
            project_hash:     Deterministic identifier for the project state
                              (e.g., SHA256 of the lock files). Injected as
                              ``--build-arg CACHE_ID=<hash>`` to scope the
                              BuildKit cache per project, preventing cross-project
                              cache contamination on shared CI runners.
            secrets:          List of BuildKit secret mount specs in the form
                              ``id=<name>,src=<path>``. Each item becomes a
                              ``--secret`` flag. The file at ``src`` is mounted
                              read-only into the build context; nothing is baked
                              into an image layer.
            ssh:              If True, passes ``--ssh default`` so the build
                              can access private Git repositories via the SSH
                              agent socket. Requires the agent to be running.
            no_cache:         If True, passes ``--no-cache`` to disable all
                              BuildKit layer caching (useful for security scans).
                              This overrides ``cache_ref``.
            memory_gb:        Docker memory limit in gigabytes (Pillar E guardrail).
                              Default: 4 GB. Injected as ``--memory <N>g``.
            cpu_count:        Number of logical CPU cores to allocate to the build.
                              Default: ``os.cpu_count()``. Injected as a
                              ``--cpu-quota`` relative to ``--cpu-period 100000``.
            artifact_subpath: Relative path inside ``stage_path`` where the built
                              binary is expected after the Docker build completes.
                              Used to populate ``artifact_path`` in DriverResult
                              for the Audit Chain handoff. Example: ``"dist/app"``.
                              If None, the stage_path root is used as a fallback.
        """
        self.stage_path      = Path(stage_path).resolve()
        self.image_tag       = image_tag
        self.dockerfile      = Path(dockerfile).resolve() if dockerfile else self.stage_path / "Dockerfile"
        self.cache_ref       = cache_ref
        self.project_hash    = project_hash
        self.secrets         = secrets or []
        self.ssh             = ssh
        self.no_cache        = no_cache
        self.memory_gb       = memory_gb
        # Detect CPU count at init time; fall back to 1 if detection fails.
        self.cpu_count       = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
        self.artifact_subpath = artifact_subpath

        # Live async process handle — set during run(), cleared after wait().
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._aborted = False


    async def run(self) -> DriverResult:
        """
        Launch ``docker build`` and stream its output asynchronously.

        Uses ``asyncio.create_subprocess_exec`` so the event loop is never
        blocked while Docker builds. Every stdout/stderr line is sanitised
        by ``_redact_line()`` before being logged, so build commands that
        accidentally echo secrets cannot leak them to the terminal.

        Returns:
            DriverResult(success=True, output={"image_tag": ..., "stdout": ..., "artifact_path": ...})

        Raises:
            EnvError:   If the Dockerfile is missing before the build starts.
            BuildError: If the build fails, is OOM-killed, or is aborted.
            EnvError:   If the Docker binary is missing (exit 127).
        """
        # --- Feature 5: Dockerfile existence validation ---
        # Catch the missing Dockerfile BEFORE handing off to Docker so the
        # operator gets a clean EnvError, not a cryptic "unable to prepare
        # context: the Dockerfile cannot be found" from the daemon.
        if not self.dockerfile.exists():
            raise EnvError(
                f"Dockerfile not found at '{self.dockerfile}'. "
                "Verify the staging directory and dockerfile path are correct.",
                context={"dockerfile": str(self.dockerfile)},
            )

        # --- Static Dockerfile layer analysis ---
        # Run the analyser BEFORE spawning any subprocess so that a
        # cache anti-pattern (COPY . before npm install) is caught as a
        # clean EnvError rather than a slow cache-miss build failure.
        DockerfileAnalyzer(self.dockerfile).analyze()

        cmd = self._build_command()
        safe_cmd = _redact_build_args(cmd)
        logger.info("CONSTRUCTION: Launching P3 build stream → %s", safe_cmd)

        proc = await self._build_popen(cmd)
        self._proc = proc

        # Stream stdout and stderr line-by-line, redacting secrets in real time.
        stdout_lines: list[str] = []
        try:
            stdout_lines = await self._stream_output(proc)
        finally:
            # Always clear the process handle, even if _stream_output raises.
            self._proc = None

        returncode = proc.returncode
        if returncode != 0:
            stderr_tail = "\n".join(stdout_lines[-20:]) if stdout_lines else ""
            self._map_exit_code(returncode, stderr_tail, aborted=self._aborted)

        # --- Feature 6: Artifact path handoff ---
        # Resolve the expected binary location. The Supervisor passes this
        # directly to AuditChain.run() to close the P3 → audit contract.
        if self.artifact_subpath:
            artifact_path = str(self.stage_path / self.artifact_subpath)
        else:
            # Fall back to the stage root if no specific binary path is known.
            artifact_path = str(self.stage_path)

        return DriverResult(
            success=True,
            output={
                "image_tag":     self.image_tag,
                "stdout":        "\n".join(stdout_lines),
                "artifact_path": artifact_path,
            },
        )

    async def terminate(self) -> None:
        """
        Surgically abort the running build in response to a P1 Hamilton Alarm.

        Sends SIGTERM to the BuildKit process group (POSIX) or calls
        ``Process.kill()`` on Windows. If the process group does not exit
        within ``_SIGKILL_TIMEOUT`` seconds, SIGKILL is sent as a last resort.

        This is idempotent — calling it when no build is running is a no-op.

        NOTE — async design: terminate() is async so it can be awaited from
        within the Supervisor's TaskGroup cancellation handler without blocking
        the event loop during the SIGKILL grace period sleep.
        """
        if self._proc is None:
            logger.debug("CONSTRUCTION: terminate() called but no active build process.")
            return

        self._aborted = True
        pid = self._proc.pid
        logger.warning("CONSTRUCTION: P1 Alarm — terminating BuildKit process (PID=%d)", pid)

        if hasattr(os, "killpg") and hasattr(os, "getpgid"):
            # POSIX: kill the entire process group so BuildKit child processes
            # (spawners, cache writers) are also reaped immediately.
            # start_new_session=True (used in _build_popen) ensures the child
            # has its own PGID, so getpgid() always returns a distinct group.
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
                # Give the process group time to clean up gracefully.
                deadline = time.monotonic() + _SIGKILL_TIMEOUT
                while time.monotonic() < deadline:
                    if self._proc.returncode is not None:
                        break
                    await asyncio.sleep(0.1)
                if self._proc.returncode is None:
                    logger.warning("CONSTRUCTION: SIGTERM ignored — escalating to SIGKILL.")
                    os.killpg(pgid, getattr(signal, "SIGKILL", 9))
            except ProcessLookupError:
                # Process already exited between our check and the kill call.
                logger.debug("CONSTRUCTION: Process %d already exited.", pid)
        else:
            # Windows fallback: kills the process directly.
            self._proc.kill()

        logger.info("CONSTRUCTION: BuildKit process group reaped.")
        self._proc = None

    def check_health(self) -> DriverResult:
        """
        Verify Docker is available, daemon is reachable, and running rootless.

        Raises:
            EnvError: If Docker is absent, the daemon is down, or rootless
                      mode is not active (security requirement per README).
        """
        import json as _json

        if not shutil.which("docker"):
            raise EnvError(
                "docker binary not found on PATH. "
                "Install Docker from https://docs.docker.com/get-docker/",
                context={"tool": "docker"},
            )

        info_result = self._run_subprocess(["docker", "info", "--format", "{{json .}}"])
        if info_result.returncode != 0:
            raise EnvError(
                f"Docker daemon is not reachable: {info_result.stderr.strip()}",
                context={"tool": "docker", "exit_code": info_result.returncode},
            )

        try:
            info = _json.loads(info_result.stdout)
            security_options: list = info.get("SecurityOptions", [])
            # On Windows, Docker Desktop uses WSL2 as its backend.
            # The Windows-side daemon never reports "rootless" in SecurityOptions
            # even when the underlying engine is rootless. Skip the check on Windows.
            if platform.system() != "Windows":
                if not any("rootless" in opt for opt in security_options):
                    raise EnvError(
                        "Docker is running as root. Hamilton-Ops requires rootless mode. "
                        "See https://docs.docker.com/engine/security/rootless/",
                        context={"security_options": security_options},
                    )
            else:
                logger.info(
                    "CONSTRUCTION: Windows detected — rootless check skipped "
                    "(Docker Desktop uses WSL2 backend)."
                )
        except _json.JSONDecodeError:
            logger.warning("CONSTRUCTION: Could not parse 'docker info' JSON — rootless check skipped.")

        version_result = self._run_subprocess(["docker", "version", "--format", "{{.Server.Version}}"])
        version = version_result.stdout.strip() if version_result.returncode == 0 else "unknown"
        return DriverResult(success=True, output={"version": version})


    def _build_command(self) -> list[str]:
        """
        Assemble the full ``docker build`` argument list.

        Inserts BuildKit flags in this order:
          1. Core flags (--file, --tag, --no-cache)
          2. Cache flags (--cache-from, --cache-to, --build-arg CACHE_ID)
          3. Secret mounts (--secret id=...)
          4. SSH agent  (--ssh default)
          5. Resource guardrails (--memory, --cpu-period, --cpu-quota)
          6. Build context (staging directory — always last)
        """
        cmd: list[str] = [
            "docker", "build",
            "--file",  str(self.dockerfile),
            "--tag",   self.image_tag,
        ]

        if self.no_cache:
            cmd.append("--no-cache")

        # --- Cache layer ---
        if self.cache_ref and not self.no_cache:
            cmd += ["--cache-from", f"type=registry,ref={self.cache_ref}"]
            cmd += ["--cache-to",   f"type=registry,ref={self.cache_ref},mode=max"]

        if self.project_hash:
            cmd += ["--build-arg", f"CACHE_ID={self.project_hash}"]

        # --- Secret mounts (BuildKit-native) ---
        for secret_spec in self.secrets:
            cmd += ["--secret", secret_spec]

        # --- SSH agent ---
        if self.ssh:
            cmd += ["--ssh", "default"]

        # --- Feature 4: Resource guardrails (Pillar E) ---
        # Cap memory to prevent OOM cascades on shared CI hosts.
        cmd += ["--memory", f"{self.memory_gb}g"]
        # Cap CPU using period/quota so BuildKit cannot monopolise all cores.
        # cpu_quota = cpu_count * period means the build gets cpu_count full cores.
        cmd += ["--cpu-period", "100000", "--cpu-quota", str(self.cpu_count * 100000)]

        # Build context must be last — always the immutable staging directory.
        cmd.append(str(self.stage_path))
        return cmd


    @staticmethod
    def _map_exit_code(code: int, stderr: str, aborted: bool = False) -> None:
        """
        Translate a non-zero Docker exit code into the correct Hamilton signal.

        Raises:
            EnvError:   On exit 127 (binary not found).
            BuildError: On all other failures, with OOM or abort context.
        """
        if code == _EXIT_NOT_FOUND:
            raise EnvError(
                "docker binary not found during build execution (exit 127). "
                "Pre-flight health check should have caught this.",
                context={"exit_code": code},
            )
        if code == _EXIT_OOM:
            raise BuildError(
                "BuildKit process was OOM-killed (exit 137). "
                "Increase Docker memory limit or simplify the build stages.",
                context={"exit_code": code, "oom": True},
            )
        # Negative return codes on POSIX typically mean the process was killed
        # by a signal (e.g., SIGTERM = -15, SIGKILL = -9).
        if code < 0 or aborted:
            raise BuildError(
                f"BuildKit process terminated (likely a P1 Abort). "
                f"Exit code: {code}.",
                context={"exit_code": code, "aborted": True},
            )
        raise BuildError(
            f"docker build failed with exit code {code}: {stderr.strip()}",
            context={"exit_code": code, "stderr": stderr},
        )

    async def _build_popen(self, cmd: list[str]) -> asyncio.subprocess.Process:
        """
        Create the asyncio ``Process`` object for the build.

        ``start_new_session=True`` is equivalent to ``preexec_fn=os.setsid``
        — it moves the process into its own session (and therefore its own
        process group), enabling ``os.killpg`` to reap BuildKit child processes
        without touching the Python host process.

        Using ``asyncio.create_subprocess_exec`` instead of ``subprocess.Popen``
        ensures the event loop is not blocked while Docker builds, which is
        critical for the Supervisor's TaskGroup to remain responsive to P1
        cancellation signals during long builds.
        """
        return await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # merge stderr into stdout stream
            start_new_session=True,
        )

    async def _stream_output(self, proc: asyncio.subprocess.Process) -> list[str]:
        """
        Read the process stdout line-by-line, redacting secrets before logging.

        Returns the accumulated (redacted) lines for inclusion in DriverResult.
        """
        lines: list[str] = []
        if proc.stdout is None:
            await proc.wait()
            return lines

        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            # --- Feature 3: Stream sanitisation ---
            # Redact any line that matches a known secret pattern before
            # it reaches the terminal or log aggregator.
            safe_line = _redact_line(line)
            logger.debug("CONSTRUCTION [BUILD]: %s", safe_line)
            lines.append(safe_line)

        await proc.wait()
        return lines

    def _run_subprocess(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """
        Thin wrapper around subprocess.run — used for health checks only.
        Patch this in tests for check_health() coverage.
        """
        return subprocess.run(cmd, capture_output=True, text=True)


def _redact_build_args(cmd: list[str]) -> list[str]:
    """
    Return a copy of ``cmd`` with sensitive ``--build-arg`` values redacted.

    For each ``--build-arg KEY=VALUE`` pair where KEY matches a known secret
    pattern, VALUE is replaced with ``***REDACTED***``. The KEY itself is
    preserved so the operator knows which argument was sanitised.

    This function operates on the command *list* so no shell-string parsing
    is required — the same safety philosophy as the rest of the driver system.

    Example::

        _redact_build_args(["docker", "build", "--build-arg", "DB_PASSWORD=hunter2"])
        # → ["docker", "build", "--build-arg", "DB_PASSWORD=***REDACTED***"]
    """
    redacted = list(cmd)
    i = 0
    while i < len(redacted):
        if redacted[i] == "--build-arg" and i + 1 < len(redacted):
            arg_value = redacted[i + 1]
            if "=" in arg_value:
                key, _, value = arg_value.partition("=")
                if _SECRET_KEY_PATTERN.search(key):
                    redacted[i + 1] = f"{key}=***REDACTED***"
        i += 1
    return redacted


def _redact_line(line: str) -> str:
    """
    Redact secret patterns from a single build output line.

    Applied to every stdout/stderr line produced by the Docker build process
    so that a ``RUN echo $DB_PASSWORD`` or a ``printenv`` command cannot leak
    credentials to the terminal or log aggregator.

    Uses ``re.sub`` on ``_SECRET_LINE_PATTERN`` to replace the entire
    ``KEY=VALUE`` or ``KEY: VALUE`` match with ``KEY=***REDACTED***``,
    preserving the key name for auditability.

    Example::

        _redact_line("Build step: DB_PASSWORD=hunter2 injected")
        # → "Build step: DB_PASSWORD=***REDACTED*** injected"
    """
    def _replace(match: re.Match) -> str:
        # Preserve only the key portion (group 1); redact the value.
        return f"{match.group(1)}=***REDACTED***"

    return _SECRET_LINE_PATTERN.sub(_replace, line)
