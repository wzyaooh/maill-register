"""
Selenium Runner - Chrome WebDriver-based Gmail account creation flow
Handles driver creation, account creation, and verification for Selenium engine.
"""
import os
import glob
import time
import random
import logging
import tempfile
import uuid
import string

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.action_chains import ActionChains

try:
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:
    ChromeDriverManager = None

from config.settings import Config
from core.fingerprint import inject_selenium_poltergeist
from core.trust_builder import (
    warm_up_session, ghost_mode_prepare,
)
from core.account_manager import account_manager
from core.profile_runtime import (
    BrowserProfileKernel,
    ProfileConflictError,
    ProfileRuntime,
    ProfileRuntimeError,
    ProfileRuntimeMismatchError,
    ProxyUnavailableError,
    classify_session_auth,
    proxy_launch_config,
    validate_profile_identity,
    inspect_process_stopped,
    registration_cleanup_verified,
    release_registration_lease,
)
from core.operation_result import coerce_creation_result
from core.secret_safety import (
    has_durable_sms_context,
    normalize_flow_mode,
    normalize_error_code,
    normalize_sms_service,
    normalize_verification_method,
    safe_registration_result_summary,
    safe_warm_result_summary,
)

logger = logging.getLogger('gmail_creator_selenium')

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
]

SCREEN_SIZES = [
    (1366, 768), (1440, 900), (1536, 864),
    (1600, 900), (1920, 1080), (1280, 720),
]


