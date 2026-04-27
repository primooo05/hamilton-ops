import asyncio
from pathlib import Path
from audit.chain import (
    AuditChain,
    BinaryDiscoveryStep,
    SecretScannerStep,
    BuildToolLeakStep,
    SBOMGenerationStep,
)
from core.exceptions import AuditFailure
from rich.console import Console

console = Console()

def audit_cmd(artifact_path: Path):
    """Run the post-flight audit chain manually via CLI."""
    console.print(f"AUDIT: Starting forensic analysis on [bold]{artifact_path}[/bold]")

    # Pillar C: Chain of Responsibility inspection
    chain = AuditChain([
        BinaryDiscoveryStep(),
        SecretScannerStep(),
        BuildToolLeakStep(),
        SBOMGenerationStep(),
    ])

    try:
        # AuditChain.run() is a synchronous method that drives the step sequence.
        report = chain.run(artifact_path)

        if report.passed:
            console.print("[bold green]Audit Passed: Artifact is verified and secure.[/bold green]")
            console.print(f"SHA256: [cyan]{report.initial_sha256}[/cyan]")
        else:
            # AuditChain usually raises AuditFailure, but handle the case where it returns failed.
            console.print("[bold red]Audit Failed: Security vulnerabilities or tampering detected.[/bold red]")
            raise SystemExit(1)

    except AuditFailure as e:
        # AuditFailure subclasses (SecretLeakDetected, BuildToolLeakDetected) carry
        # a structured context dict. Print it so the developer knows exactly what
        # was found — not just that something failed.
        console.print(f"[bold red]Audit Error:[/bold red] {e}")
        if e.context:
            console.print("[bold red]Details:[/bold red]")
            for key, value in e.context.items():
                console.print(f"  [yellow]{key}[/yellow]: {value}")
        raise SystemExit(1)

    except Exception as e:
        # Unexpected errors (e.g., I/O errors, missing binary) — no context to print.
        console.print(f"[bold red]Audit Error:[/bold red] {e}")
        raise SystemExit(1)

