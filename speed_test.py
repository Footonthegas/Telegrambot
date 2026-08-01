"""speed_test.py — Benchmark the scraping speed of the attendance bot."""

from __future__ import annotations

import time
import sys
import os
import logging
import getpass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scraper import (
    STATUS_SUCCESS,
    STATUS_INVALID_CREDENTIALS,
    STATUS_INVALID_CAPTCHA,
    STATUS_NAVIGATION_FAILED,
    STATUS_UNKNOWN_ERROR,
    warmup_scraper_runtime,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("speed_test")

RESULTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "speed_test_results.log")


def _timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _log_result(label: str, seconds: float) -> None:
    line = f"{_timestamp()} | {label} | {seconds:.3f}s"
    logger.info(line)
    with open(RESULTS_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _load_credentials() -> tuple[str, str]:
    user_id = os.environ.get("SPEED_TEST_USER_ID", "")
    password = os.environ.get("SPEED_TEST_PASSWORD", "")

    if not user_id:
        user_id = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if user_id:
            logger.info("Using TELEGRAM_BOT_TOKEN as user_id placeholder — set SPEED_TEST_USER_ID for real credentials.")

    if not user_id or not password:
        logger.info("Credentials not found in environment variables.")
        logger.info("You can set SPEED_TEST_USER_ID and SPEED_TEST_PASSWORD env vars,")
        logger.info("or enter them when prompted below.")
        try:
            user_id = input("Enter roll number (user_id) to test with: ").strip()
            password = getpass.getpass("Enter password to test with: ").strip()
        except (EOFError, KeyboardInterrupt):
            logger.error("Interactive input cancelled.")
            sys.exit(1)

    return user_id, password


def test_warmup() -> float:
    """Time the scraper runtime warmup (ChromeDriver resolution)."""
    logger.info("=== Warmup Test ===")
    start = time.monotonic()
    warmup_scraper_runtime()
    elapsed = time.monotonic() - start
    _log_result("warmup", elapsed)
    return elapsed


def test_chrome_startup() -> float:
    """Time just the Chrome browser startup without navigating."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    logger.info("=== Chrome Startup Test ===")
    start = time.monotonic()
    opts = ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(60)
    elapsed = time.monotonic() - start
    driver.quit()
    _log_result("chrome_startup", elapsed)
    logger.info("Chrome startup + teardown completed in %.3fs", elapsed)
    return elapsed


def test_full_scrape(user_id: str, password: str) -> dict[str, float]:
    """Run a full login + attendance + timetable scrape and time each phase."""
    from scraper import fetch_attendance_detailed, fetch_today_timetable

    timings: dict[str, float] = {}

    logger.info("=== Full Scrape Test ===")

    # Phase 1: Attendance fetch
    logger.info("Phase 1: Attendance fetch")
    start = time.monotonic()
    att_results, att_timeline, att_status = fetch_attendance_detailed(user_id, password)
    att_elapsed = time.monotonic() - start
    timings["attendance_fetch"] = att_elapsed
    _log_result("attendance_fetch", att_elapsed)
    logger.info(
        "Attendance fetch completed in %.3fs — status: %s — subjects: %d",
        att_elapsed,
        att_status,
        len(att_results) if att_results else 0,
    )

    # Phase 2: Timetable fetch
    logger.info("Phase 2: Timetable fetch")
    start = time.monotonic()
    timetable_slots, timetable_status = fetch_today_timetable(user_id, password)
    timetable_elapsed = time.monotonic() - start
    timings["timetable_fetch"] = timetable_elapsed
    _log_result("timetable_fetch", timetable_elapsed)
    logger.info(
        "Timetable fetch completed in %.3fs — status: %s — slots: %d",
        timetable_elapsed,
        timetable_status,
        len(timetable_slots) if timetable_slots else 0,
    )

    # Phase 3: Total end-to-end
    total = sum(timings.values())
    timings["total_e2e"] = total
    _log_result("total_e2e", total)

    # Summary
    logger.info("--- Speed Test Summary ---")
    for key, val in timings.items():
        logger.info("  %-25s %8.3fs", key, val)
    logger.info("  %-25s %8.3fs", "TOTAL", total)

    return timings


def test_multiple_runs(user_id: str, password: str, runs: int = 3) -> list[dict[str, float]]:
    """Run the full scrape multiple times and report averages."""
    all_timings: list[dict[str, float]] = []
    for i in range(1, runs + 1):
        logger.info("--- Run %d/%d ---", i, runs)
        t = test_full_scrape(user_id, password)
        all_timings.append(t)
        if i < runs:
            logger.info("Waiting 2s between runs...")
            time.sleep(2)

    if all_timings:
        avg = {}
        for key in all_timings[0]:
            vals = [t[key] for t in all_timings if key in t]
            avg[key] = sum(vals) / len(vals)
        logger.info("--- Averages over %d runs ---", runs)
        for key, val in avg.items():
            logger.info("  %-25s %8.3fs", key, val)
        _log_result("avg_over_" + str(runs) + "runs", avg.get("total_e2e", 0))

    return all_timings


def main() -> None:
    logger.info("Attendance Bot — Speed Test")
    logger.info("=" * 50)

    # Ensure results file exists even if we exit early
    if not os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, "w", encoding="utf-8") as f:
            f.write(f"{_timestamp()} | speed_test_started | 0.000s\n")

    runs = int(os.environ.get("SPEED_TEST_RUNS", "3"))

    # Warmup first
    test_warmup()

    # Chrome startup benchmark (no credentials needed)
    test_chrome_startup()

    # Load credentials for full scrape test
    user_id, password = _load_credentials()

    # Full scrape test
    test_multiple_runs(user_id, password, runs=runs)

    logger.info("Speed test complete. Results appended to %s", RESULTS_FILE)


if __name__ == "__main__":
    main()