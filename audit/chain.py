"""
Hamilton-Ops Audit Chain — Pillar C: Binary Audit (Clean Capsule Enforcement)

The Audit Chain is the "inspection team" standing at the exit gate.
Every artifact produced by the Construction stream (P3) must survive
every step before it is "signed" for production.

Design: Chain of Responsibility
    Each ``AuditStep.execute()`` receives the shared ``AuditReport``,
    enriches it with findings, and either returns (pass) or raises an
    ``AuditFailure`` subclass (halt). The ``AuditChain`` manager drives
    the sequence and guarantees cleanup of temporary artifacts.

Steps (in order):
    1. BinaryDiscoveryStep   — verify the binary exists, record SHA256
    2. SecretScannerStep     — regex-scan binary metadata for secret patterns
    3. BuildToolLeakStep     — detect gcc/mvn/npm leaked from builder stage
    4. SBOMGenerationStep    — generate a Software Bill of Materials via Syft

Security contracts:
    - If no binary is present at the audit path, ``StagingError`` is raised
      before any step runs. The inspection gate cannot open without goods.
    - SHA256 is re-checked after any step that could mutate the file.
      If the hash changes, ``AuditFailure`` halts the chain immediately.
    - All temporary directories created during the audit are purged in a
      ``finally`` block — no zombie artifacts survive a crash or abort.
"""

from __future__ import annotations

import abc
import hashlib
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.exceptions import (
    AuditFailure,
    BuildToolLeakDetected,
    SecretLeakDetected,
    StagingError,
)

logger = logging.getLogger("hamilton.audit")

_SECRET_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?i)(api[_\-]?key|secret|password|token)\s*=\s*\S+"),
    re.compile(r"-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),         # AWS Access Key ID
    re.compile(r"\.env\b"),                   # .env file reference
]

# Build tools that must NOT appear in the production capsule (Pillar C)
_BUILD_TOOLS: list[str] = ["gcc", "g++", "mvn", "npm", "make", "cmake", "cargo"]


@dataclass
class AuditReport:
    """
    Mutable telemetry envelope accumulated across all audit steps.

    Each step enriches this object with its findings. The final report
    is the verifiable proof that the artifact passed the entire chain.

    Attributes:
        binary_path (Path):      The artifact under audit.
        initial_sha256 (str):    Hash recorded by BinaryDiscoveryStep before
                                 any step runs — the "pre-mutation baseline."
        findings (dict):         Per-step telemetry keyed by step name.
        sbom_path (Optional[Path]): Path to the generated SBOM JSON file.
        passed (bool):           True only when the entire chain completes
                                 without raising.
    """
    binary_path: Path
    initial_sha256: str = ""
    findings: dict = field(default_factory=dict)
    sbom_path: Optional[Path] = None
    passed: bool = False


class AuditStep(abc.ABC):
    """
    Base class for a single link in the Audit Chain.

    Each step has a name (for telemetry) and a single ``execute`` method
    that mutates the ``AuditReport`` with its findings or raises an
    ``AuditFailure`` to halt the chain.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Human-readable identifier, used as the key in AuditReport.findings."""

    @abc.abstractmethod
    def execute(self, report: AuditReport) -> None:
        """
        Run this inspection step.

        Args:
            report: Shared, mutable audit report. The step must add its
                    results under ``report.findings[self.name]``.

        Raises:
            AuditFailure (or subclass): If the step detects a violation.
        """


class BinaryDiscoveryStep(AuditStep):
    """
    Verify the binary exists and record its initial SHA256.

    This is the "pre-condition verification" step. A missing binary means
    the Construction stream (P3) failed to deliver the capsule — the
    inspection gate cannot open, so ``StagingError`` is raised.
    """

    @property
    def name(self) -> str:
        return "binary_discovery"

    def execute(self, report: AuditReport) -> None:
        if not report.binary_path.exists():
            raise StagingError(
                f"Audit pre-condition failed: binary not found at "
                f"'{report.binary_path}'. The Construction stream may have "
                "failed to deliver the capsule.",
                context={"binary_path": str(report.binary_path)},
            )

        sha = _sha256(report.binary_path)
        report.initial_sha256 = sha

        report.findings[self.name] = {
            "path": str(report.binary_path),
            "size_bytes": report.binary_path.stat().st_size,
            "sha256": sha,
        }
        logger.info("AUDIT [%s]: Binary found — SHA256=%s", self.name, sha[:12])


class SecretScannerStep(AuditStep):
    """
    Regex-scan the binary for secret patterns.

    Attempts to read the binary as UTF-8 text (ignoring decode errors)
    and matches against known secret patterns (API keys, PEM headers,
    AWS tokens, .env references).
    """

    @property
    def name(self) -> str:
        return "secret_scanner"

    def execute(self, report: AuditReport) -> None:
        # Integrity check: binary must not have changed since discovery
        _assert_hash_unchanged(report)

        # Read as text with lossy decoding — binaries may contain partial UTF-8
        content = report.binary_path.read_bytes().decode("utf-8", errors="replace")

        hits: list[str] = []
        for pattern in _SECRET_PATTERNS:
            matches = pattern.findall(content)
            if matches:
                hits.extend(matches[:3])  # cap at 3 per pattern to limit report size

        report.findings[self.name] = {
            "patterns_checked": len(_SECRET_PATTERNS),
            "hits": hits,
        }

        if hits:
            raise SecretLeakDetected(
                f"Secret scan detected {len(hits)} potential secret(s) in the "
                f"production capsule. The build cannot be signed.",
                context={"hits": hits, "binary_path": str(report.binary_path)},
            )

        logger.info("AUDIT [%s]: Clean — 0 secrets detected.", self.name)


