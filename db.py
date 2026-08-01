"""
db.py - SQLite storage plus optional Supabase sync for non-sensitive bot state.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

import requests
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "attendance_bot.db")
FERNET_KEY = os.getenv("FERNET_KEY")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
SUPABASE_PROFILE_TABLE = os.getenv("SUPABASE_PROFILE_TABLE", "telegram_profiles").strip()
SUPABASE_CACHE_TABLE = os.getenv("SUPABASE_CACHE_TABLE", "telegram_attendance_cache").strip()
SUPABASE_RUNTIME_TABLE = os.getenv("SUPABASE_RUNTIME_TABLE", "telegram_runtime_control").strip()
RUNTIME_CONTROL_KEY = "global"

if not FERNET_KEY:
    raise RuntimeError(
        "FERNET_KEY not set in environment. Generate one with: "
        "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    )

_master_key = FERNET_KEY.encode()


def _user_key(chat_id: str) -> bytes:
    salt = chat_id.encode("utf-8")
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"attendance_bot_per_user_key",
    ).derive(_master_key)
    return base64.urlsafe_b64encode(derived)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    return conn


def _encrypt(text: str, chat_id: str) -> str:
    user_fernet = Fernet(_user_key(chat_id))
    return user_fernet.encrypt(text.encode()).decode()


def _decrypt(token: str, chat_id: str) -> str:
    user_fernet = Fernet(_user_key(chat_id))
    return user_fernet.decrypt(token.encode()).decode()


def _supabase_enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


def _supabase_headers(prefer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _upsert_supabase_row(
    table: str,
    payload: dict[str, object],
    on_conflict: str = "chat_id",
) -> None:
    if not _supabase_enabled() or not table:
        return

    try:
        resp = requests.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            params={"on_conflict": on_conflict},
            headers=_supabase_headers("resolution=merge-duplicates,return=minimal"),
            json=[payload],
            timeout=15,
        )
        if resp.status_code >= 300:
            logger.warning(
                "Supabase upsert failed for table=%s status=%s body=%s",
                table,
                resp.status_code,
                resp.text[:400],
            )
    except Exception:
        logger.warning("Supabase upsert failed for table=%s", table, exc_info=True)


def _delete_supabase_row(table: str, chat_id: str) -> None:
    if not _supabase_enabled() or not table or not chat_id:
        return

    try:
        resp = requests.delete(
            f"{SUPABASE_URL}/rest/v1/{table}",
            params={"chat_id": f"eq.{chat_id}"},
            headers=_supabase_headers("return=minimal"),
            timeout=15,
        )
        if resp.status_code >= 300:
            logger.warning(
                "Supabase delete failed for table=%s status=%s body=%s",
                table,
                resp.status_code,
                resp.text[:400],
            )
    except Exception:
        logger.warning("Supabase delete failed for table=%s", table, exc_info=True)


def init_db() -> None:
    """Create local tables used by the bot."""
    with _get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                phone_number TEXT PRIMARY KEY,
                user_id      TEXT NOT NULL,
                password     TEXT NOT NULL,
                last_login   TIMESTAMP,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_profiles (
                chat_id     TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                year        TEXT,
                semester    TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_attendance_cache (
                chat_id       TEXT PRIMARY KEY,
                user_id       TEXT NOT NULL,
                year          TEXT,
                semester      TEXT,
                status        TEXT NOT NULL,
                fetched_at    TIMESTAMP NOT NULL,
                messages_json TEXT NOT NULL,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_runtime_control (
                control_key        TEXT PRIMARY KEY,
                service_enabled    INTEGER NOT NULL DEFAULT 1,
                updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_by_chat_id TEXT
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_user_history (
                chat_id          TEXT PRIMARY KEY,
                first_seen_at    TIMESTAMP NOT NULL,
                last_seen_at     TIMESTAMP NOT NULL,
                last_seen_source TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO telegram_runtime_control (
                control_key, service_enabled, updated_at, updated_by_chat_id
            )
            VALUES (?, 1, ?, NULL);
            """,
            (RUNTIME_CONTROL_KEY, _utcnow_iso()),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO telegram_user_history (chat_id, first_seen_at, last_seen_at, last_seen_source)
            SELECT chat_id, COALESCE(created_at, updated_at, ?), COALESCE(updated_at, created_at, ?), 'migration:profile'
            FROM telegram_profiles
            WHERE chat_id IS NOT NULL AND chat_id != '';
            """,
            (_utcnow_iso(), _utcnow_iso()),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO telegram_user_history (chat_id, first_seen_at, last_seen_at, last_seen_source)
            SELECT chat_id, COALESCE(updated_at, ?), COALESCE(updated_at, ?), 'migration:cache'
            FROM telegram_attendance_cache
            WHERE chat_id IS NOT NULL AND chat_id != '';
            """,
            (_utcnow_iso(), _utcnow_iso()),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO telegram_user_history (chat_id, first_seen_at, last_seen_at, last_seen_source)
            SELECT phone_number, COALESCE(created_at, last_login, ?), COALESCE(last_login, created_at, ?), 'migration:user'
            FROM users
            WHERE phone_number IS NOT NULL AND phone_number != '';
            """,
            (_utcnow_iso(), _utcnow_iso()),
        )


def mark_chat_seen(chat_id: str, source: str | None = None) -> None:
    """Record that this chat has interacted with the bot at least once."""
    normalized_chat_id = str(chat_id or "").strip()
    if not normalized_chat_id:
        return

    now = _utcnow_iso()
    seen_source = str(source or "message").strip() or "message"
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO telegram_user_history (chat_id, first_seen_at, last_seen_at, last_seen_source)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id)
            DO UPDATE SET last_seen_at = excluded.last_seen_at,
                          last_seen_source = excluded.last_seen_source;
            """,
            (normalized_chat_id, now, now, seen_source),
        )


