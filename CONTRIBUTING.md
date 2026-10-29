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
2. Regenerate and verify (`scripts/bb-run.py`, or the steps in the README). Both
   `bb-verify.py` runs, standard and `--fips`, must pass, and so must
   `python -m pytest -q tests`.
3. Keep org-specific choices out of the shared files: the org belongs in
   `CHAINGUARD_ORG`, and exceptions for one org belong in `orgs/<org>.yaml`.
4. Never commit pull tokens, `registryCredentials`, or other secrets.

[docs/adding-a-package.md](docs/adding-a-package.md) walks through mapping a new image.

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE), the license of this project.
