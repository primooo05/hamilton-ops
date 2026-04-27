import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import time
import psutil
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

class ExecutionStrategy(Enum):
    FULL = "full"
    REDUCED = "reduced"
    MINIMAL = "minimal"

@dataclass
class HardwareProfile:
    cores: int
    ram_gb: float
    is_ssd: bool
    cpu_model: str = "Unknown"

    @property
    def strategy(self) -> ExecutionStrategy:
        if self.cores >= 4 and self.ram_gb >= 7.5: # 7.5 to handle slightly below 8GB reports
            return ExecutionStrategy.FULL
        if self.cores >= 2:
            return ExecutionStrategy.REDUCED
        return ExecutionStrategy.MINIMAL

    @classmethod
    def detect(cls) -> "HardwareProfile":
        cores = os.cpu_count() or 1
        ram_gb = psutil.virtual_memory().total / (1024**3)
        
        cpu_model = "Unknown"
        if platform.system() == "Windows":
            try:
                cpu_model = subprocess.check_output(["wmic", "cpu", "get", "name"]).decode().split("\n")[1].strip()
            except Exception:
                pass
        
        # SSD Detection on Windows
        is_ssd = False
        if platform.system() == "Windows":
            try:
                cmd = ["powershell", "-Command", "Get-PhysicalDisk | Select-Object MediaType"]
                output = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode()
                is_ssd = "SSD" in output
            except Exception:
                pass
        
        return cls(cores=cores, ram_gb=ram_gb, is_ssd=is_ssd, cpu_model=cpu_model)

@dataclass
class DiagnosticResult:
    category: str
    name: str
    status: str  # "success", "warning", "error"
    message: str
    details: Optional[str] = None

