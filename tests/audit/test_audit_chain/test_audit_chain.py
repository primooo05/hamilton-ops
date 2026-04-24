"""
Contract tests for audit/chain.py

Strategy: All tests work against real files on the ``tmp_path`` fixture —
no file-system mocking. External tool calls (Syft) are patched at the
``SBOMGenerationStep._run_tool`` instance level, consistent with the
driver testing pattern already established in this codebase.

Test categories:
  1. "Clean Room" — pre-condition verification (binary must exist)
  2. "No-Tamper" — SHA256 integrity across non-destructive steps
  3. Context Accumulation — AuditReport contains all step findings
  4. Post-Audit Cleanup — temp dirs are purged after run()
  5. Chain-Break — a failing step prevents downstream steps from executing
  6. Secret Scanner — pattern detection and clean-path verification
  7. Build Tool Leak — presence/absence of gcc, mvn, npm
  8. SBOM Generation — Syft tool mock, path recorded in report
"""

import hashlib
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from audit.chain import (
    AuditChain,
    AuditReport,
    BinaryDiscoveryStep,
    BuildToolLeakStep,
    SBOMGenerationStep,
    SecretScannerStep,
    _sha256,
)
from core.exceptions import (
    AuditFailure,
    BuildToolLeakDetected,
    SecretLeakDetected,
    StagingError,
)


def _write_binary(path: Path, content: bytes = b"\x7fELF fake binary content") -> Path:
    """Write a minimal fake binary to *path* and return the path."""
    path.write_bytes(content)
    return path


def _completed(returncode=0, stdout="", stderr="") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _minimal_chain(tmp_path, extra_steps=None):
    """Return an AuditChain with Discovery + any extra steps."""
    steps = [BinaryDiscoveryStep()] + (extra_steps or [])
    return AuditChain(steps)


def test_run_raises_staging_error_when_binary_missing(tmp_path):
    """
    Contract: run() must raise StagingError (not AuditFailure) when the
    binary path does not exist. The inspection gate requires actual goods.
    """
    missing = tmp_path / "nonexistent_binary"
    chain = AuditChain([BinaryDiscoveryStep()])

    with pytest.raises(StagingError) as exc_info:
        chain.run(missing)

    assert "binary not found" in str(exc_info.value).lower()
    assert exc_info.value.context["binary_path"] == str(missing)


def test_run_raises_staging_error_not_audit_failure_for_missing_binary(tmp_path):
    """
    Contract: StagingError is a distinct signal from AuditFailure.
    A missing binary means construction failed, not that the audit found issues.
    """
    missing = tmp_path / "ghost"
    chain = AuditChain([BinaryDiscoveryStep()])

    with pytest.raises(StagingError) as exc_info:
        chain.run(missing)

    assert not isinstance(exc_info.value, AuditFailure)


def test_binary_discovery_step_records_sha256_in_report(tmp_path):
    """
    Contract: After BinaryDiscoveryStep, report.initial_sha256 must be set
    and must match the actual SHA256 of the binary.
    """
    binary = _write_binary(tmp_path / "app")
    chain = AuditChain([BinaryDiscoveryStep()])
    report = chain.run(binary)

    expected_sha = _sha256(binary)
    assert report.initial_sha256 == expected_sha


def test_binary_discovery_step_records_findings(tmp_path):
    """
    Contract: BinaryDiscoveryStep must add its results under
    report.findings['binary_discovery'] with path, size, and sha256.
    """
    binary = _write_binary(tmp_path / "app")
    chain = AuditChain([BinaryDiscoveryStep()])
    report = chain.run(binary)

    assert "binary_discovery" in report.findings
    d = report.findings["binary_discovery"]
    assert "sha256" in d
    assert "size_bytes" in d
    assert d["path"] == str(binary)


def test_sha256_utility_computes_correct_hash(tmp_path):
    """
    Contract: _sha256() must produce the same hex digest as hashlib.sha256.
    If this fails, the entire No-Tamper system is broken.
    """
    content = b"Hamilton-Ops payload"
    f = tmp_path / "payload"
    f.write_bytes(content)

    expected = hashlib.sha256(content).hexdigest()
    assert _sha256(f) == expected


