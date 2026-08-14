#!/usr/bin/env python3
"""Minimal HTTP UI + JSON API for Ask Hospilot."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent

from llm import load_dotenv_file  # noqa: E402

load_dotenv_file(ROOT / ".env")

from ask import ask  # noqa: E402
from init_db import DEFAULT_DB, init_db  # noqa: E402

STATIC = ROOT / "static"
PORT = int(os.getenv("PORT") or 8765)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {args[0]}")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload, default=str).encode("utf-8")
        self._send(code, raw, "application/json; charset=utf-8")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            html = (STATIC / "index.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
            return
        if path == "/health":
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/ask":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid JSON"})
            return
        question = (payload.get("question") or "").strip()
        if not question:
            self._json(400, {"error": "question is required"})
            return
        try:
            result = ask(question)
            self._json(200, result.to_dict())
        except Exception as e:  # noqa: BLE001 — surface to UI
            self._json(500, {"error": str(e)})


def main() -> None:
    if not DEFAULT_DB.exists():
        init_db(DEFAULT_DB)
        print(f"Initialized {DEFAULT_DB}")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Ask Hospilot at http://127.0.0.1:{PORT}")
    print("POST /api/ask  {\"question\": \"...\"}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
