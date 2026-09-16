"""
Proxy Manager - Advanced proxy rotation and health checking

Maintains two separate pools:
  - static: manually imported proxies from Config.PROXY_FILE (proxies.txt)
  - kooip:  KKOIP dynamic residential pool (legacy pool identifier)
"""
import os
import time
import random
import string
import logging
import requests
from config.settings import Config
from core.secret_safety import safe_proxy_label

logger = logging.getLogger('gmail_creator_proxy')

SOURCE_STATIC = "static"
SOURCE_KOOIP = "kooip"


class ProxyManager:
    def __init__(self):
        self._static_proxies = []
        self._kooip_proxies = []
        self._sources = {}
        self._current_index = 0
        self._health = {}
        self._scores = {}
        self._load_proxies()
        self._build_kooip_pool()

    # ── Pool loading ────────────────────────────────────────────

    def _load_proxies(self):
        proxy_file = Config.PROXY_FILE
        if not os.path.exists(proxy_file):
            logger.warning(f"Proxy file not found: {proxy_file}")
            return
        with open(proxy_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    self._register(line, SOURCE_STATIC)
        if self._static_proxies:
            logger.info(f"Loaded {len(self._static_proxies)} static proxies")

    def _build_kooip_pool(self):
        if not getattr(Config, 'KOOIP_ENABLED', False):
            return
        if not (Config.KOOIP_USER_ID and Config.KOOIP_AUTH_NAME and Config.KOOIP_AUTH_PASSWORD):
            logger.warning("KOOIP_ENABLED=True but KKOIP credentials are incomplete — dynamic pool disabled")
            return
        if Config.KOOIP_STICKY_SESSION:
            pool_size = max(1, Config.KOOIP_SESSION_POOL_SIZE)
            for _ in range(pool_size):
                self._register(self._build_kooip_proxy(self._new_session()), SOURCE_KOOIP)
        else:
            # Without a session the gateway rotates the exit IP on every request,
            # so a single pool entry is sufficient
            self._register(self._build_kooip_proxy(None), SOURCE_KOOIP)
        logger.info(f"KKOIP dynamic pool ready: {len(self._kooip_proxies)} gateway session(s)")

    def _register(self, proxy, source):
        pool = self._static_proxies if source == SOURCE_STATIC else self._kooip_proxies
        pool.append(proxy)
        self._sources[proxy] = source
        self._health[proxy] = True
        self._scores[proxy] = 50

    @staticmethod
    def _new_session():
        return ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))

    @staticmethod
    def _build_kooip_proxy(session):
        """Build a gateway proxy in internal host:port:user:pass format.

        KKOIP credential mode:
        {uid}-{authname}:{authpwd}-{country}[-{session}][-{interval}]@{gateway}:{port}
        """
        user = f"{Config.KOOIP_USER_ID}-{Config.KOOIP_AUTH_NAME}"
        password = f"{Config.KOOIP_AUTH_PASSWORD}-{Config.KOOIP_COUNTRY}"
        if session:
            password += f"-{session}"
            interval = getattr(Config, 'KOOIP_ROTATE_INTERVAL', '')
            if interval:
                password += f"-{interval}"
        return f"{Config.KOOIP_GATEWAY}:{Config.KOOIP_GATEWAY_PORT}:{user}:{password}"

    def _replace_kooip_session(self, proxy):
        """Swap a dead KKOIP sticky session for a fresh one."""
        if not Config.KOOIP_STICKY_SESSION:
            return None
        try:
            idx = self._kooip_proxies.index(proxy)
        except ValueError:
            return None
        self._sources.pop(proxy, None)
        self._health.pop(proxy, None)
        self._scores.pop(proxy, None)
        new_proxy = self._build_kooip_proxy(self._new_session())
        self._kooip_proxies[idx] = new_proxy
        self._sources[new_proxy] = SOURCE_KOOIP
        self._health[new_proxy] = True
        self._scores[new_proxy] = 50
        logger.info("KKOIP session replaced with a fresh one")
        return new_proxy

    def refresh_kooip_pool(self):
        """Drop all KKOIP sessions and generate a brand-new set."""
        for proxy in self._kooip_proxies:
            self._sources.pop(proxy, None)
            self._health.pop(proxy, None)
            self._scores.pop(proxy, None)
        self._kooip_proxies = []
        self._build_kooip_pool()
        return len(self._kooip_proxies)

    # ── Pool selection ──────────────────────────────────────────

    def _candidates(self, source=None):
        source = source or getattr(Config, 'PROXY_POOL_PREFERENCE', 'auto')
        if source == SOURCE_STATIC:
            pool = list(self._static_proxies)
        elif source == SOURCE_KOOIP:
            pool = list(self._kooip_proxies)
        else:  # auto
            pool = list(self._static_proxies) + list(self._kooip_proxies)
        healthy = [p for p in pool if self._health.get(p, True)]
        return healthy or pool

    def get_source(self, proxy):
        return self._sources.get(proxy)

    @property
    def count(self):
        return len(self._static_proxies) + len(self._kooip_proxies)

    @property
    def static_count(self):
        return len(self._static_proxies)

    @property
    def kooip_count(self):
        return len(self._kooip_proxies)

    @property
    def healthy_count(self):
        all_proxies = self._static_proxies + self._kooip_proxies
        return sum(1 for p in all_proxies if self._health.get(p, True))

    def get_proxy(self, source=None, strategy="best"):
        """Unified accessor. source: None/'auto'/'static'/'kooip'."""
        if strategy == "random":
            return self.get_random(source)
        if strategy == "next":
            return self.get_next(source)
        return self.get_best(source)

    def get_random(self, source=None):
        candidates = self._candidates(source)
        if not candidates:
            return None
        return random.choice(candidates)

    def get_next(self, source=None):
        candidates = self._candidates(source)
        if not candidates:
            return None
        proxy = candidates[self._current_index % len(candidates)]
        self._current_index += 1
        return proxy

    def get_best(self, source=None):
        candidates = self._candidates(source)
        if not candidates:
            return None
        return max(candidates, key=lambda p: self._scores.get(p, 50))

    # ── Health / scoring ────────────────────────────────────────

    def mark_success(self, proxy):
        if proxy in self._scores:
            self._scores[proxy] = min(100, self._scores[proxy] + 10)
            self._health[proxy] = True

    def mark_failure(self, proxy, fatal=False):
        if proxy in self._scores:
            self._scores[proxy] = max(0, self._scores[proxy] - (30 if fatal else 10))
            if self._scores[proxy] <= 10:
                self._health[proxy] = False
                logger.warning("Proxy marked unhealthy (%s): %s",
                               self._sources.get(proxy, "?"), safe_proxy_label(proxy))
                # Dead KKOIP sessions are cheap to replace — rotate in a new one
                if self._sources.get(proxy) == SOURCE_KOOIP:
                    self._replace_kooip_session(proxy)

    def check_health(self, proxy, timeout=10):
        parsed = self.parse(proxy)
        if not parsed:
            return False
        try:
            proxies_dict = {}
            if parsed["user"]:
                proxy_url = f"http://{parsed['user']}:{parsed['pass']}@{parsed['host']}:{parsed['port']}"
            else:
                proxy_url = f"http://{parsed['host']}:{parsed['port']}"
            proxies_dict = {"http": proxy_url, "https": proxy_url}
            resp = requests.get("https://httpbin.org/ip", proxies=proxies_dict, timeout=timeout)
            if resp.status_code == 200:
                self._health[proxy] = True
                return True
        except Exception as e:
            logger.debug("Proxy health check failed for %s: %s",
                         safe_proxy_label(proxy), type(e).__name__)
        self._health[proxy] = False
        return False

    def check_all_health(self):
        results = {"healthy": 0, "unhealthy": 0}
        for proxy in self._static_proxies + self._kooip_proxies:
            if self.check_health(proxy):
                results["healthy"] += 1
            else:
                results["unhealthy"] += 1
        return results

    def get_ip_info(self, proxy=None):
        try:
            proxies_dict = {}
            if proxy:
                parsed = self.parse(proxy)
                if parsed:
                    if parsed["user"]:
                        url = f"http://{parsed['user']}:{parsed['pass']}@{parsed['host']}:{parsed['port']}"
                    else:
                        url = f"http://{parsed['host']}:{parsed['port']}"
                    proxies_dict = {"http": url, "https": url}

            ip_resp = requests.get("https://api.ipify.org?format=json", proxies=proxies_dict, timeout=10)
            ip = ip_resp.json().get("ip", "Unknown")

            info_resp = requests.get(f"https://ipinfo.io/{ip}/json", timeout=10)
            info = info_resp.json()

            is_datacenter = "hosting" in str(info.get("org", "")).lower()
            return {
                "ip": ip,
                "city": info.get("city", "N/A"),
                "country": info.get("country", "N/A"),
                "org": info.get("org", "N/A"),
                "is_datacenter": is_datacenter,
            }
        except Exception as e:
            logger.warning("IP info check failed: %s", type(e).__name__)
            return None

    def rotate_mobile_ip(self):
        url = getattr(Config, 'MOBILE_PROXY_IP_CHANGE_URL', '')
        if not url:
            return False
        try:
            logger.info("Rotating mobile proxy IP...")
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                wait_time = getattr(Config, 'PROXY_CHANGE_WAIT_TIME', 10)
                logger.info(f"IP changed. Waiting {wait_time}s for propagation...")
                time.sleep(wait_time)
                return True
            logger.warning(f"IP rotation returned status {resp.status_code}")
        except Exception as e:
            logger.error("Mobile IP rotation failed: %s", type(e).__name__)
        return False

    @staticmethod
    def parse(proxy_string):
        if not proxy_string:
            return None
        parts = proxy_string.split(":")
        if len(parts) == 2:
            return {"host": parts[0], "port": parts[1], "user": None, "pass": None}
        elif len(parts) == 4:
            return {"host": parts[0], "port": parts[1], "user": parts[2], "pass": parts[3]}
        return None

    @staticmethod
    def format_for_playwright(proxy_string):
        parsed = ProxyManager.parse(proxy_string)
        if not parsed:
            return None
        result = {"server": f"http://{parsed['host']}:{parsed['port']}"}
        if parsed["user"]:
            result["username"] = parsed["user"]
            result["password"] = parsed["pass"]
        return result

    @staticmethod
    def format_for_selenium(proxy_string, proxy_type="http"):
        parsed = ProxyManager.parse(proxy_string)
        if not parsed:
            return None
        if parsed["user"]:
            return f"{proxy_type}://{parsed['user']}:{parsed['pass']}@{parsed['host']}:{parsed['port']}"
        return f"{parsed['host']}:{parsed['port']}"

    def get_stats(self):
        all_proxies = self._static_proxies + self._kooip_proxies
        return {
            "total": len(all_proxies),
            "healthy": self.healthy_count,
            "unhealthy": len(all_proxies) - self.healthy_count,
            "static": {
                "total": len(self._static_proxies),
                "healthy": sum(1 for p in self._static_proxies if self._health.get(p, True)),
            },
            "kooip": {
                "total": len(self._kooip_proxies),
                "healthy": sum(1 for p in self._kooip_proxies if self._health.get(p, True)),
                "enabled": getattr(Config, 'KOOIP_ENABLED', False),
            },
            # Endpoint labels are useful for diagnostics; credentials never
            # cross the task/API boundary as dictionary keys.
            "scores": {
                safe_proxy_label(proxy): self._scores.get(proxy, 0)
                for proxy in all_proxies[:10]
            },
        }


proxy_manager = ProxyManager()
