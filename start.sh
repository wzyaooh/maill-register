#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    printf '%s\n' \
        "Usage: ./start.sh <dev|prod> [--setup | --host HOST --port PORT]" \
        "" \
        "  dev          Local development server at 127.0.0.1:8081" \
        "  prod         Waitress server at 127.0.0.1:8080" \
        "  --setup      Create the environment, install dependencies and Chromium" \
        "" \
        "Each mode uses venv/<mode>/ and runtime/<mode>/ independently." \
        "Set PYTHON to a Python executable when running --setup (Python 3.10+ recommended)." \
        "Set WEB_ADMIN_PASSWORD in runtime/<mode>/.env before starting." \
        "Servers run in the foreground. Use Ctrl+C to stop, or a production process manager."
}

case "${1:-}" in
    dev|prod) MODE="$1"; shift ;;
    -h|--help|"") usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac

VENV="$ROOT/venv/$MODE"
PYTHON_EXEC="$VENV/bin/python"
cd "$ROOT"
unset PYTHONPATH PYTHONHOME
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1
export PYTHONNOUSERSITE=1

if [[ "${1:-}" == "--setup" ]]; then
    if [[ "$#" -ne 1 ]]; then
        printf '%s\n' "--setup cannot be combined with server arguments." >&2
        exit 2
    fi
    if [[ ! -x "$PYTHON_EXEC" ]]; then
        BOOTSTRAP="${PYTHON:-python3}"
        "$BOOTSTRAP" -c 'import sys; sys.exit("Python 3.9 or newer is required; Python 3.10+ is recommended.") if sys.version_info < (3, 9) else None'
        "$BOOTSTRAP" -m venv "$VENV"
    fi
    "$PYTHON_EXEC" -m pip install -r "$ROOT/requirements.txt"
    "$PYTHON_EXEC" -m playwright install chromium
    "$PYTHON_EXEC" -m web.runtime "$MODE"
    printf '\nSetup complete. Configure %s/runtime/%s/.env, then run ./start.sh %s\n' "$ROOT" "$MODE" "$MODE"
    exit 0
fi

for argument in "$@"; do
    case "$argument" in
        --env|--env=*)
            printf '%s\n' "Choose dev or prod as the first argument; --env cannot override it." >&2
            exit 2
            ;;
    esac
done

if [[ ! -x "$PYTHON_EXEC" ]]; then
    printf 'Missing virtual environment. Run: ./start.sh %s --setup\n' "$MODE" >&2
    exit 1
fi

exec "$PYTHON_EXEC" "$ROOT/auto_gmail_creator.py" --env "$MODE" "$@"
