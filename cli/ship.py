import asyncio
import time
from pathlib import Path
from typing import List, Optional

from rich.console import Console

from core.config import compute_project_hash, load_hamilton_config
from core.supervisor import HamiltonSupervisor, SupervisorConfig
from core.priorities import Priority

console = Console()

# Freshness threshold: warn if doctor ran more than 1 hour ago.
_DOCTOR_FRESHNESS_SECONDS = 3600


def get_doctor_state() -> dict:
    """Read the persisted doctor state from .hamilton_doctor.

    Returns a dict with at minimum ``strategy``, ``ram_gb``, ``status``,
    and ``last_run`` keys. Missing keys fall back to safe defaults so
    the caller never has to guard against KeyError.
    """
    state: dict = {"strategy": "full", "ram_gb": 16.0, "status": "unknown", "last_run": "0"}
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


def check_doctor_freshness(state: dict) -> None:
    """Warn if the doctor state is older than _DOCTOR_FRESHNESS_SECONDS.

    The host environment can change between doctor runs (e.g., Docker
    restarted, disk remounted). Stale diagnostics produce false confidence.
    Emits a console warning; does NOT raise — the developer can proceed.
    """
    try:
        last_run = float(state.get("last_run", 0))
    except (ValueError, TypeError):
        last_run = 0.0

    age_seconds = time.time() - last_run
    if last_run == 0.0 or age_seconds > _DOCTOR_FRESHNESS_SECONDS:
        age_minutes = int(age_seconds // 60)
        console.print(
            f"[bold yellow]WARN:[/bold yellow] `hamilton doctor` was last run "
            f"[yellow]{age_minutes} minute(s) ago[/yellow]. "
            "Your environment may have changed. Run `hamilton doctor` to refresh."
        )


def build_registry(config: SupervisorConfig):
    """Build and return a fully-wired DriverRegistry for the given config.

    This is the single source of truth for driver wiring. Both ``ship_cmd``
    and ``doctor.check_registry`` call this function so that diagnostics
    always validate the *actual* runtime registry — not a parallel one.

    Factory design — two-phase invocation contract:
        Phase 1 (health check, called with stage_path=None):
            The supervisor calls ``factory(stage_path=None)`` during
            _pre_flight to obtain an instance whose check_health() can run
            before staging is ready. Drivers that don't use stage_path
            (e.g., K6Driver) ignore the None safely. Drivers that require
            it (e.g., LinterDriver) should handle None gracefully — the
            health check only validates the binary, not the target path.

        Phase 2 (execution, called with stage_path=<Path>):
            The supervisor calls ``factory(stage_path=real_path)`` again
            inside each stream task to get an execution instance with the
            actual staging directory.

    Args:
        config: SupervisorConfig with all resolved parameters.

    Returns:
        A DriverRegistry with k6, linter, and docker registered.
    """
    from drivers.registry import DriverRegistry
    from drivers.construction import ConstructionDriver
    from drivers.k6_driver import K6Driver
    from drivers.linter_driver import LinterDriver

    registry = DriverRegistry()

    # K6Driver does not use stage_path — it runs against a fixed script.
    # stage_path=None is ignored by K6Driver.__init__ (it takes script_path, not stage_path).
    registry.register("k6", Priority.P1_VALIDATION)(
        lambda stage_path=None: K6Driver(script_path=config.k6_script)
    )

    # LinterDriver requires stage_path at execution time, but check_health()
    # only validates the binary on PATH — so stage_path=None is safe for
    # the health-check instance. We pass "." as a safe fallback for None.
    registry.register("linter", Priority.P2_QUALITY)(
        lambda stage_path=None: LinterDriver(
            stage_path=stage_path if stage_path is not None else Path("."),
            tool_cmd=config.linter_cmd,
        )
    )

    # ConstructionDriver similarly uses stage_path only during run(), not
    # check_health(). Pass stage_path or a sentinel that check_health ignores.
    registry.register("docker", Priority.P3_CONSTRUCTION)(
        lambda stage_path=None: ConstructionDriver(
            stage_path=stage_path if stage_path is not None else Path("."),
            image_tag=config.image_tag,
            cache_ref=config.cache_ref,
            project_hash=config.project_hash,
            secrets=config.secrets,
            memory_gb=config.docker_memory_gb,
        )
    )

    return registry


def ship_cmd(
    stage: Path,
    image_tag: Optional[str] = None,
    project: Optional[str] = None,
    strict: bool = False,
    linter_cmd: Optional[List[str]] = None,
    cache_ref: Optional[str] = None,
):
    """Orchestrate the full P1/P2/P3 mission.

    Args:
        stage:      Path to the project root to build.
        image_tag:  Docker image tag override.
        project:    Logical project name for forensics. Defaults to the
                    resolved directory name of ``stage``.
        strict:     If True, QualityViolation escalates to Hamilton Kill.
        linter_cmd: Custom linter command list, e.g. ``["eslint", "--ext", ".js"]``.
                    Defaults to flake8 if not provided.
        cache_ref:  BuildKit registry cache reference for CI layer caching.
    """
    state = get_doctor_state()
    check_doctor_freshness(state)

    strategy = state.get("strategy", "full")
    ram_gb = float(state.get("ram_gb", 16.0))

    # Default project name to the directory name so forensic logs are
    # always meaningful even when --project is omitted.
    project_name = project or Path(stage).resolve().name

    toml_config = load_hamilton_config(stage)
    toml_project = toml_config.get("project", {})
    toml_construction = toml_config.get("construction", {})

    # Resolve final values: CLI arg wins, then TOML, then hardcoded default.
    resolved_image_tag  = image_tag  or toml_project.get("image_tag",  "hamilton/app:latest")
    resolved_cache_ref  = cache_ref  or toml_project.get("cache_ref",  None)
    resolved_k6_script  = toml_project.get("k6_script", "tests/p1_validation.js")
    resolved_linter_cmd = linter_cmd or None  # TOML linter override not modelled yet

    console.print(f"SUPERVISOR: Detected execution strategy: [bold cyan]{strategy}[/bold cyan]")
    console.print(f"SUPERVISOR: Project: [bold]{project_name}[/bold]")
    if strict:
        console.print("SUPERVISOR: [bold red]--strict mode enabled[/bold red] — QualityViolation will escalate to Hamilton Kill.")

    # Adaptive resource capping based on detected RAM.
    docker_memory = toml_construction.get("memory_gb", 4)
    if ram_gb < 8:
        docker_memory = min(docker_memory, 3)  # Cap at 3GB for low-RAM hosts
        console.print(
            f"SUPERVISOR: [yellow]Low RAM detected ({ram_gb:.1f}GB). "
            f"Capping Docker at {docker_memory}GB.[/yellow]"
        )

    # -------------------------------------------------------------------
    # Compute LOCKFILE_HASH for BuildKit cache scoping.
    # This fingerprints the project's dependency manifests so the BuildKit
    # layer cache is namespaced per unique dependency state, preventing
    # cross-project cache contamination on shared CI runners.
    # -------------------------------------------------------------------
    resolved_stage = Path(stage).resolve()
    project_hash = compute_project_hash(resolved_stage)
    if project_hash:
        console.print(
            f"SUPERVISOR: LOCKFILE_HASH=[cyan]{project_hash}[/cyan] — BuildKit cache scoped."
        )
    else:
        console.print(
            "SUPERVISOR: [yellow]No lockfile found — BuildKit cache will not be scoped.[/yellow]"
        )

    config = SupervisorConfig(
        project_name=project_name,
        source_path=stage,
        image_tag=resolved_image_tag,
        binary_path="dist/app.bin",
        k6_script=resolved_k6_script,
        strict=strict,
        concurrency_strategy=strategy,
        docker_memory_gb=docker_memory,
        linter_cmd=resolved_linter_cmd,
        cache_ref=resolved_cache_ref,
        project_hash=project_hash or None,  # pass None if empty — driver skips flag
    )

    registry = build_registry(config)
    supervisor = HamiltonSupervisor(config, registry)

    try:
        report = asyncio.run(supervisor.ship())
        console.print("\n[bold green]Mission Completed Successfully[/bold green]")
        console.print(
            f"Streams: P1={report.stream_results.get('P1:Validation').outcome}, "
            f"P2={report.stream_results.get('P2:Quality').outcome}, "
            f"P3={report.stream_results.get('P3:Construction').outcome}"
        )
    except Exception as e:
        console.print(f"\n[bold red]Mission Failed:[/bold red] {e}")
        raise SystemExit(1)
