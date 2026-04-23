from core.priorities import (FlightThresholds, Priority, Impact,
                             QualityViolation, BuildError, HamiltonAlarm)
import pytest
from dataclasses import FrozenInstanceError

def  test_thresholds_contract_integrity():
    """
    To verify if thresholds correctly map from config and maintain immutability
    """
    mock_config = {
        "validation": {
            "p95_ms": 300,
            "error_rate": 1.0
        }
    }
    thresholds = FlightThresholds.from_config(mock_config)

    # Contract: Values must match the input
    assert thresholds.p95_ms == 300, "P95  contract breached"
    assert thresholds.error_rate_percent == 1.0, "Error Rate contract breached"

    # Contract: Default values must persist for missing keys
    assert thresholds.p99_ms == 500, "P99 contract breached"

def test_priority_impact_mapping():
    """
    VERIFY: P1 is always mapped to EMERGENCY impact.
    """
    # Contract: P1 must trigger a system-wide abort
    assert Priority.P1_VALIDATION.impact == Impact.EMERGENCY
    assert Priority.P1_VALIDATION.label == "Validation"

def test_immutability_contract():
    """
    VERIFY: Flight thresholds cannot be modified mid-flight.
    """
    thresholds = FlightThresholds()
    with pytest.raises(AttributeError):
        # This should fail because the dataclass is frozen
        thresholds.p95_ms = 10

def test_priority_maps_correct_impact():
    """
    Contract: P1 must always trigger EMERGENCY, never anything softer.
    The supervisor depends on this to know when to kill everything.
    """
    assert Priority.P1_VALIDATION.impact == Impact.EMERGENCY
    assert Priority.P2_QUALITY.impact == Impact.WARN
    assert Priority.P3_CONSTRUCTION.impact == Impact.ABORT

def test_priority_maps_correct_signal():
    """
    Contract: Each priority must raise the right exception type.
    state.py depends on this to route failures correctly.
    """
    assert Priority.P1_VALIDATION.signal is HamiltonAlarm
    assert Priority.P2_QUALITY.signal is QualityViolation
    assert Priority.P3_CONSTRUCTION.signal is BuildError

def test_impact_severity_ordering():
    """
    Contract: EMERGENCY must always outrank ABORT, ABORT must outrank WARN.
    If this breaks, the kill-switch logic in supervisor.py becomes unreliable.
    """
    assert Impact.EMERGENCY > Impact.ABORT > Impact.WARN > Impact.NOTIFY

def test_frozen_thresholds_are_immutable():
    """
    Contract: No one should be able to mutate thresholds mid-flight.
    """
    thresholds = FlightThresholds()
    with pytest.raises(FrozenInstanceError):
        thresholds.p95_ms = 999