def save_user(phone_number: str, user_id: str, password: str) -> None:
    """Legacy credential storage helper kept for compatibility."""
    now = _utcnow_iso()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO users (phone_number, user_id, password, last_login, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(phone_number)
            DO UPDATE SET user_id    = excluded.user_id,
                          password   = excluded.password,
                          last_login = excluded.last_login;
            """,
            (phone_number, user_id, _encrypt(password, phone_number), now, now),
        )


def get_user(phone_number: str) -> dict | None:
    """Return decrypted legacy credential row or None."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE phone_number = ?",
            (phone_number,),
        ).fetchone()
    if row is None:
        return None
    return {
        "phone_number": row["phone_number"],
        "user_id": row["user_id"],
        "password": _decrypt(row["password"], row["phone_number"]),
        "last_login": row["last_login"],
        "created_at": row["created_at"],
    }


def delete_user(phone_number: str) -> bool:
    """Delete legacy credential row. Returns True if a row was deleted."""
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM users WHERE phone_number = ?", (phone_number,))
        return cur.rowcount > 0


def update_last_login(phone_number: str) -> None:
    """Bump the last_login timestamp for the legacy credentials table."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE users SET last_login = ? WHERE phone_number = ?",
            (_utcnow_iso(), phone_number),
        )


def save_profile(chat_id: str, user_id: str, year: str | None = None, semester: str | None = None) -> None:
    """Persist non-sensitive user profile state."""
    now = _utcnow_iso()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO telegram_profiles (chat_id, user_id, year, semester, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id)
            DO UPDATE SET user_id    = excluded.user_id,
                          year       = excluded.year,
                          semester   = excluded.semester,
                          updated_at = excluded.updated_at;
            """,
            (chat_id, user_id, year, semester, now, now),
        )

    _upsert_supabase_row(
        SUPABASE_PROFILE_TABLE,
        {
            "chat_id": chat_id,
            "user_id": user_id,
            "year": year,
            "semester": semester,
            "updated_at": now,
        },
    )


