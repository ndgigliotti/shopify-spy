"""The shipped user agent list and scripts/update_user_agents.py, which updates it."""

import datetime
import importlib.util
import json
from pathlib import Path

import pytest

from shopify_spy._policy import USER_AGENTS

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "update_user_agents.py"
spec = importlib.util.spec_from_file_location("update_user_agents", SCRIPT)
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

SHIPPED = gen.load()
# Deliberately calendar-based: they fail when the monthly update stops, which is the alarm
MAX_AGE_DAYS = 120  # versions: the monthly job keeps them well under this
MAX_FORMAT_CHECK_AGE_DAYS = 180  # formats: researched at least twice a year


@pytest.fixture
def fixed_versions(monkeypatch):
    """Generate without the network: every feed reports the same versions."""
    monkeypatch.setattr(gen, "chrome_majors", lambda: [150, 151])
    monkeypatch.setattr(gen, "firefox_majors", lambda: [140, 150])
    monkeypatch.setattr(gen, "edge_major", lambda: 151)


@pytest.fixture
def output(tmp_path, monkeypatch):
    path = tmp_path / "user_agents.json"
    monkeypatch.setattr(gen, "OUTPUT", path)
    return path


def age_days(iso_date):
    return (datetime.date.today() - datetime.date.fromisoformat(iso_date)).days


# --- The shipped list -----------------------------------------------------------------


def test_shipped_list_is_well_formed():
    assert USER_AGENTS == gen.flatten(SHIPPED["agents"])
    assert len(USER_AGENTS) == len(set(USER_AGENTS))
    assert set(SHIPPED["agents"]) == {"Chrome", "Edge", "Firefox", "Safari"}


def test_shipped_list_matches_real_browser_shapes():
    assert gen.shape_errors(USER_AGENTS) == []


def test_shipped_versions_are_recent():
    age = age_days(SHIPPED["generated"])
    assert age <= MAX_AGE_DAYS, f"versions are {age} days old; run scripts/update_user_agents.py"


def test_formats_were_researched_recently():
    assert SHIPPED.get("formats_verified"), "user agent formats were never researched"
    age = age_days(SHIPPED["formats_verified"])
    assert age <= MAX_FORMAT_CHECK_AGE_DAYS, (
        f"user agent formats were last checked {age} days ago; research them online "
        "(see the reminder scripts/update_user_agents.py prints) and update formats_verified"
    )


# --- Real browser shapes (cases from 0.2.x's test_browser_shape_check) -----------------

_MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X)"
_IPAD = "Mozilla/5.0 (iPad; CPU OS 18_6 like Mac OS X)"
_WIN = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_WEBVIEW = "Mozilla/5.0 (Linux; Android 10; K; wv)"
_SK = "AppleWebKit/605.1.15 (KHTML, like Gecko)"
_CK = "AppleWebKit/537.36 (KHTML, like Gecko)"
_FF = "Gecko/20100101 Firefox"