def _load_names():
    names_file = Config.NAMES_FILE if hasattr(Config, 'NAMES_FILE') else "data/names.txt"
    names = []
    try:
        with open(names_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    names.append(line)
    except FileNotFoundError:
        pass
    return names


_names_list = _load_names()


def generate_name():
    if _names_list:
        return random.choice(_names_list)
    return f"User{random.randint(1000, 9999)}"


def generate_password(length=14):
    """Generate a strong unique password per account."""
    upper = random.choices(string.ascii_uppercase, k=3)
    lower = random.choices(string.ascii_lowercase, k=5)
    digits = random.choices(string.digits, k=3)
    specials = random.choices("!@#$%&*", k=2)
    filler = random.choices(string.ascii_letters + string.digits, k=max(0, length - 13))
    pool = upper + lower + digits + specials + filler
    random.shuffle(pool)
    return "".join(pool)


def validate_birthday(birthday_str):
    try:
        month, day, year = birthday_str.split()
        month = str(int(month))
        if not (1 <= int(month) <= 12):
            month = "1"
        if not (1 <= int(day) <= 31):
            day = "1"
        if not (1900 <= int(year) <= 2010):
            year = "1990"
        return month, day, year
    except Exception:
        return "1", "1", "1990"


def _parse_proxy(proxy_string):
    """Parse proxy string into components: host:port or user:pass@host:port."""
    try:
        config = proxy_launch_config(proxy_string)
        if not config:
            return None
        server = config["server"].split("://", 1)[-1]
        host, port = server.rsplit(":", 1)
        return {
            "host": host.strip("[]"), "port": port,
            "user": config.get("username"), "pass": config.get("password"),
        }
    except Exception:
        pass
    return None


def create_driver(proxy=None, profile_path=None, profile_manifest=None,
                  lease_owned=False, purpose="registration", profile_lease=None):
    """Create Chrome using the profile manifest's native identity.

    ``purpose`` is ``registration`` for the legacy compatibility hooks.  Warm
    and health launches are observation-oriented and never regenerate identity
    values or inject registration-only scripts.
    """
    try:
        if profile_manifest is not None and not lease_owned:
            raise ProfileConflictError(
                "a persisted profile must be initialized while its lease is held"
            )
        if profile_manifest is not None and not profile_path:
            raise ProfileConflictError(
                "a persisted profile requires its persistent profile path"
            )
        if purpose in ("health", "warm") and (
                profile_manifest is None or not profile_path or not lease_owned):
            raise ProfileConflictError(
                "health/warm profile launches require a resolved manifest and lease"
            )
        if profile_manifest is not None and profile_manifest.get("engine") != "selenium":
            raise ProfileConflictError(
                "profile manifest is bound to a different engine"
            )
        if profile_manifest is not None:
            # Never synthesize a new persona for a persisted profile.  A
            # missing user-agent/viewport/etc. is a corrupt binding, not a
            # reason to choose a random Selenium default.
            validate_profile_identity(profile_manifest.get("identity"))
        stable_check = getattr(profile_lease, "assert_stable", None)
        if callable(stable_check):
            stable_check()
        chrome_options = ChromeOptions()

        profile_dir = profile_path or os.path.join(
            tempfile.gettempdir(), f"chrome_profile_{str(uuid.uuid4())[:8]}"
        )
        if callable(stable_check):
            stable_check()
        if profile_manifest is None:
            os.makedirs(profile_dir, exist_ok=True)
        chrome_options.add_argument(f'--user-data-dir={profile_dir}')

        identity = (profile_manifest or {}).get("identity", {}) if profile_manifest else {}
        viewport = identity.get("viewport") or {}
        width = int(viewport.get("width", 1366))
        height = int(viewport.get("height", 768))
        chrome_options.add_argument(f'--window-size={width},{height}')
        user_agent = (
            identity["user_agent"] if profile_manifest is not None
            else identity.get("user_agent") or random.choice(USER_AGENTS)
        )
        chrome_options.add_argument(f'user-agent={user_agent}')
        locale = str(identity.get("locale") or "").strip()
        if locale:
            language = locale.split("-", 1)[0]
            accept_languages = (
                f"{locale},{language}" if language != locale else locale
            )
            chrome_options.add_argument(f'--lang={locale}')
            chrome_options.add_experimental_option(
                "prefs", {"intl.accept_languages": accept_languages}
            )

        chrome_options.add_experimental_option('excludeSwitches', ['enable-automation', 'enable-logging'])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_argument("--disable-webrtc")
        chrome_options.add_argument("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--disable-infobars')
        chrome_options.add_argument('--disable-notifications')
        chrome_options.add_argument('--disable-software-rasterizer')
        chrome_options.add_argument('--disable-logging')
        chrome_options.add_argument('--log-level=3')
        chrome_options.add_argument('--ignore-certificate-errors')
        chrome_options.add_argument('--ignore-ssl-errors')
        chrome_options.add_argument('--no-experiments')
        chrome_options.add_argument('--no-default-browser-check')
        chrome_options.add_argument('--no-first-run')
        chrome_options.add_argument('--disable-extensions')
        chrome_options.add_argument('--disable-popup-blocking')

        if Config.HEADLESS_MODE:
            chrome_options.add_argument('--headless=new')
            if not profile_manifest:
                chrome_options.add_argument('--window-size=1920,1080')

        if profile_manifest:
            ProfileRuntime.from_environment().validate_proxy(profile_manifest, proxy)
        if proxy:
            proxy_settings = proxy_launch_config(proxy)
            if proxy_settings is None:
                raise ProxyUnavailableError("configured proxy has an invalid format")
            if proxy_settings.get("username") is not None:
                # Chrome's --proxy-server flag cannot safely carry HTTP
                # credentials.  Refuse to launch rather than silently falling
                # back to a direct connection.
                raise ProxyUnavailableError(
                    "Selenium adapter cannot apply an authenticated proxy without a credential bridge"
                )
            chrome_options.add_argument(
                f'--proxy-server={proxy_settings["server"]}'
            )
            logger.info("Using configured unauthenticated proxy")

        service = _get_chrome_service()

        max_retries = 3
        for attempt in range(max_retries):
            driver = None
            try:
                if callable(stable_check):
                    # Check on every attempt: a failed driver startup may have
                    # given another process a chance to replace the path.
                    stable_check()
                driver = webdriver.Chrome(service=service, options=chrome_options)

                if profile_manifest is not None:
                    driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                        "width": width,
                        "height": height,
                        "deviceScaleFactor": 1,
                        "mobile": bool(identity.get("is_mobile", False)),
                    })
                    driver.execute_cdp_cmd(
                        "Emulation.setLocaleOverride", {"locale": locale}
                    )

                if purpose == "registration":
                    driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
                    'source': '''
                        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                        Object.defineProperty(navigator, 'hardwareConcurrency', {
                            get: () => [2, 4, 6, 8][Math.floor(Math.random() * 4)]
                        });
                        Object.defineProperty(navigator, 'deviceMemory', {
                            get: () => [4, 8, 16][Math.floor(Math.random() * 3)]
                        });
                        const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
                        HTMLCanvasElement.prototype.toDataURL = function(type) {
                            if (this.width > 0 && this.height > 0) {
                                const ctx = this.getContext('2d');
                                ctx.fillStyle = 'rgba(' + Math.random()*255 + ',' + Math.random()*255 + ',' + Math.random()*255 + ',0.01)';
                                ctx.fillRect(0, 0, 1, 1);
                            }
                            return originalToDataURL.apply(this, arguments);
                        };
                    '''
                    })

                # Native timezone/geolocation settings are applied through CDP
                # where Chrome supports them; failures are non-fatal on old
                # drivers and remain visible through runtime info.
                if identity.get("timezone_id"):
                    try:
                        driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {
                            "timezoneId": identity["timezone_id"]
                        })
                    except Exception:
                        pass
                location = identity.get("geolocation") or {}
                if location:
                    try:
                        driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {
                            "latitude": float(location.get("latitude", 0)),
                            "longitude": float(location.get("longitude", 0)),
                            "accuracy": 100,
                        })
                    except Exception:
                        pass

                driver.set_page_load_timeout(30)
                # Health/warm probes must validate the recorded runtime before
                # their first navigation.  The registration flow may perform
                # this lightweight readiness visit, but persisted profiles are
                # opened as an observation-only session until the kernel has
                # checked channel/version and proxy bindings.
                if purpose == "registration":
                    driver.get("https://www.google.com")
                    time.sleep(2)
                logger.info("Selenium browser created successfully")
                try:
                    capabilities = getattr(driver, "capabilities", {}) or {}
                    version = capabilities.get("browserVersion") or capabilities.get("version") or ""
                    driver._profile_runtime_info = {
                        "channel": "chrome", "major_version": str(version).split(".", 1)[0]
                    }
                except Exception:
                    driver._profile_runtime_info = {"channel": "chrome", "major_version": ""}
                recorded_major = str(
                    ((profile_manifest or {}).get("browser") or {}).get("major_version")
                    or ""
                ).strip()
                actual_major = str(
                    driver._profile_runtime_info.get("major_version") or ""
                ).strip()
                if recorded_major and actual_major and recorded_major != actual_major:
                    raise ProfileRuntimeMismatchError(
                        "running browser major version does not match the profile manifest"
                    )
                return driver
            except Exception as e:
                logger.warning("Browser creation attempt %s failed: %s", attempt + 1, type(e).__name__)
                if driver is not None:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    finally:
                        driver = None
                if isinstance(e, ProfileRuntimeError):
                    # Binding/path/runtime errors are deterministic and must
                    # not be retried against a potentially different profile.
                    raise
                if attempt == max_retries - 1:
                    raise
                time.sleep(2)

    except ProfileRuntimeError:
        raise
    except Exception as e:
        logger.error("Failed to create Selenium driver: %s", type(e).__name__)
        return None


