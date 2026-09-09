"""Editable configuration, derived from the existing Config declarations."""
import ast
import os
import tempfile
import threading
from pathlib import Path

from dotenv import dotenv_values, set_key


CHOICES = {
    "ENGINE_MODE": ["playwright", "selenium", "appium"],
    "YOUR_GENDER": ["1", "2", "3"],
    "PROXY_TYPE": ["residential", "mobile", "datacenter"],
    "PROXY_POOL_PREFERENCE": ["auto", "static", "kooip"],
    "WARMING_INTENSITY": ["low", "medium", "high"],
    "LOG_LEVEL": ["DEBUG", "INFO", "WARNING", "ERROR"],
    "EXPORT_FORMAT": ["txt", "csv", "json"],
}
RESOURCE_KEYS = {
    "proxies": ("PROXY_FILE", "config/proxies.txt"),
    "names": ("NAMES_FILE", "data/names.txt"),
    "user_agents": ("USER_AGENTS_FILE", "config/user_agents.txt"),
}


def is_secret(key):
    return any(word in key for word in ("PASSWORD", "TOKEN", "API_KEY", "AUTH_NAME", "USER_ID"))


def group_for(key):
    for prefixes, group in (
        (("FIVESIM", "SMS_", "ONLINESIM", "GETSMS"), "SMS"),
        (("TWOCAPTCHA", "ANTICAPTCHA", "CAPMONSTER"), "CAPTCHA"),
        (("KOOIP",), "KooIP"),
        (("PROXY", "ENABLE_PROXY", "ROTATE_PROXY", "MOBILE_PROXY"), "Proxy"),
        (("TELEGRAM",), "Telegram"),
        (("VOICE",), "Voice"),
        (("YOUR_", "RECOVERY_", "FORCE_RECOVERY", "ENABLE_RECOVERY", "CHAIN_"), "Account"),
        (("ENGINE", "HEADLESS", "BROWSER"), "Browser"),
        (("LOG_", "ENABLE_LOGGING", "ACCOUNTS_", "EXPORT_", "NAMES_", "USER_AGENTS_", "USE_ARABIC"), "Files"),
    ):
        if key.startswith(prefixes):
            return group
    return "Behavior"


def configuration_schema(source):
    tree = ast.parse(source)
    config = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Config")
    fields = {}
    for node in config.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.targets[0], ast.Name):
            continue
        env = next((
            child for child in ast.walk(node.value)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
            and isinstance(child.func.value, ast.Name) and child.func.value.id == "os"
            and child.func.attr == "getenv"
        ), None)
        if env is None:
            continue
        key = ast.literal_eval(env.args[0])
        default = ast.literal_eval(env.args[1]) if len(env.args) > 1 else ""
        kind = "str"
        if isinstance(node.value, ast.Compare):
            kind = "bool"
        elif isinstance(node.value, ast.Call):
            if isinstance(node.value.func, ast.Name) and node.value.func.id == "int":
                kind = "int"
            elif isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "split":
                kind = "list"
        fields[key] = {"key": key, "default": default, "type": kind,
                       "group": group_for(key), "secret": is_secret(key)}
        if key in CHOICES:
            fields[key]["choices"] = CHOICES[key]
    return fields


class Configuration:
    def __init__(self, root, environment=None):
        self.root = Path(root).resolve()
        self.path = self.root / ".env"
        self.schema = configuration_schema((self.root / "config/settings.py").read_text(encoding="utf-8"))
        self.environment = dict(os.environ if environment is None else environment)
        self.lock = threading.RLock()

    def values(self):
        with self.lock:
            local = dotenv_values(self.path, interpolate=False) if self.path.exists() else {}
        return {key: str(self.environment.get(key, local.get(key, field["default"]) or ""))
                for key, field in self.schema.items()}

    def fields(self):
        values = self.values()
        result = []
        for key, field in self.schema.items():
            value = values[key]
            if field["secret"]:
                value = ""
            elif field["type"] == "bool":
                value = value.lower() == "true"
            elif field["type"] == "int":
                try:
                    value = int(value)
                except ValueError:
                    pass  # Keep invalid configuration visible and editable.
            result.append({**field, "value": value, "configured": bool(values[key]),
                           "readonly": key in self.environment})
        return result

    def save(self, changes):
        if not isinstance(changes, dict):
            raise ValueError("values must be an object")
        clean = {}
        for key, value in changes.items():
            if key not in self.schema:
                raise ValueError("Unknown configuration key: " + key)
            if key in self.environment:
                raise ValueError(key + " is controlled by the server environment")
            field = self.schema[key]
            if field["type"] == "bool":
                if not isinstance(value, bool):
                    raise ValueError(key + " must be true or false")
                value = str(value)
            elif field["type"] == "int":
                if isinstance(value, bool) or not isinstance(value, (str, int)):
                    raise ValueError(key + " must be an integer")
                try:
                    value = str(int(value))
                except ValueError:
                    raise ValueError(key + " must be an integer") from None
                if not 0 <= int(value) <= 86400000:
                    raise ValueError(key + " is outside the supported range")
            elif not isinstance(value, str):
                raise ValueError(key + " must be text")
            if len(value) > 4096 or any(c in value for c in ("\x00", "\r", "\n")):
                raise ValueError(key + " contains invalid or excessive text")
            if field.get("choices") and value not in field["choices"]:
                raise ValueError("Invalid option for " + key)
            for resource_key, _ in RESOURCE_KEYS.values():
                if key == resource_key:
                    self._resource_path(value)
            clean[key] = value
        with self.lock:
            fd, temp = tempfile.mkstemp(dir=self.root, prefix=".env-web-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    if self.path.exists():
                        stream.write(self.path.read_text(encoding="utf-8"))
                for key, value in clean.items():
                    set_key(temp, key, value, quote_mode="always")
                os.chmod(temp, 0o600)
                os.replace(temp, self.path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)

    def resource_path(self, kind):
        if kind not in RESOURCE_KEYS:
            raise ValueError("Unknown resource")
        key, default = RESOURCE_KEYS[kind]
        return self._resource_path(self.values().get(key) or default)

    def _resource_path(self, value):
        path = (self.root / value).resolve()
        # Resource editors must never become arbitrary server file editors.
        if self.root not in path.parents or path.suffix.lower() != ".txt":
            raise ValueError("Resource paths must be .txt files inside the project")
        allowed = (self.root / "config", self.root / "data")
        if not any(folder in path.parents for folder in allowed):
            raise ValueError("Resources must be inside config/ or data/")
        if path.name in ("password.txt", "5sim_config.txt"):
            raise ValueError("Use the configuration page to manage secrets")
        return path

    def read_resource(self, kind):
        path = self.resource_path(kind)
        return {"path": str(path.relative_to(self.root)),
                "content": path.read_text(encoding="utf-8") if path.exists() else ""}

    def save_resource(self, kind, content):
        if not isinstance(content, str) or len(content.encode("utf-8")) > 1024 * 1024 or "\x00" in content:
            raise ValueError("Resource content must be text of at most 1 MiB")
        if kind == "proxies":
            for line in content.splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                parts = line.strip().split(":")
                if len(parts) not in (2, 4) or not parts[0] or not parts[1].isdigit() or not 1 <= int(parts[1]) <= 65535:
                    raise ValueError("Proxy format: host:port or host:port:user:pass")
        with self.lock:
            path = self.resource_path(kind)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".web-resource-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(content)
                os.replace(temp, path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
