from types import SimpleNamespace
import pytest
import logging
from core.priorities import Priority
from core.state import HamiltonAlarm, StateMachine, FlightState, Impact

def test_emergency_kill_switch_transition():
    """
    VERIFY: A HamiltonAlarm triggers a transition to ABORTED.
    (Original test – unchanged, uses existing Priority.P1_VALIDATION)
    """
    fsm = StateMachine()
    fsm.transition_to(FlightState.SHIPPING)

    impact = fsm.handle_signal(HamiltonAlarm("P95 High"), Priority.P1_VALIDATION)

    assert fsm.current == FlightState.ABORTED
    assert not fsm.is_healthy
    assert impact == Impact.EMERGENCY

def test_emergency_from_non_alarm_exception():
    """
    VERIFY: A plain Exception (not HamiltonAlarm) with priority.impact == EMERGENCY
    also triggers transition to ABORTED.
    """
    fsm = StateMachine()
    fsm.transition_to(FlightState.SHIPPING)

    # Use an existing Priority that has Impact.EMERGENCY (e.g., P1_VALIDATION)
    # Or create a mock object if none exists
    emergency_priority = Priority.P1_VALIDATION  # assumes this has impact EMERGENCY

    plain_error = Exception("Catastrophic sensor failure")
    impact_result = fsm.handle_signal(plain_error, emergency_priority)

    assert fsm.current == FlightState.ABORTED
    assert not fsm.is_healthy
    assert impact_result == Impact.EMERGENCY

def test_abort_impact_does_not_transition_to_aborted(caplog):
    """
    VERIFY: When priority.impact == ABORT, the FSM stays in its current state,
    logs an error, and returns Impact.ABORT. No emergency transition occurs.
    """
    caplog.set_level(logging.ERROR)
    fsm = StateMachine()
    fsm.transition_to(FlightState.SHIPPING)

    # Simple mock object (safe and explicit)
    abort_priority = SimpleNamespace(impact=Impact.ABORT, label="TEST_ABORT")
    error = ValueError("Stream failure but not fatal")

    result = fsm.handle_signal(error, abort_priority)

    assert result == Impact.ABORT
    assert fsm.current == FlightState.SHIPPING
    assert fsm.is_healthy is True
    assert caplog.records[0].levelname == "ERROR"
    assert "STREAM FAILURE [TEST_ABORT]" in caplog.records[0].message

def test_warn_impact_logs_warning_and_continues(caplog):
    """
    VERIFY: When priority.impact == WARN, the FSM logs a warning,
    returns Impact.WARN, and makes no state change.
    """
    caplog.set_level(logging.WARNING)
    fsm = StateMachine()
    fsm.transition_to(FlightState.SHIPPING)

    warn_priority = SimpleNamespace(impact=Impact.WARN, label="TEST_WARN")
    anomaly = RuntimeError("High temperature but recoverable")

    result = fsm.handle_signal(anomaly, warn_priority)

    assert result == Impact.WARN
    assert fsm.current == FlightState.SHIPPING
    assert fsm.is_healthy is True

    # Verify log was emitted
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelname == "WARNING"
    assert f"FLIGHT ANOMALY [TEST_WARN]: {anomaly}" in record.message

def test_handle_signal_when_already_aborted():
    """
    VERIFY: Calling handle_signal on an already ABORTED state machine does not
    raise exceptions, remains ABORTED, and still returns the correct impact.
    """
    fsm = StateMachine()
    fsm.transition_to(FlightState.ABORTED)
    assert fsm.current == FlightState.ABORTED
    assert not fsm.is_healthy

    # Any priority works – we just need one that would normally cause a transition
    any_priority = Priority.P1_VALIDATION   # or SimpleNamespace(impact=Impact.EMERGENCY, label="ANY")

    error = Exception("Second failure after abort")
    result = fsm.handle_signal(error, any_priority)

    assert fsm.current == FlightState.ABORTED   # unchanged
    assert not fsm.is_healthy
    assert result == Impact.EMERGENCY           # returned impact is still correct

@pytest.mark.parametrize("initial_state", [
    FlightState.IDLE,
    FlightState.STAGING,
    FlightState.VERIFYING,
    FlightState.SUCCESS,    # even from a "successful" state, alarm should abort
])
def test_hamilton_alarm_from_various_states(initial_state):
    """
    VERIFY: A HamiltonAlarm triggers ABORTED regardless of the current state.
    """
    fsm = StateMachine()
    fsm.transition_to(initial_state)

    alarm = HamiltonAlarm("P95 High")
    result = fsm.handle_signal(alarm, Priority.P1_VALIDATION)

    assert fsm.current == FlightState.ABORTED
    assert not fsm.is_healthy
    assert result == Impact.EMERGENCY