def get_runtime_info(driver):
    """Return the adapter's best-effort channel/version observation."""
    return dict(getattr(driver, "_profile_runtime_info", {}) or {})


def _close_registration_driver(driver):
    """Close a registration driver and require a concrete stop observation."""
    if driver is None:
        return {"success": True, "browser_process_stopped": True}
    try:
        quit_result = driver.quit()
    except BaseException:
        return {"success": False, "browser_process_stopped": False}

    if isinstance(quit_result, dict):
        cleanup = {
            "success": quit_result.get("success") is True,
            "browser_process_stopped": (
                quit_result.get("browser_process_stopped") is True
            ),
        }
    else:
        # Selenium's normal quit() API returns None.  The service process is
        # the authoritative stop fact in that compatibility case.
        cleanup = {"success": False, "browser_process_stopped": False}

    observed = inspect_process_stopped(driver)
    if observed is False:
        return {"success": False, "browser_process_stopped": False}
    if observed is True:
        cleanup["browser_process_stopped"] = True
        if quit_result is None:
            cleanup["success"] = True
    return cleanup


def _registration_session_facts(driver, expected_email):
    """Collect registration proof through the shared profile auth protocol."""
    try:
        driver.get("https://mail.google.com/")
    except Exception:
        return classify_session_auth(expected_email=expected_email)

    application_shell = BrowserProfileKernel._selenium_application_shell(driver)
    business_origin = getattr(driver, "current_url", "")
    try:
        cookies = driver.get_cookies()
    except Exception:
        cookies = []
    kernel = BrowserProfileKernel()
    identity = kernel._fetch_identity_selenium(
        driver,
        expected_email=expected_email,
        manifest_email=expected_email,
    )
    return kernel._identity_auth_facts(
        identity,
        expected_email=expected_email,
        manifest={},
        text=str(getattr(driver, "page_source", "") or ""),
        cookies=cookies,
        origin=business_origin,
        application_shell=application_shell,
    )


