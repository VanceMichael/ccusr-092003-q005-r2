"""HTTP 端到端测试：真实 socket 上验证角色头、路由与 JSON 契约。"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from app.main import Handler, build_service


class Server:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db = str(Path(self.tmp.name) / "http.sqlite3")
        Handler.service = build_service(db)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def request(self, method: str, path: str, body=None, role: str | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if role:
            headers["X-Role"] = role
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))


class HttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.srv = Server()
        self.req = self.srv.request
        self._setup_race()

    def tearDown(self) -> None:
        self.srv.stop()

    def _setup_race(self) -> None:
        self.req("PUT", "/races", {"race_ref": "RACE-1", "title": "测试赛"}, role="judge")
        self.req("PUT", "/legs", {
            "leg_ref": "LEG-1", "race_ref": "RACE-1", "track_ref": "WW-1",
            "title": "绕标赛", "seq": 1, "required_zones": ["ZONE-1"],
            "wind_min_kn": 3, "wind_max_kn": 20, "visibility_min_m": 1000,
        }, role="judge")
        self.req("PUT", "/entries", {
            "entry_ref": "TEAM-A", "race_ref": "RACE-1", "boat_ref": "BOAT-1",
        }, role="judge")

    def _satisfy(self, t: str) -> None:
        self.req("POST", "/events/course-revision", {
            "race_ref": "RACE-1", "leg_ref": "LEG-1", "occurred_at": t,
            "revision": 1, "track_ref": "WW-1", "confirmed": True,
        }, role="judge")
        for kind, value, ref in (("wind", 10.0, "OW"), ("visibility", 3000.0, "OV")):
            self.req("POST", "/events/observation", {
                "race_ref": "RACE-1", "leg_ref": "LEG-1", "occurred_at": t,
                "kind": kind, "value": value, "observation_ref": ref,
            }, role="judge")
        self.req("POST", "/events/rescue-coverage", {
            "race_ref": "RACE-1", "leg_ref": "LEG-1", "occurred_at": t,
            "zone": "ZONE-1", "active": True,
        }, role="rescue")
        for etype, extra in (
            ("checkin", {"fleet_ref": "TEAM-A"}),
            ("boat-check", {"fleet_ref": "TEAM-A", "status": "passed"}),
            ("training", {"fleet_ref": "TEAM-A", "status": "completed"}),
        ):
            body = {"race_ref": "RACE-1", "occurred_at": t, **extra}
            if etype == "checkin":
                body["leg_ref"] = "LEG-1"
            self.req("POST", f"/events/{etype}", body, role="judge")

    def test_health(self) -> None:
        status, body = self.req("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_public_blocks_internal_and_start_requires_judge(self) -> None:
        status, _ = self.req("POST", "/legs/LEG-1/start",
                             {"occurred_at": "2026-09-20T09:00:00+08:00"})
        self.assertEqual(status, 403)  # 默认 public
        status, body = self.req("GET", "/legs/LEG-1/attempts", role="rescue")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")

    def test_buoy_drift_story_over_http(self) -> None:
        t0 = "2026-09-20T08:40:00+08:00"
        self._satisfy(t0)

        status, body = self.req("POST", "/legs/LEG-1/start",
                                {"occurred_at": "2026-09-20T09:00:00+08:00"},
                                role="judge")
        self.assertEqual(status, 200)
        self.assertTrue(body["permitted"])

        # 直播端（公众）此时看到 open
        status, public = self.req("GET", "/legs/LEG-1/decision")
        self.assertEqual(status, 200)
        self.assertEqual(public["status"], "open")
        self.assertTrue(public["ready"])
        self.assertIn("basis", public)
        self.assertIn("updated_at", public)

        # 浮标移位 → 暂停；直播端立即变为 suspended，不再允许出发
        self.req("POST", "/events/suspend", {
            "race_ref": "RACE-1", "leg_ref": "LEG-1",
            "occurred_at": "2026-09-20T09:31:00+08:00",
            "reason": "浮标移位",
        }, role="judge")
        status, public = self.req("GET", "/legs/LEG-1/decision")
        self.assertEqual(public["status"], "suspended")
        self.assertFalse(public["ready"])

        status, body = self.req("POST", "/legs/LEG-1/start",
                                {"occurred_at": "2026-09-20T09:35:00+08:00"},
                                role="judge")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "use_resume")

        # 未重新确认前恢复被拒
        status, body = self.req("POST", "/legs/LEG-1/resume",
                                {"occurred_at": "2026-09-20T09:50:00+08:00"},
                                role="judge")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "preconditions_not_met")

        # 复核新赛道版本 + 新观测后恢复
        self.req("POST", "/events/course-revision", {
            "race_ref": "RACE-1", "leg_ref": "LEG-1",
            "occurred_at": "2026-09-20T09:50:00+08:00",
            "revision": 2, "track_ref": "WW-1", "confirmed": True,
        }, role="judge")
        self.req("POST", "/events/observation", {
            "race_ref": "RACE-1", "leg_ref": "LEG-1",
            "occurred_at": "2026-09-20T09:55:00+08:00",
            "kind": "wind", "value": 9.0, "observation_ref": "OW2",
        }, role="judge")
        self.req("POST", "/events/observation", {
            "race_ref": "RACE-1", "leg_ref": "LEG-1",
            "occurred_at": "2026-09-20T09:55:00+08:00",
            "kind": "visibility", "value": 4000.0, "observation_ref": "OV2",
        }, role="judge")
        status, body = self.req("POST", "/legs/LEG-1/resume",
                                {"occurred_at": "2026-09-20T10:00:00+08:00"},
                                role="judge")
        self.assertEqual(status, 200)
        self.assertEqual(body["basis"]["course"]["revision"], 2)

        status, public = self.req("GET", "/legs/LEG-1/decision")
        self.assertEqual(public["status"], "open")

        # 签署后冻结：迟到观测被拒，公众看到 finished
        self.req("POST", "/legs/LEG-1/sign", {
            "occurred_at": "2026-09-20T11:00:00+08:00", "result_ref": "RESULT-1",
        }, role="judge")
        status, body = self.req("POST", "/events/observation", {
            "race_ref": "RACE-1", "leg_ref": "LEG-1",
            "occurred_at": "2026-09-20T11:30:00+08:00",
            "kind": "wind", "value": 30.0, "observation_ref": "LATE",
        }, role="judge")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "leg_closed")
        status, public = self.req("GET", "/legs/LEG-1/decision")
        self.assertEqual(public["status"], "finished")
        self.assertTrue(public["frozen"])
        self.assertEqual(public["signed_result"]["result_ref"], "RESULT-1")

    def test_rescue_cannot_touch_judge_domain(self) -> None:
        status, body = self.req("POST", "/events/course-revision", {
            "race_ref": "RACE-1", "leg_ref": "LEG-1",
            "occurred_at": "2026-09-20T08:00:00+08:00",
            "revision": 1, "track_ref": "WW-1", "confirmed": True,
        }, role="rescue")
        self.assertEqual(status, 403)
        status, _ = self.req("POST", "/events/safety-incident", {
            "race_ref": "RACE-1", "occurred_at": "2026-09-20T09:00:00+08:00",
            "incident_ref": "INC-1", "summary_sha256": "a" * 64, "severity": "minor",
        }, role="judge")
        self.assertEqual(status, 403)

    def test_naive_timestamp_rejected(self) -> None:
        status, body = self.req("POST", "/events/checkin", {
            "race_ref": "RACE-1", "leg_ref": "LEG-1", "fleet_ref": "TEAM-A",
            "occurred_at": "2026-09-20T08:00:00",
        }, role="judge")
        self.assertEqual(status, 400)
        self.assertIn("occurred_at", body["message"])


if __name__ == "__main__":
    unittest.main()
