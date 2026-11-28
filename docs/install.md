# Installation

Shopify Spy needs Python 3.11 or later. Install it as a command-line tool, as a library in the environment your scripts run in, or both.

## As a command-line tool

[uv tool](https://docs.astral.sh/uv/) installs CLI tools in isolated environments, so they won't conflict with other Python projects:

```bash
uv tool install shopify-spy

# Or run it once without installing
uvx shopify-spy scrape --peek https://www.example.com
```

[pipx](https://pipx.pypa.io/) works the same way: `pipx install shopify-spy`.

Installing provides two identical commands, `spy` and `shopify-spy`; the docs use `spy`. With `uvx`, name the package: `uvx shopify-spy ...` or `uvx --from shopify-spy spy ...`. `uvx spy` would fetch an unrelated package named `spy`.

## As a library

To use it as a Python library (`import shopify_spy`), the package has to be in the environment the script runs in. A `pipx` or `uv tool` install only provides the command.

```bash
# In a project (this also installs the CLI)
uv add shopify-spy

# A one-off script, no project or virtual environment needed
uv run --with 'shopify-spy>=0.5' python script.py
```

A script can also declare the dependency itself ([inline script metadata](https://packaging.python.org/en/latest/specifications/inline-script-metadata/)), so `uv run script.py` works from any directory:

```python
# /// script
# dependencies = ["shopify-spy>=0.5"]
# ///
import shopify_spy as spy
```

### With pip

Without uv, install it into a virtual environment. While the environment is active, scripts can import it and the `spy` command works too.

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install shopify-spy
```

## Excel support

Excel output (`--format xlsx`, or an `.xlsx` path in the library) needs the `excel` extra:

```bash
uv tool install 'shopify-spy[excel]'
uv add 'shopify-spy[excel]'
pipx install 'shopify-spy[excel]'
pip install 'shopify-spy[excel]'
```

## Browser profiles

[Browser profiles](cli.md#browser-profiles) come from [curl_cffi](https://github.com/lexiforest/curl_cffi), a dependency that every install includes; no extra is needed. It bundles a compiled libcurl, built by its maintainers, that sends browsers' TLS handshakes. Its wheels cover Linux (glibc and musl), macOS and Windows on x86-64 and ARM64, among others. Where no wheel fits, the installer builds it from source, and if that fails, Shopify Spy can't be installed there: `--user-agent` also goes through curl_cffi.

## Nix

The repository is a flake. Its build runs the test suite.

```bash
# Run the CLI once
nix run github:ndgigliotti/shopify-spy -- scrape --peek https://www.example.com

# Install it into your profile, which adds spy and shopify-spy
nix profile add github:ndgigliotti/shopify-spy
spy scrape --peek https://www.example.com

# A shell whose Python can import shopify_spy
nix develop github:ndgigliotti/shopify-spy
```

```

The CLI and the dev shell include Excel support, so `--format xlsx` works without the `excel` extra. In your own flake, `overlays.default` adds `python3Packages.shopify-spy` (for every Python version) and a top-level `shopify-spy`. As in nixpkgs, the library package leaves Excel support out; add it with `python3.withPackages (ps: [ ps.shopify-spy ] ++ ps.shopify-spy.optional-dependencies.excel)`.
