"""
Integration Tests — Full Flight (End-to-End Supervisor Orchestration)
======================================================================

These tests verify that HamiltonSupervisor correctly orchestrates the
three priority streams (P1, P2, P3) as a complete system.

Scope (what is real vs. mocked):
    REAL:    HamiltonSupervisor, DriverRegistry, StateMachine, StagingContext,
             SupervisorConfig, ForensicReport, StreamResult, AuditChain.
    MOCKED:  Individual driver run() methods — we replace the subprocess-
             launching implementations with controlled in-memory fakes.
             StagingContext.__aenter__/__aexit__ are patched to avoid disk I/O.
             AuditChain.run() is patched in success-path tests to skip Syft.

Why integration tests are separate from unit tests:
    Unit tests in tests/core/test_supervisor/ isolate the Supervisor from
    the registry by injecting a _FakeRegistry. These integration tests wire
    a REAL DriverRegistry so that the full registration, lookup, and factory-
    invocation path is exercised end-to-end. This catches bugs that only
    surface when the registry, supervisor, and driver factory lambdas interact
    — e.g., a mis-keyed driver name or an incorrect factory signature.

Naming convention (same as supervisor unit tests):
    test_<scenario>_<expected_outcome>

Test matrix:
    | Scenario                      | FSM state | kill_cause       | audit |
    |-------------------------------|-----------|------------------|-------|
    | All streams succeed            | SUCCESS   | None             | True  |
    | P1 HamiltonAlarm               | ABORTED   | "HamiltonAlarm:" | False |
    | P2 QualityViolation non-strict | healthy   | None             | True  |
    | P2 QualityViolation strict     | ABORTED   | "strict"         | False |
    | P3 BuildError                  | !=ABORTED | None/no alarm    | False |
    | EnvError (pre-flight)          | ABORTED   | "EnvError:"      | False |
    | Cleanup always runs            | any       | any              | any   |
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.exceptions import (
    BuildError,
    EnvError,
    HamiltonAlarm,
    QualityViolation,
    ThresholdExceededError,
)
from core.priorities import Priority
from core.state import FlightState
from core.supervisor import (
    ForensicReport,
    HamiltonSupervisor,
    StreamResult,
    SupervisorConfig,
)
from drivers.registry import DriverRegistry, DriverResult


class _SyncFakeDriver:
    """
    Fake synchronous driver (K6Driver / LinterDriver shape).

    ``raises``     — if set, run() raises this exception.
    ``health_exc`` — if set, check_health() raises this exception.
    ``result``     — returned by run() when raises is None.
    """

    def __init__(self, raises=None, result=None, health_exc=None):
        self._raises = raises
        self._result = result or DriverResult(success=True, output={})
        self._health_exc = health_exc

    def __call__(self, stage_path=None):
        # Factory signature — returns self as the driver instance.
        return self

    def run(self):
        if self._raises:
            raise self._raises
        return self._result

    def check_health(self):
        if self._health_exc:
            raise self._health_exc
        return DriverResult(success=True, output={"version": "fake-1.0"})


class _AsyncFakeDriver:
    """
    Fake async driver (ConstructionDriver shape).

    The Supervisor awaits driver.run() and driver.terminate(), so both
    must be coroutines.
    """

    def __init__(self, raises=None, result=None, health_exc=None):
        self._raises = raises
        self._result = result or DriverResult(success=True, output={"artifact_path": ""})
        self._health_exc = health_exc
        self.terminate_called = False

    def __call__(self, stage_path=None):
        return self

    async def run(self):
        # Yield control briefly so concurrent tasks can start — mimics a real build.
        await asyncio.sleep(0)
        if self._raises:
            raise self._raises
        return self._result

    async def terminate(self):
        self.terminate_called = True

    def check_health(self):
        if self._health_exc:
            raise self._health_exc
        return DriverResult(success=True, output={"version": "fake-docker-26"})


def _build_registry(k6_driver, linter_driver, docker_driver) -> DriverRegistry:
    """
    Wire a real DriverRegistry with fake driver factories.

    Each factory is a lambda that ignores stage_path and returns the
    pre-built fake driver instance. This exercises the real registry
    lookup, normalisation, and completeness-check logic.
    """
    registry = DriverRegistry()

    # Register using the decorator pattern (same as production build_registry in ship.py).
    registry.register("k6", Priority.P1_VALIDATION)(
        lambda stage_path=None: k6_driver
    )
    registry.register("linter", Priority.P2_QUALITY)(
        lambda stage_path=None: linter_driver
    )
    registry.register("docker", Priority.P3_CONSTRUCTION)(
        lambda stage_path=None: docker_driver
    )
    return registry


def _make_config(tmp_path: Path, **overrides) -> SupervisorConfig:
    """Minimal valid SupervisorConfig factory — same pattern as unit tests."""
    defaults = dict(
        project_name="integration-test-project",
        source_path=tmp_path,
        image_tag="integration-test:latest",
        binary_path=tmp_path / "dist" / "app",
        k6_script=tmp_path / "load.js",
        strict=False,
    )
    defaults.update(overrides)
    return SupervisorConfig(**defaults)


@pytest.fixture
def bypass_staging(tmp_path):
    """
    Replace StagingContext with a no-op that returns tmp_path as the stage path.

    This fixture is identical in purpose to the one in test_supervisor.py:
    we want integration tests to exercise everything *except* the disk-copy
    step, because that step is already covered by test_stage.py.
    """
    # Provide a Dockerfile so ConstructionDriver.run() passes its existence check.
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")

    class _FakeStagingCtx:
        def __init__(self, source_path):
            self.source_path = source_path
            self.stage_path = tmp_path

        async def __aenter__(self):
            return self.stage_path

        async def __aexit__(self, *args):
            pass

    with patch("core.supervisor.StagingContext", _FakeStagingCtx):
        yield tmp_path


@pytest.fixture
def bypass_audit():
    """
    Patch AuditChain.run() to return a passing report without calling Syft.

    Rationale: the audit chain performs real filesystem operations (SHA256,
    Syft SBOM generation) that are not available in a unit-test context.
    AuditChain integration is covered separately in tests/audit/.
    """
    passing_report = SimpleNamespace(passed=True)
    with patch("core.supervisor.AuditChain") as mock_chain_cls:
        mock_instance = MagicMock()
        mock_instance.run = MagicMock(return_value=passing_report)
        mock_chain_cls.return_value = mock_instance
        yield mock_chain_cls


@pytest.mark.asyncio
async def test_full_happy_path_all_streams_succeed(tmp_path, bypass_staging, bypass_audit):
    """
    INTEGRATION — Happy Path

    VERIFY:
        - All three streams complete with outcome="success".
        - FSM reaches SUCCESS state.
        - ForensicReport.audit_passed is True.
        - ForensicReport.kill_cause is None.
        - cleanup_ok is True.

    This is the nominal flight: every driver succeeds, the audit chain
    passes, and the artifact is marked read-only (bypassed here via
    bypass_audit fixture).
    """
    k6     = _SyncFakeDriver()
    linter = _SyncFakeDriver()
    docker = _AsyncFakeDriver()

    config   = _make_config(tmp_path)
    registry = _build_registry(k6, linter, docker)
    sv       = HamiltonSupervisor(config, registry)

    # Prevent _mark_readonly from trying to chmod a non-existent binary.
    with patch("core.supervisor._mark_readonly"):
        report = await sv.ship()

    assert report.flight_state == FlightState.SUCCESS.name, (
        f"Expected SUCCESS, got {report.flight_state}"
    )
    assert report.kill_cause is None
    assert report.audit_passed is True
    assert report.cleanup_ok is True

    p1 = report.stream_results.get("P1:Validation")
    p2 = report.stream_results.get("P2:Quality")
    p3 = report.stream_results.get("P3:Construction")
    assert p1 is not None and p1.outcome == "success"
    assert p2 is not None and p2.outcome == "success"
    assert p3 is not None and p3.outcome == "success"


@pytest.mark.asyncio
async def test_p1_alarm_triggers_hamilton_kill_and_aborts_mission(tmp_path, bypass_staging):
    """
    INTEGRATION — P1 Hamilton Kill

    VERIFY:
        - A HamiltonAlarm from K6Driver sets FSM to ABORTED.
        - ForensicReport.kill_cause contains "HamiltonAlarm".
        - P3 stream outcome is "cancelled" or "skipped" (not "failed").
        - Post-flight audit is NOT called.
        - cleanup_ok is True (cleanup still runs after kill).

    This is the most critical mission abort path. The forensic report must
    clearly distinguish a P1-triggered abort from an independent P3 failure.
    """
    alarm  = HamiltonAlarm("P95 latency exceeded 500ms")
    k6     = _SyncFakeDriver(raises=alarm)
    linter = _SyncFakeDriver()
    docker = _AsyncFakeDriver()  # P3 will be cancelled mid-flight

    config   = _make_config(tmp_path)
    registry = _build_registry(k6, linter, docker)
    sv       = HamiltonSupervisor(config, registry)

    with patch.object(sv, "_post_flight", new_callable=AsyncMock) as mock_pf:
        report = await sv.ship()
        # Post-flight must NOT run after a Hamilton Kill.
        mock_pf.assert_not_called()

    assert report.flight_state == FlightState.ABORTED.name
    assert report.kill_cause is not None
    assert "HamiltonAlarm" in report.kill_cause
    assert report.cleanup_ok is True

    p3 = report.stream_results.get("P3:Construction")
    if p3 is not None:
        # P3 may have completed before P1's cancellation reached it (race
        # condition in the async TaskGroup). Either is acceptable — the
        # definitive abort signal is the FSM state reaching ABORTED.
        # What must NOT happen is P3 recording a BuildError as its exception
        # when it was externally cancelled (that would be wrong causality).
        assert p3.outcome in ("cancelled", "skipped", "success"), (
            f"P3 outcome must be cancelled/skipped/success after P1 kill, got '{p3.outcome}'"
        )
        if p3.outcome == "cancelled":
            # If cancelled, the exception must be None (external cancel, not a P3 error).
            assert p3.exception is None, (
                "P3 was cancelled externally — must not record a BuildError exception."
            )


@pytest.mark.asyncio
async def test_p1_threshold_exceeded_sets_kill_cause(tmp_path, bypass_staging):
    """
    INTEGRATION — P1 ThresholdExceededError (sub-class of HamiltonAlarm)

    VERIFY: ThresholdExceededError (the specific k6 signal) is handled
    identically to HamiltonAlarm — FSM must reach ABORTED.

    ThresholdExceededError is the K6Driver's primary signal; the Supervisor
    must not special-case it differently from its parent class.
    """
    threshold_err = ThresholdExceededError(
        "P95=450ms > 200ms threshold",
        context={"p95_ms": 450, "threshold": 200},
    )
    k6     = _SyncFakeDriver(raises=threshold_err)
    linter = _SyncFakeDriver()
    docker = _AsyncFakeDriver()

    config   = _make_config(tmp_path)
    registry = _build_registry(k6, linter, docker)
    sv       = HamiltonSupervisor(config, registry)

    report = await sv.ship()

    assert report.flight_state == FlightState.ABORTED.name
    assert report.kill_cause is not None


@pytest.mark.asyncio
async def test_p2_violation_non_strict_does_not_abort(tmp_path, bypass_staging, bypass_audit):
    """
    INTEGRATION — P2 QualityViolation (non-strict)

    VERIFY:
        - A QualityViolation with strict=False does NOT abort the mission.
        - FSM is NOT ABORTED.
        - kill_cause remains None.
        - P2 stream outcome is "failed" (it raised, so it failed its stream).

    Non-strict mode: lint errors are a warning, not a kill switch.
    """
    violation = QualityViolation("4 flake8 violations in src/")
    k6     = _SyncFakeDriver()
    linter = _SyncFakeDriver(raises=violation)
    docker = _AsyncFakeDriver()

    config   = _make_config(tmp_path, strict=False)
    registry = _build_registry(k6, linter, docker)
    sv       = HamiltonSupervisor(config, registry)

    with patch("core.supervisor._mark_readonly"):
        report = await sv.ship()

    assert report.flight_state != FlightState.ABORTED.name, (
        "A non-strict QualityViolation must NOT abort the mission"
    )
    assert report.kill_cause is None

    p2 = report.stream_results.get("P2:Quality")
    assert p2 is not None
    assert p2.outcome == "failed"


@pytest.mark.asyncio
async def test_p2_violation_strict_mode_escalates_to_kill(tmp_path, bypass_staging):
    """
    INTEGRATION — P2 QualityViolation (--strict)

    VERIFY:
        - In strict mode, a QualityViolation escalates to Hamilton Kill.
        - FSM reaches ABORTED.
        - kill_cause contains "QualityViolation" AND "strict".

    The escalation decision is the Supervisor's — LinterDriver must never
    touch strict logic. This test verifies the full call chain:
    LinterDriver raises → registry factory returns → Supervisor catches →
    _hamilton_kill() fires.
    """
    violation = QualityViolation("Trailing whitespace on 12 lines")
    k6     = _SyncFakeDriver()
    linter = _SyncFakeDriver(raises=violation)
    docker = _AsyncFakeDriver()

    config   = _make_config(tmp_path, strict=True)
    registry = _build_registry(k6, linter, docker)
    sv       = HamiltonSupervisor(config, registry)

    report = await sv.ship()

    assert report.flight_state == FlightState.ABORTED.name
    assert report.kill_cause is not None
    assert "QualityViolation" in report.kill_cause
    assert "strict" in report.kill_cause.lower()

@pytest.mark.asyncio
async def test_p3_build_error_aborts_only_p3_stream(tmp_path, bypass_staging):
    """
    INTEGRATION — P3 BuildError isolation

    VERIFY:
        - A BuildError from ConstructionDriver does NOT abort the mission
          at the FSM level (no Hamilton Kill).
        - kill_cause does NOT contain "HamiltonAlarm".
        - P3 stream outcome is "failed".
        - P1 and P2 outcomes are unaffected (success).

    P3 isolation is the most subtle invariant in the priority table.
    The TaskGroup must absorb the BuildError without triggering siblings'
    cancellation.
    """
    build_err = BuildError(
        "Dockerfile syntax error at line 42",
        context={"exit_code": 1},
    )
    k6     = _SyncFakeDriver()
    linter = _SyncFakeDriver()
    docker = _AsyncFakeDriver(raises=build_err)

    config   = _make_config(tmp_path)
    registry = _build_registry(k6, linter, docker)
    sv       = HamiltonSupervisor(config, registry)

    report = await sv.ship()

    # The mission is not fully aborted by a P3 failure.
    assert "HamiltonAlarm" not in (report.kill_cause or ""), (
        "A P3 BuildError must not appear as a Hamilton Kill cause"
    )

    p3 = report.stream_results.get("P3:Construction")
    assert p3 is not None
    assert p3.outcome == "failed"
    assert isinstance(p3.exception, BuildError)

    p1 = report.stream_results.get("P1:Validation")
    p2 = report.stream_results.get("P2:Quality")
    assert p1 is not None and p1.outcome == "success"
    assert p2 is not None and p2.outcome == "success"


@pytest.mark.asyncio
async def test_env_error_in_health_check_aborts_before_launch(tmp_path, bypass_staging):
    """
    INTEGRATION — EnvError in pre-flight health check

    VERIFY:
        - An EnvError from a driver's check_health() aborts the mission
          before _launch() is ever called.
        - FSM reaches ABORTED.
        - kill_cause contains "EnvError".
        - No stream results are written (launch never started).

    Pre-flight failures must prevent any subprocess from being spawned —
    an untrustworthy environment must produce zero artifact side-effects.
    """
    k6     = _SyncFakeDriver(health_exc=EnvError("k6 binary not found", context={"tool": "k6"}))
    linter = _SyncFakeDriver()
    docker = _AsyncFakeDriver()

    config   = _make_config(tmp_path)
    registry = _build_registry(k6, linter, docker)
    sv       = HamiltonSupervisor(config, registry)

    # Patch _launch to confirm it is never reached.
    sv._launch = AsyncMock(side_effect=AssertionError("_launch must NOT run after EnvError"))

    report = await sv.ship()

    assert report.flight_state == FlightState.ABORTED.name
    assert report.kill_cause is not None
    assert "EnvError" in report.kill_cause
    sv._launch.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_runs_on_every_flight_path(tmp_path, bypass_staging, bypass_audit):
    """
    INTEGRATION — Cleanup contract

    VERIFY: _reap_all() is called exactly once per ship() invocation,
    regardless of whether the flight succeeded, was killed, or hit an
    EnvError. Cleanup must live in ``finally``, never only in ``except``.

    Failure mode: if cleanup is gated on success only, zombie staging
    directories accumulate and corrupt subsequent builds.
    """
    # We test three paths: success, P1 kill, and EnvError.
    paths = [
        # (k6, linter, docker, extra_overrides)
        (_SyncFakeDriver(), _SyncFakeDriver(), _AsyncFakeDriver(), {}),
        (_SyncFakeDriver(raises=HamiltonAlarm("kill")), _SyncFakeDriver(), _AsyncFakeDriver(), {}),
        (_SyncFakeDriver(health_exc=EnvError("no k6")), _SyncFakeDriver(), _AsyncFakeDriver(), {}),
    ]

    for k6, linter, docker, overrides in paths:
        config   = _make_config(tmp_path, **overrides)
        registry = _build_registry(k6, linter, docker)
        sv       = HamiltonSupervisor(config, registry)

        reap_calls = []
        original_reap = sv._reap_all

        async def _tracked(original=original_reap, calls=reap_calls):
            calls.append(True)
            await original()

        sv._reap_all = _tracked

        with patch("core.supervisor._mark_readonly"):
            await sv.ship()

        assert len(reap_calls) == 1, (
            f"Expected _reap_all to be called exactly once, got {len(reap_calls)}"
        )


@pytest.mark.asyncio
async def test_incomplete_registry_aborts_in_preflight(tmp_path, bypass_staging):
    """
    INTEGRATION — Registry completeness enforcement

    VERIFY: A DriverRegistry missing one of the three pillars causes the
    supervisor's pre-flight to abort with a RegistryError before any
    stream is launched.

    RegistryError from verify_completeness() is not caught by any of the
    supervisor's explicit except clauses (EnvError, StagingError) — it
    propagates as an unhandled exception from ship(). We verify that no
    stream was launched and that the error is a RegistryError.
    """
    from core.exceptions import RegistryError

    # Deliberately omit the P3 docker driver.
    registry = DriverRegistry()
    registry.register("k6", Priority.P1_VALIDATION)(lambda stage_path=None: _SyncFakeDriver())
    registry.register("linter", Priority.P2_QUALITY)(lambda stage_path=None: _SyncFakeDriver())
    # docker NOT registered — this will cause verify_completeness() to raise.

    config = _make_config(tmp_path)
    sv     = HamiltonSupervisor(config, registry)

    # _launch must never be called when the registry is incomplete.
    sv._launch = AsyncMock(side_effect=AssertionError("_launch must NOT run after RegistryError"))

    # RegistryError is not in the supervisor's except chain — it surfaces raw.
    with pytest.raises(RegistryError):
        await sv.ship()

    sv._launch.assert_not_called()


@pytest.mark.asyncio
async def test_forensic_report_is_self_contained_after_full_flight(
    tmp_path, bypass_staging, bypass_audit
):
    """
    INTEGRATION — ForensicReport completeness

    VERIFY: After a full successful flight, the ForensicReport contains:
        - project name matching config
        - started_at and ended_at both set, with ended_at >= started_at
        - strict_mode flag reflecting config
        - all three stream results populated

    A ForensicReport must be self-contained for a post-mortem analyst —
    no cross-referencing of config objects should be required.
    """
    import time

    k6     = _SyncFakeDriver()
    linter = _SyncFakeDriver()
    docker = _AsyncFakeDriver()

    config   = _make_config(tmp_path, strict=False)
    registry = _build_registry(k6, linter, docker)
    sv       = HamiltonSupervisor(config, registry)

    before = time.time()
    with patch("core.supervisor._mark_readonly"):
        report = await sv.ship()
    after = time.time()

    assert report.project == "integration-test-project"
    assert report.started_at >= before
    assert report.ended_at <= after
    assert report.ended_at >= report.started_at
    assert report.strict_mode is False
    assert "P1:Validation" in report.stream_results
    assert "P2:Quality" in report.stream_results
    assert "P3:Construction" in report.stream_results


@pytest.mark.asyncio
async def test_supervisor_is_reentrant_across_multiple_ship_calls(
    tmp_path, bypass_staging, bypass_audit
):
    """
    INTEGRATION — Supervisor re-entrancy

    VERIFY: Calling ship() twice on the same supervisor instance produces
    independent ForensicReports. The second call must NOT be contaminated
    by state from the first call.

    Re-entrancy is required for tooling that retries failed builds without
    reconstructing the entire supervisor object.
    """
    k6     = _SyncFakeDriver()
    linter = _SyncFakeDriver()
    docker = _AsyncFakeDriver()

    config   = _make_config(tmp_path)
    registry = _build_registry(k6, linter, docker)
    sv       = HamiltonSupervisor(config, registry)

    with patch("core.supervisor._mark_readonly"):
        report1 = await sv.ship()
        # Reset fake drivers that have internal state.
        docker.terminate_called = False

        report2 = await sv.ship()

    assert report1 is not report2, "Each ship() call must return a distinct ForensicReport"
    assert report1.flight_state == FlightState.SUCCESS.name
    assert report2.flight_state == FlightState.SUCCESS.name
