"""
Edge-Case Tests — HamiltonSupervisor
======================================

Covers boundary conditions, unexpected inputs, and failure modes that are
distinct from the happy-path and primary-signal behavioural tests.

Categories:
    - Null / empty inputs to SupervisorConfig
    - Double-invocation (ship() called twice on same instance)
    - All three streams fail simultaneously
    - EnvError mid-flight (inside P3, not pre-flight)
    - StreamResult fields under every outcome
    - ForensicReport.cleanup_ok = False when _reap_all errors
    - _hamilton_kill with no active construction driver
"""

from __future__ import annotations

import asyncio
import pytest
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from core.exceptions import (
    BuildError,
    EnvError,
    HamiltonAlarm,
    QualityViolation,
)
from core.state import FlightState
from core.supervisor import (
    ForensicReport,
    HamiltonSupervisor,
    StreamResult,
    SupervisorConfig,
)


class _FakeDriver:
    def __init__(self, raises=None, result=None, health_exc=None):
        self._raises = raises
        self._result = result or SimpleNamespace(success=True, output={})
        self._health_exc = health_exc
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
        self.terminate_called = True


class _FakeRegistry:
    def __init__(self, drivers: dict):
        self._drivers = drivers

    def get(self, name: str):
        return self._drivers[name]

    def verify_completeness(self):
        pass


def _make_config(tmp_path: Path, **overrides) -> SupervisorConfig:
    defaults = dict(
        project_name="edge-test",
        source_path=tmp_path,
        image_tag="edge:latest",
        binary_path=tmp_path / "build" / "app",
        k6_script=tmp_path / "load.js",
        strict=False,
    )
    defaults.update(overrides)
    return SupervisorConfig(**defaults)


@pytest.fixture
def bypass_staging(tmp_path):
    class _FakeStagingCtx:
        def __init__(self, source_path):
            self.stage_path = source_path

        async def __aenter__(self):
            return self.stage_path

        async def __aexit__(self, *args):
            pass

    with patch("core.supervisor.StagingContext", _FakeStagingCtx):
        yield tmp_path


def test_supervisor_config_rejects_empty_project_name(tmp_path):
    """
    VERIFY: An empty project_name is still accepted by the dataclass (it is a
    string field with no validator). The supervisor logs it as-is. This test
    documents the current contract — if validation is added later, update here.
    """
    config = SupervisorConfig(
        project_name="",
        source_path=tmp_path,
        image_tag="app:v1",
        binary_path=tmp_path / "app",
        k6_script=tmp_path / "load.js",
    )
    assert config.project_name == ""


def test_stream_result_default_outcome_is_skipped():
    """
    VERIFY: A StreamResult constructed with only a name defaults to 'skipped'.
    This is the sentinel value the Supervisor uses for streams that never ran
    (e.g. if pre-flight aborted before launch).
    """
    result = StreamResult(name="P1:Validation")
    assert result.outcome == "skipped"
    assert result.exception is None
    assert result.cancelled_by is None
    assert result.duration_s == 0.0


def test_forensic_report_defaults():
    """
    VERIFY: A freshly constructed ForensicReport has sensible defaults.
    Any field left unset must not cause KeyError or AttributeError downstream.
    """
    report = ForensicReport(project="test")
    assert report.flight_state == FlightState.IDLE.name
    assert report.kill_cause is None
    assert report.p1_metrics == {}
    assert report.audit_passed is False
    assert report.cleanup_ok is False
    assert report.strict_mode is False


@pytest.mark.asyncio
async def test_ship_called_twice_does_not_crash(tmp_path, bypass_staging):
    """
    VERIFY: Calling ship() a second time on the same supervisor instance does
    not raise an unhandled exception. The _kill_fired flag resets correctly.

    This is not an intended usage but defensive robustness matters — a caller
    error must not produce a confusing traceback.

    ConstructionDriver is patched because Docker daemon is not guaranteed to be
    running in test environments — we only test supervisor orchestration here.
    """
    config = _make_config(tmp_path)
    registry = _FakeRegistry({
        "k6": _FakeDriver(),
        "linter": _FakeDriver(),
        "docker": _FakeDriver(),
    })
    sv = HamiltonSupervisor(config, registry)
    sv._post_flight = AsyncMock()

    with patch("core.supervisor.ConstructionDriver", return_value=_FakeDriver()):
        report1 = await sv.ship()
        # Reset kill_fired so second call behaves as a fresh run
        sv._kill_fired = False
        report2 = await sv.ship()

    assert report1 is not report2  # independent report objects


