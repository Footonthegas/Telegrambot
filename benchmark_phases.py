"""
benchmark_phases.py – Phase-by-phase benchmark for all 3 scrapers.
Measures time spent in each phase: session init, CAPTCHA, login, navigation, parsing.
"""
import asyncio
import time
import re
import sys
import httpx
from urllib.parse import urljoin

IMS_BASE = "https://www.imsnsit.org/imsnsit/"
USER_ID = "2024UME4113"
PASSWORD = "Amanguliani@12345"
YEAR = "2025-26"
SEMESTER = "4"
RUNS = 3


# ─── Python Async Scraper (httpx) ───────────────────────────────────────────
async def benchmark_python_async():
    print(f"\n{'='*60}")
    print(f"  PYTHON ASYNC SCRAPER (httpx) – {RUNS} runs, sem {SEMESTER}, year {YEAR}")
    print(f"{'='*60}")

    # Warm up imports first
    from scraper import _parse_attendance_from_sources, _dedupe_timeline_entries
    from fast_scraper import _get_ocr, _extract_selected_option_value

    times_total = []
    phase_totals = {
        "session_init": [],
        "captcha": [],
        "login_submit": [],
        "my_activities": [],
        "my_attendance": [],
        "year_sem_submit": [],
        "parse_html": [],
    }

    for run in range(RUNS):
        p = {k: 0.0 for k in phase_totals}
        t0 = time.perf_counter()

        async with httpx.AsyncClient(
            http2=True,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            timeout=httpx.Timeout(15.0, connect=10.0),
            follow_redirects=True,
        ) as client:
            client.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/146.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            })

            # Phase 1: Session init (5 HTTP requests)
            ph = time.perf_counter()
            await client.get(IMS_BASE, timeout=10)
            await client.get(urljoin(IMS_BASE, "plum5_fw_login.php?t=sw&w=1"), timeout=10)
            await client.get(urljoin(IMS_BASE, "student.htm"), headers={"Referer": IMS_BASE}, timeout=10)
            await client.get(urljoin(IMS_BASE, "student_login110.php"), headers={"Referer": urljoin(IMS_BASE, "student.htm")}, timeout=10)
            login_page = await client.get(urljoin(IMS_BASE, "student_login.php"), headers={"Referer": urljoin(IMS_BASE, "student.htm"), "Upgrade-Insecure-Requests": "1"}, timeout=10)
            p["session_init"] = time.perf_counter() - ph

            login_html = login_page.text or ""
            fy_m = re.search(r"name='fy' id='fy' value='([^']+)'", login_html)
            comp_m = re.search(r"name='comp' id='comp' type='hidden' readonly value='([^']+)'", login_html)
            hrand_m = re.search(r"name='HRAND_NUM' id='HRAND_NUM' value='([^']+)'", login_html)
            capsrc_m = re.search(r"<img src='([^']+captcha[^']+)' id='captchaimg'", login_html)
            if not all([fy_m, comp_m, hrand_m, capsrc_m]):
                print(f"  Run {run+1}: FORM FIELDS FAILED")
                continue

            # Phase 2: CAPTCHA download + solve (in-process ddddocr)
            ph = time.perf_counter()
            captcha_resp = await client.get(urljoin(IMS_BASE, capsrc_m.group(1)), headers={"Referer": urljoin(IMS_BASE, "student_login.php")}, timeout=10)
            ocr = _get_ocr()
            captcha_text = ocr.classification(captcha_resp.content)
            p["captcha"] = time.perf_counter() - ph

            # Phase 3: Login form submit
            ph = time.perf_counter()
            login_resp = await client.post(urljoin(IMS_BASE, "student_login.php"), data={
                "f": "", "uid": USER_ID, "pwd": PASSWORD,
                "HRAND_NUM": hrand_m.group(1), "fy": fy_m.group(1),
                "comp": comp_m.group(1), "cap": captcha_text, "logintype": "student",
            }, headers={
                "Referer": urljoin(IMS_BASE, "student_login.php"),
                "Origin": "https://www.imsnsit.org",
                "Content-Type": "application/x-www-form-urlencoded",
                "Upgrade-Insecure-Requests": "1",
            }, timeout=10)
            p["login_submit"] = time.perf_counter() - ph

            banner_html = login_resp.text or ""
            banner_lower = banner_html.lower()
            if "logout" not in banner_lower or "my activities" not in banner_lower:
                print(f"  Run {run+1}: LOGIN FAILED")
                continue

            # Phase 4: My Activities navigation
            ph = time.perf_counter()
            my_act_match = re.search(r"href='(https://www\.imsnsit\.org/imsnsit/plum_url\.php\?[^']+)'[^>]*>My Activities<", banner_html, re.I)
            if not my_act_match:
                print(f"  Run {run+1}: MY ACTIVITIES NOT FOUND")
                continue
            menu_resp = await client.get(my_act_match.group(1), headers={"Referer": urljoin(IMS_BASE, "student_login.php")}, timeout=10)
            p["my_activities"] = time.perf_counter() - ph

            # Phase 5: My Attendance navigation
            ph = time.perf_counter()
            att_match = re.search(r"href='(https://www\.imsnsit\.org/imsnsit/plum_url\.php\?[^']+)'[^>]*>My Attendance<", menu_resp.text or "", re.I)
            if not att_match:
                print(f"  Run {run+1}: MY ATTENDANCE NOT FOUND")
                continue
            att_page = await client.get(att_match.group(1), headers={"Referer": my_act_match.group(1)}, timeout=10)
            p["my_attendance"] = time.perf_counter() - ph

            att_html = att_page.text or ""
            enc_year_m = re.search(r"name='enc_year' id='enc_year' value='([^']+)'", att_html)
            enc_sem_m = re.search(r"name='enc_sem' id='enc_sem' value='([^']+)'", att_html)
            recentity_m = re.search(r"name=recentitycode value='([^']+)'", att_html)
            dept_m = re.search(r"name=dept value='([^']+)'", att_html)
            degree_m = re.search(r"name=degree value='([^']+)'", att_html)
            if not all([enc_year_m, enc_sem_m, recentity_m, dept_m, degree_m]):
                print(f"  Run {run+1}: ATTENDANCE FORM FIELDS FAILED")
                continue

            # Phase 6: Year/Sem submit
            ph = time.perf_counter()
            result_resp = await client.post(att_match.group(1), data={
                "year": YEAR, "enc_year": enc_year_m.group(1),
                "sem": SEMESTER, "enc_sem": enc_sem_m.group(1),
                "submit": "Submit", "recentitycode": recentity_m.group(1),
                "dept": dept_m.group(1), "degree": degree_m.group(1),
                "ename": "", "ecode": "",
            }, headers={
                "Referer": att_match.group(1),
                "Origin": "https://www.imsnsit.org",
                "Content-Type": "application/x-www-form-urlencoded",
                "Upgrade-Insecure-Requests": "1",
            }, timeout=10)
            p["year_sem_submit"] = time.perf_counter() - ph

            # Phase 7: HTML parse
            ph = time.perf_counter()
            data, timeline = _parse_attendance_from_sources([result_resp.text or ""], include_timeline=True)
            p["parse_html"] = time.perf_counter() - ph

        total = time.perf_counter() - t0
        times_total.append(total)
        for k in phase_totals:
            phase_totals[k].append(p[k])
        print(f"  Run {run+1}: {total*1000:.0f}ms, subjects={len(data)}")

    # Print averages
    print(f"\n  --- Python Async Phase Averages ({RUNS} runs) ---")
    for k in ["session_init", "captcha", "login_submit", "my_activities", "my_attendance", "year_sem_submit", "parse_html"]:
        avg = sum(phase_totals[k]) / len(phase_totals[k]) * 1000
        print(f"    {k:25s}: {avg:7.0f}ms")
    avg_total = sum(times_total) / len(times_total) * 1000
    print(f"    {'TOTAL':25s}: {avg_total:7.0f}ms")


