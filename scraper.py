"""
scraper.py – Selenium-based login and attendance scraping for IMS NSIT portal.
"""

import io
import json
import subprocess
import tempfile
import hashlib
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urljoin
from pathlib import Path
from datetime import datetime

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoAlertPresentException, UnexpectedAlertPresentException, SessionNotCreatedException, WebDriverException
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup

from captcha_solver import solve_captcha_with_debug
from attendance_calc import parse_cell, is_valid_subject

logger = logging.getLogger(__name__)

IMS_LOGIN_URL = "https://www.imsnsit.org/imsnsit/student.htm"
FAST_MODE = os.getenv("FAST_MODE", "1") == "1"
MAX_CAPTCHA_RETRIES = int(os.getenv("MAX_CAPTCHA_RETRIES", "2" if FAST_MODE else "4"))
WAIT_TIMEOUT = int(os.getenv("WAIT_TIMEOUT", "10" if FAST_MODE else "15"))
ACTION_DELAY = float(os.getenv("ACTION_DELAY", "0.25" if FAST_MODE else "1.0"))
PAGE_SETTLE_DELAY = float(os.getenv("PAGE_SETTLE_DELAY", "0.6" if FAST_MODE else "2.0"))
ENABLE_PAGINATION = os.getenv("ENABLE_PAGINATION", "0" if FAST_MODE else "1") == "1"
INCLUDE_DATEWISE_TIMELINE = os.getenv("INCLUDE_DATEWISE_TIMELINE", "0" if FAST_MODE else "1") == "1"
CAPTCHA_DEBUG = os.getenv("CAPTCHA_DEBUG", "0") == "1"
CAPTCHA_DEBUG_DIR = Path(os.getenv("CAPTCHA_DEBUG_DIR", "captcha_debug"))
SOURCE_DEDUPE_SAMPLE_CHARS = int(os.getenv("SOURCE_DEDUPE_SAMPLE_CHARS", "1200"))
LOGIN_FRAME_WAIT = float(os.getenv("LOGIN_FRAME_WAIT", "2.0" if FAST_MODE else "5.0"))
ENABLE_WARM_SESSIONS = os.getenv("ENABLE_WARM_SESSIONS", "1") == "1"
WARM_SESSION_TTL_SECONDS = int(os.getenv("WARM_SESSION_TTL_SECONDS", "900"))
WARM_SESSION_MAX_POOL = int(os.getenv("WARM_SESSION_MAX_POOL", "4"))
ENABLE_HTTP_FAST_PATH = os.getenv("ENABLE_HTTP_FAST_PATH", "1") == "1"
HTTP_LOGIN_RETRIES = int(os.getenv("HTTP_LOGIN_RETRIES", "5"))

STATUS_SUCCESS = "success"
STATUS_INVALID_CAPTCHA = "invalid_captcha"
STATUS_INVALID_CREDENTIALS = "invalid_credentials"
STATUS_NAVIGATION_FAILED = "navigation_failed"
STATUS_UNKNOWN_ERROR = "unknown_error"
_GO_SCRAPER_SCRIPT = os.path.join(os.path.dirname(__file__), "fast_scraper_go", "main.go")
_GO_SCRAPER_BINARY_WIN = os.path.join(os.path.dirname(__file__), "fast_scraper_go", "fast_scraper_go.exe")
_GO_SCRAPER_BINARY_LINUX = os.path.join(os.path.dirname(__file__), "fast_scraper_go", "fast_scraper_go")
_GO_SCRAPER_CACHE_DIR = os.path.join(os.path.dirname(__file__), "fast_scraper_go")
_GO_SCRAPER_CACHE_KEY_LOCK = threading.Lock()
_GO_SCRAPER_CACHE: dict[str, str] = {}

KNOWN_SUBJECT_NAMES: dict[str, str] = {
    "MEICC405": "Control Systems",
    "MEMEC401": "Theory of Machines",
    "MEMEC402": "Manufacturing Processes II",
    "MEMEC403": "Thermal Engineering II",
    "MEMEC404": "Metrology and Statistical Quality Control",
    "VAPD0101": "Sports-I",
}


@dataclass
class _RegisteredCourse:
    code: str
    name: str = ""
    section: str = ""
    group: str = ""
    batch: str = ""

_last_login_diagnostic = ""
_last_datewise_timeline: dict[str, list[dict[str, str]]] = {}
_last_selected_filters: dict[str, str | None] = {"year": None, "semester": None}
_chromedriver_path: str | None = None
_chromedriver_lock = threading.Lock()
_warm_sessions_lock = threading.Lock()


@dataclass
class _WarmBrowserSession:
    key: str
    user_id: str
    driver: webdriver.Chrome
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)


_warm_sessions: dict[str, _WarmBrowserSession] = {}


def _set_login_diagnostic(message: str) -> None:
    global _last_login_diagnostic
    _last_login_diagnostic = message


def get_last_login_diagnostic() -> str:
    """Return last login diagnostic text for CLI/debug output."""
    return _last_login_diagnostic


def get_last_datewise_timeline() -> dict[str, list[dict[str, str]]]:
    """Return last scraped date-wise attendance timeline."""
    return _last_datewise_timeline


def get_last_selected_filters() -> dict[str, str | None]:
    """Return the last detected or selected year/semester used during scraping."""
    return dict(_last_selected_filters)


def _set_selected_filters(year: str | None = None, semester: str | None = None) -> None:
    global _last_selected_filters
    _last_selected_filters = {
        "year": year or None,
        "semester": semester or None,
    }


def warmup_scraper_runtime() -> None:
    """Pre-resolve local ChromeDriver so the first live fetch starts faster."""
    try:
        _get_driver_service()
        logger.info("Scraper runtime warmed up")
    except Exception:
        logger.debug("Scraper warmup failed", exc_info=True)


def _close_driver_safely(driver: webdriver.Chrome | None) -> None:
    if driver is None:
        return
    try:
        driver.quit()
    except Exception:
        logger.debug("Could not close Chrome driver cleanly", exc_info=True)


def _make_warm_session_key(user_id: str, password: str) -> str:
    raw = f"{user_id}\0{password}".encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()


def _is_driver_alive(driver: webdriver.Chrome | None) -> bool:
    if driver is None:
        return False
    try:
        driver.current_url
        return True
    except Exception:
        return False


def _prune_warm_sessions() -> None:
    if not ENABLE_WARM_SESSIONS:
        return

    now = time.time()
    to_close: list[webdriver.Chrome] = []
    with _warm_sessions_lock:
        stale_keys = [
            key
            for key, session in _warm_sessions.items()
            if not session.lock.locked() and (now - session.last_used) > WARM_SESSION_TTL_SECONDS
        ]
        for key in stale_keys:
            session = _warm_sessions.pop(key, None)
            if session:
                to_close.append(session.driver)

        if len(_warm_sessions) > WARM_SESSION_MAX_POOL:
            overflow = len(_warm_sessions) - WARM_SESSION_MAX_POOL
            candidates = sorted(
                (
                    (key, session)
                    for key, session in _warm_sessions.items()
                    if not session.lock.locked()
                ),
                key=lambda item: item[1].last_used,
            )
            for key, session in candidates[:overflow]:
                current = _warm_sessions.pop(key, None)
                if current:
                    to_close.append(current.driver)

    for driver in to_close:
        _close_driver_safely(driver)


def _acquire_warm_session(user_id: str, password: str) -> _WarmBrowserSession | None:
    if not ENABLE_WARM_SESSIONS:
        return None

    _prune_warm_sessions()
    key = _make_warm_session_key(user_id, password)
    with _warm_sessions_lock:
        session = _warm_sessions.get(key)

    if not session:
        return None
    if not session.lock.acquire(blocking=False):
        logger.info("Warm session for user %s is busy; using a fresh browser", user_id)
        return None

    if not _is_driver_alive(session.driver):
        logger.info("Warm session for user %s is no longer alive; discarding it", user_id)
        with _warm_sessions_lock:
            if _warm_sessions.get(key) is session:
                _warm_sessions.pop(key, None)
        _close_driver_safely(session.driver)
        session.lock.release()
        return None

    session.last_used = time.time()
    logger.info("Reusing warm session for user %s", user_id)
    return session


def _store_warm_session(user_id: str, password: str, driver: webdriver.Chrome) -> bool:
    if not ENABLE_WARM_SESSIONS:
        return False

    key = _make_warm_session_key(user_id, password)
    session = _WarmBrowserSession(key=key, user_id=user_id, driver=driver, last_used=time.time())
    to_close: list[webdriver.Chrome] = []

    with _warm_sessions_lock:
        existing = _warm_sessions.get(key)
        if existing and existing.lock.locked():
            logger.info("Keeping existing in-use warm session for user %s; not replacing it", user_id)
            return False

        if existing:
            _warm_sessions.pop(key, None)
            to_close.append(existing.driver)

        _warm_sessions[key] = session

        if len(_warm_sessions) > WARM_SESSION_MAX_POOL:
            overflow = len(_warm_sessions) - WARM_SESSION_MAX_POOL
            candidates = sorted(
                (
                    (other_key, other_session)
                    for other_key, other_session in _warm_sessions.items()
                    if other_key != key and not other_session.lock.locked()
                ),
                key=lambda item: item[1].last_used,
            )
            for other_key, other_session in candidates[:overflow]:
                current = _warm_sessions.pop(other_key, None)
                if current:
                    to_close.append(current.driver)

    for old_driver in to_close:
        _close_driver_safely(old_driver)

    logger.info("Stored warm session for user %s", user_id)
    return True


def _discard_warm_session(session: _WarmBrowserSession | None) -> None:
    if session is None:
        return
    with _warm_sessions_lock:
        if _warm_sessions.get(session.key) is session:
            _warm_sessions.pop(session.key, None)
    _close_driver_safely(session.driver)


# ---------------------------------------------------------------------------
# Driver setup
# ---------------------------------------------------------------------------

def _create_driver() -> webdriver.Chrome:
    """Create Chrome WebDriver with fallback option profiles for stability."""
    headless_mode = os.getenv("SELENIUM_HEADLESS", "1") == "1"
    service = _get_driver_service()

    def build_options(profile: str) -> ChromeOptions:
        opts = ChromeOptions()
        opts.page_load_strategy = os.getenv("PAGE_LOAD_STRATEGY", "eager")
        if headless_mode:
            # new headless is more stable with recent Chrome builds.
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--disable-extensions")
        opts.add_argument("--disable-infobars")
        opts.add_argument("--no-first-run")
        opts.add_argument("--disable-default-apps")
        opts.add_argument("--disable-background-networking")
        opts.add_argument("--disable-background-timer-throttling")
        opts.add_argument("--disable-renderer-backgrounding")
        opts.add_argument("--disable-component-update")
        opts.add_argument("--disable-sync")
        opts.add_argument("--metrics-recording-only")
        opts.add_argument("--mute-audio")
        opts.add_argument("--hide-scrollbars")
        opts.add_experimental_option(
            "prefs",
            {
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False,
                "profile.default_content_setting_values.notifications": 2,
            },
        )

        if profile == "stealth":
            opts.add_argument("--disable-blink-features=AutomationControlled")
            opts.add_experimental_option("excludeSwitches", ["enable-automation"])
            opts.add_experimental_option("useAutomationExtension", False)
        elif profile == "minimal":
            # Minimal profile for environments where Chrome exits early.
            pass
        return opts

    last_exc: Exception | None = None
    for profile in ["stealth", "minimal"]:
        try:
            options = build_options(profile)
            driver = webdriver.Chrome(service=service, options=options)
            try:
                driver.execute_cdp_cmd(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"},
                )
            except Exception:
                logger.debug("Could not apply webdriver stealth script", exc_info=True)
            driver.set_page_load_timeout(60)
            logger.info("Chrome driver created with profile: %s", profile)
            return driver
        except (SessionNotCreatedException, WebDriverException) as exc:
            last_exc = exc
            logger.warning("Chrome start failed with profile '%s': %s", profile, exc)
            continue

    if last_exc:
        raise last_exc
    raise RuntimeError("Failed to create Chrome driver")


def _get_driver_service() -> Service:
    """Resolve the ChromeDriver path once per process for faster retries."""
    configured_path = os.getenv("CHROMEDRIVER_PATH", "").strip()
    if configured_path:
        return Service(configured_path)

    global _chromedriver_path
    if _chromedriver_path:
        return Service(_chromedriver_path)

    with _chromedriver_lock:
        if not _chromedriver_path:
            _chromedriver_path = ChromeDriverManager().install()
    return Service(_chromedriver_path)


def _wait_for_document_ready(driver: webdriver.Chrome, timeout: float | None = None) -> bool:
    """Wait until DOM is interactive/complete, returning early when page is ready."""
    deadline = time.time() + float(timeout if timeout is not None else WAIT_TIMEOUT)
    interval = 0.05 if FAST_MODE else 0.1
    while time.time() < deadline:
        try:
            state = str(driver.execute_script("return document.readyState") or "").lower()
            if state in {"interactive", "complete"}:
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _report_progress(
    progress_callback: Callable[[int, str], None] | None,
    percent: int,
    stage: str,
) -> None:
    if progress_callback is None:
        return
    try:
        progress_callback(max(0, min(100, int(percent))), stage)
    except Exception:
        logger.debug("Progress callback raised unexpectedly", exc_info=True)


def _settle_after_action(driver: webdriver.Chrome, delay: float) -> None:
    """
    Keep a short post-click pause for frame-heavy IMS pages, then return early
    when the DOM is already ready instead of always paying the full delay.
    """
    target = max(0.0, float(delay))
    if target:
        time.sleep(min(target, 0.22 if FAST_MODE else 0.35))
    _wait_for_document_ready(driver, target + 0.4)


def _requests_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/146.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }


def _extract_selected_option_value(html: str, select_name: str) -> str | None:
    pattern = (
        rf"<select[^>]+(?:name|id)=['\"]{re.escape(select_name)}['\"][^>]*>(.*?)</select>"
    )
    match = re.search(pattern, html, flags=re.I | re.S)
    if not match:
        return None
    select_html = match.group(1)
    selected = re.search(
        r"<option[^>]*value=['\"]([^'\"]+)['\"][^>]*selected",
        select_html,
        flags=re.I | re.S,
    )
    if selected:
        return (selected.group(1) or "").strip() or None
    first = re.search(
        r"<option[^>]*value=['\"]([^'\"]+)['\"]",
        select_html,
        flags=re.I | re.S,
    )
    if first:
        return (first.group(1) or "").strip() or None
    return None


