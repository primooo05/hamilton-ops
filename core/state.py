"""
Hamilton Guidance Controller

Manages the lifecycle of a Hamilton flight through a Finite State Machine (FSM).
Handles transitions between IDLE, SHIP, and ABORT states based on
telemetry and Priority-based signals.
"""
import logging
from enum import Enum, auto
from typing import Optional

from .exceptions import HamiltonAlarm
from .priorities import Priority, Impact
logger = logging.getLogger("hamilton.state")

class FlightState(Enum):
    """The lifecycle stages of a Hamilton execution."""
    IDLE = auto()       # Pre-flight / Ready
    STAGING = auto()    # Immutable snapshot in progress
    SHIPPING = auto()   # Parallel P1/P2/P3 streams active
    VERIFYING = auto()  # Post-build Audit Chain active
    SUCCESS = auto()    # Mission accomplished
    ABORTED = auto()    # Emergency landing triggered by Alarm

class StateMachine:
    """
    Finite State Machine (FSM) for mission guidance.

    Ensures that transitions are deterministic and that 'Alarms'
    trigger the correct systemic cleanup.
    """

    def __init__(self):
        self.current: FlightState = FlightState.IDLE
        self._failure_reason: Optional[str] = None

    def transition_to(self, next_state: FlightState):
        """
        Executes a surgical state transition.
        Logs every movement for a flight telemetry
        :param next_state:
        :return:
        """
        logger.info(f"GUIDANCE: Transitioning {self.current.name} -> {next_state.name}")
        self.current = next_state

    def handle_signal(self, error: Exception, priority: Priority):
        """
        Determines the systemic impact of a stream failure

        Logic:
        - If impact is EMERGENCY: Transition to ABORTED and trigger Hamilton Kill Switch.
        - If impact is ABORT: Log failure and stop the specific stream.
        - If impact is WARN: Log as anomaly but maintain flight path.
        :param error:
        :param priority:
        :return:
        """
        if isinstance(error, HamiltonAlarm) or priority.impact == Impact.EMERGENCY:
            self._failure_reason = str(error)
            self.transition_to(FlightState.ABORTED)
            return Impact.EMERGENCY
        if priority.impact == Impact.ABORT:
            logger.error(f"STREAM FAILURE [{priority.label}]: {error}")
            return Impact.ABORT
        logger.warning(f"FLIGHT ANOMALY [{priority.label}]: {error}")
        return Impact.WARN

    @property
    def is_healthy(self) -> bool:
        """
        Returns True if the Flight has not been ABORTED
        :return:
        """
        return self.current != FlightState.ABORTED



