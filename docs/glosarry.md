# Hamilton-Ops Glossary

A reference for uncommon or domain-specific terms used throughout the codebase.
When you encounter an unfamiliar term in a docstring or comment, look here first.

---
## B

**base linter**
The standard or default tool used to scan code for style and errors. In Hamilton-Ops, the default is `flake8`.

> Referenced in: `drivers/linter_driver.py` → `_DEFAULT_LINTER_CMD`

**BuildError**
An error that occurs during the construction of software (e.g., during a Docker build). It means the "recipe" for creating the final product failed to complete.

> Referenced in: `core/exceptions.py` → `BuildError`
---
## C

**cache poisoning**
A security risk where "dirty" or malicious data is trickily inserted into a system's memory (cache). Hamilton-Ops prevents this by forcing clean builds and isolated workspaces.

> Referenced in: `drivers/docker_driver.py`
---
## D

**Daemon running as root**
When the Docker engine has full administrator control over the computer. This is dangerous because a bug in a build could theoretically take over the entire machine.

> Referenced in: `drivers/docker_driver.py` → `check_health`

**Docker daemon**
The background engine or "brain" that does the heavy lifting for Docker, such as building and running containers.

> Referenced in: `drivers/docker_driver.py` → `check_health`

**docker binary missing**
When the `docker` program itself isn't installed or can't be found on the computer. This is an environment problem that stops the build before it even starts.

> Referenced in: `drivers/docker_driver.py` → `_map_exit_code`
---
## E

**EnvError**
Short for "Environment Error." It means the computer running Hamilton-Ops is missing a required tool (like k6 or Docker) or is configured incorrectly.

> Referenced in: `core/exceptions.py` → `EnvError`

**error_rate**
The percentage of tasks or requests that failed compared to the total number attempted. It's a way to measure how "broken" the system is during a test.

> Referenced in: `core/priorities.py` → `FlightThresholds`
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
## K

**k6**
A performance testing tool used to "stress test" your application by simulating many people using it at once to see where it breaks.

> Referenced in: `drivers/k6_driver.py`
---
## L

**live source tree**
The actual folders and files on your computer where you are currently writing and editing your code. Hamilton-Ops copies these to a staging directory to keep them safe during builds.

> Referenced in: `core/stage.py`
---
## N

**newline-delimited**
A way of formatting data (like logs) where each new piece of information is placed on its own brand-new line, making it easier for computers and people to read.

> Referenced in: `drivers/k6_driver.py`

**Non-zero exit**
When a computer program finishes, it sends back a number. `0` means "Success," and any other number (non-zero) means "Something went wrong."

> Referenced in: `drivers/registry.py`
---
## O

**OOM-killed**
Stands for "Out Of Memory." This happens when a task tries to use more RAM than the computer has available, forcing the system to kill the task to prevent a total crash.

> Referenced in: `drivers/k6_driver.py` → `_map_exit_code`
---
## P

**p95**
A measurement meaning "95% of events were faster than this." It helps you ignore the absolute slowest outliers and see how the system performs for the vast majority of users.

> Referenced in: `core/priorities.py` → `FlightThresholds`

**p99**
A stricter version of p95, meaning "99% of events were faster than this." It focuses on the experience of almost everyone, including those who had a slightly slower experience.

> Referenced in: `core/priorities.py` → `FlightThresholds`
---
## R

**race conditions**
A type of bug where the outcome of a task depends on the unpredictable timing of other tasks. It's like two people trying to walk through a narrow door at the exact same time.

> Referenced in: `core/stage.py`

**registry**
The central "phonebook" of Hamilton-Ops. It maps tool names (like "k6") to the specific code needed to run them, keeping the system organized.

> Referenced in: `drivers/registry.py`

**rootless**
Running tools (like Docker) without giving them full administrator control. This keeps the rest of the computer safe even if something goes wrong inside the build.

> Referenced in: `drivers/docker_driver.py` → `check_health`
---
## S

**STAGING directory**
A temporary, "clean-room" workspace where your code is copied before the build starts. This ensures the build isn't affected by messy files on your personal computer.

> Referenced in: `core/stage.py` → `StagingContext`

**Stateless translation layer**
A part of the code (like a driver) that converts high-level instructions into tool-specific commands without needing to remember what happened in the past.

> Referenced in: `drivers/registry.py`

**Subprocess**
A separate task or program started by Hamilton-Ops to do a specific job, like running a linter or building a Docker image.

> Referenced in: `drivers/k6_driver.py` → `_run_subprocess`

**Supervisor**
The "pilot" or main controller of the system. It watches over all the different tasks (P1, P2, P3) and decides when to keep flying or when to perform an emergency landing.

> Referenced in: `core/state.py`

**Symlink (Symbolic Link)**
A file that acts as a pointer or shortcut to another file or directory on the
filesystem. Symlinks are a known attack vector in build systems — a malicious
repo could include a symlink pointing to a sensitive host file (e.g. `/etc/passwd`),
causing it to be copied into the build context. Hamilton-Ops neutralizes this via
`shutil.copytree(symlinks=False)`, which resolves and copies the real file instead
of preserving the pointer.

> Referenced in: `core/stage.py` → `StagingContext.__aenter__`

**Tool-agnostic**
Software designed to work with any tool (e.g., any linter) rather than being hard-coded to just one specific tool.

> Referenced in: `drivers/linter_driver.py`
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