from enum import Enum, IntEnum
from dataclasses import dataclass
from typing import Any
from .exceptions import HamiltonAlarm, QualityViolation, BuildError

class Impact(IntEnum):
    """
    Defines the systemic consequence of a failure
    """
    NOTIFY = 1 # Log only, continue flight
    WARN = 2 # Log and flag, continue unless --strict
    ABORT = 3 # Stop specific stream, this should not kill others
    EMERGENCY = 4 # KILL EVERYTHING (Hamilton KillSwitch)

class Priority(Enum):
    """
    Mapping of Mission Pillars to Systemic Impact.
    """
    P1_VALIDATION = ("Validation",Impact.EMERGENCY, HamiltonAlarm)
    P2_QUALITY = ("Quality",Impact.WARN, QualityViolation)
    P3_CONSTRUCTION = ("Construction",Impact.ABORT, BuildError)

    def __init__(self,label: str, impact: Impact, signal: type):
        self.label = label
        self.impact = impact
        self.signal = signal

@dataclass(frozen=True)
class FlightThresholds:
    """
    Read-only P1 telemetry thresholds loaded from pyproject.toml.
    Frozen post-construction to prevent mid-flight mutation.

    Defaults match the baseline in the Hamilton-Ops specification:
        p95_ms=200, p99_ms=500, error_rate_percent=1.0
    """
    p95_ms: int = 200
    p99_ms: int = 500
    error_rate_percent: float = 1.0

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "FlightThresholds":
        """
        Extract the Threshold from the Pydantic-validated config.
        """
        val = config.get("validation", {})
        return cls(
            p95_ms=val.get("p95_ms",200),
            p99_ms=val.get("p99_ms",500),
            error_rate_percent=val.get("error_rate_percent",1.0),
        )