def get_profile(chat_id: str) -> dict | None:
    """Return saved non-sensitive profile for the chat."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM telegram_profiles WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "chat_id": row["chat_id"],
        "user_id": row["user_id"],
        "year": row["year"],
        "semester": row["semester"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def update_profile_filters(chat_id: str, year: str | None = None, semester: str | None = None) -> bool:
    """Update saved year/semester if a profile already exists."""
    profile = get_profile(chat_id)
    if not profile:
        return False

    merged_year = year if year is not None else profile.get("year")
    merged_semester = semester if semester is not None else profile.get("semester")
    save_profile(chat_id, profile["user_id"], merged_year, merged_semester)
    return True


def delete_profile(chat_id: str) -> bool:
    """Delete saved non-sensitive profile."""
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM telegram_profiles WHERE chat_id = ?", (chat_id,))
        deleted = cur.rowcount > 0
    _delete_supabase_row(SUPABASE_PROFILE_TABLE, chat_id)
    return deleted


def save_attendance_cache(
    chat_id: str,
    user_id: str,
    messages: list[str],
    status: str,
    year: str | None = None,
    semester: str | None = None,
    fetched_at: float | None = None,
) -> None:
    """Persist the last successful attendance response for quick reuse."""
    ts = float(fetched_at if fetched_at is not None else _utcnow().timestamp())
    now = _utcnow_iso()
    messages_json = json.dumps(messages, ensure_ascii=False)

    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO telegram_attendance_cache (
                chat_id, user_id, year, semester, status, fetched_at, messages_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id)
            DO UPDATE SET user_id       = excluded.user_id,
                          year          = excluded.year,
                          semester      = excluded.semester,
                          status        = excluded.status,
                          fetched_at    = excluded.fetched_at,
                          messages_json = excluded.messages_json,
                          updated_at    = excluded.updated_at;
            """,
            (chat_id, user_id, year, semester, status, ts, messages_json, now),
        )

    _upsert_supabase_row(
        SUPABASE_CACHE_TABLE,
        {
            "chat_id": chat_id,
            "user_id": user_id,
            "year": year,
            "semester": semester,
            "status": status,
            "fetched_at": ts,
            "messages_json": messages_json,
            "updated_at": now,
        },
    )