def _get_chrome_service():
    """Try multiple methods to get a ChromeDriver service."""
    if ChromeDriverManager:
        try:
            path = ChromeDriverManager().install()
            if path and os.path.exists(path) and os.path.isfile(path):
                return ChromeService(path)
        except Exception:
            pass

    common_paths = [
        os.path.join(os.getcwd(), "chromedriver.exe"),
        "C:\\chromedriver\\chromedriver.exe",
    ]
    wdm_glob = os.path.join(os.path.expanduser("~"), ".wdm", "drivers", "chromedriver", "*", "chromedriver.exe")
    matches = glob.glob(wdm_glob)
    if matches:
        common_paths.insert(0, matches[0])

    for path in common_paths:
        if os.path.exists(path) and os.path.isfile(path):
            return ChromeService(path)

    return ChromeService()


def _click_next(driver):
    """Find and click the Next button using multiple strategies."""
    selectors = [
        "//button[contains(text(), 'Next')]",
        "//button[contains(@class, 'VfPpkd-LgbsSe')]",
        "//button[@type='submit']",
        "//button[contains(@aria-label, 'Next')]",
        "//span[contains(text(), 'Next')]/parent::button",
    ]
    for sel in selectors:
        try:
            elements = driver.find_elements(By.XPATH, sel)
            for el in elements:
                if el.is_displayed() and el.is_enabled():
                    driver.execute_script("arguments[0].scrollIntoView(true);", el)
                    time.sleep(0.5)
                    try:
                        el.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", el)
                    return True
        except Exception:
            continue
    return False


def _human_typing(element, text, delay_range=(0.08, 0.18)):
    for char in text:
        element.send_keys(char)
        time.sleep(random.uniform(*delay_range))


def _fill_field(driver, element, value):
    """Fill a field using multiple methods."""
    try:
        element.clear()
        time.sleep(0.3)
        element.send_keys(value)
        return True
    except Exception:
        pass
    try:
        driver.execute_script(
            "arguments[0].value = arguments[1];"
            "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));"
            "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
            element, value
        )
        return True
    except Exception:
        pass
    try:
        ActionChains(driver).move_to_element(element).click().send_keys(value).perform()
        return True
    except Exception:
        return False


