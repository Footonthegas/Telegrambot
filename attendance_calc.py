"""
attendance_calc.py – Parse the raw attendance table and compute statistics.
"""

import math
import re
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Cell values that mean "no class held" → skip
_SKIP_VALUES = {"TL", "GH", "MB", "MS", "NA", "", "-", "---"}


def parse_cell(value: str) -> tuple[int, int]:
    """
    Parse a single attendance cell value.

    Returns (classes_held, classes_attended).

    Examples
    --------
    >>> parse_cell("1")
    (1, 1)
    >>> parse_cell("0")
    (1, 0)
    >>> parse_cell("1+1")
    (2, 2)
    >>> parse_cell("1+0")
    (2, 1)
    >>> parse_cell("0+0")
    (2, 0)
    >>> parse_cell("TL")
    (0, 0)
    """
    val = value.strip().upper()

    if val in _SKIP_VALUES:
        return (0, 0)

    # Handle compound values like "1+1", "0+1", "1+0+1"
    if "+" in val:
        parts = val.split("+")
        held = 0
        present = 0
        for part in parts:
            p = part.strip()
            if p in _SKIP_VALUES:
                continue
            try:
                n = int(p)
                held += 1
                present += n
            except ValueError:
                continue
        return (held, present)

    # Single digit
    try:
        n = int(val)
        return (1, n)
    except ValueError:
        return (0, 0)


def calculate_attendance(subject_data: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """
    Compute attendance statistics from already-aggregated subject totals.

    Parameters
    ----------
    subject_data : dict
        ``{ "SUBJECT_CODE": {"total": int, "present": int}, ... }``

    Returns
    -------
    dict
        ``{ "SUBJECT_CODE": {"total", "present", "percent", "missable", "classes_needed", "below_75"}, ... }``
    """
    results: dict[str, dict[str, Any]] = {}

    for subject, data in subject_data.items():
        total = data.get("total", 0)
        present = data.get("present", 0)

        if total == 0:
            continue  # skip subjects with no classes

        percent = round((present / total) * 100, 1)
        below_75 = percent < 75.0
        # missable is only meaningful when the subject is already at or above 75%.
        missable = math.floor(present / 0.75) - total
        classes_needed = max(0, (3 * total) - (4 * present))

        if missable < 0:
            missable = 0

        results[subject] = {
            "total": total,
            "present": present,
            "percent": percent,
            "missable": missable,
            "classes_needed": classes_needed,
            "below_75": below_75,
        }
        name = (data.get("name") or "").strip() if isinstance(data, dict) else ""
        if name:
            results[subject]["name"] = name

    return results


def aggregate_from_rows(
    rows: list[dict[str, str]],
    subject_codes: list[str],
) -> dict[str, dict[str, int]]:
    """
    Aggregate attendance from raw row data.

    Parameters
    ----------
    rows : list[dict]
        Each dict maps subject_code → cell_value for one date row.
    subject_codes : list[str]
        Ordered list of subject codes (column headers).

    Returns
    -------
    dict
        ``{ "SUBJECT_CODE": {"total": int, "present": int} }``
    """
    agg: dict[str, dict[str, int]] = {
        code: {"total": 0, "present": 0} for code in subject_codes
    }

    for row in rows:
        for code in subject_codes:
            cell = row.get(code, "")
            held, attended = parse_cell(cell)
            agg[code]["total"] += held
            agg[code]["present"] += attended

    return agg


def is_valid_subject(code: str) -> bool:
    """Filter out garbage / unallotted subjects."""
    if not code or len(code) < 3:
        return False
    upper = code.upper()
    if "NOT ALLOTTED" in upper or "NA" == upper:
        return False
    # Must contain at least some alphanumeric chars
    return bool(re.search(r"[A-Z0-9]", upper))


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Sample data mimicking the user's screenshots
    sample = {
        "MEMMEC402": {"total": 16, "present": 15},
        "MEMMEC404": {"total": 15, "present": 13},
        "MEMMEC403": {"total": 16, "present": 16},
        "MEICC405":  {"total": 15, "present": 11},
        "EMPTY":     {"total": 0,  "present": 0},
    }

    results = calculate_attendance(sample)
    for subj, info in results.items():
        if info["below_75"]:
            status = f'❌ needs {info["classes_needed"]} more class{"es" if info["classes_needed"] != 1 else ""} to reach 75%'
        else:
            status = f'✅ can miss {info["missable"]}'
        print(f'{subj}: {info["percent"]}% ({info["present"]}/{info["total"]}) {status}')

    # Cell parsing tests
    assert parse_cell("1") == (1, 1)
    assert parse_cell("0") == (1, 0)
    assert parse_cell("1+1") == (2, 2)
    assert parse_cell("1+0") == (2, 1)
    assert parse_cell("0+0") == (2, 0)
    assert parse_cell("TL") == (0, 0)
    assert parse_cell("GH") == (0, 0)
    assert parse_cell("") == (0, 0)
    print("\n✅ All parse_cell tests passed.")
