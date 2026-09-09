"""Web launcher shared by the primary script and python -m web."""
import argparse
import atexit
import os
from pathlib import Path


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
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (use 0.0.0.0 for remote access)")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    try:
        from waitress import serve
        from web.app import create_app
    except ImportError as exc:
        parser.exit(1, f"Missing Web dependency: {exc}. Install requirements.txt in your virtual environment.\n")
    try:
        server_lock = lock_server(root)
        app = create_app()
    except ValueError as exc:
        parser.exit(1, str(exc) + "\n")
    manager = app.extensions["web_tasks"]
    atexit.register(manager.close)
    print(f"Web interface listening on http://{args.host}:{args.port}", flush=True)
    print("Remote access must use HTTPS (TLS reverse proxy) or a trusted encrypted tunnel.", flush=True)
    try:
        serve(app, host=args.host, port=args.port, threads=8)
    finally:
        manager.close()
        server_lock.close()


if __name__ == "__main__":
    main()
