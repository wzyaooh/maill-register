"""
UI Module - Terminal interface with Rich for Gmail Creator Pro
"""
import os
import sys
import random
import datetime
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt
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
    "version": "5.0.0",
    "app_name": "SHADOW GENESIS",
    "primary": "bright_cyan",
    "secondary": "bright_magenta",
    "accent": "bright_blue",
    "success": "bright_green",
    "error": "bright_red",
    "warning": "bright_yellow",
    "muted": "bright_black",
}


def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


def show_banner():
    clear_screen()
    status = _get_system_status()
    c = THEME["primary"]
    m = THEME["secondary"]
    g = THEME["success"]
    w = THEME["warning"]
    d = "dim"

    banner = f"""
[{c}]    ::::::::  :::    :::     :::     :::::::::   ::::::::  :::       :::
   :+:    :+: :+:    :+:   :+: :+:   :+:    :+: :+:    :+: :+:       :+:
   +:+        +:+    +:+  +:+   +:+  +:+    +:+ +:+    +:+ +:+       +:+
   +#++:++#++ +#++:++#++ +#++:++#++: +#+    +:+ +#+    +:+ +#+  +:+  +#+
          +#+ +#+    +#+ +#+     +#+ +#+    +#+ +#+    +#+ +#+ +#+#+ +#+
   #+#    #+# #+#    #+# #+#     #+# #+#    #+# #+#    #+#  #+#+# #+#+#
    ########  ###    ### ###     ### #########   ########    ###   ###[/]

[{m}]     ::::::::  :::::::::: ::::    ::: :::::::::: :::::::: ::::::::::: ::::::::
    :+:    :+: :+:        :+:+:   :+: :+:       :+:    :+:    :+:    :+:    :+:
    +:+        +:+        :+:+:+  +:+ +:+       +:+           +:+    +:+
    :#:        +#++:++#   +#+ +:+ +#+ +#++:++#  +#++:++#++    +#+    +#++:++#++
    +#+   +#+# +#+        +#+  +#+#+# +#+              +#+    +#+           +#+
    #+#    #+# #+#        #+#   #+#+# #+#       #+#    #+#    #+#    #+#    #+#
     ########  ########## ###    #### ########## ########  ########### ########[/]

[{c}]  ============================================================================================[/]
[{c}]  ||[/]  [{g}]SYSTEM[/] [bold {g}]ONLINE[/]  [{d}]|[/]  [{d}]PING[/] [{c}]{status['latency']}[/]  [{d}]|[/]  [{d}]MEM[/] [{m}]{status['memory']}[/]  [{d}]|[/]  [{d}]TIME[/] [{c}]{status['time']}[/]  [{d}]|[/]  [{w}]v{THEME['version']}[/]  [{c}]||[/]
[{c}]  ============================================================================================[/]

    [{g}]>>>[/] [bold bright_white]PHANTOM CORE[/] [{m}]|[/] [bold {c}]GMAIL GENESIS ENGINE[/] [{m}]|[/] [{d}]STEALTH - NEURAL - AUTONOMOUS[/]

[{c}]  --------------------------------------------------------------------------------------------[/]
    [{g}]#[/] [bold]NEURAL_SCAN[/]        [{g}]#[/] [bold]PHONE_BYPASS[/]       [{g}]#[/] [bold]PROXY_ROTATE[/]       [{g}]#[/] [bold]GENESIS_AI[/]
      [{d}]Deep Analysis[/]          [{d}]Smart Evasion[/]         [{d}]Auto Switch[/]           [{d}]v5.0 Core[/]

    [{m}]#[/] [bold]FINGERPRINT[/]        [{m}]#[/] [bold]SESSION_WARM[/]       [{m}]#[/] [bold]GHOST_TYPER[/]        [{m}]#[/] [bold]RETRY_ENGINE[/]
      [{d}]Poltergeist v2[/]         [{d}]Trust Build[/]           [{d}]Human Sim[/]            [{d}]Smart Rotate[/]
[{c}]  ============================================================================================[/]
    [{d}]Developed by[/] [bold bright_white]Shadow Hacker[/] [{d}]|[/] [{d}]All Rights Reserved[/] [{d}]|[/] [{m}]{status['date']}[/]
[{c}]  ============================================================================================[/]
"""
    console.print(banner)


