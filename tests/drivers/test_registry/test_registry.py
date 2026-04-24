"""
Contract tests for drivers/registry.py
=======================================

This suite verifies the integrity of the Driver Registry:
  1. Existence & completeness  — all Essential Pillars are registered.
  2. Immutability (No-Hijack)  — overwriting a driver raises RegistryError.
  3. Return-type contract      — drivers return a DriverResult.
  4. Case sensitivity          — 'K6' and 'k6' resolve to the same driver.
  5. Deterministic failure     — missing keys raise DriverNotFoundError, not KeyError.
  6. Input validation          — empty/whitespace names are rejected cleanly.
"""

import pytest
from types import SimpleNamespace

from drivers.registry import DriverRegistry, DriverResult, _PILLAR_KEYS
from core.exceptions import DriverNotFoundError, RegistryError
from core.priorities import Priority


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def empty_registry() -> DriverRegistry:
    """A fresh, empty DriverRegistry — no drivers registered."""
    return DriverRegistry()


@pytest.fixture()
def complete_registry() -> DriverRegistry:
    """
    A registry with all three Essential Pillar drivers registered.
    Each stub returns a successful DriverResult for contract verification.
    """
    registry = DriverRegistry()

    @registry.register("k6", priority=Priority.P1_VALIDATION)
    def run_k6(**kwargs) -> DriverResult:
        return DriverResult(success=True, output="k6 passed")

    @registry.register("linter", priority=Priority.P2_QUALITY)
    def run_linter(**kwargs) -> DriverResult:
        return DriverResult(success=True, output="linter clean")

    @registry.register("docker", priority=Priority.P3_CONSTRUCTION)
    def run_docker(**kwargs) -> DriverResult:
        return DriverResult(success=True, output="image built")

    return registry


def test_all_essential_pillars_are_registered(complete_registry):
    """
    Contract: verify_completeness() must not raise when all three
    Essential Pillars (k6/P1, linter/P2, docker/P3) are registered.
    """
    # Should not raise — a missing pillar would be a pre-flight failure.
    complete_registry.verify_completeness()


def test_verify_completeness_raises_when_p1_missing(empty_registry):
    """
    Contract: A registry missing P1 (k6) must raise RegistryError on
    verify_completeness(). We cannot fly blind without a validation tool.
    """
    # Register P2 and P3 only
    @empty_registry.register("linter", priority=Priority.P2_QUALITY)
    def run_linter(**kwargs) -> DriverResult:
        return DriverResult(success=True)

    @empty_registry.register("docker", priority=Priority.P3_CONSTRUCTION)
    def run_docker(**kwargs) -> DriverResult:
        return DriverResult(success=True)

    with pytest.raises(RegistryError) as exc_info:
        empty_registry.verify_completeness()

    assert "k6" in str(exc_info.value)


def test_verify_completeness_raises_when_p3_missing(empty_registry):
    """
    Contract: A registry with only P1 and P2 raises RegistryError —
    the construction pillar is required for a complete toolchain.
    """
    @empty_registry.register("k6", priority=Priority.P1_VALIDATION)
    def run_k6(**kwargs) -> DriverResult:
        return DriverResult(success=True)

    @empty_registry.register("linter", priority=Priority.P2_QUALITY)
    def run_linter(**kwargs) -> DriverResult:
        return DriverResult(success=True)

    with pytest.raises(RegistryError) as exc_info:
        empty_registry.verify_completeness()

    assert "docker" in str(exc_info.value)


def test_verify_completeness_raises_on_empty_registry(empty_registry):
    """
    Contract: An entirely empty registry must report all three
    missing pillars in the RegistryError context.
    """
    with pytest.raises(RegistryError) as exc_info:
        empty_registry.verify_completeness()

    error = exc_info.value
    assert "k6" in str(error)
    assert "linter" in str(error)
    assert "docker" in str(error)


def test_registering_same_driver_name_raises_registry_error(empty_registry):
    """
    Contract: Attempting to register a second function under the same
    name must raise RegistryError. This is the primary security control
    preventing hostile repos from injecting code into the toolchain.
    """
    @empty_registry.register("k6", priority=Priority.P1_VALIDATION)
    def legitimate_k6(**kwargs) -> DriverResult:
        return DriverResult(success=True)

    # A hostile actor tries to overwrite the real k6 driver.
    with pytest.raises(RegistryError) as exc_info:
        @empty_registry.register("k6", priority=Priority.P1_VALIDATION)
        def malicious_k6(**kwargs) -> DriverResult:
            return DriverResult(success=False, error="injected!")

    assert "already registered" in str(exc_info.value).lower()


def test_hijack_attempt_leaves_original_driver_intact(empty_registry):
    """
    Contract: After a failed hijack, the original driver is still
    callable and returns its expected output — the Registry is unharmed.
    """
    @empty_registry.register("k6", priority=Priority.P1_VALIDATION)
    def real_k6(**kwargs) -> DriverResult:
        return DriverResult(success=True, output="authentic")

    # Attempted injection — must be silently blocked (after raising).
    try:
        @empty_registry.register("k6", priority=Priority.P1_VALIDATION)
        def fake_k6(**kwargs) -> DriverResult:
            return DriverResult(success=False, output="injected")
    except RegistryError:
        pass

    # The original driver must still return its authentic result.
    result = empty_registry.get("k6")()
    assert result.output == "authentic"