def _looks_like_time_label(text: str) -> bool:
    t = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not t:
        return False
    if re.search(r"\b\d{1,2}(:\d{2})?\s*(am|pm)?\s*[-–to]+\s*\d{1,2}(:\d{2})?\s*(am|pm)?\b", t):
        return True
    if re.search(r"\bperiod\s*\d+\b", t):
        return True
    return False


def _to_12h(hour: int, minute: int) -> tuple[int, str]:
    suffix = "am" if hour < 12 else "pm"
    h12 = hour % 12
    if h12 == 0:
        h12 = 12
    return h12, suffix


def _format_slot_time(raw: str) -> str:
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text:
        return ""

    m = re.search(
        r"(?P<h1>\d{1,2})(?::(?P<m1>\d{2}))?\s*(?P<a1>am|pm)?\s*[-–to]+\s*(?P<h2>\d{1,2})(?::(?P<m2>\d{2}))?\s*(?P<a2>am|pm)?",
        text,
        flags=re.I,
    )
    if not m:
        return text

    h1 = int(m.group("h1"))
    h2 = int(m.group("h2"))
    m1 = int(m.group("m1") or 0)
    m2 = int(m.group("m2") or 0)
    a1 = (m.group("a1") or "").lower()
    a2 = (m.group("a2") or "").lower()

    if not a1 and not a2:
        # Assume 24h format when no AM/PM is present.
        if h1 <= 7 and h2 <= 8:
            h1 += 12
            h2 += 12
        elif h2 <= h1 and 7 <= h1 <= 12 and h2 <= 7:
            # Common timetable shorthand: "12:00-1:00" means 12pm-1pm.
            h2 += 12
        start_h, start_s = _to_12h(h1, m1)
        end_h, end_s = _to_12h(h2, m2)
    else:
        def to24(h: int, mer: str) -> int:
            hh = h % 12
            if mer == "pm":
                hh += 12
            return hh

        end_suffix = a2 or a1 or "am"
        start_suffix = a1 or end_suffix
        start_h24 = to24(h1, start_suffix)
        end_h24 = to24(h2, end_suffix)
        # If one side omits AM/PM and appears to move backward, infer same half-day forward.
        if not a2 and end_h24 <= start_h24:
            end_h24 += 12
        if not a1 and start_h24 > end_h24:
            start_h24 = max(0, start_h24 - 12)
        start_h, start_s = _to_12h(start_h24, m1)
        end_h, end_s = _to_12h(end_h24, m2)

    if start_s == end_s and m1 == 0 and m2 == 0:
        return f"{start_h}-{end_h}{end_s}"
    if start_s == end_s:
        return f"{start_h}:{m1:02d}-{end_h}:{m2:02d}{end_s}"
    return f"{start_h}:{m1:02d}{start_s}-{end_h}:{m2:02d}{end_s}"


def _normalize_subject_from_slot(raw: str) -> str:
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text:
        return ""

    is_lab = bool(re.search(r"\bgrp\s*[- ]?\s*[12]\b", text, flags=re.I))
    text = re.sub(r"\bgrp\s*[- ]?\s*[12]\b", "", text, flags=re.I)

    code_match = re.search(r"\b([A-Z]{2,}[A-Z0-9]*\d{2,})\b", text)
    subject_code = (code_match.group(1) if code_match else "").upper()
    if subject_code:
        text = re.sub(rf"\b{re.escape(subject_code)}\b", "", text, flags=re.I)

    text = re.sub(r"^[\s:\-–|/]+", "", text)
    text = re.sub(r"\([^)]*\)", "", text).strip()

    # If we still have a code-like prefix with '-', strip it.
    text = re.sub(r"^[A-Z]{2,}[A-Z0-9]*\d{2,}\s*[-:]+\s*", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" -:;,|")

    if not text and subject_code:
        text = KNOWN_SUBJECT_NAMES.get(subject_code, subject_code)

    if subject_code and subject_code in KNOWN_SUBJECT_NAMES:
        # Always prefer canonical known name over noisy portal labels.
        text = KNOWN_SUBJECT_NAMES[subject_code]

    if not subject_code and re.search(r"\bsprt|sport\b", text, flags=re.I):
        text = "Sports-I"

    if is_lab and text and not text.lower().endswith("lab"):
        text = f"{text} Lab"

    return text.strip()


def _today_tokens() -> set[str]:
    now = datetime.now()
    full = now.strftime("%A").lower()
    short = now.strftime("%a").lower()
    alias = {
        "monday": {"mon", "monday"},
        "tuesday": {"tue", "tues", "tuesday"},
        "wednesday": {"wed", "wednesday"},
        "thursday": {"thu", "thur", "thurs", "thursday"},
        "friday": {"fri", "friday"},
        "saturday": {"sat", "saturday"},
        "sunday": {"sun", "sunday"},
    }
    return set(alias.get(full, {full, short}))


def _is_today_label(label: str) -> bool:
    clean = re.sub(r"[^a-z]", "", (label or "").lower())
    if not clean:
        return False
    return any(clean.startswith(token) for token in _today_tokens())


def _normalize_group_label(value: str) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    m = re.search(r"grp\s*[- ]?\s*(\d+)", raw)
    if m:
        return f"grp-{m.group(1)}"
    return re.sub(r"\s+", "", raw)


def _parse_registered_courses_html(html: str) -> dict[str, _RegisteredCourse]:
    out: dict[str, _RegisteredCourse] = {}
    if not html:
        return out

    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        header_row_index = -1
        header: list[str] = []
        for i, row in enumerate(rows[:6]):
            cells = [
                re.sub(r"\s+", " ", c.get_text(" ", strip=True)).strip().lower()
                for c in row.find_all(["th", "td"])
            ]
            if not cells:
                continue
            if any("subject" in c and "code" in c for c in cells) and any("group" in c for c in cells):
                header_row_index = i
                header = cells
                break
        if header_row_index < 0 or len(header) < 4:
            continue

        def _find_col(*keys: str) -> int:
            for i, h in enumerate(header):
                if all(k in h for k in keys):
                    return i
            return -1

        code_i = _find_col("subject", "code")
        name_i = _find_col("subject", "name")
        section_i = _find_col("section")
        group_i = _find_col("group")
        if code_i < 0 or group_i < 0:
            continue

        for row in rows[header_row_index + 1:]:
            cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)).strip() for c in row.find_all(["th", "td"])]
            max_idx = max(code_i, group_i, section_i if section_i >= 0 else 0, name_i if name_i >= 0 else 0)
            if len(cells) <= max_idx:
                continue

            code = (cells[code_i] or "").strip().upper()
            if not re.match(r"^[A-Z]{2,}[A-Z0-9]*\d{2,}$", code):
                continue

            name = (cells[name_i] if name_i >= 0 else "").strip()
            section_raw = (cells[section_i] if section_i >= 0 else "").strip()
            group_raw = (cells[group_i] if group_i >= 0 else "").strip()

            batch = ""
            # Example: section/batch can be "2/19" where 19 is practical/sports batch.
            sec_match = re.match(r"\s*(\d+)\s*/\s*(\d+)\s*$", section_raw)
            if sec_match:
                section = sec_match.group(1)
                batch = sec_match.group(2)
            else:
                section_num = re.search(r"\d+", section_raw)
                section = section_num.group(0) if section_num else ""

            group = _normalize_group_label(group_raw)

            out[code] = _RegisteredCourse(
                code=code,
                name=name,
                section=section,
                group=group,
                batch=batch,
            )

    return out


def _find_registered_courses_link(menu_html: str, base: str) -> str | None:
    if not menu_html:
        return None
    soup = BeautifulSoup(menu_html, "html.parser")
    best: tuple[int, str] | None = None
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip().lower()
        score = 0
        if "current" in text and "sem" in text and "course" in text and "registered" in text:
            score += 8
        elif "registered courses" in text:
            score += 5
        if score <= 0:
            continue
        cand = (score, urljoin(base, href))
        if best is None or cand[0] > best[0]:
            best = cand
    return best[1] if best else None


def _extract_registered_courses_from_menu(
    session: requests.Session,
    menu_html: str,
    *,
    base: str,
    referer: str,
) -> dict[str, _RegisteredCourse]:
    url = _find_registered_courses_link(menu_html, base)
    if not url:
        return {}
    try:
        resp = session.get(url, headers={"Referer": referer}, timeout=20)
        return _parse_registered_courses_html(resp.text or "")
    except Exception:
        logger.debug("Could not fetch/parse registered courses page", exc_info=True)
        return {}


def _pick_subject_for_slot(
    raw_cell: str,
    registered: dict[str, _RegisteredCourse],
) -> str:
    raw = re.sub(r"\s+", " ", (raw_cell or "").strip())
    if not raw:
        return ""

    raw_l = raw.lower()
    if raw_l in {"-", "--", "na", "n/a", "off", "holiday", "break", "lunch", "free"}:
        return ""

    code_matches = re.findall(r"\b([A-Z]{2,}[A-Z0-9]*\d{2,})\b", raw)
    unique_codes = [c for c in dict.fromkeys(code_matches) if c in registered]
    if not unique_codes:
        return _normalize_subject_from_slot(raw)

    # Build per-code text windows so markers from one course do not leak into another.
    code_windows: dict[str, str] = {}
    spans = list(re.finditer(r"\b([A-Z]{2,}[A-Z0-9]*\d{2,})\b", raw))
    for i, m in enumerate(spans):
        code = (m.group(1) or "").upper()
        if code not in registered:
            continue
        start = m.start()
        end = spans[i + 1].start() if i + 1 < len(spans) else len(raw)
        piece = raw[start:end].strip()
        if not piece:
            continue
        if code in code_windows:
            code_windows[code] = f"{code_windows[code]} {piece}".strip()
        else:
            code_windows[code] = piece

    disqualified: set[str] = set()

    # Prefer exact group/section/batch matching for the student's registered course variant.
    for code in unique_codes:
        reg = registered.get(code)
        if not reg:
            continue
        slot_text = code_windows.get(code, raw)

        # Detect explicit markers present for this specific code inside the same slot text.
        has_batch_marker_for_code = bool(
            re.search(rf"{re.escape(code)}[^\n]*?bat\s*[:\- ]\s*\d+", slot_text, flags=re.I)
        )
        has_group_marker_for_code = bool(
            re.search(rf"{re.escape(code)}[^\n]*?grp\s*[- ]?\s*\d+", slot_text, flags=re.I)
        )

        if reg.batch:
            bat_hits = re.findall(rf"{re.escape(code)}[^\n]*?bat\s*[:\- ]\s*(\d+)", slot_text, flags=re.I)
            if bat_hits:
                if reg.batch in {b.strip() for b in bat_hits if b.strip()}:
                    return KNOWN_SUBJECT_NAMES.get(code, reg.name or code)
                # Batch markers exist but student's batch is not present -> reject this code.
                disqualified.add(code)
                continue

        if reg.group:
            if re.search(rf"{re.escape(code)}[^\n]*?grp\s*[- ]?\s*{re.escape(reg.group.split('-')[-1])}\b", slot_text, flags=re.I):
                subject = KNOWN_SUBJECT_NAMES.get(code, reg.name or code)
                if re.search(r"grp\s*[- ]?\s*\d+", slot_text, flags=re.I):
                    return f"{subject} Lab"
                return subject
            if has_group_marker_for_code:
                disqualified.add(code)
                continue

        if reg.section:
            # If slot explicitly carries a group/batch marker for this code and it didn't match above,
            # do not fall back to section-only matching (this caused GP-1 to leak into GP-2 output).
            if has_group_marker_for_code or has_batch_marker_for_code:
                continue
            if re.search(rf"{re.escape(code)}[^\n]*?sec\s*[:\- ]\s*{re.escape(reg.section)}\b", slot_text, flags=re.I):
                return KNOWN_SUBJECT_NAMES.get(code, reg.name or code)

    unique_codes = [c for c in unique_codes if c not in disqualified]
    if not unique_codes:
        return ""

    # If no strict match, prefer a single registered code in slot.
    if len(unique_codes) == 1:
        code = unique_codes[0]
        reg = registered.get(code)
        return KNOWN_SUBJECT_NAMES.get(code, (reg.name if reg else "") or code)

    # Final fallback: normalize first matching registered code.
    code = unique_codes[0]
    reg = registered.get(code)
    return KNOWN_SUBJECT_NAMES.get(code, (reg.name if reg else "") or code)


def _extract_today_timetable_from_matrix_table(
    table,
    registered: dict[str, _RegisteredCourse] | None = None,
) -> list[dict[str, str]]:
    rows = table.find_all("tr")
    if len(rows) < 2:
        return []

    header_index = -1
    slot_times: list[str] = []
    for idx, row in enumerate(rows[:8]):
        cells = row.find_all(["th", "td"])
        texts = [c.get_text(" ", strip=True) for c in cells]
        if len(texts) < 2:
            continue
        candidate_times = [
            "" if i == 0 or not _looks_like_time_label(t) else _format_slot_time(t)
            for i, t in enumerate(texts)
        ]
        if sum(1 for t in candidate_times if t) >= 1:
            header_index = idx
            slot_times = candidate_times
            break

    if header_index < 0 or not slot_times:
        return []

    for row in rows[header_index + 1:]:
        cells = row.find_all(["th", "td"])
        texts = [c.get_text(" ", strip=True) for c in cells]
        if not texts:
            continue

        day_label = texts[0]
        if not _is_today_label(day_label):
            continue

        slots: list[dict[str, str]] = []
        limit = min(len(texts), len(slot_times))
        for col_idx in range(1, limit):
            time_label = slot_times[col_idx]
            if not time_label:
                continue
            raw_subject = (texts[col_idx] or "").strip()
            if not raw_subject:
                continue
            low = raw_subject.lower()
            if low in {"-", "--", "na", "n/a", "off", "holiday", "break", "lunch", "free"}:
                continue
            subject = _pick_subject_for_slot(raw_subject, registered or {})
            if not subject:
                continue
            slots.append({"time": time_label, "subject": subject})
        if slots:
            return slots
    return []