def test_secret_scanner_does_not_mutate_binary(tmp_path):
    """
    Contract: SecretScannerStep must not alter the binary — its SHA256 must
    be identical before and after the scan (the No-Tamper rule).
    """
    binary = _write_binary(tmp_path / "app", content=b"\x7fELF clean content")
    chain = AuditChain([BinaryDiscoveryStep(), SecretScannerStep()])
    report = chain.run(binary)

    sha_after = _sha256(binary)
    assert report.initial_sha256 == sha_after


def test_audit_failure_raised_if_binary_changes_mid_chain(tmp_path):
    """
    Contract: If a step somehow alters the binary, _assert_hash_unchanged
    must raise AuditFailure with context identifying both hashes.

    Simulate this by writing a custom step that corrupts the file.
    """
    from audit.chain import AuditStep, _assert_hash_unchanged

    class CorruptingStep(AuditStep):
        @property
        def name(self):
            return "corrupting_step"

        def execute(self, report):
            # Corrupt the binary mid-chain
            report.binary_path.write_bytes(b"CORRUPTED")
            _assert_hash_unchanged(report)

    binary = _write_binary(tmp_path / "app")
    chain = AuditChain([BinaryDiscoveryStep(), CorruptingStep()])

    with pytest.raises(AuditFailure) as exc_info:
        chain.run(binary)

    assert "integrity violation" in str(exc_info.value).lower()
    ctx = exc_info.value.context
    assert "initial_sha256" in ctx
    assert "current_sha256" in ctx
    assert ctx["initial_sha256"] != ctx["current_sha256"]


def test_report_passed_is_true_when_all_steps_succeed(tmp_path):
    """
    Contract: report.passed must be True only when every step completes
    without raising. A partial chain must not mark the report as passed.
    """
    binary = _write_binary(tmp_path / "app")
    chain = AuditChain([BinaryDiscoveryStep(), SecretScannerStep(), BuildToolLeakStep()])
    report = chain.run(binary)

    assert report.passed is True


def test_report_accumulates_findings_from_all_steps(tmp_path):
    """
    Contract: The final AuditReport.findings dict must contain a key for
    each step that ran. The chain is a data pipeline, not just pass/fail.
    """
    binary = _write_binary(tmp_path / "app")
    chain = AuditChain([BinaryDiscoveryStep(), SecretScannerStep(), BuildToolLeakStep()])
    report = chain.run(binary)

    assert "binary_discovery" in report.findings
    assert "secret_scanner" in report.findings
    assert "build_tool_leak" in report.findings


def test_sbom_path_is_recorded_in_report_after_sbom_step(tmp_path):
    """
    Contract: After SBOMGenerationStep, report.sbom_path must point to a
    file. Subsequent steps (like signing) depend on this reference.
    """
    binary = _write_binary(tmp_path / "app")
    sbom_step = SBOMGenerationStep()
    sbom_step._run_tool = MagicMock(
        return_value=_completed(returncode=0, stdout='{"sbom": "data"}')
    )

    chain = AuditChain([BinaryDiscoveryStep(), sbom_step])
    report = chain.run(binary)

    assert report.sbom_path is not None
    assert report.sbom_path.exists()
    assert report.findings["sbom_generation"]["sbom_path"] == str(report.sbom_path)


def test_registered_temp_dirs_are_cleaned_up_on_success(tmp_path):
    """
    Contract: Temp directories registered via chain.register_temp() must
    be purged after a successful run() — no zombie audit artifacts.
    """
    tmp_dir = tmp_path / "audit_scratch"
    tmp_dir.mkdir()
    (tmp_dir / "layer.tar").write_bytes(b"fake layer")

    binary = _write_binary(tmp_path / "app")
    chain = AuditChain([BinaryDiscoveryStep()])
    chain.register_temp(tmp_dir)

    chain.run(binary)

    assert not tmp_dir.exists()


