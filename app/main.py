"""安全判定服务 HTTP 入口。

接口：
  GET  /health                                 健康检查
  POST /v1/events                              按角色写入业务事件（实际发生时间）
  GET  /v1/legs/<leg_ref>/decision             唯一权威当前决定（含依据版本与更新时间）
  GET  /v1/legs/<leg_ref>/events               赛段事件审计流（只追加日志）

写入职责由事件信封中的 actor_role 声明，服务端按角色矩阵强制约束：
裁判负责暂停/恢复/发令/签署，救援协调员只维护救援覆盖与安全事件。
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from . import domain
from .domain import ValidationError
from .store import Conflict, Store


def build_handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SafetyDecision/2.0"

        # ---- 工具 -------------------------------------------------------

        def _send_json(self, status: int, body: dict | list) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                raise ValidationError("bad_request", "缺少请求体")
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("bad_request", "请求体不是合法 JSON") from exc
            if not isinstance(body, dict):
                raise ValidationError("bad_request", "请求体必须是 JSON 对象")
            return body

        def log_message(self, format: str, *args: object) -> None:
            return

        # ---- 路由 -------------------------------------------------------

        def do_GET(self) -> None:
            path = urlsplit(self.path).path.rstrip("/") or "/"
            if path == "/health":
                self._send_json(200, {"status": "ok"})
                return
            if path.startswith("/v1/legs/") and path.endswith("/decision"):
                leg_ref = unquote(path[len("/v1/legs/"): -len("/decision")])
                if leg_ref:
                    self._send_json(200, domain.current_decision(store, leg_ref))
                    return
            if path.startswith("/v1/legs/") and path.endswith("/events"):
                leg_ref = unquote(path[len("/v1/legs/"): -len("/events")])
                if leg_ref:
                    events = store.list_events(leg_ref)
                    self._send_json(200, {"leg_ref": leg_ref, "count": len(events), "events": events})
                    return
            self._send_json(404, {"error": "not_found", "message": f"无此路径：{path}"})

        def do_POST(self) -> None:
            path = urlsplit(self.path).path.rstrip("/") or "/"
            if path == "/v1/events":
                try:
                    envelope = self._read_json()
                    result = domain.ingest(store, envelope)
                except ValidationError as exc:
                    self._send_json(422, {"error": exc.code, "message": exc.message})
                    return
                except Conflict as exc:
                    self._send_json(409, {"error": "conflict", "message": str(exc)})
                    return
                status = {
                    "denied": 409,
                    "open_frozen": 201,
                    "result_signed_frozen": 201,
                }.get(result.get("status"), 200)
                self._send_json(status, result)
                return
            self._send_json(404, {"error": "not_found", "message": f"无此路径：{path}"})

    return Handler


def make_server(host: str, port: int, store: Store) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), build_handler(store))


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    database_path = os.getenv("DATABASE_PATH", "data/app.sqlite3")
    store = Store(database_path)
    server = make_server("0.0.0.0", port, store)
    print(f"安全判定服务监听 0.0.0.0:{port}，数据库 {database_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
