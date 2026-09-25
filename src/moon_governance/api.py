"""云赏月内容治理服务的 HTTP/JSON 边界，复用基础层路由。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from festival_foundation.api import route as foundation_route
from festival_foundation.errors import DomainError
from festival_foundation.storage import Database

from .service import GovernanceService


def _write_result(result: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return (200 if result.get("replayed") else 201), result


def _dispatch(service: GovernanceService, method: str, segments: list[str],
              actor_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
    """匹配治理接口，未命中时返回 None 交给基础层路由。"""
    if method == "POST" and segments == ["submissions"]:
        return _write_result(service.submit_submission(actor_id=actor_id, **body))
    if len(segments) == 3 and segments[0] == "submissions":
        submission_id = segments[1]
        if method == "POST" and segments[2] == "revisions":
            return _write_result(service.revise_submission(
                actor_id=actor_id, submission_id=submission_id, **body))
        if method == "POST" and segments[2] == "withdraw":
            return _write_result(service.withdraw_submission(
                actor_id=actor_id, submission_id=submission_id, **body))
        if method == "POST" and segments[2] == "compliance":
            return _write_result(service.compliance_remove(
                actor_id=actor_id, submission_id=submission_id, **body))
        if method == "POST" and segments[2] == "appeals":
            return _write_result(service.request_appeal(
                actor_id=actor_id, submission_id=submission_id, **body))
    if len(segments) == 2 and segments[0] == "submissions" and method == "GET":
        return 200, service.get_submission(segments[1])
    if len(segments) == 3 and segments[0] == "submissions" and method == "GET":
        if segments[2] == "content":
            return 200, service.read_submission_content(segments[1])
        if segments[2] == "explain":
            return 200, service.explain_submission(segments[1])
    if len(segments) == 3 and segments[0] == "versions" and segments[2] == "explain" \
            and method == "GET":
        return 200, service.explain_version(segments[1])
    if method == "POST" and segments == ["review-claims"]:
        return _write_result(service.claim_review(actor_id=actor_id, **body))
    if len(segments) == 3 and segments[0] == "review-tasks" and segments[2] == "decisions" \
            and method == "POST":
        return _write_result(service.decide_review(actor_id=actor_id, task_id=segments[1], **body))
    if method == "GET" and segments == ["review-queue"]:
        return 200, {"items": service.review_queue()}
    if method == "GET" and segments == ["appeal-queue"]:
        return 200, {"items": service.appeal_queue()}
    if method == "POST" and segments == ["rule-upgrades"]:
        return _write_result(service.upgrade_rules(actor_id=actor_id, **body))
    if method == "POST" and segments == ["topics"]:
        return _write_result(service.create_topic(actor_id=actor_id, **body))
    if len(segments) == 2 and segments[0] == "topics" and method == "GET":
        return 200, service.get_topic(segments[1])
    if len(segments) == 3 and segments[0] == "topics":
        topic_id = segments[1]
        if method == "POST" and segments[2] == "members":
            return _write_result(service.add_topic_member(actor_id=actor_id, topic_id=topic_id, **body))
        if method == "POST" and segments[2] == "member-removals":
            return _write_result(service.remove_topic_member(
                actor_id=actor_id, topic_id=topic_id, **body))
        if method == "POST" and segments[2] == "publish":
            return _write_result(service.publish_topic(actor_id=actor_id, topic_id=topic_id, **body))
    return None


def route(service: GovernanceService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把 HTTP 语义请求分派到治理服务，未命中路径回落到基础层路由。"""

    headers = headers or {}
    actor_id = headers.get("X-Actor-Id", "")
    segments = [segment for segment in urlparse(path).path.split("/") if segment]
    try:
        result = _dispatch(service, method, segments, actor_id, body if body is not None else {})
        if result is not None:
            return result
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}
    return foundation_route(service, method, path, body, headers)


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为治理路由调用。"""

    service: GovernanceService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
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
    """启动云赏月内容治理 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动云赏月互动内容治理服务")
    parser.add_argument("--database", default="governance.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = GovernanceService(database)
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