def test_registered_temp_dirs_are_cleaned_up_on_failure(tmp_path):
    """
    Contract: Temp directories must also be purged when the chain halts on
    a failure — the 'finally' block must run even during an AuditFailure.
    """
    tmp_dir = tmp_path / "audit_scratch_fail"
    tmp_dir.mkdir()

    # A binary with a secret will cause SecretScannerStep to fail
    binary = _write_binary(tmp_path / "app", content=b"SECRET=hunter2 AWS API_KEY=AKIAIOSFODNN7EXAMPLE")
    chain = AuditChain([BinaryDiscoveryStep(), SecretScannerStep()])
    chain.register_temp(tmp_dir)

    with pytest.raises(SecretLeakDetected):
        chain.run(binary)

    assert not tmp_dir.exists()


def test_cleanup_is_idempotent_on_missing_temp(tmp_path):
    """
    Contract: If a registered temp dir was already cleaned up (or never
    created), _cleanup_temps() must not raise — it must be a safe no-op.
    """
    binary = _write_binary(tmp_path / "app")
    chain = AuditChain([BinaryDiscoveryStep()])
    chain.register_temp(tmp_path / "ghost_dir")  # does not exist

    # Must not raise
    chain.run(binary)



def test_failing_step_prevents_downstream_execution(tmp_path):
    """
    Contract: If Step 2 fails, Step 3 must never execute. This verifies
    that the chain is a strict linear gate, not a parallel scanner.
    """
    execution_log: list[str] = []

    from audit.chain import AuditStep

    class AlwaysFailStep(AuditStep):
        @property
        def name(self):
            return "always_fail"

        def execute(self, report):
            raise AuditFailure("Deliberate failure")

    class ShouldNeverRunStep(AuditStep):
        @property
        def name(self):
            return "should_never_run"

        def execute(self, report):
            execution_log.append(self.name)

    binary = _write_binary(tmp_path / "app")
    chain = AuditChain([BinaryDiscoveryStep(), AlwaysFailStep(), ShouldNeverRunStep()])

    with pytest.raises(AuditFailure):
        chain.run(binary)

    assert "should_never_run" not in execution_log


def test_report_passed_is_false_when_chain_breaks(tmp_path):
    """
    Contract: If the chain halts mid-way, report.passed must NOT be True.
    The AuditReport is created at the start of run() — we verify this via
    a custom step that reads the report state before raising.
    """
    from audit.chain import AuditStep

    captured_report: list[AuditReport] = []

    class CapturingFailStep(AuditStep):
        @property
        def name(self):
            return "capturing_fail"

        def execute(self, report):
            captured_report.append(report)
            raise AuditFailure("Captured and failed")

    binary = _write_binary(tmp_path / "app")
    chain = AuditChain([BinaryDiscoveryStep(), CapturingFailStep()])

    with pytest.raises(AuditFailure):
        chain.run(binary)

    assert captured_report[0].passed is False


def test_secret_scanner_raises_on_aws_key_pattern(tmp_path):
    """
    Contract: SecretScannerStep must raise SecretLeakDetected when the
    binary contains an AWS Access Key ID (AKIA...) pattern.
    """
    binary = _write_binary(tmp_path / "app", content=b"config: AKIAIOSFODNN7EXAMPLE123")
    chain = AuditChain([BinaryDiscoveryStep(), SecretScannerStep()])

    with pytest.raises(SecretLeakDetected) as exc_info:
        chain.run(binary)

    assert "secret" in str(exc_info.value).lower()
    assert len(exc_info.value.context["hits"]) > 0


def test_secret_scanner_raises_on_pem_header(tmp_path):
    """
    Contract: SecretScannerStep must detect PEM private key headers,
    which indicate a private key was baked into the production image.
    """
    binary = _write_binary(
        tmp_path / "app",
        content=b"-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA...",
    )
    chain = AuditChain([BinaryDiscoveryStep(), SecretScannerStep()])

    with pytest.raises(SecretLeakDetected):
        chain.run(binary)


def test_secret_scanner_passes_on_clean_binary(tmp_path):
    """
    Contract: SecretScannerStep must not raise when the binary contains
    no secret patterns. A clean capsule should pass this step.
    """
    binary = _write_binary(tmp_path / "app", content=b"\x7fELF clean production binary")
    chain = AuditChain([BinaryDiscoveryStep(), SecretScannerStep()])
    report = chain.run(binary)

    assert report.findings["secret_scanner"]["hits"] == []


