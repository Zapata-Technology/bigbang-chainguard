# Contributing

Changes are welcome, and if you run this for your own Chainguard org, please send
your improvements back so everyone's Big Bang conversion gets better.

- **Fixes and new mappings:** open a pull request against `main`. New Big Bang
  packages, renamed Chainguard images, value paths the generator missed, and
  corrections to `stays` reasons are especially useful.
- **Problems:** open an issue with the Big Bang version (`BIGBANG_VERSION`), the
  package, and the output of `bb-convert.py` or `bb-verify.py`.

## Making a change

1. Edit `mapping.yaml`, `scripts/` or `builds/*/Dockerfile`. Never hand-edit the
   generated files (`overlays/`, `flux/`, `flux-fips/`, `docs/images.md`,
   `docs/required-images.md`, `builds/*/build.yaml`).
2. Regenerate and verify (`scripts/bb-run.py`, or the steps in the README), then run
   `python scripts/check.py --online`: unit tests, chart scan, overlays current, and both
   `bb-verify.py` runs (standard and `--fips`) must pass.
   [AGENTS.md](AGENTS.md) has the rules and step-by-step playbooks (written for AI agents,
   useful for anyone).
3. Keep org-specific choices out of the shared files: the org belongs in
   `CHAINGUARD_ORG`, and exceptions for one org belong in `orgs/<org>.yaml`.
4. Never commit pull tokens, `registryCredentials`, or other secrets.

[docs/adding-a-package.md](docs/adding-a-package.md) walks through mapping a new image.

## Pull requests from forks

GitHub gives a fork's pull request no secrets and no OIDC token, so CI can't log into
Chainguard for it. `ci.yml`'s `verify` then runs only what needs neither: unit tests, ruff,
discovery, the chart scan and both `bb-verify.py` renders. It skips the checks that read
the org's registry (overlays current, image age, signatures, CVEs) with a notice, and
`builds.yml` and the kind tests don't run. Run `python scripts/check.py --online` yourself
if you have a Chainguard org.

A maintainer runs the full set before merging. Push the fork's branch to this repository
under a new name and open a pull request from it (the fork's PR can then be closed in its
favour, or merged once the full run passes):

```bash
gh pr checkout <number>                  # the fork's branch, locally
git push origin HEAD:fork/<number>       # a branch here: CI runs with the repo's secrets
gh pr create --head fork/<number> --title "<title> (from #<number>)" --body "Full CI for #<number>."
```

Read the diff before pushing it: once it is a branch here, its workflows run with this
repository's secrets. CI never uses `pull_request_target` for that reason.

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE), the license of this project.
