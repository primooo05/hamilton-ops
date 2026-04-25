"""
Gap tests for drivers/registry.py

This file targets the behavioral gaps identified after the initial
test_registry.py review. It is intentionally separate from the core
contract tests.

Gaps covered:
  GAP-01 — P2 (linter) pillar never tested in isolation
  GAP-02 — Priority mismatch not caught by verify_completeness()
  GAP-03 — context["missing"] never directly inspected (only str repr checked)
  GAP-04 — context["existing"] forensic field on hijack never asserted
  GAP-05 — Decorator identity: register() must return the original fn unchanged

Strategy:
  - Each test uses its own DriverRegistry instance (no shared state).
  - _PILLAR_KEYS is imported to keep driver name strings DRY — if the
    canonical names change, both the source and these tests update together.
  - Each test has ONE clear assertion focus.
"""

import pytest

from core.exceptions import DriverNotFoundError, RegistryError
from core.priorities import Priority
from drivers.registry import DriverRegistry, DriverResult, _PILLAR_KEYS


def _make_stub(output="ok") -> callable:
    """Return a fresh stub driver function with a unique identity each call."""
    def stub(**kwargs) -> DriverResult:
        return DriverResult(success=True, output=output)
    return stub


def _register_all_except(registry: DriverRegistry, skip_priority: Priority) -> None:
    """Register all three canonical pillars except the one to skip."""
    for priority, key in _PILLAR_KEYS.items():
        if priority != skip_priority:
            registry.register(key, priority=priority)(_make_stub())


def test_verify_completeness_raises_when_p2_missing():
    """
    GAP-01 | verify_completeness(): P1 and P3 registered, P2 (linter) absent → RegistryError.

    The core tests cover P1-missing and P3-missing but never P2-missing.
    This asymmetry means the linter pillar could be silently dropped and no
    existing test would catch it.
    """
    registry = DriverRegistry()
    _register_all_except(registry, skip_priority=Priority.P2_QUALITY)

    with pytest.raises(RegistryError) as exc_info:
        registry.verify_completeness()

    assert "linter" in str(exc_info.value)


def test_verify_completeness_p2_missing_context_names_linter():
    """
    GAP-01 (context) | verify_completeness(): context["missing"] must reference the linter key.

    Direct inspection of the context dict — not just the string representation —
    so a key rename or empty list is caught immediately.
    """
    registry = DriverRegistry()
    _register_all_except(registry, skip_priority=Priority.P2_QUALITY)

    with pytest.raises(RegistryError) as exc_info:
        registry.verify_completeness()

    missing = exc_info.value.context["missing"]
    assert isinstance(missing, list)
    assert any("linter" in entry for entry in missing), (
        f"Expected 'linter' to appear in context['missing'], got: {missing}"
    )


def test_verify_completeness_raises_when_k6_registered_as_wrong_priority():
    """
    GAP-02 | verify_completeness(): "k6" registered as P3_CONSTRUCTION → RegistryError.

    The old implementation only checked `key not in self._drivers`. A driver
    named "k6" but assigned P3_CONSTRUCTION would satisfy the key check and
    silently pass — leaving the Flight Computer without a real P1 tool.
    """
    registry = DriverRegistry()

    # Register "k6" under the WRONG priority
    @registry.register("k6", priority=Priority.P3_CONSTRUCTION)
    def mis_prioritized_k6(**kwargs) -> DriverResult:
        return DriverResult(success=True)

    # Register the remaining two correctly
    @registry.register("linter", priority=Priority.P2_QUALITY)
    def run_linter(**kwargs) -> DriverResult:
        return DriverResult(success=True)

    @registry.register("docker", priority=Priority.P3_CONSTRUCTION)
    def run_docker(**kwargs) -> DriverResult:
        return DriverResult(success=True)

    with pytest.raises(RegistryError) as exc_info:
        registry.verify_completeness()

    # The P1 pillar must be flagged as missing/misconfigured
    assert "k6" in str(exc_info.value)


