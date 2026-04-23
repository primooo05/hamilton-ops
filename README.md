```md
# Hamilton-Ops (v2.0 — Secure Edition)

**Tagline**  
*A deterministic, security-hardened CI/CD orchestrator for polyglot systems.*

---

## I. Project Description

**Hamilton-Ops** is built on a simple premise:  
> Speed follows precision. Security comes first.

Unlike traditional CI/CD pipelines that assume a trusted local environment, Hamilton-Ops treats the developer machine as **hostile by default**. Every build is isolated, audited, and sanitized before it is allowed to become a production artifact.

### Core Philosophy
- **Isolation over convenience**
- **Determinism over speed**
- **Auditability over assumption**

---

## II. Core Innovation: Out-of-Band Security

Hamilton-Ops introduces **parallel validation streams** that operate independently of the main build pipeline.

Instead of simply building code, it:
- Audits the **software supply chain in real time**
- Detects **secret leaks, cache poisoning, and tool leakage**
- Blocks compromised builds **before image tagging**

---

## III. System Design: Hardened Architecture

### 1. Orchestration Layer ("Flight Computer")

The Python dispatcher acts as a **Supervisor**, not just a subprocess manager.

#### Process Group Isolation
- Each child process runs under a unique session (`os.setsid`)
- A single `SIGTERM` kills the entire process tree
- Prevents zombie processes (e.g., k6 helpers)

#### Sandbox Execution
- Quality/Linting stream runs inside **minimal containers**
- Prevents arbitrary code execution from malicious plugins

---

### 2. Pillar A: Hardened Immutable Staging

Before any build begins, the staging environment is sanitized.

#### Secret Scan (Pre-Flight)
- Uses **gitleaks logic**
- Detects `.env`, `.pem`, and sensitive files
- Hard-blocks the build if secrets are found

#### Symlink Guard
- Uses `shutil.copytree(symlinks=False)`
- Prevents path traversal attacks (e.g., `/etc/passwd` leaks)

---

### 3. Pillar C: Safety State Machine (Priority Logic)

The **Guidance System** that controls execution safety.

#### P1 Validation ("Radar")
- Runs `k6` with `TARGET=localhost`
- Blocks outbound traffic to private IP ranges
- Prevents accidental DDoS during builds

#### The "Hamilton Kill"
On failure:
- Terminates all running processes
- Cleans staging environment
- Purges temporary containers
- Generates forensic logs

---

### 4. Pillar D: Binary Audit (Clean Capsule Enforcement)

Ensures the final artifact is production-safe.

#### Extraction
- Pulls compiled binaries from builder stage
- Verifies via **SHA256 checksum**
- Marks artifacts as **read-only**

#### Zero-Tooling Verification
- Scans final image for build tools (`gcc`, `mvn`, `npm`)
- Fails if any tooling is present

#### SBOM Generation
- Generates a **Software Bill of Materials**
- Flags `dev` or `build-essential` dependencies

---

### 5. Pillar E: Sanitized Logging & Resource Guardrails

#### Log Redaction
- Regex-based sanitizer on all subprocess output
- Automatically redacts secrets (e.g., API keys, AWS tokens)

#### Resource Guardrails
- Detects CPU cores dynamically
- Limits Docker memory usage (e.g., 4GB)
- Prevents system slowdowns during builds

---

## IV. Technical Specifications

| Component     | Technology         | Hardening Feature                                      |
|--------------|-------------------|--------------------------------------------------------|
| Orchestrator | Python 3.10+      | `asyncio` + `os.setsid` process isolation              |
| Sandbox      | Docker (Rootless) | Read-only Alpine containers for linting                |
| Cache        | BuildKit Mounts   | Namespaced via `$PROJECT_HASH`                         |
| Audit        | Syft / Cosign     | SBOM generation and binary signing                     |
| Validation   | k6                | Network-restricted (localhost-only execution)          |

---

## V. Success Criteria

### `hamilton doctor`
Diagnostic tool that fails when:
- Docker daemon runs as root
- Tool versions drift from `.lock` files

---

### `hamilton audit`
Post-deployment verification report:

- **0 secrets detected**
- **0 build tools leaked into production**
- **Verified SHA256 checksum of binary capsule**

---

### Test Coverage

- **100% coverage on the State Machine**
- Ensures:
  - Deterministic behavior
  - No race conditions
  - Reliable kill-switch execution

---

## Summary

Hamilton-Ops is not just a CI/CD tool—  
it is a **security-first build system** that guarantees every artifact is:

- **Clean**
- **Verified**
- **Production-safe**
```
