from __future__ import annotations

import os
import socket
import threading
import time
from urllib.parse import quote, urlparse

import requests


def _base_url() -> str:
    return os.getenv("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001").rstrip("/")


def get_vendor_risk(vendor_name: str, timeout_seconds: float = 3.0) -> dict:
    """Low-level API client (starter). Raises on 404/503 - use ``fetch_vendor_risk`` for agent tools."""
    url = f"{_base_url()}/vendor-risk/{quote(vendor_name, safe='')}"
    response = requests.get(url, timeout=timeout_seconds)
    response.raise_for_status()
    return response.json()


# --------------------------------------------------------------------------- embedded mock service

_server_lock = threading.Lock()
_server_started = False


def ensure_mock_api(timeout_seconds: float = 10.0) -> bool:
    """Start the mock vendor-risk API inside this process if nothing is listening yet.

    Lets ``handle_request`` / the eval runners work from a clean checkout without a second terminal.
    Only ever auto-starts for a localhost base URL. Returns True when the API answers /health.
    """
    global _server_started
    base = _base_url()
    parsed = urlparse(base)
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        return False

    def healthy() -> bool:
        try:
            return requests.get(f"{base}/health", timeout=0.5).ok
        except requests.RequestException:
            return False

    if healthy():
        return True
    with _server_lock:
        if healthy():
            return True
        if not _server_started:
            import uvicorn
            from mock_api.app import app

            port = parsed.port or 8001
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:  # port taken by something that is not our API
                    return False
            config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
            server = uvicorn.Server(config)
            threading.Thread(target=server.run, daemon=True, name="mock-vendor-risk-api").start()
            _server_started = True
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if healthy():
                return True
            time.sleep(0.1)
    return False


def fetch_vendor_risk(vendor_name: str, timeout_seconds: float = 3.0) -> dict:
    """Agent-facing wrapper. Never raises; always returns a normalised envelope.

    {"status": "ok" | "not_found" | "unavailable", "data": {...}|None, "error": str|None, "http_status": int|None}
    A failure is reported as a failure - it is never turned into a favourable status.
    """
    last_error = "unknown error"
    http_status = None
    for attempt in range(2):  # one retry for transient failures
        try:
            data = get_vendor_risk(vendor_name, timeout_seconds)
            return {"status": "ok", "data": data, "error": None, "http_status": 200}
        except requests.HTTPError as exc:
            http_status = exc.response.status_code if exc.response is not None else None
            try:
                detail = exc.response.json().get("detail")
            except Exception:
                detail = None
            last_error = f"HTTP {http_status}: {detail or exc}"
            if http_status == 404:
                return {"status": "not_found", "data": None, "error": last_error, "http_status": 404}
        except requests.ConnectionError as exc:
            last_error = f"connection failed: {type(exc).__name__}"
            if attempt == 0 and os.getenv("AUTOSTART_MOCK_API", "1") != "0" and ensure_mock_api():
                continue
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.15)
    return {"status": "unavailable", "data": None, "error": last_error, "http_status": http_status}
