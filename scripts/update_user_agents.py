"""Update the browser versions in shopify_spy/user_agents.json from the vendors' feeds.

Run monthly or before a release: uv run python scripts/update_user_agents.py

Browsers freeze most of their user agent (OS tokens, minor versions), so only
major versions change, and this script updates only those, from official sources:

- Chrome: Google's Version History API. The newest major that has been on stable
  for two weeks (an early rollout to a few percent doesn't count) and the one before.
- Firefox: Mozilla's product-details (latest release and ESR)
- Edge: Microsoft's Edge update API (newest stable major)

Everything else is hand-set: the string templates and SAFARI_VERSION (Apple has no
machine-readable feed). The script can't tell when a vendor changes its format, so
every run ends with a reminder to research that (an agent with web access, or by
hand), update them here, and set "formats_verified" in the JSON to the date of that
check.

Exit status: 0 when the file is current or was updated (stdout says which); 2 when
a generated string doesn't match a real browser's shape (SHAPES; nothing is
written; the templates are wrong); 1 with a traceback when a feed is unreachable
or has changed shape.
"""

from __future__ import annotations

import datetime
import json
import re
import sys
import urllib.request
from pathlib import Path

# major.minor (sometimes .patch), as Safari always sends it; a bare major is a fake tell
SAFARI_VERSION = "27.0"

OUTPUT = Path(__file__).resolve().parent.parent / "shopify_spy" / "user_agents.json"
# Releases currently served on the stable channel, with the fraction of users each gets
# Recent stable releases with their start dates (a few months' worth)
CHROME_API = (
    "https://versionhistory.googleapis.com/v1/chrome/platforms/{platform}/channels/stable/"
    "versions/all/releases?order_by=starttime%20desc&pageSize=300"
)
FIREFOX_API = "https://product-details.mozilla.org/1.0/firefox_versions.json"
EDGE_API = "https://edgeupdates.microsoft.com/api/products"
ROLLOUT_DAYS = 14  # a new Chrome major reaches most stable users within about this

# Frozen platform tokens, as each browser sends them whatever the real OS version
CHROME = {  # name -> (Version History platform id, token)
    "Windows": ("win", "Windows NT 10.0; Win64; x64"),
    "macOS": ("mac", "Macintosh; Intel Mac OS X 10_15_7"),
    "Linux": ("linux", "X11; Linux x86_64"),
}
FIREFOX = {
    "Windows": ["Windows NT 10.0; Win64; x64"],
    "macOS": ["Macintosh; Intel Mac OS X 10.15"],
    "Linux": ["X11; Linux x86_64", "X11; Ubuntu; Linux x86_64"],
}

# Exact shapes of real browser UAs, on the platforms each browser reports; every
# generated string must match one in full. Mirrors 0.2.x's tests/test_middlewares.py
# _UA_SHAPES (master 1f1b501), which several reviews tightened.
_V = r"[1-9]\d*"  # a major version, never zero-padded
_CHROME_KIT = r"AppleWebKit/537\.36 \(KHTML, like Gecko\)"
_SAFARI_KIT = r"AppleWebKit/605\.1\.15 \(KHTML, like Gecko\)"
_SAFARI_VERSION = rf"Version/{_V}\.\d+(?:\.\d+)?"
SHAPES = [
    # Desktop Chrome and Edge (reduced UA: major version only, Edge's equal to Chrome's)
    re.compile(
        r"Mozilla/5\.0 \((?:Windows NT 10\.0; Win64; x64|Macintosh; Intel Mac OS X 10_15_7"
        rf"|X11; Linux x86_64)\) {_CHROME_KIT} Chrome/(?P<v>{_V})\.0\.0\.0 Safari/537\.36"
        r"(?: Edg/(?P=v)\.0\.0\.0)?"
    ),
    # Android Chrome
    re.compile(
        rf"Mozilla/5\.0 \(Linux; Android {_V}; K\) {_CHROME_KIT} "
        rf"Chrome/{_V}\.0\.0\.0 Mobile Safari/537\.36"
    ),
    # Android WebView (wv), which adds Version/4.0
    re.compile(
        rf"Mozilla/5\.0 \(Linux; Android {_V}; K; wv\) {_CHROME_KIT} "
        rf"Version/4\.0 Chrome/{_V}\.0\.0\.0 Mobile Safari/537\.36"
    ),
    # Desktop Firefox: rv: equals the Firefox version
    re.compile(
        r"Mozilla/5\.0 \((?:Windows NT 10\.0; Win64; x64|Macintosh; Intel Mac OS X 10\.15"
        rf"|X11; (?:Ubuntu; )?Linux x86_64); rv:(?P<v>{_V}\.\d+)\) Gecko/20100101 Firefox/(?P=v)"
    ),
    # Safari on macOS
    re.compile(
        rf"Mozilla/5\.0 \(Macintosh; Intel Mac OS X 10_15_7\) {_SAFARI_KIT} "
        rf"{_SAFARI_VERSION} Safari/605\.1\.15"
    ),
    # Safari on iOS (iPhone sends "CPU iPhone OS") and iPadOS ("CPU OS")
    re.compile(
        rf"Mozilla/5\.0 \((?:iPhone; CPU iPhone OS|iPad; CPU OS) {_V}(?:_\d+)* like Mac OS X\) "
        rf"{_SAFARI_KIT} {_SAFARI_VERSION} Mobile/\w+ Safari/604\.1"
    ),
]

