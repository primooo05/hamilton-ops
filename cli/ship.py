import asyncio
from pathlib import Path
from typing import Optional
from core.supervisor import HamiltonSupervisor, SupervisorConfig
from core.priorities import Priority
from rich.console import Console

console = Console()

def get_doctor_state() -> dict:
    state = {"strategy": "full", "ram_gb": 16.0}
    state_file = Path(".hamilton_doctor")
    if state_file.exists():
        try:
            with open(state_file, "r") as f:
                for line in f:
                    if "=" in line:
                        k, v = line.split("=", 1)
                        state[k.strip()] = v.strip()
        except Exception:
            pass
    return state

def ship_cmd(stage: Path, image_tag: Optional[str] = None):
    state = get_doctor_state()
    strategy = state.get("strategy", "full")
    ram_gb = float(state.get("ram_gb", 16.0))
    
    console.print(f"SUPERVISOR: Detected execution strategy: [bold cyan]{strategy}[/bold cyan]")
    
    # Adaptive resource capping
    docker_memory = 4 # Default 4GB
    if ram_gb < 8:
        docker_memory = 3 # Cap at 3GB as per user requirement
        console.print(f"SUPERVISOR: [yellow]Low RAM detected ({ram_gb:.1f}GB). Capping Docker at {docker_memory}GB.[/yellow]")

    # Mock config for demonstration
    config = SupervisorConfig(
        project_name="hamilton-ops",
        source_path=stage,
        image_tag=image_tag or "hamilton/app:latest",
        binary_path="dist/app.bin",
        k6_script="tests/p1_validation.js",
        strict=False,
        concurrency_strategy=strategy,
        docker_memory_gb=docker_memory
    )
    
    # Register drivers before creating supervisor
    from drivers.registry import DriverRegistry
    from drivers.construction import ConstructionDriver
    from drivers.k6_driver import K6Driver
    from drivers.linter_driver import LinterDriver
    from core.priorities import Priority
    
    registry = DriverRegistry()
    registry.register("k6", Priority.P1_VALIDATION)(K6Driver)
    registry.register("linter", Priority.P2_QUALITY)(LinterDriver)
    registry.register("docker", Priority.P3_CONSTRUCTION)(ConstructionDriver)
    
    supervisor = HamiltonSupervisor(config, registry)
    
    try:
        report = asyncio.run(supervisor.ship())
        console.print("\n[bold green]Mission Completed Successfully[/bold green]")
        console.print(f"Streams: P1={report.stream_results.get('P1:Validation').outcome}, "
                      f"P2={report.stream_results.get('P2:Quality').outcome}, "
                      f"P3={report.stream_results.get('P3:Construction').outcome}")
    except Exception as e:
        console.print(f"\n[bold red]Mission Failed:[/bold red] {e}")
        raise SystemExit(1)
