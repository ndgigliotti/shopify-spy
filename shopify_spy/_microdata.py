"""schema.org items from a page's microdata (``itemscope``, ``itemtype``, ``itemprop``),
in the shape JSON-LD gives them, for pages that have no JSON-LD Product.

The page is read with the standard library's HTML parser, which keeps no tree: each
item collects its properties while its element is open. Values follow the W3C
microdata rules (https://www.w3.org/TR/microdata/#values): ``content`` on ``meta``,
``src`` or ``href`` on media and links (resolved against the page), ``value`` on
``data`` and ``meter``, ``datetime`` on ``time``, else ``content`` if present, else
the element's text. ``itemref`` isn't followed.
"""

from __future__ import annotations

import re
import urllib.parse
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

MAX_ITEMS = 2_000  # items read per page
MAX_TEXT = 100_000  # characters of text read for the values open at once
MAX_TEXT_VALUES = 32  # text values open at once; ones nested deeper are skipped
# A cheap test before parsing, which costs tens of milliseconds on a large page
_PRODUCT = re.compile(r"""\bitemtype\s*=\s*["']?[^"'>]*schema\.org/Product""", re.I)
_SCHEMA = re.compile(r"^(?:https?://schema\.org/|schema:)", re.I)
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
     "track", "wbr", "param", "keygen"}
)  # fmt: skip
# Elements a start tag of their own kind closes, and those that close an open <p>
_SELF_CLOSING = frozenset({"p", "li", "dt", "dd", "option", "tr", "td", "th"})
_CLOSES_P = frozenset(
    {"address", "article", "aside", "blockquote", "div", "dl", "fieldset", "figure", "footer",
     "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "main", "nav", "ol", "p", "pre",
     "section", "table", "ul"}
)  # fmt: skip
_SRC = frozenset({"audio", "embed", "iframe", "img", "source", "track", "video"})
_HREF = frozenset({"a", "area", "link"})
RATINGS = frozenset({"AggregateRating", "Rating"})


@dataclass
class _Item:
    types: list[str]
    itemid: str | None
    properties: dict[str, list[Any]] = field(default_factory=dict)

    def add(self, names: list[str], value: Any) -> None:
        for name in names:
            self.properties.setdefault(name, []).append(value)


@dataclass
class _Open:
    """An open element: the item it starts, and the properties it's the text of."""

    tag: str
    item: _Item | None = None
    names: list[str] | None = None  # itemprop names whose value is this element's text
    owner: _Item | None = None  # the item those properties belong to
    start: int = 0  # where its text begins in the reader's chunks


