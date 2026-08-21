"""
speed_compare.py – Benchmark the current Selenium scraper vs the new async HTTP scraper.

Usage:
    python speed_compare.py <user_id> <password> [year] [semester] [runs]

No files are modified and nothing is pushed to GitHub or deployed.
"""

from scraper import STATUS_SUCCESS

import asyncio
import logging
import os
import sys
import time
from typing import Any, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def benchmark_current_scraper(
    user_id: str,
    password: str,
    year: Optional[str],
    semester: Optional[str],
    runs: int,
) -> dict[str, Any]:
    """Benchmark the current Selenium-based scraper."""
    from scraper import fetch_attendance_detailed, STATUS_SUCCESS

    times: list[float] = []
    statuses: list[str] = []
    subject_counts: list[int] = []

    for i in range(runs):
        start = time.monotonic()
        data, timeline, status = fetch_attendance_detailed(
            user_id, password, year, semester, include_timeline=True,
        )
        elapsed = time.monotonic() - start
        times.append(elapsed)
        statuses.append(status)
        subject_counts.append(len(data))
        logger.info(
            "Current scraper run %d/%d: %.3fs, status=%s, subjects=%d",
            i + 1, runs, elapsed, status, len(data),
        )
        time.sleep(0.5)

    return {
        "scraper": "current (Selenium + requests)",
        "runs": runs,
        "times": times,
        "avg_time": sum(times) / len(times) if times else 0,
        "min_time": min(times) if times else 0,
        "max_time": max(times) if times else 0,
        "statuses": statuses,
        "subject_counts": subject_counts,
    }


async def benchmark_fast_scraper(
    user_id: str,
    password: str,
    year: Optional[str],
    semester: Optional[str],
    runs: int,
) -> dict[str, Any]:
    """Benchmark the new async HTTP scraper."""
    from fast_scraper import fetch_attendance_async, STATUS_SUCCESS

    times: list[float] = []
    statuses: list[str] = []
    subject_counts: list[int] = []

    for i in range(runs):
        start = time.monotonic()
        data, timeline, status = await fetch_attendance_async(
            user_id, password, year, semester, include_timeline=True,
        )
        elapsed = time.monotonic() - start
        times.append(elapsed)
        statuses.append(status)
        subject_counts.append(len(data))
        logger.info(
            "Fast scraper run %d/%d: %.3fs, status=%s, subjects=%d",
            i + 1, runs, elapsed, status, len(data),
        )
        await asyncio.sleep(0.5)

    return {
        "scraper": "fast (httpx async)",
        "runs": runs,
        "times": times,
        "avg_time": sum(times) / len(times) if times else 0,
        "min_time": min(times) if times else 0,
        "max_time": max(times) if times else 0,
        "statuses": statuses,
        "subject_counts": subject_counts,
    }


def print_results(current: dict[str, Any], fast: dict[str, Any]) -> None:
    print("\n" + "=" * 60)
    print("SPEED COMPARISON RESULTS")
    print("=" * 60)

    print(f"\n{'Metric':<25} {'Current (Selenium)':<20} {'Fast (httpx async)':<20}")
    print("-" * 65)

    print(f"{'Average time':<25} {current['avg_time']:<20.3f} {fast['avg_time']:<20.3f}")
    print(f"{'Min time':<25} {current['min_time']:<20.3f} {fast['min_time']:<20.3f}")
    print(f"{'Max time':<25} {current['max_time']:<20.3f} {fast['max_time']:<20.3f}")
    print(f"{'Runs':<25} {current['runs']:<20} {fast['runs']:<20}")

    if current['avg_time'] > 0 and fast['avg_time'] > 0:
        speedup = current['avg_time'] / fast['avg_time']
        pct_faster = (1 - fast['avg_time'] / current['avg_time']) * 100
        print(f"\nSpeedup: {speedup:.2f}x faster")
        print(f"Time saved: {pct_faster:.1f}%")

    print(f"\n{'Statuses':<25} {str(current['statuses']):<20} {str(fast['statuses']):<20}")
    print(f"{'Subjects fetched':<25} {str(current['subject_counts']):<20} {str(fast['subject_counts']):<20}")

    all_success_current = all(s == STATUS_SUCCESS for s in current['statuses'])
    all_success_fast = all(s == STATUS_SUCCESS for s in fast['statuses'])
    print(f"\nAll runs successful:")
    print(f"  Current: {'YES' if all_success_current else 'NO'}")
    print(f"  Fast:    {'YES' if all_success_fast else 'NO'}")

    print("\n" + "=" * 60)


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python speed_compare.py <user_id> <password> [year] [semester] [runs]")
        print("Example: python speed_compare.py 2024/ME4113 mypassword 2025-26 4 3")
        sys.exit(1)

    user_id = sys.argv[1]
    password = sys.argv[2]
    year = sys.argv[3] if len(sys.argv) > 3 else None
    semester = sys.argv[4] if len(sys.argv) > 4 else None
    runs = int(sys.argv[5]) if len(sys.argv) > 5 else 3

    print(f"Running speed comparison ({runs} runs each)...")
    print(f"User: {user_id}, Year: {year}, Semester: {semester}")
    print()

    print("[1/2] Benchmarking current Selenium-based scraper...")
    current = benchmark_current_scraper(user_id, password, year, semester, runs)

    print("\n[2/2] Benchmarking new async HTTP scraper...")
    fast = asyncio.run(
        benchmark_fast_scraper(user_id, password, year, semester, runs)
    )

    print_results(current, fast)


if __name__ == "__main__":
    main()