def create_account_selenium(driver, wait, username, password, birthday_str, gender,
                            mode="standard", progress=None, task_id=None, *,
                            use_sms_api=False, job_id="", attempt_id="",
                            order_store=None):
    """
    Create a Gmail account using Selenium WebDriver.

    Returns: (success: bool, error_type: str or None)
    """
    try:
        if mode == "youtube":
            driver.get("https://accounts.google.com/signup/v2/webcreateaccount?"
                        "continue=https://www.youtube.com/&flowName=GlifWebSignIn&flowEntry=SignUp")
        elif mode == "workspace":
            driver.get("https://accounts.google.com/signup/v2/webcreateaccount?"
                        "continue=https://workspace.google.com/&flowName=GlifWebSignIn&flowEntry=SignUp")
        else:
            driver.get("https://accounts.google.com/signup/v2/createaccount?"
                        "flowName=GlifWebSignIn&flowEntry=SignUp")

        wait.until(EC.presence_of_element_located((By.NAME, "firstName")))
        time.sleep(random.uniform(1, 3))

        full_name = generate_name()
        parts = full_name.split()
        first_name = parts[0] if parts else "User"
        last_name = parts[-1] if len(parts) > 1 else "User"

        first_el = driver.find_element(By.NAME, "firstName")
        _fill_field(driver, first_el, first_name)

        last_el = driver.find_element(By.NAME, "lastName")
        _fill_field(driver, last_el, last_name)

        time.sleep(1.5)
        _click_next(driver)
        time.sleep(3)

        month, day, year = validate_birthday(birthday_str)
        month_names = ["January", "February", "March", "April", "May", "June",
                       "July", "August", "September", "October", "November", "December"]

        driver.execute_script("""
            var sel = document.getElementById('month');
            if (sel) { sel.value = arguments[0]; sel.dispatchEvent(new Event('change', {bubbles:true})); }
        """, month)
        time.sleep(0.5)

        driver.execute_script("""
            var d = document.querySelector('input[name="day"]');
            if (d) { d.value = arguments[0]; d.dispatchEvent(new Event('input', {bubbles:true})); }
            var y = document.querySelector('input[name="year"]');
            if (y) { y.value = arguments[1]; y.dispatchEvent(new Event('input', {bubbles:true})); }
        """, day, year)

        driver.execute_script("""
            var sel = document.getElementById('gender');
            if (sel) { sel.value = arguments[0]; sel.dispatchEvent(new Event('change', {bubbles:true})); }
        """, gender)
        time.sleep(1)

        _click_next(driver)
        time.sleep(3)

        # Find and click "Create your own Gmail address"
        _select_create_own(driver)
        time.sleep(2)

        # Fill username
        username_field = _find_username_field(driver, wait)
        if username_field:
            _fill_field(driver, username_field, username)
            time.sleep(1)
            _click_next(driver)
            time.sleep(2)
        else:
            logger.error("Username field not found")
            return False, "USERNAME_FIELD_NOT_FOUND"

        # Fill password
        try:
            pw_field = wait.until(EC.presence_of_element_located((By.NAME, "Passwd")))
            confirm_field = wait.until(EC.presence_of_element_located((By.NAME, "PasswdAgain")))
            wait.until(EC.element_to_be_clickable((By.NAME, "Passwd")))

            pw_field.clear()
            confirm_field.clear()
            time.sleep(0.5)
            _human_typing(pw_field, password, (0.05, 0.12))
            time.sleep(0.5)
            _human_typing(confirm_field, password, (0.05, 0.12))
            time.sleep(1)

            _click_next(driver)
            time.sleep(3)
        except Exception as e:
            logger.error("Password entry failed: %s", type(e).__name__)
            return False, "PASSWORD_ENTRY_FAILED"

        # Check what page we're on now
        page_source = driver.page_source.lower()

        if "phone" in page_source and ("verify" in page_source or "number" in page_source):
            logger.warning("Phone verification detected")
            if use_sms_api:
                from core.selenium_phone_bypass import run_selenium_sms_verification

                sms_success, sms_result = run_selenium_sms_verification(
                    driver, wait, use_sms_api=True, job_id=job_id,
                    attempt_id=attempt_id, order_store=order_store,
                )
                if sms_success:
                    verification_method = normalize_verification_method(
                        sms_result, default="unknown"
                    )
                    if verification_method == "unknown":
                        return False, "sms_error"
                    return True, verification_method
                return False, normalize_error_code(
                    sms_result, default="sms_error", allow_empty=False
                )
            return False, "PHONE_REQUIRED"

        if "qr" in page_source or "scan" in page_source:
            logger.warning("QR code verification detected")
            return False, "QR_BLOCKED"

        email = f"{username}@gmail.com"
        logger.info(f"Account created: {email}")
        return True, None

    except Exception as e:
        logger.error("Selenium account creation error: %s", type(e).__name__)
        return False, "UNKNOWN_ERROR"


