"""HTTP 入口。

角色通过 X-Role 请求头声明：judge（裁判）、rescue（救援）、public（公众，默认）。
服务只按角色授权，不做身份认证，生产部署应置于认证网关之后。
"""

from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from scripts.migrate import migrate
from .service import JUDGE, PUBLIC, RESCUE, ROLES, ApiError, Service
from .store import Store, connect

_DB_LOCK = threading.RLock()


def build_service(database_path: str) -> Service:
    migrate(database_path)
    conn = connect(database_path)
    conn.execute("PRAGMA journal_mode=WAL")
    return Service(Store(conn))


class Handler(BaseHTTPRequestHandler):
    service: Service  # 由 main 注入到类属性

    # ---- 基础工具 ---------------------------------------------------------

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            raise ApiError(400, "invalid_json", "请求体不是合法 JSON")
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_body", "请求体必须是 JSON 对象")
        return payload

    def _role(self) -> str:
        role = self.headers.get("X-Role", PUBLIC)
        if role not in ROLES:
            raise ApiError(400, "invalid_role", f"X-Role 必须是 {', '.join(ROLES)} 之一")
        return role

    def _handle(self, fn):
        try:
            with _DB_LOCK:
                status, payload = fn()
            self._send_json(status, payload)
        except ApiError as err:
            body: dict[str, Any] = {"error": err.code, "message": err.message}
            body.update(err.extra)
            self._send_json(err.status, body)
        except ValueError as exc:
            self._send_json(400, {"error": "invalid_field", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 统一错误边界，避免断连
            self._send_json(500, {"error": "internal_error", "message": str(exc)})

    # ---- 路由 -------------------------------------------------------------

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        self._handle(lambda: self._route_get(path))

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        self._handle(lambda: self._route_post(path))

    def do_PUT(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        self._handle(lambda: self._route_put(path))

    def _route_get(self, path: str) -> tuple[int, Any]:
        role = self._role()
        m = re.fullmatch(r"/legs/([^/]+)/decision", path)
        if m:
            return 200, self.service.leg_decision(m.group(1))
        m = re.fullmatch(r"/legs/([^/]+)/attempts", path)
        if m:
            if role != JUDGE:
                raise ApiError(403, "forbidden", "发令判定记录仅裁判可查")
            return 200, self.service.attempts(m.group(1))
        m = re.fullmatch(r"/races/([^/]+)/decision", path)
        if m:
            return 200, self.service.race_decision(m.group(1))
        m = re.fullmatch(r"/races/([^/]+)/safety-events", path)
        if m:
            return 200, self.service.safety_events(role, m.group(1))
        raise ApiError(404, "not_found", f"未知路径 {path}")

    def _route_put(self, path: str) -> tuple[int, Any]:
        role = self._role()
        body = self._read_json()
        if path == "/races":
            return 200, self.service.put_race(role, body)
        if path == "/legs":
            return 200, self.service.put_leg(role, body)
        if path == "/entries":
            return 200, self.service.put_entry(role, body)
        raise ApiError(404, "not_found", f"未知路径 {path}")

    def _route_post(self, path: str) -> tuple[int, Any]:
        role = self._role()
        body = self._read_json()
        m = re.fullmatch(r"/events/([a-z-]+)", path)
        if m:
            return 201, self.service.record_event(m.group(1), role, body)
        m = re.fullmatch(r"/legs/([^/]+)/(start|resume|sign)", path)
        if m:
            leg_ref, action = m.group(1), m.group(2)
            if action == "start":
                return 200, self.service.start(role, leg_ref, body)
            if action == "resume":
                return 200, self.service.resume(role, leg_ref, body)
            return 200, self.service.sign_result(role, leg_ref, body)
        raise ApiError(404, "not_found", f"未知路径 {path}")

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    database_path = os.getenv("DATABASE_PATH", "data/app.sqlite3")
    Handler.service = build_service(database_path)
    port = int(os.getenv("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
