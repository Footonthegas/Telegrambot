"""
telegram_bot.py - Telegram bot runner with a simple chat-first flow.

Commands:
- /start, /help
- /login
- /login <user_id> <password>
- /refresh
- /refresh_force
- /logout
- /setyear <YYYY-YY>
- /setsem <N>
- /whoami
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Callable

import requests
from dotenv import load_dotenv

from attendance_calc import calculate_attendance
from backend_server import get_backend_base_url, start_backend_server_in_thread
from db import (
    clear_user_state,
    delete_attendance_cache,
    get_all_known_chat_ids,
    get_bot_stats,
    get_attendance_cache,
    get_profile,
    get_user,
    get_service_control,
    init_db,
    mark_chat_seen,
    save_attendance_cache,
    save_profile,
    save_user,
    set_service_enabled,
    update_profile_filters,
)
from scraper import (
    STATUS_INVALID_CAPTCHA,
    STATUS_INVALID_CREDENTIALS,
    STATUS_NAVIGATION_FAILED,
    STATUS_SUCCESS,
    fetch_attendance_detailed,
    fetch_today_timetable,
    get_last_login_diagnostic,
    get_last_selected_filters,
    warmup_scraper_runtime,
)
from utils import setup_logging

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
setup_logging()
logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
POLL_TIMEOUT = int(os.getenv("TELEGRAM_POLL_TIMEOUT", "25"))
CACHE_TTL_SECONDS = int(os.getenv("ATTENDANCE_CACHE_TTL_SECONDS", "900"))
TIMETABLE_CACHE_TTL_SECONDS = int(os.getenv("TIMETABLE_CACHE_TTL_SECONDS", "900"))
DEFAULT_ACADEMIC_YEAR = os.getenv("DEFAULT_ACADEMIC_YEAR", "2025-26").strip() or "2025-26"
ADMIN_CHAT_IDS = {
    value.strip()
    for value in os.getenv("TELEGRAM_ADMIN_CHAT_IDS", "").split(",")
    if value.strip()
}

# Pending manual filters before a profile exists.
_sessions: dict[str, dict[str, str]] = {}
# Roll-number/password conversational state.
_pending_auth: dict[str, dict[str, str]] = {}
# In-memory cache layered on top of the persisted cache table.
_attendance_cache: dict[str, dict[str, Any]] = {}
_cache_lock = threading.Lock()
_volatile_credentials: dict[str, dict[str, str]] = {}
_volatile_profiles: dict[str, dict[str, str | None]] = {}
_telegram_local = threading.local()

TELEGRAM_SEND_TIMEOUT = (3.0, 8.0)
TELEGRAM_EDIT_TIMEOUT = (2.5, 6.0)
TELEGRAM_DELETE_TIMEOUT = (2.5, 5.0)
TELEGRAM_CALLBACK_TIMEOUT = (2.0, 3.0)


def _api_url(method: str) -> str:
    return f"{API_BASE}{TOKEN}/{method}"


def _telegram_session() -> requests.Session:
    session = getattr(_telegram_local, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=0)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _telegram_local.session = session
    return session


def _telegram_post_json(
    method: str,
    payload: dict[str, Any],
    *,
    timeout: tuple[float, float],
) -> requests.Response:
    return _telegram_session().post(
        _api_url(method),
        json=payload,
        timeout=timeout,
    )


def _send_message(
    chat_id: str,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not text:
        return None

    chunk_size = 3800
    chunks = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)] or [text]
    first_result: dict[str, Any] | None = None
    for idx, chunk in enumerate(chunks):
        try:
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if reply_markup and idx == 0:
                payload["reply_markup"] = reply_markup
            resp = _telegram_post_json("sendMessage", payload, timeout=TELEGRAM_SEND_TIMEOUT)
            if resp.status_code >= 300:
                logger.warning(
                    "Telegram sendMessage failed chat_id=%s status=%s body=%s",
                    chat_id,
                    resp.status_code,
                    resp.text[:400],
                )
            elif idx == 0:
                try:
                    body = resp.json()
                    if body.get("ok"):
                        first_result = body.get("result")
                except Exception:
                    logger.debug("Could not decode sendMessage response JSON", exc_info=True)
        except Exception:
            logger.exception("Failed sending Telegram message to chat_id=%s", chat_id)
    return first_result


def _edit_message(
    chat_id: str,
    message_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    if not text or not message_id:
        return
    try:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        resp = _telegram_post_json("editMessageText", payload, timeout=TELEGRAM_EDIT_TIMEOUT)
        if resp.status_code >= 300 and "message is not modified" not in resp.text.lower():
            logger.warning(
                "Telegram editMessageText failed chat_id=%s message_id=%s status=%s body=%s",
                chat_id,
                message_id,
                resp.status_code,
                resp.text[:400],
            )
    except Exception:
        logger.debug("Failed editing Telegram message chat_id=%s message_id=%s", chat_id, message_id, exc_info=True)


def _delete_message(chat_id: str, message_id: int) -> None:
    if not chat_id or not message_id:
        return
    try:
        resp = _telegram_post_json(
            "deleteMessage",
            {"chat_id": chat_id, "message_id": message_id},
            timeout=TELEGRAM_DELETE_TIMEOUT,
        )
        if resp.status_code >= 300:
            logger.warning(
                "Telegram deleteMessage failed chat_id=%s message_id=%s status=%s body=%s",
                chat_id,
                message_id,
                resp.status_code,
                resp.text[:400],
            )
    except Exception:
        logger.debug("Failed deleting Telegram message chat_id=%s message_id=%s", chat_id, message_id, exc_info=True)


def _delete_message_async(chat_id: str, message_id: int) -> None:
    if not chat_id or not message_id:
        return
    threading.Thread(target=_delete_message, args=(chat_id, message_id), daemon=True).start()


def _run_async(name: str, target: Callable[..., Any], *args: Any) -> None:
    def runner() -> None:
        try:
            target(*args)
        except Exception:
            logger.exception("Async task failed: %s", name)

    threading.Thread(target=runner, daemon=True, name=name).start()


def _answer_callback_query(callback_query_id: str) -> None:
    try:
        resp = _telegram_post_json(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id},
            timeout=TELEGRAM_CALLBACK_TIMEOUT,
        )
        if resp.status_code >= 300:
            logger.warning(
                "Telegram answerCallbackQuery failed id=%s status=%s body=%s",
                callback_query_id,
                resp.status_code,
                resp.text[:400],
            )
    except Exception:
        logger.debug("Failed answering callback query id=%s", callback_query_id, exc_info=True)


def _answer_callback_query_async(callback_query_id: str) -> None:
    if not callback_query_id:
        return
    threading.Thread(target=_answer_callback_query, args=(callback_query_id,), daemon=True).start()


def _normalize_cmd(text: str) -> tuple[str, list[str]]:
    raw = (text or "").strip()
    if not raw:
        return "", []
    parts = raw.split()
    if not parts[0].startswith("/"):
        return "", parts
    cmd = parts[0].lower()
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    return cmd, parts[1:]


def _command_payload(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    parts = raw.split(maxsplit=1)
    if len(parts) < 2:
        return ""
    return parts[1].strip()


def _help_text() -> str:
    return (
        "NSUT Attendance Bot\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Fastest way to start:\n"
        "1. Send your roll number\n"
        "2. I will ask for your password\n"
        "3. I save your encrypted login locally\n"
        "4. You press /attendance whenever you want the latest attendance\n\n"
        "Commands:\n"
        "/login\n"
        "/attendance\n"
        "/timetable\n"
        "/login <user_id> <password>\n"
        "/refresh\n"
        "/refresh_force\n"
        "/setyear <YYYY-YY>\n"
        "/setsem <N>\n"
        "/whoami\n"
        "/logout\n"
        "/help\n\n"
        "Notes:\n"
        "- Your IMS password is stored only in local encrypted SQLite for faster reuse.\n"
        "- Password is not sent to Supabase.\n"
        "- /attendance and /refresh use your saved session automatically.\n"
        "- /refresh_force bypasses cache and fetches live using your saved session.\n"
        f"- Default academic year is {DEFAULT_ACADEMIC_YEAR} unless you change it with /setyear.\n"
        "- /server shows admin-only start/stop controls for the live service."
    )


def _welcome_text() -> str:
    return (
        "💸 Rs20 on WhatsApp just to see your own attendance?\n"
        "🫠 That is a wild business model.\n"
        "😌 This bot does it for free.\n\n"
        "🚀 Send your roll number to begin.\n"
        "🔐 I will ask for your IMS password in the next message.\n"
        "📊 Then just press /attendance whenever you want the latest attendance."
    )


def _help_text() -> str:
    return (
        "\U0001F916 NSUT Attendance Bot\n"
        "--------------------\n"
        "Fastest way to start:\n"
        "1. Send your roll number\n"
        "2. I will ask for your password\n"
        "3. I save your encrypted login locally\n"
        "4. You press /attendance whenever you want the latest attendance\n\n"
        "Commands:\n"
        "/login\n"
        "/attendance\n"
        "/timetable\n"
        "/login <user_id> <password>\n"
        "/refresh\n"
        "/refresh_force\n"
        "/setyear <YYYY-YY>\n"
        "/setsem <N>\n"
        "/whoami\n"
        "/logout\n"
        "/help\n\n"
        "Notes:\n"
        "- Your IMS password is stored only in local encrypted SQLite for faster reuse.\n"
        "- Password is not sent to Supabase.\n"
        "- /attendance fetches live and shows progress while it works.\n"
        "- /refresh uses your saved snapshot first when it is still fresh.\n"
        "- /refresh_force bypasses cache and fetches live using your saved session.\n"
        f"- Default academic year is {DEFAULT_ACADEMIC_YEAR} unless you change it with /setyear.\n"
        "- /server shows admin-only start/stop controls for the live service."
    )


def _welcome_text() -> str:
    return (
        "\U0001F916 NSUT Attendance Bot\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Welcome. If you're here, it means you're smart enough not to pay some scammy WhatsApp bot for data the university gives you for free. Good choice. \U0001F9E0\u2728\n\n"
        "\U0001F393 Send your roll number first.\n"
        "\U0001F510 I will ask for your IMS password in the next message.\n"
        "\U0001F4CA Then tap /attendance whenever you want the latest attendance.\n"
    )


def _cache_get(chat_id: str) -> dict[str, Any] | None:
    def _legacy_snapshot(cache: dict[str, Any] | None) -> bool:
        if not cache:
            return False
        messages = cache.get("messages") or []
        if not messages:
            return False
        first = str(messages[0] or "")
        return "ATTENDANCE SNAPSHOT" in first

    with _cache_lock:
        cache = _attendance_cache.get(chat_id)
        if _legacy_snapshot(cache):
            _attendance_cache.pop(chat_id, None)
            cache = None
        if cache and (cache.get("messages") or cache.get("status") or cache.get("user_id")):
            return dict(cache)

    persisted = get_attendance_cache(chat_id)
    if not persisted:
        return None
    if _legacy_snapshot(persisted):
        delete_attendance_cache(chat_id)
        return None

    persisted.setdefault("fetching", False)
    persisted.setdefault("timeline_fetching", False)
    persisted.setdefault("timetable_fetching", False)
    persisted.setdefault("timetable_day", "")
    persisted.setdefault("timetable_ts", 0.0)
    persisted.setdefault("timetable_slots", [])
    with _cache_lock:
        current = _attendance_cache.setdefault(chat_id, persisted)
        return dict(current)


def _cache_set(
    chat_id: str,
    user_id: str,
    messages: list[str],
    status: str,
    year: str | None,
    semester: str | None,
    fetched_at: float | None = None,
    results: dict[str, dict[str, Any]] | None = None,
    timeline: dict[str, list[dict[str, str]]] | None = None,
) -> None:
    ts = float(fetched_at if fetched_at is not None else time.time())
    payload = {
        "ts": ts,
        "messages": list(messages),
        "status": status,
        "fetching": False,
        "timeline_fetching": False,
        "timetable_fetching": False,
        "timetable_day": "",
        "timetable_ts": 0.0,
        "timetable_slots": [],
        "user_id": user_id,
        "year": year,
        "semester": semester,
        "results": dict(results or {}),
        "timeline": dict(timeline or {}),
    }
    with _cache_lock:
        _attendance_cache[chat_id] = payload

    def _persist() -> None:
        try:
            save_attendance_cache(
                chat_id=chat_id,
                user_id=user_id,
                messages=list(messages),
                status=status,
                year=year,
                semester=semester,
                fetched_at=ts,
            )
        except Exception:
            logger.exception("Failed persisting attendance cache for chat_id=%s", chat_id)

    threading.Thread(target=_persist, daemon=True).start()


def _cache_delete(chat_id: str) -> None:
    with _cache_lock:
        _attendance_cache.pop(chat_id, None)
    if get_attendance_cache(chat_id) is not None:
        delete_attendance_cache(chat_id)


def _cache_mark_fetching(chat_id: str, value: bool) -> None:
    persisted = get_attendance_cache(chat_id)
    with _cache_lock:
        cache = _attendance_cache.get(chat_id)
        if cache is None and persisted:
            cache = dict(persisted)
            cache.setdefault("fetching", False)
            cache.setdefault("timeline_fetching", False)
            cache.setdefault("timetable_fetching", False)
            cache.setdefault("timetable_day", "")
            cache.setdefault("timetable_ts", 0.0)
            cache.setdefault("timetable_slots", [])
            _attendance_cache[chat_id] = cache
        if cache is None:
            cache = {
                "ts": 0.0,
                "messages": [],
                "status": "",
                "fetching": False,
                "timeline_fetching": False,
                "timetable_fetching": False,
                "timetable_day": "",
                "timetable_ts": 0.0,
                "timetable_slots": [],
                "user_id": "",
                "year": None,
                "semester": None,
            }
            _attendance_cache[chat_id] = cache
        cache["fetching"] = value


def _cache_mark_timeline_fetching(chat_id: str, value: bool) -> None:
    persisted = get_attendance_cache(chat_id)
    with _cache_lock:
        cache = _attendance_cache.get(chat_id)
        if cache is None and persisted:
            cache = dict(persisted)
            cache.setdefault("fetching", False)
            cache.setdefault("timeline_fetching", False)
            cache.setdefault("timetable_fetching", False)
            cache.setdefault("timetable_day", "")
            cache.setdefault("timetable_ts", 0.0)
            cache.setdefault("timetable_slots", [])
            _attendance_cache[chat_id] = cache
        if cache is None:
            cache = {
                "ts": 0.0,
                "messages": [],
                "status": "",
                "fetching": False,
                "timeline_fetching": False,
                "timetable_fetching": False,
                "timetable_day": "",
                "timetable_ts": 0.0,
                "timetable_slots": [],
                "user_id": "",
                "year": None,
                "semester": None,
                "results": {},
                "timeline": {},
            }
            _attendance_cache[chat_id] = cache
        cache["timeline_fetching"] = value


def _cache_mark_timetable_fetching(chat_id: str, value: bool) -> None:
    persisted = get_attendance_cache(chat_id)
    with _cache_lock:
        cache = _attendance_cache.get(chat_id)
        if cache is None and persisted:
            cache = dict(persisted)
            cache.setdefault("fetching", False)
            cache.setdefault("timeline_fetching", False)
            cache.setdefault("timetable_fetching", False)
            cache.setdefault("timetable_day", "")
            cache.setdefault("timetable_ts", 0.0)
            cache.setdefault("timetable_slots", [])
            _attendance_cache[chat_id] = cache
        if cache is None:
            cache = {
                "ts": 0.0,
                "messages": [],
                "status": "",
                "fetching": False,
                "timeline_fetching": False,
                "timetable_fetching": False,
                "timetable_day": "",
                "timetable_ts": 0.0,
                "timetable_slots": [],
                "user_id": "",
                "year": None,
                "semester": None,
                "results": {},
                "timeline": {},
            }
            _attendance_cache[chat_id] = cache
        cache["timetable_fetching"] = value


def _cache_age_seconds(cache: dict[str, Any] | None) -> int:
    if not cache:
        return 10**9
    ts = float(cache.get("ts", 0.0) or 0.0)
    return int(max(0, time.time() - ts))


def _cache_matches_profile(
    cache: dict[str, Any] | None,
    user_id: str,
    year: str | None,
    semester: str | None,
) -> bool:
    if not cache or not cache.get("messages"):
        return False
    if cache.get("status") != STATUS_SUCCESS:
        return False
    if str(cache.get("user_id") or "").strip() != str(user_id or "").strip():
        return False
    cache_year = str(cache.get("year") or "").strip()
    cache_sem = str(cache.get("semester") or "").strip()
    if year and cache_year and cache_year != year:
        return False
    if semester and cache_sem and cache_sem != semester:
        return False
    return True


def _resolve_filters(chat_id: str) -> tuple[str | None, str | None]:
    pending = _sessions.get(chat_id, {})
    volatile = _volatile_profiles.get(chat_id, {})
    profile = get_profile(chat_id) or {}
    year = (
        pending.get("year")
        or volatile.get("year")
        or profile.get("year")
        or DEFAULT_ACADEMIC_YEAR
    ).strip() or DEFAULT_ACADEMIC_YEAR
    semester = (
        pending.get("semester")
        or volatile.get("semester")
        or profile.get("semester")
        or ""
    ).strip() or None
    return year, semester


def _get_saved_credentials(chat_id: str) -> dict[str, Any] | None:
    volatile = _volatile_credentials.get(chat_id)
    if volatile:
        return dict(volatile)
    return get_user(chat_id)


def _clear_volatile_session(chat_id: str) -> None:
    _volatile_credentials.pop(chat_id, None)
    _volatile_profiles.pop(chat_id, None)


def _persist_login_session(chat_id: str, user_id: str, password: str, year: str, semester: str | None) -> None:
    try:
        save_user(chat_id, user_id, password)
        save_profile(chat_id, user_id, year, semester)
        _clear_volatile_session(chat_id)
        logger.info("Persisted login session for chat_id=%s user_id=%s", chat_id, user_id)
    except Exception:
        logger.exception("Failed persisting login session for chat_id=%s user_id=%s", chat_id, user_id)
        _send_message(
            chat_id,
            "Registration save hit a temporary issue. Please send /login and register once more.",
        )


def _save_login_session(chat_id: str, user_id: str, password: str) -> None:
    existing = get_profile(chat_id) or {}
    pending = _sessions.get(chat_id, {})
    existing_same_user = existing.get("user_id") == user_id

    year = (pending.get("year") or (existing.get("year") if existing_same_user else "") or DEFAULT_ACADEMIC_YEAR).strip()
    semester = (pending.get("semester") or (existing.get("semester") if existing_same_user else "") or "").strip() or None

    _volatile_credentials[chat_id] = {
        "phone_number": chat_id,
        "user_id": user_id,
        "password": password,
    }
    _volatile_profiles[chat_id] = {
        "chat_id": chat_id,
        "user_id": user_id,
        "year": year,
        "semester": semester,
    }
    _sessions.pop(chat_id, None)
    with _cache_lock:
        _attendance_cache[chat_id] = {
            "ts": 0.0,
            "messages": [],
            "status": "",
            "fetching": False,
            "user_id": user_id,
            "year": year,
            "semester": semester,
            "results": {},
            "timeline": {},
        }
    _persist_login_session(chat_id, user_id, password, year, semester)


def _is_admin(chat_id: str) -> bool:
    return chat_id in ADMIN_CHAT_IDS


def _is_live_service_enabled() -> bool:
    return bool(get_service_control().get("service_enabled"))


def _live_service_guard(chat_id: str, *, send_message: bool = True) -> bool:
    if _is_admin(chat_id):
        return True
    if _is_live_service_enabled():
        return True
    if send_message:
        _send_message(
            chat_id,
            "Live attendance fetching is temporarily paused by admin.\n"
            "The bot is still online and /refresh can still return your last saved snapshot.",
        )
    return False


def _server_controls_markup() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Start Server", "callback_data": "server:start"},
                {"text": "Stop Server", "callback_data": "server:stop"},
            ],
            [
                {"text": "Status", "callback_data": "server:status"},
            ],
        ]
    }


def _server_status_text() -> str:
    control = get_service_control()
    status = "RUNNING" if control.get("service_enabled") else "PAUSED"
    updated_by = control.get("updated_by_chat_id") or "-"
    updated_at = control.get("updated_at") or "-"
    return (
        "SERVER CONTROL\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Backend: online\n"
        f"Live fetch service: {status}\n"
        f"Backend URL: {get_backend_base_url()}\n"
        f"Last change by: {updated_by}\n"
        f"Last change at: {updated_at}\n\n"
        "Start/Stop changes only the live attendance service.\n"
        "The bot and backend stay online for 24/7 monitoring."
    )


def _format_overall_card(
    results: dict[str, dict[str, Any]],
    year: str | None,
    semester: str | None,
    updated_at: datetime | None = None,
) -> str:
    if not results:
        return "No attendance data found."

    total_subjects = len(results)
    total_classes = sum(int(info.get("total", 0) or 0) for info in results.values())
    total_present = sum(int(info.get("present", 0) or 0) for info in results.values())
    overall_percent = round((total_present / total_classes) * 100, 1) if total_classes else 0.0
    below_count = sum(1 for info in results.values() if info.get("below_75"))
    zero_buffer_count = sum(
        1
        for info in results.values()
        if not info.get("below_75") and int(info.get("missable", 0) or 0) == 0
    )
    safe_count = max(0, total_subjects - below_count)
    stamp = (updated_at or datetime.now()).strftime("%d-%b-%Y %H:%M")

    return (
        "ATTENDANCE SNAPSHOT\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Overall: {overall_percent}% ({total_present}/{total_classes})\n"
        f"Subjects tracked: {total_subjects}\n"
        f"Safe subjects: {safe_count}\n"
        f"Below 75%: {below_count}\n"
        f"No skip buffer: {zero_buffer_count}\n"
        f"Year: {year or '-'}\n"
        f"Semester: {semester or '-'}\n"
        f"Updated: {stamp}"
    )


def _format_subject_rows(results: dict[str, dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    ordered = sorted(
        results.items(),
        key=lambda item: (not item[1].get("below_75", False), float(item[1].get("percent", 0.0)), item[0]),
    )
    for subject, info in ordered:
        percent = float(info.get("percent", 0.0) or 0.0)
        present = int(info.get("present", 0) or 0)
        total = int(info.get("total", 0) or 0)
        missable = int(info.get("missable", 0) or 0)
        classes_needed = int(info.get("classes_needed", 0) or 0)
        below_75 = bool(info.get("below_75"))

        status_emoji = "🔴" if below_75 else "🟢"
        if below_75:
            note = f"Need {classes_needed} more class{'es' if classes_needed != 1 else ''} to reach 75%"
        elif missable <= 0:
            note = "No skip buffer"
        else:
            note = f"Can miss {missable}"

        lines.append(f"{status_emoji} {subject}: {percent:.1f}% ({present}/{total}) • {note}")
    return lines


def _format_attendance_card(
    results: dict[str, dict[str, Any]],
    year: str | None,
    semester: str | None,
    updated_at: datetime | None = None,
) -> str:
    subject_rows = _format_subject_rows(results)
    stamp = (updated_at or datetime.now()).strftime("%d-%b-%Y %H:%M")
    if not subject_rows:
        return (
            "SUBJECT-WISE ATTENDANCE\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Year: {year or DEFAULT_ACADEMIC_YEAR}\n"
            f"Semester: {semester or '-'}\n"
            f"Updated: {stamp}"
        )
    return (
        "SUBJECT-WISE ATTENDANCE\n"
        "━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(subject_rows)
        + f"\n\nYear: {year or DEFAULT_ACADEMIC_YEAR}\nSemester: {semester or '-'}\nUpdated: {stamp}"
    )


def _format_subject_rows(results: dict[str, dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    ordered = sorted(
        results.items(),
        key=lambda item: (not item[1].get("below_75", False), float(item[1].get("percent", 0.0)), item[0]),
    )
    for subject, info in ordered:
        percent = float(info.get("percent", 0.0) or 0.0)
        present = int(info.get("present", 0) or 0)
        total = int(info.get("total", 0) or 0)
        missable = int(info.get("missable", 0) or 0)
        classes_needed = int(info.get("classes_needed", 0) or 0)
        below_75 = bool(info.get("below_75"))

        status_emoji = "\U0001F534" if below_75 else "\U0001F7E2"
        if below_75:
            note = f"Need {classes_needed} more class{'es' if classes_needed != 1 else ''} to reach 75%"
        elif missable <= 0:
            note = "No skip buffer"
        else:
            note = f"Can miss {missable}"

        subject_name = str(info.get("name") or "").strip()
        if subject_name and subject_name.lower() != subject.lower():
            lines.append(
                f"{status_emoji} {subject_name}\n"
                f"   {subject} \u2022 {percent:.1f}% ({present}/{total}) \u2022 {note}"
            )
        else:
            lines.append(
                f"{status_emoji} {subject}\n"
                f"   {percent:.1f}% ({present}/{total}) \u2022 {note}"
            )
    return lines


def _format_attendance_card(
    results: dict[str, dict[str, Any]],
    year: str | None,
    semester: str | None,
    updated_at: datetime | None = None,
) -> str:
    subject_rows = _format_subject_rows(results)
    stamp = (updated_at or datetime.now()).strftime("%d-%b-%Y %H:%M")
    header = "\U0001F4DA Subject-wise Attendance\n--------------------"
    footer = (
        f"\n\n\U0001F4C5 Year: {year or DEFAULT_ACADEMIC_YEAR}"
        f"\n\U0001F393 Semester: {semester or '-'}"
        f"\n\U0001F551 Updated: {stamp}"
    )
    if not subject_rows:
        return f"{header}\nNo attendance data found.{footer}"
    return f"{header}\n" + "\n\n".join(subject_rows) + footer


def _shortcut_markup(*buttons: str) -> dict[str, Any] | None:
    clean = [button.strip() for button in buttons if button and button.strip()]
    if not clean:
        return None
    rows = [[{"text": button} for button in clean[:2]]]
    if len(clean) > 2:
        for idx in range(2, len(clean), 2):
            rows.append([{"text": button} for button in clean[idx:idx + 2]])
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "input_field_placeholder": "Use a shortcut or just type in chat",
    }


def _inline_markup(rows: list[list[tuple[str, str]]]) -> dict[str, Any] | None:
    keyboard: list[list[dict[str, str]]] = []
    for row in rows:
        clean_row = [
            {"text": str(text).strip(), "callback_data": str(callback_data).strip()}
            for text, callback_data in row
            if str(text).strip() and str(callback_data).strip()
        ]
        if clean_row:
            keyboard.append(clean_row)
    if not keyboard:
        return None
    return {"inline_keyboard": keyboard}


def _semester_markup() -> dict[str, Any] | None:
    return _inline_markup(
        [
            [("1", "sem:1"), ("2", "sem:2"), ("3", "sem:3"), ("4", "sem:4")],
            [("5", "sem:5"), ("6", "sem:6"), ("7", "sem:7"), ("8", "sem:8")],
        ]
    )


def _check_attendance_markup() -> dict[str, Any] | None:
    return _inline_markup([[("Check Attendance", "action:attendance")]])


def _registration_retry_markup() -> dict[str, Any] | None:
    return _inline_markup([[("Start Registration Again", "auth:restart_registration")]])


def _format_duration(seconds: int | float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    def part(value: int, unit: str) -> str:
        suffix = "" if value == 1 else "s"
        return f"{value} {unit}{suffix}"

    if hours:
        parts = [part(hours, "hour")]
        if minutes:
            parts.append(part(minutes, "minute"))
        return " ".join(parts)

    if minutes:
        parts = [part(minutes, "minute")]
        if secs:
            parts.append(part(secs, "second"))
        return " ".join(parts)

    return part(secs, "second")


def _start_onboarding(chat_id: str, purpose: str = "login") -> None:
    _pending_auth[chat_id] = {"stage": "awaiting_semester", "purpose": purpose}
    _send_message(chat_id, _welcome_text())
    _send_message(
        chat_id,
        "\U0001F393 Which semester are you in right now?\nTap one option below to continue.",
        reply_markup=_semester_markup(),
    )


def _prompt_existing_user_semester(chat_id: str, purpose: str = "attendance_fetch") -> None:
    _pending_auth[chat_id] = {"stage": "awaiting_semester", "purpose": purpose}
    _send_message(
        chat_id,
        "\U0001F393 Your saved login is ready.\nSelect your current semester to continue.",
        reply_markup=_semester_markup(),
    )


def _continue_after_semester_selection(chat_id: str, semester: str, flow: dict[str, str]) -> None:
    purpose = str(flow.get("purpose") or "login").strip() or "login"
    if purpose == "attendance_fetch":
        update_profile_filters(chat_id, semester=semester)
        _send_message(chat_id, f"\u2705 Semester {semester} selected. Loading attendance...")
        saved_user = _get_saved_credentials(chat_id)
        if saved_user:
            _pending_auth.pop(chat_id, None)
            _start_live_fetch(chat_id, str(saved_user["user_id"]), str(saved_user["password"]))
            return
        _pending_auth[chat_id] = {"stage": "awaiting_user_id", "purpose": "attendance_fetch"}
        _send_message(
            chat_id,
            "\u2705 Semester selected.\n\ud83d\udcdd Now send your roll number.",
        )
        return

    _start_user_id_prompt(chat_id, purpose, announce=False)
    _send_message(
        chat_id,
        f"\u2705 Semester {semester} selected.\n\ud83c\udf93 Now send your roll number.",
    )


def _apply_semester_selection(chat_id: str, semester: str, *, announce: bool = True) -> bool:
    normalized = str(semester or "").strip()
    if normalized not in {"1", "2", "3", "4", "5", "6", "7", "8"}:
        return False

    if not update_profile_filters(chat_id, semester=normalized):
        saved_user = get_user(chat_id)
        if saved_user:
            save_profile(chat_id, saved_user["user_id"], DEFAULT_ACADEMIC_YEAR, normalized)
        else:
            _sessions.setdefault(chat_id, {})["semester"] = normalized
    _cache_delete(chat_id)

    if announce:
        _send_message(chat_id, f"\U0001F393 Semester set to {normalized}.")
    return True


def _send_registration_success(chat_id: str) -> None:
    _send_message(
        chat_id,
        "\u2705 Registration successful.\n"
        "\U0001F4CA Your login is saved and ready.\n"
        "Tap the button below to check your attendance.",
        reply_markup=_check_attendance_markup(),
    )


def _send_registration_retry_prompt(chat_id: str, message: str, message_id: int = 0) -> None:
    markup = _registration_retry_markup()
    if message_id:
        _edit_message(chat_id, message_id, message)
        _send_message(chat_id, "Tap below to start registration again.", reply_markup=markup)
        return
    _send_message(chat_id, message, reply_markup=markup)


def _restart_registration(chat_id: str) -> None:
    clear_user_state(chat_id)
    _pending_auth.pop(chat_id, None)
    _sessions.pop(chat_id, None)
    _clear_volatile_session(chat_id)
    with _cache_lock:
        _attendance_cache.pop(chat_id, None)
    _send_message(chat_id, "Let's register again from the top.")
    _start_onboarding(chat_id, "login")


def _attendance_actions_markup() -> dict[str, Any] | None:
    return _inline_markup(
        [
            [("Attendance", "menu:attendance"), ("Subject-wise Attendance", "menu:subjects")],
            [("Time Table", "menu:timetable")],
        ]
    )


def _send_attendance_actions(chat_id: str) -> None:
    _send_message(
        chat_id,
        "What would you like to open next?",
        reply_markup=_attendance_actions_markup(),
    )


def _attendance_security_message() -> str:
    return (
        "🔐 Security note: Your IMS password is not stored in any external/shared database. "
        "It remains only in your local encrypted bot storage, and the bot flow is secured."
    )


def _today_day_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _format_today_timetable_message(slots: list[dict[str, str]]) -> str:
    day_label = datetime.now().strftime("%A")
    lines = [f"🗓️ Time Table for {day_label}", "--------------------"]
    if not slots:
        lines.append("No classes found for today.")
        return "\n".join(lines)

    for slot in slots:
        time_label = str(slot.get("time") or "").strip() or "-"
        subject = str(slot.get("subject") or "").strip() or "Class"
        lines.append(f"{time_label}: {subject}")
    return "\n".join(lines)


def _send_today_timetable(chat_id: str, slots: list[dict[str, str]]) -> None:
    _send_message(chat_id, _format_today_timetable_message(slots))
    _send_attendance_actions(chat_id)


def _save_timetable_cache(chat_id: str, slots: list[dict[str, str]]) -> None:
    persisted = get_attendance_cache(chat_id)
    with _cache_lock:
        cache = _attendance_cache.get(chat_id)
        if cache is None and persisted:
            cache = dict(persisted)
            _attendance_cache[chat_id] = cache
        if cache is None:
            cache = {
                "ts": 0.0,
                "messages": [],
                "status": "",
                "fetching": False,
                "timeline_fetching": False,
                "timetable_fetching": False,
                "user_id": "",
                "year": None,
                "semester": None,
                "results": {},
                "timeline": {},
            }
            _attendance_cache[chat_id] = cache
        cache["timetable_day"] = _today_day_key()
        cache["timetable_ts"] = time.time()
        cache["timetable_slots"] = list(slots)


def _run_timetable_job(chat_id: str, user_id: str, password: str) -> None:
    if not _live_service_guard(chat_id, send_message=False):
        return

    _cache_mark_timetable_fetching(chat_id, True)
    try:
        slots, status = fetch_today_timetable(user_id, password)
        if status != STATUS_SUCCESS:
            _send_message(chat_id, "Could not fetch timetable right now. Please try again shortly.")
            return
        _save_timetable_cache(chat_id, slots)
        _send_today_timetable(chat_id, slots)
    finally:
        _cache_mark_timetable_fetching(chat_id, False)


def _handle_timetable_request(chat_id: str) -> None:
    cache = _cache_get(chat_id) or {}
    if cache.get("timetable_fetching"):
        _send_message(chat_id, "⏳ Timetable fetch is already running. Please wait.")
        return

    cached_day = str(cache.get("timetable_day") or "")
    cached_ts = float(cache.get("timetable_ts", 0.0) or 0.0)
    cached_slots = list(cache.get("timetable_slots") or [])
    age = int(max(0, time.time() - cached_ts)) if cached_ts else 10**9
    if cached_day == _today_day_key() and cached_slots and age <= TIMETABLE_CACHE_TTL_SECONDS:
        _send_today_timetable(chat_id, cached_slots)
        return

    saved_user = _get_saved_credentials(chat_id)
    if not saved_user:
        _send_message(chat_id, "Please register first, then try timetable.")
        return
    if not _live_service_guard(chat_id):
        return

    _send_message(chat_id, "Loading today's timetable...")
    _run_async(
        "menu-timetable",
        _run_timetable_job,
        chat_id,
        str(saved_user["user_id"]),
        str(saved_user["password"]),
    )


def _send_cached_attendance_and_actions(chat_id: str, cache: dict[str, Any]) -> None:
    for message in cache.get("messages", []):
        _send_message(chat_id, message)
    if cache.get("messages"):
        _send_message(chat_id, _attendance_security_message())
    _send_attendance_actions(chat_id)


def _subject_button_label(subject_code: str, info: dict[str, Any]) -> str:
    name = str(info.get("name") or "").strip()
    if name:
        short_name = name[:18].strip()
        return f"{subject_code} {short_name}"
    return subject_code


def _subject_selection_markup(results: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if not results:
        return None

    rows: list[list[tuple[str, str]]] = []
    current_row: list[tuple[str, str]] = []
    for subject_code, info in sorted(results.items(), key=lambda item: item[0]):
        current_row.append((_subject_button_label(subject_code, info), f"subject:{subject_code}"))
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    return _inline_markup(rows)


def _send_subject_selection(chat_id: str, results: dict[str, dict[str, Any]]) -> None:
    markup = _subject_selection_markup(results)
    if not markup:
        _send_message(chat_id, "I could not find subject data yet. Press Attendance once first.")
        return
    _send_message(
        chat_id,
        "Choose the subject you want date-wise attendance for.",
        reply_markup=markup,
    )


def _has_cached_timeline(cache: dict[str, Any] | None) -> bool:
    if not cache:
        return False
    timeline = cache.get("timeline") or {}
    return any(entries for entries in timeline.values())


def _timeline_status_emoji(status: str) -> str:
    normalized = (status or "").strip().lower()
    if normalized == "present":
        return "\U0001F7E2"
    if normalized == "absent":
        return "\U0001F534"
    return "\U0001F7E1"


def _format_subject_timeline_message(
    subject_code: str,
    info: dict[str, Any] | None,
    entries: list[dict[str, str]],
    year: str | None,
    semester: str | None,
) -> str:
    subject_name = str((info or {}).get("name") or "").strip()
    title = f"\U0001F4D8 {subject_code}"
    if subject_name and subject_name.lower() != subject_code.lower():
        title = f"\U0001F4D8 {subject_name}\n{subject_code}"

    lines = [title, "--------------------"]
    visible_entries = []
    for entry in entries:
        date_label = str(entry.get("date") or "").strip()
        raw = str(entry.get("raw") or "").strip()
        status = (entry.get("status") or "other").lower()
        if not date_label:
            continue
        # Skip empty portal cells that only produce a fake "No Class" row.
        if status == "holiday" and not raw:
            continue
        visible_entries.append(entry)

    if not visible_entries:
        lines.append("No date-wise attendance was available for this subject.")
    else:
        lines.append(f"Full previous attendance: {len(visible_entries)} entries")
        lines.append("")
        for entry in visible_entries:
            status = (entry.get("status") or "other").lower()
            raw = str(entry.get("raw") or "").strip()
            date_label = str(entry.get("date") or "-").strip()
            emoji = _timeline_status_emoji(status)
            if status == "present":
                status_text = "Present"
            elif status == "absent":
                status_text = "Absent"
            elif status == "holiday":
                status_text = "Holiday"
            elif status == "mixed":
                status_text = "Mixed"
            else:
                status_text = status.replace("_", " ").title()
            if raw and raw.lower() not in {status.lower(), status_text.lower()}:
                lines.append(f"{emoji} {date_label}: {status_text} ({raw})")
            else:
                lines.append(f"{emoji} {date_label}: {status_text}")
    lines.append("")
    lines.append(f"\U0001F4C5 Year: {year or DEFAULT_ACADEMIC_YEAR}")
    lines.append(f"\U0001F393 Semester: {semester or '-'}")
    return "\n".join(lines)


def _start_subject_timeline_fetch(chat_id: str, subject_code: str | None = None, *, open_menu_only: bool = False) -> None:
    cache = _cache_get(chat_id)
    if cache and cache.get("fetching"):
        _send_message(chat_id, "\u23F3 A fetch is already running. Please wait.")
        return
    if cache and cache.get("timeline_fetching"):
        if open_menu_only and cache.get("results"):
            _send_subject_selection(chat_id, cache.get("results") or {})
        else:
            _send_message(chat_id, "\u23F3 Full subject history is still syncing. Please try again in a few seconds.")
        return

    saved_user = _get_saved_credentials(chat_id)
    if not saved_user:
        _send_message(chat_id, "Please register first, then try again.")
        return

    if open_menu_only:
        _send_message(chat_id, "Loading your subjects...")
    else:
        _send_message(chat_id, f"Loading date-wise attendance for {subject_code}...")

    worker = threading.Thread(
        target=_run_subject_timeline_job,
        args=(chat_id, saved_user["user_id"], saved_user["password"], subject_code, open_menu_only),
        daemon=True,
    )
    worker.start()


def _start_subject_timeline_prefetch(chat_id: str, user_id: str, password: str) -> None:
    return


def _run_subject_timeline_prefetch_job(chat_id: str, user_id: str, password: str) -> None:
    if not _live_service_guard(chat_id, send_message=False):
        return

    _cache_mark_timeline_fetching(chat_id, True)
    try:
        year, semester = _resolve_filters(chat_id)
        _messages, status, resolved_year, resolved_semester, results, timeline = _build_live_attendance_messages(
            user_id,
            password,
            year,
            semester,
            include_timeline=True,
        )
        if status != STATUS_SUCCESS or not timeline:
            return

        current_cache = _cache_get(chat_id) or {}
        cached_messages = current_cache.get("messages") or []
        cached_status = current_cache.get("status") or status
        cached_results = current_cache.get("results") or results
        _cache_set(
            chat_id=chat_id,
            user_id=user_id,
            messages=list(cached_messages),
            status=cached_status,
            year=resolved_year,
            semester=resolved_semester,
            results=cached_results,
            timeline=timeline,
        )
    finally:
        _cache_mark_timeline_fetching(chat_id, False)


def _run_subject_timeline_job(
    chat_id: str,
    user_id: str,
    password: str,
    subject_code: str | None,
    open_menu_only: bool,
) -> None:
    if not _live_service_guard(chat_id):
        return

    _cache_mark_fetching(chat_id, True)
    try:
        year, semester = _resolve_filters(chat_id)
        messages, status, resolved_year, resolved_semester, results, timeline = _build_live_attendance_messages(
            user_id,
            password,
            year,
            semester,
            include_timeline=True,
        )

        if status != STATUS_SUCCESS:
            diagnostic = get_last_login_diagnostic()
            for idx, message in enumerate(messages):
                if idx == 0 and _should_offer_registration_retry(status, diagnostic):
                    _send_registration_retry_prompt(chat_id, message)
                else:
                    _send_message(chat_id, message)
            return

        current_cache = _cache_get(chat_id) or {}
        cached_messages = current_cache.get("messages") or messages
        _cache_set(
            chat_id=chat_id,
            user_id=user_id,
            messages=list(cached_messages),
            status=status,
            year=resolved_year,
            semester=resolved_semester,
            results=results,
            timeline=timeline,
        )

        if open_menu_only:
            _send_subject_selection(chat_id, results)
            return

        info = results.get(subject_code or "", {})
        entries = timeline.get(subject_code or "", [])
        _send_message(
            chat_id,
            _format_subject_timeline_message(
                subject_code or "Subject",
                info,
                entries,
                resolved_year,
                resolved_semester,
            ),
        )
        _send_attendance_actions(chat_id)
    finally:
        _cache_mark_fetching(chat_id, False)


def _progress_bar(percent: int, slots: int = 12) -> str:
    clamped = max(0.0, min(100.0, float(percent)))
    total_units = int(round((clamped / 100.0) * slots * 8))
    full_slots, partial_unit = divmod(total_units, 8)
    partials = ["", "\u258F", "\u258E", "\u258D", "\u258C", "\u258B", "\u258A", "\u2589"]
    bar = "\u2588" * min(full_slots, slots)
    if full_slots < slots and partial_unit:
        bar += partials[partial_unit]
    empty_slots = max(0, slots - len(bar))
    return bar + ("\u2591" * empty_slots)


def _roasty_stage_text(stage: str) -> str:
    raw = (stage or "").strip().lower()
    if "security" in raw or "captcha" in raw:
        return "\U0001F575\uFE0F Cracking the portal's dramatic little security puzzle..."
    if "signing in" in raw or "login" in raw:
        return "\U0001F6AA Knocking on IMS like it owes us your attendance..."
    if "my activities" in raw or "attendance page" in raw or "navigation" in raw:
        return "\U0001F5FA\uFE0F Sneaking through the portal's ancient maze before it gets confused..."
    if "year" in raw or "semester" in raw or "submit" in raw:
        return "\U0001F4DA Reminding IMS which year and semester you are actually in..."
    if "reading" in raw or "subject-wise" in raw or "telegram reply" in raw:
        return "\U0001F9FE Scooping up the evidence before the portal changes its mood..."
    if "opening" in raw or "http" in raw or "session" in raw:
        return "\U0001FAE0 Waking up NSUT's museum-piece portal for one honest job..."
    return "\U0001F9C2 Dusting off the attendance machinery and hoping it behaves..."


def _format_live_progress(percent: int, stage: str) -> str:
    clamped = max(0, min(100, int(percent)))
    stage_text = _roasty_stage_text(stage)
    return (
        "\U0001F680 Attendance Heist In Progress\n"
        "--------------------\n"
        f"{_progress_bar(clamped)} {clamped}%\n"
        f"{stage_text}\n\n"
        "\u23F3 Hold on. We are politely robbing the portal for your attendance."
    )


def _format_live_progress_done(title: str, detail: str, *, success: bool) -> str:
    prefix = "\u2705" if success else "\u26A0\uFE0F"
    return (
        f"{prefix} {title}\n"
        "--------------------\n"
        f"{_progress_bar(100)} 100%\n"
        f"{detail}"
    )


def _configure_bot_commands() -> None:
    if not TOKEN:
        return
    commands = [
        {"command": "start", "description": "Start the bot"},
        {"command": "attendance", "description": "Fetch live attendance"},
        {"command": "timetable", "description": "Show today's timetable"},
        {"command": "refresh", "description": "Show saved attendance"},
        {"command": "login", "description": "Save roll number and password"},
        {"command": "help", "description": "Show help"},
        {"command": "logout", "description": "Clear saved session"},
    ]
    try:
        resp = requests.post(
            _api_url("setMyCommands"),
            json={"commands": commands},
            timeout=20,
        )
        if resp.status_code >= 300:
            logger.warning(
                "Telegram setMyCommands failed status=%s body=%s",
                resp.status_code,
                resp.text[:400],
            )
    except Exception:
        logger.debug("Failed setting Telegram bot commands", exc_info=True)


def _status_message(status: str, diagnostic: str | None = None) -> str:
    diagnostic_text = str(diagnostic or "").strip().lower()
    if status == STATUS_INVALID_CREDENTIALS:
        return "Login failed. The password you entered is wrong. Tap Start Registration Again below."
    if status == STATUS_INVALID_CAPTCHA:
        if "invalid security number" in diagnostic_text or "still on login page" in diagnostic_text:
            return (
                "Login failed. The password you entered looks wrong, so IMS sent the login back again. "
                "Tap Start Registration Again below."
            )
        if "password" in diagnostic_text or "credentials" in diagnostic_text or "login" in diagnostic_text:
            return "Login failed. The password you entered looks wrong. Tap Start Registration Again below."
        return (
            "Login failed before attendance could open. The password you entered may be wrong. "
            "Tap Start Registration Again below."
        )
    if status == STATUS_NAVIGATION_FAILED:
        return "IMS opened, but attendance could not be read right now. Please try again shortly."
    return "Attendance fetch failed. IMS may be slow or temporarily unavailable."


def _should_offer_registration_retry(status: str, diagnostic: str | None = None) -> bool:
    diagnostic_text = str(diagnostic or "").strip().lower()
    if status in {STATUS_INVALID_CREDENTIALS, STATUS_INVALID_CAPTCHA}:
        return True
    return any(
        hint in diagnostic_text
        for hint in ("password", "credentials", "login page", "invalid security number", "invalid login")
    )


def _build_live_attendance_messages(
    user_id: str,
    password: str,
    year: str | None,
    semester: str | None,
    progress_callback: Callable[[int, str], None] | None = None,
    include_timeline: bool = False,
) -> tuple[
    list[str],
    str,
    str | None,
    str | None,
    dict[str, dict[str, Any]],
    dict[str, list[dict[str, str]]],
]:
    data, _timeline, status = fetch_attendance_detailed(
        user_id,
        password,
        year,
        semester,
        include_timeline=include_timeline,
        progress_callback=progress_callback,
    )
    selected = get_last_selected_filters()
    resolved_year = selected.get("year") or year
    resolved_semester = selected.get("semester") or semester

    if not data:
        diagnostic = get_last_login_diagnostic()
        if diagnostic:
            logger.info("Fetch diagnostic for user %s: %s", user_id, diagnostic)
        return [f"❌ {_status_message(status, diagnostic)}"], status, resolved_year, resolved_semester

    results = calculate_attendance(data)
    card = _format_attendance_card(results, resolved_year, resolved_semester)
    return [card], status, resolved_year, resolved_semester


def _start_user_id_prompt(chat_id: str, purpose: str, announce: bool = True) -> None:
    _pending_auth[chat_id] = {"stage": "awaiting_user_id", "purpose": purpose}
    if announce:
        _send_message(chat_id, "\U0001F393 Send your roll number.")


def _start_password_prompt(chat_id: str, user_id: str, purpose: str) -> None:
    if not _live_service_guard(chat_id):
        return
    if not user_id:
        _start_user_id_prompt(chat_id, purpose)
        return

    _pending_auth[chat_id] = {
        "stage": "awaiting_password",
        "purpose": purpose,
        "user_id": user_id,
    }
    _send_message(
        chat_id,
        f"\U0001F4DD Roll number locked in: {user_id}\n\U0001F510 Now send your IMS password.",
    )


def _start_live_fetch(chat_id: str, user_id: str, password: str) -> None:
    if not _live_service_guard(chat_id):
        return
    cache = _cache_get(chat_id)
    if cache and cache.get("fetching"):
        _send_message(chat_id, "\u23F3 A fetch is already running. Please wait.")
        return

    _cache_mark_fetching(chat_id, True)
    progress_message = _send_message(
        chat_id,
        _format_live_progress(0, "Connecting to IMS and opening your attendance dashboard..."),
    )
    progress_message_id = int((progress_message or {}).get("message_id") or 0)
    worker = threading.Thread(
        target=_run_live_fetch_job,
        args=(chat_id, user_id, password, progress_message_id),
        daemon=True,
    )
    worker.start()


def _run_live_fetch_job(chat_id: str, user_id: str, password: str, progress_message_id: int = 0) -> None:
    if not _live_service_guard(chat_id):
        _cache_mark_fetching(chat_id, False)
        return
    animation_stop = threading.Event()
    progress_state = {
        "current": 0.0,
        "target": 0.0,
        "stage": "Connecting to IMS and opening your attendance dashboard...",
        "started_at": time.time(),
    }
    progress_lock = threading.Lock()

    def render_progress(percent: int | None = None, stage: str | None = None) -> None:
        if not progress_message_id:
            return
        with progress_lock:
            rendered_percent = max(0, min(100, int(round(float(progress_state["current"])))))
            rendered_stage = str(progress_state["stage"])
        if percent is not None:
            rendered_percent = max(0, min(100, int(percent)))
        if stage is not None:
            rendered_stage = stage
        _edit_message(
            chat_id,
            progress_message_id,
            _format_live_progress(rendered_percent, rendered_stage),
        )

    def animate_progress() -> None:
        if not progress_message_id:
            return
        last_rendered: tuple[int, str] | None = None
        while not animation_stop.is_set():
            with progress_lock:
                current = float(progress_state["current"])
                target = float(progress_state["target"])
                elapsed = max(0.0, time.time() - float(progress_state["started_at"]))
                auto_target = min(80.0, elapsed * 24.0)
                desired = min(95.0, max(auto_target, target))
                if current < desired:
                    if current < 15:
                        step = 1.3
                    elif current < 35:
                        step = 1.5
                    elif current < 60:
                        step = 1.35
                    elif current < 80:
                        step = 1.15
                    else:
                        step = 0.55
                    progress_state["current"] = min(desired, current + step)
                    current = float(progress_state["current"])
                rendered_percent = max(0, min(100, int(round(current))))
                rendered = (rendered_percent, str(progress_state["stage"]))
            if rendered != last_rendered:
                render_progress(rendered[0], rendered[1])
                last_rendered = rendered
            wait_for = 0.09 if rendered[0] < 80 else 0.15
            if animation_stop.wait(wait_for):
                break

    def animate_to_percent(percent: int, stage: str, duration: float = 0.45) -> None:
        if not progress_message_id:
            return
        target_percent = max(0, min(100, int(percent)))
        with progress_lock:
            start = float(progress_state["current"])
            progress_state["stage"] = stage
        if start >= float(target_percent):
            render_progress(target_percent, stage)
            return
        distance = max(1.0, float(target_percent) - start)
        steps = max(10, min(32, int(distance)))
        delay = max(0.02, duration / steps)
        for idx in range(1, steps + 1):
            t = idx / steps
            eased = t * t * (3.0 - (2.0 * t))
            current = start + (distance * eased)
            with progress_lock:
                progress_state["current"] = current
                progress_state["target"] = max(float(progress_state["target"]), current)
            render_progress(int(round(current)), stage)
            if idx < steps:
                time.sleep(delay)
        with progress_lock:
            progress_state["current"] = float(target_percent)
            progress_state["target"] = float(target_percent)

    def stop_animator() -> None:
        animation_stop.set()
        if animator is not None:
            animator.join(timeout=0.6)

    def publish_final_messages(messages: list[str]) -> None:
        if progress_message_id and messages:
            _edit_message(chat_id, progress_message_id, messages[0])
            for message in messages[1:]:
                _send_message(chat_id, message)
            return
        for message in messages:
            _send_message(chat_id, message)

    animator: threading.Thread | None = None
    if progress_message_id:
        animator = threading.Thread(target=animate_progress, daemon=True)
        animator.start()

    def report_progress(percent: int, stage: str, *, force: bool = False) -> None:
        clamped = max(0.0, min(99.0, float(percent)))
        with progress_lock:
            progress_state["target"] = max(float(progress_state["target"]), clamped)
            progress_state["stage"] = stage
        if force and progress_message_id:
            render_progress(stage=stage)

    try:
        report_progress(10, "Loading your saved filters...", force=True)
        year, semester = _resolve_filters(chat_id)
        messages, status, resolved_year, resolved_semester, results, timeline = _build_live_attendance_messages(
            user_id,
            password,
            year,
            semester,
            progress_callback=report_progress,
            include_timeline=False,
        )

        if status == STATUS_SUCCESS:
            save_profile(chat_id, user_id, resolved_year, resolved_semester)
            _sessions.pop(chat_id, None)
            _cache_set(
                chat_id=chat_id,
                user_id=user_id,
                messages=messages,
                status=status,
                year=resolved_year,
                semester=resolved_semester,
                results=results,
                timeline=timeline,
            )
            if progress_message_id:
                stop_animator()
                animate_to_percent(100, "Attendance ready")
            publish_final_messages(messages)
            _send_message(chat_id, _attendance_security_message())
            _send_attendance_actions(chat_id)
            _start_subject_timeline_prefetch(chat_id, user_id, password)
            return

        diagnostic = get_last_login_diagnostic()
        if _should_offer_registration_retry(status, diagnostic):
            if progress_message_id:
                stop_animator()
            if messages:
                _send_registration_retry_prompt(chat_id, messages[0], progress_message_id)
                for message in messages[1:]:
                    _send_message(chat_id, message)
            else:
                _send_registration_retry_prompt(chat_id, _status_message(status, diagnostic), progress_message_id)
            return

        if progress_message_id:
            stop_animator()
            animate_to_percent(100, "Wrapping up the portal tantrum...")
        publish_final_messages(messages)

        cached = _cache_get(chat_id)
        if _cache_matches_profile(cached, user_id, year, semester):
            age = _cache_age_seconds(cached)
            _send_message(
                chat_id,
                f"\u26A0\uFE0F Showing your last saved attendance snapshot instead ({_format_duration(age)} old).",
            )
            for message in cached.get("messages", []):
                _send_message(chat_id, message)
            _send_message(chat_id, _attendance_security_message())
            _send_attendance_actions(chat_id)
    finally:
        animation_stop.set()
        if animator is not None:
            animator.join(timeout=0.6)
        _cache_mark_fetching(chat_id, False)


def _build_live_attendance_messages(
    user_id: str,
    password: str,
    year: str | None,
    semester: str | None,
    progress_callback: Callable[[int, str], None] | None = None,
    include_timeline: bool = False,
) -> tuple[
    list[str],
    str,
    str | None,
    str | None,
    dict[str, dict[str, Any]],
    dict[str, list[dict[str, str]]],
]:
    data, _timeline, status = fetch_attendance_detailed(
        user_id,
        password,
        year,
        semester,
        include_timeline=include_timeline,
        progress_callback=progress_callback,
    )
    selected = get_last_selected_filters()
    resolved_year = selected.get("year") or year
    resolved_semester = selected.get("semester") or semester

    if not data:
        diagnostic = get_last_login_diagnostic()
        if diagnostic:
            logger.info("Fetch diagnostic for user %s: %s", user_id, diagnostic)
        return [f"\u274C {_status_message(status, diagnostic)}"], status, resolved_year, resolved_semester, {}, {}

    results = calculate_attendance(data)
    card = _format_attendance_card(results, resolved_year, resolved_semester)
    return [card], status, resolved_year, resolved_semester, results, _timeline


def _handle_login_command(chat_id: str, args: list[str], source_message_id: int = 0) -> None:
    if not _live_service_guard(chat_id):
        return
    if not args:
        semester = (_sessions.get(chat_id, {}) or {}).get("semester") or (get_profile(chat_id) or {}).get("semester")
        if semester:
            _start_user_id_prompt(chat_id, "login")
        else:
            _start_onboarding(chat_id, "login")
        return
    if len(args) == 1:
        _start_password_prompt(chat_id, args[0].strip(), "login")
        return

    user_id = args[0].strip()
    password = " ".join(args[1:]).strip()
    if not user_id or not password:
        _send_message(chat_id, "Usage: /login <user_id> <password>")
        return
    _pending_auth.pop(chat_id, None)
    _save_login_session(chat_id, user_id, password)
    _send_message(chat_id, "✅ Session saved.\nNow press /attendance and I will fetch your attendance.")


def _handle_refresh_request(chat_id: str, force_live: bool = False) -> None:
    profile = get_profile(chat_id)
    saved_user = _get_saved_credentials(chat_id)
    year, semester = _resolve_filters(chat_id)
    cache = _cache_get(chat_id)

    if cache and cache.get("fetching"):
        _send_message(chat_id, "\u23F3 A fetch is already running. Please wait.")
        return

    if force_live:
        if saved_user:
            if not _live_service_guard(chat_id):
                return
            profile = get_profile(chat_id)
            if not profile or not profile.get("semester"):
                if semester:
                    _pending_auth[chat_id] = {"stage": "awaiting_semester", "purpose": "attendance_fetch"}
                    _send_message(
                        chat_id,
                        "\U0001F393 Your saved login is ready.\nSelect your current semester to continue.",
                        reply_markup=_semester_markup(),
                    )
                    return
                _start_user_id_prompt(chat_id, "login")
                return
            _start_live_fetch(chat_id, saved_user["user_id"], saved_user["password"])
            return
        if cache and cache.get("messages"):
            age = _cache_age_seconds(cache)
            _send_cached_attendance_and_actions(chat_id, cache)
            _send_message(
                chat_id,
                f"\u26A0\uFE0F Showing saved attendance snapshot ({_format_duration(age)} old). "
                "Finish registration for a new live fetch.",
            )
            return
        if not _live_service_guard(chat_id):
            return
        if semester:
            _start_user_id_prompt(chat_id, "login")
        else:
            _start_onboarding(chat_id, "login")
        return

    if saved_user:
        if not _live_service_guard(chat_id):
            return
        profile = get_profile(chat_id)
        if not profile or not profile.get("semester"):
            if semester:
                _pending_auth[chat_id] = {"stage": "awaiting_semester", "purpose": "attendance_fetch"}
                _send_message(
                    chat_id,
                    "\U0001F393 Your saved login is ready.\nSelect your current semester to continue.",
                    reply_markup=_semester_markup(),
                )
                return
            _prompt_existing_user_semester(chat_id, "attendance_fetch")
            return
        _start_live_fetch(chat_id, saved_user["user_id"], saved_user["password"])
        return

    if profile and _cache_matches_profile(cache, profile["user_id"], year, semester):
        age = _cache_age_seconds(cache)
        _send_cached_attendance_and_actions(chat_id, cache)
        if age <= CACHE_TTL_SECONDS:
            _send_message(chat_id, f"\u26A1 Instant result from saved cache ({_format_duration(age)} old).")
        else:
            _send_message(
                chat_id,
                f"\u26A0\uFE0F Showing saved attendance snapshot ({_format_duration(age)} old). "
                "Use /refresh_force for a live fetch.",
            )
        return

    if cache and cache.get("messages"):
        age = _cache_age_seconds(cache)
        _send_cached_attendance_and_actions(chat_id, cache)
        _send_message(
            chat_id,
            f"\u26A0\uFE0F Showing saved attendance snapshot ({_format_duration(age)} old). "
            "Finish registration for a new live fetch.",
        )
        return

    if not _live_service_guard(chat_id):
        return
    if semester:
        _start_user_id_prompt(chat_id, "login")
    else:
        _start_onboarding(chat_id, "login")


def _handle_setyear(chat_id: str, args: list[str]) -> None:
    if not args:
        _send_message(chat_id, "Usage: /setyear <YYYY-YY>")
        return

    year = args[0].strip()
    if not year:
        _send_message(chat_id, "Usage: /setyear <YYYY-YY>")
        return

    if not update_profile_filters(chat_id, year=year):
        _sessions.setdefault(chat_id, {})["year"] = year
    _cache_delete(chat_id)
    _send_message(chat_id, f"Year set to {year}. The next live fetch will use it.")


def _handle_setsem(chat_id: str, args: list[str]) -> None:
    if not args:
        _send_message(chat_id, "Usage: /setsem <N>")
        return

    semester = args[0].strip()
    if not semester:
        _send_message(chat_id, "Usage: /setsem <N>")
        return

    if not _apply_semester_selection(chat_id, semester, announce=False):
        _send_message(chat_id, "Semester must be between 1 and 8.")
        return
    _send_message(chat_id, f"Semester set to {semester}. The next live fetch will use it.")


def _handle_whoami(chat_id: str) -> None:
    profile = get_profile(chat_id)
    pending = _sessions.get(chat_id, {})
    cache = _cache_get(chat_id)
    if not profile and not pending and not cache:
        _send_message(chat_id, "No saved profile yet. Send your roll number to begin.")
        return

    user_id = (profile or {}).get("user_id") or "-"
    year = pending.get("year") or (profile or {}).get("year") or DEFAULT_ACADEMIC_YEAR
    semester = pending.get("semester") or (profile or {}).get("semester") or "-"
    snapshot_age = "-"
    if cache and cache.get("messages"):
        snapshot_age = f"{_format_duration(_cache_age_seconds(cache))} old"

    _send_message(
        chat_id,
        (
            "PROFILE\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Chat ID: {chat_id}\n"
            f"Roll number: {user_id}\n"
            f"Year: {year}\n"
            f"Semester: {semester}\n"
            f"Saved snapshot: {snapshot_age}\n"
            f"Encrypted session: {'saved' if _get_saved_credentials(chat_id) else 'not saved'}\n"
            f"Admin access: {'yes' if _is_admin(chat_id) else 'no'}"
        ),
    )


def _handle_server_command(chat_id: str) -> None:
    if not _is_admin(chat_id):
        _send_message(chat_id, "Server controls are admin-only.")
        return
    _send_message(chat_id, _server_status_text(), reply_markup=_server_controls_markup())


def _handle_stats_command(chat_id: str) -> None:
    if not _is_admin(chat_id):
        _send_message(chat_id, "Stats are admin-only.")
        return

    stats = get_bot_stats()
    control = get_service_control()
    live_status = "RUNNING" if control.get("service_enabled") else "PAUSED"
    _send_message(
        chat_id,
        (
            "BOT STATS\n"
            "--------------------\n"
            f"Total users ever: {stats['total_users_ever']}\n"
            f"Registered users: {stats['registered_users']}\n"
            f"Saved credentials: {stats['saved_credentials']}\n"
            f"Users with cache: {stats['cached_users']}\n"
            f"Users with successful fetches: {stats['successful_fetch_users']}\n"
            f"Active last 24h: {stats['active_last_24h']}\n"
            f"Active last 7d: {stats['active_last_7d']}\n"
            f"Live fetch service: {live_status}\n"
            f"Your chat ID: {chat_id}"
        ),
    )


def _handle_broadcast_command(chat_id: str, message: str) -> None:
    if not _is_admin(chat_id):
        _send_message(chat_id, "Broadcast is admin-only.")
        return

    text = (message or "").strip()
    if not text:
        _send_message(chat_id, "Usage: /broadcast <message>")
        return

    recipients = get_all_known_chat_ids()
    sent = 0
    failed = 0
    for recipient in recipients:
        try:
            result = _send_message(recipient, text)
            if result is None:
                failed += 1
            else:
                sent += 1
        except Exception:
            failed += 1
            logger.exception("Broadcast failed for chat_id=%s", recipient)

    _send_message(
        chat_id,
        (
            "BROADCAST COMPLETE\n"
            "--------------------\n"
            f"Recipients found: {len(recipients)}\n"
            f"Sent: {sent}\n"
            f"Failed: {failed}"
        ),
    )


def _handle_callback(update: dict[str, Any]) -> None:
    cq = update.get("callback_query") or {}
    callback_id = str(cq.get("id", "")).strip()
    data = str(cq.get("data", "") or "").strip()
    msg = cq.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id", "")).strip()

    logger.info("Received callback query chat_id=%s data=%s", chat_id or "-", data or "-")

    if callback_id:
        _answer_callback_query_async(callback_id)
    if not chat_id:
        return
    mark_chat_seen(chat_id, source="callback")

    if data.startswith("sem:"):
        semester = data.split(":", 1)[1].strip()
        if not _apply_semester_selection(chat_id, semester, announce=False):
            _send_message(chat_id, "Semester must be between 1 and 8.")
            return

        flow = _pending_auth.get(chat_id, {})
        if flow.get("stage") == "awaiting_semester":
            _continue_after_semester_selection(chat_id, semester, flow)
            return

        _send_message(chat_id, f"\u2705 Semester set to {semester}.")
        return

    if data == "action:attendance":
        _run_async("attendance-button", _handle_refresh_request, chat_id, True)
        return

    if data == "auth:restart_registration":
        _restart_registration(chat_id)
        return

    if data == "menu:attendance":
        _run_async("menu-attendance", _handle_refresh_request, chat_id, True)
        return

    if data == "menu:subjects":
        cache = _cache_get(chat_id) or {}
        results = cache.get("results") or {}
        if results:
            if not _has_cached_timeline(cache):
                saved_user = _get_saved_credentials(chat_id)
                if saved_user:
                    _start_subject_timeline_prefetch(chat_id, saved_user["user_id"], saved_user["password"])
            _send_subject_selection(chat_id, results)
        else:
            _start_subject_timeline_fetch(chat_id, open_menu_only=True)
        return

    if data == "menu:timetable":
        _run_async("menu-timetable", _handle_timetable_request, chat_id)
        return

    if data.startswith("subject:"):
        subject_code = data.split(":", 1)[1].strip()
        cache = _cache_get(chat_id) or {}
        results = cache.get("results") or {}
        timeline = cache.get("timeline") or {}
        year, semester = _resolve_filters(chat_id)
        if timeline.get(subject_code):
            _send_message(
                chat_id,
                _format_subject_timeline_message(
                    subject_code,
                    results.get(subject_code, {}),
                    timeline.get(subject_code, []),
                    year,
                    semester,
                ),
            )
            _send_attendance_actions(chat_id)
        elif cache.get("timeline_fetching"):
            _send_message(chat_id, "\u23F3 Full subject history is still syncing. Tap the subject again in a few seconds.")
        else:
            _start_subject_timeline_fetch(chat_id, subject_code)
        return

    if not data.startswith("server:"):
        return

    if not _is_admin(chat_id):
        _send_message(chat_id, "Server controls are admin-only.")
        return

    if data == "server:start":
        set_service_enabled(True, updated_by_chat_id=chat_id)
        _send_message(
            chat_id,
            f"Live attendance service started.\n\n{_server_status_text()}",
            reply_markup=_server_controls_markup(),
        )
        return

    if data == "server:stop":
        set_service_enabled(False, updated_by_chat_id=chat_id)
        _send_message(
            chat_id,
            f"Live attendance service paused.\n\n{_server_status_text()}",
            reply_markup=_server_controls_markup(),
        )
        return

    if data == "server:status":
        _send_message(chat_id, _server_status_text(), reply_markup=_server_controls_markup())
        return


def _handle_plain_text(chat_id: str, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return

    flow = _pending_auth.get(chat_id)
    if flow:
        stage = flow.get("stage")
        if stage == "awaiting_user_id":
            user_id = text.split()[0].strip()
            if not user_id:
                _send_message(chat_id, "Send a valid roll number.")
                return
            _start_password_prompt(chat_id, user_id, flow.get("purpose", "login"))
            return

        if stage == "awaiting_password":
            user_id = flow.get("user_id", "").strip()
            password = text
            _pending_auth.pop(chat_id, None)
            if not user_id or not password:
                _send_message(chat_id, "Roll number or password is missing. Send /login to start again.")
                return
            _save_login_session(chat_id, user_id, password)
            _send_message(chat_id, "✅ Session saved.\nNow press /attendance and I will fetch your attendance.")
            return

    lowered = text.lower()
    if lowered in {"attendance", "/attendance", "check", "refresh"}:
        _handle_refresh_request(chat_id, force_live=False)
        return
    if lowered in {"timetable", "time table", "/timetable"}:
        _handle_timetable_request(chat_id)
        return
    if lowered in {"refresh_force", "live"}:
        _handle_refresh_request(chat_id, force_live=True)
        return

    profile = get_profile(chat_id)
    if profile:
        _send_message(chat_id, "Send /attendance to fetch attendance using your saved session.")
        return

    user_id = text.split()[0].strip()
    if not user_id:
        _send_message(chat_id, "Send your roll number to begin.")
        return
    if not _live_service_guard(chat_id):
        return
    _start_password_prompt(chat_id, user_id, "login")


def _handle_message(update: dict[str, Any]) -> None:
    if update.get("callback_query"):
        _handle_callback(update)
        return

    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id", "")).strip()
    text = (msg.get("text") or "").strip()

    if not chat_id:
        return

    mark_chat_seen(chat_id, source="message")

    cmd, args = _normalize_cmd(text)
    if not cmd and text:
        _handle_plain_text(chat_id, text)
        return

    if cmd in {"/start", "/help"}:
        if cmd == "/start":
            if _get_saved_credentials(chat_id):
                _send_message(chat_id, f"{_welcome_text()}\n\nYou already have a saved session.\nPress /attendance.")
                return
            _start_user_id_prompt(chat_id, "login", announce=False)
            _send_message(chat_id, _welcome_text())
            return
        _send_message(chat_id, _help_text())
        return

    if cmd == "/login":
        _handle_login_command(chat_id, args)
        return

    if cmd == "/logout":
        deleted = clear_user_state(chat_id)
        _sessions.pop(chat_id, None)
        _clear_volatile_session(chat_id)
        _pending_auth.pop(chat_id, None)
        with _cache_lock:
            _attendance_cache.pop(chat_id, None)
        _send_message(chat_id, "Logged out and cleared saved state." if deleted else "No saved state found.")
        return

    if cmd == "/setyear":
        _handle_setyear(chat_id, args)
        return

    if cmd == "/setsem":
        _handle_setsem(chat_id, args)
        return

    if cmd == "/whoami":
        _handle_whoami(chat_id)
        return

    if cmd == "/attendance":
        _handle_refresh_request(chat_id, force_live=False)
        return

    if cmd == "/timetable":
        _handle_timetable_request(chat_id)
        return

    if cmd == "/server":
        _handle_server_command(chat_id)
        return

    if cmd == "/stats":
        _handle_stats_command(chat_id)
        return

    if cmd == "/broadcast":
        _handle_broadcast_command(chat_id, _command_payload(text))
        return

    if cmd == "/refresh":
        _handle_refresh_request(chat_id, force_live=False)
        return

    if cmd == "/refresh_force":
        _handle_refresh_request(chat_id, force_live=True)
        return

    _send_message(chat_id, "Unknown command. Send /help.")


def _handle_login_command(chat_id: str, args: list[str], source_message_id: int = 0) -> None:
    if not _live_service_guard(chat_id):
        return
    if not args:
        semester = (_sessions.get(chat_id, {}) or {}).get("semester") or (get_profile(chat_id) or {}).get("semester")
        if semester:
            _start_user_id_prompt(chat_id, "login")
        else:
            _start_onboarding(chat_id, "login")
        return
    if len(args) == 1:
        _start_password_prompt(chat_id, args[0].strip(), "login")
        return

    user_id = args[0].strip()
    password = " ".join(args[1:]).strip()
    if not user_id or not password:
        _send_message(chat_id, "Usage: /login <user_id> <password>")
        return
    _pending_auth.pop(chat_id, None)
    _save_login_session(chat_id, user_id, password)
    _send_registration_success(chat_id)
    _delete_message_async(chat_id, source_message_id)


def _handle_plain_text(chat_id: str, text: str, source_message_id: int = 0) -> None:
    text = (text or "").strip()
    if not text:
        return

    flow = _pending_auth.get(chat_id)
    if flow:
        stage = flow.get("stage")
        if stage == "awaiting_semester":
            semester = text.split()[0].strip()
            if not _apply_semester_selection(chat_id, semester, announce=False):
                _send_message(chat_id, "\U0001F393 Please choose a semester from 1 to 8.")
                return
            _continue_after_semester_selection(chat_id, semester, flow)
            return

        if stage == "awaiting_user_id":
            user_id = text.split()[0].strip()
            if not user_id:
                _send_message(chat_id, "\U0001F6A7 Send a valid roll number.")
                return
            _start_password_prompt(chat_id, user_id, flow.get("purpose", "login"))
            return

        if stage == "awaiting_password":
            user_id = flow.get("user_id", "").strip()
            password = text
            _pending_auth.pop(chat_id, None)
            if not user_id or not password:
                _send_message(chat_id, "Roll number or password is missing. Send /login to start again.")
                return
            _save_login_session(chat_id, user_id, password)
            _send_registration_success(chat_id)
            _delete_message_async(chat_id, source_message_id)
            return

    lowered = text.lower()
    if lowered in {"attendance", "/attendance", "check", "check attendance"}:
        _handle_refresh_request(chat_id, force_live=True)
        return
    if lowered in {"timetable", "time table", "/timetable"}:
        _handle_timetable_request(chat_id)
        return
    if lowered in {"refresh"}:
        _handle_refresh_request(chat_id, force_live=False)
        return
    if lowered in {"refresh_force", "live"}:
        _handle_refresh_request(chat_id, force_live=True)
        return

    profile = get_profile(chat_id)
    if profile:
        _send_message(chat_id, "\U0001F4AC Send /attendance whenever you want a live fetch.")
        return

    pending = _sessions.get(chat_id, {})
    if not pending.get("semester"):
        _start_onboarding(chat_id, "login")
        return

    user_id = text.split()[0].strip()
    if not user_id:
        _send_message(chat_id, "\U0001F393 Send your roll number to begin.")
        return
    if not _live_service_guard(chat_id):
        return
    _start_password_prompt(chat_id, user_id, "login")


def _handle_message(update: dict[str, Any]) -> None:
    if update.get("callback_query"):
        _handle_callback(update)
        return

    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id", "")).strip()
    text = (msg.get("text") or "").strip()
    message_id = int(msg.get("message_id", 0) or 0)

    if not chat_id:
        return

    mark_chat_seen(chat_id, source="message")

    cmd, args = _normalize_cmd(text)
    if not cmd and text:
        _handle_plain_text(chat_id, text, source_message_id=message_id)
        return

    if cmd in {"/start", "/help"}:
        if cmd == "/start":
            if _get_saved_credentials(chat_id):
                _send_message(
                    chat_id,
                    f"{_welcome_text()}\n\n\u2705 You are already registered.\nTap below whenever you want a live fetch.",
                    reply_markup=_check_attendance_markup(),
                )
                return
            _start_onboarding(chat_id, "login")
            return
        _send_message(chat_id, _help_text())
        return

    if cmd == "/login":
        _handle_login_command(chat_id, args, source_message_id=message_id)
        return

    if cmd == "/logout":
        deleted = clear_user_state(chat_id)
        _sessions.pop(chat_id, None)
        _clear_volatile_session(chat_id)
        _pending_auth.pop(chat_id, None)
        with _cache_lock:
            _attendance_cache.pop(chat_id, None)
        _send_message(chat_id, "Logged out and cleared saved state." if deleted else "No saved state found.")
        return

    if cmd == "/setyear":
        _handle_setyear(chat_id, args)
        return

    if cmd == "/setsem":
        _handle_setsem(chat_id, args)
        return

    if cmd == "/whoami":
        _handle_whoami(chat_id)
        return

    if cmd == "/attendance":
        _handle_refresh_request(chat_id, force_live=True)
        return

    if cmd == "/timetable":
        _handle_timetable_request(chat_id)
        return

    if cmd == "/server":
        _handle_server_command(chat_id)
        return

    if cmd == "/stats":
        _handle_stats_command(chat_id)
        return

    if cmd == "/broadcast":
        _handle_broadcast_command(chat_id, _command_payload(text))
        return

    if cmd == "/refresh":
        _handle_refresh_request(chat_id, force_live=False)
        return

    if cmd == "/refresh_force":
        _handle_refresh_request(chat_id, force_live=True)
        return

    _send_message(chat_id, "Unknown command. Send /help.")


def run() -> None:
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set in environment.")

    try:
        init_db()
    except Exception:
        logger.exception("init_db failed during startup")

    try:
        _configure_bot_commands()
    except Exception:
        logger.debug("Bot commands configuration failed", exc_info=True)

    try:
        start_backend_server_in_thread()
    except Exception:
        logger.exception("Backend server startup failed")

    time.sleep(1)

    try:
        clear_resp = requests.post(
            _api_url("deleteWebhook"),
            json={"drop_pending_updates": True},
            timeout=10,
        )
        if clear_resp.ok:
            logger.info("Deleted Telegram webhook and dropped pending updates")
        else:
            logger.warning("Failed to delete webhook: %s", clear_resp.status_code)
    except Exception:
        logger.debug("Webhook deletion failed", exc_info=True)

    try:
        clear_resp = requests.post(
            _api_url("getUpdates"),
            json={"timeout": 0, "allowed_updates": ["message", "edited_message", "callback_query"]},
            timeout=10,
        )
        if clear_resp.ok:
            clear_data = clear_resp.json()
            cleared = len(clear_data.get("result", []))
            logger.info("Cleared %d pending Telegram updates", cleared)
        else:
            logger.warning("Failed to clear pending updates: %s", clear_resp.status_code)
    except Exception:
        logger.debug("Pending update cleanup failed", exc_info=True)

    logger.info("Telegram bot started (long polling)")

    offset: int | None = None
    _409_backoff = 1.0
    while True:
        try:
            params: dict[str, Any] = {
                "timeout": POLL_TIMEOUT,
                "allowed_updates": json.dumps(["message", "edited_message", "callback_query"]),
            }
            if offset is not None:
                params["offset"] = offset

            resp = requests.get(_api_url("getUpdates"), params=params, timeout=POLL_TIMEOUT + 10)
            if resp.status_code == 409:
                logger.warning("Telegram 409 Conflict detected; another instance may be polling. Backing off for %.1fs", _409_backoff)
                time.sleep(_409_backoff)
                _409_backoff = min(_409_backoff * 2, 60.0)
                continue
            resp.raise_for_status()
            payload = resp.json()
            _409_backoff = 1.0

            if not payload.get("ok"):
                logger.error("Telegram getUpdates failed: %s", payload)
                time.sleep(3)
                continue

            updates = payload.get("result", [])
            for upd in updates:
                try:
                    update_id = int(upd.get("update_id", 0))
                    offset = update_id + 1
                    _handle_message(upd)
                except Exception:
                    logger.exception("Failed handling update: %s", upd)

        except requests.RequestException as exc:
            logger.warning("Telegram polling error: %s", exc)
            time.sleep(3)
        except Exception:
            logger.exception("Unexpected Telegram bot error")
            time.sleep(3)


if __name__ == "__main__":
    run()
