import time
from pyfiglet import Figlet
from rich.console import Console
from rich.panel import Panel
from rich.align import Align
from rich.text import Text

def type_text(text: str, style: str = "bold white", delay: float = 0.02):
    console = Console()
    for char in text:
        console.print(char, style=style, end="")
        time.sleep(delay)
    console.print()

def print_welcome_panel():
    console = Console()
    
    fig = Figlet(font="ansi_shadow")
    art = fig.renderText("HAMILTON")

    title = Text()
    title.append("Welcome to Hamilton-Ops\n", style="bold white")
    title.append("Flight-ready Command Interface", style="dim")

    welcome = Align.center(
        Panel.fit(
            f"[bold cyan]{art}[/]\n{title}",
            border_style="blue",
            padding=(1, 4),
            title="[bold]hamilton-ops v0.1[/]",
            subtitle="[dim]type 'help' or ask naturally[/]"
        )
    )

    console.print(welcome)
    console.print("[dim]Session memory: on • cwd: ./ops[/]\n")
    
    # Example prompts to guide the user
    console.print("[bold cyan]Try:[/bold cyan] [dim]\"check staging health\" • \"rollback last deploy\" • \"/help\"[/]\n")
