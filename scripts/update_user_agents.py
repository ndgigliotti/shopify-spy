"""Update the browser versions in shopify_spy/user_agents.json from the vendors' feeds.

Run monthly or before a release: uv run python scripts/update_user_agents.py

Browsers freeze most of their user agent (OS tokens, minor versions), so only
major versions change, and this script updates only those, from official sources:

- Chrome: Google's Version History API (two newest stable majors per platform)
- Firefox: Mozilla's product-details (latest release and ESR)
- Edge: Microsoft's Edge update API (newest stable major)

Everything else is a template: the string formats below and SAFARI_VERSION (Apple
has no machine-readable feed). The script can't tell when a vendor changes its
format, so every run ends with a reminder to research that (an agent with web
access, or by hand), update the templates here, and set "formats_verified" in the
JSON to the date of that check.

Exit status: 0 when the file is current or was updated (stdout says which); 2 when
a generated string breaks a version-token rule real browsers follow (nothing is
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
CHROME_API = (
    "https://versionhistory.googleapis.com/v1/chrome/platforms/{platform}/channels/stable/"
    "versions?order_by=version%20desc&pageSize=200"
)
FIREFOX_API = "https://product-details.mozilla.org/1.0/firefox_versions.json"
EDGE_API = "https://edgeupdates.microsoft.com/api/products"

# Frozen platform tokens, as each browser sends them whatever the real OS version
# (Chrome: name -> (Version History platform id, token))
CHROME = {
    "Windows": ("win", "Windows NT 10.0; Win64; x64"),
    "macOS": ("mac", "Macintosh; Intel Mac OS X 10_15_7"),
    "Linux": ("linux", "X11; Linux x86_64"),
}
FIREFOX = {
    "Windows": ["Windows NT 10.0; Win64; x64"],
    "macOS": ["Macintosh; Intel Mac OS X 10.15"],
    "Linux": ["X11; Linux x86_64", "X11; Ubuntu; Linux x86_64"],
}

# (marker, pattern a string containing the marker must match, problem) -- version
# tokens a real browser always sends together
TOKEN_RULES = [
    (
        "Chrome/",
        re.compile(r"Chrome/\d+\.0\.0\.0 Safari/537\.36"),
        "Chrome not in reduced 'Chrome/N.0.0.0 Safari/537.36' form",
    ),
    (
        "Edg/",
        re.compile(r"Chrome/(\d+)\.0\.0\.0 Safari/537\.36 Edg/\1\.0\.0\.0$"),
        "Edge major doesn't match its Chrome major",
    ),
    (
        "Firefox/",
        re.compile(r"rv:([\d.]+)\).*Firefox/\1$"),
        "Firefox rv: doesn't equal its Firefox/ version",
    ),
    (
        # major.minor, sometimes with a patch (Version/26.0, Version/26.6.1); never bare
        "Version/",
        re.compile(r"Version/\d+\.\d+(\.\d+)? Safari/"),
        "Safari Version/ isn't major.minor",
    ),
]

REMINDER = """\
REMINDER: only major versions were updated. String formats and SAFARI_VERSION
({safari}) were last verified {verified}. Before merging, research online whether any
of them changed: Safari's current version (Apple's Safari release notes), and
announced user agent changes from Chrome (chromestatus.com), Firefox (MDN) and Edge.
Compare against real user agents, e.g. useragents.me; informal lists like
jnrbsn/user-agents are templated themselves (they drop Safari's minor version).
If anything changed, edit the templates in scripts/update_user_agents.py; either way,
set "formats_verified" in {output} to the date you checked."""

Agents = dict[str, list[str]]  # group label -> user agents


def get_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def major(version: str) -> int:
    return int(version.split(".")[0])


def chrome_majors() -> list[int]:
    """The two newest majors released on every desktop platform.

    A major on only one platform is still rolling out (rare in real traffic), so
    the same two majors are used everywhere.
    """
    per_platform = [
        {major(v["version"]) for v in get_json(CHROME_API.format(platform=p))["versions"]}
        for p, _ in CHROME.values()
    ]
    return sorted(set.intersection(*per_platform))[-2:]


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
    """User agents by group label, in the order they're listed."""
    agents: Agents = {}
    chrome = chrome_majors()
    for name, (_, token) in CHROME.items():
        agents[f"Chrome - {name}"] = [chrome_ua(token, m) for m in chrome]
    ff = firefox_majors()
    for name, tokens in FIREFOX.items():
        agents[f"Firefox - {name}"] = [
            f"Mozilla/5.0 ({token}; rv:{m}.0) Gecko/20100101 Firefox/{m}.0"
            for token in tokens
            for m in ff
        ]
    agents["Safari - macOS"] = [
        f"Mozilla/5.0 ({CHROME['macOS'][1]}) AppleWebKit/605.1.15 (KHTML, like Gecko) "
        f"Version/{SAFARI_VERSION} Safari/605.1.15"
    ]
    e = edge_major()
    agents["Edge - Windows"] = [f"{chrome_ua(CHROME['Windows'][1], e)} Edg/{e}.0.0.0"]
    return agents


def consistency_errors(agents: list[str]) -> list[str]:
    """Version tokens a real browser always sends together; a mismatch is a fake-UA tell."""
    errors = []
    for a in agents:
        for marker, pattern, problem in TOKEN_RULES:
            if marker in a and not pattern.search(a):
                errors.append(f"{problem}: {a}")
    return errors


def load() -> dict:
    try:
        return json.loads(OUTPUT.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def main() -> int:
    agents = build()
    errors = consistency_errors([a for group in agents.values() for a in group])
    if errors:
        for error in errors:
            print(f"ERROR {error}")
        print(f"Not writing {OUTPUT}: fix the templates in this script first")
        return 2
    current = load()
    today = datetime.date.today().isoformat()
    # Only research sets this; never stamp a check that didn't happen
    verified = current.get("formats_verified")
    # Leave the file (and its dates) alone unless the list changed, so a clean
    # git diff means there's nothing to update
    if current.get("agents") == agents:
        print(f"Unchanged: {OUTPUT}")
    else:
        data = {"generated": today, "formats_verified": verified, "agents": agents}
        OUTPUT.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
        print(f"Wrote {OUTPUT}")
    print(REMINDER.format(safari=SAFARI_VERSION, verified=verified or "never", output=OUTPUT.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