def test_verify_completeness_raises_when_linter_registered_as_wrong_priority():
    """
    GAP-02 (linter) | verify_completeness(): "linter" registered as P1_VALIDATION → RegistryError.

    Verifies the priority check works for the P2 pillar, not just P1.
    """
    registry = DriverRegistry()

    @registry.register("k6", priority=Priority.P1_VALIDATION)
    def run_k6(**kwargs) -> DriverResult:
        return DriverResult(success=True)

    # Register "linter" under the WRONG priority
    @registry.register("linter", priority=Priority.P1_VALIDATION)
    def mis_prioritized_linter(**kwargs) -> DriverResult:
        return DriverResult(success=True)

    @registry.register("docker", priority=Priority.P3_CONSTRUCTION)
    def run_docker(**kwargs) -> DriverResult:
        return DriverResult(success=True)

    with pytest.raises(RegistryError) as exc_info:
        registry.verify_completeness()

    assert "linter" in str(exc_info.value)


def test_verify_completeness_passes_when_all_priorities_are_correct():
    """
    GAP-02 (positive case) | verify_completeness(): Correct name + correct Priority → no error.

    Regression guard: the new priority check must not break the happy path.
    """
    registry = DriverRegistry()

    @registry.register("k6", priority=Priority.P1_VALIDATION)
    def run_k6(**kwargs) -> DriverResult:
        return DriverResult(success=True)

    @registry.register("linter", priority=Priority.P2_QUALITY)
    def run_linter(**kwargs) -> DriverResult:
        return DriverResult(success=True)

    @registry.register("docker", priority=Priority.P3_CONSTRUCTION)
    def run_docker(**kwargs) -> DriverResult:
        return DriverResult(success=True)

    # Must not raise
    registry.verify_completeness()


def test_verify_completeness_context_missing_is_a_list():
    """
    GAP-03 | verify_completeness(): context["missing"] is a list, not None or a string.

    All existing tests check str(exc_info.value). If the key were renamed,
    the list were empty, or the entries were malformed, those tests would still
    pass. This test pins the shape of the context dict directly.
    """
    registry = DriverRegistry()  # completely empty

    with pytest.raises(RegistryError) as exc_info:
        registry.verify_completeness()

    ctx = exc_info.value.context
    assert "missing" in ctx, "context must have a 'missing' key"
    assert isinstance(ctx["missing"], list), "context['missing'] must be a list"


def test_verify_completeness_context_missing_contains_all_three_on_empty_registry():
    """
    GAP-03 (completeness) | verify_completeness(): Empty registry → context["missing"] has 3 entries.

    The existing test for an empty registry only checks str(error). This test
    verifies that the list carries exactly three entries — one per pillar.
    """
    registry = DriverRegistry()

    with pytest.raises(RegistryError) as exc_info:
        registry.verify_completeness()

    missing = exc_info.value.context["missing"]
    assert len(missing) == 3, (
        f"Expected 3 missing pillars for an empty registry, got {len(missing)}: {missing}"
    )


def test_verify_completeness_context_missing_contains_exactly_one_when_two_registered():
    """
    GAP-03 (precision) | verify_completeness(): Two pillars registered → context["missing"] has 1 entry.

    Verifies the list is precise — not over- or under-reporting.
    """
    registry = DriverRegistry()
    _register_all_except(registry, skip_priority=Priority.P1_VALIDATION)

    with pytest.raises(RegistryError) as exc_info:
        registry.verify_completeness()

    missing = exc_info.value.context["missing"]
    assert len(missing) == 1, (
        f"Expected exactly 1 missing pillar, got {len(missing)}: {missing}"
    )


