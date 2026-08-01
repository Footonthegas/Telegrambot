"""
utils.py – Formatting helpers and logging setup.
"""

import logging
import os
from datetime import datetime
from typing import Any


def setup_logging() -> None:
    """Configure root logger to write to bot.log and console."""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_file = os.path.join(os.path.dirname(__file__), "bot.log")

    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(formatter)

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level, logging.INFO))
    # Avoid duplicate handlers on reload
    if not root.handlers:
        root.addHandler(fh)
        root.addHandler(ch)


def format_attendance_report(data: dict[str, dict[str, Any]]) -> str:
    """
    Format attendance data into a WhatsApp-friendly message.

    Parameters
    ----------
    data : dict
        Output of ``attendance_calc.calculate_attendance()``.

    Returns
    -------
    str
        Formatted message string.
    """
    if not data:
        return "❌ No attendance data found. The portal may be down or your credentials might be wrong."

    lines: list[str] = ["📊 *Your Attendance Report*\n"]

    # Sort: subjects below 75% first, then by name
    sorted_subjects = sorted(data.items(), key=lambda x: (not x[1]["below_75"], x[0]))

    for subject, info in sorted_subjects:
        pct = info["percent"]
        present = info["present"]
        total = info["total"]
        missable = info["missable"]
        classes_needed = info.get("classes_needed", 0)
        below = info["below_75"]

        if below:
            lines.append(
                f"⚠️ *{subject}*: {pct}% ({present}/{total})\n"
                f"📈 Need *{classes_needed}* more class{'es' if classes_needed != 1 else ''} to reach 75%."
            )
        elif missable == 0:
            lines.append(
                f"🟡 *{subject}*: {pct}% ({present}/{total})\n"
                f"⚠️ You cannot miss any more classes."
            )
        else:
            lines.append(
                f"✅ *{subject}*: {pct}% ({present}/{total})\n"
                f"👍 You can miss *{missable}* more class{'es' if missable != 1 else ''}."
            )

    now = datetime.now().strftime("%d-%b-%Y %H:%M")
    lines.append(f"\n🕐 _Last updated: {now}_")

    # === FUTURE PAYMENT INTEGRATION ===
    # If you add premium features later, you can append:
    # lines.append("\n💎 _Upgrade to Premium for auto-alerts & detailed analytics._")

    return "\n\n".join(lines)


def format_datewise_report(
    datewise: dict[str, list[dict[str, str]]],
    max_subjects: int = 8,
    max_entries_per_subject: int = 12,
) -> str:
    """
    Format date-wise attendance with emoji status markers.

    Status mapping:
    - present -> 🟢
    - absent  -> 🔴
    - holiday/mixed/other -> 🟡
    """
    if not datewise:
        return ""

    def _emoji(status: str) -> str:
        s = (status or "").lower()
        if s == "present":
            return "🟢"
        if s == "absent":
            return "🔴"
        return "🟡"

    lines: list[str] = ["🗓️ *Date-wise Attendance*\n"]
    shown_subjects = 0

    for subject, entries in datewise.items():
        if shown_subjects >= max_subjects:
            break
        if not entries:
            continue

        lines.append(f"*{subject}*")
        for entry in entries[:max_entries_per_subject]:
            date_label = entry.get("date", "-")
            status = (entry.get("status") or "other").lower()
            raw = entry.get("raw", "")
            emoji = _emoji(status)
            status_text = status.capitalize()
            if raw:
                lines.append(f"{emoji} {date_label}: {status_text} ({raw})")
            else:
                lines.append(f"{emoji} {date_label}: {status_text}")

        if len(entries) > max_entries_per_subject:
            lines.append(f"…and {len(entries) - max_entries_per_subject} more entries")

        lines.append("")
        shown_subjects += 1

    if len(datewise) > shown_subjects:
        lines.append(f"Showing {shown_subjects}/{len(datewise)} subjects. Send a narrower query in future if needed.")

    return "\n".join(lines).strip()


HELP_TEXT = """🤖 *NSUT Attendance Bot – Help*

*Commands:*
• *LOGIN <user_id> <password>*
  Save credentials & fetch attendance.
  Example: `LOGIN 2024/ME4113 mypassword`

• *REFRESH*
  Re-fetch attendance with saved credentials.

• *LOGOUT*
  Delete your saved credentials.

• *HELP*
  Show this message.

📌 _Your password is stored encrypted and never shared._

💡 _Tip: Use REFRESH daily to track your attendance!_
"""

WELCOME_TEXT = """👋 *Welcome to NSUT Attendance Bot!*

This bot fetches your attendance from the IMS portal and tells you how many classes you can miss while keeping ≥75%.

🆓 *Completely free to use!*

To get started, send:
*LOGIN <user_id> <password>*

Example:
`LOGIN 2024/ME4113 mypassword`

Send *HELP* for more commands.
"""

LOGOUT_SUCCESS = "✅ Your credentials have been deleted. Send *LOGIN* to register again."
LOGOUT_NOT_FOUND = "ℹ️ No saved credentials found for your number."
LOGIN_USAGE = "❌ Invalid format.\n\nUsage: *LOGIN <user_id> <password>*\nExample: `LOGIN 2024/ME4113 mypassword`"
FETCHING_MSG = "⏳ Fetching your attendance from IMS portal... This may take 30-60 seconds."
ERROR_MSG = "❌ Failed to fetch attendance. The IMS portal might be down, or your credentials may be wrong. Please try again later."
REFRESH_NO_CREDS = "ℹ️ No saved credentials found. Please use *LOGIN <user_id> <password>* first."