def get_attendance_cache(chat_id: str) -> dict | None:
    """Return the last persisted attendance cache entry."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM telegram_attendance_cache WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
    if row is None:
        return None

    try:
        messages = json.loads(row["messages_json"])
        if not isinstance(messages, list):
            messages = []
    except Exception:
        messages = []

    return {
        "chat_id": row["chat_id"],
        "user_id": row["user_id"],
        "year": row["year"],
        "semester": row["semester"],
        "status": row["status"],
        "messages": messages,
        "ts": float(row["fetched_at"] or 0.0),
        "updated_at": row["updated_at"],
    }


def get_bot_stats(now_ts: float | None = None) -> dict[str, int]:
    """Return lightweight aggregate usage stats for admin dashboards."""
    current_ts = float(now_ts if now_ts is not None else _utcnow().timestamp())
    last_24h = current_ts - 86400.0
    last_7d = current_ts - (7 * 86400.0)

    with _get_conn() as conn:
        registered_users = int(
            conn.execute("SELECT COUNT(DISTINCT chat_id) FROM telegram_profiles").fetchone()[0] or 0
        )
        total_users_ever = int(
            conn.execute("SELECT COUNT(DISTINCT chat_id) FROM telegram_user_history").fetchone()[0] or 0
        )
        saved_credentials = int(
            conn.execute("SELECT COUNT(DISTINCT phone_number) FROM users").fetchone()[0] or 0
        )
        cached_users = int(
            conn.execute("SELECT COUNT(DISTINCT chat_id) FROM telegram_attendance_cache").fetchone()[0] or 0
        )
        successful_fetch_users = int(
            conn.execute(
                "SELECT COUNT(DISTINCT chat_id) FROM telegram_attendance_cache WHERE status = ?",
                ("success",),
            ).fetchone()[0]
            or 0
        )
        active_last_24h = int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT chat_id)
                FROM telegram_attendance_cache
                WHERE status = ? AND fetched_at >= ?
                """,
                ("success", last_24h),
            ).fetchone()[0]
            or 0
        )
        active_last_7d = int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT chat_id)
                FROM telegram_attendance_cache
                WHERE status = ? AND fetched_at >= ?
                """,
                ("success", last_7d),
            ).fetchone()[0]
            or 0
        )

    return {
        "total_users_ever": total_users_ever,
        "registered_users": registered_users,
        "saved_credentials": saved_credentials,
        "cached_users": cached_users,
        "successful_fetch_users": successful_fetch_users,
        "active_last_24h": active_last_24h,
        "active_last_7d": active_last_7d,
    }


def get_all_known_chat_ids() -> list[str]:
    """Return every known bot chat ID from saved profile/cache/credential tables."""
    chat_ids: set[str] = set()
    with _get_conn() as conn:
        for (value,) in conn.execute("SELECT chat_id FROM telegram_profiles WHERE chat_id IS NOT NULL AND chat_id != ''"):
            chat_ids.add(str(value).strip())
        for (value,) in conn.execute(
            "SELECT chat_id FROM telegram_attendance_cache WHERE chat_id IS NOT NULL AND chat_id != ''"
        ):
            chat_ids.add(str(value).strip())
        for (value,) in conn.execute("SELECT phone_number FROM users WHERE phone_number IS NOT NULL AND phone_number != ''"):
            chat_ids.add(str(value).strip())
    return sorted(chat_id for chat_id in chat_ids if chat_id)


def delete_attendance_cache(chat_id: str) -> bool:
    """Delete the persisted attendance cache for a chat."""
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM telegram_attendance_cache WHERE chat_id = ?", (chat_id,))
        deleted = cur.rowcount > 0
    _delete_supabase_row(SUPABASE_CACHE_TABLE, chat_id)
    return deleted


def clear_user_state(chat_id: str) -> bool:
    """Delete all stored state for a chat, including any legacy credentials."""
    deleted_any = False
    deleted_any = delete_attendance_cache(chat_id) or deleted_any
    deleted_any = delete_profile(chat_id) or deleted_any
    deleted_any = delete_user(chat_id) or deleted_any
    return deleted_any


def get_service_control() -> dict[str, object]:
    """Return the current service runtime control state."""
    with _get_conn() as conn:
        row = conn.execute(
            """
            SELECT control_key, service_enabled, updated_at, updated_by_chat_id
            FROM telegram_runtime_control
            WHERE control_key = ?
            """,
            (RUNTIME_CONTROL_KEY,),
        ).fetchone()

    if row is None:
        return set_service_enabled(True, updated_by_chat_id=None)

    return {
        "control_key": row["control_key"],
        "service_enabled": bool(row["service_enabled"]),
        "updated_at": row["updated_at"],
        "updated_by_chat_id": row["updated_by_chat_id"],
    }


def set_service_enabled(enabled: bool, updated_by_chat_id: str | None) -> dict[str, object]:
    """Enable or pause live attendance fetching globally."""
    now = _utcnow_iso()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO telegram_runtime_control (
                control_key, service_enabled, updated_at, updated_by_chat_id
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(control_key)
            DO UPDATE SET service_enabled    = excluded.service_enabled,
                          updated_at         = excluded.updated_at,
                          updated_by_chat_id = excluded.updated_by_chat_id;
            """,
            (RUNTIME_CONTROL_KEY, 1 if enabled else 0, now, updated_by_chat_id),
        )

    _upsert_supabase_row(
        SUPABASE_RUNTIME_TABLE,
        {
            "control_key": RUNTIME_CONTROL_KEY,
            "service_enabled": enabled,
            "updated_at": now,
            "updated_by_chat_id": updated_by_chat_id,
        },
        on_conflict="control_key",
    )

    return {
        "control_key": RUNTIME_CONTROL_KEY,
        "service_enabled": bool(enabled),
        "updated_at": now,
        "updated_by_chat_id": updated_by_chat_id,
    }


if __name__ == "__main__":
    os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())
    init_db()
    save_profile("12345", "2024UME4113", "2025-26", "4")
    save_attendance_cache("12345", "2024UME4113", ["Cached summary"], "success")
    print("Profile:", get_profile("12345"))
    print("Cache:", get_attendance_cache("12345"))
    clear_user_state("12345")
