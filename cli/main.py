import typer
from pathlib import Path
from typing import List, Optional
from rich.console import Console
from rich.prompt import Confirm

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
    project: Optional[str] = typer.Option(
        None,
        "--project",
        help="Project name for forensics. Defaults to the current directory name.",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Escalate QualityViolation to Hamilton Kill (P1 abort).",
    ),
    linter_cmd: Optional[List[str]] = typer.Option(
        None,
        "--linter-cmd",
        help=(
            "Custom linter command. Pass each token separately: "
            "--linter-cmd eslint --linter-cmd '--ext' --linter-cmd '.js'"
        ),
    ),
    cache_ref: Optional[str] = typer.Option(
        None,
        "--cache-ref",
        help="BuildKit registry cache reference, e.g. ghcr.io/org/app:buildcache",
    ),
    programmatic: bool = typer.Option(
        False,
        "--programmatic",
        "-p",
        help="Run without interactive confirmation prompt.",
    ),
):
    """Execute the P1/P2/P3 build and validation streams."""
    from cli.ui import print_welcome_panel, type_text
    print_welcome_panel()

    if not check_doctor_passed():
        console.print("[red]Error: `hamilton doctor` must pass before `hamilton ship` is callable.[/red]")
        console.print("[yellow]Skip this and you'll debug environment issues for hours that doctor would have exposed in seconds.[/yellow]")
        raise typer.Exit(code=1)

    if not programmatic:
        type_text(f"Mission Plan: Execute P1/P2/P3 streams for project [bold]{project or stage.resolve().name}[/bold].", delay=0.01)
        if not Confirm.ask("[bold yellow]Ready for ignition?[/bold yellow]", default=False):
            console.print("[dim]Aborted by user.[/dim]")
            raise typer.Exit(code=0)

    ship_cmd(
        stage=stage,
        image_tag=image,
        project=project,
        strict=strict,
        linter_cmd=linter_cmd or None,
        cache_ref=cache_ref,
    )

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

