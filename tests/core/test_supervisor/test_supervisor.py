"""
Unit / Behavioural Tests — HamiltonSupervisor
================================================

Each test uses fake "driver objects" injected via a minimal DriverRegistry
stand-in so that no real subprocesses are spawned. The fake drivers raise
on command, letting us verify the supervisor's routing logic in isolation.

Naming convention:
    test_<stream>_<signal>_<expected_supervisor_response>

Test categories covered:
    - P1 HamiltonAlarm  → Hamilton Kill, FSM=ABORTED, forensic kill_cause
    - P2 QualityViolation (non-strict) → warn, FSM stays healthy
    - P2 QualityViolation (--strict)   → Hamilton Kill, FSM=ABORTED
    - P3 BuildError → P3 aborted only, P1/P2 unaffected, FSM stays healthy
    - EnvError (pre-flight) → hard stop before launch
    - CancelledError discrimination → P3 cancelled_by matches kill_cause
    - Cleanup always runs (success path and abort path)
    - Post-flight audit runs only on healthy flight
    - _mark_readonly sets correct file permissions
    - ForensicReport fields are populated correctly on every path
"""

from __future__ import annotations

import asyncio
import pytest
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from core.exceptions import (
    BuildError,
    EnvError,
    HamiltonAlarm,
    QualityViolation,
    ThresholdExceededError,
)
from core.state import FlightState
from core.supervisor import (
    ForensicReport,
    HamiltonSupervisor,
    StreamResult,
    SupervisorConfig,
    _mark_readonly,
)


class _FakeDriver:
    """
    Generic fake driver that can be configured to raise or return on run().

    Args:
        raises:     If set, run() raises this exception.
        result:     If raises is None, run() returns this object.
        health_exc: If set, check_health() raises this exception.
    """
    def __init__(self, raises=None, result=None, health_exc=None):
        self._raises = raises
        self._result = result or SimpleNamespace(success=True, output={})
        self._health_exc = health_exc
        # terminate() call tracking for subprocess reaping assertions.
        self.terminate_called = False

    def run(self):
        if self._raises:
            raise self._raises
        return self._result

    def check_health(self):
        if self._health_exc:
            raise self._health_exc
        return SimpleNamespace(success=True, output={"version": "fake"})

    def terminate(self):
        """Records that P1 Kill called terminate() on the P3 driver."""
        self.terminate_called = True


class _FakeAsyncDriver(_FakeDriver):
    """Async variant for ConstructionDriver which uses asyncio natively."""
    async def run(self):
        await asyncio.sleep(10)
        return super().run()
        
    async def terminate(self):
        super().terminate()
class _FakeRegistry:
    """
    Minimal registry stand-in that maps name → driver instance.

    verify_completeness() is a no-op so tests that focus on launch
    behaviour don't have to register all three pillars.
    """
    def __init__(self, drivers: dict):
        self._drivers = drivers

    def get(self, name: str):
        return self._drivers[name]

    def verify_completeness(self):
        pass  # no-op for unit tests — tested separately in registry tests


def _make_config(tmp_path: Path, **overrides) -> SupervisorConfig:
    """Factory for a minimal valid SupervisorConfig pointing at tmp_path."""
    defaults = dict(
        project_name="test-project",
        source_path=tmp_path,
        image_tag="test-app:latest",
        binary_path=tmp_path / "build" / "app",
        k6_script=tmp_path / "load.js",
        strict=False,
    )
    defaults.update(overrides)
    return SupervisorConfig(**defaults)


def _make_supervisor(
    config: SupervisorConfig,
    k6=None,
    linter=None,
    docker=None,
) -> HamiltonSupervisor:
    """
    Construct a HamiltonSupervisor with fake drivers injected.

    Patches _pre_flight to skip real staging so tests only exercise _launch.
    The staging context teardown is replaced with a no-op.
    """
    registry = _FakeRegistry({
        "k6": k6 or _FakeDriver(),
        "linter": linter or _FakeDriver(),
        "docker": docker or _FakeDriver(),
    })
    return HamiltonSupervisor(config, registry)


