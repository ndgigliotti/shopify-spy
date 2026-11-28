# Opening our own nixpkgs bump PR

These are the nixpkgs rules that apply to a `python3Packages.shopify-spy` version
bump, and the steps that meet them. Reviewers enforce them: our 0.5.0 bump
(NixOS/nixpkgs#593397) got a changes-requested review for dropping the PR
template, and its first description broke the AI policy.

The rules live on nixpkgs master and change over time. Before each PR, re-read
the [automation/AI policy](https://github.com/NixOS/nixpkgs/blob/master/CONTRIBUTING.md#automationai-policy)
and fetch the current PR template (step 5). If either disagrees with this file,
follow them and update this file.

## The AI policy

It covers commits, commit messages, PR descriptions, reviews and GitHub comments.

- **No LLM prose** in PR descriptions, comments, reviews, or commit bodies after
  the first line. That includes LLM output the user then edits ("verbatim or
  edited"), so never draft a sentence for the user to adjust; give them facts as
  terminal notes instead. Exempt: the commit subject, trailers (`Changelog:`,
  `Assisted-by:`), the template's own text, and quotes of upstream text (such as
  our CHANGELOG) that the user selects.
- **Disclosure in the commit:** an `Assisted-by:` trailer with the tool and the
  primary model and its version, such as `Assisted-by: Claude Code (Claude Opus 5.5)`.
  `Co-authored-by:` doesn't count.
- **Disclosure in the PR,** separately from the commit: the user's summary has to
  say the change was prepared with Claude Code, in their words.
- **A responsible person:** the user reviews the diff and must be able to answer
  questions about it on their own. Show them the full diff and the facts behind
  each line.
- **Every comment and reply is the user's own words,** posted as they wrote it,
  never relayed from you. That includes nudges on a stalled PR.
- **Exempt:** the r-ryantm bot and `nix-update`, and research, testing and private
  review whose output doesn't end up in the text (such as a subagent checking the
  PR).

## Steps

1. **Branch.** In the user's nixpkgs checkout, `git fetch origin` (NixOS/nixpkgs)
   and branch `shopify-spy-X.Y.Z` from `origin/master`, not the fork's possibly
   stale master. The base is `master`: a bump rebuilds a handful of packages, far
   below the staging thresholds.
2. **Change** `pkgs/development/python-modules/shopify-spy/default.nix`:
   - `version` and `hash`. `nix flake prefetch github:ndgigliotti/shopify-spy/vX.Y.Z`
     gives the hash `fetchFromGitHub` expects, and a build with a wrong hash
     reports the right one.
   - `dependencies` match `[project] dependencies` in `pyproject.toml` at the tag,
     as unversioned attributes. An extra on a dependency is
     `++ <pkg>.optional-dependencies.<extra>` for `pkg[extra]`.
   - If nixpkgs ships a dependency older than our lower bound, the runtime-deps
     check fails. Use `pythonRelaxDeps` with a comment saying why it's safe, not an
     override, or raise it with the user if the older version really won't work.
   - `optional-dependencies` hold only extras our metadata defines
     (`excel = [ xlsxwriter ]`). `build-system` matches `[build-system]`, and
     test-only inputs go in `nativeCheckInputs`. Keep `pythonImportsCheck`.
   - Every special packaging choice needs a comment saying why: a disabled test
     (and why skipping it is safe), a relaxed dependency, a `substituteInPlace` or
     a non-default flag.
   - Don't add `overrideAttrs` or `overridePythonAttrs` anywhere. The CLI wrapper's
     existing one was accepted at init, so don't extend it either.
   - If a new extra should ship with the CLI, the wrapper
     `pkgs/by-name/sh/shopify-spy/package.nix` changes too.
   - Format with nixfmt from the checkout (`nix fmt` or `treefmt`).
3. **Build** with tests: `nix-build -A python3Packages.shopify-spy -A python313Packages.shopify-spy -A shopify-spy`.
   `python3` is the newest Python (3.14 at the time of writing), and nixpkgs-review
   builds only the Python sets Hydra caches (3.13 and 3.14). `python311Packages` and
   `python312Packages` aren't in the binary cache, so building them compiles the
   whole dependency tree; that's optional. Run `shopify-spy --version` and
   `spy --version` from the result.
4. **Commit:** one commit (squash any fixups). The subject is
   `python3Packages.shopify-spy: OLD -> X.Y.Z`, with no trailing period, and the
   body is trailers only:

   ```
   Changelog: https://github.com/ndgigliotti/shopify-spy/blob/vX.Y.Z/CHANGELOG.md
   Assisted-by: Claude Code (<model name and version>)
   ```

   "Package upgrades with more extensive changes require more verbose commit
   messages." For a dependency change, ask the user whether they want to add a
   line of their own above the trailers.
5. **PR body.** Write the current template to a file:

   ```bash
   gh api repos/NixOS/nixpkgs/contents/.github/PULL_REQUEST_TEMPLATE.md \
     -H 'Accept: application/vnd.github.raw' > ~/Projects/nixpkgs-pr-body.md
   ```

   Keep all of the template, its comments and headings included. Add only:
   - the changelog link on the first line, above the template's opening comment
     ("^ Please summarise ... here ^");
   - ticks on the "Built on platform" and "Tested" boxes that are true. Leave the
     CONTRIBUTING and automation/AI policy boxes for the user. Before they tick
     them, show them this file's rules.

   Then give the user, in the terminal and not in the file, the facts their
   summary should cover: the version, each dependency change and why, notable
   fixes, and that it must disclose Claude Code. Ask them to write the summary
   between the changelog link and the template's comment
   (`micro ~/Projects/nixpkgs-pr-body.md`) and to read the diff.
6. **Check and open.** Read the file back. The summary must be the user's text,
   with nothing of yours in it besides the link and the ticks. Use it exactly as
   written. Then:
   - **(ask)** push the branch to the user's fork and open the PR with that body,
     as a draft, titled like the commit;
   - run `nixpkgs-review pr N --no-shell --print-result` locally (it posts
     nothing), tick its box in the body, and update the PR;
   - check that CI passes (`gh pr checks N -R NixOS/nixpkgs`), then **(ask)** mark
     it ready.
7. **Reviews.** Comments don't block unless they come as "Request changes", but
   every suggestion should be acknowledged before merge, in the user's words.
   Once one is addressed, re-request the review
   (`gh api -X POST repos/NixOS/nixpkgs/pulls/N/requested_reviewers -f 'reviewers[]=LOGIN'`).
   A committer merges the PR; the merge bot can't, since the change is in
   `python-modules`.

## Checklist before marking it ready

These come from [CONTRIBUTING.md](https://github.com/NixOS/nixpkgs/blob/master/CONTRIBUTING.md),
[pkgs/README.md](https://github.com/NixOS/nixpkgs/blob/master/pkgs/README.md),
[maintainers/README.md](https://github.com/NixOS/nixpkgs/blob/master/maintainers/README.md)
and the [Python section](https://github.com/NixOS/nixpkgs/blob/master/doc/languages-frameworks/python.section.md)
of the manual.

- [ ] One commit. Its subject is `python3Packages.shopify-spy: OLD -> X.Y.Z` with
      no period; it has the changelog link and the `Assisted-by:` trailer, and its
      body has no LLM prose.
- [ ] The PR title matches the commit, the base is `master`, and the branch name
      names the change.
- [ ] The whole template is there, with the user's summary and disclosure, and
      only true boxes are ticked.
- [ ] Dependencies, extras, the build system and test inputs match `pyproject.toml`
      at the tag, and every special packaging choice has a why-comment.
- [ ] `meta` is unchanged unless upstream changed: the license is still MIT, and
      `mainProgram` and the maintainers stay as they are.
- [ ] A major or breaking update has an entry in `doc/release-notes` and the
      template's "Package update" box ticked.
- [ ] nixfmt is clean, the package built with tests on 3.13 and 3.14, both
      binaries run, nixpkgs-review passed, and CI is green.
- [ ] Every review comment has been acknowledged, in the user's words.
