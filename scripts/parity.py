"""Parity check between the legacy Scrapy CLI and the new httpx CLI.

Record a corpus once with the legacy engine (hits the network):

    uv run python scripts/parity.py record tests/fixtures/corpus/shop.json.gz -- \\
        https://store.example --limit 300

Replay it into both engines and diff their output (offline):

    uv run python scripts/parity.py compare tests/fixtures/corpus/shop.json.gz -- \\
        https://store.example --limit 300

Expected differences (plan §7.2) are normalized away; anything else is reported.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LEGACY = "shopify_spy.cli"
NEW = "shopify_spy._cli_next"
SEED = "1234"


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SHOPIFY_SPY_")}
    env["COLUMNS"] = "1000"  # keep Rich from wrapping lines
    env.update(extra)
    return env


def run_cli(module: str, args: list[str], out: Path, env: dict[str, str]) -> dict[str, Any]:
    """Run a CLI in a subprocess; return exit code, output text and produced files."""
    out.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-m", module, "scrape", *args, "--output", str(out), "--quiet"],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,  # the legacy engine needs scrapy.cfg (or SCRAPY_SETTINGS_MODULE)
        timeout=1800,
    )
    return {"code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr, "out": out}


def record(corpus: Path, args: list[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        result = run_cli(
            LEGACY,
            args,
            Path(tmp),
            _env(
                SCRAPY_SETTINGS_MODULE="shopify_spy._parity.settings",
                SHOPIFY_SPY_RECORD=str(corpus.resolve()),
                SHOPIFY_SPY_SEED=SEED,
            ),
        )
    print(result["stdout"], result["stderr"], sep="\n")
    print(f"recorded {corpus} (exit {result['code']})")


def _items(out: Path) -> list[Any]:
    files = sorted((out / "data").glob("*")) if (out / "data").exists() else []
    items: list[Any] = []
    for path in files:
        if path.suffix == ".jsonl":
            items += [json.loads(line) for line in path.read_text().splitlines() if line]
        elif path.suffix == ".json":
            items += json.loads(path.read_text())
        elif path.suffix == ".csv":
            for row in csv.DictReader(path.open(newline="", encoding="utf-8")):
                cells = {k: _csv_cell(v) for k, v in row.items()}
                items.append({k: v for k, v in cells.items() if v not in ("", [], None)})
        elif path.suffix == ".xml":
            # Compare each <item> element as text; item order isn't deterministic
            text = path.read_text()
            items += re.findall(r"<item>.*?</item>", text, flags=re.S)
    return items


def _csv_columns(out: Path) -> set[str] | None:
    files = list((out / "data").glob("*.csv")) if (out / "data").exists() else []
    if not files:
        return None
    with files[0].open(newline="", encoding="utf-8") as f:
        return set(next(csv.reader(f), []))


def _csv_cell(value: str) -> Any:
    # D2: the new writer JSON-encodes nested cells; Scrapy wrote Python repr. Decode
    # both to the same structure where possible.
    try:
        decoded = json.loads(value)
    except ValueError:
        try:
            import ast

            decoded = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            decoded = value
    # Scrapy joined lists of strings with commas
    if isinstance(decoded, (list, tuple)) and all(isinstance(v, str) for v in decoded):
        return ",".join(decoded)
    return decoded


def _normalize_item(item: Any) -> Any:
    if isinstance(item, dict) and "scraped_images" in item:
        # D1: original image bytes and extensions, so paths/checksums differ
        item = dict(item)
        item["scraped_images"] = sorted(
            (rec.get("url"), re.sub(r"\.\w+$", "", rec.get("path", "")), rec.get("status"))
            for rec in item["scraped_images"]
        )
    return item


def _status(out: Path) -> dict[str, Any]:
    files = list((out / "status").glob("*_status.json")) if (out / "status").exists() else []
    if not files:
        return {}
    data = json.loads(files[0].read_text())
    return {
        "items_scraped": data["items_scraped"],
        "finish_reason": data["finish_reason"],
        "urls": sorted((e["url"], e["items"], e["status"]) for e in data["urls"]),
    }


def _summary(text: str) -> list[str]:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("Log:", "Output:")) or "shopify-spy/logs" in line:
            continue  # paths contain timestamps and temp dirs
        lines.append(line.rstrip())
    return [line for line in lines if line]


def compare(corpus: Path, args: list[str]) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        legacy = run_cli(
            LEGACY,
            args,
            Path(tmp) / "legacy",
            _env(
                SCRAPY_SETTINGS_MODULE="shopify_spy._parity.settings",
                SHOPIFY_SPY_REPLAY=str(corpus.resolve()),
                SHOPIFY_SPY_SEED=SEED,
            ),
        )
        new = run_cli(
            NEW,
            args,
            Path(tmp) / "new",
            _env(SHOPIFY_SPY_REPLAY=str(corpus.resolve()), SHOPIFY_SPY_PARITY="1"),
        )
        problems = []
        if legacy["code"] != new["code"]:
            problems.append(f"exit code: legacy {legacy['code']} vs new {new['code']}")
        legacy_items, new_items_list = _items(legacy["out"]), _items(new["out"])
        columns = _csv_columns(legacy["out"])
        if columns is not None:
            # D2: Scrapy took CSV columns from the first item and dropped the rest
            new_items_list = [{k: v for k, v in i.items() if k in columns} for i in new_items_list]
        old_items = Counter(json.dumps(_normalize_item(i), sort_keys=True) for i in legacy_items)
        new_items = Counter(json.dumps(_normalize_item(i), sort_keys=True) for i in new_items_list)
        if old_items != new_items:
            missing = old_items - new_items
            extra = new_items - old_items
            problems.append(
                f"items: legacy {sum(old_items.values())} vs new {sum(new_items.values())}; "
                f"{sum(missing.values())} only in legacy, {sum(extra.values())} only in new"
            )
            for label, diff in (("only in legacy", missing), ("only in new", extra)):
                for text in list(diff)[:3]:
                    problems.append(f"  {label}: {text[:300]}")
        old_status, new_status = _status(legacy["out"]), _status(new["out"])
        if old_status != new_status:
            problems.append(f"status: legacy {old_status} vs new {new_status}")
        old_text = _summary(legacy["stdout"] + legacy["stderr"])
        new_text = _summary(new["stdout"] + new["stderr"])
        if old_text != new_text:
            problems.append(f"summary text:\n  legacy: {old_text}\n  new:    {new_text}")
    total = sum(old_items.values())
    if problems:
        print(f"PARITY DIFFERENCES for {corpus.name}:")
        print("\n".join(problems))
        return 1
    print(f"parity OK for {corpus.name}: {total} items, exit {legacy['code']}")
    return 0


CORPUS = ROOT / "tests" / "fixtures" / "corpus"
MULTI = ["https://www.snowdevil.ca", "https://www.beardbrand.com",
         "https://shop.nonexistent-store.invalid"]  # fmt: skip
# (corpus, CLI args) pairs checked by `parity.py suite`
SUITE: list[tuple[str, list[str]]] = [
    ("multi_store.json.gz", MULTI),
    ("multi_store.json.gz", [*MULTI, "--limit", "5"]),
    ("collections.json.gz", ["https://www.snowdevil.ca", "--collections"]),
    ("collections.json.gz", ["https://www.snowdevil.ca", "--collections", "-F", "json"]),
    ("collections.json.gz", ["https://www.snowdevil.ca", "--collections", "-F", "xml"]),
    ("collections.json.gz", ["https://www.snowdevil.ca", "--collections", "-F", "csv"]),
    ("images.json.gz", ["https://www.snowdevil.ca", "--images", "--limit", "3"]),
    ("woocommerce.json.gz", ["https://porterandyork.com", "--platform", "woocommerce"]),
    ("robots_blocked.json.gz",
     ["https://woocommerce.com", "--platform", "woocommerce", "--limit", "150"]),
]  # fmt: skip


def suite() -> int:
    failures = 0
    for corpus, args in SUITE:
        print(f"$ compare {corpus} -- {' '.join(args)}")
        failures += compare(CORPUS / corpus, args)
    print(f"{len(SUITE) - failures}/{len(SUITE)} parity cases OK")
    return 1 if failures else 0


def main() -> int:
    if sys.argv[1:] == ["suite"]:
        return suite()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["record", "compare", "suite"])
    parser.add_argument("corpus", type=Path)
    parser.add_argument("args", nargs=argparse.REMAINDER, help="CLI arguments after --")
    ns = parser.parse_args()
    args = ns.args[1:] if ns.args[:1] == ["--"] else ns.args
    if ns.command == "record":
        record(ns.corpus, args)
        return 0
    return compare(ns.corpus, args)


if __name__ == "__main__":
    sys.exit(main())
