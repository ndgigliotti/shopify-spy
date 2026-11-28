"""End-to-end BigCommerce tests: run the real CLI against local Storefront GraphQL servers.

Each server port plays one store scenario: a homepage carrying the Storefront API
token, and POST ``/graphql`` answering for that token.
Run with: uv run pytest -m e2e
"""

import base64
import csv
import http.server
import json
import threading

import pytest

from tests.flow_support import bc_token
from tests.test_e2e_diagnosis import _free_port, _run

pytestmark = pytest.mark.e2e

TOKEN = bc_token(sid=1)
NEXT_TOKEN = bc_token(sid=2)
DECOY = bc_token(iss="shopify")
TOO_COMPLEX = (
    "The query is too complex as it has a complexity score of 12000 out of 10000. "
    "Please remove some elements and try again"
)


def _money(value):
    return {"value": value, "currencyCode": "EUR"}


def _simple(i):
    return {
        "entityId": i,
        "sku": f"SIMPLE-{i}",
        "name": f"Simple {i}",
        "path": f"/simple-{i}/",
        "prices": {"price": _money(10 + i), "basePrice": _money(10 + i)},
        "inventory": {"isInStock": True},
        "images": {"edges": []},
        "variants": {"pageInfo": {"hasNextPage": False}, "edges": []},
    }


def _variant(entity_id, sku, size, price, in_stock):
    values = {"edges": [{"node": {"entityId": entity_id, "label": size}}]}
    return {
        "node": {
            "entityId": entity_id,
            "sku": sku,
            "isPurchasable": in_stock,
            "prices": {"price": _money(price), "basePrice": _money(50)},
            "inventory": {"isInStock": in_stock},
            "defaultImage": {"urlOriginal": f"https://cdn.test/{sku}.jpg"},
            "options": {
                "edges": [{"node": {"entityId": 1, "displayName": "Size", "values": values}}]
            },
        }
    }


TEE = {
    "entityId": 100,
    "sku": "TEE",
    "name": "Tee",
    "path": "/tee/",
    "prices": {
        "price": _money(40),
        "basePrice": _money(50),
        "priceRange": {"min": _money(40), "max": _money(50)},
    },
    "inventory": {"isInStock": True},
    "images": {"edges": []},
    "productOptions": {
        "edges": [{"node": {"entityId": 1, "displayName": "Size", "isVariantOption": True}}]
    },
    "variants": {
        "pageInfo": {"hasNextPage": False},
        "edges": [_variant(101, "TEE-S", "S", 40, True), _variant(102, "TEE-L", "L", 50, False)],
    },
}
CATALOG = [_simple(i) for i in range(6)] + [TEE]
SERVER_PAGE = {"two_pages": 5, "token_expires": 5}  # most products the server pages at once
MAX_COMPLEXITY_FIRST = 3


def _homepage(token):
    # Escaped, as Stencil's jsContext carries it, beside an unrelated JWT
    context = json.dumps(json.dumps({"graphQLToken": token}))
    return (
        "<html><head><meta name='platform' content='bigcommerce.stencil' /></head>"
        f'<body><a href="/buyer_flags?t={DECOY}">x</a>'
        f"<script>var jsContext = JSON.parse({context});</script></body></html>"
    ).encode()


def _cursor(offset):
    return base64.b64encode(f"offset:{offset}".encode()).decode()


def _offset(cursor):
    return 0 if cursor is None else int(base64.b64decode(cursor).decode().split(":")[1])


