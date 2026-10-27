"""The shipped user agent list and scripts/update_user_agents.py, which updates it."""

import datetime
import importlib.util
import re
from pathlib import Path

import pytest

from shopify_spy import _user_agents
from shopify_spy._user_agents import USER_AGENTS

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "update_user_agents.py"
spec = importlib.util.spec_from_file_location("update_user_agents", SCRIPT)
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

MAX_AGE_DAYS = 120  # the monthly job keeps it well under this


@pytest.fixture
def fixed_versions(monkeypatch):
    """Generate without the network: every feed reports the same versions."""
    monkeypatch.setattr(gen, "chrome_majors", lambda platform: [150, 151])
    monkeypatch.setattr(gen, "firefox_majors", lambda: [140, 150])
    monkeypatch.setattr(gen, "edge_major", lambda: 151)


@pytest.fixture
def output(tmp_path, monkeypatch):
    path = tmp_path / "_user_agents.py"
    monkeypatch.setattr(gen, "OUTPUT", path)
    return path


def generated(groups):
    return [agent for _, agents in groups for agent in agents]


# --- The shipped list -----------------------------------------------------------------


def test_shipped_list_is_well_formed():
    assert len(USER_AGENTS) == len(set(USER_AGENTS))
    assert all(a.startswith("Mozilla/5.0 (") for a in USER_AGENTS)
    for browser in ("Chrome/", "Firefox/", "Edg/", "Version/"):
        assert any(browser in a for a in USER_AGENTS), browser


def test_shipped_list_version_tokens_are_consistent():
    # Same rules as master's test_user_agents_version_tokens_consistent
    assert gen.consistency_errors(USER_AGENTS) == []


def test_shipped_list_is_recent():
    match = re.search(r"\((\d{4}-\d{2}-\d{2})\)", _user_agents.__doc__ or "")
    assert match, "missing the generation date; run scripts/update_user_agents.py"
    age = datetime.date.today() - datetime.date.fromisoformat(match.group(1))
    assert age.days <= MAX_AGE_DAYS, (
        f"user agents are {age.days} days old; run scripts/update_user_agents.py"
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
    assert gen.consistency_errors(generated(gen.build())) == []


# --- Writing --------------------------------------------------------------------------


def test_render_round_trips(fixed_versions, output):
    groups = gen.build()
    output.write_text(gen.render(groups))
    assert gen.current_agents() == generated(groups)


def test_main_writes_a_changed_list_and_reminds_to_research(fixed_versions, output, capsys):
    assert gen.main() == 0
    assert gen.current_agents() == generated(gen.build())
    assert "research online" in capsys.readouterr().out


def test_main_leaves_an_unchanged_file_alone(fixed_versions, output):
    output.write_text(gen.render(gen.build()).replace("(20", "(19"))  # an old date
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
    agents = generated(gen.build())
    assert len(agents) == len(USER_AGENTS)
    assert gen.consistency_errors(agents) == []
