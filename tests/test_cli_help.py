"""Tests for --help: plain and complete when piped, Rich panels in a terminal (#75)."""

import inspect
import os
import re
import subprocess
import sys

import click
import pytest
import typer.core
import typer.main
import typer.rich_utils

import shopify_spy as spy
from shopify_spy import cli as cli_module
from shopify_spy._api import ScrapeOptions
from shopify_spy._config import Config
from shopify_spy.cli import app

from .conftest import runner, strip_ansi

BOX_CHARS = set("╭╮╰╯│─")


def _piped(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the CLI with stdout and stderr piped, under the env vars that force Rich."""
    env = {**os.environ, "FORCE_COLOR": "1", "GITHUB_ACTIONS": "true", "PY_COLORS": "1"}
    return subprocess.run(
        [sys.executable, "-m", "shopify_spy.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _scrape_command() -> click.Command:
    group = typer.main.get_command(app)
    assert isinstance(group, click.Group)
    return group.commands["scrape"]


def _assert_plain(text: str) -> None:
    assert not BOX_CHARS & set(text)
    assert "…" not in text
    assert "[bold]" not in text
    assert "\\[" not in text


@pytest.mark.parametrize("args", [["--help"], ["scrape", "--help"]])
def test_piped_help_is_plain_and_complete(args):
    result = _piped(*args)
    assert result.returncode == 0, result.stderr
    _assert_plain(result.stdout)
    assert "import shopify_spy as spy" in result.stdout
    if args == ["scrape", "--help"]:
        for param in _scrape_command().params:
            if param.help:
                assert param.help in result.stdout, param.name


def test_piped_scrape_help_content():
    out = _piped("scrape", "--help").stdout
    assert "shopify-spy[excel]" in out
    assert f"default: {Config().network.concurrent_requests}" in out
    assert out.count("Shopify only") == 2
    for text in (
        "spy scrape --peek",
        "spy scrape --limit 10",
        "<output>/data/",
        "<output>/status/",
        "<output>/images/full/",
        "Exit status: 0",
        "./shopify-spy.yaml",
        "~/.config/shopify-spy/config.yaml",
    ):
        assert text in out, text


def test_piped_help_lists_options_by_task():
    out = _piped("scrape", "--help").stdout
    headings = ["What to scrape:", "Output:", "Network:", "Run control:"]
    positions = [out.index(h) for h in headings]
    assert positions == sorted(positions)
    assert out.index("--url-file") < out.index("--platform") < out.index("--limit")
    assert out.index("--limit") < out.index("--output") < out.index("--concurrent")
    assert out.index("--ignore-robots") < out.index("--bail") < out.index("--quiet")


def test_terminal_help_renders_rich_panels(monkeypatch):
    monkeypatch.setattr(cli_module, "use_rich_help", lambda: True)
    monkeypatch.setattr(typer.rich_utils, "MAX_WIDTH", 200)
    result = runner.invoke(app, ["scrape", "--help"])
    assert result.exit_code == 0
    out = strip_ansi(result.stdout)
    panels = ["─ What to scrape ─", "─ Output ─", "─ Network ─", "─ Run control ─"]
    positions = [out.index(p) for p in panels]
    assert positions == sorted(positions)
    # Help text is plain, so Rich mode must escape it rather than drop [excel]
    assert "shopify-spy[excel]" in out


def test_terminal_help_without_rich_is_plain(monkeypatch):
    monkeypatch.setattr(cli_module, "use_rich_help", lambda: True)
    monkeypatch.setattr(typer.core, "HAS_RICH", False)
    result = runner.invoke(app, ["scrape", "--help"])
    assert result.exit_code == 0
    _assert_plain(result.stdout)
    assert "shopify-spy[excel]" in result.stdout


def test_help_defaults_come_from_config():
    defaults = Config()
    helps = {p.name: p.help or "" for p in _scrape_command().params}
    on_off = {True: "on", False: "off"}
    expected = {
        "platform": defaults.scrape.platform.value,
        "products": on_off[defaults.scrape.products],
        "collections": on_off[defaults.scrape.collections],
        "images": on_off[defaults.scrape.images],
        "output": f"./{defaults.output.dir.as_posix()}",
        "format": defaults.output.format,
        "concurrent": str(defaults.network.concurrent_requests),
        "max_stores": str(defaults.network.max_concurrent_stores),
        "throttle": on_off[defaults.throttle.enabled],
        "bail": str(defaults.scrape.bail),
    }
    for name, value in expected.items():
        assert f"default: {value}" in helps[name], name


def test_scrape_doc_defaults_come_from_scrape_options():
    doc = " ".join(inspect.getdoc(spy.scrape).split())
    fields = ScrapeOptions.model_fields
    for name in ("bail", "concurrent_requests", "request_timeout", "retries"):
        assert re.search(rf"\(default {int(fields[name].default)}[;)]", doc), name
    assert f"(default {fields['max_concurrent_stores'].default};" in doc
    assert f"(default {fields['images'].default})" in doc
    assert "images_dir: Directory for downloaded images" in doc


def test_h_is_a_help_alias():
    for args in (["-h"], ["scrape", "-h"], ["init", "-h"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, args
        assert "Usage:" in strip_ansi(result.stdout)


def test_piped_usage_error_is_plain():
    result = _piped("scrape", "--bogus")
    assert result.returncode == 2
    assert "No such option: --bogus" in result.stderr
    assert not BOX_CHARS & set(result.stderr)


@pytest.mark.parametrize("func", [spy.scrape, spy.scrape_async])
def test_docstring_sections_share_one_indent(func):
    doc = inspect.getdoc(func)
    assert "\nArgs:\n    urls: " in doc
    assert "\nFailures: " in doc