class _Reader(HTMLParser):
    def __init__(self, url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.url = url
        self.top: list[_Item] = []
        self.count = 0
        self._open: list[_Open] = []
        self._tags: Counter[str] = Counter()  # of the open elements
        self._scopes: list[_Item] = []
        self._values = 0  # open elements whose text is a value
        self._chunks: list[str] = []  # text since the outermost of them opened
        self._size = 0
        self._skip = 0  # inside script or style

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._open and (
            (tag in _SELF_CLOSING and self._open[-1].tag == tag)
            or (tag in _CLOSES_P and self._open[-1].tag == "p")
        ):
            self._close(self._open.pop())
        values = {k: v or "" for k, v in attrs}
        names = [_SCHEMA.sub("", n) for n in values.get("itemprop", "").split()]
        owner = self._scopes[-1] if self._scopes else None
        element = _Open(tag)
        if "itemscope" in values and self.count < MAX_ITEMS:
            self.count += 1
            types = [_SCHEMA.sub("", t).rstrip("/") for t in values.get("itemtype", "").split()]
            element.item = _Item(types, values.get("itemid", "").strip() or None)
            if names and owner is not None:
                owner.add(names, element.item)
            else:
                self.top.append(element.item)
        elif names and owner is not None:
            value = self._attribute_value(tag, values)
            if value is not None:
                owner.add(names, value)
            elif tag not in _VOID and self._values < MAX_TEXT_VALUES:
                element.names, element.owner, element.start = names, owner, len(self._chunks)
                self._values += 1
        if tag in _VOID:
            return
        if tag in ("script", "style"):
            self._skip += 1
        self._open.append(element)
        self._tags[tag] += 1
        if element.item is not None:
            self._scopes.append(element.item)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def _attribute_value(self, tag: str, values: dict[str, str]) -> str | None:
        if tag == "meta":
            return values.get("content", "")
        if tag in _SRC:
            return self._link(values.get("src", ""))
        if tag in _HREF:
            return self._link(values.get("href", ""))
        if tag == "object":
            return self._link(values.get("data", ""))
        if tag in ("data", "meter"):
            return values.get("value", "")
        if tag == "time" and "datetime" in values:
            return values["datetime"]
        # Not in the W3C rules, but schema.org's examples use it
        return values.get("content") or None

    def _link(self, value: str) -> str:
        # Without the attribute the value is empty, not the page (a lazy-loaded
        # <img> has only data-src)
        if not value.strip():
            return ""
        try:
            return urllib.parse.urljoin(self.url, value.strip())
        except ValueError:
            return value.strip()

    def handle_data(self, data: str) -> None:
        if self._values and not self._skip and self._size < MAX_TEXT:
            self._chunks.append(data)
            self._size += len(data)

    def handle_endtag(self, tag: str) -> None:
        # An end tag closes the elements opened inside it that HTML left unclosed;
        # one with nothing open to match is ignored
        if not self._tags[tag]:
            return
        while (element := self._open.pop()).tag != tag:
            self._close(element)
        self._close(element)

    def close(self) -> None:
        super().close()
        while self._open:
            self._close(self._open.pop())

    def _close(self, element: _Open) -> None:
        self._tags[element.tag] -= 1
        if element.tag in ("script", "style") and self._skip:
            self._skip -= 1
        if element.item is not None and self._scopes and self._scopes[-1] is element.item:
            self._scopes.pop()
        if element.names is not None and element.owner is not None:
            value = " ".join("".join(self._chunks[element.start :]).split())
            element.owner.add(element.names, value)
            self._values -= 1
            if not self._values:
                self._chunks.clear()
                self._size = 0


def _node(item: _Item, depth: int = 0) -> dict[str, Any]:
    """An item as a JSON-LD node: one value as is, several as a list.

    Repeats of a value are dropped: pages often give a property twice, as a
    ``<meta itemprop="name">`` beside an ``<h1 itemprop="name">``, and a list of
    one name isn't read as a name."""
    node: dict[str, Any] = {}
    if item.types:
        node["@type"] = item.types[0] if len(item.types) == 1 else item.types
    if item.itemid:
        node["@id"] = item.itemid
    for name, values in item.properties.items():
        found = [_node(v, depth + 1) if isinstance(v, _Item) and depth < 32 else v for v in values]
        texts: set[str] = set()
        found = [v for v in found if not isinstance(v, _Item) and not _repeat(v, texts)]
        if found:
            node[name] = found[0] if len(found) == 1 else found
    kinds = set(item.types)
    # A rating is read only on a stated scale: without one, Magento's 60 (of 100)
    # and BigCommerce's 0 (no reviews) look like ratings out of 5
    if kinds & RATINGS and "bestRating" not in node and "worstRating" not in node:
        node.pop("ratingValue", None)
    # Shopware puts the product's name inside its offer
    if "Product" in kinds and "name" not in node:
        offers = [o for o in _list(node.get("offers")) if isinstance(o, dict) and o.get("name")]
        if len(offers) == 1:
            node["name"] = offers[0]["name"]
    return node


def _repeat(value: Any, texts: set[str]) -> bool:
    """Whether ``value`` is a string already in ``texts``; adds it if not."""
    if not isinstance(value, str):
        return False
    if value in texts:
        return True
    texts.add(value)
    return False


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def items(page: str, url: str) -> list[dict[str, Any]]:
    """The page's top-level microdata items, as JSON-LD nodes; none unless the page
    names a schema.org Product type."""
    if not _PRODUCT.search(page):
        return []
    reader = _Reader(url)
    reader.feed(page)
    reader.close()
    return [_node(item) for item in reader.top]