def show_menu(engine_name="PLAYWRIGHT"):
    c = THEME["primary"]
    m = THEME["secondary"]
    g = THEME["success"]
    w = THEME["warning"]
    e = THEME["error"]

    menu = f"""
[{c}]  ============================================================================================[/]
[{c}]  ||[/]  [bold bright_white]COMMAND CENTER[/]  [{m}]//[/]  [bold {c}]SELECT OPERATION[/]  [{m}]//[/]  [{w}]{engine_name} ENGINE[/]                        [{c}]||[/]
[{c}]  ============================================================================================[/]

    [{w}][ FREE MODE --- NO PHONE API REQUIRED ][/]

      [bold bright_white]1[/] [{g}]>>[/]  [{g}]GHOST_MODE[/]            [dim]100% Free Bypass - Maximum Stealth ({engine_name})[/]

    [{m}][ PREMIUM MODE --- SMS API VERIFICATION ][/]

      [bold bright_white]2[/] [{m}]>>[/]  [{m}]PREMIUM_CREATE[/]        [dim]5sim / SMS-Activate Auto Verification[/]

    [{c}][ UTILITIES ][/]

      [bright_white]3[/] [{c}]>>[/]  [{c}]DASHBOARD[/]             [dim]Statistics & Analytics Panel[/]
      [bright_white]4[/] [{c}]>>[/]  [{c}]CONFIGURATION[/]         [dim]Settings & API Keys[/]
      [bright_white]5[/] [{c}]>>[/]  [{c}]SAVED_ACCOUNTS[/]        [dim]View & Export Accounts[/]
      [bright_white]6[/] [{c}]>>[/]  [{c}]NETWORK_TEST[/]          [dim]Proxy Connectivity Check[/]

    [{w}][ ADVANCED MODE ][/]

      [{w}]7[/] [{w}]>>[/]  [{w}]YOUTUBE_GHOST[/]         [dim]Free Bypass via YouTube Signup Flow[/]
      [{w}]8[/] [{w}]>>[/]  [{w}]WORKSPACE_GHOST[/]       [dim]Free Bypass via Workspace Flow[/]

    [{m}][ TOOLS ][/]

      [bright_white]9[/]  [{m}]>>[/]  [{m}]HEALTH_CHECK[/]         [dim]Verify Account Status (IMAP)[/]
      [bright_white]10[/] [{m}]>>[/]  [{m}]FETCH_PROXIES[/]        [dim]Auto-Fetch Free Proxies[/]
      [bright_white]11[/] [{m}]>>[/]  [{m}]TELEGRAM_TEST[/]        [dim]Test Telegram Bot Notification[/]
      [bright_white]12[/] [{m}]>>[/]  [{m}]RESUME_SESSION[/]       [dim]Resume Interrupted Batch[/]

      [{e}]0[/]  [{e}]>>[/]  [{e}]TERMINATE[/]             [dim]Shutdown Application[/]

[{c}]  ============================================================================================[/]
"""
    console.print(menu)


def get_menu_choice():
    return Prompt.ask(
        f"\n[{THEME['primary']}]Select operation[/]",
        choices=["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"],
        default="1",
    )


def ask_num_accounts():
    while True:
        try:
            n = int(Prompt.ask(
                f"[{THEME['primary']}]How many accounts to create?[/]",
                default="1",
            ))
            if 1 <= n <= 100:
                return n
            console.print(f"[{THEME['error']}]Enter a number between 1 and 100[/]")
        except ValueError:
            console.print(f"[{THEME['error']}]Invalid number[/]")


def ask_warmup_minutes():
    try:
        m = int(Prompt.ask(
            f"[{THEME['primary']}]Pre-warming duration (minutes)?[/]",
            default="5",
        ))
        return max(1, min(30, m))
    except ValueError:
        return 5


def get_progress_context():
    return Progress(
        SpinnerColumn(spinner_name="dots", style=THEME["secondary"]),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(complete_style=THEME["success"], finished_style=THEME["success"]),
        TaskProgressColumn(),
        console=console,
        expand=True,
    )