def _extract_today_timetable_from_rowwise_table(
    table,
    registered: dict[str, _RegisteredCourse] | None = None,
) -> list[dict[str, str]]:
    rows = table.find_all("tr")
    if len(rows) < 2:
        return []

    headers = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["th", "td"])]
    if len(headers) < 3:
        return []

    day_idx = -1
    time_idx = -1
    subj_idx = -1
    for i, h in enumerate(headers):
        if day_idx < 0 and ("day" in h or "weekday" in h):
            day_idx = i
        if time_idx < 0 and ("time" in h or "slot" in h or "period" in h):
            time_idx = i
        if subj_idx < 0 and ("subject" in h or "course" in h or "paper" in h):
            subj_idx = i

    if time_idx < 0 or subj_idx < 0:
        return []

    slots: list[dict[str, str]] = []
    for row in rows[1:]:
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["th", "td"])]
        if len(cells) <= max(time_idx, subj_idx, day_idx if day_idx >= 0 else 0):
            continue
        if day_idx >= 0 and not _is_today_label(cells[day_idx]):
            continue
        time_label = _format_slot_time(cells[time_idx])
        if not time_label:
            continue
        subject = _pick_subject_for_slot(cells[subj_idx], registered or {})
        if not subject:
            continue
        slots.append({"time": time_label, "subject": subject})

    return slots


def _parse_today_timetable_html(
    html: str,
    registered: dict[str, _RegisteredCourse] | None = None,
) -> list[dict[str, str]]:
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        matrix = _extract_today_timetable_from_matrix_table(table, registered=registered)
        if matrix:
            return matrix
    for table in soup.find_all("table"):
        rowwise = _extract_today_timetable_from_rowwise_table(table, registered=registered)
        if rowwise:
            return rowwise
    return []


def _login_and_fetch_today_timetable_via_requests(
    user_id: str,
    password: str,
    progress_callback: Callable[[int, str], None] | None = None,
) -> tuple[list[dict[str, str]], str]:
    if not ENABLE_HTTP_FAST_PATH:
        return [], STATUS_UNKNOWN_ERROR

    base = "https://www.imsnsit.org/imsnsit/"

    def _find_timetable_link(menu_html: str) -> str | None:
        if not menu_html:
            return None

        soup = BeautifulSoup(menu_html, "html.parser")
        candidates: list[tuple[int, str]] = []

        for a in soup.find_all("a"):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            text = re.sub(r"\s+", " ", a.get_text(" ", strip=True) or "").strip().lower()
            href_l = href.lower()

            score = 0
            if "time table" in text or "timetable" in text:
                score += 6
            if "my" in text:
                score += 1
            if "time" in href_l and "table" in href_l:
                score += 4
            if "timetable" in href_l:
                score += 3
            if "plum_url.php" in href_l:
                score += 1

            if score > 0:
                candidates.append((score, urljoin(base, href)))

        if not candidates:
            # Fallback regex for pages with odd HTML serialization.
            match = re.search(
                r"href=['\"]([^'\"]+)['\"][^>]*>\s*(?:my\s*)?time\s*table\s*<",
                menu_html,
                flags=re.I,
            )
            if match:
                return urljoin(base, match.group(1))
            return None

        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    for attempt in range(1, HTTP_LOGIN_RETRIES + 1):
        try:
            _report_progress(progress_callback, 8, f"Opening IMS session for timetable ({attempt}/{HTTP_LOGIN_RETRIES})")
            session = requests.Session()
            session.headers.update(_requests_headers())

            session.get(base, timeout=20)
            session.get(urljoin(base, "plum5_fw_login.php?t=sw&w=1"), timeout=20)
            session.get(urljoin(base, "student.htm"), headers={"Referer": base}, timeout=20)
            session.get(
                urljoin(base, "student_login110.php"),
                headers={"Referer": urljoin(base, "student.htm")},
                timeout=20,
            )

            login_page = session.get(
                urljoin(base, "student_login.php"),
                headers={"Referer": urljoin(base, "student.htm"), "Upgrade-Insecure-Requests": "1"},
                timeout=20,
            )
            login_html = login_page.text or ""
            if not login_html:
                continue

            fy_match = re.search(r"name='fy' id='fy' value='([^']+)'", login_html)
            comp_match = re.search(r"name='comp' id='comp' type='hidden' readonly value='([^']+)'", login_html)
            hrand_match = re.search(r"name='HRAND_NUM' id='HRAND_NUM' value='([^']+)'", login_html)
            capsrc_match = re.search(r"<img src='([^']+captcha[^']+)' id='captchaimg'", login_html)
            if not all([fy_match, comp_match, hrand_match, capsrc_match]):
                continue

            _report_progress(progress_callback, 22, "Solving IMS security number")
            captcha_response = session.get(
                urljoin(base, capsrc_match.group(1)),
                headers={"Referer": urljoin(base, "student_login.php")},
                timeout=20,
            )
            captcha_text, _ = solve_captcha_with_debug(captcha_response.content)
            if not captcha_text or len(captcha_text) < 4:
                continue

            login_payload = {
                "f": "",
                "uid": user_id,
                "pwd": password,
                "HRAND_NUM": hrand_match.group(1),
                "fy": fy_match.group(1),
                "comp": comp_match.group(1),
                "cap": captcha_text,
                "logintype": "student",
            }
            _report_progress(progress_callback, 38, "Signing in for timetable")
            login_response = session.post(
                urljoin(base, "student_login.php"),
                data=login_payload,
                headers={
                    "Referer": urljoin(base, "student_login.php"),
                    "Origin": "https://www.imsnsit.org",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Upgrade-Insecure-Requests": "1",
                },
                timeout=20,
            )
            banner_html = login_response.text or ""
            banner_lower = banner_html.lower()
            if "invalid security" in banner_lower or "please login" in banner_lower:
                continue
            if "logout" not in banner_lower or "my activities" not in banner_lower:
                continue

            _report_progress(progress_callback, 55, "Opening My Activities")
            my_activities_match = re.search(
                r"href='(https://www\.imsnsit\.org/imsnsit/plum_url\.php\?[^']+)'[^>]*>My Activities<",
                banner_html,
                flags=re.I,
            )
            if not my_activities_match:
                return [], STATUS_NAVIGATION_FAILED

            my_activities_url = my_activities_match.group(1)
            menu_page = session.get(
                my_activities_url,
                headers={"Referer": urljoin(base, "student_login.php")},
                timeout=20,
            )
            menu_html = menu_page.text or ""
            registered = _extract_registered_courses_from_menu(
                session,
                menu_html,
                base=base,
                referer=my_activities_url,
            )
            timetable_url = _find_timetable_link(menu_html)
            if not timetable_url:
                logger.info("HTTP timetable fast path could not find timetable link in My Activities menu")
                return [], STATUS_NAVIGATION_FAILED

            _report_progress(progress_callback, 82, "Reading today's timetable")
            timetable_page = session.get(
                timetable_url,
                headers={"Referer": my_activities_url},
                timeout=20,
            )
            timetable_html = timetable_page.text or ""
            if not timetable_html:
                return [], STATUS_NAVIGATION_FAILED

            slots = _parse_today_timetable_html(timetable_html, registered=registered)
            _report_progress(progress_callback, 96, "Preparing timetable reply")
            return slots, STATUS_SUCCESS

        except requests.RequestException as exc:
            logger.info("HTTP timetable fast path network issue on attempt %d: %s", attempt, exc)
            continue
        except Exception:
            logger.exception("HTTP timetable fast path failed unexpectedly on attempt %d", attempt)
            continue

    return [], STATUS_UNKNOWN_ERROR