def _select_create_own(driver):
    """Try to click 'Create your own Gmail address' option."""
    selectors = [
        "//div[contains(text(), 'Create your own Gmail address')]",
        "//span[contains(text(), 'Create your own Gmail address')]",
        "//span[contains(text(), 'Create your own')]",
        "//div[contains(text(), 'Create your own')]",
    ]
    for sel in selectors:
        try:
            elements = driver.find_elements(By.XPATH, sel)
            for el in elements:
                if el.is_displayed():
                    driver.execute_script("arguments[0].scrollIntoView(true);", el)
                    time.sleep(0.5)
                    el.click()
                    return True
        except Exception:
            continue
    return False


def _find_username_field(driver, wait):
    """Find the username input field using multiple strategies."""
    selectors = [
        "//input[@type='text' and contains(@name, 'user')]",
        "//input[@type='text' and contains(@id, 'user')]",
        "//input[@type='text' and contains(@aria-label, 'email')]",
        "//input[@type='text' and contains(@aria-label, 'Email')]",
        "//input[@name='Username']",
        "//input[@jsname='YPqjbf']",
        "//input[contains(@class, 'whsOnd')]",
    ]
    for sel in selectors:
        try:
            elements = driver.find_elements(By.XPATH, sel)
            for el in elements:
                if el.is_displayed() and el.is_enabled():
                    return el
        except Exception:
            continue

    try:
        return wait.until(EC.presence_of_element_located((By.XPATH, "//input[@type='text']")))
    except Exception:
        return None


