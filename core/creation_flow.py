"""Non-interactive account creation orchestration for background tasks."""
import sys
import random
import time
import logging

from config.settings import Config
from core.progress import (
    THEME, get_progress_context, show_session_summary,
    print_success, print_error, print_warning,
)
from core.database import DatabaseManager
from core.proxy_manager import proxy_manager

if sys.platform == 'win32':
    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport

        def _silence_proactor():
            def safe_del(self, _orig=getattr(_ProactorBasePipeTransport, '__del__', None)):
                try:
                    if getattr(self, '_sock', None) is None:
                        return
                    if _orig:
                        _orig(self)
                except Exception:
                    pass
            _ProactorBasePipeTransport.__del__ = safe_del

        _silence_proactor()
    except ImportError:
        pass


def _generate_username():
    from core.selenium_runner import generate_name
    name = generate_name()
    parts = name.split()
    first = parts[0].lower() if parts else "user"
    last = parts[-1].lower() if len(parts) > 1 else "gmail"
    return f"{first}{last}{random.randint(1000, 9999)}", parts


def run_creation_flow(num_accounts, warmup_minutes=10, flow_mode='standard', use_sms_api=False,
                      on_progress=None, resume_state=None):
    """
    Unified account creation flow. Routes to Playwright, Appium, or Selenium
    based on Config.ENGINE_MODE.
    """
    engine = (resume_state or {}).get("batch_config", {}).get(
        "engine", getattr(Config, 'ENGINE_MODE', 'playwright')).lower()
    password = Config.YOUR_PASSWORD

    if not password:
        try:
            with open("config/password.txt", "r", encoding="utf-8") as f:
                password = f.read().strip()
        except FileNotFoundError:
            pass

    use_generated_passwords = not password

    previous = (resume_state or {}).get("results", {})
    successes = previous.get("successes", 0)
    failures = previous.get("failures", 0)
    completed_indices = list((resume_state or {}).get("completed_indices", []))
    if resume_state:
        num_accounts = resume_state["batch_config"]["num_accounts"]
    pending_indices = [i for i in range(num_accounts) if i not in completed_indices]
    start_time = time.time()

    with get_progress_context() as progress:
        overall = progress.add_task(f"[{THEME['success']}]Overall Progress", total=num_accounts)
        progress.update(overall, completed=len(completed_indices))
        current = progress.add_task(f"[{THEME['primary']}]Current Account", total=100)

        for i in pending_indices:
            username, name_parts = _generate_username()
            first_name = name_parts[0] if name_parts else "User"
            last_name = name_parts[-1] if len(name_parts) > 1 else "User"

            if use_generated_passwords:
                from core.selenium_runner import generate_password
                password = generate_password()

            progress.update(current, completed=5,
                            description=f"[{THEME['primary']}]Account {i+1}/{num_accounts}...[/]")

            proxy = proxy_manager.get_best() or proxy_manager.get_next()

            success = False
            max_retries = 2 if proxy_manager.count > 1 else 1

            for attempt in range(max_retries):
                if attempt > 0:
                    # Retry with a different proxy
                    old_proxy = proxy
                    if old_proxy:
                        proxy_manager.mark_failure(old_proxy, fatal=True)
                    proxy = proxy_manager.get_next()
                    if proxy == old_proxy:
                        proxy = proxy_manager.get_random()
                    username, name_parts = _generate_username()
                    first_name = name_parts[0] if name_parts else "User"
                    last_name = name_parts[-1] if len(name_parts) > 1 else "User"
                    if use_generated_passwords:
                        password = generate_password()
                    print_warning(f"Retrying with {'new proxy' if proxy else 'no proxy'} (attempt {attempt+1})...")
                    progress.update(current, completed=5,
                                    description=f"[{THEME['warning']}]Retry {attempt+1} — Account {i+1}...[/]")
                    time.sleep(random.randint(5, 15))

                try:
                    if engine == 'playwright':
                        from core.runners import run_playwright_flow
                        success = run_playwright_flow(
                            i, num_accounts, username, first_name, last_name,
                            password, progress, current, proxy,
                            use_sms_api=use_sms_api, flow_mode=flow_mode,
                        )
                    elif engine == 'appium':
                        from core.runners import run_appium_flow
                        month, day, year = Config.YOUR_BIRTHDAY.split() if Config.YOUR_BIRTHDAY else ("1", "1", "1990")
                        success = run_appium_flow(
                            i, num_accounts, username, first_name, last_name,
                            password, month, day, year, str(Config.YOUR_GENDER),
                            progress, current,
                        )
                    else:
                        from core.selenium_runner import run_selenium_flow
                        success = run_selenium_flow(
                            i, num_accounts, username, password,
                            warmup_minutes=warmup_minutes,
                            stealth_mode=(not use_sms_api),
                            mode=flow_mode, proxy=proxy,
                        )
                except Exception as e:
                    print_error(f"Account {i+1} error: {e}")

                if success:
                    break

            if success:
                successes += 1
                print_success(f"Account {i+1}/{num_accounts}: {username}@gmail.com CREATED")
                if proxy:
                    proxy_manager.mark_success(proxy)
            else:
                failures += 1
                print_error(f"Account {i+1}/{num_accounts}: {username}@gmail.com FAILED")
                if proxy:
                    proxy_manager.mark_failure(proxy)

            progress.update(overall, advance=1)
            progress.update(current, completed=0)

            # Save session state for resume capability
            try:
                from core.session_resume import session_manager
                completed_indices.append(i)
                session_manager.save_state(
                    batch_config={"num_accounts": num_accounts, "flow_mode": flow_mode,
                                  "use_sms_api": use_sms_api, "warmup_minutes": warmup_minutes,
                                  "engine": engine},
                    completed_indices=completed_indices,
                    results={"successes": successes, "failures": failures},
                )
            except OSError as exc:
                logging.error("Unable to save resumable session: %s", exc)
                raise

            if on_progress:
                on_progress(len(completed_indices), num_accounts,
                            {"successes": successes, "failures": failures})

            if i < num_accounts - 1:
                delay = getattr(Config, 'DELAY_BETWEEN_ACCOUNTS', 30)
                time.sleep(delay)

    duration = time.time() - start_time
    show_session_summary(num_accounts, successes, failures, duration)

    db = DatabaseManager()
    db.save_session_stats(
        total_attempts=num_accounts, successes=successes, failures=failures,
        strategies_used={flow_mode: num_accounts}, errors={},
        duration_seconds=duration,
    )

    # Telegram batch notification
    try:
        from core.telegram_notifier import notifier
        notifier.notify_batch_complete(num_accounts, successes, failures, duration)
    except Exception:
        pass

    # Clear saved session on completion
    try:
        from core.session_resume import session_manager
        session_manager.clear_state()
    except Exception:
        pass

    return {"total": num_accounts, "successes": successes, "failures": failures,
            "duration": duration}
