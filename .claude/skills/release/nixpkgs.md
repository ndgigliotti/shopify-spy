# Opening our own nixpkgs bump PR

Every rule nixpkgs applies to a `python3Packages.shopify-spy` version bump, with
the steps that meet them. Reviewers enforce these: our 0.5.0 bump
(NixOS/nixpkgs#593397) got a changes-requested review for dropping the PR
template, and its first description broke the AI policy.

The rules come from nixpkgs master and change over time. Before each PR, re-read
the [automation/AI policy](https://github.com/NixOS/nixpkgs/blob/master/CONTRIBUTING.md#automationai-policy)
and fetch the current PR template (step 5); if either disagrees with this file,
follow them and update this file.

## The AI policy

It covers commits, commit messages, PR descriptions, reviews and GitHub comments.

- **No LLM prose** in PR descriptions, comments, reviews, or commit bodies after
  the first line. This includes LLM output the user edits ("verbatim or
  edited"), so never draft sentences for the user to tweak; give facts as notes.
  Exempt: the commit subject, trailers (`Changelog:`, `Assisted-by:`), quotes
  of upstream text such as our CHANGELOG, and the template's own text.
- **Disclosure in the commit:** an `Assisted-by:` trailer with the tool and the
  primary model and version, such as `Assisted-by: Claude Code (Claude Opus 5.5)`.
  `Co-authored-by:` doesn't satisfy it.
- **Disclosure in the PR,** separately from the commit: a line in the user's
  summary, such as "This change has been prepared with Claude Code."
- **A responsible person:** the user reviews the diff and must be able to answer
  questions about it themselves. Show them the full diff and the facts behind
  each line.
- **Replies are the user's own words,** posted as written, never relayed from you.
- Exempt: the r-ryantm bot and `nix-update`, and research, testing and private
  review that don't end up in the text (such as a subagent checking the PR).

## Steps

1. **Branch.** In the user's nixpkgs checkout, from current master:
   `shopify-spy-X.Y.Z`, pushed to their fork. The base is `master`; a bump
   rebuilds a handful of packages, far below the staging thresholds.
2. **Change** `pkgs/development/python-modules/shopify-spy/default.nix`:
   - `version` and `hash` (`nix flake prefetch github:ndgigliotti/shopify-spy/vX.Y.Z`
     gives the hash `fetchFromGitHub` expects; a build with a wrong hash also
     reports it).
   - `dependencies` matching `[project] dependencies` in `pyproject.toml` at the
     tag, as unversioned attributes; an extra on a dependency is
     `++ <pkg>.optional-dependencies.<extra>` (as `httpx[socks]` is).
   - `optional-dependencies` only for extras our metadata defines
     (`excel = [ xlsxwriter ]`), `build-system` matching `[build-system]`, and
     test-only inputs in `nativeCheckInputs`.
   - A disabled test needs a comment saying why skipping it is safe.
   - Don't add `overrideAttrs` or `overridePythonAttrs` anywhere new (the CLI
     wrapper's existing one predates this rule).
   - Format with nixfmt from the checkout (`nix fmt` or `treefmt`).
3. **Build** with tests: `nix-build -A python3Packages.shopify-spy -A shopify-spy`,
   plus `python312Packages.shopify-spy` and any other Python nixpkgs ships that we
   support (nixpkgs-review builds only the default ones). Run
   `shopify-spy --version` and `spy --version` from the result.
4. **Commit:** one commit (squash any fixups) with the subject
   `python3Packages.shopify-spy: OLD -> X.Y.Z`, no trailing period, and a body of
   trailers only:

   ```
   Changelog: https://github.com/ndgigliotti/shopify-spy/blob/vX.Y.Z/CHANGELOG.md
   Assisted-by: Claude Code (<model name and version>)
   ```

   An update that changes dependencies "requires more verbose commit messages";
   any line explaining it is the user's to write, above the trailers.
5. **PR body.** Write the current template to a file for the user:

   ```bash
   gh api repos/NixOS/nixpkgs/contents/.github/PULL_REQUEST_TEMPLATE.md \
     -H 'Accept: application/vnd.github.raw' > ~/Projects/nixpkgs-pr-body.md
   ```

   Keep every heading and the template itself; only add to it:
   - the changelog link on the first line;
   - in place of the summary comment, an HTML comment listing the facts to cover
     (the version, each dependency change and why, notable fixes) and a reminder
     to disclose the AI use;
   - ticks on the "Built on platform" and "Tested" boxes that are true. Leave the
     CONTRIBUTING and automation/AI policy boxes for the user; before they tick
     them, show them this file's rules.

   Ask the user to write the summary (`micro ~/Projects/nixpkgs-pr-body.md`,
   or `wl-copy` it to their clipboard) and to read the diff. Use their text as
   written.
6. **(ask)** Open the PR with their body, as a draft, titled like the commit.
   Run `nixpkgs-review pr N --no-shell --print-result` locally (it posts
   nothing), tick its box, and **(ask)** mark the PR ready. Check that CI passes
   (`gh pr checks N -R NixOS/nixpkgs`).
7. **Reviews.** Comments are non-blocking unless they come as "Request changes",
   but every suggestion should be acknowledged before merge, in the user's words.
   Re-request the review once it's addressed
   (`gh api -X POST repos/NixOS/nixpkgs/pulls/N/requested_reviewers -f 'reviewers[]=LOGIN'`).
   A committer merges it; the merge bot can't, since the change is in
   `python-modules`. Nudging reviewers on a stalled PR is expected.

## Checklist before marking it ready

From [CONTRIBUTING.md](https://github.com/NixOS/nixpkgs/blob/master/CONTRIBUTING.md),
[pkgs/README.md](https://github.com/NixOS/nixpkgs/blob/master/pkgs/README.md),
[maintainers/README.md](https://github.com/NixOS/nixpkgs/blob/master/maintainers/README.md)
and the [Python section](https://github.com/NixOS/nixpkgs/blob/master/doc/languages-frameworks/python.section.md)
of the manual:

- [ ] One commit; subject `python3Packages.shopify-spy: OLD -> X.Y.Z`, no period;
      changelog link; `Assisted-by:` trailer; no LLM prose in the body.
- [ ] PR title matches the commit; base `master`; branch name names the change.
- [ ] The full template, with the user's summary and disclosure, and only true
      boxes ticked.
- [ ] Dependencies, extras, build system and test inputs match `pyproject.toml`
      at the tag.
- [ ] `meta` unchanged unless upstream changed (license still MIT, `mainProgram`,
      maintainers).
- [ ] nixfmt clean; built with tests on every supported Python; both binaries run;
      nixpkgs-review passed; CI green.
- [ ] Every review comment acknowledged in the user's words.
