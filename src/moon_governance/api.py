"""提供云赏月内容治理服务的 HTTP/JSON 边界，并复用基础层路由。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from festival_foundation import api as foundation_api
from festival_foundation.errors import DomainError
from festival_foundation.service import DomainService
from festival_foundation.storage import Database

from .service import MoonGovernanceService


def route(moon: MoonGovernanceService, foundation: DomainService, method: str, path: str,
          body: dict[str, Any] | None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到治理服务，未命中时回落到基础层路由。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    segments = [segment for segment in parsed.path.split("/") if segment]
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "POST" and segments == ["submissions"]:
            return _created(moon.submit_submission(actor_id=actor_id, **body))
        if segments and segments[0] == "submissions" and len(segments) >= 2:
            submission_id = segments[1]
            rest = segments[2:]
            if method == "POST" and rest == ["versions"]:
                return _created(moon.add_version(actor_id=actor_id, submission_id=submission_id, **body))
            if method == "POST" and rest == ["withdraw"]:
                return _created(moon.withdraw_submission(actor_id=actor_id, submission_id=submission_id, **body))
            if method == "POST" and rest == ["takedown"]:
                return _created(moon.takedown_submission(actor_id=actor_id, submission_id=submission_id, **body))
            if method == "POST" and rest == ["appeals"]:
                return _created(moon.request_appeal(actor_id=actor_id, submission_id=submission_id, **body))
            if method == "GET" and not rest:
                return 200, moon.get_submission(submission_id)
            if method == "GET" and rest == ["explain"]:
                return 200, moon.explain_submission(submission_id)
            if method == "GET" and len(rest) == 2 and rest[0] == "versions":
                return 200, moon.get_version(submission_id, int(rest[1]))
        if method == "GET" and segments == ["review-queue"]:
            kind = parse_qs(parsed.query).get("kind", [None])[0]
            return 200, {"items": moon.list_review_queue(kind)}
        if segments and segments[0] == "review-tasks" and len(segments) >= 2:
            task_id = segments[1]
            rest = segments[2:]
            if method == "GET" and not rest:
                return 200, moon.get_review_task(task_id)
            if method == "POST" and rest == ["lease"]:
                return 200, moon.acquire_lease(actor_id=actor_id, task_id=task_id,
                                               lease_seconds=body.get("lease_seconds", 300))
            if method == "POST" and rest == ["release"]:
                return 200, moon.release_lease(actor_id=actor_id, task_id=task_id)
            if method == "POST" and rest == ["decision"]:
                return _created(moon.submit_decision(actor_id=actor_id, task_id=task_id, **body))
        if method == "POST" and segments == ["rules", "upgrade"]:
            return _created(moon.upgrade_rules(actor_id=actor_id, **body))
        if method == "POST" and segments == ["collections"]:
            return _created(moon.create_collection(actor_id=actor_id, **body))
        if segments and segments[0] == "collections" and len(segments) >= 2:
            collection_id = segments[1]
            rest = segments[2:]
            if method == "GET" and not rest:
                return 200, moon.get_collection(collection_id)
            if method == "POST" and rest == ["items"]:
                return _created(moon.add_collection_item(actor_id=actor_id, collection_id=collection_id, **body))
            if method == "POST" and rest == ["publish"]:
                return _created(moon.publish_collection(actor_id=actor_id, collection_id=collection_id, **body))
            if method == "GET" and rest == ["publication"]:
                return 200, moon.get_published_collection(collection_id)
        return foundation_api.route(foundation, method, path, body, headers)
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _created(receipt) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else 201), receipt.__dict__


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    moon: MoonGovernanceService
    foundation: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.moon, self.foundation, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动云赏月互动内容治理服务")
    parser.add_argument("--database", default="moon_governance.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.foundation = DomainService(database)
    Handler.moon = MoonGovernanceService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