def _parse_attendance_from_sources(
    sources: list[str],
    include_timeline: bool = True,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, str]]]]:
    def _extract_subject_name_map(src: str) -> dict[str, str]:
        mapping: dict[str, str] = {}
        legend_boundary = r"(?:CR|CS|GH|MB|MS|NA|NT|OD|TL)\s*-"
        pair_pattern = re.compile(
            r"\b([A-Z]{2,}[A-Z0-9]*\d{2,})\s*-\s*(.+?)(?=\s+[A-Z]{2,}[A-Z0-9]*\d{2,}\s*-|\s+"
            + legend_boundary
            + r"|$)"
        )

        soup = BeautifulSoup(src, "html.parser")
        for cell in soup.find_all(["td", "th", "div", "span"]):
            text = cell.get_text(" ", strip=True)
            if "-" not in text or not re.search(r"\d", text):
                continue
            for match in pair_pattern.finditer(text):
                code = (match.group(1) or "").strip().upper()
                name = re.sub(r"\s+", " ", match.group(2) or "").strip(" -:;,")
                if (
                    code
                    and name
                    and "--->" not in name
                    and not name.startswith(">")
                    and re.search(r"[A-Za-z]{3}", name)
                ):
                    mapping.setdefault(code, name)
        return mapping

    def _attach_subject_names(
        parsed: dict[str, dict[str, int]],
        names: dict[str, str],
    ) -> dict[str, dict[str, Any]]:
        enriched: dict[str, dict[str, Any]] = {}
        for code, stats in parsed.items():
            row: dict[str, Any] = dict(stats)
            name = names.get(code.upper())
            if name:
                row["name"] = name
            enriched[code] = row
        return enriched

    subject_names: dict[str, str] = {}
    best_overall: dict[str, dict[str, int]] = {}
    best_overall_table = None
    best_overall_score: tuple[int, int] = (0, 0)
    best: dict[str, dict[str, int]] = {}
    best_table = None
    best_score: tuple[int, int, int] = (0, 0, 0)
    merged_timeline: dict[str, list[dict[str, str]]] = {}

    for src in sources:
        subject_names.update(_extract_subject_name_map(src))
        soup = BeautifulSoup(src, "html.parser")
        for table in soup.find_all("table"):
            parsed = _try_parse_table(table)
            if not parsed:
                continue

            table_text = table.get_text(" ", strip=True)
            table_text_l = table_text.lower()
            has_overall_rows = (
                "overall class" in table_text_l
                and "overall present" in table_text_l
                and "overall absent" in table_text_l
            )

            subj_count = len(parsed)
            total_sum = sum(v.get("total", 0) for v in parsed.values())
            binary_marks = len(re.findall(r"(?<!\d)[01](?!\d)", table_text))
            score = (subj_count, binary_marks, total_sum)

            if include_timeline:
                candidate_timeline = _extract_datewise_timeline(table)
                if candidate_timeline:
                    for subject_code, entries in candidate_timeline.items():
                        merged_timeline.setdefault(subject_code, []).extend(entries)

            if has_overall_rows:
                overall_score = (subj_count, total_sum)
                if overall_score > best_overall_score:
                    best_overall = parsed
                    best_overall_table = table
                    best_overall_score = overall_score
                if not include_timeline and subj_count >= 2 and total_sum >= subj_count:
                    return _attach_subject_names(parsed, subject_names), {}

            if score > best_score:
                best = parsed
                best_table = table
                best_score = score

    if best_overall:
        tl = merged_timeline
        if not tl and include_timeline and best_overall_table is not None:
            tl = _extract_datewise_timeline(best_overall_table)
        return _attach_subject_names(best_overall, subject_names), tl

    tl = merged_timeline
    if not tl and include_timeline and best_table is not None:
        tl = _extract_datewise_timeline(best_table)
    return _attach_subject_names(best, subject_names), tl


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def _solve_and_login(
    driver: webdriver.Chrome,
    user_id: str,
    password: str,
    attempt_no: int = 1,
    progress_callback: Callable[[int, str], None] | None = None,
) -> tuple[bool, str]:
    """
    Attempt to solve the CAPTCHA, fill the form, and log in.
    Returns True on success, False on failure.
    """
    try:
        _set_login_diagnostic("Starting login flow")
        _report_progress(progress_callback, 15, "Opening IMS portal")

        # Visit the main page first to initialize session/variables
        driver.get("https://www.imsnsit.org/imsnsit/")
        _wait_for_document_ready(driver, PAGE_SETTLE_DELAY + 1.0)
        
        # Try to click the Student Login link to maintain referrer/state
        try:
            link = driver.find_element(By.XPATH, "//a[contains(@href, 'student.htm')]")
            link.click()
        except Exception:
            # Fallback to direct navigation if link isn't found
            driver.get(IMS_LOGIN_URL)
        # --- Handle frames: IMS portal may use frames ---
        _wait_for_document_ready(driver, PAGE_SETTLE_DELAY + 1.0)
        frames = driver.find_elements(By.TAG_NAME, "frame")
        if not frames:
            frames = driver.find_elements(By.TAG_NAME, "iframe")

        # Try to find the login form; it might be in a frame
        login_frame_found = False
        if frames:
            for frame in frames:
                try:
                    driver.switch_to.default_content()
                    driver.switch_to.frame(frame)
                    
                    # Wait up to 5 seconds for form to appear in this frame
                    start_time = time.time()
                    forms = driver.find_elements(By.TAG_NAME, "form")
                    while not forms and (time.time() - start_time < LOGIN_FRAME_WAIT):
                        time.sleep(min(0.25, ACTION_DELAY))
                        forms = driver.find_elements(By.TAG_NAME, "form")
                        
                    if forms:
                        login_frame_found = True
                        break
                except Exception:
                    pass

        if not login_frame_found:
            driver.switch_to.default_content()

        # --- Fill form fields ---
        # Prefer exact IMS selectors first.
        uid_field = None
        pwd_field = None
        cap_field = None
        try:
            uid_field = driver.find_element(By.ID, "uid")
        except Exception:
            try:
                uid_field = driver.find_element(By.NAME, "uid")
            except Exception:
                pass
        try:
            pwd_field = driver.find_element(By.ID, "pwd")
        except Exception:
            try:
                pwd_field = driver.find_element(By.NAME, "pwd")
            except Exception:
                pass
        try:
            cap_field = driver.find_element(By.ID, "cap")
        except Exception:
            try:
                cap_field = driver.find_element(By.NAME, "cap")
            except Exception:
                pass

        # Fallback to generic search when exact selectors are unavailable.
        uid_field = uid_field or _find_input(driver, ["userid", "user_id", "username", "uid", "login", "txtUserId"], input_types=["text"])
        pwd_field = pwd_field or _find_input(driver, ["password", "passwd", "pwd", "txtPassword"], input_types=["password"])
        cap_field = cap_field or _find_input(driver, ["cap", "captcha", "captchaText", "txtCaptcha", "security", "securityCode"], input_types=["text"])

        if not uid_field or not pwd_field:
            logger.error("Could not locate userid or password input fields")
            _set_login_diagnostic("Unable to locate visible user ID and/or password field")
            return False, STATUS_UNKNOWN_ERROR

        # Fill uid/pwd immediately so user can see fields are populated.
        _report_progress(progress_callback, 34, "Filling login details")
        _set_input_value(uid_field, user_id)
        _set_input_value(pwd_field, password)

        # --- Locate Security Number image (prefer exact IMS image id) ---
        captcha_img = None
        try:
            captcha_img = driver.find_element(By.ID, "captchaimg")
        except Exception:
            captcha_img = _find_captcha_image(driver, cap_field)
        captcha_text = ""

        # Keep hidden value only for diagnostics (do not trust it as entered security number).
        hidden_captcha = _read_hidden_captcha_value(driver)

        if captcha_img:
            try:
                _report_progress(progress_callback, 46, "Solving IMS security number")
                captcha_bytes = _extract_captcha_bytes(driver, captcha_img)
                ocr_text, candidates = solve_captcha_with_debug(captcha_bytes)
                captcha_text = ocr_text
                logger.info("Security Number solved: %s", captcha_text)
                _save_captcha_debug_artifacts(
                    driver=driver,
                    attempt_no=attempt_no,
                    captcha_bytes=captcha_bytes,
                    chosen=captcha_text,
                    candidates=candidates,
                )
            except Exception:
                logger.exception("Failed to capture CAPTCHA image")

        # Fallback to hidden value only if OCR could not produce a usable number.
        if (not captcha_text or len(captcha_text) < 4) and hidden_captcha:
            captcha_text = hidden_captcha
            logger.info("Falling back to hidden Security Number value")

        if not captcha_text or len(captcha_text) < 4:
            logger.warning("Could not solve Security Number")
            _set_login_diagnostic("Security Number missing/too short before submit")
            return False, STATUS_INVALID_CAPTCHA

        if cap_field:
            _set_input_value(cap_field, captcha_text)

        entered_security = (cap_field.get_attribute("value") or "").strip() if cap_field else ""
        hidden_security = _read_hidden_captcha_value(driver)
        entered_uid = (uid_field.get_attribute("value") or "").strip()
        entered_pwd_len = len((pwd_field.get_attribute("value") or ""))
        if not entered_uid:
            _set_login_diagnostic("User ID field is empty at submit time")
            return False, STATUS_INVALID_CREDENTIALS
        if entered_pwd_len == 0:
            _set_login_diagnostic("Password field is empty at submit time")
            return False, STATUS_INVALID_CREDENTIALS
        if not entered_security:
            _set_login_diagnostic("Security Number field is empty at submit time")
            return False, STATUS_INVALID_CAPTCHA
        if entered_security != captcha_text:
            _set_login_diagnostic(
                f"Security Number input mismatch at submit time (expected='{captcha_text}', actual='{entered_security}')"
            )
            return False, STATUS_INVALID_CAPTCHA

        # --- Submit ---
        _report_progress(progress_callback, 58, "Submitting login")
        submit_btn = None
        submit_selectors = [
            (By.CSS_SELECTOR, "input[type='submit']"),
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.XPATH, "//input[@value='Login']"),
            (By.XPATH, "//input[@value='LOGIN']"),
            (By.XPATH, "//input[@value='Sign In']"),
            (By.XPATH, "//button[contains(text(),'Login')]"),
        ]
        for sel_by, sel_val in submit_selectors:
            try:
                submit_btn = driver.find_element(sel_by, sel_val)
                if submit_btn:
                    break
            except Exception:
                continue

        submitted = False
        try:
            # Match website's own login path first.
            if driver.execute_script("return typeof Login === 'function'"):
                js_ok = driver.execute_script("return Login();")
                submitted = bool(js_ok)
                if not submitted:
                    _set_login_diagnostic("Portal Login() returned false before submit (client-side validation failed)")
        except Exception:
            logger.debug("Calling Login() via JavaScript failed", exc_info=True)

        if not submitted and submit_btn:
            submit_btn.click()
            submitted = True
        elif not submitted:
            # Try submitting the form directly
            forms = driver.find_elements(By.TAG_NAME, "form")
            if forms:
                forms[0].submit()
                submitted = True

        if not submitted:
            _set_login_diagnostic("Could not submit login form")
            return False, STATUS_UNKNOWN_ERROR

        # --- Check if login succeeded ---
        # Give the portal a moment to return alert/response.
        _wait_for_document_ready(driver, ACTION_DELAY + 1.0)

        # Handle JS alerts first (common path on invalid Security Number).
        try:
            alert = driver.switch_to.alert
            alert_text = (alert.text or "").lower()
            alert.accept()
            if "security" in alert_text or "captcha" in alert_text:
                security_matched_server = bool(hidden_security) and entered_security == hidden_security
                if security_matched_server:
                    logger.warning("Login failed – portal reported Security Number invalid, but submitted value matched server hidden value")
                    _set_login_diagnostic(
                        f"Portal alert '{alert_text}' even though entered_security matches hidden_security ({entered_security}). "
                        "Likely invalid credentials (masked by portal) or bot/session rejection."
                    )
                    return False, STATUS_INVALID_CREDENTIALS

                logger.warning("Login failed – Invalid Security Number (alert)")
                _set_login_diagnostic(
                    f"Portal alert after submit: '{alert_text}'. entered_security='{entered_security}', hidden_security='{hidden_security}', "
                    f"user_id_filled={bool(entered_uid)}, password_length={entered_pwd_len}"
                )
                return False, STATUS_INVALID_CAPTCHA
            if "user" in alert_text or "password" in alert_text or "invalid" in alert_text:
                logger.warning("Login failed – Invalid credentials (alert)")
                _set_login_diagnostic(f"Portal alert indicates credentials issue: '{alert_text}'")
                return False, STATUS_INVALID_CREDENTIALS
            logger.warning("Login failed – alert received: %s", alert_text)
            _set_login_diagnostic(f"Portal returned unknown alert: '{alert_text}'")
            return False, STATUS_UNKNOWN_ERROR
        except NoAlertPresentException:
            pass

        try:
            page_src = driver.page_source.lower()
            if ("invalid" in page_src and "captcha" in page_src) or ("invalid security" in page_src):
                security_matched_server = bool(hidden_security) and entered_security == hidden_security
                if security_matched_server:
                    logger.warning("Login failed – invalid Security Number message but submitted value matched hidden server value")
                    _set_login_diagnostic(
                        f"Page reported invalid Security Number, but entered_security matches hidden_security ({entered_security}). "
                        "Likely invalid credentials (masked by portal) or bot/session rejection."
                    )
                    return False, STATUS_INVALID_CREDENTIALS

                logger.warning("Login failed – likely bad Security Number")
                _set_login_diagnostic("Page content indicates invalid Security Number")
                return False, STATUS_INVALID_CAPTCHA
            if "invalid" in page_src and ("userid" in page_src or "password" in page_src):
                logger.warning("Login failed – invalid credentials")
                _set_login_diagnostic("Page content indicates invalid credentials")
                return False, STATUS_INVALID_CREDENTIALS
            if "invalid user" in page_src or "invalid login" in page_src or "wrong password" in page_src:
                logger.warning("Login failed – invalid credentials")
                _set_login_diagnostic("Page content indicates wrong user ID/password")
                return False, STATUS_INVALID_CREDENTIALS

            if _is_login_page_still_visible(driver):
                _set_login_diagnostic("Still on login page after submit; login not completed")
                return False, STATUS_INVALID_CREDENTIALS
                
            # If we can find a logout link or user dashboard, login worked
            success_indicators = ["logout", "my activities", "welcome", "dashboard"]
            if any(ind in page_src for ind in success_indicators):
                logger.info("Login successful")
                _set_login_diagnostic("Login succeeded and dashboard indicators found")
                _report_progress(progress_callback, 66, "Login successful")
                return True, STATUS_SUCCESS
                
        except UnexpectedAlertPresentException as e:
            alert_text = str(e).lower()
            if "security" in alert_text or "captcha" in alert_text:
                logger.warning("Login failed – Invalid Security Number (alert)")
            else:
                logger.warning(f"Login failed – Invalid credentials or other error (alert: {alert_text})")
            if "user" in alert_text or "password" in alert_text:
                _set_login_diagnostic(f"Unexpected alert indicates invalid credentials: '{alert_text}'")
                return False, STATUS_INVALID_CREDENTIALS
            _set_login_diagnostic(f"Unexpected alert indicates invalid Security Number: '{alert_text}'")
            return False, STATUS_INVALID_CAPTCHA

        # Assume success if no obvious error
        logger.info("Login presumed successful (no error detected)")
        _set_login_diagnostic("No explicit error found after submit; login presumed successful")
        _report_progress(progress_callback, 66, "Login successful")
        return True, STATUS_SUCCESS

    except Exception:
        logger.exception("Exception during login attempt")
        _set_login_diagnostic("Exception occurred during login attempt; see logs/stacktrace")
        return False, STATUS_UNKNOWN_ERROR


def _find_input(driver: webdriver.Chrome, name_hints: list[str], input_types: list[str] | None = None):
    """Find an input element by trying multiple name/id patterns."""
    expected_types = {t.lower() for t in (input_types or [])}

    def _match(el) -> bool:
        try:
            if not el.is_displayed():
                return False
            if not el.is_enabled():
                return False
            if expected_types:
                typ = (el.get_attribute("type") or "").lower()
                if typ not in expected_types:
                    return False
            return True
        except Exception:
            return False

    for hint in name_hints:
        for attr in ["name", "id", "placeholder"]:
            try:
                candidates = driver.find_elements(By.CSS_SELECTOR, f"input[{attr}*='{hint}' i]")
                for el in candidates:
                    if _match(el):
                        return el
            except Exception:
                continue
    # Fallback: try XPath contains
    for hint in name_hints:
        try:
            candidates = driver.find_elements(By.XPATH, f"//input[contains(@name, '{hint}') or contains(@id, '{hint}')]")
            for el in candidates:
                if _match(el):
                    return el
        except Exception:
            continue
    return None


def _is_login_page_still_visible(driver: webdriver.Chrome) -> bool:
    """Return True if login form is still present in current frame/default content."""
    try:
        url = (driver.current_url or "").lower()
        if "student.htm" in url or "student_login.php" in url:
            # URL hint alone is weak; confirm form controls too.
            pass

        # Current context check
        if driver.find_elements(By.ID, "uid") and driver.find_elements(By.ID, "pwd") and driver.find_elements(By.ID, "cap"):
            return True

        # Check default + frames
        driver.switch_to.default_content()
        if driver.find_elements(By.ID, "uid") and driver.find_elements(By.ID, "pwd") and driver.find_elements(By.ID, "cap"):
            return True

        frames = driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
        for frame in frames:
            try:
                driver.switch_to.default_content()
                driver.switch_to.frame(frame)
                if driver.find_elements(By.ID, "uid") and driver.find_elements(By.ID, "pwd") and driver.find_elements(By.ID, "cap"):
                    return True
            except Exception:
                continue
    except Exception:
        return False
    finally:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
    return False


def _set_input_value(element, value: str) -> None:
    """Set input value robustly and trigger events expected by page scripts."""
    try:
        element.click()
    except Exception:
        pass

    try:
        element.clear()
    except Exception:
        pass

    try:
        element.send_keys(value)
    except Exception:
        pass

    current = (element.get_attribute("value") or "").strip()
    if current == value:
        return

    # Fallback: force exact value via JavaScript and dispatch events.
    driver = element.parent
    driver.execute_script(
        """
        const el = arguments[0];
        const val = arguments[1];
        el.value = val;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        """,
        element,
        value,
    )


def _read_hidden_captcha_value(driver: webdriver.Chrome) -> str:
    """Return hidden CAPTCHA value if IMS exposes it in a hidden input."""
    for name in ["HRAND_NUM", "hrand_num", "rand_num", "securitycode"]:
        try:
            el = driver.find_element(By.NAME, name)
            if (el.get_attribute("type") or "").lower() == "hidden":
                val = (el.get_attribute("value") or "").strip()
                if val.isdigit() and len(val) >= 4:
                    return val
        except Exception:
            continue
    return ""


def _find_captcha_image(driver: webdriver.Chrome, cap_field=None):
    """Find CAPTCHA image element using robust selectors and proximity to captcha input."""
    if cap_field is not None:
        nearby_xpaths = [
            "./preceding::img[1]",
            "./following::img[1]",
            "./ancestor::tr[1]//img",
            "./ancestor::table[1]//img[contains(@src,'cap') or contains(@src,'sec') or contains(@id,'cap') or contains(@id,'sec')]",
        ]
        for xp in nearby_xpaths:
            try:
                img = cap_field.find_element(By.XPATH, xp)
                if img and img.is_displayed():
                    return img
            except Exception:
                continue

    selectors = [
        "img[src*='captcha' i]",
        "img[id*='captcha' i]",
        "img[name*='captcha' i]",
        "img[src*='security' i]",
        "img[id*='security' i]",
        "img[src*='rand' i]",
        "img[src*='code' i]",
    ]
    for sel in selectors:
        try:
            img = driver.find_element(By.CSS_SELECTOR, sel)
            if img and img.is_displayed():
                return img
        except Exception:
            continue

    # Last fallback: likely captcha-sized visible image.
    for img in driver.find_elements(By.TAG_NAME, "img"):
        try:
            if not img.is_displayed():
                continue
            w = img.size.get("width", 0)
            h = img.size.get("height", 0)
            if 40 <= w <= 260 and 20 <= h <= 120:
                return img
        except Exception:
            continue
    return None


def _extract_captcha_bytes(driver: webdriver.Chrome, captcha_img) -> bytes:
    """Return CAPTCHA bytes, prioritizing rendered screenshot to avoid session/token mismatch."""
    try:
        rendered = captcha_img.screenshot_as_png
        if rendered and len(rendered) > 100:
            return rendered
    except Exception:
        logger.debug("Element screenshot failed, trying direct image download", exc_info=True)

    src = (captcha_img.get_attribute("src") or "").strip()
    if src:
        try:
            captcha_url = urljoin(driver.current_url, src)
            sess = requests.Session()

            # Reuse browser cookies so CAPTCHA request maps to the same server-side session.
            for ck in driver.get_cookies():
                sess.cookies.set(
                    ck.get("name", ""),
                    ck.get("value", ""),
                    domain=ck.get("domain"),
                    path=ck.get("path", "/"),
                )

            user_agent = driver.execute_script("return navigator.userAgent")
            headers = {"User-Agent": user_agent} if user_agent else {}
            resp = sess.get(captcha_url, headers=headers, timeout=12)
            resp.raise_for_status()
            if resp.content and len(resp.content) > 100:
                return resp.content
        except Exception:
            logger.debug("Direct CAPTCHA download failed", exc_info=True)

    # Final fallback.
    return captcha_img.screenshot_as_png


