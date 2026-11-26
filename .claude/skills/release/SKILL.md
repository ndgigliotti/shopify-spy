---
name: release
description: Publish a shopify-spy release from master to PyPI and GitHub (version check, tag, build, smoke test, uv publish, GitHub release, nixpkgs follow-up). Use when asked to release, publish or cut a version, after the PRs going into it are merged.
---

# Release shopify-spy

A release ships what is already on master. It never starts from an open PR.

Confirm with the user before each step marked **(ask)**: they are public and can't
be undone.

## 1. Check master

- `git fetch origin`, then work on an up-to-date, clean `master` checkout.
- The latest CI run on master passed: `gh run list --branch master --limit 1`.
- The live-store tests pass from this machine: `uv run pytest -m integration`.
  CI doesn't run them, because Shopify rate-limits GitHub's runner IPs (429 on
  every run from 2026-11-14), whatever the code does. A `rate_limited` failure
  here means wait a few minutes and retry, not a bad build.
- The version X.Y.Z is the same in `pyproject.toml` and in the project's
  `version =` line in `uv.lock` (under `name = "shopify-spy"`).
- `CHANGELOG.md` starts with a `## X.Y.Z` section describing what's on master.
- X.Y.Z has no tag yet (`git tag -l vX.Y.Z`) and isn't on PyPI
  (`curl -s https://pypi.org/pypi/shopify-spy/X.Y.Z/json` is a 404).

If the version or CHANGELOG needs updating, do it in a PR (master is protected):
the last commit of the release's PR, or a small PR of its own. Release once it's
merged and master's CI has passed.

## 2. Tag

- **(ask)** `git tag -a vX.Y.Z -m vX.Y.Z` on master's head (the merge commit),
  then `git push origin vX.Y.Z`. Tagging first means the tag exists before
  anything is published.

## 3. Build and smoke-test

- `rm -rf dist && uv build -q --no-config`. Without `--no-config`, `uv build`
  (like `uv run`, `uv sync` and `uv lock`) can rewrite `uv.lock`: check
  `git status` afterwards and `git checkout uv.lock` if it changed.
- Install the wheel into a fresh venv, **with the working directory outside the
  repo** (0.1.0–0.2.2 shipped broken because a file in the checkout hid a
  packaging bug), and check:
  - `shopify-spy --version` prints X.Y.Z
  - `shopify-spy scrape --peek <a Shopify store>` prints one item (a 429 or
    other store failure means retry or pick another store, not a bad build)
  - `python -c "import shopify_spy as spy; print(spy.scrape('<store>').first()['store'])"`

## 4. Publish

- **(ask)** `uv publish dist/*`. `UV_PUBLISH_TOKEN` is set in the user's
  environment and needs no flags; don't print it.
- Check PyPI has exactly these files: the `digests.sha256` of each file in
  `https://pypi.org/pypi/shopify-spy/X.Y.Z/json` matches `sha256sum dist/*`.
  Use the version-specific URL; `/pypi/shopify-spy/json` is cached and can lag
  a minute.
- **(ask)** GitHub release, no assets attached:
  `gh release create vX.Y.Z --verify-tag --title "vX.Y.Z - <short summary>" --notes-file <file>`,
  with the CHANGELOG's `## X.Y.Z` section as the notes. Titles follow earlier
  releases (`gh release list`).

## 5. Verify

- Install the release from PyPI in an empty directory outside the repo. The
  user's global uv config has a 7-day cooldown (`exclude-newer = "7 days"` in
  `~/.config/uv/uv.toml`), so exempt this package:
  `uvx --exclude-newer-package shopify-spy=false --refresh-package shopify-spy shopify-spy@X.Y.Z --version`
- `gh release view vX.Y.Z` shows the notes.

Report the PyPI and GitHub release links now; step 6 can take days.

## 6. Nixpkgs

The nixpkgs package (`pkgs/development/python-modules/shopify-spy/default.nix`,
with a CLI wrapper in `pkgs/by-name/sh/shopify-spy/package.nix`) lists its
dependencies by hand. The nixpkgs update bot (r-ryantm) runs the default
`nix-update` script: it bumps only the version and hash, builds the package with
our tests, and opens a PR only if that build passes. A failed build leaves
nothing but its log. Bump PRs touch `python-modules`, so the maintainer merge bot
can't merge them; a nixpkgs committer has to.

- Diff the dependencies since the last release:
  `git diff vPREV vX.Y.Z -- pyproject.toml` (`dependencies`, optional
  dependencies, the test group, `[build-system]`, and extras on a dependency like
  `httpx[socks]`). A new top-level dependency or a raised bound fails nixpkgs'
  runtime-deps check; a new extra on an existing dependency isn't checked, so
  only our tests catch it.
- No changes: wait for the bot's PR
  (`gh pr list -R NixOS/nixpkgs --search shopify-spy`) and **(ask)** approve it
  as maintainer. If none appears within a few days, read the newest log under
  https://nixpkgs-update-logs.nixos.org/python3Packages.shopify-spy/
- Changes: **(ask)** open the bump PR yourself with the dependency changes in
  `default.nix`. It merges when a committer reviews it. If the bot's PR for the
  same version is already open (an extra-only change can pass its build), don't
  approve it; **(ask)** comment there linking yours so a committer closes it.
- Close any shopify-spy issue tracking a nixpkgs change (like #71) once its PR
  merges.

Report the nixpkgs PR link once there is one.