class Doctor:
    def __init__(self):
        self.results: List[DiagnosticResult] = []

    def check_hardware(self) -> HardwareProfile:
        profile = HardwareProfile.detect()
        
        # CPU Check
        if profile.cores >= 4:
            self.results.append(DiagnosticResult("Hardware", "CPU", "success", f"{profile.cores} cores detected ({profile.cpu_model})"))
        else:
            self.results.append(DiagnosticResult("Hardware", "CPU", "success", f"{profile.cores} cores detected ({profile.cpu_model})"))
            self.results.append(DiagnosticResult("Hardware", "CPU", "warning", f"Below recommended 4 cores. Parallelism reduced. Estimated runtime: ~90s (vs ~40s)"))

        # RAM Check
        if profile.ram_gb >= 8:
            self.results.append(DiagnosticResult("Hardware", "RAM", "success", f"{profile.ram_gb:.1f} GB available"))
        else:
            self.results.append(DiagnosticResult("Hardware", "RAM", "success", f"{profile.ram_gb:.1f} GB available"))
            self.results.append(DiagnosticResult("Hardware", "RAM", "warning", f"Below recommended 8 GB. Docker memory capped at 3 GB (auto-adjusted)"))

        # Disk Check
        if profile.is_ssd:
            self.results.append(DiagnosticResult("Hardware", "Disk", "success", "SSD detected - I/O target achievable"))
        else:
            self.results.append(DiagnosticResult("Hardware", "Disk", "warning", "Non-SSD or unknown disk detected - I/O might be a bottleneck"))

        return profile

    def _check_tool(self, name: str, cmd: List[str], version_parse_fn=None) -> None:
        try:
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode().strip()
            version = version_parse_fn(output) if version_parse_fn else output.split("\n")[0]
            self.results.append(DiagnosticResult("Software", name, "success", version))
        except (subprocess.CalledProcessError, FileNotFoundError):
            self.results.append(DiagnosticResult("Software", name, "error", "NOT FOUND on PATH"))

    def check_software(self):
        self._check_tool("Python", ["python", "--version"])
        self._check_tool("Docker", ["docker", "--version"], lambda x: x.replace("Docker version ", "").split(",")[0])
        
        # BuildKit check
        try:
            # Simple check if buildx is available as a proxy for BuildKit preference
            subprocess.check_output(["docker", "buildx", "version"], stderr=subprocess.STDOUT)
            self.results.append(DiagnosticResult("Software", "BuildKit", "success", "enabled"))
        except Exception:
            self.results.append(DiagnosticResult("Software", "BuildKit", "warning", "not detected or older docker version"))

        self._check_tool("k6", ["k6", "version"])
        self._check_tool("syft", ["syft", "--version"])
        self._check_tool("flake8", ["flake8", "--version"])

    def check_registry(self):
        """Validate registry completeness using the same wiring as ship_cmd.

        Imports ``build_registry`` from ``cli.ship`` and calls
        ``verify_completeness()`` on the result. This guarantees that the
        doctor is testing the *actual* runtime registry — not a parallel one
        that could diverge from the real wiring over time.
        """
        from cli.ship import build_registry
        from core.supervisor import SupervisorConfig
        from core.exceptions import RegistryError

        # Construct a minimal config so build_registry can resolve all factories.
        # Paths and script names don't need to exist — we're only checking that
        # the registry has all three pillars wired at the correct priorities,
        # not that the tools can actually run (that's check_software()'s job).
        mock_config = SupervisorConfig(
            project_name="doctor-check",
            source_path=Path("."),
            image_tag="hamilton/check:latest",
            binary_path="dist/app.bin",
            k6_script="tests/p1_validation.js",
        )

        try:
            reg = build_registry(mock_config)
            reg.verify_completeness()

            self.results.append(DiagnosticResult("Registry", "P1 Validation", "success", "registered"))
            self.results.append(DiagnosticResult("Registry", "P2 Quality", "success", "registered"))
            self.results.append(DiagnosticResult("Registry", "P3 Construction", "success", "registered"))
        except RegistryError as e:
            self.results.append(DiagnosticResult("Registry", "Completeness", "error", str(e)))

    def run_diagnostics(self, fix: bool = False, persist: bool = False) -> Tuple[HardwareProfile, int, int]:
        if fix:
            self.fix_environment()
            
        # Clear results before running if we are in fix mode or re-running
        self.results = []
        
        profile = self.check_hardware()
        self.check_software()
        self.check_registry()
        
        errors = len([r for r in self.results if r.status == "error"])
        warnings = len([r for r in self.results if r.status == "warning"])
        
        if persist:
            # Persist state
            state_file = Path(".hamilton_doctor")
            status = "pass" if errors == 0 else "fail"
            with open(state_file, "w") as f:
                f.write(f"status={status}\n")
                f.write(f"strategy={profile.strategy.value}\n")
                f.write(f"ram_gb={profile.ram_gb}\n")
                f.write(f"last_run={time.time()}\n")
        
        return profile, errors, warnings

    def fix_environment(self):
        """Auto-remediate what we can."""
        console.print("[yellow]Doctor is attempting to fix environment issues...[/yellow]")
        
        # Install flake8
        try:
            subprocess.check_output(["flake8", "--version"], stderr=subprocess.STDOUT)
        except (subprocess.CalledProcessError, FileNotFoundError):
            console.print("[cyan]Installing flake8...[/cyan]")
            subprocess.run(["python", "-m", "pip", "install", "flake8"], check=False)

        # Install Syft
        try:
            subprocess.check_output(["syft", "--version"], stderr=subprocess.STDOUT)
        except (subprocess.CalledProcessError, FileNotFoundError):
            console.print("[cyan]Installing syft...[/cyan]")
            if platform.system() == "Windows":
                console.print("[yellow]Automatic syft installation on Windows is not supported. Please use 'scoop install syft' or download it manually.[/yellow]")
            else:
                sh_cmd = "curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /usr/local/bin"
                subprocess.run(["sh", "-c", sh_cmd], check=False)

        # Enable BuildKit (best effort)
        os.environ["DOCKER_BUILDKIT"] = "1"

    def report(self):
        console.print("\n[bold]Hamilton-Ops - Pre-Flight Diagnostic[/bold]")
        console.print("=" * 40 + "\n")

        categories = ["Hardware", "Software", "Registry"]
        for cat in categories:
            console.print(f"[bold]{cat}[/bold]")
            console.print("-" * 40)
            
            cat_results = [r for r in self.results if r.category == cat]
            for res in cat_results:
                icon = ""
                if res.status == "success":
                    icon = "[green](v)[/green]"
                elif res.status == "warning":
                    icon = "[yellow](!)[/yellow]"
                elif res.status == "error":
                    icon = "[red](x)[/red]"
                
                # Align columns
                name_text = f"{icon}  {res.name:<10}"
                console.print(f"{name_text} {res.message}")
                if res.status == "warning" and "WARNING" in res.message:
                    # The message might already contain the warning text, 
                    # but the user's design shows it indented on a new line.
                    pass 
            console.print("")

        errors = len([r for r in self.results if r.status == "error"])
        warnings = len([r for r in self.results if r.status == "warning"])
        
        console.print("=" * 40)
        result_color = "red" if errors > 0 else ("yellow" if warnings > 0 else "green")
        console.print(f"Result: [{result_color}]{errors} error(s), {warnings} warning(s)[/{result_color}]")
        
        if errors > 0:
            console.print("\n[red]Errors block `ship`.[/red]")
        if warnings > 0:
            console.print("[yellow]Warnings adjust execution but do not stop it.[/yellow]")
        console.print("")

def doctor_cmd(fix: bool = False):
    doc = Doctor()
    profile, errors, warnings = doc.run_diagnostics(fix=fix, persist=True)
    doc.report()
    
    if errors > 0:
        raise SystemExit(1)