def _save_captcha_debug_artifacts(
    driver: webdriver.Chrome,
    attempt_no: int,
    captcha_bytes: bytes,
    chosen: str,
    candidates: list[str],
) -> None:
    """Persist CAPTCHA image + OCR metadata for offline tuning when CAPTCHA_DEBUG=1."""
    if not CAPTCHA_DEBUG:
        return
    try:
        CAPTCHA_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        stem = f"attempt_{attempt_no:02d}_{ts}"

        img_path = CAPTCHA_DEBUG_DIR / f"{stem}.png"
        meta_path = CAPTCHA_DEBUG_DIR / f"{stem}.txt"
        page_path = CAPTCHA_DEBUG_DIR / f"{stem}_page.png"

        img_path.write_bytes(captcha_bytes)

        with meta_path.open("w", encoding="utf-8") as f:
            f.write(f"timestamp={ts}\n")
            f.write(f"attempt={attempt_no}\n")
            f.write(f"url={driver.current_url}\n")
            f.write(f"chosen={chosen}\n")
            f.write(f"candidates={candidates}\n")

        try:
            driver.save_screenshot(str(page_path))
        except Exception:
            logger.debug("Failed to save page screenshot for CAPTCHA debug", exc_info=True)

        logger.info("Saved CAPTCHA debug artifacts: %s", img_path)
    except Exception:
        logger.exception("Failed to save CAPTCHA debug artifacts")


# ---------------------------------------------------------------------------
# Navigate to attendance
# ---------------------------------------------------------------------------

def _navigate_to_attendance(
    driver: webdriver.Chrome,
    year: str | None = None,
    semester: str | None = None,
    progress_callback: Callable[[int, str], None] | None = None,
) -> bool:
    """Navigate from dashboard to attendance table. Returns True on success."""
    wait = WebDriverWait(driver, WAIT_TIMEOUT)

    try:
        _report_progress(progress_callback, 72, "Opening attendance page")
        # Switch back to default content in case we're in a frame
        driver.switch_to.default_content()
        _settle_after_action(driver, ACTION_DELAY)

        # Look for frames again on the dashboard
        frames = driver.find_elements(By.TAG_NAME, "frame")
        if not frames:
            frames = driver.find_elements(By.TAG_NAME, "iframe")

        # Search current context for menu items that may be links, buttons, text nodes, or JS handlers.
        def search_and_click(text_patterns: list[str]) -> bool:
            for pattern in text_patterns:
                p = pattern.lower()
                selectors = [
                    (By.XPATH, f"//a[contains(translate(normalize-space(text()), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{p}')]"),
                    (By.XPATH, f"//button[contains(translate(normalize-space(text()), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{p}')]"),
                    (By.XPATH, f"//input[contains(translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{p}')]"),
                    (By.XPATH, f"//*[@title and contains(translate(@title, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{p}')]"),
                    (By.XPATH, f"//*[@aria-label and contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{p}')]"),
                    (By.XPATH, f"//*[@name and contains(translate(@name, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{p}')]"),
                    (By.XPATH, f"//*[@id and contains(translate(@id, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{p}')]"),
                    (By.XPATH, f"//*[@onclick and contains(translate(@onclick, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{p}')]"),
                    (By.XPATH, f"//img[@alt and contains(translate(@alt, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{p}')]"),
                    (By.XPATH, f"//*[self::td or self::span or self::div][contains(translate(normalize-space(text()), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{p}')]"),
                ]

                for sel_by, sel_val in selectors:
                    try:
                        elements = driver.find_elements(sel_by, sel_val)
                        for el in elements:
                            # Try clicking element, then closest clickable ancestor.
                            candidates = [el]
                            try:
                                anc = el.find_element(
                                    By.XPATH,
                                    "ancestor-or-self::*[self::a or self::button or self::input or @onclick or @role='button'][1]",
                                )
                                if anc is not None:
                                    candidates.append(anc)
                            except Exception:
                                pass

                            for target in candidates:
                                try:
                                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", target)
                                except Exception:
                                    pass
                                try:
                                    if target.is_displayed() and target.is_enabled():
                                        target.click()
                                    else:
                                        driver.execute_script("arguments[0].click();", target)
                                    _settle_after_action(driver, PAGE_SETTLE_DELAY)
                                    return True
                                except Exception:
                                    try:
                                        driver.execute_script("arguments[0].click();", target)
                                        _settle_after_action(driver, PAGE_SETTLE_DELAY)
                                        return True
                                    except Exception:
                                        continue
                    except Exception:
                        continue
            return False

        def click_plus_for_attendance() -> bool:
            """Click the '+' expander associated with Attendance menu node."""
            plus_selectors = [
                # Any plus icon/button in the same row/container as Attendance text.
                (By.XPATH, "//tr[.//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'attendance')]]//*[self::img or self::a or self::span][contains(translate(@src, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'plus') or normalize-space(text())='+' or contains(translate(@class, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'plus') or contains(translate(@title, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'expand') or contains(translate(@onclick, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'expand')]"),
                (By.XPATH, "//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'attendance')]/preceding::*[self::img or self::a][1][contains(translate(@src, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'plus') or normalize-space(text())='+']"),
                (By.XPATH, "//img[contains(translate(@src, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'plus')]"),
            ]
            for sel_by, sel_val in plus_selectors:
                try:
                    elements = driver.find_elements(sel_by, sel_val)
                    for el in elements:
                        if el and el.is_displayed():
                            el.click()
                            _settle_after_action(driver, ACTION_DELAY)
                            return True
                except Exception:
                    continue
            return False

        def click_attendance_fallback_controls() -> bool:
            """Fallback for button/JS menus where text is not directly visible."""
            fallback_selectors = [
                (By.XPATH, "//*[@onclick and (contains(translate(@onclick,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'attendance') or contains(translate(@onclick,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'myatt'))]"),
                (By.XPATH, "//*[@id and contains(translate(@id,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'attendance')]"),
                (By.XPATH, "//*[@name and contains(translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'attendance')]"),
                (By.XPATH, "//img[contains(translate(@src,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'attendance')]"),
            ]
            for sel_by, sel_val in fallback_selectors:
                try:
                    elements = driver.find_elements(sel_by, sel_val)
                    for el in elements:
                        try:
                            if el.is_displayed():
                                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                            driver.execute_script("arguments[0].click();", el)
                            _settle_after_action(driver, PAGE_SETTLE_DELAY)
                            return True
                        except Exception:
                            continue
                except Exception:
                    continue
            return False

        def build_context_paths() -> list[tuple[int, ...]]:
            """Return frame context paths to try: default, 1-level, and 2-level frames."""
            paths: list[tuple[int, ...]] = [tuple()]
            driver.switch_to.default_content()
            top_frames = driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
            for i in range(len(top_frames)):
                paths.append((i,))
                try:
                    driver.switch_to.default_content()
                    current_top = driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
                    if i >= len(current_top):
                        continue
                    driver.switch_to.frame(current_top[i])
                    child_frames = driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
                    for j in range(len(child_frames)):
                        paths.append((i, j))
                except Exception:
                    continue
            driver.switch_to.default_content()
            return paths

        def switch_to_path(path: tuple[int, ...]) -> bool:
            try:
                driver.switch_to.default_content()
                for idx in path:
                    fs = driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
                    if idx >= len(fs):
                        return False
                    driver.switch_to.frame(fs[idx])
                return True
            except Exception:
                return False

        # Try clicking through the menu in main content or frames
        def try_navigation() -> bool:
            # Step 1: Click "My Activities" or similar
            my_activities_clicked = search_and_click(["my activities", "activities"])
            if not my_activities_clicked:
                logger.debug("Could not find 'My Activities' link")
                # Continue: some layouts open directly into menu tree.

            _settle_after_action(driver, ACTION_DELAY)

            # Step 2: Click "Attendance" tab/menu
            attendance_clicked = search_and_click(["attendance"])
            if not attendance_clicked:
                attendance_clicked = click_attendance_fallback_controls()

            if not attendance_clicked:
                logger.debug("Could not find 'Attendance' link")
                return False

            _settle_after_action(driver, ACTION_DELAY)

            # Step 3: Expand Attendance via '+' (if tree/menu requires expansion)
            click_plus_for_attendance()
            _settle_after_action(driver, ACTION_DELAY)

            # Step 4: Click "My Attendance"
            if not search_and_click(["my attendance", "attendance report", "attendence", "my attend"]):
                # Final fallback: click attendance-like controls again and re-try My Attendance.
                click_attendance_fallback_controls()
                _settle_after_action(driver, ACTION_DELAY)
                if search_and_click(["my attendance", "attendance report", "attendence", "my attend"]):
                    return True
                logger.debug("Could not find 'My Attendance' link after Attendance expansion")
                return False
            _settle_after_action(driver, ACTION_DELAY)

            return True

        navigation_success = False
        for path in build_context_paths():
            if not switch_to_path(path):
                continue
            if try_navigation():
                navigation_success = True
                break

        if not navigation_success:
            logger.error("Attendance navigation path not found (My Activities -> Attendance -> + -> My Attendance)")
            _set_login_diagnostic("Navigation failed: could not locate My Activities/Attendance/My Attendance controls in any frame context")
            return False

        _report_progress(progress_callback, 82, "Applying year and semester")
        _settle_after_action(driver, PAGE_SETTLE_DELAY)

        # --- Select year/semester (if required) and click Proceed ---
        if not _select_filters_and_proceed(driver, year, semester):
            logger.error("Failed to select year/semester and proceed")
            return False

        _report_progress(progress_callback, 88, "Attendance page ready")
        return True

    except Exception:
        logger.exception("Failed to navigate to attendance page")
        return False


def _clean_filter_value(name_hints: list[str], text: str, raw_value: str) -> str:
    combined = (text or raw_value or "").strip()
    if any("sem" in hint or "term" in hint for hint in name_hints):
        match = re.search(r"\d+", combined)
        if match:
            return match.group(0)
    return combined


def _matches_requested_filter(value: str, text: str, raw_value: str) -> bool:
    requested = (value or "").strip().lower()
    clean_text = (text or "").strip().lower()
    clean_value = (raw_value or "").strip().lower()
    if not requested:
        return False
    return requested == clean_text or requested == clean_value or requested in clean_text or requested in clean_value


def _is_meaningful_option_text(text: str, raw_value: str) -> bool:
    clean_text = (text or "").strip().lower()
    clean_value = (raw_value or "").strip().lower()
    if not clean_text:
        return False
    if clean_text in {"select", "select...", "--select--", "choose", "-", "--"}:
        return False
    if clean_value in {"", "0", "-1"} and "select" in clean_text:
        return False
    return True


def _select_dropdown(
    driver: webdriver.Chrome,
    name_hints: list[str],
    value: str | None,
) -> tuple[bool, str | None]:
    """Find a <select>, keep the current meaningful selection, or set the requested value."""
    for hint in name_hints:
        try:
            selects = driver.find_elements(By.TAG_NAME, "select")
            for sel_elem in selects:
                name = (sel_elem.get_attribute("name") or "").lower()
                sel_id = (sel_elem.get_attribute("id") or "").lower()
                if hint not in name and hint not in sel_id:
                    continue

                select = Select(sel_elem)
                options = select.options
                if not options:
                    continue

                # If the right value is already selected, keep it and move on.
                try:
                    current = select.first_selected_option
                    current_text = (current.text or "").strip()
                    current_val = (current.get_attribute("value") or "").strip()
                    if value and _matches_requested_filter(value, current_text, current_val):
                        logger.info("Detected requested dropdown '%s' value '%s'", hint, current_text)
                        return True, _clean_filter_value(name_hints, current_text, current_val)
                except Exception:
                    pass

                # If a value is provided, prefer an explicit match.
                if value:
                    for opt in options:
                        text = (opt.text or "").strip()
                        val = (opt.get_attribute("value") or "").strip()
                        if _matches_requested_filter(value, text, val):
                            select.select_by_visible_text(opt.text)
                            logger.info("Selected '%s' in dropdown '%s'", opt.text, hint)
                            return True, _clean_filter_value(name_hints, text, val)
                    logger.info("Requested value '%s' not found in dropdown '%s'; trying another context", value, hint)
                    return False, None

                # Keep the portal's current selection when caller did not request a specific value.
                try:
                    current = select.first_selected_option
                    current_text = (current.text or "").strip()
                    current_val = (current.get_attribute("value") or "").strip()
                    if _is_meaningful_option_text(current_text, current_val):
                        logger.info("Detected active dropdown '%s' value '%s'", hint, current_text)
                        return True, _clean_filter_value(name_hints, current_text, current_val)
                except Exception:
                    pass

                # When caller didn't request a specific value, avoid forcing a new selection.
                # IMS usually already has the active academic period selected, and choosing the
                # first visible option can switch to the wrong year/semester and yield no rows.
                if not value:
                    logger.info("No explicit value provided for dropdown '%s'; leaving selection unchanged", hint)
                    return False, None
        except Exception:
            continue
    return False, None


def _has_attendance_table_like_content(driver: webdriver.Chrome) -> bool:
    """Quick check whether current context seems to contain attendance data table."""
    try:
        html = (driver.page_source or "").lower()
        if "total classes" in html or "total present" in html:
            return True
        if "my attendance" in html and "<table" in html:
            return True
    except Exception:
        return False
    return False


