"""
Manual P1 Alarm Verification Script
=====================================
Simulates a "high VU count" build by spawning a long-running subprocess
(python -c "import time; time.sleep(60)") and then triggering terminate()
to verify the process group is reaped immediately.
"""

import sys
import time
import subprocess
import tempfile
import logging
from pathlib import Path

# Ensure project root is on the path (go up from tests/drivers/test_construction/)
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from drivers.construction import ConstructionDriver
from core.exceptions import BuildError

# --- Logging Configuration ---
# Configure a clean output for the manual verification script.
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s | %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("hamilton.test.p1_alarm")

class SimulatedBuildDriver(ConstructionDriver):
    """Overrides _build_popen to spawn a multi-process dummy build."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.child_pid_file = Path(tempfile.gettempdir()) / "hamilton_test_child.pid"

    def _build_popen(self, cmd):
        if self.child_pid_file.exists():
            self.child_pid_file.unlink()
            
        logger.info("[SIM] Spawning dummy multi-process build (Parent + Child)...")
        # Child writes its PID to a file then sleeps.
        child_script = (
            "import os, time; "
            f"open('{self.child_pid_file.as_posix()}', 'w').write(str(os.getpid())); "
            "time.sleep(60)"
        )
        parent_script = (
            "import subprocess, sys, time; "
            "print('PARENT: Spawning child...'); "
            f"subprocess.Popen([sys.executable, '-c', \"{child_script}\"], "
            "                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
            "time.sleep(1); "  # Give the child a moment to write its PID file
            "print('PARENT: Burning CPU...'); "
            "time.sleep(60)"
        )
        return subprocess.Popen(
            [sys.executable, "-c", parent_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )


def is_process_alive(pid):
    """Check if a process is alive without using psutil."""
    import os
    if pid <= 0: return False
    try:
        if hasattr(os, "kill"):
            os.kill(pid, 0)
        else:
            # Windows fallback
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], 
                                 capture_output=True, text=True).stdout
            return str(pid) in out
    except OSError:
        return False
    return True


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("Hamilton-Ops — Manual P1 Alarm Verification")
    logger.info("=" * 60)

    # Use a real temp directory for the stage path to avoid resolution warnings
    with tempfile.TemporaryDirectory() as tmp_stage:
        driver = SimulatedBuildDriver(stage_path=tmp_stage, image_tag="test:alarm")

        import threading

        def trigger_alarm_after(seconds):
            time.sleep(seconds)
            logger.warning("[P1 ALARM] HamiltonAlarm fired after %ds! Calling terminate()...", seconds)
            driver.terminate()

        # Fire the P1 alarm after 2 seconds
        alarm_thread = threading.Thread(target=trigger_alarm_after, args=(2,))
        alarm_thread.start()

        logger.info("[CONSTRUCTION] run() started — multi-process dummy build running...")
        start = time.monotonic()
        caught_build_error = False
        aborted_flag = False

        try:
            driver.run()
            logger.info("[CONSTRUCTION] Build completed normally (unexpected in this test).")
        except BuildError as exc:
            elapsed = time.monotonic() - start
            caught_build_error = True
            aborted_flag = exc.context.get("aborted", False)
            logger.info("[CONSTRUCTION] Caught BuildError as expected after %.2fs.", elapsed)
            logger.info("[CONSTRUCTION] Context: %s", exc.context)
        except Exception as exc:
            logger.error("[ERROR] Caught unexpected exception type: %s: %s", type(exc).__name__, exc)

        alarm_thread.join()

        # Validation logic
        logger.info("-" * 60)
        success = True
        
        if not caught_build_error:
            logger.error("FAIL: Expected BuildError was not raised.")
            success = False
        
        if not aborted_flag:
            logger.error("FAIL: BuildError context missing 'aborted': True.")
            success = False
            
        # The build should stop around 2s (SIGTERM) or up to 7s (SIGKILL escalation)
        elapsed = time.monotonic() - start
        if elapsed > 15:
            logger.error("FAIL: Build took too long to terminate (%.2fs). Escalation failed?", elapsed)
            success = False

        if driver._proc is not None:
            logger.error("FAIL: driver._proc was not cleared after terminate().")
            success = False

        # --- Surgical Reaping Check (Multi-process) ---
        import os
        if driver.child_pid_file.exists():
            child_pid = int(driver.child_pid_file.read_text())
            child_alive = is_process_alive(child_pid)
            
            if hasattr(os, "killpg"):
                # POSIX: Surgical reaping must kill the entire group
                if child_alive:
                    logger.error("FAIL: Surgical reaping failed — Child PID %d is still alive.", child_pid)
                    success = False
                else:
                    logger.info("SUCCESS: Surgical reaping — Child PID %d was reaped.", child_pid)
            else:
                # Windows: kill() only reaps parent
                if child_alive:
                    logger.info("INFO: Windows fallback (kill) — Child PID %d remains (expected).", child_pid)
                else:
                    logger.info("SUCCESS: Child PID %d reaped (unexpected but welcome on Windows).", child_pid)
            
            driver.child_pid_file.unlink()
        else:
            logger.warning("WARNING: Could not verify surgical reaping — Child PID file not found.")

        result_msg = "SUCCESS [OK]" if success else "FAILED [FAIL]"
        logger.info("RESULT: P1 Alarm Reaping %s", result_msg)
        logger.info("=" * 60)


