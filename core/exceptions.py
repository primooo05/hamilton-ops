"""
Hamilton-Ops Exception Hierarchy

This module defines the telemetry signals for the Hamilton flight computer.
Exceptions are categorized by their priority (P1/P2/P3) and their impact
on the asynchronous supervisor's lifecycle.
"""

from typing import Any,Optional

class HamiltonError(Exception):
    """
    Base class for all Hamilton-Ops telemetry signals.

    Attributes:
        message (str): A human-readable description of the failure.
        context (dict): Arbitrary metadata (e.g., latency metrics, PIDs)
                        used by the logger for surgical reporting.
    """
    def __init__(self, message: str, context: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.context = context or {}

# --- P1: The Kill Switch ---
class HamiltonAlarm(HamiltonError):
    """
    Priority 1 (P1) Alarm: System-wide abort signal.

    Raised by validation drivers (like k6) when safety or performance
    thresholds are breached. Raising this within an asyncio.TaskGroup
    triggers the immediate cancellation of all sibling build tasks (P3).
    """
    pass

class ThresholdExceededError(HamiltonAlarm):
    """
    Raised when system performance metrics deviate from the flight plan.
    Example: P95 latency > 200ms or Error Rate > 1%.
    """
    pass

# --- P2/P3: Execution Errors ---
class BuildError(HamiltonError):
    """
    Priority 3 (P3) Failure: Construction failure.

    Raised when the Docker/BuildKit engine fails to compile, tag,
    or push an image. This does not necessarily kill P1/P2 streams
    but marks the ship as 'Aborted'.
    """
    pass

class QualityViolation(HamiltonError):
    """
    Priority 2 (P2) Warning/Failure: Code hygiene violation.

    Raised when linters or formatters detect 'dirty' code. If Hamilton
    is in --strict mode, this behaves like a P1 Alarm; otherwise,
    it is logged as a non-breaking flight anomaly.
    """
    pass

# --- Audit & Security ---
class AuditFailure(HamiltonError):
    """
    Security Protocol Violation.

    Raised during the post-build Chain of Responsibility if the
    production capsule fails safety verification (e.g., binary leakage).
    """
    pass

class SecretLeakDetected(AuditFailure):
    """
    Raised when the pre-build or post-build scanner identifies
    unencrypted secrets (API keys, .env, .pem) in the staging area.
    """
    pass

class BuildToolLeakDetected(AuditFailure):
    """
    Raised when the binary audit discovers build tools (gcc, mvn, npm, etc.)
    that survived into the production image.

    Per Pillar C, any dev/build-essential dependency in the final capsule
    is a hard failure — it indicates the multi-stage Dockerfile is broken
    and the image is not production-safe.
    """
    pass

# --- System & Environment ---
class EnvError(HamiltonError):
    """
    Pre-flight Failure.

    Raised by 'hamilton doctor' when the host environment (Docker,
    rootless mode, or dependency versions) does not match the
    required mission specifications.
    """
    pass

class StagingError(HamiltonError):
    """
    Workspace Failure.

    Raised when the orchestrator fails to create, lock, or clean
    the immutable snapshot directory (.hamilton/stage/).
    """
    pass

class DriverNotFoundError(HamiltonError):
    """
    Registry Lookup Failure.

    Raised by DriverRegistry.get() when a requested tool name
    does not exist in the phonebook. Provides a deterministic
    failure instead of a raw Python KeyError, enabling the
    Supervisor to emit a meaningful telemetry signal.
    """
    pass


class RegistryError(HamiltonError):
    """
    Registry Integrity Violation.

    Raised when code attempts to overwrite an already-registered
    driver. Enforces the 'No-Hijack' rule: once a driver is locked
    into the Registry, it cannot be replaced at runtime — preventing
    hostile injection of malicious functions into the toolchain.
    """
    pass


