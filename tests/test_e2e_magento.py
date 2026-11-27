"""End-to-end Magento tests: run the real CLI against local GraphQL servers.

Each server port plays one store scenario over GET ``/graphql?query=...``.
Run with: uv run pytest -m e2e
"""

import csv
import http.server
import json
import re
import threading
import urllib.parse

import pytest

from tests.test_e2e_diagnosis import _free_port, _run, _run_verbose

pytestmark = pytest.mark.e2e


def _price(value, regular=None):
    money = {
        "regular_price": {"value": value if regular is None else regular, "currency": "EUR"},
        "final_price": {"value": value, "currency": "EUR"},
    }
    return {"minimum_price": money, "maximum_price": money}


def _simple(i):
    return {
        "__typename": "SimpleProduct",
        "uid": f"S{i}",
        "sku": f"SIMPLE-{i}",
        "name": f"Simple {i}",
        "url_key": f"simple-{i}",
        "url_suffix": ".html",
        "stock_status": "IN_STOCK",
        "price_range": _price(10 + i),
        "media_gallery": [],
    }


def _variant(uid, sku, size, price, stock):
    return {
        "attributes": [{"code": "size", "label": size, "value_index": 1}],
        "product": {
            "uid": uid,
            "sku": sku,
            "stock_status": stock,
            "image": {"url": f"https://cdn.test/{sku}.jpg"},
            "price_range": {"minimum_price": _price(price, 50)["minimum_price"]},
        },
    }


CONFIGURABLE = {
    "__typename": "ConfigurableProduct",
    "uid": "C1",
    "sku": "TEE",
    "name": "Tee",
    "url_key": "tee",
    "url_suffix": ".html",
    "stock_status": "IN_STOCK",
    "price_range": _price(40, 50),
    "media_gallery": [],
    "configurable_options": [{"attribute_code": "size", "label": "Size", "values": []}],
    "variants": [
        _variant("C1-S", "TEE-S", "S", 40, "IN_STOCK"),
        _variant("C1-L", "TEE-L", "L", 50, "OUT_OF_STOCK"),
    ],
}
CATALOG = [_simple(i) for i in range(6)] + [CONFIGURABLE]
MAX_PAGE_SIZE = 3


class _Handler(http.server.BaseHTTPRequestHandler):
    scenario = ""

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
        path, _, query = self.path.partition("?")
        s = self.scenario
        if path == "/robots.txt":
            if s == "robots_disallow":
                return self._send(200, b"User-agent: *\nDisallow: /graphql\n", "text/plain")
            if s == "robots_query_strings":
                return self._send(200, b"User-agent: *\nDisallow: /*?\n", "text/plain")
            return self._send(404)
        if path != "/graphql":
            return self._send(404)
        if s == "graphql_404":
            return self._send(404, b"<html>Not Found</html>", "text/html")
        if s == "graphql_503":
            return self._send(503, b"<html>Maintenance</html>", "text/html")
        if s == "errors_only":
            return self._json(200, {"errors": [{"message": "The GraphQL module is disabled"}]})
        text = urllib.parse.parse_qs(query).get("query", [""])[0]
        size = int(re.search(r"pageSize: (\d+)", text).group(1))
        page = int(re.search(r"currentPage: (\d+)", text).group(1))
        if s == "page_size_3" and size > MAX_PAGE_SIZE:
            return self._json(
                200, {"errors": [{"message": f"Maximum pageSize is {MAX_PAGE_SIZE}"}]}
            )
        items = CATALOG[(page - 1) * size : page * size]
        self._json(200, {"data": {"products": {"total_count": len(CATALOG), "items": items}}})


@pytest.fixture
def stores():
    """Start one local Magento server per scenario; call with a scenario for its URL."""
    servers = []

    def start(scenario):
        handler = type(f"M_{scenario}", (_Handler,), {"scenario": scenario})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()


EXPECTED = {
    "ok": "ok",
    "page_size_3": "ok",
    "graphql_404": "404 Not Found",
    "graphql_503": "unavailable (5xx)",
    "robots_disallow": "blocked by robots.txt",
    "robots_query_strings": "blocked by robots.txt",
    "errors_only": "failed",
}


def test_magento_diagnosis(tmp_path, stores):
    urls = {scenario: stores(scenario) for scenario in EXPECTED}
    statuses = _run(tmp_path, list(urls.values()), "--platform", "magento")
    assert {scenario: statuses[url] for scenario, url in urls.items()} == EXPECTED
    [output] = tmp_path.rglob("*.jsonl")
    items = [json.loads(line) for line in output.read_text().splitlines()]
    port = urls["page_size_3"].rsplit(":", 1)[1]
    capped = sorted(i["uid"] for i in items if i["store"].endswith(f":{port}"))
    assert capped == sorted(p["uid"] for p in CATALOG)


def test_magento_query_string_rule_hint(tmp_path, stores):
    url = stores("robots_query_strings")
    host = urllib.parse.urlsplit(url).netloc
    result, status = _run_verbose(tmp_path, [url], "--platform", "magento")
    assert status["urls"][0]["status"] == "blocked by robots.txt"
    assert f"{host}'s robots.txt disallows URLs with query strings" in result.stdout


def test_magento_variant_rows_in_csv(tmp_path, stores):
    url = stores("ok")
    statuses = _run(tmp_path, [url], "--platform", "magento", "--format", "csv")
    assert statuses[url] == "ok"
    [output] = tmp_path.rglob("*.csv")
    rows = list(csv.DictReader(output.open(newline="", encoding="utf-8")))
    assert len(rows) == 8
    assert [
        (r["variant_id"], r["variant_title"], r["sku"], r["price"], r["compare_at_price"])
        + (r["available"], r["html_url"])
        for r in rows
        if r["id"] == "C1"
    ] == [
        ("C1-S", "S", "TEE-S", "40", "50", "true", f"{url}/tee.html"),
        ("C1-L", "L", "TEE-L", "50", "", "false", f"{url}/tee.html"),
    ]
