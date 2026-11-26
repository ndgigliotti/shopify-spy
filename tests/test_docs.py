"""Checks that the README and docs/ pages stay linked up and in step with the code."""

import re
import tomllib
from pathlib import Path

import pytest

import shopify_spy as spy
from shopify_spy import StoreStatus, cli

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
DOCS = ROOT / "docs"
PAGES = [README, *sorted(DOCS.glob("*.md"))]

# The README is the PyPI page, where relative links break, so it links to docs/ by URL,
# pinned to the release whose PyPI page shows it.
DOCS_URL = re.compile(
    r"https://github\.com/ndgigliotti/shopify-spy/blob/([^/]+)/docs/([^)#\s]+)(#[^)\s]*)?"
)
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
FENCE = re.compile(r"^```.*?^```", re.M | re.S)


def _version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def _anchors(page: Path) -> set[str]:
    text = FENCE.sub("", page.read_text(encoding="utf-8"))
    return {_slug(m) for m in re.findall(r"^#+ (.+)$", text, re.M)}


def _links(page: Path) -> list[str]:
    return MD_LINK.findall(FENCE.sub("", page.read_text(encoding="utf-8")))


def test_readme_docs_links_name_the_current_version():
    tags = {m.group(1) for m in DOCS_URL.finditer(README.read_text(encoding="utf-8"))}
    assert tags == {f"v{_version()}"}


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_links_resolve(page):
    broken = []
    for link in _links(page):
        if m := DOCS_URL.fullmatch(link):
            target, anchor = DOCS / m.group(2), (m.group(3) or "")[1:]
        elif link.startswith(("http://", "https://", "mailto:")):
            continue
        else:
            path, _, anchor = link.partition("#")
            target = (page.parent / path) if path else page
        if not target.is_file() or (anchor and anchor not in _anchors(target)):
            broken.append(link)
    assert not broken


@pytest.mark.parametrize(
    "text",
    [cli.DOCS_EPILOG, spy.scrape.__doc__, spy.scrape_async.__doc__],
    ids=["cli", "scrape", "scrape_async"],
)
def test_help_links_name_installed_docs(text):
    links = list(DOCS_URL.finditer(text))
    assert links
    for m in links:
        assert m.group(1) == f"v{_version()}"
        assert (DOCS / m.group(2)).is_file()


def test_scrape_doc_names_every_platform():
    # Lowercase quoted platform lists, which a case-sensitive grep for stale
    # three-platform text misses
    doc = spy.scrape.__doc__
    assert doc.count('"jsonld"') >= 2
    assert "JsonLdProductItem" in doc
    assert '"woocommerce" or "magento"' not in doc


def test_troubleshooting_covers_every_status():
    text = (DOCS / "troubleshooting.md").read_text(encoding="utf-8")
    missing = [s for s in [*StoreStatus, "invalid_url"] if f"| `{s}` |" not in text]
    assert not missing
