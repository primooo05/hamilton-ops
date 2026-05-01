"""
Hamilton-Ops Initialization Command
=====================================

Scaffolds a `.hamilton.toml` and a starter `tests/p1_validation.js` for a
project. Uses the ``DiscoveryEngine`` to detect the project's structure and
ecosystem before generating a tailored configuration.

Behavior by discovery result count
------------------------------------
    0 units found   → Falls back to generic template at workspace root.
    1 unit found    → Auto-configures for that unit; no prompt needed.
    >1 units found  → Interactive selection via Rich prompt (unless
                      ``programmatic=True``, which picks the shallowest unit).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table

from cli.discovery import DiscoveryEngine, ProjectUnit
from cli.templates import K6_SCRIPT_TEMPLATE, TEMPLATES

logger = logging.getLogger("hamilton.cli.init")
console = Console()


def _build_discovery_table(units: list[ProjectUnit], workspace: Path) -> Table:
    """Render a Rich table of discovered project units for user selection."""
    table = Table(
        title=f"[bold cyan]Discovered {len(units)} buildable component(s)[/bold cyan]",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("#", style="dim", width=4)
    table.add_column("Component", style="bold")
    table.add_column("Ecosystem", style="cyan")
    table.add_column("Dockerfile", style="green")
    table.add_column("Path")

    for i, unit in enumerate(units, start=1):
        dockerfile_display = (
            str(unit.dockerfile.relative_to(workspace))
            if unit.dockerfile else "[dim]Not found[/dim]"
        )
        path_display = str(unit.root.relative_to(workspace)) or "."
        table.add_row(
            str(i),
            unit.name,
            unit.ecosystem,
            dockerfile_display,
            path_display,
        )
    return table


def _select_unit_interactive(units: list[ProjectUnit], workspace: Path) -> ProjectUnit:
    """
    Display the discovery table and prompt the user to pick a component.
    Returns the selected ``ProjectUnit``.
    """
    table = _build_discovery_table(units, workspace)
    console.print(table)
    console.print()

    while True:
        raw = console.input(
            f"[bold yellow]Select a component to initialize [1-{len(units)}]:[/bold yellow] "
        ).strip()
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(units):
                return units[idx]
        console.print(f"[red]Please enter a number between 1 and {len(units)}.[/red]")


def init_cmd(path: Path, force: bool = False, programmatic: bool = False) -> None:
    """
    Scaffold a new Hamilton-Ops configuration for the project at ``path``.

    Runs the ``DiscoveryEngine`` to index the project structure in parallel,
    then generates a tailored ``.hamilton.toml`` and starter k6 script.

    Args:
        path:         Project root to initialize.
        force:        If True, overwrite existing files.
        programmatic: If True, skip interactive prompts and pick automatically.
    """
    workspace = path.resolve()
    if not workspace.is_dir():
        console.print(f"[bold red]Error:[/bold red] '{path}' is not a directory.")
        raise SystemExit(1)

    # -----------------------------------------------------------------------
    # Phase 1 — Parallel Discovery
    # -----------------------------------------------------------------------
    console.print(
        f"\nINIT: [bold]Indexing[/bold] project structure at [cyan]{workspace}[/cyan] ...\n"
    )

    engine = DiscoveryEngine(workspace=workspace)
    units = engine.scan()

    # -----------------------------------------------------------------------
    # Phase 2 — Component Selection
    # -----------------------------------------------------------------------
    selected: Optional[ProjectUnit] = None

    if len(units) == 0:
        # No known ecosystem found. Use generic template rooted at workspace.
        console.print(
            "[yellow]No known ecosystem fingerprints found. "
            "Using generic template at workspace root.[/yellow]"
        )
        ecosystem = "generic"
        component_name = workspace.name
        dockerfile_path: Optional[str] = None

    elif len(units) == 1:
        selected = units[0]
        console.print(
            f"INIT: Detected [bold cyan]{selected.ecosystem}[/bold cyan] "
            f"project: [bold]{selected.name}[/bold]"
        )
        ecosystem = selected.ecosystem
        component_name = selected.name
        dockerfile_path = (
            selected.dockerfile.relative_to(workspace).as_posix()
            if selected.dockerfile else None
        )

    else:
        # Multiple components — let the user choose.
        console.print(
            "[bold]INIT:[/bold] Multiple buildable components detected in this workspace.\n"
        )

        if programmatic:
            # Non-interactive: pick the shallowest (first) unit.
            selected = units[0]
            console.print(
                f"[dim]Programmatic mode: auto-selecting '{selected.name}'.[/dim]"
            )
        else:
            selected = _select_unit_interactive(units, workspace)

        console.print(
            f"\nINIT: Configuring for [bold cyan]{selected.name}[/bold cyan] "
            f"([{selected.ecosystem}])\n"
        )
        ecosystem = selected.ecosystem
        component_name = selected.name
        dockerfile_path = (
            selected.dockerfile.relative_to(workspace).as_posix()
            if selected.dockerfile else None
        )

    # -----------------------------------------------------------------------
    # Phase 3 — Generate .hamilton.toml
    # -----------------------------------------------------------------------
    config_path = workspace / ".hamilton.toml"

    if config_path.exists() and not force:
        console.print(
            f"[yellow]Skipping {config_path.name} (already exists). "
            "Use [bold]--force[/bold] to overwrite.[/yellow]"
        )
    else:
        template = TEMPLATES.get(ecosystem, TEMPLATES["generic"])
        config_content = template.format(name=component_name)

        # Patch in the discovered Dockerfile path if it differs from the default.
        if dockerfile_path and dockerfile_path != "Dockerfile":
            config_content = config_content.replace(
                'dockerfile = "Dockerfile"',
                f'dockerfile = "{dockerfile_path}"',
            )

        config_path.write_text(config_content)
        console.print(
            f"[green]✓[/green] Created [bold]{config_path.name}[/bold] "
            f"using [bold]{ecosystem}[/bold] template."
        )

    # -----------------------------------------------------------------------
    # Phase 4 — Scaffold tests/p1_validation.js
    # -----------------------------------------------------------------------
    tests_dir = workspace / "tests"
    k6_script_path = tests_dir / "p1_validation.js"

    if k6_script_path.exists() and not force:
        console.print(
            f"[yellow]Skipping tests/p1_validation.js (already exists).[/yellow]"
        )
    else:
        tests_dir.mkdir(exist_ok=True)
        k6_script_path.write_text(K6_SCRIPT_TEMPLATE)
        console.print("[green]✓[/green] Created [bold]tests/p1_validation.js[/bold] (k6 baseline script).")

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    console.print()
    console.print(
        "[bold green]Initialization Complete.[/bold green] "
        "Run [bold cyan]hamilton ship[/bold cyan] to validate and build your project."
    )

    if len(units) > 1:
        console.print(
            f"\n[dim]Tip: Run [bold]hamilton init --force[/bold] again to reconfigure "
            f"for a different component. {len(units)} component(s) are available.[/dim]"
        )
