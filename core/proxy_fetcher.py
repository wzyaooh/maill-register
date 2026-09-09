"""
Auto Proxy Fetcher - Automatically fetch free proxies from public sources
"""
import logging
import requests
import random
import time

logger = logging.getLogger('gmail_creator_proxy_fetch')

PROXY_SOURCES = [
    {
        "name": "ProxyScrape (HTTP)",
        "url": "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=yes&anonymity=all",
        "type": "plain",
    },
    {
        "name": "ProxyScrape (SOCKS5)",
        "url": "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=10000&country=all",
        "type": "plain",
    },
    {
        "name": "TheSpeedX HTTP",
        "url": "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt",
        "type": "plain",
    },
    {
        "name": "ShiftyTR HTTP",
        "url": "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
        "type": "plain",
    },
    {
        "name": "MoNoSolo HTTP",
        "url": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
        "type": "plain",
    },
]


def fetch_proxies(max_per_source=50):
    """
    Fetch proxies from all public sources.
    Returns: list of proxy strings (host:port)
    """
    all_proxies = set()

    for source in PROXY_SOURCES:
        try:
            resp = requests.get(source["url"], timeout=15)
            if resp.status_code == 200:
                lines = resp.text.strip().split("\n")
                proxies = [l.strip() for l in lines if l.strip() and ":" in l]
                sample = proxies[:max_per_source] if len(proxies) > max_per_source else proxies
                all_proxies.update(sample)
                logger.info(f"Fetched {len(sample)} proxies from {source['name']}")
        except Exception as e:
            logger.warning(f"Failed to fetch from {source['name']}: {e}")

    return list(all_proxies)


def test_proxy(proxy, timeout=10):
    """Test if a proxy is working by connecting to httpbin."""
    try:
        resp = requests.get(
            "https://httpbin.org/ip",
            proxies={"http": f"http://{proxy}", "https": f"http://{proxy}"},
            timeout=timeout,
        )
        if resp.status_code == 200:
            ip = resp.json().get("origin", "")
            return True, ip
    except Exception:
        pass
    return False, None


def fetch_and_test(max_proxies=20, test_count=50, timeout=8):
    """
    Fetch proxies, test a sample, and return only working ones.
    Returns: list of working proxy strings
    """
    logger.info("Fetching proxies from public sources...")
    raw = fetch_proxies()
    logger.info(f"Fetched {len(raw)} raw proxies total")

    if not raw:
        return []

    sample = random.sample(raw, min(test_count, len(raw)))
    working = []

    logger.info(f"Testing {len(sample)} proxies...")
    for proxy in sample:
        ok, ip = test_proxy(proxy, timeout=timeout)
        if ok:
            working.append(proxy)
            logger.info(f"Working proxy: {proxy} -> {ip}")
            if len(working) >= max_proxies:
                break

    logger.info(f"Found {len(working)} working proxies out of {len(sample)} tested")
    return working


def save_proxies_to_file(proxies, filepath="config/proxies.txt"):
    """Save proxy list to file, appending to existing."""
    import os
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    existing = set()
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            existing = {l.strip() for l in f if l.strip() and not l.startswith("#")}

    new_proxies = [p for p in proxies if p not in existing]

    if new_proxies:
        with open(filepath, "a", encoding="utf-8") as f:
            for p in new_proxies:
                f.write(f"{p}\n")
        logger.info(f"Saved {len(new_proxies)} new proxies to {filepath}")

    return len(new_proxies)
