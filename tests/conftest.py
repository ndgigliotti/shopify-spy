import http.server
import re
import threading
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

# Pattern to strip ANSI escape codes from Rich output
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """Strip ANSI escape codes from text."""
    return ANSI_ESCAPE.sub("", text)


runner = CliRunner()


PROXY_ENV_VARS = [
    name
    for base in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
    for name in (base, base.lower())
]


@pytest.fixture
def clean_proxy_env(monkeypatch):
    """Remove proxy variables, so a test sees only the ones it sets."""
    for name in PROXY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


class _RecordingProxyHandler(http.server.BaseHTTPRequestHandler):
    reject_connect = False

    def log_message(self, *args):
        pass

    def _record(self):
        self.server.records.append(  # type: ignore[attr-defined]
            (self.command, self.path, self.headers.get("Proxy-Authorization"))
        )

    def do_GET(self):  # noqa: N802
        self._record()
        body = b'{"proxied": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_CONNECT(self):  # noqa: N802
        self._record()
        if self.reject_connect:
            self.send_response(407)
            self.send_header("Proxy-Authenticate", 'Basic realm="proxy"')
        else:
            self.send_response(502)
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.close_connection = True


@pytest.fixture
def recording_proxy():
    """Factory for local proxies that record ``(command, path, Proxy-Authorization)``.

    Absolute-form GETs get a 200 JSON body; CONNECT gets a 407 with
    ``reject_connect=True`` (a 502 otherwise), so nothing is ever tunnelled.
    """
    servers = []

    def start(reject_connect: bool = False):
        handler = type("Handler", (_RecordingProxyHandler,), {"reject_connect": reject_connect})
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        httpd.daemon_threads = True
        httpd.records = []  # type: ignore[attr-defined]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return SimpleNamespace(
            url=f"http://127.0.0.1:{httpd.server_address[1]}",
            port=httpd.server_address[1],
            records=httpd.records,  # type: ignore[attr-defined]
        )

    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()
