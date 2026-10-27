"""The shipped user agent list and scripts/update_user_agents.py, which generates it."""

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


def generated(groups):
    return [agent for _, agents in groups for agent in agents]


# --- The shipped list -----------------------------------------------------------------


def test_shipped_list_is_well_formed():
    assert len(USER_AGENTS) == len(set(USER_AGENTS))
    assert all(a.startswith("Mozilla/5.0 (") for a in USER_AGENTS)
    for browser in ("Chrome/", "Firefox/", "Edg/", "Version/"):
        assert any(browser in a for a in USER_AGENTS), browser


def test_shipped_list_comes_from_the_generator(fixed_versions):
    # Hand edits that break a template show up as a format the generator never makes
    templates = {gen.shape(a) for a in generated(gen.build())}
    assert {gen.shape(a) for a in USER_AGENTS} == templates


def test_shipped_list_is_recent():
    match = re.search(r"\((\d{4}-\d{2}-\d{2})\)", _user_agents.__doc__ or "")
    assert match, "missing the generation date; run scripts/update_user_agents.py"
    age = datetime.date.today() - datetime.date.fromisoformat(match.group(1))
    assert age.days <= MAX_AGE_DAYS, (
        f"user agents are {age.days} days old; run scripts/update_user_agents.py"
    )


# --- Format check ---------------------------------------------------------------------


def test_shape_masks_major_versions_only():
    chrome = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    assert gen.shape(chrome + "Chrome/157.0.0.0 Safari/537.36").endswith(
        "Chrome/N.0.0.0 Safari/537.36"
    )
    firefox = "Mozilla/5.0 (X11; Linux x86_64; rv:158.0) Gecko/20100101 Firefox/158.0"
    assert gen.shape(firefox) == (
        "Mozilla/5.0 (X11; Linux x86_64; rv:N.0) Gecko/20100101 Firefox/N.0"
    )
    assert "Version/N Safari" in gen.shape("... Version/18.5 Safari/605.1.15")


def test_format_change_is_a_warning(fixed_versions):
    ours = generated(gen.build())
    # Chrome stops freezing its minor version: our template no longer matches
    reference = [a.replace(".0.0.0", ".1.2.3") for a in ours]
    warnings, _ = gen.check_format(ours, reference)
    assert warnings and all("no longer seen" in w for w in warnings)


def test_extra_reference_formats_are_only_notices(fixed_versions):
    ours = generated(gen.build())
    opera = "Mozilla/5.0 (Windows NT 10.0) Chrome/150.0.0.0 Safari/537.36 OPR/120.0.0.0"
    warnings, notices = gen.check_format(ours, [*ours, opera])
    assert warnings == []
    assert any("don't generate" in n for n in notices)


def test_newer_safari_is_a_notice(fixed_versions, monkeypatch):
    ours = generated(gen.build())
    newer = [a.replace(f"Version/{gen.SAFARI_VERSION} ", "Version/99 ") for a in ours]
    warnings, notices = gen.check_format(ours, newer)
    assert warnings == []
    assert any("Safari 99" in n for n in notices)


# --- Writing --------------------------------------------------------------------------


@pytest.fixture
def output(tmp_path, monkeypatch):
    path = tmp_path / "_user_agents.py"
    monkeypatch.setattr(gen, "OUTPUT", path)
    return path


def test_render_round_trips(fixed_versions, output):
    groups = gen.build()
    output.write_text(gen.render(groups))
    assert gen.current_agents() == generated(groups)


def test_main_writes_a_changed_list(fixed_versions, output, monkeypatch):
    monkeypatch.setattr(gen, "get_json", lambda url: generated(gen.build()))
    assert gen.main() == 0
    assert gen.current_agents() == generated(gen.build())


def test_main_leaves_an_unchanged_file_alone(fixed_versions, output, monkeypatch):
    output.write_text(gen.render(gen.build()).replace("(20", "(19"))  # an old date
    before = output.read_text()
    monkeypatch.setattr(gen, "get_json", lambda url: generated(gen.build()))
    assert gen.main() == 0
    assert output.read_text() == before


def test_main_refuses_to_write_on_a_format_change(fixed_versions, output, monkeypatch):
    drifted = [a.replace("Gecko/20100101", "Gecko/20300101") for a in generated(gen.build())]
    monkeypatch.setattr(gen, "get_json", lambda url: drifted)
    assert gen.main() == 2
    assert not output.exists()


def test_main_still_writes_when_the_reference_is_down(fixed_versions, output, monkeypatch):
    def down(url):
        raise OSError("unreachable")

    monkeypatch.setattr(gen, "get_json", down)
    assert gen.main() == 0
    assert output.exists()


# --- Live -----------------------------------------------------------------------------


@pytest.mark.integration
def test_live_feeds_produce_a_current_list():
    agents = generated(gen.build())
    reference = [a for a in gen.get_json(gen.REFERENCE_LIST) if isinstance(a, str)]
    warnings, _ = gen.check_format(agents, reference)
    assert warnings == []
