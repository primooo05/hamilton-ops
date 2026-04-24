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
from pathlib import Path

# Ensure project root is on the path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drivers.construction import ConstructionDriver


class SimulatedBuildDriver(ConstructionDriver):
    """Overrides _build_popen to spawn a long-running dummy instead of docker."""
    def _build_popen(self, cmd):
        print(f"  [SIM] Spawning dummy long-running build process...")
        return subprocess.Popen(
            [sys.executable, "-c", "import time; print('BUILD: Burning CPU...'); time.sleep(60)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )


if __name__ == "__main__":
    print("=" * 60)
    print("Hamilton-Ops — Manual P1 Alarm Verification")
    print("=" * 60)

    driver = SimulatedBuildDriver(stage_path="/tmp/stage", image_tag="test:alarm")

    import threading

    def trigger_alarm_after(seconds):
        time.sleep(seconds)
        print(f"\n  [P1 ALARM] HamiltonAlarm fired after {seconds}s! Calling terminate()...")
        driver.terminate()

    # Fire the P1 alarm after 2 seconds
    alarm_thread = threading.Thread(target=trigger_alarm_after, args=(2,))
    alarm_thread.start()

    print("\n  [CONSTRUCTION] run() started — dummy build running...")
    start = time.monotonic()
    try:
        driver.run()
        print("  [CONSTRUCTION] Build completed normally (unexpected in this test).")
    except Exception as exc:
        elapsed = time.monotonic() - start
        print(f"\n  [CONSTRUCTION] Aborted in {elapsed:.2f}s — received: {type(exc).__name__}: {exc}")

    alarm_thread.join()
    print("\nRESULT: Process group reaped successfully. [OK]" if driver._proc is None else "RESULT: _proc still alive - FAIL!")
