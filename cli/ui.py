from rich.console import Console
from rich.text import Text

def print_schematic_banner():
    """Display the Hamilton-Ops schematic ASCII banner.
    
    This banner uses high-contrast cyan for the 'blocks' and dim white for
    the 'wires', creating a technical, flight-ready aesthetic.
    """
    console = Console()
    
    # We use different colors for the 'Blocks' and the 'Wires' (lines)
    # Style: Blocks = bold cyan, Wires = dim white or grey
    raw_art = [
        "       │██   ██  █████ │███   │███ ██ ██│     ████████│ ██████│ ███│   ██│",
        "       │██   ██│██   ██│████  ████ ██ ██│        ██│── ██    ██│████│  ██│",
        "       │███████│███████│██ ████│██ ██ ██│        ██│   ██    ██│██ ██│ ██│",
        "       │██   ██│██  │██│██  ██ │██ ██ ██│        ██│   ██    ██│██  ██│██│",
        "       │██   ██│██  │██│██     │██ ██ ███████│   ██│    ██████│ ██   ████│"
    ]

    # Display the title first to give context to the ASCII art
    title = "[bold white]Welcome to Hamilton-Ops[/]\n[bold blue]HAMILTON[/]\n[dim]Flight-ready Command Interface[/]\n"
    console.print(title)

    styled_banner = Text()
    for line in raw_art:
        for char in line:
            if char in "│─":
                styled_banner.append(char, style="dim white")
            elif char == "█":
                styled_banner.append(char, style="bold cyan")
            else:
                styled_banner.append(char)
        styled_banner.append("\n")

    console.print(styled_banner)