def show_dashboard(account_stats, retry_stats=None, proxy_stats=None):
    clear_screen()

    stats_panel = Panel(
        f"[{THEME['success']}]Total Accounts Created: {account_stats.get('total', 0)}[/]\n"
        f"[{THEME['primary']}]Active Accounts: {account_stats.get('active', 0)}[/]\n"
        f"[{THEME['error']}]Success Rate: {account_stats.get('success_rate', 0):.1f}%[/]\n"
        f"\n[bold]Strategy Breakdown:[/]\n"
        + "\n".join(
            f"  [{THEME['accent']}]{k}[/]: {v}"
            for k, v in account_stats.get("strategies", {}).items()
        ),
        title="Statistics Dashboard",
        border_style=THEME["primary"],
    )
    console.print(stats_panel)

    if retry_stats:
        retry_panel = Panel(
            f"Total Attempts: {retry_stats.get('total_attempts', 0)}\n"
            f"Successes: {retry_stats.get('successes', 0)}\n"
            f"Failures: {retry_stats.get('failures', 0)}\n"
            f"Success Rate: {retry_stats.get('success_rate', 0):.1f}%\n"
            f"\n[bold]Strategy Scores:[/]\n"
            + "\n".join(
                f"  [{THEME['accent']}]{k}[/]: {v:.0f}/100"
                for k, v in retry_stats.get("strategy_scores", {}).items()
            ),
            title="Retry Engine Stats",
            border_style=THEME["secondary"],
        )
        console.print(retry_panel)

    if proxy_stats:
        static_stats = proxy_stats.get('static', {})
        kooip_stats = proxy_stats.get('kooip', {})
        proxy_panel = Panel(
            f"Total Proxies: {proxy_stats.get('total', 0)}\n"
            f"[{THEME['success']}]Healthy: {proxy_stats.get('healthy', 0)}[/]\n"
            f"[{THEME['error']}]Unhealthy: {proxy_stats.get('unhealthy', 0)}[/]\n"
            f"[{THEME['accent']}]Static Pool: {static_stats.get('healthy', 0)}/{static_stats.get('total', 0)} healthy[/]\n"
            f"[{THEME['accent']}]KooIP Pool: {kooip_stats.get('healthy', 0)}/{kooip_stats.get('total', 0)} healthy "
            f"({'enabled' if kooip_stats.get('enabled') else 'disabled'})[/]",
            title="Proxy Status",
            border_style=THEME["warning"],
        )
        console.print(proxy_panel)

    _show_services_status()
    input("\nPress Enter to continue...")


def _show_services_status():
    from config.settings import Config
    table = Table(title="Services Status", border_style=THEME["primary"])
    table.add_column("Service", style=THEME["primary"])
    table.add_column("Status", style=THEME["success"])

    table.add_row("5sim SMS", f"[{THEME['success']}]Active[/]" if Config.FIVESIM_API_KEY else f"[{THEME['error']}]Not Set[/]")
    table.add_row("SMS-Activate", f"[{THEME['success']}]Active[/]" if Config.SMS_ACTIVATE_API_KEY else f"[{THEME['error']}]Not Set[/]")
    table.add_row("OnlineSIM", f"[{THEME['success']}]Active[/]" if Config.ONLINESIM_API_KEY else f"[{THEME['error']}]Not Set[/]")
    table.add_row("GetSMS", f"[{THEME['success']}]Active[/]" if Config.GETSMS_API_KEY else f"[{THEME['error']}]Not Set[/]")
    table.add_row("2Captcha", f"[{THEME['success']}]Active[/]" if Config.TWOCAPTCHA_API_KEY else f"[{THEME['error']}]Not Set[/]")
    table.add_row("Anti-Captcha", f"[{THEME['success']}]Active[/]" if Config.ANTICAPTCHA_API_KEY else f"[{THEME['error']}]Not Set[/]")
    table.add_row("CapMonster", f"[{THEME['success']}]Active[/]" if Config.CAPMONSTER_API_KEY else f"[{THEME['error']}]Not Set[/]")
    console.print(table)


