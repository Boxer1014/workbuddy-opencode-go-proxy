#!/usr/bin/env python3
"""Local forwarding proxy for the OpenCode Go inference gateway."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_SESSION_ID = "ses_workbuddy"
DEFAULT_TIMEOUT = 900.0
DEFAULT_UPSTREAM = "https://opencode.ai"
DEFAULT_LOG_FILE = "opencode-go-proxy.log"
CHUNK_SIZE = 8192

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


@dataclass(frozen=True, slots=True)
class Upstream:
    scheme: str
    host: str
    port: int
    base_path: str = ""


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    upstream: Upstream
    session_id: str = DEFAULT_SESSION_ID
    timeout: float = DEFAULT_TIMEOUT
    log_path: Path | None = None


class RequestBodyError(ValueError):
    """Raised when an incoming request body cannot be read safely."""


def parse_upstream(value: str) -> Upstream:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("upstream URL must start with http:// or https://")
    if not parsed.hostname:
        raise ValueError("upstream URL must include a hostname")
    if parsed.query or parsed.fragment:
        raise ValueError("upstream URL cannot contain a query string or fragment")

    default_port = 443 if parsed.scheme == "https" else 80
    return Upstream(
        scheme=parsed.scheme,
        host=parsed.hostname,
        port=parsed.port or default_port,
        base_path=parsed.path.rstrip("/"),
    )


def map_upstream_path(path: str) -> str:
    """Convert the client-facing path to an OpenCode Go gateway path."""
    if path in {"/chat/completions", "/responses", "/models"}:
        path = "/v1" + path
    elif path == "/messages":
        path = "/v1/messages"

    # Some clients combine a base URL ending in /v1 with another /v1.
    if path.startswith("/v1/v1/"):
        path = path[3:]

    if path == "/v1/messages" or path.startswith("/v1/messages/"):
        return "/inference/go/anthropic" + path
    if path.startswith("/v1/"):
        return "/inference/go/openai" + path
    return path


def build_upstream_target(upstream: Upstream, raw_path: str) -> str:
    parsed = urlsplit(raw_path)
    target = upstream.base_path + map_upstream_path(parsed.path or "/")
    if parsed.query:
        target += "?" + parsed.query
    return target


def _connection_tokens(headers: list[tuple[str, str]]) -> set[str]:
    tokens: set[str] = set()
    for name, value in headers:
        if name.lower() == "connection":
            tokens.update(
                token.strip().lower() for token in value.split(",") if token.strip()
            )
    return tokens


def filter_headers(
    headers: list[tuple[str, str]],
    *,
    extra_skip: set[str] | None = None,
) -> list[tuple[str, str]]:
    skip = set(HOP_BY_HOP_HEADERS)
    skip.update(_connection_tokens(headers))
    skip.update(name.lower() for name in (extra_skip or set()))
    return [(name, value) for name, value in headers if name.lower() not in skip]


def port_in_use(port: int, host: str = DEFAULT_HOST) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) == 0


def request_metadata(body: bytes | None, accept: str, content_type: str) -> str:
    stream = None
    payload: dict[str, object] | None = None
    if body and "json" in content_type.lower():
        try:
            parsed = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed
            if isinstance(payload.get("stream"), bool):
                stream = payload["stream"]

    details = [
        f"body-bytes={len(body) if body is not None else 0}, "
        f"stream={stream if stream is not None else 'unknown'}, "
        f"accept={accept or '-'}, content-type={content_type or '-'}"
    ]
    if payload is not None:
        model = payload.get("model")
        if isinstance(model, str) and model:
            details.append(f"model={model[:100]}")

        messages = payload.get("messages")
        if isinstance(messages, list):
            details.append(f"messages={len(messages)}")

        input_value = payload.get("input")
        if isinstance(input_value, list):
            details.append(f"input-items={len(input_value)}")
        elif isinstance(input_value, str):
            details.append(f"input-chars={len(input_value)}")

        tools = payload.get("tools")
        if isinstance(tools, list):
            details.append(f"tools={len(tools)}")

        effort = payload.get("reasoning_effort")
        reasoning = payload.get("reasoning")
        if effort is None and isinstance(reasoning, dict):
            effort = reasoning.get("effort")
        if isinstance(effort, str) and effort:
            details.append(f"reasoning={effort[:30]}")

    return ", ".join(details)


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    disable_nagle_algorithm = True

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler: type[BaseHTTPRequestHandler],
        config: ProxyConfig,
    ) -> None:
        self.config = config
        super().__init__(server_address, request_handler)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OpenCodeGoProxy/1.0"

    @property
    def config(self) -> ProxyConfig:
        return self.server.config  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        self._handle_proxy_request()

    def do_POST(self) -> None:
        self._handle_proxy_request()

    def do_PUT(self) -> None:
        self._handle_proxy_request()

    def do_PATCH(self) -> None:
        self._handle_proxy_request()

    def do_DELETE(self) -> None:
        self._handle_proxy_request()

    def do_HEAD(self) -> None:
        self._handle_proxy_request()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, x-opencode-session",
        )
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def _handle_proxy_request(self) -> None:
        if self.client_address[0] != DEFAULT_HOST:
            self._send_json(403, "access_denied", "This proxy only accepts local requests.")
            return

        parsed_path = urlsplit(self.path)
        if self.command == "GET" and parsed_path.path == "/health":
            self._send_json(
                200,
                "ok",
                "Proxy is running.",
                upstream=f"{self.config.upstream.scheme}://{self.config.upstream.host}",
            )
            return

        started = time.perf_counter()
        try:
            body = self._read_request_body()
        except RequestBodyError as exc:
            self._send_json(400, "invalid_request_body", str(exc))
            return
        body_read_at = time.perf_counter()

        target = build_upstream_target(self.config.upstream, self.path)
        headers = self._build_upstream_headers()
        connection: http.client.HTTPConnection | None = None
        self._response_started = False
        self._connected_at: float | None = None
        self._request_sent_at: float | None = None
        self._response_header_at: float | None = None
        self._first_response_byte_at: float | None = None
        upstream_read_seconds = 0.0
        client_write_seconds = 0.0
        response_bytes = 0
        response_chunks = 0
        response_content_type = "-"
        status = 0

        try:
            connection_class = (
                http.client.HTTPSConnection
                if self.config.upstream.scheme == "https"
                else http.client.HTTPConnection
            )
            connection = connection_class(
                self.config.upstream.host,
                self.config.upstream.port,
                timeout=self.config.timeout,
            )
            connection.connect()
            self._connected_at = time.perf_counter()
            if connection.sock is not None:
                connection.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            _send_upstream_request(connection, self.command, target, body, headers)
            self._request_sent_at = time.perf_counter()
            response = connection.getresponse()
            self._response_header_at = time.perf_counter()
            status = response.status
            response_content_type = response.getheader("Content-Type", "-") or "-"
            response_chunks, response_bytes, upstream_read_seconds, client_write_seconds = (
                self._forward_response(response)
            )
        except (BrokenPipeError, ConnectionResetError):
            client_write_seconds = time.perf_counter() - started
            return
        except Exception as exc:
            self.log_error("upstream request failed: %s", exc)
            if not self._response_started:
                self._send_json(
                    502,
                    "upstream_error",
                    f"Could not complete the upstream request: {exc}",
                )
        finally:
            if connection is not None:
                connection.close()
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            headers_ms = (
                int((self._response_header_at - started) * 1000)
                if self._response_header_at is not None
                else None
            )
            first_byte_ms = (
                int((self._first_response_byte_at - started) * 1000)
                if self._first_response_byte_at is not None
                else None
            )
            connect_ms = _elapsed_ms(started, self._connected_at)
            request_sent_ms = _elapsed_ms(started, self._request_sent_at)
            body_read_ms = int((body_read_at - started) * 1000)
            metadata = request_metadata(
                body,
                self.headers.get("Accept", ""),
                self.headers.get("Content-Type", ""),
            )
            self.log_message(
                "%s %s -> %s %s (%s, body=%d ms, connect=%s ms, "
                "request-sent=%s ms, headers=%s ms, first-byte=%s ms, "
                "total=%d ms, chunks=%d, bytes=%d, upstream-read=%d ms, "
                "client-write=%d ms, response-type=%s)",
                self.command,
                self.path,
                target,
                status or "-",
                metadata,
                body_read_ms,
                connect_ms if connect_ms is not None else "-",
                request_sent_ms if request_sent_ms is not None else "-",
                headers_ms if headers_ms is not None else "-",
                first_byte_ms if first_byte_ms is not None else "-",
                elapsed_ms,
                response_chunks,
                response_bytes,
                int(upstream_read_seconds * 1000),
                int(client_write_seconds * 1000),
                response_content_type,
            )

    def _build_upstream_headers(self) -> list[tuple[str, str]]:
        raw_headers = list(self.headers.items())
        session_id = self.headers.get("x-opencode-session") or self.config.session_id
        headers = filter_headers(
            raw_headers,
            extra_skip={
                "accept-encoding",
                "content-length",
                "expect",
                "host",
                "x-opencode-session",
            },
        )
        headers.append(("x-opencode-session", session_id))
        headers.append(("Accept-Encoding", "identity"))
        headers.append(("Connection", "close"))
        return headers

    def _read_request_body(self) -> bytes | None:
        transfer_encoding = ",".join(self.headers.get_all("Transfer-Encoding", []))
        if "chunked" in transfer_encoding.lower():
            return self._read_chunked_body()

        content_length = self.headers.get("Content-Length")
        if content_length is None:
            return None
        try:
            length = int(content_length)
        except ValueError as exc:
            raise RequestBodyError("Content-Length must be an integer.") from exc
        if length < 0:
            raise RequestBodyError("Content-Length cannot be negative.")

        body = self.rfile.read(length)
        if len(body) != length:
            raise RequestBodyError("The request body ended before Content-Length.")
        return body

    def _read_chunked_body(self) -> bytes:
        body = bytearray()
        while True:
            line = self.rfile.readline(65537)
            if not line or len(line) > 65536:
                raise RequestBodyError("Invalid chunked request body.")
            size_text = line.split(b";", 1)[0].strip()
            try:
                size = int(size_text, 16)
            except ValueError as exc:
                raise RequestBodyError("Invalid chunk size.") from exc
            if size == 0:
                while True:
                    trailer = self.rfile.readline(65537)
                    if trailer in {b"\r\n", b"\n", b""}:
                        return bytes(body)
            chunk = self.rfile.read(size)
            if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                raise RequestBodyError("Truncated chunked request body.")
            body.extend(chunk)

    def _forward_response(
        self,
        response: http.client.HTTPResponse,
    ) -> tuple[int, int, float, float]:
        self._response_started = True
        self.send_response_only(response.status, response.reason)

        response_headers = filter_headers(
            list(response.getheaders()),
            extra_skip={"content-length"},
        )
        for name, value in response_headers:
            self.send_header(name, value)

        if self.command == "HEAD" or response.status in {204, 304}:
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return 0, 0, 0.0, 0.0

        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        if "text/event-stream" in response.getheader("Content-Type", "").lower():
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        chunks = 0
        total_bytes = 0
        upstream_read_seconds = 0.0
        client_write_seconds = 0.0
        while True:
            read_started = time.perf_counter()
            chunk = response.read1(CHUNK_SIZE)
            upstream_read_seconds += time.perf_counter() - read_started
            if not chunk:
                break
            if self._first_response_byte_at is None:
                self._first_response_byte_at = time.perf_counter()
            chunks += 1
            total_bytes += len(chunk)
            write_started = time.perf_counter()
            self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
            self.wfile.write(chunk)
            self.wfile.write(b"\r\n")
            self.wfile.flush()
            client_write_seconds += time.perf_counter() - write_started
        write_started = time.perf_counter()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()
        client_write_seconds += time.perf_counter() - write_started
        return chunks, total_bytes, upstream_read_seconds, client_write_seconds

    def _send_json(
        self,
        status: int,
        code: str,
        message: str,
        **extra: object,
    ) -> None:
        payload = {"error": code, "message": message, **extra}
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def log_message(self, format_string: str, *args: object) -> None:
        message = format_string % args
        line = f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}"
        print(line, file=sys.stderr, flush=True)
        log_path = self.config.log_path
        if log_path is None:
            return
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(line + "\n")
        except OSError as exc:
            print(f"warning: could not write log file: {exc}", file=sys.stderr)


def create_server(config: ProxyConfig, port: int) -> ProxyServer:
    return ProxyServer((DEFAULT_HOST, port), ProxyHandler, config)


def _send_upstream_request(
    connection: http.client.HTTPConnection,
    method: str,
    target: str,
    body: bytes | None,
    headers: list[tuple[str, str]],
) -> None:
    # Sending headers manually preserves duplicate values on every supported
    # Python version; HTTPConnection.request() only accepts mappings here.
    connection.putrequest(method, target, skip_accept_encoding=True)
    if body is not None:
        connection.putheader("Content-Length", str(len(body)))
    for name, value in headers:
        connection.putheader(name, value)
    connection.endheaders(body)


def _elapsed_ms(started: float, finished: float | None) -> int | None:
    if finished is None:
        return None
    return int((finished - started) * 1000)


def _positive_int(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return number


def _positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("timeout must be greater than zero")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Forward local API requests to OpenCode Go with a session header."
    )
    parser.add_argument(
        "port",
        nargs="?",
        default=os.getenv("OPENCODE_GO_PROXY_PORT", str(DEFAULT_PORT)),
        type=_positive_int,
        help=f"local port (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "session_id",
        nargs="?",
        default=os.getenv("OPENCODE_GO_SESSION_ID", DEFAULT_SESSION_ID),
        help=f"fallback x-opencode-session value (default: {DEFAULT_SESSION_ID})",
    )
    parser.add_argument(
        "--upstream",
        default=os.getenv("OPENCODE_GO_PROXY_UPSTREAM", DEFAULT_UPSTREAM),
        help=f"upstream base URL (default: {DEFAULT_UPSTREAM})",
    )
    parser.add_argument(
        "--timeout",
        default=os.getenv("OPENCODE_GO_PROXY_TIMEOUT", str(DEFAULT_TIMEOUT)),
        type=_positive_float,
        help=f"upstream socket timeout in seconds (default: {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--log-file",
        default=os.getenv("OPENCODE_GO_PROXY_LOG_FILE", DEFAULT_LOG_FILE),
        help=f"append request timing logs to this file (default: {DEFAULT_LOG_FILE})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    session_id = args.session_id.strip()
    if not session_id:
        print("error: session ID cannot be empty", file=sys.stderr)
        return 2

    try:
        upstream = parse_upstream(args.upstream)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if port_in_use(args.port):
        print(
            f"error: 127.0.0.1:{args.port} is already in use. "
            "Stop the existing proxy or choose another port.",
            file=sys.stderr,
        )
        return 2

    config = ProxyConfig(
        upstream=upstream,
        session_id=session_id,
        timeout=args.timeout,
        log_path=Path(args.log_file).expanduser().resolve(),
    )
    try:
        server = create_server(config, args.port)
    except OSError as exc:
        print(f"error: could not start proxy: {exc}", file=sys.stderr)
        return 2

    print("OpenCode Go proxy is running.")
    print(f"OpenAI Base URL:    http://{DEFAULT_HOST}:{args.port}/v1")
    print(f"Anthropic Base URL: http://{DEFAULT_HOST}:{args.port}")
    print(f"Session:            {session_id}")
    print(f"Upstream:           {args.upstream.rstrip('/')}")
    print(f"Log file:           {config.log_path}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping proxy.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
