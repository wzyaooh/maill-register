"""Non-interactive progress and output helpers for background workers."""
import sys

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

if sys.platform == 'win32':
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleCP(65001)
        kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    try:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

console = Console(force_terminal=True)

THEME = {
    "primary": "bright_cyan",
    "secondary": "bright_magenta",
    "success": "bright_green",
    "error": "bright_red",
    "warning": "bright_yellow",
}


def get_progress_context():
    return Progress(
        SpinnerColumn(spinner_name="dots", style=THEME["secondary"]),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(complete_style=THEME["success"], finished_style=THEME["success"]),
        TaskProgressColumn(),
        console=console,
        expand=True,
    )


def print_success(msg):
    console.print(f"[{THEME['success']}]> {msg}[/]")


def print_error(msg):
    console.print(f"[{THEME['error']}]> {msg}[/]")


def print_warning(msg):
    console.print(f"[{THEME['warning']}]> {msg}[/]")


def show_session_summary(total, successes, failures, duration_s):
    console.print(f"\n[{THEME['primary']}]{'='*60}[/]")
    console.print(f"[bold]SESSION SUMMARY[/]")
    console.print(f"[{THEME['primary']}]{'='*60}[/]")
    console.print(f"  Total Attempts: {total}")
    console.print(f"  [{THEME['success']}]Successes: {successes}[/]")
    console.print(f"  [{THEME['error']}]Failures: {failures}[/]")
    rate = (successes / total * 100) if total > 0 else 0
    console.print(f"  Success Rate: {rate:.1f}%")
    console.print(f"  Duration: {duration_s:.0f}s ({duration_s/60:.1f}m)")
    console.print(f"[{THEME['primary']}]{'='*60}[/]\n")