def _select_filters_and_proceed(driver: webdriver.Chrome, year: str | None, semester: str | None) -> bool:
    """Try all contexts to set filters and click Proceed/Show button."""
    def has_matching_select(name_hints: list[str]) -> bool:
        try:
            selects = driver.find_elements(By.TAG_NAME, "select")
        except Exception:
            return False
        for sel_elem in selects:
            name = (sel_elem.get_attribute("name") or "").lower()
            sel_id = (sel_elem.get_attribute("id") or "").lower()
            if any(hint in name or hint in sel_id for hint in name_hints):
                return True
        return False

    def click_proceed_here() -> bool:
        selectors = [
            (By.XPATH, "//input[contains(translate(@value,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'proceed') or contains(translate(@value,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'show') or contains(translate(@value,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'view') or contains(translate(@value,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'submit') or contains(translate(@value,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'get') ]"),
            (By.XPATH, "//button[contains(translate(normalize-space(text()),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'proceed') or contains(translate(normalize-space(text()),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'show') or contains(translate(normalize-space(text()),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'view') or contains(translate(normalize-space(text()),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'submit') or contains(translate(normalize-space(text()),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'get')]"),
            (By.XPATH, "//*[@onclick and (contains(translate(@onclick,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'proceed') or contains(translate(@onclick,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'show') or contains(translate(@onclick,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'attendance'))]"),
        ]
        for by, sel in selectors:
            try:
                for el in driver.find_elements(by, sel):
                    try:
                        if el.is_displayed():
                            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        driver.execute_script("arguments[0].click();", el)
                        _settle_after_action(driver, PAGE_SETTLE_DELAY)
                        return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    def try_in_current_context() -> bool:
        has_select = bool(driver.find_elements(By.TAG_NAME, "select"))
        selected_year: str | None = year
        selected_semester: str | None = semester
        year_ok = False
        sem_ok = False
        if has_select:
            if year and not has_matching_select(["year", "acad", "session"]):
                return False
            if semester and not has_matching_select(["sem", "semester", "term"]):
                return False
            year_ok, selected_year = _select_dropdown(driver, ["year", "acad", "session"], year)
            sem_ok, selected_semester = _select_dropdown(driver, ["sem", "semester", "term"], semester)
            _set_selected_filters(selected_year, selected_semester)
            logger.info(
                "Filter selection status: year=%s(%s) semester=%s(%s)",
                year_ok,
                selected_year,
                sem_ok,
                selected_semester,
            )

            # When a specific filter was requested, skip contexts that do not expose it correctly.
            if year and not year_ok:
                return False
            if semester and not sem_ok:
                return False
        else:
            _set_selected_filters(year, semester)

        # Click proceed if available.
        clicked = click_proceed_here()
        if clicked:
            return True

        # Some pages auto-refresh on dropdown change. Allow this only when table-like content appears.
        if has_select:
            _settle_after_action(driver, PAGE_SETTLE_DELAY)
            return _has_attendance_table_like_content(driver)

        # No select/no proceed in this context; only valid if table already present.
        return _has_attendance_table_like_content(driver)

    # default context
    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    if try_in_current_context():
        return True

    # try frames
    try:
        frames = driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
    except Exception:
        frames = []

    for frame in frames:
        try:
            driver.switch_to.default_content()
            driver.switch_to.frame(frame)
            if try_in_current_context():
                return True

            # nested frames
            child_frames = driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
            for child in child_frames:
                try:
                    driver.switch_to.default_content()
                    driver.switch_to.frame(frame)
                    driver.switch_to.frame(child)
                    if try_in_current_context():
                        return True
                except Exception:
                    continue
        except Exception:
            continue

    return False


# ---------------------------------------------------------------------------
# Parse attendance table
# ---------------------------------------------------------------------------

def _classify_timeline_status(cell_text: str) -> str | None:
    """Classify one attendance cell to present/absent/holiday/mixed/other."""
    txt = (cell_text or "").strip().upper()
    if not txt or txt in {"TL", "GH", "MB", "MS", "NA", "-", "---"}:
        return "holiday"

    bits = re.findall(r"(?<!\d)[01](?!\d)", txt)
    if bits:
        has_1 = "1" in bits
        has_0 = "0" in bits
        if has_1 and has_0:
            return "mixed"
        if has_1:
            return "present"
        return "absent"

    if txt in {"P", "PR", "PRESENT"}:
        return "present"
    if txt in {"A", "AB", "ABSENT"}:
        return "absent"
    return "other"


def _extract_datewise_timeline(table) -> dict[str, list[dict[str, str]]]:
    """Extract per-subject date-wise statuses from IMS matrix attendance table."""
    rows = table.find_all("tr")
    if len(rows) < 2:
        return {}

    def _norm(t: str) -> str:
        return (t or "").strip().lower()

    def _is_subject_code_strict(text: str) -> bool:
        raw = (text or "").strip().upper()
        if not raw:
            return False
        code = re.sub(r"[^A-Z0-9]", "", raw)
        if len(code) < 5:
            return False
        if not re.search(r"[A-Z]", code) or not re.search(r"\d", code):
            return False
        return bool(re.match(r"^[A-Z]{2,}[A-Z0-9]*\d{2,}$", code))

    for i, row in enumerate(rows[:8]):
        cells = row.find_all(["th", "td"])
        texts = [c.get_text(" ", strip=True) for c in cells]
        if len(texts) < 3:
            continue

        first = _norm(texts[0])
        if first not in {"days", "day", "date"}:
            continue

        subject_codes: list[str] = []
        code_col_indices: list[int] = []
        for j, t in enumerate(texts[1:], start=1):
            if _is_subject_code_strict(t):
                subject_codes.append(t.strip())
                code_col_indices.append(j)

        if len(subject_codes) < 2:
            continue

        timeline: dict[str, list[dict[str, str]]] = {c: [] for c in subject_codes}

        for data_row in rows[i + 1:]:
            dcells = data_row.find_all(["th", "td"])
            dtexts = [c.get_text(" ", strip=True) for c in dcells]
            if not dtexts:
                continue

            first_text = (dtexts[0] or "").strip()
            first_lower = _norm(first_text)
            if (
                "overall" in first_lower
                or "total" in first_lower
                or "legend" in first_lower
                or "note" in first_lower
                or "->" in first_text
            ):
                continue

            date_label = first_text
            if not date_label:
                continue

            for k, col_idx in enumerate(code_col_indices):
                if col_idx >= len(dtexts):
                    continue
                raw = (dtexts[col_idx] or "").strip()
                status = _classify_timeline_status(raw)
                if not status:
                    continue
                timeline[subject_codes[k]].append(
                    {
                        "date": date_label,
                        "status": status,
                        "raw": raw,
                    }
                )

        timeline = {k: v for k, v in timeline.items() if v}
        if timeline:
            return timeline

    return {}


def _parse_attendance_table_with_timeline(
    driver: webdriver.Chrome,
    include_timeline: bool = True,
) -> tuple[dict[str, dict[str, int]], dict[str, list[dict[str, str]]]]:
    """
    Parse attendance table(s) and date-wise timeline from current page.
    Returns ({ subject_code: {total, present} }, { subject_code: [date-wise entries] }).
    """
    results: dict[str, dict[str, int]] = {}
    timeline: dict[str, list[dict[str, str]]] = {}

    def _iter_sources():
        seen: set[tuple[int, str, str]] = set()

        def _capture_current_source() -> str | None:
            try:
                src = driver.page_source
            except Exception:
                return None
            if not src:
                return None

            sample = min(SOURCE_DEDUPE_SAMPLE_CHARS, max(len(src), 1))
            key = (len(src), src[:sample], src[-sample:])
            if key in seen:
                return None
            seen.add(key)
            return src

        try:
            driver.switch_to.default_content()
            src = _capture_current_source()
            if src:
                yield src
        except Exception:
            pass

        try:
            top_frames = driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
        except Exception:
            top_frames = []

        for i in range(len(top_frames)):
            try:
                driver.switch_to.default_content()
                cur_top = driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
                if i >= len(cur_top):
                    continue
                driver.switch_to.frame(cur_top[i])
                src = _capture_current_source()
                if src:
                    yield src

                nested = driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
                for j in range(len(nested)):
                    try:
                        driver.switch_to.default_content()
                        cur_top = driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
                        if i >= len(cur_top):
                            continue
                        driver.switch_to.frame(cur_top[i])
                        cur_nested = driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
                        if j >= len(cur_nested):
                            continue
                        driver.switch_to.frame(cur_nested[j])
                        src = _capture_current_source()
                        if src:
                            yield src
                    except Exception:
                        continue
            except Exception:
                continue

    # Pick best table from current page contexts instead of merging all tables blindly.
    current_sources = _iter_sources()
    results, timeline = _parse_attendance_from_sources(list(current_sources), include_timeline=include_timeline)

    fixed_overall_totals = False
    if results:
        totals = [v.get("total", 0) for v in results.values()]
        if totals and max(totals) >= 20:
            fixed_overall_totals = True
        if fixed_overall_totals and not include_timeline:
            return results, timeline

    # Handle pagination – click "Next" and parse again
    if not ENABLE_PAGINATION and not include_timeline:
        return results, timeline

    try:
        driver.switch_to.default_content()
        previous_snapshot = str((results, timeline))
        while True:
            next_btn = None
            frames = driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
            for frame in frames:
                try:
                    driver.switch_to.default_content()
                    driver.switch_to.frame(frame)
                    next_candidates = driver.find_elements(By.XPATH, "//a[contains(text(),'Next')]")
                    next_candidates += driver.find_elements(By.XPATH, "//input[@value='Next']")
                    next_candidates += driver.find_elements(By.XPATH, "//button[contains(text(),'Next')]")
                    if next_candidates:
                        next_btn = next_candidates[0]
                        break
                except Exception:
                    continue

            if not next_btn:
                # Also check default content
                driver.switch_to.default_content()
                next_candidates = driver.find_elements(By.XPATH, "//a[contains(text(),'Next')]")
                next_candidates += driver.find_elements(By.XPATH, "//input[@value='Next']")
                if next_candidates:
                    next_btn = next_candidates[0]

            if not next_btn:
                break

            next_btn.click()
            time.sleep(PAGE_SETTLE_DELAY)

            # Parse best table on the new page and merge with cumulative totals.
            new_best, new_timeline = _parse_attendance_from_sources(list(_iter_sources()), include_timeline=include_timeline)
            if not new_best and not new_timeline:
                continue

            # Guard against infinite loop when Next doesn't change the parsed content.
            snapshot = str((new_best, new_timeline))
            if snapshot == previous_snapshot:
                break
            previous_snapshot = snapshot

            if not fixed_overall_totals:
                for subj, data in new_best.items():
                    if subj in results:
                        results[subj]["total"] += data["total"]
                        results[subj]["present"] += data["present"]
                    else:
                        results[subj] = dict(data)
            elif not results and new_best:
                results = {subj: dict(data) for subj, data in new_best.items()}

            for subj, entries in new_timeline.items():
                timeline.setdefault(subj, []).extend(entries)
    except Exception:
        logger.debug("No pagination or pagination handling failed")

    return results, timeline


def _parse_attendance_table(driver: webdriver.Chrome) -> dict[str, dict[str, int]]:
    """
    Backward-compatible parser returning only summary totals.
    """
    summary, _timeline = _parse_attendance_table_with_timeline(driver, include_timeline=False)
    return summary


def _dedupe_timeline_entries(
    timeline: dict[str, list[dict[str, str]]],
) -> dict[str, list[dict[str, str]]]:
    """Remove duplicate date-wise entries while preserving order."""
    out: dict[str, list[dict[str, str]]] = {}
    for subj, entries in timeline.items():
        seen: set[tuple[str, str, str]] = set()
        clean: list[dict[str, str]] = []
        for e in entries:
            key = (
                (e.get("date") or "").strip(),
                (e.get("status") or "").strip().lower(),
                (e.get("raw") or "").strip(),
            )
            if key in seen:
                continue
            seen.add(key)
            clean.append(e)
        if clean:
            out[subj] = clean
    return out


def _parse_attendance_from_current_driver(
    driver: webdriver.Chrome,
    year: str | None,
    semester: str | None,
    include_timeline: bool,
    progress_callback: Callable[[int, str], None] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, str]]], str]:
    """
    Fast-path for an already logged-in browser. Prefer refreshing the current
    attendance page first, then fall back to full menu navigation if needed.
    """
    try:
        _report_progress(progress_callback, 12, "Reusing warm browser session")
        driver.switch_to.default_content()
    except Exception:
        pass

    if _is_login_page_still_visible(driver):
        logger.info("Warm browser session appears to be back on the login page")
        _set_login_diagnostic("Warm browser session expired and returned to login page")
        return {}, {}, STATUS_NAVIGATION_FAILED

    refreshed_here = False
    try:
        _report_progress(progress_callback, 74, "Refreshing attendance page")
        refreshed_here = _select_filters_and_proceed(driver, year, semester)
    except Exception:
        logger.debug("Warm-session attendance refresh from current page failed", exc_info=True)

    if not refreshed_here:
        if not _navigate_to_attendance(driver, year, semester, progress_callback=progress_callback):
            logger.info("Warm browser session could not reach attendance page")
            return {}, {}, STATUS_NAVIGATION_FAILED

    _report_progress(progress_callback, 93, "Reading subject-wise attendance")
    data, timeline = _parse_attendance_table_with_timeline(driver, include_timeline=include_timeline)
    timeline = _dedupe_timeline_entries(timeline)
    if not data:
        logger.info("Warm browser session returned no attendance rows")
        return {}, timeline, STATUS_NAVIGATION_FAILED

    _report_progress(progress_callback, 97, "Preparing Telegram reply")
    return data, timeline, STATUS_SUCCESS


