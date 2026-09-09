"""Run one trusted operation without blocking a web request."""
import asyncio
import logging
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path

from web.tasks import TaskStore, now


def execute(action, params, report):
    from config.settings import Config

    if action == "validate":
        from core.config_validator import validate_config
        warnings, errors = validate_config()
        return {"warnings": warnings, "errors": errors}

    if action in ("create", "resume"):
        from core.creation_flow import run_creation_flow
        from core.session_resume import session_manager
        resume_state = None
        if action == "resume":
            resume_state = session_manager.load_state()
            if not resume_state or not session_manager.get_remaining(resume_state):
                raise ValueError("No unfinished session to resume")
            cfg = resume_state["batch_config"]
            params = {**cfg, "parallel": False}
            if cfg.get("engine"):
                Config.ENGINE_MODE = cfg["engine"]
        elif session_manager.has_saved_session():
            raise ValueError("An unfinished session exists; resume or clear it before creating a new batch")
        if params.get("use_sms_api") and not any((
            Config.FIVESIM_API_KEY, Config.SMS_ACTIVATE_API_KEY,
            Config.ONLINESIM_API_KEY, Config.GETSMS_API_KEY,
        )):
            raise ValueError("Premium mode requires at least one SMS API key")
        if params.get("parallel"):
            from core.batch_runner import run_batch
            completed = 0

            def on_result(result):
                nonlocal completed
                completed += 1
                report(completed, params["num_accounts"], "Account completed")

            result = run_batch(
                params["num_accounts"], max_threads=params["max_threads"],
                warmup_minutes=params["warmup_minutes"], flow_mode=params["flow_mode"],
                use_sms_api=params["use_sms_api"], on_result=on_result,
            )
            if result["total"] == 0:
                raise ValueError("Parallel creation requires YOUR_PASSWORD or config/password.txt")
        else:
            result = run_creation_flow(
                params["num_accounts"], warmup_minutes=params["warmup_minutes"],
                flow_mode=params["flow_mode"], use_sms_api=params["use_sms_api"],
                on_progress=lambda done, total, stats: report(done, total, str(stats)),
                resume_state=resume_state,
            )
        from core.retry_engine import retry_engine
        result["retry_stats"] = retry_engine.get_stats()
        return result

    if action in ("health", "warm"):
        from core.account_manager import account_manager
        accounts = account_manager.get_all()
        ids = set(params["account_ids"])
        if ids:
            if ids - {account["id"] for account in accounts}:
                raise ValueError("One or more selected accounts no longer exist")
            accounts = [account for account in accounts if account["id"] in ids]
        if not accounts:
            raise ValueError("No accounts available")
        results = []
        for index, account in enumerate(accounts):
            if action == "health":
                from core.health_checker import AccountHealthChecker
                result = AccountHealthChecker.check_single(account["email"], account["password"])
                if result["status"] not in ("error", "network_error", "unknown"):
                    if not account_manager.db.update_account_status(
                        account["email"], result["status"], result["message"]
                    ):
                        raise RuntimeError("Unable to persist account health status")
            else:
                from core.account_warmer import warm_account_playwright, warm_account_selenium
                if params["engine"] == "playwright":
                    ok = asyncio.run(warm_account_playwright(
                        account["email"], account["password"], params["duration_minutes"]))
                else:
                    ok = warm_account_selenium(
                        account["email"], account["password"], params["duration_minutes"])
                result = {"email": account["email"], "success": ok}
            results.append(result)
            print(f"{action}: {account['email']}: {result}", flush=True)
            report(index + 1, len(accounts), account["email"])
        if action == "health":
            return {"results": results, "summary": AccountHealthChecker.get_summary(results)}
        return {"results": results, "successes": sum(r["success"] for r in results),
                "failures": sum(not r["success"] for r in results)}

    if action == "proxy_test":
        from core.proxy_manager import proxy_manager
        from core.trust_builder import network_trust_check
        network = network_trust_check()
        health = proxy_manager.check_all_health()
        return {"network": network, "health": health, "pools": proxy_manager.get_stats()}

    if action == "proxy_fetch":
        from core.proxy_fetcher import fetch_and_test, save_proxies_to_file
        working = fetch_and_test(max_proxies=20, test_count=50)
        if not working:
            raise RuntimeError("No working proxies found")
        return {"found": len(working), "saved": save_proxies_to_file(working, Config.PROXY_FILE)}

    if action == "telegram_test":
        from core.telegram_notifier import notifier
        ok, message = notifier.test_connection()
        if not ok:
            raise RuntimeError(message)
        if not notifier.send("Gmail Creator Pro: Web test notification"):
            raise RuntimeError("Connected to bot, but sending a message to the chat failed")
        return {"message": message}

    if action == "sms_balance":
        from services.sms_manager import check_balance
        result = asyncio.run(check_balance())
        if not result:
            raise ValueError("Balance checks require a configured 5sim or SMS-Activate key")
        if all(value is None for value in result.values()):
            raise RuntimeError("Unable to retrieve balances; check API keys and connection")
        return result

    if action == "migrate":
        from core.database import DatabaseManager
        return {"migrated": DatabaseManager().run_migration()}

    if action == "voice":
        from services.voice import run_server
        run_server()
        return {"message": "Voice server stopped"}
    raise ValueError("Unsupported action: " + action)


def main():
    task_id = sys.argv[1]
    store = TaskStore(Path(os.environ["WEB_TASK_DIRECTORY"]))
    task = store.get(task_id)
    if task is None:
        raise ValueError("Task does not exist")

    def cancelled(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, cancelled)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)], force=True)

    parent_pid = int(os.environ.get("WEB_PARENT_PID", os.getppid()))
    finished = threading.Event()

    def watch_parent():
        while not finished.wait(1):
            if os.getppid() != parent_pid:
                store.update(task_id, status="stopping")
                if os.name == "nt":
                    os.kill(os.getpid(), signal.SIGTERM)
                else:
                    os.killpg(os.getpid(), signal.SIGTERM)
                    time.sleep(10)
                    os.killpg(os.getpid(), signal.SIGKILL)
                return

    # A lost parent must not let this cleanup thread disappear with the main
    # worker before resistant browser descendants receive the final signal.
    watchdog = threading.Thread(target=watch_parent)
    watchdog.start()

    def report(completed, total, message):
        store.update(task_id, progress={"completed": completed, "total": total, "message": message})

    try:
        result = execute(task["action"], task["params"], report)
        status = "stopping" if store.get(task_id)["status"] == "stopping" else "completed"
        if status == "completed" and store.get(task_id)["progress"]["completed"] == 0:
            report(1, 1, "Completed; inspect the result for operation errors")
        store.update(task_id, status=status, result=result, finished_at=now())
        print("Task " + status, flush=True)
    except KeyboardInterrupt:
        if store.get(task_id)["status"] != "stopping":
            store.update(task_id, status="cancelled", finished_at=now())
        print("Task cancelled. Completed serial accounts remain resumable.", flush=True)
    except Exception as exc:
        # This is the process boundary: exceptions must become visible failed tasks.
        traceback.print_exc()
        store.update(task_id, status="failed", error=str(exc), finished_at=now())
        sys.exit(1)
    finally:
        finished.set()


if __name__ == "__main__":
    main()