def run_selenium_flow(i, num_accounts, username, password, warmup_minutes=5,
                      stealth_mode=True, mode="standard", proxy=None,
                      *, use_sms_api=False, job_id="", attempt_id="",
                      order_store=None, return_result=False):
    """
    Complete Selenium-based account creation flow.

    Returns: bool (success)
    """
    mode = normalize_flow_mode(mode)

    def finish_result(success, error_code=""):
        if return_result:
            payload = {
                "success": bool(success),
                "error_code": error_code or "",
                "registration_result": registration_result,
                "warm_result": safe_warm_result_summary(warm_result)
                if warm_result else {},
            }
            return payload
        return bool(success)

    profile_handle = None
    profile_manifest = None
    profile_lease = None
    lease_released = False
    profile_ready = False
    driver = None
    cleanup_failure_reason = ""
    enclosing_result = None
    registration_result = {
        "success": False,
        "status": "failed",
        "error_code": "",
    }
    warm_result = {}
    if use_sms_api and not has_durable_sms_context(job_id, attempt_id, order_store):
        registration_result["error_code"] = "sms_missing_attempt_context"
        return finish_result(False, "sms_missing_attempt_context")

    runtime = ProfileRuntime.from_environment()
    try:
        # Provision and lease before starting Chrome.  The manifest is the
        # durable engine/identity/proxy binding used by all later operations.
        profile_handle = runtime.provision("", "selenium", proxy=proxy)
        profile_manifest = runtime.load(profile_handle)
        profile_lease = runtime.lease(profile_handle, "registration")
        profile_lease.acquire()
        profile_lease.assert_stable()

        driver = create_driver(
            proxy=proxy, profile_path=str(profile_handle.path),
            profile_manifest=profile_manifest, lease_owned=True,
            profile_lease=profile_lease,
            purpose="registration",
        )
        if not driver:
            return finish_result(False, "browser_crash")
        runtime.record_runtime(profile_handle, get_runtime_info(driver))

        wait = WebDriverWait(driver, Config.BROWSER_TIMEOUT)

        # Inject trust cookies via Cookie Reaper
        if Config.ENABLE_COOKIE_REAPER:
            try:
                from core.cookie_reaper import inject_cookies_selenium
                inject_cookies_selenium(driver)
            except Exception:
                pass

        if stealth_mode:
            ghost_mode_prepare(driver, warmup_minutes)
        else:
            if Config.ENABLE_FINGERPRINT_MASKING:
                inject_selenium_poltergeist(driver)
            if Config.ENABLE_SESSION_WARMING:
                warm_up_session(driver)

        birthday = Config.YOUR_BIRTHDAY
        gender = str(Config.YOUR_GENDER)

        success, error_type = create_account_selenium(
            driver, wait, username, password,
            birthday, gender, mode=mode,
            use_sms_api=use_sms_api, job_id=job_id,
            attempt_id=attempt_id, order_store=order_store,
        )
        if not success:
            return finish_result(False, error_type)

        email = f"{username}@gmail.com"
        auth_facts = _registration_session_facts(driver, email)
        if not auth_facts.get("authenticated"):
            return finish_result(
                False, auth_facts.get("code") or "login_required"
            )
        verification_method = ""
        if success and error_type:
            normalized_method = normalize_verification_method(
                error_type, default="unknown"
            )
            if normalized_method != "unknown":
                verification_method = normalized_method
        browser_info = get_runtime_info(driver)
        runtime.bind(profile_handle, email, browser_info)
        bound_manifest = runtime.load(profile_handle)
        strategy_label = "selenium_" + mode
        saved = account_manager.save(
            email=email,
            password=password,
            proxy=proxy or "",
            strategy=strategy_label,
            profile_path=str(profile_handle.path),
            profile_id=profile_handle.profile_id,
            engine="selenium",
            profile_state="bound",
            identity_state=bound_manifest.get("identity_state", "native"),
            browser_status="authenticated",
            overall_status="active",
            sms_service=(
                normalize_sms_service(verification_method[4:])
                if verification_method.startswith("sms_") else ""
            ),
            registration_result={
                "success": True,
                "status": "created",
                "error_code": "",
                "verification_method": verification_method,
            },
            warm_result={},
        )
        if not saved:
            raise RuntimeError("Unable to persist account/profile binding")
        runtime.mark_ready(profile_handle)
        if not account_manager.db.update_profile_state(
            email, "ready", bound_manifest.get("identity_state", "native")
        ):
            raise RuntimeError("Unable to persist ready profile state")
        profile_ready = True
        registration_result = safe_registration_result_summary({
            "success": True,
            "status": "created",
            "error_code": "",
            "verification_method": verification_method,
        })

        # Post-creation account warming.  Registration is already durable at
        # this point; any warm/cleanup failure is recorded separately.
        if Config.ENABLE_SESSION_WARMING:
            try:
                from core.account_warmer import warm_account_selenium
                # Chromium locks a user-data directory while the registration
                # driver is alive. Close it before reopening the same profile.
                close_result = _close_registration_driver(driver)
                if registration_cleanup_verified(close_result):
                    driver = None
                if not registration_cleanup_verified(close_result):
                    cleanup_failure_reason = "browser_cleanup_failed"
                    warm_result = {
                        "success": False,
                        "error_code": "cleanup_failed",
                        "cleanup_status": "failed",
                        "browser_process_stopped": (
                            close_result.get("browser_process_stopped") is True
                            if isinstance(close_result, dict) else False
                        ),
                        "lease_released": False,
                    }
                else:
                    lease_released = release_registration_lease(profile_lease)
                    if not lease_released:
                        cleanup_failure_reason = "lease_release_failed"
                        warm_result = {
                            "success": False,
                            "error_code": "cleanup_failed",
                            "cleanup_status": "failed",
                            "browser_process_stopped": True,
                            "lease_released": False,
                        }
                    else:
                        raw_warm_result = warm_account_selenium(
                            email, password, duration_minutes=2,
                            profile_id=profile_handle.profile_id, engine="selenium",
                            proxy=proxy,
                        )
                        warm_result = safe_warm_result_summary(raw_warm_result)
                        if not warm_result.get("success"):
                            logger.warning(
                                "Post-registration warming failed: %s",
                                safe_warm_result_summary(warm_result),
                            )
            except Exception as warm_err:
                logger.debug("Account warming (non-fatal): %s", type(warm_err).__name__)
                if driver is not None:
                    cleanup_failure_reason = (
                        cleanup_failure_reason or "browser_cleanup_failed"
                    )
                elif not lease_released:
                    cleanup_failure_reason = (
                        cleanup_failure_reason or "lease_release_failed"
                    )
                warm_result = {
                    "success": False,
                    "error_code": (
                        "cleanup_failed"
                        if driver is not None or not lease_released else "error"
                    ),
                    "cleanup_status": (
                        "failed" if driver is not None or not lease_released else "completed"
                    ),
                    "browser_process_stopped": driver is None,
                    "lease_released": lease_released is True,
                }
        else:
            warm_result = {"success": True, "status": "skipped", "error_code": ""}

        try:
            if not account_manager.db.update_operation_results(
                email, warm_result=warm_result
            ):
                logger.debug("Unable to persist post-registration warm result")
        except Exception as persist_err:
            logger.debug(
                "Unable to persist post-registration warm result: %s",
                type(persist_err).__name__,
            )

        # Telegram notification
        if success:
            try:
                from core.telegram_notifier import notifier
                notifier.notify_account_created(
                    email=f"{username}@gmail.com", strategy=mode,
                )
            except Exception:
                pass

        enclosing_result = finish_result(success)

    except Exception as e:
        logger.error("Selenium flow error: %s", type(e).__name__)
        return finish_result(False, getattr(e, "code", None) or type(e).__name__)
    finally:
        if driver is not None:
            close_result = _close_registration_driver(driver)
            if registration_cleanup_verified(close_result):
                driver = None
            else:
                cleanup_failure_reason = "browser_cleanup_failed"
        if profile_lease is not None and not lease_released:
            # Keep the OS lease held while a live/unknown browser process is
            # still reachable.  The profile is quarantined below and the
            # process boundary closes the descriptor when this flow exits.
            if driver is None and release_registration_lease(profile_lease):
                lease_released = True
            elif driver is None:
                cleanup_failure_reason = cleanup_failure_reason or "lease_release_failed"
            else:
                cleanup_failure_reason = cleanup_failure_reason or "browser_cleanup_failed"
        if profile_handle is not None:
            try:
                if cleanup_failure_reason:
                    runtime.mark_cleanup_failed(profile_handle, cleanup_failure_reason)
                elif not profile_ready:
                    runtime.mark_orphaned(profile_handle, "registration_failed")
            except Exception as cleanup_exc:
                logger.debug(
                    "Unable to persist registration profile cleanup state: %s",
                    type(cleanup_exc).__name__,
                )
    if cleanup_failure_reason and isinstance(enclosing_result, dict) \
            and enclosing_result.get("success"):
        enclosing_result["success"] = False
        enclosing_result["error_code"] = "cleanup_failed"
    elif cleanup_failure_reason and enclosing_result is True:
        enclosing_result = False
    return enclosing_result
