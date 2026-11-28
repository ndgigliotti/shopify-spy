"""CLI contract test: run two installed shopify-spy CLIs against the same local fake
stores and diff everything a user or script can observe (exit codes, files written,
items in every format, status files, stdout/stderr, --help).

No network: the stores are local HTTP servers. Install each version in its own venv
(e.g. the latest release from PyPI or `git archive origin/master`, and a wheel of this
branch), then:

    python scripts/contract_test.py OLD/bin/shopify-spy NEW/bin/shopify-spy REPORT_DIR

REPORT_DIR/report.md lists each case as SAME or DIFF. The run fails (exit 1) when the
new CLI misses a case's expected outcome in EXPECT (exit code, and each store's status
`code` and item count in the status file), or on any difference between the two CLIs
that no `--allow-diff REGEX` (repeatable) approves. Each difference is one line,
"CASE: what differs" (status file differences are one line per field), and a pattern
approves the lines it matches with re.search. Approve only the deliberate changes the
CHANGELOG lists, for example:

    --allow-diff 'status: urls\\[\\d+\\]\\.(hint|identity_changes|products_from) added'

--help and --version differences are reported but never fail the run.
"""

import argparse
import ast
import base64
import csv
import difflib
import http.server
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

_parser = argparse.ArgumentParser(description="Diff two shopify-spy CLIs on local stores.")
_parser.add_argument("old", type=Path)
_parser.add_argument("new", type=Path)
_parser.add_argument("report", type=Path)
_parser.add_argument("--allow-diff", action="append", default=[], metavar="REGEX")
ARGS = _parser.parse_args()
OLD, NEW, REPORT = ARGS.old.absolute(), ARGS.new.absolute(), ARGS.report.absolute()
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGP8z8DAwMDAxMDAwMDAAAANHQEDasKb6QAAAABJRU5ErkJggg=="
)

# --- Fake stores -----------------------------------------------------------------------


def shopify_product(base, i):
    return {
        "id": 1000 + i,
        "title": f"Product {i} — “quoted”",
        "handle": f"p{i}",
        "body_html": f"<p>Line one, with a comma.\nLine {i}</p>",
        "vendor": "Acme",
        "product_type": "Chair",
        "tags": ["blue", "velvet"],
        "published_at": "2026-01-01T00:00:00-05:00",
        "created_at": "2026-01-01T00:00:00-05:00",
        "updated_at": "2026-02-01T00:00:00-05:00",
        "variants": [
            {"id": 5000 + i, "title": "Default", "price": "19.99", "sku": f"SKU{i}",
             "available": i % 2 == 0, "compare_at_price": None, "grams": 1200},
        ],
        "images": [{"id": 7000 + i, "src": f"{base}/cdn/p{i}.png?v=1"}] if i < 3 else [],
        "options": [{"name": "Title", "values": ["Default Title"]}],
    }  # fmt: skip


def woo_product(base, i):
    return {
        "id": 300 + i,
        "name": f"Mug {i}",
        "slug": f"mug-{i}",
        "permalink": f"{base}/product/mug-{i}/",
        "prices": {"price": "1250", "regular_price": "1500", "currency_code": "EUR",
                   "currency_minor_unit": 2, "price_range": None},
        "images": [{"id": 1, "src": f"{base}/cdn/m{i}.png"}],
        "categories": [{"id": 1, "name": "Mugs", "slug": "mugs"}],
        "is_in_stock": True,
    }  # fmt: skip


def urlset(*locs):
    body = "".join(f"<url><loc>{u}</loc></url>" for u in locs)
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'


def sitemapindex(*locs):
    body = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in locs)
    return f'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</sitemapindex>'


