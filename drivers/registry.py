"""
Hamilton-Ops Driver Registry
=============================

The Registry is the central phonebook of the Flight Computer.

Instead of hard-coding which tool to invoke in the Supervisor, every
driver factory function is registered here by name. The Supervisor calls
``registry.get("k6")`` and receives a factory that produces a ready-to-use driver instance.

Security contract:
  - Registration is **immutable**: once a driver factory is recorded, it cannot
    be replaced at runtime (the 'No-Hijack' rule).
  - Key lookups are **case-insensitive** for ergonomics, but internally
    stored in lowercase for determinism.
  - A missing key raises ``DriverNotFoundError``, never a bare KeyError.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from core.exceptions import DriverNotFoundError, RegistryError
from core.priorities import Priority

logger = logging.getLogger("hamilton.registry")

@dataclass(frozen=True)
class DriverResult:
    """
    The standard output envelope for every Hamilton driver.

    Every function in the Registry must return a DriverResult so that
    the Supervisor can read outcomes uniformly — regardless of which
    underlying tool (k6, Docker, linter) produced them.

    Attributes:
        success (bool):  True when the driver completed without error.
        output  (Any):   The raw payload from the tool (stdout, metrics, etc.).
        error   (Optional[str]):
                         Human-readable failure message when success=False.
    """
    success: bool
    output: Any = None
    error: Optional[str] = None

# Canonical priority -> key mapping for the three essential pillars.
# Used by verify_completeness() to detect a partially-assembled toolchain.
_PILLAR_KEYS: dict[Priority, str] = {
    Priority.P1_VALIDATION:  "k6",
    Priority.P2_QUALITY:     "linter",
    Priority.P3_CONSTRUCTION: "docker",
}


@dataclass
class DriverRegistry:
    """
    Immutable phonebook that maps tool names to driver callables.

    Usage (registration via decorator)::

        registry = DriverRegistry()

        @registry.register("k6", priority=Priority.P1_VALIDATION)
        def run_k6(**kwargs) -> DriverResult:
            ...

    Usage (direct call)::

        driver = registry.get("k6")
        result: DriverResult = driver(target="localhost", thresholds=thresholds)

    The registry is intentionally **not** a singleton: tests can construct
    independent instances without polluting shared state.
    """

    # Internal phonebook: normalised_name  -> callable.
    # Using field(default_factory=...) keeps instances independent.
    _drivers: dict[str, Callable[..., DriverResult]] = field(
        default_factory=dict, repr=False
    )

    # Tracks which Priority pillar each driver satisfies.
    _priorities: dict[str, Priority] = field(
        default_factory=dict, repr=False
    )

    def register(
        self,
        name: str,
        priority: Priority,
    ) -> Callable:
        """
        Decorator that locks a driver function into the Registry.

        Raises:
            RegistryError: If *name* is already registered (No-Hijack rule).
            ValueError:    If *name* is empty or whitespace-only.
        """
        normalised = self._normalise(name)

        def decorator(fn: Callable[..., DriverResult]) -> Callable[..., DriverResult]:
            # Guard: reject re-registration to prevent runtime injection.
            if normalised in self._drivers:
                raise RegistryError(
                    f"Driver '{normalised}' is already registered. "
                    "The Registry is immutable — overwriting an existing driver "
                    "violates the No-Hijack rule.",
                    context={"key": normalised, "existing": self._drivers[normalised].__name__},
                )

            self._drivers[normalised] = fn
            self._priorities[normalised] = priority
            logger.info(
                "REGISTRY: Locked driver '%s' → %s [%s]",
                normalised, fn.__name__, priority.label,
            )
            return fn

        return decorator

    def get(self, name: str) -> Callable[..., DriverResult]:
        """
        Retrieve a driver callable by name (case-insensitive).

        Raises:
            DriverNotFoundError: If *name* is not in the Registry.
        """
        normalised = self._normalise(name)
        driver = self._drivers.get(normalised)
        if driver is None:
            raise DriverNotFoundError(
                f"No driver registered for '{name}'. "
                f"Available tools: {sorted(self._drivers.keys())}",
                context={"requested": normalised, "available": sorted(self._drivers.keys())},
            )
        return driver

    def verify_completeness(self) -> None:
        """
        Assert that the three Essential Pillars are present.

        Checks that P1 (k6), P2 (linter), and P3 (docker) are all
        registered. Call this during pre-flight to detect a partially-
        assembled toolchain before any stream is launched.

        Raises:
            RegistryError: If one or more pillar drivers are missing.
        """
        missing = [
            f"{priority.label} → '{key}'"
            for priority, key in _PILLAR_KEYS.items()
            # Check BOTH that the key exists AND that it was registered with the
            # correct Priority.  A driver named "k6" registered as P3_CONSTRUCTION
            # would pass the old `key not in self._drivers` check but satisfy the
            # wrong pillar — the Flight Computer would launch without a real
            # P1 Validation tool.
            if key not in self._drivers or self._priorities.get(key) != priority
        ]
        if missing:
            raise RegistryError(
                f"Incomplete toolchain — missing Essential Pillars: {missing}",
                context={"missing": missing},
            )
        logger.info("REGISTRY: Toolchain verified — all Essential Pillars present.")

    @staticmethod
    def _normalise(name: str) -> str:
        """
        Coerce a driver name to its canonical lowercase form.

        Raises:
            ValueError: If *name* is empty or whitespace-only.
        """
        if not name or not name.strip():
            raise ValueError("Driver name must be a non-empty string.")
        return name.strip().lower()
