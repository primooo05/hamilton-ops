# Hamilton-Ops Glossary

A reference for uncommon or domain-specific terms used throughout the codebase.
When you encounter an unfamiliar term in a docstring or comment, look here first.

---
## F

**Frozen Immutability**
A state where an object's data is locked and cannot be modified after its initial
creation. In Hamilton-Ops, `FlightThresholds` uses `@dataclass(frozen=True)` to
act as a "Read-Only Flight Plan" — the build's thresholds cannot be altered
mid-execution by a race condition or misconfiguration.

> Referenced in: `core/priorities.py` → `FlightThresholds`
---
## I

**Idempotent**
An operation that produces the same result no matter how many times it is called.
In Hamilton-Ops, `_clear_stage()` is idempotent — calling it on an already-clean
staging directory is a safe no-op. This is critical for crash recovery, where the
cleanup path may be triggered more than once.

> Referenced in: `core/stage.py` → `StagingContext.__aexit__`
---
## S

**Symlink (Symbolic Link)**
A file that acts as a pointer or shortcut to another file or directory on the
filesystem. Symlinks are a known attack vector in build systems — a malicious
repo could include a symlink pointing to a sensitive host file (e.g. `/etc/passwd`),
causing it to be copied into the build context. Hamilton-Ops neutralizes this via
`shutil.copytree(symlinks=False)`, which resolves and copies the real file instead
of preserving the pointer.

> Referenced in: `core/stage.py` → `StagingContext.__aenter__`
---
## T

**Telemetry**
The real-time metrics (latency, error rate) fed into the Supervisor during a build.
If actual values deviate from `FlightThresholds`, P1 triggers an Emergency Abort.
In Hamilton-Ops, telemetry is the "sensory input" that allows the system to detect
anomalies before they reach production.

> Referenced in: `core/priorities.py` → `FlightThresholds`
---
## Z

**Zombie (Process / Artifact)**
A process or resource that has completed its task or been killed, but whose entry
remains in the system table because it was never properly reaped or cleaned up.
In Hamilton-Ops, zombie artifacts are staging directories or containers left behind
by a crashed or aborted build. `StagingContext.__aexit__` guarantees cleanup runs
even on failure, and `cleanup_zombies()` provides an emergency reap hook for the
Supervisor during a P1 Hamilton Kill.

> Referenced in: `core/stage.py` → `StagingContext.__aexit__`, `StagingContext.cleanup_zombies`