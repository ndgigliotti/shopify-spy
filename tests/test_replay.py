"""Replay recorded store traffic through the engine and compare with snapshots.

The corpora in tests/fixtures/corpus were recorded from real stores with the old
Scrapy engine, and the new engine's output on them was verified identical (plan
§7.2). These snapshots pin that verified output. To regenerate after an
intentional change:

    uv run python tests/test_replay.py --update
"""

import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

import shopify_spy as spy
from shopify_spy._fetch import ReplayFetcher, load_corpus
from shopify_spy._flatten import flatten
from shopify_spy._utils import canonicalize_url
from shopify_spy._woocommerce import API_PATH, BY_ID

CORPUS = Path(__file__).parent / "fixtures" / "corpus"
SNAPSHOTS = CORPUS / "snapshots.json"
MULTI = [
    "https://www.snowdevil.ca",
    "https://www.beardbrand.com",
    "https://shop.nonexistent-store.invalid",
]

CASES: dict[str, tuple[str, list[str], dict[str, Any]]] = {
    "multi_store": ("multi_store.json.gz", MULTI, {}),
    "multi_store_limit": ("multi_store.json.gz", MULTI, {"max_items_per_store": 5}),
    "collections": ("collections.json.gz", ["https://www.snowdevil.ca"], {"collections": True}),
    "images": (
        "images.json.gz",
        ["https://www.snowdevil.ca"],
        {"images": True, "max_items_per_store": 3},
    ),
    "woocommerce": (
        "woocommerce.json.gz",
        ["https://porterandyork.com"],
        {"platform": "woocommerce"},
    ),
    "robots_blocked": (
        "robots_blocked.json.gz",
        ["https://woocommerce.com"],
        {"platform": "woocommerce", "max_items_per_store": 150},
    ),
}


def _identity(item: dict[str, Any]) -> str:
    if "url" in item:
        return item["url"]
    return f"{item['store']}#{item['id']}"


def _sorted_by_id_aliases(entries: dict[str, Any]) -> dict[str, Any]:
    """Serve Store API pages recorded in the default order under the orderby=id URLs too.

    The corpus predates sorting by ID, so this replays a store that ignores the sort.
    """
    aliases = {}
    for key, outcomes in entries.items():
        method, url = key.split(" ", 1)
        if API_PATH in url and "orderby=" not in url:
            query = "&".join(f"{k}={v}" for k, v in BY_ID.items())
            aliases[f"{method} {canonicalize_url(f'{url}&{query}')}"] = outcomes
    return {**entries, **aliases}


def scrape_case(name: str, tmp_path: Path) -> spy.RunResult:
    corpus, urls, options = CASES[name]
    entries = _sorted_by_id_aliases(load_corpus(CORPUS / corpus))
    if options.get("images"):
        options = {**options, "images_dir": tmp_path / "images"}
    return spy.scrape(
        urls,
        fetcher_factory=lambda: ReplayFetcher(entries),
        on_failure="ignore",
        _backoff=False,
        throttle=False,
        **options,
    ).collect()


def run_case(name: str, tmp_path: Path) -> dict[str, Any]:
    result = scrape_case(name, tmp_path)
    snapshot: dict[str, Any] = {
        "items": sorted(_identity(i) for i in result.items),
        "stores": [[s.input_url, s.status.value, s.items] for s in result.stores],
        "finish_reason": result.finish_reason,
    }
    if CASES[name][2].get("images"):
        snapshot["images"] = sorted(
            (rec["url"], rec["checksum"]) for i in result.items for rec in i["scraped_images"]
        )
    return json.loads(json.dumps(snapshot))


@pytest.mark.parametrize("name", list(CASES))
def test_replay_matches_snapshot(name, tmp_path):
    expected = json.loads(SNAPSHOTS.read_text())[name]
    assert run_case(name, tmp_path) == expected


def test_woocommerce_variations_have_their_own_prices():
    entries = load_corpus(CORPUS / CASES["woocommerce"][0])
    fetcher = ReplayFetcher(entries)
    result = spy.scrape(
        "https://porterandyork.com",
        platform="woocommerce",
        fetcher_factory=lambda: fetcher,
        _backoff=False,
        throttle=False,
    ).collect()
    rows = [row for item in result.items for row in flatten(item) or []]
    filet = sorted((r["price"], r["sku"]) for r in rows if r["id"] == 132)
    assert [str(price) for price, _ in filet] == ["28", "34", "35", "40", "40", "50"]
    assert len({sku for _, sku in filet}) == 6
    # One variation listing covers the whole product page
    assert not fetcher.misses
    assert sum("type=variation" in key for key in entries) == 1
    assert result.stores[0].responses_by_status == {200: 2}


def test_one_csv_holds_both_platforms(tmp_path):
    items = scrape_case("collections", tmp_path).items + scrape_case("woocommerce", tmp_path).items
    path = tmp_path / "mixed.csv"
    spy.write_items(items, path)
    rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    counts = Counter((r["platform"], r["kind"]) for r in rows)
    assert set(counts) == {
        ("shopify", "product"),
        ("shopify", "collection"),
        ("woocommerce", "product"),
    }

    def variants(item: dict[str, Any]) -> list[Any]:
        return (item.get("product") or {}).get("variants") or item.get("variations") or []

    assert len(rows) == sum(max(len(variants(i)), 1) for i in items)


if __name__ == "__main__" and sys.argv[1:] == ["--update"]:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        data = {name: run_case(name, Path(tmp) / name) for name in CASES}
    SNAPSHOTS.write_text(json.dumps(data, indent=1) + "\n")
    print(f"wrote {SNAPSHOTS}")
