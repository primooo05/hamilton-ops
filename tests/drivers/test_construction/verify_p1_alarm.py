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
from pathlib import Path

# Ensure project root is on the path (go up from tests/drivers/test_construction/)
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from drivers.construction import ConstructionDriver


from core.exceptions import BuildError

class SimulatedBuildDriver(ConstructionDriver):
    """Overrides _build_popen to spawn a multi-process dummy build."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.child_pid_file = Path(tempfile.gettempdir()) / "hamilton_test_child.pid"

    def _build_popen(self, cmd):
        if self.child_pid_file.exists():
            self.child_pid_file.unlink()
            
        print(f"  [SIM] Spawning dummy multi-process build (Parent + Child)...")
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
    print("=" * 60)
    print("Hamilton-Ops — Manual P1 Alarm Verification")
    print("=" * 60)

    # Use a real temp directory for the stage path to avoid resolution warnings
    with tempfile.TemporaryDirectory() as tmp_stage:
        driver = SimulatedBuildDriver(stage_path=tmp_stage, image_tag="test:alarm")

        import threading

        def trigger_alarm_after(seconds):
            time.sleep(seconds)
            print(f"\n  [P1 ALARM] HamiltonAlarm fired after {seconds}s! Calling terminate()...")
            driver.terminate()

        # Fire the P1 alarm after 2 seconds
        alarm_thread = threading.Thread(target=trigger_alarm_after, args=(2,))
        alarm_thread.start()

        print("\n  [CONSTRUCTION] run() started — multi-process dummy build running...")
        start = time.monotonic()
        caught_build_error = False
        aborted_flag = False

        try:
            driver.run()
            print("  [CONSTRUCTION] Build completed normally (unexpected in this test).")
        except BuildError as exc:
            elapsed = time.monotonic() - start
            caught_build_error = True
            aborted_flag = exc.context.get("aborted", False)
            print(f"\n  [CONSTRUCTION] Caught BuildError as expected after {elapsed:.2f}s.")
            print(f"  [CONSTRUCTION] Context: {exc.context}")
        except Exception as exc:
            print(f"\n  [ERROR] Caught unexpected exception type: {type(exc).__name__}: {exc}")

        alarm_thread.join()

        # Validation logic
        print("-" * 60)
        success = True
        
        if not caught_build_error:
            print("FAIL: Expected BuildError was not raised.")
            success = False
        
        if not aborted_flag:
            print("FAIL: BuildError context missing 'aborted': True.")
            success = False
            
        # The build should stop around 2s (SIGTERM) or up to 7s (SIGKILL escalation)
        elapsed = time.monotonic() - start
        if elapsed > 15:
            print(f"FAIL: Build took too long to terminate ({elapsed:.2f}s). Escalation failed?")
            success = False

        if driver._proc is not None:
            print("FAIL: driver._proc was not cleared after terminate().")
            success = False

        # --- Surgical Reaping Check (Multi-process) ---
        import os
        if driver.child_pid_file.exists():
            child_pid = int(driver.child_pid_file.read_text())
            child_alive = is_process_alive(child_pid)
            
            if hasattr(os, "killpg"):
                # POSIX: Surgical reaping must kill the entire group
                if child_alive:
                    print(f"FAIL: Surgical reaping failed — Child PID {child_pid} is still alive.")
                    success = False
                else:
                    print(f"SUCCESS: Surgical reaping — Child PID {child_pid} was reaped.")
            else:
                # Windows: kill() only reaps parent
                if child_alive:
                    print(f"INFO: Windows fallback (kill) — Child PID {child_pid} remains (expected).")
                else:
                    print(f"SUCCESS: Child PID {child_pid} reaped (unexpected but welcome on Windows).")
            
            driver.child_pid_file.unlink()
        else:
            print("WARNING: Could not verify surgical reaping — Child PID file not found.")

        print("\nRESULT: P1 Alarm Reaping " + ("SUCCESS [OK]" if success else "FAILED [FAIL]"))
        print("=" * 60)

