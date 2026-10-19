from __future__ import annotations

import contextlib
import gzip
import shutil
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent_web.config import Config

FIXTURES = Path(__file__).parent / "fixtures"

requires_pandoc = pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc not installed")


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reads the user's config, cache or state, or the plugin's data dir."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for name in (
        "AGENT_WEB_CONFIG",
        "AGENT_WEB_CACHE_DIR",
        "AGENT_WEB_ALLOW_PRIVATE_NETWORKS",
        "AGENT_WEB_PANDOC",
        "CLAUDE_PLUGIN_DATA",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Defaults, but with a temporary cache and local addresses allowed (for the test server)."""
    return Config(cache_dir=tmp_path / "cache" / "agent-web", allow_private_networks=True)


def make_pdf(pages: list[str]) -> bytes:
    import pymupdf

    document = pymupdf.open()
    for text in pages:
        page = document.new_page()
        if text:
            page.insert_text((72, 72), text, fontsize=12)  # pyright: ignore[reportUnknownMemberType]
    data = document.tobytes()  # pyright: ignore[reportUnknownMemberType]
    document.close()
    return data


@dataclass
class Route:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict[str, str])
    delay: float = 0.0
    chunked: bool = False


@dataclass
class Server:
    port: int
    routes: dict[str, Route | Callable[[BaseHTTPRequestHandler], Route]]
    seen: dict[str, list[dict[str, str]]]

    def url(self, path: str, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}{path}"


HTML_PAGE = b"""<!doctype html><html><head><title>Test page</title></head>
<body><h1>Hello</h1><p>Some <b>content</b> here.</p></body></html>"""


def _routes(port: int) -> dict[str, Route | Callable[[BaseHTTPRequestHandler], Route]]:
    def echo_cookies(handler: BaseHTTPRequestHandler) -> Route:
        cookie = handler.headers.get("Cookie") or "none"
        return Route(body=f"cookies: {cookie}".encode(), headers={"Content-Type": "text/plain"})

    html = {"Content-Type": "text/html; charset=utf-8"}
    return {
        "/html": Route(body=HTML_PAGE, headers=html),
        "/redirect-same": Route(status=302, headers={"Location": "/html"}),
        "/redirect-cross": Route(
            status=302, headers={"Location": f"http://localhost:{port}/echo-cookies"}
        ),
        "/redirect-ftp": Route(status=301, headers={"Location": "ftp://example.com/file"}),
        "/echo-cookies": echo_cookies,
        "/loop": Route(status=302, headers={"Location": "/loop"}),
        "/notfound": Route(status=404, body=b"<html><body>Not here</body></html>", headers=html),
        "/captcha": Route(
            body=b"<html><head><title>Prove your humanity</title></head><body>Hi</body></html>",
            headers=html,
        ),
        "/big": Route(body=b"x" * (2 * 1024 * 1024), headers={"Content-Type": "text/plain"}),
        "/big-chunked": Route(
            body=b"y" * (2 * 1024 * 1024), headers={"Content-Type": "text/plain"}, chunked=True
        ),
        "/slow": Route(body=b"late", headers={"Content-Type": "text/plain"}, delay=3),
        "/gzip": Route(
            body=gzip.compress(HTML_PAGE),
            headers={**html, "Content-Encoding": "gzip"},
        ),
        "/latin1": Route(
            body='<html><head><meta charset="iso-8859-1"><title>Caf\u00e9</title></head>'
            "<body><p>Caf\u00e9 cr\u00e8me</p></body></html>".encode("latin-1"),
            headers={"Content-Type": "text/html"},
        ),
        "/no-type-html": Route(body=HTML_PAGE),
        "/text": Route(body=b"plain words", headers={"Content-Type": "text/plain; charset=utf-8"}),
        "/json": Route(body=b'{"a": 1}', headers={"Content-Type": "application/json"}),
        "/binary": Route(
            body=b"\x89PNG\r\n\x1a\n\x00\x00binary", headers={"Content-Type": "image/png"}
        ),
        "/pdf": Route(
            body=make_pdf(["First page text", "Second page text"]),
            headers={"Content-Type": "application/pdf"},
        ),
    }


@pytest.fixture
def server() -> Generator[Server]:
    seen: dict[str, list[dict[str, str]]] = {}
    state: dict[str, Server] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            srv = state["server"]
            path = self.path.split("?", 1)[0]
            seen.setdefault(path, []).append(dict(self.headers.items()))
            entry = srv.routes.get(path)
            route = entry(self) if callable(entry) else entry
            if route is None:
                route = Route(
                    status=404, body=b"no such route", headers={"Content-Type": "text/plain"}
                )
            if route.delay:
                time.sleep(route.delay)
            self.send_response(route.status)
            for key, value in route.headers.items():
                self.send_header(key, value)
            if route.chunked:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                step = 64 * 1024
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    for i in range(0, len(route.body), step):
                        chunk = route.body[i : i + step]
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                return
            self.send_header("Content-Length", str(len(route.body)))
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(route.body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    state["server"] = Server(port=port, routes=_routes(port), seen=seen)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield state["server"]
    httpd.shutdown()
    httpd.server_close()