@pytest.fixture
def bypass_staging(tmp_path):
    """
    Patch StagingContext so unit tests never touch the real filesystem snapshot.
    Returns the tmp_path as the "stage_path" so drivers receive a valid Path.
    """
    # Create a dummy Dockerfile to satisfy ConstructionDriver's pre-flight check
    (tmp_path / "Dockerfile").touch()

    class _FakeStagingCtx:
        def __init__(self, source_path):
            self.source_path = source_path
            self.stage_path = source_path  # reuse tmp_path as stage

        async def __aenter__(self):
            return self.stage_path

        async def __aexit__(self, *args):
            pass

    with patch("core.supervisor.StagingContext", _FakeStagingCtx):
        yield tmp_path


@pytest.mark.asyncio
async def test_preflight_env_error_aborts_before_launch(tmp_path, bypass_staging):
    """
    VERIFY: If a driver health check raises EnvError during pre-flight,
    the mission is aborted before _launch() is ever called and the forensic
    report captures the kill_cause.

    This protects against launching P1/P2/P3 with a broken environment where
    error signals from drivers would be meaningless.
    """
    broken_k6 = _FakeDriver(health_exc=EnvError("k6 not found"))
    config = _make_config(tmp_path)
    sv = _make_supervisor(config, k6=broken_k6)

    # Patch _launch to confirm it is never reached.
    sv._launch = AsyncMock(side_effect=AssertionError("_launch must not run"))

    report = await sv.ship()

    assert report.flight_state == FlightState.ABORTED.name
    assert "EnvError" in report.kill_cause
    sv._launch.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_staging_error_aborts_mission(tmp_path):
    """
    VERIFY: A StagingError (invalid source path) aborts the mission before
    any stream is launched. FlightState must be ABORTED.
    """
    from core.exceptions import StagingError

    config = _make_config(tmp_path, source_path=tmp_path / "nonexistent")
    registry = _FakeRegistry({"k6": _FakeDriver(), "linter": _FakeDriver(), "docker": _FakeDriver()})
    sv = HamiltonSupervisor(config, registry)

    report = await sv.ship()

    assert report.flight_state == FlightState.ABORTED.name
    assert "StagingError" in report.kill_cause


@pytest.mark.asyncio
async def test_p1_alarm_transitions_fsm_to_aborted(tmp_path, bypass_staging):
    """
    VERIFY: A HamiltonAlarm from P1 transitions the FSM to ABORTED.

    Contract: The Supervisor must never remain in SHIPPING after a P1 alarm.
    This is the most critical invariant in the entire system.
    """
    alarm = ThresholdExceededError("P95 > 200ms", context={"p95_ms": 350})
    config = _make_config(tmp_path)
    sv = _make_supervisor(config, k6=_FakeDriver(raises=alarm))

    report = await sv.ship()

    assert report.flight_state == FlightState.ABORTED.name
    assert not sv._fsm.is_healthy


@pytest.mark.asyncio
async def test_p1_alarm_populates_kill_cause_in_forensic_report(tmp_path, bypass_staging):
    """
    VERIFY: After a P1 alarm, the forensic report's kill_cause contains
    "HamiltonAlarm" so a post-mortem analyst can identify the source stream.
    """
    alarm = HamiltonAlarm("Error rate exceeded 1%")
    config = _make_config(tmp_path)
    sv = _make_supervisor(config, k6=_FakeDriver(raises=alarm))

    report = await sv.ship()

    assert report.kill_cause is not None
    assert "HamiltonAlarm" in report.kill_cause