def route(kind, base, path, query):
    """(status, content_type, body) for a request to a store of this kind."""
    q = urllib.parse.parse_qs(query)
    page = int(q.get("page", ["1"])[0])
    if kind == "notfound":
        return 404, "text/html", "<h1>Not found</h1>"
    if path == "/robots.txt":
        rules = "Disallow: /" if kind == "blocked" else "Disallow: /admin"
        return 200, "text/plain", f"User-agent: *\n{rules}\n"
    if path.startswith("/cdn/"):
        return 200, "image/png", PNG
    if kind == "woo":
        if path == "/wp-json/wc/store/v1/products":
            items = [woo_product(base, i) for i in range(3)] if page == 1 else []
            return 200, "application/json", json.dumps(items)
        return 404, "text/html", "nope"
    if path == "/products.json":
        if kind == "fallback":
            return 404, "text/html", "nope"
        n = {1: 250, 2: 3}.get(page, 0)
        start = 0 if page == 1 else 250
        products = [shopify_product(base, start + i) for i in range(n)]
        return 200, "application/json", json.dumps({"products": products})
    if path == "/sitemap.xml":
        return (
            200,
            "application/xml",
            sitemapindex(f"{base}/sitemap_products_1.xml", f"{base}/sitemap_collections_1.xml"),
        )
    if path == "/sitemap_products_1.xml":
        return 200, "application/xml", urlset(*(f"{base}/products/p{i}" for i in range(3)))
    if path == "/sitemap_collections_1.xml":
        return 200, "application/xml", urlset(f"{base}/collections/c1", f"{base}/collections/c2")
    if path == "/collections.json":
        cols = [{"id": 900 + i, "title": f"Coll {i}", "handle": f"c{i}"} for i in (1, 2)]
        return 200, "application/json", json.dumps({"collections": cols if page == 1 else []})
    if m := re.fullmatch(r"/collections/([\w-]+)/products\.json", path):
        products = [shopify_product(base, i) for i in range(3)] if page == 1 else []
        return 200, "application/json", json.dumps({"products": products})
    if m := re.fullmatch(r"/products/p(\d+)\.json", path):
        return 200, "application/json", json.dumps({"product": shopify_product(base, int(m[1]))})
    if m := re.fullmatch(r"/collections/c(\d+)\.json", path):
        c = {"id": 900 + int(m[1]), "title": f"Coll {m[1]}", "handle": f"c{m[1]}",
             "body_html": "<p>c</p>", "image": {"src": f"{base}/cdn/c{m[1]}.png"}}  # fmt: skip
        return 200, "application/json", json.dumps({"collection": c})
    return 404, "text/html", "nope"


def serve(kind):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    base = f"http://127.0.0.1:{port}"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            status, ctype, body = route(kind, base, parsed.path, parsed.query)
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return base


STORES = {k: serve(k) for k in ("shop", "fallback", "woo", "blocked", "notfound")}

# --- Cases -----------------------------------------------------------------------------


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return str(path)


def cases(work):
    s = STORES
    url_file = write(work / "urls.txt", f"{s['shop']}\nnot a url\n{s['woo']}\n")
    url_file_comments = write(work / "urls_comments.txt", f"# comment\n{s['shop']}\n")
    config = write(
        work / "config.yaml",
        "scrape:\n  collections: true\n  bail: 20\noutput:\n  format: json\n"
        "network:\n  retries: 1\nthrottle:\n  enabled: false\n",
    )
    return {
        "shopify-jsonl": [s["shop"]],
        "shopify-json": [s["shop"], "-F", "json"],
        "shopify-csv": [s["shop"], "-F", "csv"],
        "shopify-xml": [s["shop"], "-F", "xml"],
        "collections": [s["shop"], "--collections"],
        "collections-only": [s["shop"], "--no-products", "--collections"],
        "limit": [s["shop"], "-n", "5"],
        "sitemap-fallback": [s["fallback"]],
        "woo": [s["woo"], "-p", "woocommerce"],
        "woo-csv": [s["woo"], "-p", "woocommerce", "-F", "csv"],
        "images": [s["shop"], "--images", "-n", "5"],
        "mixed-failures": [s["shop"], s["blocked"], s["notfound"], "-n", "3"],
        "all-failed": [s["notfound"]],
        "peek": [s["shop"], "--peek"],
        "url-file": ["-f", url_file, "-n", "2"],
        "url-file-comments": ["-f", url_file_comments, "-n", "2"],
        "config-file": [s["shop"], "-c", config, "-n", "4"],
        "ignore-robots": [s["blocked"], "-i", "-n", "2"],
        "quiet": [s["shop"], "-q", "-n", "2"],
        "collections-csv": [s["shop"], "--collections", "-F", "csv", "-n", "3"],
        "collections-xml": [s["shop"], "--no-products", "--collections", "-F", "xml"],
        "collections-json": [s["shop"], "--no-products", "--collections", "-F", "json"],
        "woo-json": [s["woo"], "-p", "woocommerce", "-F", "json"],
        "woo-xml": [s["woo"], "-p", "woocommerce", "-F", "xml"],
        "images-collections": [s["shop"], "--images", "--collections", "-n", "3"],
        "network-flags": [s["shop"], "--no-throttle", "--concurrent", "4", "-n", "10"],
        "bail-off-failure": [s["notfound"], "--bail", "0"],
        "verbose": [s["shop"], "-v", "-n", "2"],
    }


