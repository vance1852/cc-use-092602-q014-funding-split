"""无第三方依赖的危房改造联合资金分摊 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import FundingError, ValidationFailed
from .service import FundingService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: FundingService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"],
                    payload.get("household_id"),
                ))
            actor = self._actor(normalized)
            if method == "POST" and path == "/households":
                return Response(201, self.service.register_household(actor, payload))
            if method == "POST" and path == "/batches":
                return Response(201, self.service.register_batch(actor, payload))
            if method == "GET" and path == "/batches":
                return Response(200, {"batches": self.service.list_batches(actor)})
            if method == "GET" and len(parts) == 2 and parts[0] == "batches":
                return Response(200, self.service.get_batch(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "carry-forward":
                return Response(201, self.service.carry_forward_batch(actor, parts[1], payload))
            if method == "POST" and path == "/rules":
                return Response(201, self.service.create_rule(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "rules":
                version = query.get("version", [None])[0]
                return Response(200, self.service.get_rule(
                    actor, parts[1], None if version is None else int(version),
                ))
            if method == "POST" and len(parts) == 3 and parts[0] == "rules" and parts[2] == "activate":
                return Response(200, self.service.activate_rule(actor, parts[1], int(payload["version"])))
            if method == "POST" and len(parts) == 3 and parts[0] == "rules" and parts[2] == "revise":
                return Response(201, self.service.revise_rule(actor, parts[1], payload))
            if method == "POST" and path == "/projects":
                return Response(201, self.service.register_project(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "projects":
                return Response(200, self.service.get_project(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "allocation":
                return Response(200, self.service.get_allocation(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "confirm":
                return Response(200, self.service.confirm_project(actor, parts[1], payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "settle":
                return Response(200, self.service.settle_project(actor, parts[1], payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "projects" and parts[2] == "adjustments":
                return Response(201, self.service.propose_adjustment(actor, parts[1], payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "adjustments" and parts[2] == "review":
                return Response(200, self.service.review_adjustment(actor, parts[1], payload))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except FundingError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    # 单个 SQLite 连接上的事务状态是连接级的，请求处理整体串行化，
    # 避免两个写事务在同一连接上交错。
    mutex = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "RenovationFunding/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            with mutex:
                response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动危房改造联合资金分摊服务")
    parser.add_argument("--database", type=Path, default=Path("renovation_funding.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(FundingService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