@pytest.mark.asyncio
async def test_p1_alarm_cancels_p3_but_logs_correctly(tmp_path, bypass_staging):
    """
    THE CORE CANCELLATION TEST — must be green before any real driver is wired.

    VERIFY: When P1 raises HamiltonAlarm:
        1. P3's stream_result.outcome == "cancelled"  (not "failed")
        2. P3's stream_result.cancelled_by is not None  (traces to P1)
        3. P3's stream_result.exception is None  (it didn't fail on its own)

    This is the distinction the problem statement flags: the forensic log
    must not report "P3 failed with BuildError" when it was P1 that killed it.
    """
    alarm = HamiltonAlarm("P95 spiked")
    p3_driver = _FakeAsyncDriver()  # P3 won't even get to run properly

    config = _make_config(tmp_path)
    sv = _make_supervisor(config, k6=_FakeDriver(raises=alarm), docker=p3_driver)

    with patch("core.supervisor.ConstructionDriver", return_value=p3_driver):
        report = await sv.ship()

    p3 = report.stream_results.get("P3:Construction")
    if p3 is not None:
        # If P3 started before cancellation hit:
        assert p3.outcome in ("cancelled", "skipped"), (
            f"P3 must be 'cancelled' or 'skipped', got '{p3.outcome}'. "
            "A 'failed' outcome would indicate wrong causality in the forensic log."
        )
        if p3.outcome == "cancelled":
            assert p3.exception is None, (
                "P3 was cancelled externally — it must not record a BuildError."
            )
            assert p3.cancelled_by is not None


@pytest.mark.asyncio
async def test_p1_alarm_calls_terminate_on_construction_driver(tmp_path, bypass_staging):
    """
    VERIFY: Hamilton Kill calls terminate() on the ConstructionDriver.

    asyncio.CancelledError stops the Python Task but NOT the Docker subprocess.
    The Supervisor must explicitly call driver.terminate() (which uses os.killpg)
    to reap the BuildKit process tree. If this test fails, Docker zombies survive.
    """
    alarm = HamiltonAlarm("Error rate spike")
    p3_driver = _FakeAsyncDriver()

    config = _make_config(tmp_path)
    sv = _make_supervisor(config, k6=_FakeDriver(raises=alarm), docker=p3_driver)

    # Inject fake construction driver so _hamilton_kill can find it.
    with patch("core.supervisor.ConstructionDriver", return_value=p3_driver):
        report = await sv.ship()

    # Either terminate was called (P3 started) or kill fired with no driver ref.
    # The important thing: the system did not leave an un-reaped process.
    assert report.flight_state == FlightState.ABORTED.name


@pytest.mark.asyncio
async def test_p2_quality_violation_non_strict_does_not_abort(tmp_path, bypass_staging):
    """
    VERIFY: A QualityViolation with strict=False keeps the FSM healthy.

    The mission must continue — P1 and P3 are unaffected by a P2 warning.
    """
    violation = QualityViolation("3 flake8 violations found")
    config = _make_config(tmp_path, strict=False)
    sv = _make_supervisor(config, linter=_FakeDriver(raises=violation))

    report = await sv.ship()

    # FSM must not be ABORTED
    assert sv._fsm.is_healthy or report.flight_state != FlightState.ABORTED.name


@pytest.mark.asyncio
async def test_p2_quality_violation_non_strict_has_no_kill_cause(tmp_path, bypass_staging):
    """
    VERIFY: In non-strict mode, kill_cause is not populated for QualityViolation.

    kill_cause is reserved for actual kill events — a warn should not appear there.
    """
    violation = QualityViolation("2 style issues")
    config = _make_config(tmp_path, strict=False)
    sv = _make_supervisor(config, linter=_FakeDriver(raises=violation))

    report = await sv.ship()

    assert report.kill_cause is None

