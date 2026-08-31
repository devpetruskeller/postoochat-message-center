from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent
STORE_DIR = BASE_DIR / "store"
HOST = os.environ.get("MS_CLIENT_HOST", "127.0.0.1")
PORT = int(os.environ.get("MS_CLIENT_PORT", "8876"))
MAX_NOTIFICATION_BYTES = 64 * 1024
MAX_EXPORT_BYTES = 50 * 1024 * 1024


def allowed_collection_hosts() -> set[str]:
    return {
        value.strip().lower()
        for value in os.environ.get("MS_CLIENT_ALLOWED_COLLECTION_HOSTS", "").split(",")
        if value.strip()
    }


def validate_collection_url(value: Any) -> str:
    collection_url = str(value or "").strip()
    parsed = urlparse(collection_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("invalid_collection_url")
    allowed_hosts = allowed_collection_hosts()
    if allowed_hosts and parsed.hostname.lower() not in allowed_hosts:
        raise ValueError("collection_host_not_allowed")
    return collection_url


def validate_bearer_header(value: Any) -> str:
    authorization = str(value or "").strip()
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise ValueError("missing_collector_token")
    return f"Bearer {token.strip()}"


def collect_export(collection_url: str, authorization: str) -> dict[str, Any]:
    request = Request(
        collection_url,
        headers={
            "Accept": "application/json",
            "Authorization": authorization,
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=30) as response:
            content_length = int(response.headers.get("Content-Length", "0") or "0")
            if content_length > MAX_EXPORT_BYTES:
                raise ValueError("export_too_large")
            body = response.read(MAX_EXPORT_BYTES + 1)
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"collection_http_{error.code}: {detail or error.reason}") from error
    except URLError as error:
        raise RuntimeError(f"collection_failed: {error.reason}") from error

    if len(body) > MAX_EXPORT_BYTES:
        raise ValueError("export_too_large")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid_export_json") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        raise ValueError("invalid_export_payload")
    return payload


def safe_collector_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_") or "collector"


def save_import(payload: dict[str, Any], collector: str) -> Path:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    filename = f"messages.import-{timestamp}.json"
    target = STORE_DIR / filename
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return target


class CollectionClientHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if urlparse(self.path).path != "/message-export-ready":
            self.respond_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0") or "0")
            if content_length <= 0 or content_length > MAX_NOTIFICATION_BYTES:
                raise ValueError("invalid_notification_size")
            notification = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if not isinstance(notification, dict) or notification.get("event") != "message_export.ready":
                raise ValueError("invalid_notification")
            authorization = validate_bearer_header(self.headers.get("Authorization", ""))
            collection_url = validate_collection_url(notification.get("collection_url"))
            export_payload = collect_export(collection_url, authorization)
            collector = safe_collector_name(notification.get("collector"))
            saved_path = save_import(export_payload, collector)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
            self.respond_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return
        except RuntimeError as error:
            self.respond_json({"error": str(error)}, status=HTTPStatus.BAD_GATEWAY)
            return

        self.respond_json({
            "ok": True,
            "collector": collector,
            "message_count": len(export_payload.get("messages", [])),
            "saved_path": str(saved_path),
        })

    def respond_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), CollectionClientHandler)
    print(f"Collection test client running at http://{HOST}:{PORT}/message-export-ready")
    print(f"Imports will be saved in {STORE_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