def _login_and_fetch_attendance_via_requests(
    user_id: str,
    password: str,
    year: str | None,
    semester: str | None,
    include_timeline: bool,
    progress_callback: Callable[[int, str], None] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, str]]], str]:
    if not ENABLE_HTTP_FAST_PATH:
        return {}, {}, STATUS_UNKNOWN_ERROR

    base = "https://www.imsnsit.org/imsnsit/"

    for attempt in range(1, HTTP_LOGIN_RETRIES + 1):
        try:
            _report_progress(progress_callback, 6, f"Opening IMS session via fast HTTP path ({attempt}/{HTTP_LOGIN_RETRIES})")
            session = requests.Session()
            session.headers.update(_requests_headers())

            session.get(base, timeout=20)
            session.get(urljoin(base, "plum5_fw_login.php?t=sw&w=1"), timeout=20)
            session.get(urljoin(base, "student.htm"), headers={"Referer": base}, timeout=20)
            session.get(
                urljoin(base, "student_login110.php"),
                headers={"Referer": urljoin(base, "student.htm")},
                timeout=20,
            )

            login_page = session.get(
                urljoin(base, "student_login.php"),
                headers={"Referer": urljoin(base, "student.htm"), "Upgrade-Insecure-Requests": "1"},
                timeout=20,
            )
            login_html = login_page.text or ""
            if not login_html:
                logger.info("HTTP fast path returned empty student_login.php page on attempt %d", attempt)
                continue

            fy_match = re.search(r"name='fy' id='fy' value='([^']+)'", login_html)
            comp_match = re.search(r"name='comp' id='comp' type='hidden' readonly value='([^']+)'", login_html)
            hrand_match = re.search(r"name='HRAND_NUM' id='HRAND_NUM' value='([^']+)'", login_html)
            capsrc_match = re.search(r"<img src='([^']+captcha[^']+)' id='captchaimg'", login_html)
            if not all([fy_match, comp_match, hrand_match, capsrc_match]):
                logger.info("HTTP fast path could not parse login form fields on attempt %d", attempt)
                continue

            _report_progress(progress_callback, 18, "Solving IMS security number")
            captcha_response = session.get(
                urljoin(base, capsrc_match.group(1)),
                headers={"Referer": urljoin(base, "student_login.php")},
                timeout=20,
            )
            captcha_text, _ = solve_captcha_with_debug(captcha_response.content)
            if not captcha_text or len(captcha_text) < 4:
                logger.info("HTTP fast path produced no usable CAPTCHA on attempt %d", attempt)
                continue

            login_payload = {
                "f": "",
                "uid": user_id,
                "pwd": password,
                "HRAND_NUM": hrand_match.group(1),
                "fy": fy_match.group(1),
                "comp": comp_match.group(1),
                "cap": captcha_text,
                "logintype": "student",
            }
            _report_progress(progress_callback, 32, "Signing in through fast HTTP path")
            login_response = session.post(
                urljoin(base, "student_login.php"),
                data=login_payload,
                headers={
                    "Referer": urljoin(base, "student_login.php"),
                    "Origin": "https://www.imsnsit.org",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Upgrade-Insecure-Requests": "1",
                },
                timeout=20,
            )
            banner_html = login_response.text or ""
            banner_lower = banner_html.lower()
            if "invalid security" in banner_lower or "please login" in banner_lower:
                logger.info("HTTP fast path login attempt %d failed at security validation", attempt)
                continue
            if "logout" not in banner_lower or "my activities" not in banner_lower:
                logger.info("HTTP fast path login attempt %d did not reach student dashboard", attempt)
                continue

            _report_progress(progress_callback, 48, "Opening My Activities")
            my_activities_match = re.search(
                r"href='(https://www\.imsnsit\.org/imsnsit/plum_url\.php\?[^']+)'[^>]*>My Activities<",
                banner_html,
                flags=re.I,
            )
            if not my_activities_match:
                logger.info("HTTP fast path could not find My Activities link")
                return {}, {}, STATUS_NAVIGATION_FAILED

            my_activities_url = my_activities_match.group(1)
            menu_page = session.get(
                my_activities_url,
                headers={"Referer": urljoin(base, "student_login.php")},
                timeout=20,
            )
            menu_html = menu_page.text or ""
            attendance_match = re.search(
                r"href='(https://www\.imsnsit\.org/imsnsit/plum_url\.php\?[^']+)'[^>]*>My Attendance<",
                menu_html,
                flags=re.I,
            )
            if not attendance_match:
                logger.info("HTTP fast path could not find My Attendance link")
                return {}, {}, STATUS_NAVIGATION_FAILED

            attendance_url = attendance_match.group(1)
            attendance_page = session.get(
                attendance_url,
                headers={"Referer": my_activities_url},
                timeout=20,
            )
            attendance_html = attendance_page.text or ""
            if not attendance_html:
                logger.info("HTTP fast path received empty attendance landing page")
                return {}, {}, STATUS_NAVIGATION_FAILED

            initial_year = _extract_selected_option_value(attendance_html, "year")
            initial_sem = _extract_selected_option_value(attendance_html, "sem")
            resolved_year = (year or initial_year or "").strip() or None
            resolved_semester = (semester or initial_sem or "").strip() or None
            _set_selected_filters(resolved_year, resolved_semester)

            enc_year_match = re.search(r"name='enc_year' id='enc_year' value='([^']+)'", attendance_html)
            enc_sem_match = re.search(r"name='enc_sem' id='enc_sem' value='([^']+)'", attendance_html)
            recentity_match = re.search(r"name=recentitycode value='([^']+)'", attendance_html)
            dept_match = re.search(r"name=dept value='([^']+)'", attendance_html)
            degree_match = re.search(r"name=degree value='([^']+)'", attendance_html)
            if not all([enc_year_match, enc_sem_match, recentity_match, dept_match, degree_match]):
                logger.info("HTTP fast path could not parse attendance form fields")
                return {}, {}, STATUS_NAVIGATION_FAILED

            _report_progress(progress_callback, 72, "Submitting year and semester")
            attendance_payload = {
                "year": resolved_year or "",
                "enc_year": enc_year_match.group(1),
                "sem": resolved_semester or "",
                "enc_sem": enc_sem_match.group(1),
                "submit": "Submit",
                "recentitycode": recentity_match.group(1),
                "dept": dept_match.group(1),
                "degree": degree_match.group(1),
                "ename": "",
                "ecode": "",
            }
            attendance_response = session.post(
                attendance_url,
                data=attendance_payload,
                headers={
                    "Referer": attendance_url,
                    "Origin": "https://www.imsnsit.org",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Upgrade-Insecure-Requests": "1",
                },
                timeout=20,
            )
            result_html = attendance_response.text or ""
            if not result_html:
                logger.info("HTTP fast path received empty attendance response")
                return {}, {}, STATUS_NAVIGATION_FAILED

            selected_year = _extract_selected_option_value(result_html, "year") or resolved_year
            selected_sem = _extract_selected_option_value(result_html, "sem") or resolved_semester
            _set_selected_filters(selected_year, selected_sem)

            _report_progress(progress_callback, 92, "Reading subject-wise attendance")
            data, timeline = _parse_attendance_from_sources([result_html], include_timeline=include_timeline)
            timeline = _dedupe_timeline_entries(timeline)
            if not data:
                logger.info("HTTP fast path reached attendance page but parsed no data")
                return {}, timeline, STATUS_NAVIGATION_FAILED

            _report_progress(progress_callback, 97, "Preparing Telegram reply")
            logger.info("HTTP fast path fetched attendance for user %s", user_id)
            return data, timeline, STATUS_SUCCESS

        except requests.RequestException as exc:
            logger.info("HTTP fast path network issue on attempt %d: %s", attempt, exc)
            continue
        except Exception:
            logger.exception("HTTP fast path failed unexpectedly on attempt %d", attempt)
            continue

    return {}, {}, STATUS_UNKNOWN_ERROR


def _try_parse_table(table) -> dict[str, dict[str, int]] | None:
    """
    Try to interpret a BeautifulSoup <table> as an attendance table.
    Returns None if this doesn't look like an attendance table.
    """
    rows = table.find_all("tr")
    if len(rows) < 2:
        return None

    def _norm(t: str) -> str:
        return (t or "").strip().lower()

    def _is_noise_header(text: str) -> bool:
        t = _norm(text)
        noise = {
            "date", "day", "period", "lecture", "practical", "tutorial", "remarks",
            "total", "total classes", "total class", "total present", "total absent",
            "present", "absent", "attendance", "subject", "sr", "s.no", "sno",
        }
        return t in noise or t.startswith("total")

    def _is_subject_code_strict(text: str) -> bool:
        """Prefer real subject codes like MEICC405, MEMMEC402, etc."""
        raw = (text or "").strip().upper()
        if not raw:
            return False
        # Remove separators
        code = re.sub(r"[^A-Z0-9]", "", raw)
        if len(code) < 5:
            return False
        if not re.search(r"[A-Z]", code):
            return False
        if not re.search(r"\d", code):
            return False
        # Starts with letters and ends with digits is common in course codes.
        if not re.match(r"^[A-Z]{2,}[A-Z0-9]*\d{2,}$", code):
            return False
        return True

    def _count_binary_marks(cell_text: str) -> tuple[int, int, int]:
        """Count explicit 1/0 marks in a cell: 1=present, 0=absent."""
        txt = (cell_text or "").strip()
        if not txt:
            return (0, 0, 0)

        # Accept values like 1, 0, 1+0, 1/0, 1 0 etc as separate binary marks.
        bits = re.findall(r"(?<!\d)[01](?!\d)", txt)
        if bits:
            present = sum(1 for b in bits if b == "1")
            absent = sum(1 for b in bits if b == "0")
            total = present + absent
            return (total, present, absent)

        # Fallback for non-binary representations handled by existing parser.
        held, attended = parse_cell(txt)
        return (held, attended, max(held - attended, 0))

    def _to_int(text: str) -> int:
        return int(re.sub(r"[^0-9]", "", text or "") or "0")

    # --- Strategy 0 (exact IMS layout): Days row + subject columns + summary rows ---
    # Example:
    #   Days | MEICC405 | MEMEC401 ...
    #   ... daily rows ...
    #   Total Classes / Total Absent / Total Present
    for i, row in enumerate(rows[:8]):
        cells = row.find_all(["th", "td"])
        texts = [c.get_text(" ", strip=True) for c in cells]
        if len(texts) < 3:
            continue

        first = _norm(texts[0])
        if first not in {"days", "day", "date"}:
            continue

        subject_codes: list[str] = []
        code_col_indices: list[int] = []
        for j, t in enumerate(texts[1:], start=1):
            if _is_subject_code_strict(t):
                subject_codes.append(t.strip())
                code_col_indices.append(j)

        if len(subject_codes) < 2:
            continue

        agg: dict[str, dict[str, int]] = {c: {"total": 0, "present": 0, "absent": 0} for c in subject_codes}
        overall_classes_row: dict[str, int] = {}
        overall_present_row: dict[str, int] = {}
        overall_absent_row: dict[str, int] = {}
        total_classes_row: dict[str, int] = {}
        total_present_row: dict[str, int] = {}
        total_absent_row: dict[str, int] = {}

        for data_row in rows[i + 1:]:
            dcells = data_row.find_all(["th", "td"])
            dtexts = [c.get_text(" ", strip=True) for c in dcells]
            if not dtexts:
                continue

            first_lower = _norm(dtexts[0])

            # Summary rows at bottom of the attendance grid.
            if "overall" in first_lower and "class" in first_lower:
                for k, col_idx in enumerate(code_col_indices):
                    if col_idx < len(dtexts):
                        overall_classes_row[subject_codes[k]] = _to_int(dtexts[col_idx])
                continue
            if "overall" in first_lower and "present" in first_lower:
                for k, col_idx in enumerate(code_col_indices):
                    if col_idx < len(dtexts):
                        overall_present_row[subject_codes[k]] = _to_int(dtexts[col_idx])
                continue
            if "overall" in first_lower and "absent" in first_lower:
                for k, col_idx in enumerate(code_col_indices):
                    if col_idx < len(dtexts):
                        overall_absent_row[subject_codes[k]] = _to_int(dtexts[col_idx])
                continue

            if "total" in first_lower and "class" in first_lower:
                for k, col_idx in enumerate(code_col_indices):
                    if col_idx < len(dtexts):
                        total_classes_row[subject_codes[k]] = _to_int(dtexts[col_idx])
                continue
            if "total" in first_lower and "present" in first_lower:
                for k, col_idx in enumerate(code_col_indices):
                    if col_idx < len(dtexts):
                        total_present_row[subject_codes[k]] = _to_int(dtexts[col_idx])
                continue
            if "total" in first_lower and "absent" in first_lower:
                for k, col_idx in enumerate(code_col_indices):
                    if col_idx < len(dtexts):
                        total_absent_row[subject_codes[k]] = _to_int(dtexts[col_idx])
                continue

            # Skip notes/legend row (often has colspan and explanatory text).
            if "->" in (dtexts[0] if dtexts else "") or "legend" in first_lower or "note" in first_lower:
                continue

            # Daily rows: count 1/0 marks per subject.
            for k, col_idx in enumerate(code_col_indices):
                if col_idx >= len(dtexts):
                    continue
                held, attended, absent = _count_binary_marks(dtexts[col_idx])
                agg[subject_codes[k]]["total"] += held
                agg[subject_codes[k]]["present"] += attended
                agg[subject_codes[k]]["absent"] += absent

        # If explicit OVERALL totals exist, prefer them as authoritative.
        if overall_classes_row:
            out: dict[str, dict[str, int]] = {}
            for code in subject_codes:
                total = overall_classes_row.get(code, 0)
                if total <= 0:
                    continue
                present = overall_present_row.get(code, agg[code]["present"])
                absent = overall_absent_row.get(code, max(total - present, 0))
                if present < 0:
                    present = 0
                if present > total:
                    present = total
                if absent < 0:
                    absent = max(total - present, 0)
                out[code] = {"total": total, "present": present, "absent": absent}
            if out:
                return out

        # Otherwise if monthly TOTAL rows exist, use them.
        if total_classes_row:
            out: dict[str, dict[str, int]] = {}
            for code in subject_codes:
                total = total_classes_row.get(code, 0)
                if total <= 0:
                    continue
                present = total_present_row.get(code, agg[code]["present"])
                absent = total_absent_row.get(code, max(total - present, 0))
                if present < 0:
                    present = 0
                if present > total:
                    present = total
                if absent < 0:
                    absent = max(total - present, 0)
                out[code] = {"total": total, "present": present, "absent": absent}
            if out:
                return out

        # No explicit total rows found; use counted daily marks.
        agg = {c: d for c, d in agg.items() if d["total"] > 0}
        if agg:
            return agg

    # --- Strategy A: matrix table (subjects as columns, rows as dates/slots with 1/0) ---
    best_matrix: dict[str, dict[str, int]] = {}
    best_matrix_score = 0

    for i, row in enumerate(rows[:8]):  # header usually near top
        cells = row.find_all(["th", "td"])
        texts = [c.get_text(strip=True) for c in cells]
        if len(texts) < 3:
            continue

        subject_codes: list[str] = []
        code_col_indices: list[int] = []
        for j, text in enumerate(texts):
            if not text:
                continue
            if _is_noise_header(text):
                continue
            if _is_subject_code_strict(text):
                subject_codes.append(text.strip())
                code_col_indices.append(j)

        if len(subject_codes) < 2:
            continue

        agg: dict[str, dict[str, int]] = {c: {"total": 0, "present": 0, "absent": 0} for c in subject_codes}
        parsed_marks = 0

        # Prefer explicit summary block at bottom if present:
        # Overall Class / Overall Absent / Overall Present
        overall_class: dict[str, int] = {}
        overall_present: dict[str, int] = {}
        overall_absent: dict[str, int] = {}

        for data_row in rows[i + 1:]:
            dcells = data_row.find_all(["th", "td"])
            dtexts = [c.get_text(strip=True) for c in dcells]
            if not dtexts:
                continue

            first_lower = _norm(dtexts[0])
            if any(kw in first_lower for kw in ["note", "legend", "na not"]):
                continue

            # summary rows in matrix layout
            if "overall class" in first_lower:
                for k, col_idx in enumerate(code_col_indices):
                    if col_idx < len(dtexts):
                        try:
                            overall_class[subject_codes[k]] = int(re.sub(r"[^0-9]", "", dtexts[col_idx]) or "0")
                        except Exception:
                            pass
                continue
            if "overall present" in first_lower:
                for k, col_idx in enumerate(code_col_indices):
                    if col_idx < len(dtexts):
                        try:
                            overall_present[subject_codes[k]] = int(re.sub(r"[^0-9]", "", dtexts[col_idx]) or "0")
                        except Exception:
                            pass
                continue
            if "overall absent" in first_lower:
                for k, col_idx in enumerate(code_col_indices):
                    if col_idx < len(dtexts):
                        try:
                            overall_absent[subject_codes[k]] = int(re.sub(r"[^0-9]", "", dtexts[col_idx]) or "0")
                        except Exception:
                            pass
                continue

            if "total classes" in first_lower or "total class" in first_lower:
                for k, col_idx in enumerate(code_col_indices):
                    if col_idx < len(dtexts):
                        try:
                            agg[subject_codes[k]]["total"] = int(dtexts[col_idx].strip())
                        except Exception:
                            pass
                continue
            if "total present" in first_lower:
                for k, col_idx in enumerate(code_col_indices):
                    if col_idx < len(dtexts):
                        try:
                            agg[subject_codes[k]]["present"] = int(dtexts[col_idx].strip())
                        except Exception:
                            pass
                continue

            for k, col_idx in enumerate(code_col_indices):
                if col_idx >= len(dtexts):
                    continue
                held, attended, absent = _count_binary_marks(dtexts[col_idx])
                if held > 0:
                    parsed_marks += held
                agg[subject_codes[k]]["total"] += held
                agg[subject_codes[k]]["present"] += attended
                agg[subject_codes[k]]["absent"] += absent

        # If overall summary block exists, trust it (most accurate final totals).
        if overall_class and overall_present:
            final_agg: dict[str, dict[str, int]] = {}
            for code in subject_codes:
                total = overall_class.get(code, 0)
                present = overall_present.get(code, 0)
                if total <= 0:
                    continue
                if present < 0:
                    present = 0
                if present > total:
                    present = total
                absent = overall_absent.get(code, max(total - present, 0))
                if absent < 0:
                    absent = max(total - present, 0)
                final_agg[code] = {
                    "total": total,
                    "present": present,
                    "absent": absent,
                }
            if final_agg:
                parsed_marks = sum(v["total"] for v in final_agg.values())
                agg = final_agg

        agg = {c: d for c, d in agg.items() if d["total"] > 0}
        score = parsed_marks + len(agg) * 3
        if score > best_matrix_score:
            best_matrix = agg
            best_matrix_score = score

    # --- Strategy B: row-wise summary table (one row per subject) ---
    best_rowwise: dict[str, dict[str, int]] = {}
    best_rowwise_score = 0

    header_map: dict[str, int] = {}
    header_idx = -1
    for i, row in enumerate(rows[:8]):
        cells = row.find_all(["th", "td"])
        texts = [_norm(c.get_text(strip=True)) for c in cells]
        if not texts:
            continue
        # Identify columns by meaning
        for idx, t in enumerate(texts):
            if "subject" in t and "code" in t:
                header_map["subject"] = idx
            elif t == "subject":
                header_map.setdefault("subject", idx)
            elif "total" in t and "class" in t:
                header_map["total"] = idx
            elif "present" in t:
                header_map["present"] = idx
            elif "attend" in t and "(" not in t:
                # sometimes % column or textual, keep as fallback only
                header_map.setdefault("present", idx)
        if "subject" in header_map and "total" in header_map and "present" in header_map:
            header_idx = i
            break

    if header_idx >= 0:
        subj_i = header_map["subject"]
        total_i = header_map["total"]
        present_i = header_map["present"]
        candidate: dict[str, dict[str, int]] = {}
        for row in rows[header_idx + 1:]:
            cells = row.find_all(["th", "td"])
            texts = [c.get_text(strip=True) for c in cells]
            max_i = max(subj_i, total_i, present_i)
            if len(texts) <= max_i:
                continue
            code = texts[subj_i].strip()
            if not _is_subject_code_strict(code):
                continue
            try:
                total = int(re.sub(r"[^0-9]", "", texts[total_i]) or "0")
                present = int(re.sub(r"[^0-9]", "", texts[present_i]) or "0")
            except Exception:
                continue
            if total > 0:
                candidate[code] = {"total": total, "present": min(present, total)}

        best_rowwise = candidate
        best_rowwise_score = len(candidate) * 10 + sum(v["total"] for v in candidate.values())

    # Prefer matrix parsing (1/0 marks per subject) whenever available.
    if best_matrix:
        return best_matrix
    # Fallback to row-wise summary table only if matrix parsing fails.
    if best_rowwise:
        return best_rowwise
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_attendance_with_status(
    user_id: str,
    password: str,
    year: str | None = None,
    semester: str | None = None,
) -> tuple[dict[str, dict[str, Any]], str]:
    """
    Log into IMS NSIT, scrape attendance, and return parsed data.

    Parameters
    ----------
    user_id : str
        IMS portal user ID (e.g. "2024/ME4113").
    password : str
        IMS portal password.
    year : str, optional
        Academic year (e.g. "2025-26"). If None, uses default on portal.
    semester : str, optional
        Semester number (e.g. "4"). If None, uses default on portal.

    Returns ``(data, status)`` where status is one of STATUS_* constants.
    """
    data, _timeline, status = fetch_attendance_detailed(user_id, password, year, semester)
    return data, status


