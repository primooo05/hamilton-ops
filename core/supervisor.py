"""
Hamilton-Ops Supervisor — Flight Computer
==========================================

The Supervisor is the mission controller. It owns the full lifecycle:

    Pre-flight  → validate environment, stage source, verify registry
    Launch      → start P1 (k6), P2 (linter), P3 (Docker) concurrently
    Monitor     → route exceptions by priority, trigger Hamilton Kill on P1 alarm
    Post-flight → audit chain, SBOM, SHA256 verify, mark artifact read-only
    Cleanup     → always reap staging, containers, subprocesses (finally block)

Async cancellation contract (Python 3.11+ TaskGroup):
    When a TaskGroup child raises, the group injects CancelledError into siblings.
    This means if P1 raises HamiltonAlarm, P3 receives CancelledError — NOT
    BuildError. The forensic log must distinguish "P3 cancelled by P1" from
    "P3 failed independently". We track the kill cause in self._kill_cause and
    stamp each stream's exit in ForensicReport.stream_results.

P3 isolation rule:
    BuildError from P3 must NOT cancel P1/P2. To achieve this we catch
    BuildError *inside* _run_p3_task so it never escapes the TaskGroup boundary
    as an uncaught exception — it is converted into a ForensicReport entry.

--strict mode:
    The driver raises QualityViolation; the Supervisor decides escalation.
    If strict=True and QualityViolation is caught, _hamilton_kill() fires.
    The driver never touches strict logic — that decision lives here only.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.exceptions import (
    BuildError,
    EnvError,
    HamiltonAlarm,
    QualityViolation,
    StagingError,
)
from core.priorities import Priority
from core.stage import StagingContext
from core.state import FlightState, StateMachine
from audit.chain import (
    AuditChain,
    BinaryDiscoveryStep,
    BuildToolLeakStep,
    SBOMGenerationStep,
    SecretScannerStep,
)

# --- Python 3.10 Compatibility Layer ---
import sys

if sys.version_info >= (3, 11):
    from asyncio import TaskGroup
    _ExceptionGroup = (ExceptionGroup, BaseExceptionGroup)
else:
    # Use standard Exception as a catch-all if ExceptionGroup isn't available
    try:
        from exceptiongroup import ExceptionGroup, BaseExceptionGroup
        _ExceptionGroup = (ExceptionGroup, BaseExceptionGroup)
    except ImportError:
        class ExceptionGroup(Exception):
            """Fallback for ExceptionGroup."""
            def __init__(self, message, exceptions):
                super().__init__(message)
                self.exceptions = exceptions
        _ExceptionGroup = (ExceptionGroup,)

    # TaskGroup shim for 3.10
    try:
        from asyncio import TaskGroup
    except ImportError:
        class TaskGroup:
            def __init__(self):
                self._tasks = []
            async def __aenter__(self): return self
            async def __aexit__(self, et, ev, tb):
                if not self._tasks: return
                done, pending = await asyncio.wait(self._tasks, return_when=asyncio.FIRST_EXCEPTION)
                for t in pending: t.cancel()
                if pending: await asyncio.gather(*pending, return_exceptions=True)
                exceptions = [t.exception() for t in done if t.exception()]
                if exceptions: raise ExceptionGroup("TaskGroup failure", exceptions)
            def create_task(self, coro, name=None):
                t = asyncio.create_task(coro, name=name)
                self._tasks.append(t)
                return t

logger = logging.getLogger("hamilton.supervisor")


@dataclass
class StreamResult:
    """
    Telemetry snapshot for a single priority stream.

    Attributes:
        name:         Human label (e.g. "P1:Validation").
        outcome:      "success" | "failed" | "cancelled" | "skipped".
        exception:    The raw exception if the stream failed, else None.
        cancelled_by: Name of the stream that triggered this cancellation
                      (populated by the Supervisor, not the driver).
        duration_s:   Wall-clock seconds the stream ran before exiting.
    """
    name: str
    outcome: str = "skipped"
    exception: Optional[Exception] = None
    cancelled_by: Optional[str] = None
    duration_s: float = 0.0


@dataclass
class ForensicReport:
    """
    Immutable audit trail written regardless of mission outcome.

    Designed so that a post-mortem analyst can answer:
        - Which streams ran?
        - What signal was raised and by whom?
        - What were the P1 telemetry numbers?
        - Was cleanup successful?

    Attributes:
        project:        Name of the project being built.
        started_at:     Unix timestamp when ship() was called.
        ended_at:       Unix timestamp when cleanup completed.
        flight_state:   Final FlightState value as a string.
        stream_results: Per-stream outcome (P1, P2, P3).
        kill_cause:     Which exception triggered Hamilton Kill (if any).
        p1_metrics:     Raw k6 telemetry dict (p95_ms, p99_ms, error_rate).
        audit_passed:   True if the post-flight audit chain passed.
        cleanup_ok:     True if _reap_all completed without error.
        strict_mode:    Whether --strict was active during this mission.
    """
    project: str
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    flight_state: str = FlightState.IDLE.name
    stream_results: dict[str, StreamResult] = field(default_factory=dict)
    kill_cause: Optional[str] = None
    p1_metrics: dict = field(default_factory=dict)
    audit_passed: bool = False
    cleanup_ok: bool = False
    strict_mode: bool = False

@dataclass
class SupervisorConfig:
    """
    Mission parameters passed to the Supervisor at construction time.

    Attributes:
        project_name:   Logical name for logging and forensics.
        source_path:    Absolute path to the project root.
        image_tag:      Docker image tag for the P3 Construction stream.
        binary_path:    Expected location of the compiled artifact for audit.
        k6_script:      Path to the k6 JavaScript test script.
        strict:         If True, QualityViolation escalates to Hamilton Kill.
        cache_ref:      Optional BuildKit registry cache reference.
        project_hash:   Optional lock-file hash for BuildKit cache scoping.
        secrets:        Optional list of BuildKit --secret specs.
        linter_cmd:     Optional custom linter command list (default: flake8).
    """
    project_name: str
    source_path: str | Path
    image_tag: str
    binary_path: str | Path
    k6_script: str | Path
    strict: bool = False
    cache_ref: Optional[str] = None
    project_hash: Optional[str] = None
    secrets: Optional[list[str]] = None
    linter_cmd: Optional[list[str]] = None
    concurrency_strategy: str = "full"  # "full", "reduced", "minimal"
    docker_memory_gb: int = 4


class HamiltonSupervisor:
    """
    Mission controller for a Hamilton-Ops build run.

    Owns the full lifecycle from pre-flight validation through post-flight
    audit and cleanup. Delegates driver invocations to the DriverRegistry so
    that the Supervisor is decoupled from tool-specific implementation details.

    Key design choices:
        - ``asyncio.TaskGroup`` (Python 3.11+) for concurrent P1/P2/P3 streams.
        - P3 BuildError is caught *inside* its task wrapper so siblings survive.
        - ``except*`` handles ExceptionGroup from TaskGroup for P1/P2 signals.
        - ``_hamilton_kill`` is idempotent — safe to call multiple times.
        - All cleanup happens in a ``finally`` block — never only in except.
        - The ConstructionDriver reference is stored so ``terminate()`` can be
          called to reap the Docker subprocess when a P1 Alarm fires.
    """

    def __init__(self, config: SupervisorConfig, registry) -> None:
        """
        Args:
            config:   Mission parameters (paths, flags, project name).
            registry: DriverRegistry instance with k6, linter, docker registered.
        """
        self._config = config
        self._registry = registry
        self._fsm = StateMachine()
        self._report = ForensicReport(
            project=config.project_name,
            strict_mode=config.strict,
        )

        # Live driver references used by _hamilton_kill() for subprocess reaping.
        # These are set in _run_p3_task() before the blocking call so that the
        # kill handler always has a handle even if P1 fires mid-build.
        self._construction_driver = None

        # Guards _hamilton_kill against double-execution (P1 + strict-P2 race).
        self._kill_fired = False

        # Set to the active StagingContext in _pre_flight; guarded in _reap_all.
        self._staging_ctx = None

    async def ship(self) -> ForensicReport:
        """
        Execute the full mission lifecycle.

        Returns:
            ForensicReport describing the completed (or aborted) mission.

        The structure is intentionally rigid:
            try:
                pre-flight
                launch
                post-flight (only on success)
            finally:
                reap all  ← ALWAYS runs, regardless of outcome
                write forensic log
        """
        # Re-initialize mission state so each ship() call is independent.
        # This makes the supervisor safely re-entrant and ensures the returned
        # ForensicReport is a fresh object, not a mutation of a previous run.
        self._report = ForensicReport(
            project=self._config.project_name,
            strict_mode=self._config.strict,
            started_at=time.time(),
        )
        self._fsm = StateMachine()
        self._kill_fired = False
        self._staging_ctx = None
        self._construction_driver = None
        self._p3_artifact_path = None  # populated by _run_p3_task on success

        try:
            stage_path = await self._pre_flight()
            await self._launch(stage_path)

            # Post-flight runs only if launch succeeded (FSM is still healthy).
            if self._fsm.is_healthy:
                await self._post_flight()

        except EnvError as exc:
            # EnvError is a hard stop — the environment is untrustworthy.
            # It fires before or outside the TaskGroup so plain except applies.
            logger.critical("SUPERVISOR: Environment error — hard stop. %s", exc)
            self._fsm.handle_signal(exc, Priority.P1_VALIDATION)
            self._report.kill_cause = f"EnvError: {exc}"

        except StagingError as exc:
            logger.critical("SUPERVISOR: Staging failed — cannot proceed. %s", exc)
            self._fsm.handle_signal(exc, Priority.P1_VALIDATION)
            self._report.kill_cause = f"StagingError: {exc}"

        finally:
            await self._reap_all()
            self._report.ended_at = time.time()
            self._report.flight_state = self._fsm.current.name
            self._write_forensic_log()

        return self._report

    async def _pre_flight(self) -> Path:
        """
        Validate the environment and create the immutable staging snapshot.

        Steps:
            1. Verify all three Essential Pillars are in the Registry.
            2. Run driver health checks (k6, linter, docker) to catch missing
               binaries before any stream launches.
            3. Create the immutable staging snapshot via StagingContext.

        Returns:
            Path to the active staging directory.

        Raises:
            EnvError:    If any driver binary is absent or broken.
            StagingError: If the staging snapshot cannot be created.
        """
        logger.info("SUPERVISOR [PRE-FLIGHT]: Verifying registry completeness.")
        self._fsm.transition_to(FlightState.STAGING)

        # Verify all three pillars are registered — fail fast before any I/O.
        self._registry.verify_completeness()

        # Run health checks on all three drivers before staging begins.
        # EnvError from any check surfaces here and terminates in ship().
        logger.info("SUPERVISOR [PRE-FLIGHT]: Running driver health checks.")
        k6_driver_cls = self._registry.get("k6")
        linter_driver_cls = self._registry.get("linter")
        docker_driver_cls = self._registry.get("docker")

        await asyncio.to_thread(lambda: k6_driver_cls.check_health())
        await asyncio.to_thread(lambda: linter_driver_cls.check_health())
        await asyncio.to_thread(lambda: docker_driver_cls.check_health())

        # Stage the source — this creates an immutable snapshot.
        logger.info("SUPERVISOR [PRE-FLIGHT]: Staging source at %s", self._config.source_path)
        ctx = StagingContext(self._config.source_path)
        stage_path = await ctx.__aenter__()

        # Store context so _reap_all() can call __aexit__ in the finally block.
        # Using the raw context rather than an async-with so that cleanup is
        # explicitly controlled and always deferred to _reap_all().
        self._staging_ctx = ctx
        return stage_path

    async def _launch(self, stage_path: Path) -> None:
        """
        Start P1, P2, P3 concurrently inside an asyncio.TaskGroup.

        Cancellation contract:
            TaskGroup cancels all siblings when any child raises. This means
            if P1 raises HamiltonAlarm, P3's task receives CancelledError.
            We distinguish "P3 cancelled externally" vs "P3 failed on its own"
            by checking self._kill_fired inside _run_p3_task.

        P3 isolation:
            BuildError is caught *inside* _run_p3_task so it does NOT escape
            into the TaskGroup as an uncaught error. P1/P2 are unaffected.

        Raises:
            ExceptionGroup containing HamiltonAlarm — triggers Hamilton Kill.
            ExceptionGroup containing QualityViolation — triggers warn / strict.
        """
        self._fsm.transition_to(FlightState.SHIPPING)
        logger.info("SUPERVISOR [LAUNCH]: Starting P1/P2/P3 streams concurrently.")

        try:
            async with TaskGroup() as tg:
                if self._config.concurrency_strategy == "full":
                    # P1 + P2 + P3 (Parallel)
                    tg.create_task(self._run_p1_task(stage_path), name="P1:Validation")
                    tg.create_task(self._run_p2_task(stage_path), name="P2:Quality")
                    tg.create_task(self._run_p3_task(stage_path), name="P3:Construction")
                
                elif self._config.concurrency_strategy == "reduced":
                    # P1 + P2 (Parallel), then P3
                    pass # Handled below
                
                elif self._config.concurrency_strategy == "minimal":
                    # Sequential: P1, then P2, then P3
                    pass # Handled below

            # Adaptive logic for reduced/minimal strategies
            if self._config.concurrency_strategy == "reduced":
                # Start P1 and P2 in parallel, wait for them, then P3
                async with TaskGroup() as tg:
                    tg.create_task(self._run_p1_task(stage_path), name="P1:Validation")
                    tg.create_task(self._run_p2_task(stage_path), name="P2:Quality")
                # After P1/P2 finish (and pass), start P3
                await self._run_p3_task(stage_path)

            elif self._config.concurrency_strategy == "minimal":
                # Fully sequential
                await self._run_p1_task(stage_path)
                await self._run_p2_task(stage_path)
                await self._run_p3_task(stage_path)

        except _ExceptionGroup as eg:
            # Python 3.10 compatible except* logic
            # Extract specific exceptions and leave the rest in a new group.
            
            p1_alarms = []
            p2_violations = []
            other_exceptions = []
            
            def process_group(group):
                for e in getattr(group, "exceptions", []):
                    if isinstance(e, HamiltonAlarm):
                        p1_alarms.append(e)
                    elif isinstance(e, QualityViolation):
                        p2_violations.append(e)
                    elif hasattr(e, "exceptions"):
                        process_group(e)
                    else:
                        other_exceptions.append(e)
            
            process_group(eg)
            
            # P1 Alarms have highest priority. Kill everything immediately.
            if p1_alarms:
                alarm = p1_alarms[0]
                logger.critical(
                    "SUPERVISOR [MONITOR]: HamiltonAlarm received — executing Hamilton Kill. %s", alarm
                )
                self._report.kill_cause = f"HamiltonAlarm: {alarm}"
                self._fsm.handle_signal(alarm, Priority.P1_VALIDATION)
                await self._hamilton_kill(cause="P1:Validation")
                
            # If no P1 Alarm, check for QualityViolation
            elif p2_violations:
                violation = p2_violations[0]
                if self._config.strict:
                    logger.error(
                        "SUPERVISOR [MONITOR]: QualityViolation in --strict mode — executing Hamilton Kill."
                    )
                    self._report.kill_cause = f"QualityViolation(strict): {violation}"
                    self._fsm.handle_signal(violation, Priority.P1_VALIDATION)
                    await self._hamilton_kill(cause="P1:Validation")
                else:
                    logger.warning(
                        "SUPERVISOR [MONITOR]: QualityViolation detected (non-strict) — continuing mission."
                    )
                    self._fsm.handle_signal(violation, Priority.P2_QUALITY)
                    # If we aren't killing, we must re-raise any other exceptions
                    if other_exceptions:
                        raise _ExceptionGroup[0]("Unhandled exceptions", other_exceptions) from None
            
            else:
                # No Hamilton signals found; re-raise the entire group.
                raise eg from None

    async def _run_p1_task(self, stage_path: str | Path) -> None:
        """
        P1 Validation stream — wraps K6Driver.run() in a thread.

        Any HamiltonAlarm or EnvError raised here escapes the TaskGroup
        and triggers ExceptionGroup handling in _launch(). This is intentional:
        P1 failures are always system-wide emergencies.
        """
        result_slot = StreamResult(name="P1:Validation")
        self._report.stream_results["P1:Validation"] = result_slot
        start = time.monotonic()

        try:
            k6_driver_factory = self._registry.get("k6")
            k6_driver = k6_driver_factory(stage_path=stage_path)
            result = await asyncio.to_thread(k6_driver.run)
            result_slot.outcome = "success"
            # Store raw k6 telemetry for forensic report.
            if result and result.output:
                self._report.p1_metrics = result.output

        except (HamiltonAlarm, EnvError) as exc:
            result_slot.outcome = "failed"
            result_slot.exception = exc
            raise  # Escape the TaskGroup to trigger _launch() except* handler.

        except asyncio.CancelledError:
            # P1 was cancelled — only happens if P2 or P3 killed the group first.
            result_slot.outcome = "cancelled"
            result_slot.cancelled_by = "external"
            raise

        finally:
            result_slot.duration_s = time.monotonic() - start

    async def _run_p2_task(self, stage_path: str | Path) -> None:
        """
        P2 Quality stream — wraps LinterDriver.run() in a thread.

        QualityViolation escapes the TaskGroup so the except* handler in
        _launch() can decide whether to warn or escalate (--strict).

        EnvError also escapes — a broken linter environment is untrustworthy.
        """
        result_slot = StreamResult(name="P2:Quality")
        self._report.stream_results["P2:Quality"] = result_slot
        start = time.monotonic()

        try:
            linter_driver_factory = self._registry.get("linter")
            linter_driver = linter_driver_factory(stage_path=stage_path)
            await asyncio.to_thread(linter_driver.run)
            result_slot.outcome = "success"

        except (QualityViolation, EnvError) as exc:
            result_slot.outcome = "failed"
            result_slot.exception = exc
            raise  # Escape to _launch() except* handler.

        except asyncio.CancelledError:
            # P2 was cancelled by a P1 HamiltonAlarm killing the group.
            result_slot.outcome = "cancelled"
            result_slot.cancelled_by = self._report.kill_cause or "P1:Validation"
            raise

        finally:
            result_slot.duration_s = time.monotonic() - start

    async def _run_p3_task(self, stage_path: Path) -> None:
        """
        P3 Construction stream — calls ConstructionDriver.run() directly (async).

        CRITICAL ISOLATION RULE:
            BuildError is caught HERE and never escapes the TaskGroup. This
            ensures P1 and P2 are unaffected by a Docker build failure.

        CANCELLATION AWARENESS:
            If CancelledError arrives, it means P1 fired a HamiltonAlarm and
            the TaskGroup is tearing down. We await terminate() directly on the
            driver to kill the Docker subprocess (asyncio.CancelledError cancels
            the Python Task, not the os-level subprocess). We then log
            "cancelled_by" so the forensic report shows the true cause.

        ASYNC DESIGN:
            ConstructionDriver.run() is async and uses asyncio.create_subprocess_exec
            internally, so this task does NOT block the event loop during the build.
            The TaskGroup remains responsive to P1 signals at all times.
        """
        result_slot = StreamResult(name="P3:Construction")
        self._report.stream_results["P3:Construction"] = result_slot
        start = time.monotonic()

        try:
            docker_driver_factory = self._registry.get("docker")
            driver = docker_driver_factory(stage_path=stage_path)
        except Exception as e:
            result_slot.outcome = "failed"
            result_slot.exception = e
            raise
        # Store reference before any await — kill handler needs it.
        self._construction_driver = driver

        try:
            # Direct await — driver.run() is async and non-blocking.
            p3_result = await driver.run()
            result_slot.outcome = "success"

            # Propagate artifact_path so _post_flight() can hand it to AuditChain.
            # Stored on the supervisor so _post_flight() can read it without
            # threading result objects through the call chain.
            if p3_result and p3_result.output:
                self._p3_artifact_path = p3_result.output.get("artifact_path")

        except BuildError as exc:
            # P3-only failure — log it, record it, do NOT re-raise.
            # P1 and P2 continue unaffected.
            logger.error("SUPERVISOR [P3]: BuildError — aborting construction only. %s", exc)
            self._fsm.handle_signal(exc, Priority.P3_CONSTRUCTION)
            result_slot.outcome = "failed"
            result_slot.exception = exc
            # Do not raise — this is the isolation boundary.

        except asyncio.CancelledError:
            # Externally cancelled — most likely by a P1 HamiltonAlarm.
            cause = self._report.kill_cause or "P1:Validation"
            logger.warning(
                "SUPERVISOR [P3]: Task cancelled (cause=%s) — reaping Docker subprocess.", cause
            )
            # Await terminate() to surgically reap the BuildKit process group.
            # CancelledError only stops the asyncio Task; the subprocess keeps
            # running until explicitly killed.
            await driver.terminate()
            result_slot.outcome = "cancelled"
            result_slot.cancelled_by = cause
            raise  # Must re-raise CancelledError so TaskGroup tears down cleanly.

        except EnvError as exc:
            # Docker binary missing mid-build — escalate like P1.
            logger.critical("SUPERVISOR [P3]: EnvError from docker — hard stop. %s", exc)
            result_slot.outcome = "failed"
            result_slot.exception = exc
            raise HamiltonAlarm(str(exc), context={"source": "P3:EnvError"}) from exc

        finally:
            result_slot.duration_s = time.monotonic() - start
            self._construction_driver = None

    async def _hamilton_kill(self, cause: str = "unknown") -> None:
        """
        Idempotent emergency stop procedure.

        Terminates the Docker subprocess (P3) immediately via the driver's
        async terminate() method, which uses os.killpg on POSIX to reap the
        entire BuildKit process tree — not just the top-level docker process.

        Safe to call multiple times — subsequent calls are no-ops.

        Args:
            cause: Label of the stream that triggered the kill, for forensics.
        """
        if self._kill_fired:
            logger.debug("SUPERVISOR: _hamilton_kill already fired — skipping duplicate call.")
            return

        self._kill_fired = True
        logger.critical("SUPERVISOR: *** HAMILTON KILL *** triggered by %s", cause)

        # Await terminate() so the SIGTERM → SIGKILL grace period runs
        # asynchronously without blocking the event loop.
        if self._construction_driver is not None:
            logger.warning("SUPERVISOR: Terminating P3 Docker subprocess (PID reaping).")
            await self._construction_driver.terminate()

    async def _post_flight(self) -> None:
        """
        Run the Binary Audit Chain and mark the artifact read-only on success.

        Only called when the FSM is healthy (no P1 alarm fired).

        Steps (Chain of Responsibility):
            1. BinaryDiscoveryStep  — verify binary exists, record SHA256
            2. SecretScannerStep    — regex-scan for embedded secrets
            3. BuildToolLeakStep    — detect gcc/mvn/npm leaks
            4. SBOMGenerationStep   — generate SBOM via Syft

        On success, the artifact is chmod'd to 0o444 (read-only).

        ARTIFACT HANDOFF CONTRACT:
            The binary_path passed to AuditChain is resolved from the P3
            DriverResult's ``artifact_path`` key when available. This closes
            the design gap between construction.py and audit/chain.py —
            P3 now explicitly tells the Supervisor where the artifact landed
            rather than relying on the static SupervisorConfig.binary_path.
            If P3 did not report an artifact_path (e.g., it was cancelled),
            we fall back to the config value so post-flight always has a path.

        NOTE: StagingError is deliberately caught here because AuditChain raises
        it for a missing binary. That must NOT propagate to ship()'s outer
        except StagingError handler (which is reserved for pre-flight failures).
        """
        from core.exceptions import AuditFailure, StagingError as _StagingError

        self._fsm.transition_to(FlightState.VERIFYING)

        # Prefer the artifact_path reported by P3; fall back to config.
        # _p3_artifact_path is set in _run_p3_task on successful build.
        raw_path = getattr(self, "_p3_artifact_path", None) or self._config.binary_path
        binary_path = Path(raw_path)

        logger.info("SUPERVISOR [POST-FLIGHT]: Starting audit chain on %s", binary_path)

        chain = AuditChain([
            BinaryDiscoveryStep(),
            SecretScannerStep(),
            BuildToolLeakStep(),
            SBOMGenerationStep(),
        ])

        try:
            audit_report = await asyncio.to_thread(chain.run, binary_path)
            self._report.audit_passed = audit_report.passed

            if audit_report.passed:
                # Mark artifact read-only — no further tampering allowed.
                _mark_readonly(binary_path)
                logger.info("SUPERVISOR [POST-FLIGHT]: Artifact signed and locked read-only.")
                self._fsm.transition_to(FlightState.SUCCESS)
            else:
                logger.error("SUPERVISOR [POST-FLIGHT]: Audit chain did not pass.")

        except (AuditFailure, _StagingError) as exc:
            # StagingError from AuditChain = missing binary (P3 failed to deliver).
            # Treat this as an audit non-pass, NOT a pre-flight staging failure.
            logger.error("SUPERVISOR [POST-FLIGHT]: Audit failure — %s", exc)
            self._report.audit_passed = False


    async def _reap_all(self) -> None:
        """
        Unconditional cleanup — always called from the finally block in ship().

        Sequence (order matters — containers before staging directory):
            1. Terminate any lingering Docker subprocess.
            2. Remove temporary Docker containers (best-effort).
            3. Clean up the staging directory via StagingContext.__aexit__.

        Sets report.cleanup_ok = True only if no step raised.
        """
        logger.info("SUPERVISOR [CLEANUP]: Reaping all resources.")

        try:
            # Final check on construction driver — may have already
            # been reaped by _hamilton_kill(), but terminate() is idempotent.
            if self._construction_driver is not None:
                logger.debug("SUPERVISOR [CLEANUP]: Late-reaping Docker subprocess.")
                await self._construction_driver.terminate()

            # Remove dangling Docker containers created by this build.
            await self._cleanup_containers()

            # Reap the staging directory.
            if self._staging_ctx is not None:
                await self._staging_ctx.__aexit__(None, None, None)

            self._report.cleanup_ok = True
            logger.info("SUPERVISOR [CLEANUP]: All resources reaped successfully.")

        except Exception as exc:
            # Cleanup errors are logged but never re-raised — the forensic
            # report records cleanup_ok=False for post-mortem analysis.
            logger.error("SUPERVISOR [CLEANUP]: Error during reap — %s", exc)
            self._report.cleanup_ok = False

    async def _cleanup_containers(self) -> None:
        """
        Remove dangling Docker containers tagged with this build's image_tag.

        Best-effort: a non-zero exit code is logged but not raised, because
        a failed container cleanup must never block the staging directory cleanup.
        """
        import subprocess

        tag = self._config.image_tag
        logger.debug("SUPERVISOR [CLEANUP]: Pruning dangling containers for tag=%s", tag)
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["docker", "ps", "-aq", "--filter", f"ancestor={tag}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            container_ids = result.stdout.strip().splitlines()
            if container_ids:
                await asyncio.to_thread(
                    subprocess.run,
                    ["docker", "rm", "-f"] + container_ids,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                logger.info("SUPERVISOR [CLEANUP]: Removed %d container(s).", len(container_ids))
        except Exception as exc:
            logger.warning("SUPERVISOR [CLEANUP]: Container cleanup failed (non-fatal): %s", exc)


    def _write_forensic_log(self) -> None:
        """
        Emit a structured forensic summary to the logger.

        Written regardless of mission outcome (success or abort) so that
        every run leaves an auditable trail. The structured format is
        machine-parseable for log aggregation pipelines.
        """
        duration = self._report.ended_at - self._report.started_at
        logger.info(
            "FORENSIC REPORT | project=%s | state=%s | duration=%.2fs | "
            "strict=%s | kill_cause=%s | audit=%s | cleanup=%s | "
            "p1=%s | p2=%s | p3=%s | p1_metrics=%s",
            self._report.project,
            self._report.flight_state,
            duration,
            self._report.strict_mode,
            self._report.kill_cause or "none",
            self._report.audit_passed,
            self._report.cleanup_ok,
            self._report.stream_results.get("P1:Validation", StreamResult("P1:Validation")).outcome,
            self._report.stream_results.get("P2:Quality", StreamResult("P2:Quality")).outcome,
            self._report.stream_results.get("P3:Construction", StreamResult("P3:Construction")).outcome,
            self._report.p1_metrics or "{}",
        )

def _mark_readonly(path: Path) -> None:
    """
    Remove write permissions from ``path`` (chmod 0o444).

    Makes the production artifact immutable on disk so that no post-audit
    process can tamper with it. Logs a warning if the path does not exist
    rather than raising — audit chain would have caught a missing binary first.
    """
    if not path.exists():
        logger.warning("SUPERVISOR: Cannot mark read-only — path does not exist: %s", path)
        return
    current = stat.S_IMODE(os.stat(path).st_mode)
    readonly = current & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    os.chmod(path, readonly)
    logger.debug("SUPERVISOR: %s marked read-only (mode=%o)", path, readonly)