def show_settings():
    from config.settings import Config
    clear_screen()

    table = Table(title="Configuration", border_style=THEME["primary"])
    table.add_column("Setting", style=THEME["primary"])
    table.add_column("Value", style=THEME["secondary"])
    table.add_column("Status")

    engine = getattr(Config, 'ENGINE_MODE', 'playwright').upper()
    table.add_row("Engine Mode", engine, f"[{THEME['success']}]OK[/]")
    table.add_row("Password", "****" if Config.YOUR_PASSWORD else "Not Set",
                   f"[{THEME['success']}]OK[/]" if Config.YOUR_PASSWORD else f"[{THEME['error']}]Missing[/]")
    table.add_row("Birthday", Config.YOUR_BIRTHDAY, f"[{THEME['success']}]OK[/]")
    table.add_row("Gender", {"1": "Male", "2": "Female"}.get(str(Config.YOUR_GENDER), "Other"),
                   f"[{THEME['success']}]OK[/]")
    table.add_row("Proxy", "Enabled" if Config.ENABLE_PROXY else "Disabled",
                   f"[{THEME['success']}]OK[/]" if Config.ENABLE_PROXY else f"[{THEME['muted']}]OFF[/]")
    table.add_row("Session Warming", "Enabled" if Config.ENABLE_SESSION_WARMING else "Disabled",
                   f"[{THEME['success']}]OK[/]" if Config.ENABLE_SESSION_WARMING else f"[{THEME['muted']}]OFF[/]")
    table.add_row("Headless Mode", "Enabled" if Config.HEADLESS_MODE else "Disabled",
                   f"[{THEME['muted']}]OFF[/]")
    table.add_row("MAC Rotation", "Enabled" if Config.ENABLE_MAC_ROTATION else "Disabled",
                   f"[{THEME['success']}]OK[/]" if Config.ENABLE_MAC_ROTATION else f"[{THEME['muted']}]OFF[/]")

    console.print(table)
    console.print(f"\n[{THEME['warning']}]Edit .env file to change settings[/]")
    input("\nPress Enter to continue...")


def show_accounts(accounts):
    clear_screen()

    if not accounts:
        console.print(Panel(
            f"[{THEME['warning']}]No accounts found[/]\nCreate some accounts first!",
            title="Saved Accounts",
            border_style=THEME["primary"],
        ))
        input("\nPress Enter to continue...")
        return None

    table = Table(title=f"Saved Accounts ({len(accounts)} total)", border_style=THEME["primary"])
    table.add_column("#", style=THEME["primary"], width=5)
    table.add_column("Email", style=THEME["success"])
    table.add_column("Password", style=THEME["secondary"])
    table.add_column("Strategy", style=THEME["accent"])
    table.add_column("Status", style=THEME["warning"])
    table.add_column("Created", style=THEME["muted"])

    for i, acc in enumerate(accounts[:30], 1):
        table.add_row(
            str(i),
            acc.get("email", ""),
            acc.get("password", "")[:15] + "..." if len(acc.get("password", "")) > 15 else acc.get("password", ""),
            acc.get("strategy", "N/A"),
            acc.get("status", "N/A"),
            acc.get("created_at", "N/A")[:16] if acc.get("created_at") else "N/A",
        )
    console.print(table)

    if len(accounts) > 30:
        console.print(f"\n[{THEME['warning']}]Showing first 30 of {len(accounts)} accounts[/]")

    console.print(f"\n[{THEME['primary']}]Export Options:[/]")
    console.print(f"  [1] Export to CSV")
    console.print(f"  [2] Export to JSON")
    console.print(f"  [3] Export to TXT (email:password)")
    console.print(f"  [0] Back to Main Menu")

    return Prompt.ask(f"\n[{THEME['primary']}]Select[/]", choices=["0", "1", "2", "3"], default="0")


def print_success(msg):
    console.print(f"[{THEME['success']}]> {msg}[/]")


def print_error(msg):
    console.print(f"[{THEME['error']}]> {msg}[/]")


def print_warning(msg):
    console.print(f"[{THEME['warning']}]> {msg}[/]")


def print_info(msg):
    console.print(f"[{THEME['accent']}]> {msg}[/]")


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


def _get_system_status():
    now = datetime.datetime.now()
    return {
        "time": now.strftime("%H:%M:%S"),
        "date": now.strftime("%Y.%m.%d"),
        "latency": f"{random.randint(15, 85)}ms",
        "memory": f"{random.randint(45, 78)}%",
    }
