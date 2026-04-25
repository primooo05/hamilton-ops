import typer
from pathlib import Path
from typing import Optional
from rich.console import Console

from cli.doctor import doctor_cmd
from cli.ship import ship_cmd
from cli.audit import audit_cmd

app = typer.Typer(
    help="Hamilton-Ops CLI — Validate, Build, and Audit with Priority Awareness",
    add_completion=False,
)
console = Console()

def check_doctor_passed() -> bool:
    """Enforce the Doctor-First rule."""
    state_file = Path(".hamilton_doctor")
    if not state_file.exists():
        return False
    
    try:
        with open(state_file, "r") as f:
            lines = f.readlines()
            for line in lines:
                if line.strip() == "status=pass":
                    return True
    except Exception:
        pass
    return False

@app.command()
def doctor(
    fix: bool = typer.Option(False, "--fix", help="Auto-remediate what is fixable")
):
    """Run pre-flight diagnostics to validate hardware and environment."""
    doctor_cmd(fix=fix)

@app.command()
def ship(
    stage: Path = typer.Option(Path("."), "--stage", help="Path to project stage"),
    image: Optional[str] = typer.Option(None, "--image", help="Custom image tag"),
):
    """Execute the P1/P2/P3 build and validation streams."""
    if not check_doctor_passed():
        console.print("[red]Error: `hamilton doctor` must pass before `hamilton ship` is callable.[/red]")
        console.print("[yellow]Skip this and you’ll debug environment issues for hours that doctor would have exposed in seconds.[/yellow]")
        raise typer.Exit(code=1)
    
    ship_cmd(stage=stage, image_tag=image)

@app.command()
def audit(
    artifact: Path = typer.Option(..., "--artifact", help="Path to the built binary artifact"),
):
    """Run post-flight forensic analysis and secret scanning."""
    if not check_doctor_passed():
        console.print("[red]Error: `hamilton doctor` must pass before `hamilton audit` is callable.[/red]")
        raise typer.Exit(code=1)
    
    audit_cmd(artifact_path=artifact)

if __name__ == "__main__":
    app()