def test_registered_driver_returns_driver_result(complete_registry):
    """
    Contract: Every driver in the Registry must return a DriverResult
    so the Supervisor can read outcomes with a uniform interface.
    """
    driver = complete_registry.get("k6")
    result = driver()

    assert isinstance(result, DriverResult)


def test_driver_result_fields_on_success(complete_registry):
    """
    Contract: A successful DriverResult must have success=True and
    a non-None output field — the Supervisor relies on both.
    """
    result: DriverResult = complete_registry.get("docker")()

    assert result.success is True
    assert result.output is not None
    assert result.error is None


def test_driver_result_fields_on_failure(empty_registry):
    """
    Contract: A failing driver must set success=False and populate
    the error field, leaving output as None (or a partial payload).
    """
    @empty_registry.register("k6", priority=Priority.P1_VALIDATION)
    def failing_k6(**kwargs) -> DriverResult:
        return DriverResult(success=False, error="P95 threshold exceeded")

    result = empty_registry.get("k6")()

    assert result.success is False
    assert result.error == "P95 threshold exceeded"


def test_driver_result_is_immutable(complete_registry):
    """
    Contract: DriverResult is a frozen dataclass — it must not allow
    field mutation after creation, ensuring telemetry cannot be falsified.
    """
    result = complete_registry.get("k6")()

    with pytest.raises((AttributeError, TypeError)):
        result.success = False  # type: ignore[misc]


def test_get_is_case_insensitive(empty_registry):
    """
    Contract: 'K6', 'k6', and 'K6 ' all resolve to the same driver.
    Engineers must not face KeyErrors due to capitalisation accidents.
    """
    @empty_registry.register("k6", priority=Priority.P1_VALIDATION)
    def run_k6(**kwargs) -> DriverResult:
        return DriverResult(success=True, output="k6 ran")

    # All variants must resolve successfully.
    assert empty_registry.get("K6")() .output == "k6 ran"
    assert empty_registry.get("k6")() .output == "k6 ran"
    assert empty_registry.get("K6 ")().output == "k6 ran"

def test_register_is_case_insensitive_for_collision_detection(empty_registry):
    """
    Contract: Registering 'DOCKER' after 'docker' must be treated as
    a hijack attempt and raise RegistryError — not create a second entry.
    """
    @empty_registry.register("docker", priority=Priority.P3_CONSTRUCTION)
    def real_docker(**kwargs) -> DriverResult:
        return DriverResult(success=True)

    with pytest.raises(RegistryError):
        @empty_registry.register("DOCKER", priority=Priority.P3_CONSTRUCTION)
        def fake_docker(**kwargs) -> DriverResult:
            return DriverResult(success=False)

def test_get_missing_key_raises_driver_not_found_error(empty_registry):
    """
    Contract: Requesting an unregistered tool must raise DriverNotFoundError,
    not the raw Python KeyError — enabling the Supervisor to log a
    meaningful, structured telemetry signal.
    """
    with pytest.raises(DriverNotFoundError):
        empty_registry.get("nonexistent_tool")


def test_driver_not_found_error_is_not_key_error(empty_registry):
    """
    Contract: DriverNotFoundError must never be a subclass of KeyError.
    It is a domain signal, not a dict-access primitive.
    """
    with pytest.raises(DriverNotFoundError) as exc_info:
        empty_registry.get("phantom")

    assert not isinstance(exc_info.value, KeyError)


def test_driver_not_found_error_includes_available_tools(empty_registry):
    """
    Contract: The error context must list the available driver names so
    the Supervisor can emit an actionable telemetry message.
    """
    @empty_registry.register("docker", priority=Priority.P3_CONSTRUCTION)
    def run_docker(**kwargs) -> DriverResult:
        return DriverResult(success=True)

    with pytest.raises(DriverNotFoundError) as exc_info:
        empty_registry.get("missing")

    # The error message must hint at what IS available.
    assert "docker" in str(exc_info.value)

@pytest.mark.parametrize("bad_name", ["", "   ", "\t", "\n"])
def test_register_rejects_empty_or_whitespace_name(empty_registry, bad_name):
    """
    Contract: Registering a driver with an empty or whitespace-only name
    must raise ValueError immediately — before any state is mutated.
    """
    with pytest.raises(ValueError):
        @empty_registry.register(bad_name, priority=Priority.P1_VALIDATION)
        def stub(**kwargs) -> DriverResult:
            return DriverResult(success=True)


@pytest.mark.parametrize("bad_name", ["", "   ", "\t"])
def test_get_rejects_empty_or_whitespace_name(empty_registry, bad_name):
    """
    Contract: Calling get() with an empty or whitespace-only name must
    raise ValueError, not DriverNotFoundError — it is a caller error.
    """
    with pytest.raises(ValueError):
        empty_registry.get(bad_name)

def test_two_registry_instances_do_not_share_state():
    """
    Contract: Two DriverRegistry instances must have independent
    phonebooks — a driver registered on one must not appear on the other.
    This is critical for test isolation and for running parallel builds.
    """
    registry_a = DriverRegistry()
    registry_b = DriverRegistry()

    @registry_a.register("k6", priority=Priority.P1_VALIDATION)
    def run_k6(**kwargs) -> DriverResult:
        return DriverResult(success=True)

    with pytest.raises(DriverNotFoundError):
        registry_b.get("k6")
