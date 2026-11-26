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
- The README's links to `docs/` name the tag `vX.Y.Z`
  (`.../shopify-spy/blob/vX.Y.Z/docs/...`), so the PyPI page opens this
  release's docs. `tests/test_docs.py` fails until they match `pyproject.toml`.
- `CHANGELOG.md` starts with a `## X.Y.Z` section describing what's on master.
- X.Y.Z has no tag yet (`git tag -l vX.Y.Z`) and isn't on PyPI
  (`curl -s https://pypi.org/pypi/shopify-spy/X.Y.Z/json` is a 404).

If the version, the docs links or the CHANGELOG need updating, do it in a PR (master is protected):
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
- Changes: open the bump PR yourself with the dependency changes in
  `default.nix`, following "Your own bump PR" below. It merges when a committer
  reviews it. If the bot's PR for the same version is already open (an
  extra-only change can pass its build), don't approve it; **(ask)** comment
  there linking yours so a committer closes it.
- Close any shopify-spy issue tracking a nixpkgs change (like #71) once its PR
  merges.

### Your own bump PR

Nixpkgs' [automation/AI policy](https://github.com/NixOS/nixpkgs/blob/master/CONTRIBUTING.md#automationai-policy)
bans LLM prose in PR descriptions, comments, reviews and commit bodies, and
requires AI use to be disclosed. Its reviewers enforce it, and they also expect
the PR template kept intact. So you prepare the change and the facts, and the
user writes every sentence that nixpkgs reads.

- **Branch:** a `shopify-spy-X.Y.Z` branch in the user's nixpkgs checkout, from
  current master, pushed to their fork.
- **Change:** version, hash (`nix flake prefetch github:ndgigliotti/shopify-spy/vX.Y.Z`,
  or let a build report it) and the dependency changes, formatted with nixfmt.
- **Build:** `nix-build -A python3Packages.shopify-spy -A shopify-spy`, plus
  `python312Packages.shopify-spy` and every other Python nixpkgs ships that
  we support, since nixpkgs-review covers only the default ones. Then run
  `shopify-spy --version` from the result.
- **Commit:** the subject `python3Packages.shopify-spy: OLD -> X.Y.Z` and a body
  of trailers only:

  ```
  Changelog: https://github.com/ndgigliotti/shopify-spy/blob/vX.Y.Z/CHANGELOG.md
  Assisted-by: Claude Code (<model name and version>)
  ```

  The policy exempts the subject and trailers; any explanation in the body is
  the user's to write. `Co-authored-by:` doesn't count as disclosure.
- **PR body:** write the current template to a file for the user to edit:
  `gh api repos/NixOS/nixpkgs/contents/.github/PULL_REQUEST_TEMPLATE.md -H 'Accept: application/vnd.github.raw' > ~/Projects/nixpkgs-pr-body.md`.
  Fill in only what isn't prose:
  - the changelog link on the first line;
  - in place of the template's summary comment, an HTML comment listing the facts
    to cover (the version, each dependency change and why, notable fixes) and
    a reminder to disclose the AI use (for example, "This change has been
    prepared with Claude Code"). Facts as notes, not sentences: the policy
    covers LLM output "verbatim or edited", so never draft the summary;
  - the "Built on platform" and "Tested" boxes that are true. Leave the
    CONTRIBUTING and automation/AI policy boxes for the user to tick.

  Ask the user to write the summary (`micro ~/Projects/nixpkgs-pr-body.md`;
  `wl-copy` it if they ask) and to read the diff, which they're accountable for.
  Use their text as written.
- **(ask)** Open the PR with their body, as a draft. Run
  `nixpkgs-review pr N --no-shell --print-result` locally (it posts nothing),
  tick its box in the body, and **(ask)** mark the PR ready.
- **Reviews:** every reply on nixpkgs is the user's own words; post them as
  written. Re-request a review after addressing it.

Report the nixpkgs PR link once there is one.