# ─── Selenium Scraper ───────────────────────────────────────────────────────
def benchmark_selenium():
    print(f"\n{'='*60}")
    print(f"  SELENIUM SCRAPER – 1 run, sem {SEMESTER}, year {YEAR}")
    print(f"{'='*60}")

    import os
    os.environ["FAST_MODE"] = "1"

    from scraper import fetch_attendance_detailed

    phases = {
        "total": 0.0,
    }

    t0 = time.perf_counter()
    data, timeline, status = fetch_attendance_detailed(
        USER_ID, PASSWORD, YEAR, SEMESTER,
        include_timeline=True,
    )
    total = time.perf_counter() - t0

    print(f"  Status: {status}, subjects={len(data)}, time={total*1000:.0f}ms")

    # Now measure phases individually by instrumenting key calls
    # Since we can't easily split the Selenium flow, we measure:
    # 1. Browser startup + session init + CAPTCHA + login (everything before attendance page)
    # 2. Navigation to attendance
    # 3. Year/sem submit + parsing

    print(f"\n  --- Selenium Breakdown (from diagnostics) ---")
    print(f"  (Selenium doesn't expose phase timing, so we measure the full flow)")
    print(f"  Full login-to-attendance: {total*1000:.0f}ms")


# ─── Main ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging_level = "WARNING"
    import logging
    logging.basicConfig(level=logging_level)

    asyncio.run(benchmark_python_async())
    benchmark_selenium()

    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
