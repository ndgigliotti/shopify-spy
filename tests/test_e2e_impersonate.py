"""End-to-end: the default curl_cffi transport against local stores (#95).

It must leave the diagnosis as it is with httpx (--user-agent), and no hint
mentions impersonation. Run with: uv run pytest -m e2e
"""

import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import urllib.parse

import pytest

pytestmark = pytest.mark.e2e

PRODUCTS = [{"id": 1, "handle": "p1", "title": "P1"}, {"id": 2, "handle": "p2", "title": "P2"}]


class _Store(http.server.BaseHTTPRequestHandler):
    scenario = ""
    agents: list

    def log_message(self, *args):
        pass

    def _send(self, status, body=b""):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self.agents.append(self.headers.get("User-Agent", ""))
        path = self.path.split("?")[0]
        if self.scenario == "robots_disallow":
            if path == "/robots.txt":
                return self._send(200, b"User-agent: *\nDisallow: /\n")
            return self._send(200)
        if self.scenario == "store_403":
            return self._send(403, b"<html>Forbidden</html>")
        if self.scenario == "refuse_chrome" and "Chrome/" in self.agents[-1]:
            return self._send(403, b"<html>Forbidden</html>")
        if path == "/products.json":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            page = PRODUCTS if query.get("page") == ["1"] else []
            return self._send(200, json.dumps({"products": page}).encode())
        return self._send(404)


@pytest.fixture
def store():
    servers = []

    def start(scenario):
        handler = type(f"S_{scenario}", (_Store,), {"scenario": scenario, "agents": []})
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{port}", handler.agents

    yield start
    for server in servers:
        server.shutdown()


def _scrape(out_dir, url, *args):
    """Run spy scrape on one store; return (stdout + stderr, status entry, items)."""
    # A tmp dir from mktemp, not tmp_path, whose name would carry the test's name and
    # so the word the hint check looks for
    result = subprocess.run(
        [sys.executable, "-m", "shopify_spy.cli", "scrape", url, "-o", str(out_dir)]
        + ["--bail", "4", "--no-throttle", *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=out_dir,
        env={**os.environ, "XDG_STATE_HOME": str(out_dir / "state")},
    )
    (status_file,) = out_dir.glob("status/*_status.json")
    status = json.loads(status_file.read_text())
    items = [
        json.loads(line)
        for path in sorted(out_dir.glob("data/*.jsonl"))
        for line in path.read_text().splitlines()
    ]
    return result.stdout + result.stderr, status["urls"][0], items


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [("shop", "ok"), ("robots_disallow", "blocked by robots.txt"), ("store_403", "403 Forbidden")],
)
def test_same_diagnosis_on_both_transports(store, tmp_path_factory, scenario, expected):
    url, agents = store(scenario)
    plain = _scrape(tmp_path_factory.mktemp("plain"), url, "--user-agent", "test-agent/1.0")
    agents.clear()
    output, entry, items = _scrape(tmp_path_factory.mktemp("browser"), url)

    assert entry["status"] == plain[1]["status"] == expected
    assert items == plain[2]
    assert len(items) == (len(PRODUCTS) if scenario == "shop" else 0)
    assert agents and "Chrome/" in agents[0]
    assert all(any(t in a for t in ("Chrome/", "Version/", "Firefox/")) for a in agents)
    # Neither the summary hints nor the status file point at the option
    assert "impersonate" not in output.lower()
    assert "impersonate" not in json.dumps(entry).lower()


def test_store_that_refuses_one_profile_is_served_by_the_next(store, tmp_path_factory):
    url, agents = store("refuse_chrome")
    _, entry, items = _scrape(tmp_path_factory.mktemp("browser"), url)
    assert (entry["status"], entry["identity_changes"]) == ("ok", 1)
    assert len(items) == len(PRODUCTS)
    assert "Chrome/" in agents[0] and "Version/" in agents[1]


def test_store_that_refuses_every_profile_switches_once(store, tmp_path_factory):
    url, agents = store("store_403")
    _, entry, _ = _scrape(tmp_path_factory.mktemp("browser"), url)
    assert (entry["status"], entry["identity_changes"]) == ("403 Forbidden", 1)
    # Every later request keeps the second identity, robots.txt's included
    assert len(set(agents)) == 2
    assert all(agent == agents[1] for agent in agents[1:])


def test_user_agent_is_never_switched(store, tmp_path_factory):
    url, agents = store("store_403")
    _, entry, _ = _scrape(tmp_path_factory.mktemp("plain"), url, "--user-agent", "test-agent/1.0")
    assert (entry["status"], entry["identity_changes"]) == ("403 Forbidden", 0)
    assert set(agents) == {"test-agent/1.0"}