def test_secret_scanner_records_pattern_count_in_findings(tmp_path):
    """
    Contract: findings['secret_scanner']['patterns_checked'] must reflect
    the number of patterns actually evaluated — telemetry for the report.
    """
    binary = _write_binary(tmp_path / "app")
    chain = AuditChain([BinaryDiscoveryStep(), SecretScannerStep()])
    report = chain.run(binary)

    assert report.findings["secret_scanner"]["patterns_checked"] > 0


def test_build_tool_leak_raises_on_gcc_present(tmp_path):
    """
    Contract: BuildToolLeakStep must raise BuildToolLeakDetected when a
    'gcc' binary is found in the artifact directory — a Pillar C violation.
    """
    binary = _write_binary(tmp_path / "app")
    (tmp_path / "gcc").write_bytes(b"fake gcc binary")  # leaked build tool

    chain = AuditChain([BinaryDiscoveryStep(), BuildToolLeakStep()])

    with pytest.raises(BuildToolLeakDetected) as exc_info:
        chain.run(binary)

    assert "gcc" in exc_info.value.context["leaked_tools"]


@pytest.mark.parametrize("tool_name", ["gcc", "mvn", "npm"])
def test_build_tool_leak_raises_for_each_pillar_c_tool(tmp_path, tool_name):
    """
    Contract: BuildToolLeakStep must detect each of the tools listed in
    Pillar C (gcc, mvn, npm) as individual violations.
    """
    binary = _write_binary(tmp_path / "app")
    (tmp_path / tool_name).write_bytes(b"fake binary")

    chain = AuditChain([BinaryDiscoveryStep(), BuildToolLeakStep()])

    with pytest.raises(BuildToolLeakDetected) as exc_info:
        chain.run(binary)

    assert tool_name in exc_info.value.context["leaked_tools"]


def test_build_tool_leak_passes_on_clean_directory(tmp_path):
    """
    Contract: BuildToolLeakStep must not raise when no build tools are
    present in the artifact directory.
    """
    binary = _write_binary(tmp_path / "app")
    chain = AuditChain([BinaryDiscoveryStep(), BuildToolLeakStep()])
    report = chain.run(binary)

    assert report.findings["build_tool_leak"]["leaked_tools"] == []


def test_build_tool_leak_is_build_tool_leak_detected_not_audit_failure(tmp_path):
    """
    Contract: The raised exception must be BuildToolLeakDetected specifically,
    not the generic AuditFailure — so the Supervisor can distinguish the cause.
    """
    binary = _write_binary(tmp_path / "app")
    (tmp_path / "npm").write_bytes(b"fake npm")

    chain = AuditChain([BinaryDiscoveryStep(), BuildToolLeakStep()])

    with pytest.raises(BuildToolLeakDetected):
        chain.run(binary)


def test_sbom_step_raises_audit_failure_when_syft_fails(tmp_path):
    """
    Contract: If Syft exits non-zero, SBOMGenerationStep must raise
    AuditFailure — the SBOM is required for production certification.
    """
    binary = _write_binary(tmp_path / "app")
    sbom_step = SBOMGenerationStep()
    sbom_step._run_tool = MagicMock(
        return_value=_completed(returncode=1, stderr="syft: command not found")
    )

    chain = AuditChain([BinaryDiscoveryStep(), sbom_step])

    with pytest.raises(AuditFailure) as exc_info:
        chain.run(binary)

    assert exc_info.value.context["exit_code"] == 1


def test_sbom_step_writes_sbom_content_to_file(tmp_path):
    """
    Contract: SBOMGenerationStep must write Syft's stdout to the SBOM file.
    The report.sbom_path file must contain the raw SBOM data.
    """
    sbom_content = '{"artifacts": [], "source": "app"}'
    binary = _write_binary(tmp_path / "app")
    sbom_step = SBOMGenerationStep()
    sbom_step._run_tool = MagicMock(
        return_value=_completed(returncode=0, stdout=sbom_content)
    )

    chain = AuditChain([BinaryDiscoveryStep(), sbom_step])
    report = chain.run(binary)

    assert report.sbom_path.read_text() == sbom_content
