"""Web launcher shared by the primary script and python -m web."""
import argparse
import atexit
import logging
import os
from pathlib import Path

from web.runtime import MODES, prepare_environment


ROOT = Path(__file__).resolve().parents[1]


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
        parser.exit(1, f"Unable to prepare environment: {exc}\n")
    os.chdir(ROOT)
    try:
        from waitress import serve
        from web.app import create_app
    except ImportError as exc:
        parser.exit(1, f"Missing Web dependency: {exc}. Install requirements.txt in your virtual environment.\n")
    try:
        server_lock = lock_server(root)
    except ValueError as exc:
        parser.exit(1, str(exc) + "\n")
    manager = None
    handlers = []
    logger = logging.getLogger()
    old_level = logger.level
    try:
        try:
            app = create_app(root=root, code_root=ROOT, deployment=args.env) if args.env else create_app()
        except ValueError as exc:
            parser.exit(1, f"{exc}\nConfiguration file: {root / '.env'}\n")
        manager = app.extensions["web_tasks"]
        atexit.register(manager.close)
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
        if manager is not None:
            manager.close()
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()
        logger.setLevel(old_level)
        server_lock.close()


if __name__ == "__main__":
    main()