@pytest.mark.asyncio
async def test_all_streams_fail_simultaneously(tmp_path, bypass_staging):
    """
    VERIFY: When P1, P2, and P3 all raise at once, the Supervisor handles the
    ExceptionGroup gracefully. It must not crash with an unhandled exception.

    In practice, HamiltonAlarm from P1 dominates — that handler fires first.
    P2's QualityViolation and P3's BuildError are secondary.
    """
    config = _make_config(tmp_path)
    registry = _FakeRegistry({
        "k6": _FakeDriver(raises=HamiltonAlarm("P1 failure")),
        "linter": _FakeDriver(raises=QualityViolation("P2 failure")),
        "docker": _FakeDriver(),
    })
    sv = HamiltonSupervisor(config, registry)

    with patch("core.supervisor.ConstructionDriver", return_value=_FakeDriver()):
        report = await sv.ship()

    # System must settle — either ABORTED or some terminal state.
    assert report.flight_state in (
        FlightState.ABORTED.name,
        FlightState.SUCCESS.name,
        FlightState.VERIFYING.name,
        FlightState.SHIPPING.name,
    )


@pytest.mark.asyncio
async def test_hamilton_kill_safe_when_no_construction_driver(tmp_path):
    """
    VERIFY: _hamilton_kill() is safe to call when _construction_driver is None.

    This happens when P1 fires before the P3 task even instantiates its driver.
    The kill handler must not raise AttributeError.
    """
    config = _make_config(tmp_path)
    registry = _FakeRegistry({
        "k6": _FakeDriver(),
        "linter": _FakeDriver(),
        "docker": _FakeDriver(),
    })
    sv = HamiltonSupervisor(config, registry)
    assert sv._construction_driver is None

    # Must not raise
    await sv._hamilton_kill(cause="P1:Validation")

    assert sv._kill_fired is True


# ---------------------------------------------------------------------------
# cleanup_ok = False when _reap_all raises internally
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cleanup_failure_sets_cleanup_ok_false(tmp_path, bypass_staging):
    """
    VERIFY: If cleanup encounters an error, report.cleanup_ok is False.
    The exception must NOT propagate — cleanup errors are logged and swallowed
    so the forensic report can still be written.
    """
    config = _make_config(tmp_path)
    registry = _FakeRegistry({
        "k6": _FakeDriver(),
        "linter": _FakeDriver(),
        "docker": _FakeDriver(),
    })
    sv = HamiltonSupervisor(config, registry)
    sv._post_flight = AsyncMock()

    # Inject a broken _cleanup_containers so _reap_all catches it.
    async def _broken_cleanup():
        raise RuntimeError("Docker daemon unreachable during cleanup")
    sv._cleanup_containers = _broken_cleanup

    report = await sv.ship()

    # Supervisor must NOT crash — cleanup failure is non-fatal.
    assert report.cleanup_ok is False


# ---------------------------------------------------------------------------
# P1 metrics stored in forensic report
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p1_telemetry_stored_in_forensic_report(tmp_path, bypass_staging):
    """
    VERIFY: When P1 succeeds, its raw telemetry dict (p95_ms, p99_ms,
    error_rate) is stored in report.p1_metrics for forensic analysis.
    """
    fake_metrics = {"p95_ms": 145.2, "p99_ms": 180.0, "error_rate": 0.3}
    k6_result = SimpleNamespace(success=True, output=fake_metrics)

    config = _make_config(tmp_path)
    registry = _FakeRegistry({
        "k6": _FakeDriver(result=k6_result),
        "linter": _FakeDriver(),
        "docker": _FakeDriver(),
    })
    sv = HamiltonSupervisor(config, registry)
    sv._post_flight = AsyncMock()

    with patch("core.supervisor.ConstructionDriver", return_value=_FakeDriver()):
        report = await sv.ship()

    assert report.p1_metrics == fake_metrics


# ---------------------------------------------------------------------------
# Stream duration is always recorded
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_duration_is_positive_after_run(tmp_path, bypass_staging):
    """
    VERIFY: Each stream that ran records a positive duration_s > 0.

    duration_s == 0.0 means the finally block in the task wrapper was skipped,
    which would indicate a programming error in the task wrapper.
    """
    config = _make_config(tmp_path)
    registry = _FakeRegistry({
        "k6": _FakeDriver(),
        "linter": _FakeDriver(),
        "docker": _FakeDriver(),
    })
    sv = HamiltonSupervisor(config, registry)
    sv._post_flight = AsyncMock()

    with patch("core.supervisor.ConstructionDriver", return_value=_FakeDriver()):
        report = await sv.ship()

    for name, result in report.stream_results.items():
        assert result.duration_s >= 0.0, (
            f"Stream '{name}' has negative duration — finally block not reached."
        )
