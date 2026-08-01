"""
backend_server.py - Lightweight Flask backend for health and service control.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from werkzeug.serving import make_server

from db import get_service_control, init_db, set_service_enabled
from utils import setup_logging

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
setup_logging()
logger = logging.getLogger(__name__)

BACKEND_SERVER_ENABLED = os.getenv("BACKEND_SERVER_ENABLED", "1") == "1"
BACKEND_SERVER_HOST = os.getenv("BACKEND_SERVER_HOST", "127.0.0.1").strip()
BACKEND_SERVER_PORT = int(os.getenv("BACKEND_SERVER_PORT", "8080"))
BACKEND_PUBLIC_BASE_URL = os.getenv("BACKEND_PUBLIC_BASE_URL", "").rstrip("/")
BACKEND_CONTROL_TOKEN = os.getenv("BACKEND_CONTROL_TOKEN", "").strip()

_server_lock = threading.Lock()
_server_thread: threading.Thread | None = None
_server_instance = None
_started_at = datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_backend_base_url() -> str:
    if BACKEND_PUBLIC_BASE_URL:
        return BACKEND_PUBLIC_BASE_URL
    return f"http://{BACKEND_SERVER_HOST}:{BACKEND_SERVER_PORT}"


def _build_status_payload() -> dict[str, Any]:
    control = get_service_control()
    return {
        "ok": True,
        "backend": "running",
        "service_enabled": bool(control.get("service_enabled")),
        "updated_at": control.get("updated_at"),
        "updated_by_chat_id": control.get("updated_by_chat_id"),
        "started_at": _started_at.isoformat(),
        "server_time": _utcnow_iso(),
        "base_url": get_backend_base_url(),
    }


def _authorized(req) -> bool:
    if not BACKEND_CONTROL_TOKEN:
        return True
    supplied = (req.headers.get("X-Control-Token") or req.args.get("token") or "").strip()
    return supplied == BACKEND_CONTROL_TOKEN


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/health")
    def health() -> Any:
        return jsonify(_build_status_payload())

    @app.get("/status")
    def status() -> Any:
        return jsonify(_build_status_payload())

    @app.post("/service/start")
    def service_start() -> Any:
        if not _authorized(request):
            return jsonify({"ok": False, "error": "unauthorized"}), 403
        control = set_service_enabled(True, updated_by_chat_id="backend_api")
        return jsonify({"ok": True, **control})

    @app.post("/service/stop")
    def service_stop() -> Any:
        if not _authorized(request):
            return jsonify({"ok": False, "error": "unauthorized"}), 403
        control = set_service_enabled(False, updated_by_chat_id="backend_api")
        return jsonify({"ok": True, **control})

    return app


def start_backend_server_in_thread() -> threading.Thread | None:
    """Start the backend status server once in a daemon thread."""
    if not BACKEND_SERVER_ENABLED:
        logger.info("Backend server is disabled by configuration.")
        return None

    global _server_thread, _server_instance
    with _server_lock:
        if _server_thread and _server_thread.is_alive():
            return _server_thread

        init_db()
        app = create_app()
        try:
            _server_instance = make_server(BACKEND_SERVER_HOST, BACKEND_SERVER_PORT, app)
        except Exception:
            logger.exception(
                "Backend server failed to bind on %s. Continuing without embedded backend server.",
                get_backend_base_url(),
            )
            return None

        def _serve() -> None:
            logger.info("Backend server listening on %s", get_backend_base_url())
            _server_instance.serve_forever()

        _server_thread = threading.Thread(
            target=_serve,
            name="attendance-backend-server",
            daemon=True,
        )
        _server_thread.start()
        return _server_thread


def run() -> None:
    init_db()
    app = create_app()
    logger.info("Backend server listening on %s", get_backend_base_url())
    app.run(host=BACKEND_SERVER_HOST, port=BACKEND_SERVER_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    run()
