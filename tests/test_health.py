
import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from app.main import make_server
from app.store import Store


class HealthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "health.sqlite3")
        self.server = make_server("127.0.0.1", 0, self.store)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    def _get(self, path: str) -> tuple[int, dict]:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_health_ok(self) -> None:
        status, body = self._get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_unknown_leg_decision_is_not_open(self) -> None:
        status, body = self._get("/v1/legs/LEG-NEW/decision")
        self.assertEqual(status, 200)
        self.assertFalse(body["open"])
        self.assertEqual(body["state"], "not_open")
        # 公众视图必须带更新时间与依据
        self.assertIn("updated_at", body)
        self.assertIn("basis", body["decision"])


if __name__ == "__main__":
    unittest.main()