class _Handler(http.server.BaseHTTPRequestHandler):
    scenario = ""
    state: dict = {}

    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, data):
        self._send(status, json.dumps(data).encode())

    def do_GET(self):  # noqa: N802
        s = self.scenario
        if self.path == "/robots.txt":
            if s == "robots_disallow":
                return self._send(200, b"User-agent: *\nDisallow: /graphql\n", "text/plain")
            return self._send(404)
        if self.path != "/":
            return self._send(404)
        self.state["homepages"] += 1
        if s == "homepage_403":
            return self._send(403, b"<html>Forbidden</html>", "text/html")
        if s == "no_token":
            return self._send(200, _homepage("").replace(b"graphQLToken", b"x"), "text/html")
        self._send(200, _homepage(self.state["token"]), "text/html")

    def do_POST(self):  # noqa: N802
        s = self.scenario
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path != "/graphql":
            return self._send(404)
        if s == "graphql_404":
            return self._send(404, b"<html>Not Found</html>", "text/html")
        if s == "graphql_503":
            return self._send(503, b"<html>Maintenance</html>", "text/html")
        if self.headers.get("Authorization") != f"Bearer {self.state['token']}":
            self.state["unauthorized"] += 1
            return self._json(401, {"errors": [{"message": "GraphQL invalid credentials"}]})
        if s == "errors_only":
            return self._json(200, {"errors": [{"message": "Cannot query field 'site'"}]})
        first = body["variables"]["first"]
        if s == "complexity" and first > MAX_COMPLEXITY_FIRST:
            return self._json(200, {"errors": [{"message": TOO_COMPLEX}]})
        offset = _offset(body["variables"]["after"])
        end = offset + min(first, SERVER_PAGE.get(s, 50))
        edges = [{"node": p} for p in CATALOG[offset:end]]
        info = {"hasNextPage": end < len(CATALOG), "endCursor": _cursor(end)}
        if s == "token_expires":
            # The first token expires after page 1; set before answering, so page 2 sees it
            self.state["token"] = NEXT_TOKEN
        self._json(200, {"data": {"site": {"products": {"pageInfo": info, "edges": edges}}}})


@pytest.fixture
def stores():
    """Start one local BigCommerce server per scenario; call with a scenario for its
    URL. ``stores.state[scenario]`` holds the server's counters."""
    servers = []

    def start(scenario):
        state = {"token": TOKEN, "homepages": 0, "unauthorized": 0}
        handler = type(f"B_{scenario}", (_Handler,), {"scenario": scenario, "state": state})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        start.state[scenario] = state
        return f"http://127.0.0.1:{server.server_address[1]}"

    start.state = {}
    yield start
    for server in servers:
        server.shutdown()


EXPECTED = {
    "ok": "ok",
    "two_pages": "ok",
    "no_token": "failed",
    "homepage_403": "403 Forbidden",
    "robots_disallow": "blocked by robots.txt",
    "graphql_503": "unavailable (5xx)",
    "graphql_404": "404 Not Found",
    "errors_only": "failed",
    "token_expires": "ok",
    "complexity": "ok",
}


def test_bigcommerce_diagnosis(tmp_path, stores):
    urls = {scenario: stores(scenario) for scenario in EXPECTED}
    # Several requests to one host before the first item (the complexity store halves
    # its page five times): at the default 1 s minimum they outlast the e2e --bail
    statuses = _run(tmp_path, list(urls.values()), "--platform", "bigcommerce", "--min-delay", "0")
    assert {scenario: statuses[url] for scenario, url in urls.items()} == EXPECTED
    [output] = tmp_path.rglob("*.jsonl")
    items = [json.loads(line) for line in output.read_text().splitlines()]
    for scenario in ("two_pages", "token_expires", "complexity"):
        port = urls[scenario].rsplit(":", 1)[1]
        got = sorted(i["entityId"] for i in items if i["store"].endswith(f":{port}"))
        assert got == sorted(p["entityId"] for p in CATALOG), scenario
    expired = stores.state["token_expires"]
    assert (expired["unauthorized"], expired["homepages"]) == (1, 2)


def test_bigcommerce_variant_rows_in_csv(tmp_path, stores):
    url = stores("ok")
    # Several requests to one host before the first item: at the default 1 s minimum
    # they come close to the short e2e --bail on a slow runner
    statuses = _run(
        tmp_path, [url], "--platform", "bigcommerce", "--format", "csv", "--min-delay", "0"
    )
    assert statuses[url] == "ok"
    [output] = tmp_path.rglob("*.csv")
    rows = list(csv.DictReader(output.open(newline="", encoding="utf-8")))
    assert len(rows) == 8
    assert [
        (r["variant_id"], r["variant_title"], r["sku"], r["price"], r["compare_at_price"])
        + (r["available"], r["html_url"])
        for r in rows
        if r["id"] == "100"
    ] == [
        ("101", "S", "TEE-S", "40", "50", "true", f"{url}/tee/"),
        ("102", "L", "TEE-L", "50", "", "false", f"{url}/tee/"),
    ]