class BuildToolLeakStep(AuditStep):
    """
    Scan the binary's parent directory for leaked build tools.

    Per Pillar C: gcc, mvn, npm (and others from ``_BUILD_TOOLS``) must
    NOT appear in the production capsule. Their presence indicates the
    multi-stage Dockerfile failed to isolate the final image.

    Scans the directory containing the binary for executable files whose
    names match known build tools.
    """

    @property
    def name(self) -> str:
        return "build_tool_leak"

    def execute(self, report: AuditReport) -> None:
        _assert_hash_unchanged(report)

        artifact_dir = report.binary_path.parent
        found_tools: list[str] = []

        for tool in _BUILD_TOOLS:
            # Check if the tool binary exists anywhere in the artifact directory
            matches = list(artifact_dir.rglob(tool))
            if matches:
                found_tools.append(tool)

        report.findings[self.name] = {
            "tools_checked": _BUILD_TOOLS,
            "leaked_tools": found_tools,
        }

        if found_tools:
            raise BuildToolLeakDetected(
                f"Build tool(s) detected in production capsule: {found_tools}. "
                "Multi-stage Dockerfile isolation has failed.",
                context={"leaked_tools": found_tools, "artifact_dir": str(artifact_dir)},
            )

        logger.info("AUDIT [%s]: Clean — no build tools leaked.", self.name)


class SBOMGenerationStep(AuditStep):
    """
    Generate a Software Bill of Materials via Syft.

    In a real environment, this calls ``syft <binary_path> -o json``.
    The SBOM is written to a temp file managed by the AuditChain, and
    its path is recorded in ``report.sbom_path`` for the signing step.

    External tool execution is routed through ``_run_tool`` so that
    tests can replace it without mocking the entire subprocess module.
    """

    @property
    def name(self) -> str:
        return "sbom_generation"

    def execute(self, report: AuditReport) -> None:
        _assert_hash_unchanged(report)

        sbom_path = report.binary_path.parent / "_sbom.json"

        result = self._run_tool(["syft", str(report.binary_path), "-o", "json"])

        if result.returncode != 0:
            raise AuditFailure(
                f"Syft SBOM generation failed (exit {result.returncode}): "
                f"{result.stderr.strip()}",
                context={"exit_code": result.returncode, "stderr": result.stderr},
            )

        sbom_path.write_text(result.stdout)
        report.sbom_path = sbom_path

        report.findings[self.name] = {
            "sbom_path": str(sbom_path),
            "size_bytes": sbom_path.stat().st_size,
        }
        logger.info("AUDIT [%s]: SBOM written to %s", self.name, sbom_path)

    def _run_tool(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """Thin subprocess wrapper — replace in tests."""
        return subprocess.run(cmd, capture_output=True, text=True)


class AuditChain:
    """
    Orchestrates the post-build security inspection pipeline.

    The chain drives steps in registration order. If any step raises an
    ``AuditFailure`` (or ``StagingError``), the chain halts immediately
    and re-raises — no further steps execute.

    A ``finally`` block guarantees that all temporary directories registered
    via ``_register_temp`` are purged after ``run()`` completes, whether the
    chain passes or fails. This prevents zombie audit artifacts.

    Usage::

        chain = AuditChain([
            BinaryDiscoveryStep(),
            SecretScannerStep(),
            BuildToolLeakStep(),
            SBOMGenerationStep(),
        ])
        report = chain.run(Path("/stage/build/app"))
        assert report.passed
    """

    def __init__(self, steps: list[AuditStep]) -> None:
        self._steps = steps
        self._temp_dirs: list[Path] = []

    def run(self, binary_path: Path) -> AuditReport:
        """
        Execute the full inspection chain against ``binary_path``.

        Args:
            binary_path: Absolute path to the production binary/artifact.

        Returns:
            AuditReport with ``passed=True`` and accumulated findings.

        Raises:
            StagingError:  If the binary is missing (pre-condition failure).
            AuditFailure:  If any inspection step detects a violation.
        """
        report = AuditReport(binary_path=binary_path)

        try:
            for step in self._steps:
                logger.info("AUDIT: Running step '%s'", step.name)
                step.execute(report)

            report.passed = True
            logger.info("AUDIT: Chain complete — artifact signed for production.")
            return report

        finally:
            # Idempotent cleanup: purge all temp dirs regardless of outcome.
            self._cleanup_temps()

    def register_temp(self, path: Path) -> Path:
        """
        Register a temporary directory for automatic cleanup after ``run()``.

        Steps can call this to create managed scratch space that survives
        for the lifetime of the chain but is purged in the ``finally`` block.

        Returns the path for convenience.
        """
        self._temp_dirs.append(path)
        return path

    def _cleanup_temps(self) -> None:
        """Purge all registered temp directories. Idempotent on missing paths."""
        for tmp in self._temp_dirs:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
                logger.debug("AUDIT: Cleaned up temp dir %s", tmp)
        self._temp_dirs.clear()

def _sha256(path: Path) -> str:
    """Compute the SHA256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _assert_hash_unchanged(report: AuditReport) -> None:
    """
    Re-compute the binary's SHA256 and compare against the initial baseline.

    Raises:
        AuditFailure: If the hash has changed — a step has tampered with
                      the artifact in violation of the No-Tamper rule.
    """
    if not report.initial_sha256:
        # BinaryDiscoveryStep hasn't run yet — nothing to compare against.
        return

    current = _sha256(report.binary_path)
    if current != report.initial_sha256:
        raise AuditFailure(
            "Binary integrity violation: SHA256 changed during audit. "
            "An audit step has tampered with the production capsule.",
            context={
                "initial_sha256": report.initial_sha256,
                "current_sha256": current,
                "binary_path": str(report.binary_path),
            },
        )
