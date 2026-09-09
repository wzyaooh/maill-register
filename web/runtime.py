"""Initialize isolated writable roots without copying live account data."""
import argparse
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODES = ("dev", "prod")


def prepare_environment(mode, project_root=None):
    if mode not in MODES:
        raise ValueError("Environment must be dev or prod")
    project_root = Path(project_root or ROOT).resolve()
    runtime = project_root / "runtime" / mode
    if runtime.resolve() != runtime or (project_root / "runtime").is_symlink():
        raise ValueError("Environment directories must not be symbolic links")
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    for folder in ("config", "data", "data/web"):
        target = runtime / folder
        if target.resolve() != target:
            raise ValueError("Environment data directories must not be symbolic links")
        target.mkdir(parents=True, exist_ok=True, mode=0o700)

    def seed(relative, content):
        path = runtime / relative
        if path.is_symlink():
            raise ValueError("Environment seed files must not be symbolic links")
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(content)
        except FileExistsError:
            if not path.is_file():
                raise ValueError("Expected an environment file: " + str(path)) from None

    template = project_root / "config/environments" / (mode + ".env.example")
    seed(".env", template.read_text(encoding="utf-8"))
    os.chmod(runtime / ".env", 0o600)
    seed("config/proxies.txt", "# Add proxies for this environment only.\n")
    for relative in ("config/user_agents.txt", "data/names.txt"):
        source = project_root / relative
        seed(relative, source.read_text(encoding="utf-8"))
    return runtime


def main():
    parser = argparse.ArgumentParser(description="Prepare isolated environment files")
    parser.add_argument("environment", choices=MODES)
    args = parser.parse_args()
    runtime = prepare_environment(args.environment)
    print(f"Configuration: {runtime / '.env'}")
    print("Set a unique WEB_ADMIN_PASSWORD of at least 16 characters before starting.")


if __name__ == "__main__":
    main()