REMINDER = """\
REMINDER: only major versions were updated. String formats and SAFARI_VERSION
({safari}) were last verified {verified}. Before merging, research online whether any
of them changed: Safari's current version (Apple's Safari release notes), and
announced user agent changes from Chrome (chromestatus.com), Firefox (MDN) and Edge.
Compare against real user agents, e.g. useragents.me; informal lists like
jnrbsn/user-agents are templated themselves (they drop Safari's minor version).
If anything changed, edit scripts/update_user_agents.py (templates and SHAPES
together); either way, set "formats_verified" in {output} to the date you checked."""

Agents = dict[str, dict[str, list[str]]]  # browser -> platform -> user agents


def get_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def major(version: str) -> int:
    return int(version.split(".")[0])


def chrome_majors(today: datetime.date | None = None) -> list[int]:
    """The newest major out on stable for ROLLOUT_DAYS, and the one before.

    A new major reaches a few percent of stable users first (157 at 0.5% on Windows
    only, Oct 2026) and everyone over about two weeks; until then it's rare enough
    to stand out. (The feed's per-release "fraction" is relative to a rollout group,
    not to all users, so it can't say how common a version is.)
    """
    cutoff = ((today or datetime.date.today()) - datetime.timedelta(days=ROLLOUT_DAYS)).isoformat()
    # When each major first reached stable on any platform: rollouts start on one
    # platform (157: Windows only) and others can follow later at full share
    first_seen: dict[int, str] = {}
    for platform_id, _ in CHROME.values():
        for r in get_json(CHROME_API.format(platform=platform_id))["releases"]:
            m, start = major(r["version"]), r["serving"]["startTime"][:10]
            first_seen[m] = min(first_seen.get(m, start), start)
    settled = [m for m, start in first_seen.items() if start <= cutoff]
    if not settled:
        raise RuntimeError(f"no Chrome major on stable since {cutoff}; has the feed changed?")
    return [max(settled) - 1, max(settled)]


def firefox_majors() -> list[int]:
    data = get_json(FIREFOX_API)
    return sorted({major(data["FIREFOX_ESR"]), major(data["LATEST_FIREFOX_VERSION"])})


def edge_major() -> int:
    products = get_json(EDGE_API)
    releases = [
        major(r["ProductVersion"])
        for p in products
        if p.get("Product") == "Stable"
        for r in p.get("Releases", [])
        if r.get("Platform") == "Windows"
    ]
    if not releases:
        raise RuntimeError(f"no stable Windows release in {EDGE_API}; has its format changed?")
    return max(releases)


def chrome_ua(token: str, version: int) -> str:
    return (
        f"Mozilla/5.0 ({token}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{version}.0.0.0 Safari/537.36"
    )


def build() -> Agents:
    """User agents by browser and platform."""
    chrome = chrome_majors()
    ff = firefox_majors()
    e = edge_major()
    return {
        "Chrome": {
            name: [chrome_ua(token, m) for m in chrome] for name, (_, token) in CHROME.items()
        },
        "Firefox": {
            name: [
                f"Mozilla/5.0 ({token}; rv:{m}.0) Gecko/20100101 Firefox/{m}.0"
                for token in tokens
                for m in ff
            ]
            for name, tokens in FIREFOX.items()
        },
        "Safari": {
            "macOS": [
                f"Mozilla/5.0 ({CHROME['macOS'][1]}) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                f"Version/{SAFARI_VERSION} Safari/605.1.15"
            ]
        },
        "Edge": {"Windows": [f"{chrome_ua(CHROME['Windows'][1], e)} Edg/{e}.0.0.0"]},
    }


def flatten(agents: Agents) -> list[str]:
    return [a for platforms in agents.values() for group in platforms.values() for a in group]


def shape_errors(agents: list[str]) -> list[str]:
    """User agents that don't match any real browser's shape in full."""
    return [a for a in agents if not any(shape.fullmatch(a) for shape in SHAPES)]


def load() -> dict:
    try:
        return json.loads(OUTPUT.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def main() -> int:
    agents = build()
    errors = shape_errors(flatten(agents))
    if errors:
        for agent in errors:
            print(f"ERROR not a real browser's user agent shape: {agent}")
        print(f"Not writing {OUTPUT}: fix the templates in this script first")
        return 2
    current = load()
    # Only research sets this; never stamp a check that didn't happen
    verified = current.get("formats_verified")
    # Leave the file (and its dates) alone unless something changed, so a clean
    # git diff means there's nothing to update
    if current.get("agents") == agents:
        print(f"Unchanged: {OUTPUT}")
    else:
        data = {
            "generated": datetime.date.today().isoformat(),
            "formats_verified": verified,
            "agents": agents,
        }
        OUTPUT.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
        print(f"Wrote {OUTPUT}")
    print(REMINDER.format(safari=SAFARI_VERSION, verified=verified or "never", output=OUTPUT.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