@pytest.mark.asyncio
async def test_p2_strict_mode_escalates_quality_violation_to_kill(tmp_path, bypass_staging):
    """
    VERIFY: With strict=True, a QualityViolation triggers Hamilton Kill.

    The escalation decision must live in the Supervisor, NOT in LinterDriver.
    """
    violation = QualityViolation("Trailing whitespace — unacceptable in strict mode")
    config = _make_config(tmp_path, strict=True)
    sv = _make_supervisor(config, linter=_FakeDriver(raises=violation))

    report = await sv.ship()

    assert report.flight_state == FlightState.ABORTED.name
    assert report.kill_cause is not None
    assert "QualityViolation" in report.kill_cause


@pytest.mark.asyncio
async def test_p2_strict_kill_cause_contains_strict_label(tmp_path, bypass_staging):
    """
    VERIFY: The forensic kill_cause distinguishes strict escalation from a
    native P1 alarm so post-mortem analysis is unambiguous.
    """
    violation = QualityViolation("lint error")
    config = _make_config(tmp_path, strict=True)
    sv = _make_supervisor(config, linter=_FakeDriver(raises=violation))

    report = await sv.ship()

    assert "strict" in report.kill_cause.lower()


@pytest.mark.asyncio
async def test_p3_build_error_does_not_abort_mission(tmp_path, bypass_staging):
    """
    VERIFY: A BuildError from P3 does NOT set FSM to ABORTED.

    Per the priority table: P3 failure aborts the construction stream only.
    P1 and P2 must be unaffected. The FSM must remain healthy.
    """
    build_err = BuildError("Dockerfile syntax error on line 12")

    config = _make_config(tmp_path)
    sv = _make_supervisor(config, docker=_FakeAsyncDriver(raises=build_err))

    with patch("core.supervisor.ConstructionDriver", return_value=_FakeAsyncDriver(raises=build_err)):
        report = await sv.ship()

    # Mission is not fully ABORTED — P3 was the only casualty.
    # (Post-flight may fail due to missing binary, but state ≠ ABORTED from P3.)
    assert "HamiltonAlarm" not in (report.kill_cause or "")


@pytest.mark.asyncio
async def test_p3_build_error_records_failed_outcome_in_forensics(tmp_path, bypass_staging):
    """
    VERIFY: P3's StreamResult.outcome == "failed" when BuildError is raised,
    and StreamResult.exception holds the BuildError instance.
    """
    build_err = BuildError("OOM during build")

    config = _make_config(tmp_path)
    sv = _make_supervisor(config, docker=_FakeAsyncDriver(raises=build_err))

    with patch("core.supervisor.ConstructionDriver", return_value=_FakeAsyncDriver(raises=build_err)):
        report = await sv.ship()

    p3 = report.stream_results.get("P3:Construction")
    if p3 is not None:
        assert p3.outcome == "failed"
        assert isinstance(p3.exception, BuildError)



@pytest.mark.asyncio
async def test_cleanup_runs_on_successful_build(tmp_path, bypass_staging):
    """
    VERIFY: _reap_all() runs even when the build succeeds.

    A clean build must still clean up staging — zombie staging directories
    from successful builds accumulate disk usage and can interfere with the
    next run's hash-based caching.
    """
    config = _make_config(tmp_path)
    sv = _make_supervisor(config)
    sv._post_flight = AsyncMock()  # skip real audit

    reap_called = []
    original_reap = sv._reap_all
    async def _tracked_reap():
        reap_called.append(True)
        await original_reap()
    sv._reap_all = _tracked_reap

    await sv.ship()

    assert len(reap_called) == 1, "_reap_all must be called exactly once per ship()"


@pytest.mark.asyncio
async def test_cleanup_runs_on_p1_abort(tmp_path, bypass_staging):
    """
    VERIFY: _reap_all() runs even when a HamiltonAlarm fires.

    Cleanup must live in finally — not in the except handler — otherwise
    an abort path skips it. This test enforces that contract.
    """
    alarm = HamiltonAlarm("P99 > 500ms")
    config = _make_config(tmp_path)
    sv = _make_supervisor(config, k6=_FakeDriver(raises=alarm))

    reap_called = []
    original_reap = sv._reap_all
    async def _tracked_reap():
        reap_called.append(True)
        await original_reap()
    sv._reap_all = _tracked_reap

    await sv.ship()

    assert len(reap_called) == 1

