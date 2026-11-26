from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import opencode_go_proxy as proxy


class PathMappingTests(unittest.TestCase):
    def test_openai_path(self) -> None:
        self.assertEqual(
            proxy.map_upstream_path("/v1/chat/completions"),
            "/inference/go/openai/v1/chat/completions",
        )

    def test_anthropic_path(self) -> None:
        self.assertEqual(
            proxy.map_upstream_path("/v1/messages"),
            "/inference/go/anthropic/v1/messages",
        )

    def test_missing_v1_is_added(self) -> None:
        self.assertEqual(
            proxy.map_upstream_path("/chat/completions"),
            "/inference/go/openai/v1/chat/completions",
        )

    def test_duplicate_v1_is_fixed(self) -> None:
        self.assertEqual(
            proxy.map_upstream_path("/v1/v1/messages"),
            "/inference/go/anthropic/v1/messages",
        )

    def test_request_metadata_detects_stream_without_retaining_body(self) -> None:
        metadata = proxy.request_metadata(
            b'{"model":"test","stream":true,"input":"private","tools":[{},{}]}',
            "text/event-stream",
            "application/json",
        )
        self.assertIn("body-bytes=64", metadata)
        self.assertIn("stream=True", metadata)
        self.assertIn("model=test", metadata)
        self.assertIn("input-chars=7", metadata)
        self.assertIn("tools=2", metadata)
        self.assertIn("accept=text/event-stream", metadata)
        self.assertNotIn("private", metadata)


class FakeUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list[dict[str, object]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.__class__.requests.append(
            {
                "path": self.path,
                "headers": {name.lower(): value for name, value in self.headers.items()},
                "body": body,
            }
        )

        if "text/event-stream" in self.headers.get("Accept", ""):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            if b'"delay":true' in body:
                time.sleep(1.25)
            self.wfile.write(b"data: second\n\n")
            self.wfile.flush()
            return

        payload = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format_string: str, *args: object) -> None:
        return


class ProxyIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeUpstreamHandler.requests = []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstreamHandler)
        self.upstream_thread = threading.Thread(
            target=self.upstream.serve_forever,
            daemon=True,
        )
        self.upstream_thread.start()

        upstream_url = f"http://127.0.0.1:{self.upstream.server_port}"
        config = proxy.ProxyConfig(
            upstream=proxy.parse_upstream(upstream_url),
            session_id="ses_default",
            timeout=5,
        )
        self.proxy = proxy.create_server(config, 0)
        self.proxy_thread = threading.Thread(
            target=self.proxy.serve_forever,
            daemon=True,
        )
        self.proxy_thread.start()
        self.base_url = f"http://127.0.0.1:{self.proxy.server_address[1]}"

    def tearDown(self) -> None:
        self.proxy.shutdown()
        self.upstream.shutdown()
        self.proxy.server_close()
        self.upstream.server_close()

    def test_forwards_body_auth_and_default_session(self) -> None:
        body = b'{"model":"test","message":"hello"}'
        request = Request(
            self.base_url + "/v1/chat/completions?trace=1",
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer oc_sk_test",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with urlopen(request, timeout=5) as response:
            self.assertEqual(json.loads(response.read()), {"ok": True})

        captured = FakeUpstreamHandler.requests[-1]
        self.assertEqual(
            captured["path"],
            "/inference/go/openai/v1/chat/completions?trace=1",
        )
        self.assertEqual(captured["body"], body)
        headers = captured["headers"]
        self.assertEqual(headers["authorization"], "Bearer oc_sk_test")
        self.assertEqual(headers["x-opencode-session"], "ses_default")
        self.assertEqual(headers["accept-encoding"], "identity")

    def test_streams_sse(self) -> None:
        request = Request(
            self.base_url + "/v1/messages",
            data=b'{"stream":true}',
            method="POST",
            headers={
                "Authorization": "Bearer oc_sk_test",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "x-opencode-session": "ses_client",
            },
        )
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.read(), b"data: first\n\ndata: second\n\n")

        captured = FakeUpstreamHandler.requests[-1]
        self.assertEqual(
            captured["path"],
            "/inference/go/anthropic/v1/messages",
        )
        self.assertEqual(
            captured["headers"]["x-opencode-session"],
            "ses_client",
        )

    def test_sse_first_event_is_forwarded_without_waiting_for_completion(self) -> None:
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.proxy.server_address[1],
            timeout=5,
        )
        connection.request(
            "POST",
            "/v1/messages",
            body=b'{"stream":true,"delay":true}',
            headers={
                "Authorization": "Bearer oc_sk_test",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )
        response = connection.getresponse()
        started = time.perf_counter()
        first_line = response.readline()
        first_line_ms = (time.perf_counter() - started) * 1000
        remaining = response.read()
        connection.close()

        self.assertEqual(first_line, b"data: first\n")
        self.assertLess(first_line_ms, 1000)
        self.assertEqual(remaining, b"\ndata: second\n\n")

    def test_writes_timing_log_without_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "proxy.log"
            self.proxy.config = proxy.ProxyConfig(
                upstream=self.proxy.config.upstream,
                session_id=self.proxy.config.session_id,
                timeout=self.proxy.config.timeout,
                log_path=log_path,
            )
            request = Request(
                self.base_url + "/v1/chat/completions",
                data=b'{"model":"test","stream":false}',
                method="POST",
                headers={
                    "Authorization": "Bearer oc_sk_must_not_appear",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            with urlopen(request, timeout=5) as response:
                response.read()

            deadline = time.perf_counter() + 2
            while not log_path.exists() and time.perf_counter() < deadline:
                time.sleep(0.01)
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("first-byte=", log_text)
            self.assertIn("upstream-read=", log_text)
            self.assertIn("client-write=", log_text)
            self.assertNotIn("oc_sk_must_not_appear", log_text)


if __name__ == "__main__":
    unittest.main()