@pytest.mark.parametrize(
    ("ua", "valid"),
    [
        # Safari
        pytest.param(f"{_MAC} {_SK} Version/27.0 Safari/605.1.15", True, id="safari"),
        pytest.param(f"{_MAC} {_SK} Version/26.6.1 Safari/605.1.15", True, id="safari-patch"),
        pytest.param(f"{_MAC} {_SK} Version/27 Safari/605.1.15", False, id="safari-bare-major"),
        pytest.param(f"{_MAC} {_CK} Version/27.0 Safari/605.1.15", False, id="safari-wrong-kit"),
        pytest.param(f"{_MAC} {_SK} Version/27.0 Safari/605.1", False, id="safari-short-build"),
        pytest.param(f"{_MAC} {_SK} Safari/605.1.15", False, id="safari-no-version"),
        pytest.param(
            f"{_MAC} {_SK} Version/27.0 Mobile/15E148 Safari/605.1.15",
            False,
            id="safari-mobile-mac",
        ),
        pytest.param(f"{_IPHONE} {_SK} Version/26.0 Mobile/15E148 Safari/604.1", True, id="iphone"),
        pytest.param(f"{_IPAD} {_SK} Version/26.0 Mobile/15E148 Safari/604.1", True, id="ipad"),
        pytest.param(
            f"Mozilla/5.0 (iPad; CPU iPhone OS 18_6 like Mac OS X) {_SK} "
            "Version/26.0 Mobile/15E148 Safari/604.1",
            False,
            id="ipad-iphone-os",
        ),
        pytest.param(f"{_IPHONE} {_SK} Version/26.0 Safari/604.1", False, id="iphone-no-mobile"),
        pytest.param(
            f"{_IPHONE} {_SK} Version/26.0 Mobile/15E148 Safari/605.1.15",
            False,
            id="iphone-mac-build",
        ),
        # Chrome and Edge
        pytest.param(f"{_WIN} {_CK} Chrome/156.0.0.0 Safari/537.36", True, id="chrome"),
        pytest.param(f"{_WIN} {_CK} Chrome/156.0.7000.12 Safari/537.36", False, id="chrome-full"),
        pytest.param(f"{_WIN} {_CK} Chrome/0156.0.0.0 Safari/537.36", False, id="chrome-zero-pad"),
        pytest.param(f"{_WIN} {_CK} Chrom/156.0.0.0 Safari/537.36", False, id="chrome-typo"),
        pytest.param(f"( ) {_CK} Chrome/156.0.0.0 Safari/537.36", False, id="chrome-no-platform"),
        pytest.param(
            f"{_WIN} {_CK} Chrome/156.0.0.0 Mobile Safari/537.36", False, id="chrome-mobile-win"
        ),
        pytest.param(f"{_WIN} {_CK} Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0", True, id="edge"),
        pytest.param(
            f"{_WIN} {_CK} Chrome/157.0.0.0 Safari/537.36 Edg/156.0.0.0", False, id="edge-mismatch"
        ),
        pytest.param(f"{_WIN} {_CK} Safari/537.36 Edg/156.0.0.0", False, id="edge-no-chrome"),
        pytest.param(
            f"{_WIN} {_CK} Chrome/156.0.0.0 Safari/537.36 Edg/156.0.3485.54", False, id="edge-full"
        ),
        pytest.param(
            f"{_WEBVIEW} {_CK} Version/4.0 Chrome/156.0.0.0 Mobile Safari/537.36",
            True,
            id="webview",
        ),
        pytest.param(
            f"{_WEBVIEW} {_CK} Version/4.0 Chrome/156.0.0.0 Mobile Safari/537.36 Edg/156.0.0.0",
            False,
            id="webview-edge",
        ),
        pytest.param(
            f"{_WIN} {_CK} Version/4.0 Chrome/156.0.0.0 Safari/537.36",
            False,
            id="webview-token-win",
        ),
        # Firefox
        pytest.param(
            f"Mozilla/5.0 (X11; Linux x86_64; rv:158.0) {_FF}/158.0", True, id="firefox-linux"
        ),
        pytest.param(
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:158.0) {_FF}/158.0",
            True,
            id="firefox-mac",
        ),
        pytest.param(
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7; rv:158.0) {_FF}/158.0",
            False,
            id="firefox-chrome-mac-token",
        ),
        pytest.param(
            f"Mozilla/5.0 (garbage; rv:158.0) {_FF}/158.0", False, id="firefox-bad-platform"
        ),
        pytest.param(
            f"Mozilla/5.0 (X11; Linux x86_64; rv:153.0) {_FF}/153.01", False, id="firefox-overmatch"
        ),
        pytest.param(
            f"Mozilla/5.0 (X11; Linux x86_64; rv:153.0) {_FF}/158.0",
            False,
            id="firefox-rv-mismatch",
        ),
        pytest.param(
            "Mozilla/5.0 (X11; Linux x86_64; rv:158.0) Gecko/158.0 Firefox/158.0",
            False,
            id="firefox-android-gecko",
        ),
        pytest.param(
            f"Mozilla/5.0 (X11; Linux x86_64; rv:158.0) {_FF}/158.0 Chrome/156.0.0.0 Safari/537.36",
            False,
            id="firefox-chrome-mix",
        ),
    ],
)
def test_browser_shape_check(ua, valid):
    assert (gen.shape_errors([ua]) == []) is valid


def test_generated_strings_match_real_browser_shapes(fixed_versions):
    assert gen.shape_errors(gen.flatten(gen.build())) == []


# --- Feeds ----------------------------------------------------------------------------


def test_chrome_ignores_early_rollouts(monkeypatch):
    # Oct 2026: 157 served to 0.5% of Windows users, 156 to everyone
    releases = {
        "win": [("156.0.8078.48", 1), ("157.0.8103.15", 0.005), ("156.0.8078.49", 0.005)],
        "mac": [("156.0.8078.48", 1)],
        "linux": [("156.0.8078.47", 1)],
    }

    def feed(url):
        platform = url.split("/platforms/")[1].split("/")[0]
        return {"releases": [{"version": v, "fraction": f} for v, f in releases[platform]]}

    monkeypatch.setattr(gen, "get_json", feed)
    assert gen.chrome_majors() == [155, 156]


def test_unrecognized_edge_feed_fails_clearly(monkeypatch):
    monkeypatch.setattr(gen, "get_json", lambda url: [{"Product": "Beta", "Releases": []}])
    with pytest.raises(RuntimeError, match="no stable Windows release"):
        gen.edge_major()


# --- Writing --------------------------------------------------------------------------


def test_main_writes_a_changed_list_and_keeps_formats_verified(fixed_versions, output, capsys):
    output.write_text(json.dumps({"formats_verified": "2026-01-02", "agents": {}}))
    assert gen.main() == 0
    data = json.loads(output.read_text())
    assert data["agents"] == gen.build()
    assert data["generated"] == datetime.date.today().isoformat()
    assert data["formats_verified"] == "2026-01-02"  # only research moves it
    assert "research online" in capsys.readouterr().out


def test_main_never_stamps_an_unresearched_list(fixed_versions, output):
    # A missing or corrupt file must not get formats_verified = today
    output.write_text("not json")
    assert gen.main() == 0
    assert json.loads(output.read_text())["formats_verified"] is None


def test_main_leaves_an_unchanged_file_alone(fixed_versions, output):
    data = {
        "generated": "2020-01-01",
        "formats_verified": "2020-01-01",
        "agents": gen.build(),
    }
    output.write_text(json.dumps(data))
    before = output.read_text()
    assert gen.main() == 0
    assert output.read_text() == before


def test_main_refuses_to_write_broken_templates(fixed_versions, output, monkeypatch):
    monkeypatch.setattr(gen, "SAFARI_VERSION", "27")
    assert gen.main() == 2
    assert not output.exists()


# --- Live -----------------------------------------------------------------------------


@pytest.mark.integration
def test_live_feeds_produce_valid_strings():
    assert gen.shape_errors(gen.flatten(gen.build())) == []