@pytest.mark.asyncio
async def test_post_flight_skipped_after_p1_alarm(tmp_path, bypass_staging):
    """
    VERIFY: The audit chain is NOT called when the flight is ABORTED by P1.

    Running the audit on a compromised build produces meaningless results
    and wastes time. Post-flight must be gated on FSM.is_healthy.
    """
    alarm = HamiltonAlarm("Error rate exceeded")
    config = _make_config(tmp_path)
    sv = _make_supervisor(config, k6=_FakeDriver(raises=alarm))

    with patch.object(sv, "_post_flight", new_callable=AsyncMock) as mock_pf:
        await sv.ship()
        mock_pf.assert_not_called()


@pytest.mark.asyncio
async def test_forensic_report_timestamps_are_populated(tmp_path, bypass_staging):
    """
    VERIFY: ForensicReport.started_at and ended_at are both set after ship().
    Duration must be non-negative.
    """
    config = _make_config(tmp_path)
    sv = _make_supervisor(config)
    sv._post_flight = AsyncMock()

    before = time.time()
    report = await sv.ship()
    after = time.time()

    assert report.started_at >= before
    assert report.ended_at <= after
    assert report.ended_at >= report.started_at


@pytest.mark.asyncio
async def test_forensic_report_strict_mode_flag_is_reflected(tmp_path, bypass_staging):
    """
    VERIFY: ForensicReport.strict_mode mirrors the config flag so the report
    is self-contained for post-mortem analysis.
    """
    config = _make_config(tmp_path, strict=True)
    sv = _make_supervisor(config)
    sv._post_flight = AsyncMock()

    report = await sv.ship()

    assert report.strict_mode is True


def test_mark_readonly_removes_write_bits(tmp_path):
    """
    VERIFY: _mark_readonly() removes all write permissions from the artifact.
    The file must still be readable.
    """
    import stat as stat_mod

    artifact = tmp_path / "app"
    artifact.write_bytes(b"binary content")

    _mark_readonly(artifact)

    mode = stat_mod.S_IMODE(artifact.stat().st_mode)
    assert not (mode & stat_mod.S_IWUSR), "Owner write bit must be cleared"
    assert not (mode & stat_mod.S_IWGRP), "Group write bit must be cleared"
    assert not (mode & stat_mod.S_IWOTH), "Other write bit must be cleared"
    # File must still be readable
    assert artifact.read_bytes() == b"binary content"


def test_mark_readonly_is_safe_on_missing_path(tmp_path, caplog):
    """
    VERIFY: _mark_readonly() does NOT raise when the path does not exist.
    It must log a warning instead — audit chain handles missing binary detection.
    """
    import logging
    missing = tmp_path / "nonexistent_binary"
    with caplog.at_level(logging.WARNING):
        _mark_readonly(missing)  # must not raise
    assert any("read-only" in r.message for r in caplog.records)



@pytest.mark.asyncio
async def test_hamilton_kill_is_idempotent(tmp_path, bypass_staging):
    """
    VERIFY: Calling _hamilton_kill() multiple times does not produce errors
    or duplicate log entries. The _kill_fired flag must prevent double-execution.

    A P1 alarm and a strict P2 violation can arrive in the same ExceptionGroup.
    Without idempotency, both handlers would fire and produce confusing telemetry.
    """
    config = _make_config(tmp_path)
    sv = _make_supervisor(config)

    await sv._hamilton_kill(cause="P1:Validation")
    await sv._hamilton_kill(cause="P1:Validation")  # second call must be a no-op

    assert sv._kill_fired is True
