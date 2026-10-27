"""The shipped user agent list and scripts/update_user_agents.py, which updates it."""

import datetime
import importlib.util
import json
from pathlib import Path

import pytest

from shopify_spy._user_agents import _DATA as SHIPPED
from shopify_spy._user_agents import USER_AGENTS

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "update_user_agents.py"
spec = importlib.util.spec_from_file_location("update_user_agents", SCRIPT)
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

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


def flat(agents):
    return [agent for group in agents.values() for agent in group]


def age_days(iso_date):
    return (datetime.date.today() - datetime.date.fromisoformat(iso_date)).days


# --- The shipped list -----------------------------------------------------------------


def test_shipped_list_is_well_formed():
    assert len(USER_AGENTS) == len(set(USER_AGENTS))
    assert all(a.startswith("Mozilla/5.0 (") for a in USER_AGENTS)
    for browser in ("Chrome/", "Firefox/", "Edg/", "Version/"):
        assert any(browser in a for a in USER_AGENTS), browser


def test_shipped_list_version_tokens_are_consistent():
    assert gen.consistency_errors(USER_AGENTS) == []


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


# --- Version-token rules --------------------------------------------------------------


@pytest.mark.parametrize(
    "agent",
    [
        "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Version/27 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; rv:153.0) Gecko/20100101 Firefox/158.0",
        "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/156.0.0.0 Safari/537.36 "
        "Edg/155.0.0.0",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/156.0.7600.12 Safari/537.36",
    ],
    ids=["safari-bare-major", "firefox-rv-mismatch", "edge-chrome-mismatch", "chrome-unreduced"],
)
def test_inconsistent_version_tokens_are_caught(agent):
    assert gen.consistency_errors([agent])


def test_safari_patch_versions_are_valid():
    # Real Safari sends these too (useragents.me, 2026-10)
    safari = "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Version/{} Safari/605.1.15"
    assert gen.consistency_errors([safari.format("26.6.1"), safari.format("26.0")]) == []


def test_generated_strings_follow_the_rules(fixed_versions):
    assert gen.consistency_errors(flat(gen.build())) == []


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
    data = {"generated": "2020-01-01", "formats_verified": "2020-01-01", "agents": gen.build()}
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
    assert gen.consistency_errors(flat(gen.build())) == []
