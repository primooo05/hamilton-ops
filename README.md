# Hamilton-Ops
> *Speed follows precision. Security comes first.*

A deterministic, security-hardened CI/CD orchestrator for polyglot systems.

---

## What is Hamilton-Ops?

Hamilton-Ops treats the developer machine as **hostile by default**. Every build is isolated, audited, and sanitized before it is allowed to become a production artifact.

Three principles guide every design decision:

- **Isolation over convenience** — sandboxed streams, no shared state
- **Determinism over speed** — predictable outcomes before fast outcomes
- **Auditability over assumption** — every artifact is verified, not trusted

---

## Core Innovation: Out-of-Band Security

Traditional pipelines build first and check later. Hamilton-Ops runs **parallel validation streams** that operate independently of the main build — auditing the software supply chain in real time, detecting secret leaks and cache poisoning, and blocking compromised builds **before image tagging**.

---

## Architecture

### The Flight Computer (Orchestration Layer)

The Python dispatcher acts as a **Supervisor**, not just a subprocess manager.

Each child process runs under a unique session via `os.setsid`, meaning a single `SIGTERM` kills the entire process tree — no zombies, no orphaned k6 helpers. The Quality/Linting stream runs inside minimal read-only containers to prevent arbitrary code execution from malicious plugins.

---

### Pillar A — Hardened Immutable Staging

Before any build begins, the staging environment is sanitized.

A pre-flight **secret scan** (via gitleaks logic) detects `.env`, `.pem`, and other sensitive files and hard-blocks the build if anything is found. A **symlink guard** using `shutil.copytree(symlinks=False)` prevents path traversal attacks that could expose host files like `/etc/passwd`.

---

### Pillar B — Safety State Machine

The guidance system that controls execution safety across three priority streams:

| Priority | Stream | Behavior on Failure |
|---|---|---|
| P1 | Validation (k6) | Hamilton Kill — terminates everything |
| P2 | Quality (Linter) | Warn and continue unless `--strict` |
| P3 | Construction (Docker) | Abort stream, others unaffected |

**The Hamilton Kill** — when P1 trips, the system terminates all running processes, cleans the staging environment, purges temporary containers, and generates forensic logs. k6 runs with `TARGET=localhost` and blocks outbound traffic to private IP ranges to prevent accidental DDoS during builds.

---

### Pillar C — Binary Audit (Clean Capsule Enforcement)

Ensures the final artifact is production-safe through a chain of verifications.

Compiled binaries are extracted from the builder stage, verified via **SHA256 checksum**, and marked read-only. The final image is then scanned for leaked build tools (`gcc`, `mvn`, `npm`) — any presence fails the build. A **Software Bill of Materials** is generated via Syft, flagging any `dev` or `build-essential` dependencies that survived into the production image.

---

### Pillar D — Sanitized Logging & Resource Guardrails

All subprocess output passes through a regex-based sanitizer that automatically redacts secrets (API keys, AWS tokens, etc.) before they reach the terminal or log files.

Resource guardrails detect CPU cores dynamically and cap Docker memory usage (default: 4GB) to prevent build thrash from degrading the host machine.

---

## Technical Stack

| Component | Technology | Hardening |
|---|---|---|
| Orchestrator | Python 3.10+ | `asyncio` + `os.setsid` process isolation |
| Sandbox | Docker (Rootless) | Read-only Alpine containers for linting |
| Cache | BuildKit Mounts | Namespaced via `$PROJECT_HASH` |
| Audit | Syft / Cosign | SBOM generation and binary signing |
| Validation | k6 | Network-restricted (localhost-only) |

---

## CLI

```bash
# Verify your build environment
hamilton doctor

# Run a full parallel build
hamilton ship --project <name>

# Post-build artifact verification
hamilton audit
```

### `hamilton doctor`
Fails if the Docker daemon is running as root or if tool versions have drifted from `.lock` files.

### `hamilton audit`
Produces a verification report. A clean build produces:
- 0 secrets detected
- 0 build tools leaked into production
- Verified SHA256 checksum of binary capsule

---

## Test Coverage

The State Machine (`core/state.py`) requires **100% branch coverage**. This guarantees deterministic failure handling, no race conditions, and reliable kill-switch execution under every condition.

---

## Why "Hamilton"?

During the Apollo 11 moon landing, the Lunar Module's guidance computer was overloaded with rendezvous radar data. Margaret Hamilton's software saved the mission by shedding low-priority tasks and protecting the critical flight path.

Hamilton-Ops applies the same principle: when P1 validation detects a regression, low-priority streams are killed immediately. We don't just build faster — we build with the same priority discipline that got humans to the moon.

---

## License

MIT — See `LICENSE` for details.

*Inspired by Margaret Hamilton, gitleaks, Syft, Cosign, and the k6 team.*