# What the new CLI must do in each case: its exit code, and for each store (a STORES key,
# or "invalid" for a line that isn't a URL) the status file's `code` and the least
# number of items. Cases without a status file (--peek) list no stores.
OK = ("ok", 1)
EXPECT = {
    "shopify-jsonl": (0, {"shop": ("ok", 253)}),
    "shopify-json": (0, {"shop": ("ok", 253)}),
    "shopify-csv": (0, {"shop": ("ok", 253)}),
    "shopify-xml": (0, {"shop": ("ok", 253)}),
    "collections": (0, {"shop": ("ok", 255)}),
    "collections-only": (0, {"shop": ("ok", 2)}),
    "limit": (0, {"shop": ("ok", 5)}),
    "sitemap-fallback": (0, {"fallback": ("ok", 3)}),
    "woo": (0, {"woo": ("ok", 3)}),
    "woo-csv": (0, {"woo": ("ok", 3)}),
    "images": (0, {"shop": ("ok", 5)}),
    "mixed-failures": (
        0,
        {"shop": ("ok", 3), "blocked": ("robots_blocked", 0), "notfound": ("not_found", 0)},
    ),
    "all-failed": (1, {"notfound": ("not_found", 0)}),
    "peek": (0, {}),
    # The URL file's WooCommerce store is read as Shopify, the default platform
    "url-file": (0, {"shop": ("ok", 2), "invalid": ("invalid_url", 0), "woo": ("not_found", 0)}),
    "url-file-comments": (0, {"shop": ("ok", 2)}),
    "config-file": (0, {"shop": ("ok", 4)}),
    "ignore-robots": (0, {"blocked": ("ok", 2)}),
    "quiet": (0, {"shop": ("ok", 2)}),
    "collections-csv": (0, {"shop": ("ok", 3)}),
    "collections-xml": (0, {"shop": ("ok", 2)}),
    "collections-json": (0, {"shop": ("ok", 2)}),
    "woo-json": (0, {"woo": ("ok", 3)}),
    "woo-xml": (0, {"woo": ("ok", 3)}),
    "images-collections": (0, {"shop": ("ok", 3)}),
    "network-flags": (0, {"shop": ("ok", 10)}),
    "bail-off-failure": (1, {"notfound": ("not_found", 0)}),
    "verbose": (0, {"shop": ("ok", 2)}),
}


def store_key(url):
    for key, base in STORES.items():
        if url.rstrip("/") == base:
            return key
    return "invalid"


def check_expected(name, result):
    """Notes for each way the result misses EXPECT[name]."""
    exit_code, stores = EXPECT[name]
    notes = []
    if result["exit"] != exit_code:
        notes.append(f"exit code {result['exit']}, expected {exit_code}")
    entries = {store_key(u.get("url", "")): u for u in result["status"].get("urls", [])}
    for key, (code, least) in stores.items():
        entry = entries.get(key)
        if entry is None:
            notes.append(f"{key}: no status entry")
            continue
        if entry.get("code") != code:
            notes.append(f"{key}: code {entry.get('code')!r}, expected {code!r}")
        if (entry.get("items") or 0) < least:
            notes.append(f"{key}: {entry.get('items')} items, expected at least {least}")
    return notes


# --- Normalization ---------------------------------------------------------------------

TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\+00-00")


def norm_text(text, out_dir):
    text = text.replace(str(out_dir), "<OUT>")
    for kind, base in STORES.items():
        text = text.replace(base, f"<{kind}>").replace(base.split("//")[1], f"<{kind}>")
    text = TS.sub("<TS>", text)
    text = re.sub(r"/home/\S+?/logs/\S+\.log", "<LOG>", text)
    text = re.sub(r"\d+(\.\d+)?s\b", "<N>s", text)
    # Progress: only the last count matters (0.2.x drew one line per item off a TTY)
    counts = re.findall(r"Scraped (\d+) items\.\.\.", text)
    text = re.sub(r"\r?  Scraped \d+ items\.\.\.\n?", "", text)
    if counts:
        text = f"[last progress count: {counts[-1]}]\n" + text
    return text


def canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def parse_cell(cell):
    for parse in (json.loads, ast.literal_eval):
        try:
            return parse(cell)
        except Exception:
            pass
    return cell


def xml_items(text):
    def conv(el):
        kids = list(el)
        if not kids:
            return el.text
        if all(k.tag == "value" for k in kids):
            return [conv(k) for k in kids]
        return {k.tag: conv(k) for k in kids}

    return [conv(item) for item in ET.fromstring(text)]


def read_data(path):
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.split("\n") if line], None
    if path.suffix == ".json":
        return json.loads(text), None
    if path.suffix == ".csv":
        csv.field_size_limit(10**9)
        rows = list(csv.reader(io.StringIO(text)))
        header = rows[0] if rows else []
        return [
            {h: parse_cell(c) for h, c in zip(header, r, strict=False)} for r in rows[1:]
        ], header
    if path.suffix == ".xml":
        return xml_items(text), None
    return None, None


def snapshot(out_dir):
    files, data, status = [], {}, {}
    for p in sorted(out_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = TS.sub("<TS>", str(p.relative_to(out_dir)))
        if "/full/" in rel:
            rel = re.sub(r"full/[0-9a-f]{40}", "full/<SHA1>", rel)
        files.append(rel)
        if rel.endswith("_status.json"):
            status = json.loads(p.read_text())
        elif p.suffix in (".jsonl", ".json", ".csv", ".xml"):
            data[rel] = read_data(p)
    return sorted(set(files)), data, status


def run(binary, args, out_dir, cwd):
    if out_dir.exists():
        shutil.rmtree(out_dir)
    cwd.mkdir(parents=True, exist_ok=True)
    cmd = [str(binary), "scrape", *args]
    if "--peek" not in args:
        cmd += ["--output", str(out_dir)]
    env = {**os.environ, "COLUMNS": "400"}  # no Rich line wrapping
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600, env=env)
    if "-v" in args:  # verbose logs differ by design (no Scrapy lines); keep exit/files/data
        proc = subprocess.CompletedProcess(proc.args, proc.returncode, "<verbose>", "<verbose>")
    files, data, status = snapshot(out_dir) if out_dir.exists() else ([], {}, {})
    return {
        "exit": proc.returncode,
        "stdout": norm_text(proc.stdout, out_dir),
        "stderr": norm_text(proc.stderr, out_dir),
        "files": files,
        "data": data,
        "status": status,
    }


# --- Comparison ------------------------------------------------------------------------


def item_key(item):
    # CSV rows since 0.4: one per variant, both platforms in one schema
    if "kind" in item:
        return f"{item.get('platform')}:{item['kind']}:{item.get('id')}:{item.get('variant_id')}"
    for k in ("product", "collection"):
        if isinstance(item.get(k), dict):
            return f"{k}:{item[k].get('id')}"
    return f"woo:{item.get('id')}"


def compare_items(old_items, new_items):
    notes = []
    o = {item_key(i): i for i in old_items}
    n = {item_key(i): i for i in new_items}
    if set(o) != set(n):
        only_old, only_new = sorted(set(o) - set(n))[:5], sorted(set(n) - set(o))[:5]
        notes.append(f"item sets differ: only old {only_old}, only new {only_new}")
    for k in sorted(set(o) & set(n)):
        a, b = o[k], n[k]
        if canon(a) != canon(b):
            keys = sorted(set(a) | set(b))
            diffs = [f for f in keys if canon(a.get(f)) != canon(b.get(f))]
            old_vals = canon({f: a.get(f) for f in diffs})[:300]
            new_vals = canon({f: b.get(f) for f in diffs})[:300]
            notes.append(f"{k} differs in fields {diffs}: old={old_vals} new={new_vals}")
            break  # one example is enough
    return notes


def strip_status(status):
    s = {k: v for k, v in status.items() if k not in ("duration_seconds", "log_file")}
    s["urls"] = list(status.get("urls", []))
    return s


