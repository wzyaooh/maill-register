"""Web launcher shared by the primary script and python -m web."""
import argparse
import atexit
import logging
import os
import subprocess
import sys
from pathlib import Path

from web.runtime import MODES, prepare_environment


ROOT = Path(__file__).resolve().parents[1]


class CompensationSchedulerSupervisor:
    """Own the optional periodic compensation subprocess lifecycle."""

    def __init__(self, root, configuration):
        self.root = Path(root).resolve()
        self.configuration = configuration
        self.process = None

    def start(self):
        if self.process is not None and self.process.poll() is None:
            return True
        settings = self.configuration.compensation_scheduler_settings()
        if not settings["enabled"]:
            return False
        environment = dict(self.configuration.environment)
        environment.update(self.configuration.values())
        environment.update({
            "GMAIL_CONFIG_FROM_ENV": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "LEDGER_DB_PATH": str((self.root / "data" / "database.db").resolve()),
        })
        for key in ("WEB_ADMIN_PASSWORD", "WEB_SECRET_KEY"):
            environment.pop(key, None)
        code_root = Path(self.configuration.code_root).resolve()
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
            str(code_root), environment.get("PYTHONPATH", ""),
        )))
        self.process = subprocess.Popen(
            [sys.executable, "-m", "web.compensation_scheduler", "--root", str(self.root)],
            cwd=code_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=(os.name != "nt"),
        )
        return True

    def stop(self, timeout=10, kill_timeout=2):
        process = self.process
        if process is None:
            return True
        if process.poll() is not None:
            self.process = None
            return True
        try:
            process.terminate()
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=kill_timeout)
            except subprocess.TimeoutExpired:
                return False
        except OSError:
            if process.poll() is None:
                return False
        self.process = None
        return True


def lock_server(root):
    directory = root / "data/web"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stream = (directory / "server.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise ValueError("Another Web server is already using this project's data directory") from None
    return stream


def main():
    parser = argparse.ArgumentParser(description="Gmail Creator authenticated Web interface")
    parser.add_argument("--web", action="store_true", help="Compatibility flag; Web is now the default")
    parser.add_argument("--env", choices=MODES, help="Use an isolated dev or prod configuration and data directory")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (use 0.0.0.0 for remote access)")
    parser.add_argument("--port", type=int, help="HTTP port (default: dev 8081, prod/legacy 8080)")
    args = parser.parse_args()
    port = args.port if args.port is not None else (8081 if args.env == "dev" else 8080)
    if not 1 <= port <= 65535:
        parser.error("port must be between 1 and 65535")
    if args.env == "dev" and args.host not in ("127.0.0.1", "localhost", "::1"):
        parser.error("Development mode must bind to a loopback address")
    try:
        root = prepare_environment(args.env, ROOT) if args.env else ROOT
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Unable to prepare environment ({type(exc).__name__})\n")
    os.chdir(ROOT)
    try:
        from waitress import serve
        from web.app import create_app
    except ImportError as exc:
        parser.exit(
            1,
            "Missing Web dependency (%s). Install requirements.txt in your "
            "virtual environment.\n" % type(exc).__name__,
        )
    try:
        server_lock = lock_server(root)
    except ValueError as exc:
        parser.exit(1, "Unable to acquire Web server lock (%s)\n" % type(exc).__name__)
    manager = None
    scheduler = None
    handlers = []
    logger = logging.getLogger()
    old_level = logger.level
    try:
        try:
            app = create_app(root=root, code_root=ROOT, deployment=args.env) if args.env else create_app()
        except ValueError as exc:
            parser.exit(
                1,
                "Invalid Web configuration (%s)\nConfiguration file: %s\n"
                % (type(exc).__name__, root / ".env"),
            )
        manager = app.extensions["web_tasks"]
        atexit.register(manager.close)
        scheduler = CompensationSchedulerSupervisor(
            root, app.extensions["web_configuration"],
        )
        try:
            scheduler.start()
        except (OSError, ValueError) as exc:
            parser.exit(1, "Unable to start compensation scheduler (%s)\n" % type(exc).__name__)
        atexit.register(scheduler.stop)
        if args.env:
            if not logger.handlers:
                handlers.append(logging.StreamHandler())
            handlers.append(logging.FileHandler(root / "data/web/server.log", encoding="utf-8"))
            formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
            for handler in handlers:
                handler.setFormatter(formatter)
                logger.addHandler(handler)
            logger.setLevel(logging.DEBUG if args.env == "dev" else logging.INFO)
        print(f"Environment: {args.env or 'legacy'} | Data directory: {root / 'data'}", flush=True)
        print(f"Web interface listening on http://{args.host}:{port}", flush=True)
        print("Remote access must use HTTPS (TLS reverse proxy) or a trusted encrypted tunnel.", flush=True)
        if args.env == "dev":
            app.config["TEMPLATES_AUTO_RELOAD"] = True
            # Automatic process reload would interrupt supervised browser tasks.
            app.run(host=args.host, port=port, debug=True, use_reloader=False,
                    use_debugger=False, load_dotenv=False)
        else:
            serve(app, host=args.host, port=port, threads=8)
    finally:
        if scheduler is not None and not scheduler.stop():
            logging.getLogger(__name__).error(
                "Compensation scheduler process did not exit after termination"
            )
        if manager is not None:
            manager.close()
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()
        logger.setLevel(old_level)
        server_lock.close()


if __name__ == "__main__":
    main()