def _fetch_via_go_scraper(
    user_id: str,
    password: str,
    year: str | None,
    semester: str | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, str]]], str]:
    key = f"{user_id}\0{password}\0{year or ''}\0{semester or ''}"
    script = _GO_SCRAPER_SCRIPT
    binary = _GO_SCRAPER_BINARY_LINUX if os.path.exists(_GO_SCRAPER_BINARY_LINUX) else _GO_SCRAPER_BINARY_WIN
    cmd = None
    if os.path.exists(binary):
        cmd = [binary, user_id, password]
        if year:
            cmd.extend(["--year", year])
        if semester:
            cmd.extend(["--semester", semester])
    elif os.path.exists(script):
        cmd = ["go", "run", script, user_id, password]
        if year:
            cmd.extend(["--year", year])
        if semester:
            cmd.extend(["--semester", semester])
    if not cmd:
        return {}, {}, STATUS_UNKNOWN_ERROR

    try:
        env = os.environ.copy()
        env["CAPTCHA_SOLVER_SCRIPT"] = os.path.join(os.path.dirname(script), "solve_captcha_cli.py")
        proc = subprocess.run(
            cmd,
            input=None,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            cwd=os.path.dirname(script),
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        if stderr:
            logger.debug("Go scraper stderr: %s", stderr[:500])
        if proc.returncode != 0:
            return {}, {}, STATUS_UNKNOWN_ERROR
        data = json.loads(stdout)
        status = str(data.get("status", "")).strip() or STATUS_UNKNOWN_ERROR
        attendance: dict[str, dict[str, Any]] = {}
        for code, entry in (data.get("attendance") or {}).items():
            if not isinstance(entry, dict):
                continue
            total = int(entry.get("total", 0) or 0)
            present = int(entry.get("present", 0) or 0)
            absent = int(entry.get("absent", 0) or 0)
            if total > 0:
                attendance[str(code).upper()] = {
                    "total": total,
                    "present": present,
                    "absent": absent,
                }
        timeline_raw = data.get("timeline") or {}
        timeline: dict[str, list[dict[str, str]]] = {}
        for code, entries in timeline_raw.items():
            clean: list[dict[str, str]] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                date = str(entry.get("date", "") or "").strip()
                status_val = str(entry.get("status", "") or "").strip().lower()
                raw = str(entry.get("raw", "") or "").strip()
                if not date or not status_val:
                    continue
                clean.append({"date": date, "status": status_val, "raw": raw})
            if clean:
                timeline[str(code).upper()] = clean
        return attendance, timeline, status
    except json.JSONDecodeError:
        logger.debug("Go scraper returned non-JSON output", exc_info=True)
        return {}, {}, STATUS_UNKNOWN_ERROR
    except subprocess.TimeoutExpired:
        logger.debug("Go scraper timed out", exc_info=True)
        return {}, {}, STATUS_UNKNOWN_ERROR
    except Exception:
        logger.debug("Go scraper failed", exc_info=True)
        return {}, {}, STATUS_UNKNOWN_ERROR


def fetch_attendance_detailed(
    user_id: str,
    password: str,
    year: str | None = None,
    semester: str | None = None,
    include_timeline: bool | None = None,
    progress_callback: Callable[[int, str], None] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, str]]], str]:
    """
    Log into IMS NSIT, scrape attendance summary and date-wise timeline.

    Returns ``(data, timeline, status)``.
    """
    global _last_datewise_timeline
    _last_datewise_timeline = {}
    _set_selected_filters(year, semester)
    if include_timeline is None:
        include_timeline = INCLUDE_DATEWISE_TIMELINE

    warm_session: _WarmBrowserSession | None = None
    driver = None
    try:
        data, timeline, go_status = _fetch_via_go_scraper(
            user_id,
            password,
            year,
            semester,
        )
        if go_status == STATUS_SUCCESS:
            _last_datewise_timeline = timeline
            logger.info("Go scraper fetched attendance for %s", user_id)
            return data, timeline, STATUS_SUCCESS
        if go_status not in {STATUS_UNKNOWN_ERROR, ""}:
            logger.info("Go scraper failed for %s with status %s; falling back", user_id, go_status)

        # Fastest path: use plain HTTP session against the old server-rendered portal.
        data, timeline, http_status = _login_and_fetch_attendance_via_requests(
            user_id,
            password,
            year,
            semester,
            include_timeline=include_timeline,
            progress_callback=progress_callback,
        )
        if http_status == STATUS_SUCCESS:
            _last_datewise_timeline = timeline
            return data, timeline, STATUS_SUCCESS

        # Warm-session fast path: reuse a logged-in browser already parked on IMS.
        warm_session = _acquire_warm_session(user_id, password)
        if warm_session is not None:
            data, timeline, status = _parse_attendance_from_current_driver(
                warm_session.driver,
                year,
                semester,
                include_timeline=include_timeline,
                progress_callback=progress_callback,
            )
            if status == STATUS_SUCCESS:
                _last_datewise_timeline = timeline
                warm_session.last_used = time.time()
                logger.info("Served attendance for %s from warm session", user_id)
                return data, timeline, STATUS_SUCCESS

            logger.info("Warm session reuse failed for user %s; falling back to fresh login", user_id)
            _discard_warm_session(warm_session)
            warm_session.lock.release()
            warm_session = None

        # Login with CAPTCHA retry
        logged_in = False
        last_status = STATUS_UNKNOWN_ERROR
        _report_progress(progress_callback, 5, "Preparing secure session")
        for attempt in range(1, MAX_CAPTCHA_RETRIES + 1):
            # Create a fresh browser session on each attempt.
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = None

            _report_progress(progress_callback, 10, f"Launching background browser (attempt {attempt}/{MAX_CAPTCHA_RETRIES})")
            driver = _create_driver()
            logger.info("Login attempt %d/%d for user %s", attempt, MAX_CAPTCHA_RETRIES, user_id)
            logged_in, attempt_status = _solve_and_login(
                driver,
                user_id,
                password,
                attempt_no=attempt,
                progress_callback=progress_callback,
            )
            last_status = attempt_status
            if logged_in:
                break
            if attempt_status == STATUS_INVALID_CREDENTIALS:
                logger.error("Stopping retries due to invalid credentials for user %s", user_id)
                break
            logger.warning("Login attempt %d failed, retrying...", attempt)
            _report_progress(progress_callback, 24, "Retrying after IMS verification miss")
            time.sleep(ACTION_DELAY)

        if not logged_in:
            logger.error("All login attempts failed for user %s", user_id)
            return {}, {}, last_status

        # Navigate to attendance
        if not _navigate_to_attendance(driver, year, semester, progress_callback=progress_callback):
            logger.error("Failed to navigate to attendance page")
            _set_login_diagnostic("Login successful, but attendance navigation failed")
            return {}, {}, STATUS_NAVIGATION_FAILED

        # Parse the table (summary + timeline)
        _report_progress(progress_callback, 93, "Reading subject-wise attendance")
        data, timeline = _parse_attendance_table_with_timeline(driver, include_timeline=include_timeline)
        timeline = _dedupe_timeline_entries(timeline)
        _last_datewise_timeline = timeline

        logger.info("Scraped attendance for %d subjects", len(data))
        if not data:
            _set_login_diagnostic("Reached attendance page but table data is empty (year/semester/proceed/table selector mismatch)")
            return {}, timeline, STATUS_NAVIGATION_FAILED
        _report_progress(progress_callback, 97, "Preparing Telegram reply")

        if driver and _store_warm_session(user_id, password, driver):
            driver = None

        return data, timeline, STATUS_SUCCESS

    except Exception:
        logger.exception("Unexpected error in fetch_attendance")
        return {}, {}, STATUS_UNKNOWN_ERROR

    finally:
        if warm_session is not None and warm_session.lock.locked():
            warm_session.last_used = time.time()
            try:
                warm_session.lock.release()
            except Exception:
                pass
        if driver:
            _close_driver_safely(driver)


def fetch_attendance(
    user_id: str,
    password: str,
    year: str | None = None,
    semester: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Backward-compatible wrapper returning only scraped attendance data."""
    data, _timeline, _status = fetch_attendance_detailed(user_id, password, year, semester)
    return data


def fetch_current_year_and_sem(
    user_id: str,
    password: str,
) -> tuple[str | None, str | None, str]:
    """
    Fetch attendance once and return the active academic year and semester detected from the portal.
    """
    _data, _timeline, status = fetch_attendance_detailed(
        user_id,
        password,
        year=None,
        semester=None,
        include_timeline=False,
    )
    selected = get_last_selected_filters()
    return selected.get("year"), selected.get("semester"), status


def fetch_today_timetable(
    user_id: str,
    password: str,
    progress_callback: Callable[[int, str], None] | None = None,
) -> tuple[list[dict[str, str]], str]:
    """
    Fetch only today's timetable slots in fast mode.

    Returns ``(slots, status)`` where each slot is:
    ``{"time": "12-1pm", "subject": "Thermal Engineering II"}``.
    """
    slots, status = _login_and_fetch_today_timetable_via_requests(
        user_id,
        password,
        progress_callback=progress_callback,
    )
    return slots, status


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG)

    if len(sys.argv) < 3:
        print("Usage: python scraper.py <user_id> <password> [year] [semester]")
        sys.exit(1)

    uid = sys.argv[1]
    pwd = sys.argv[2]
    yr = sys.argv[3] if len(sys.argv) > 3 else None
    sem = sys.argv[4] if len(sys.argv) > 4 else None

    result = fetch_attendance(uid, pwd, yr, sem)
    if result:
        from attendance_calc import calculate_attendance
        from utils import format_attendance_report
        report = calculate_attendance(result)
        print(format_attendance_report(report))
    else:
        print("Failed to fetch attendance.")
