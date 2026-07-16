import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request

from costweave.server import build_server


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = build_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def fetch_json(self, path, payload=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def request_json(self, path, payload=None, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(url, data=data, headers=headers or {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_health_and_catalog(self):
        status, health = self.fetch_json("/api/health")
        self.assertEqual(200, status)
        self.assertEqual("offline-simulation", health["runtime"])
        _, catalog = self.fetch_json("/api/catalog")
        self.assertGreaterEqual(len(catalog["workers"]), 6)

    def test_static_paths_cannot_escape_web_root(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as client:
            client.sendall(
                b"GET /x\\..\\..\\..\\README.md HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            response = b""
            while True:
                chunk = client.recv(8192)
                if not chunk:
                    break
                response += chunk
        status_line, _, body = response.partition(b"\r\n\r\n")
        self.assertNotIn(b" 200 ", status_line)
        self.assertNotIn(b"# CostWeave", body)

    def test_request_schema_and_cross_origin_are_rejected(self):
        status, _ = self.request_json("/api/runs", [])
        self.assertEqual(400, status)
        status, _ = self.request_json("/api/runs", {
            "goal": "这是一个足够长的测试目标",
            "simulate_replan": "false",
        })
        self.assertEqual(400, status)
        status, _ = self.request_json(
            "/api/runs",
            {"goal": "这是一个足够长的测试目标"},
            {"Content-Type": "text/plain", "Origin": "https://evil.example"},
        )
        self.assertIn(status, {403, 415})

    def test_unknown_api_is_json_404(self):
        status, body = self.request_json("/api/does-not-exist")
        self.assertEqual(404, status)
        self.assertEqual("not_found", body["error"])

    def test_public_bind_is_refused_without_authentication(self):
        with self.assertRaises(ValueError):
            build_server("0.0.0.0", 0)

    def test_create_and_poll_run(self):
        status, run = self.fetch_json("/api/runs", {
            "goal": "开发一个数据分析应用并形成报告",
            "budget": 2,
            "max_concurrency": 4,
        })
        self.assertEqual(202, status)
        for _ in range(50):
            _, current = self.fetch_json(f"/api/runs/{run['id']}")
            if current["status"] in {"completed", "failed"}:
                break
            time.sleep(.1)
        self.assertEqual("completed", current["status"], current.get("error"))
        self.assertIsNotNone(current["plan"])


if __name__ == "__main__":
    unittest.main()