def status_notes(old, new, path="status: "):
    """One note per status file field that was added, removed or changed."""
    if isinstance(old, dict) and isinstance(new, dict):
        notes = []
        for key in sorted(set(old) | set(new)):
            sub = f"{path}{'' if path.endswith(' ') else '.'}{key}"
            if key not in new:
                notes.append(f"{sub} removed (was {canon(old[key])})")
            elif key not in old:
                notes.append(f"{sub} added ({canon(new[key])})")
            else:
                notes += status_notes(old[key], new[key], sub)
        return notes
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        return [
            n for i, (o, w) in enumerate(zip(old, new)) for n in status_notes(o, w, f"{path}[{i}]")
        ]
    return [] if old == new else [f"{path}: old {canon(old)} new {canon(new)}"]


def compare(old, new):
    notes = []
    if old["exit"] != new["exit"]:
        notes.append(f"exit code: old {old['exit']} new {new['exit']}")
    if old["files"] != new["files"]:
        only_old = sorted(set(old["files"]) - set(new["files"]))
        only_new = sorted(set(new["files"]) - set(old["files"]))
        notes.append(f"files: only old {only_old}, only new {only_new}")
    for rel in sorted(set(old["data"]) & set(new["data"])):
        (oi, oh), (ni, nh) = old["data"][rel], new["data"][rel]
        if oh != nh:
            notes.append(f"{rel}: CSV header old {oh} new {nh}")
        notes += [f"{rel}: {n}" for n in compare_items(oi or [], ni or [])]
    notes += status_notes(strip_status(old["status"]), strip_status(new["status"]))
    extra = set(u.get("code") for u in new["status"].get("urls", []))
    for stream in ("stdout", "stderr"):
        if old[stream] != new[stream]:
            d = list(
                difflib.unified_diff(
                    old[stream].splitlines(),
                    new[stream].splitlines(),
                    "old",
                    "new",
                    lineterm="",
                    n=0,
                )
            )
            notes.append(f"{stream} differs:\n    " + "\n    ".join(d[2:40]))
    return notes, extra


def main():
    REPORT.mkdir(parents=True, exist_ok=True)
    work = REPORT / "work"
    lines = []
    failures = []
    approvals = [re.compile(p) for p in ARGS.allow_diff]
    used = set()
    for name, args in cases(work).items():
        res = {
            v: run(b, args, work / v / name / "out", work / v / name / "cwd")
            for v, b in (("old", OLD), ("new", NEW))
        }
        notes, codes = compare(res["old"], res["new"])
        missed = check_expected(name, res["new"])
        unapproved = []
        for note in notes:
            matched = [a.pattern for a in approvals if a.search(f"{name}: {note}")]
            used.update(matched)
            if not matched:
                unapproved.append(note)
        verdict = "SAME" if not notes else "DIFF" if unapproved else "DIFF (approved)"
        failures += [f"{name}: unapproved: {n.splitlines()[0][:200]}" for n in unapproved]
        if missed:
            verdict += ", UNEXPECTED"
            failures.append(f"{name}: {'; '.join(missed)}")
        exits = f"exit old {res['old']['exit']} / new {res['new']['exit']}"
        lines.append(
            f"## {name}: {verdict}  ({exits}; status codes {sorted(c for c in codes if c)})"
        )
        lines += [f"- expected: {m}" for m in missed]
        lines += [f"- {n}" for n in notes]
        (REPORT / f"{name}.json").write_text(json.dumps(res, indent=1, default=str)[:2_000_000])
        print(lines[-1 - len(notes) - len(missed)], flush=True)
    for flags in (["--help"], ["scrape", "--help"], ["--version"]):
        o = subprocess.run([str(OLD), *flags], capture_output=True, text=True).stdout
        n = subprocess.run([str(NEW), *flags], capture_output=True, text=True).stdout
        d = list(
            difflib.unified_diff(o.splitlines(), n.splitlines(), "old", "new", lineterm="", n=0)
        )
        lines.append(f"## {' '.join(flags)}: {'SAME' if not d else 'DIFF'}")
        lines += ["    " + x for x in d[2:80]]
    (REPORT / "report.md").write_text("\n".join(lines) + "\n")
    print(f"report: {REPORT / 'report.md'}")
    for pattern in ARGS.allow_diff:
        if pattern not in used:
            print(f"note: --allow-diff {pattern!r} matched nothing", file=sys.stderr)
    if failures:
        print("FAILED:\n  " + "\n  ".join(failures), file=sys.stderr)
        sys.exit(1)


main()