def test_hijack_registry_error_context_existing_names_original_driver():
    """
    GAP-04 | register() hijack: context["existing"] must be the original driver's __name__.

    The Supervisor uses context["existing"] in forensic logs to identify which
    legitimate driver was being protected. The existing test only checks that
    "already registered" appears in str(exc_info.value); if the key were renamed
    or the value corrupted, that check would still pass.
    """
    registry = DriverRegistry()

    @registry.register("k6", priority=Priority.P1_VALIDATION)
    def original_k6_driver(**kwargs) -> DriverResult:
        return DriverResult(success=True)

    with pytest.raises(RegistryError) as exc_info:
        @registry.register("k6", priority=Priority.P1_VALIDATION)
        def malicious_replacement(**kwargs) -> DriverResult:
            return DriverResult(success=False)

    ctx = exc_info.value.context
    assert "existing" in ctx, "context must have an 'existing' key for forensic logging"
    assert ctx["existing"] == "original_k6_driver", (
        f"Expected 'original_k6_driver', got: {ctx['existing']!r}"
    )


def test_hijack_registry_error_context_key_is_normalised_name():
    """
    GAP-04 (key field) | register() hijack: context["key"] is the normalised (lowercase) name.

    Verifies the second field of the hijack context dict so the Supervisor can
    emit a structured log with both the protected key and the original driver name.
    """
    registry = DriverRegistry()

    @registry.register("Docker", priority=Priority.P3_CONSTRUCTION)
    def real_docker(**kwargs) -> DriverResult:
        return DriverResult(success=True)

    with pytest.raises(RegistryError) as exc_info:
        @registry.register("DOCKER", priority=Priority.P3_CONSTRUCTION)
        def fake_docker(**kwargs) -> DriverResult:
            return DriverResult(success=False)

    ctx = exc_info.value.context
    assert "key" in ctx
    # The stored key must be normalised regardless of the capitalisation used at registration
    assert ctx["key"] == "docker"


def test_register_decorator_returns_original_function_unchanged():
    """
    GAP-05 | register(): The decorator must return the exact same function object.

    Source line 122: `return fn`. If this were accidentally changed to wrap fn,
    drivers called directly (outside the registry) would behave differently from
    those retrieved via get(). No test previously verified this identity contract.
    """
    registry = DriverRegistry()

    def my_k6_driver(**kwargs) -> DriverResult:
        return DriverResult(success=True, output="identity check")

    # Apply the decorator manually (not via @-syntax) to capture both sides
    registered_fn = registry.register("k6", priority=Priority.P1_VALIDATION)(my_k6_driver)

    assert registered_fn is my_k6_driver, (
        "register() must return the original function object unchanged — "
        "wrapping it would break direct callers and introspection via __name__"
    )


def test_register_decorator_does_not_alter_function_name():
    """
    GAP-05 (name) | register(): The decorated function's __name__ must remain unchanged.

    context["existing"] is populated via self._drivers[normalised].__name__.
    If the decorator wrapped fn and changed its __name__, the forensic log
    would report the wrong function name.
    """
    registry = DriverRegistry()

    def authentic_linter_driver(**kwargs) -> DriverResult:
        return DriverResult(success=True)

    registered_fn = registry.register(
        "linter", priority=Priority.P2_QUALITY
    )(authentic_linter_driver)

    assert registered_fn.__name__ == "authentic_linter_driver"


def test_registered_fn_and_retrieved_fn_are_same_object():
    """
    GAP-05 (retrieval) | register() + get(): The function retrieved via get() must be the
    identical object that was passed to the decorator.

    Ensures that neither the decorator nor the internal storage introduces a wrapper.
    """
    registry = DriverRegistry()

    def my_docker_driver(**kwargs) -> DriverResult:
        return DriverResult(success=True)

    registry.register("docker", priority=Priority.P3_CONSTRUCTION)(my_docker_driver)

    retrieved = registry.get("docker")
    assert retrieved is my_docker_driver, (
        "get() must return the original function, not a wrapper"
    )