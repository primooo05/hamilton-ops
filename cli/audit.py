import asyncio
from pathlib import Path
from audit.chain import (
    AuditChain,
    BinaryDiscoveryStep,
    SecretScannerStep,
    BuildToolLeakStep,
    SBOMGenerationStep,
)
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
            # AuditChain usually raises AuditFailure, but we handle the case where it might return failed.
            console.print("[bold red]Audit Failed: Security vulnerabilities or tampering detected.[/bold red]")
            raise SystemExit(1)
            
    except Exception as e:
        console.print(f"[bold red]Audit Error:[/bold red] {e}")
        raise SystemExit(1)
