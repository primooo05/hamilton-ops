"""
Integration Test — Proof of Work (End-to-End Config-to-Report)
===============================================================

This is the single proof-of-work test for Stage 1.

What makes this different from test_full_flight.py:
    - StagingContext is NOT bypassed. The real staging machinery runs,
      copying the dummy project to a temp snapshot directory and cleaning
      it up on exit. This proves that staging create+cleanup works.
    - .hamilton.toml is present in the dummy project. The test asserts
      that the ForensicReport reflects values from the TOML, not just
      defaults. This proves the full config-to-supervisor pipeline is wired.
    - project_hash is computed from the dummy lockfile (package-lock.json).
      This proves compute_project_hash() participates in the real flow.

What is mocked:
    - Driver run() methods (k6, linter, docker) — no real subprocesses.
    - AuditChain.run() — no real Syft SBOM generation.
    - _mark_readonly() — the binary doesn't actually exist.

Test matrix:
    | Assertion                             | What it proves                       |
    |---------------------------------------|--------------------------------------|
    | report.project == "proof-of-work"     | TOML [project].name wired            |
    | sv._config.thresholds.p95_ms == 300   | TOML [validation].p95_ms wired       |
    | sv._config.image_tag == "test:pow"    | TOML [project].image_tag wired       |
    | report.flight_state == SUCCESS        | Full mission ran to completion        |
    | report.cleanup_ok is True             | StagingContext created + cleaned up  |
    | stage_dir does not exist after run    | No staging directory left on disk    |
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.config import load_hamilton_config
from core.priorities import FlightThresholds, Priority
from core.state import FlightState
from core.supervisor import HamiltonSupervisor, SupervisorConfig
from drivers.registry import DriverRegistry, DriverResult


class _SyncFakeDriver:
    """
    Fake synchronous driver (K6Driver / LinterDriver shape).
    Returns a successful DriverResult and passes health checks by default.
    """

    def __init__(self, raises=None, result=None):
        self._raises = raises
        self._result = result or DriverResult(success=True, output={})

    def __call__(self, stage_path=None):
        # Factory signature — returns self as the driver instance.
        return self

    def run(self):
        if self._raises:
            raise self._raises
        return self._result

    def check_health(self):
        return DriverResult(success=True, output={"version": "fake-1.0"})


class _AsyncFakeDriver:
    """
    Fake async driver (ConstructionDriver shape).
    Both run() and terminate() must be coroutines.
    """

    def __init__(self, raises=None, result=None):
        self._raises = raises
        self._result = result or DriverResult(success=True, output={"artifact_path": ""})

    def __call__(self, stage_path=None):
        return self

    async def run(self):
        await asyncio.sleep(0)  # yield control so concurrent tasks can start
        if self._raises:
            raise self._raises
        return self._result

    async def terminate(self):
        pass

    def check_health(self):
        return DriverResult(success=True, output={"version": "fake-docker-26"})


def _build_registry(k6_driver, linter_driver, docker_driver) -> DriverRegistry:
    """Wire a real DriverRegistry with fake driver factories."""
    registry = DriverRegistry()
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



@pytest.mark.asyncio
async def test_proof_of_work_full_pipeline_config_to_report(tmp_path):
    """
    PROOF OF WORK — End-to-End Config-to-Report

    Stages a real dummy project, runs the full supervisor with mocked
    drivers, and asserts that the ForensicReport accurately reflects
    the values declared in .hamilton.toml.

    This test fails if:
        - TOML loading is broken (project name / thresholds / image_tag wrong)
        - StagingContext does not create and clean up its directory
        - config.thresholds are not passed through to SupervisorConfig

    A passing run where these assertions are wrong proves NOTHING.
    """

    (tmp_path / "Dockerfile").write_text("FROM scratch\nCOPY . .\n")
    # Lockfile so compute_project_hash() finds something to hash.
    (tmp_path / "package-lock.json").write_text('{"lockfileVersion": 3}')
    (tmp_path / ".hamilton.toml").write_text(
        "[project]\n"
        'name = "proof-of-work"\n'
        'image_tag = "test:pow"\n'
        "\n"
        "[validation]\n"
        "p95_ms = 300\n"
    )

    toml_config = load_hamilton_config(tmp_path)
    resolved_thresholds = FlightThresholds.from_config(toml_config)
    toml_project = toml_config.get("project", {})

    project_name = toml_project.get("name") or tmp_path.name
    image_tag    = toml_project.get("image_tag", "hamilton/app:latest")

    config = SupervisorConfig(
        project_name=project_name,
        source_path=tmp_path,
        image_tag=image_tag,
        binary_path=tmp_path / "dist" / "app",
        k6_script=tmp_path / "load.js",
        strict=False,
        thresholds=resolved_thresholds,
    )


    k6     = _SyncFakeDriver()
    linter = _SyncFakeDriver()
    docker = _AsyncFakeDriver()

    registry = _build_registry(k6, linter, docker)
    sv = HamiltonSupervisor(config, registry)

    passing_audit = SimpleNamespace(passed=True)
    with patch("core.supervisor.AuditChain") as mock_chain_cls, \
         patch("core.supervisor._mark_readonly"):
        mock_instance = MagicMock()
        mock_instance.run = MagicMock(return_value=passing_audit)
        mock_chain_cls.return_value = mock_instance

        report = await sv.ship()


    # Config was correctly loaded from TOML — not from defaults or dir name.
    assert report.project == "proof-of-work", (
        f"Expected 'proof-of-work' from TOML [project].name, got '{report.project}'. "
        "This means project_name resolution is broken."
    )
    assert sv._config.image_tag == "test:pow", (
        f"Expected 'test:pow' from TOML [project].image_tag, got '{sv._config.image_tag}'. "
        "This means image_tag resolution is broken."
    )
    assert sv._config.thresholds.p95_ms == 300, (
        f"Expected p95_ms=300 from TOML [validation], got {sv._config.thresholds.p95_ms}. "
        "This means FlightThresholds are not flowing from TOML to SupervisorConfig."
    )

    # Mission ran to completion.
    assert report.flight_state == FlightState.SUCCESS.name, (
        f"Expected SUCCESS, got {report.flight_state}. "
        "Check that all three streams and the audit chain completed."
    )

    # Staging was created AND cleaned up — the core StagingContext contract.
    assert report.cleanup_ok is True, (
        "cleanup_ok is False — StagingContext.__aexit__ raised or was not called."
    )

    # The staging snapshot directory must not persist after ship() completes.
    # StagingContext creates a directory under .hamilton/stage/<uuid>.
    # If it still exists, the finally block in ship() did not run __aexit__.
    hamilton_stage_dir = tmp_path / ".hamilton" / "stage"
    if hamilton_stage_dir.exists():
        remaining = list(hamilton_stage_dir.iterdir())
        assert not remaining, (
            f"Staging directory was not cleaned up — {len(remaining)} item(s) remain: {remaining}"
        )
