# Contributing

Changes are welcome, and if you run this for your own Chainguard org, please send
your improvements back so everyone's Big Bang conversion gets better.

- **Fixes and new mappings:** open a pull request against `main`. New Big Bang
  packages, renamed Chainguard images, value paths the generator missed, and
  corrections to `stays` reasons are especially useful.
- **Problems:** open an issue with the Big Bang version (`BIGBANG_VERSION`), the
  package, and the output of `bb-convert.py` or `bb-verify.py`.

## Making a change

1. Edit `mapping.yaml` or `scripts/`. Never hand-edit the generated files
   (`overlays/`, `flux/`, `flux-fips/`, `docs/images.md`).
2. Regenerate and verify (see "Run it locally" in the README). Both
   `bb-verify.py` runs, standard and `--fips`, must pass.
3. Keep org-specific settings out of the change: the org belongs in
   `CHAINGUARD_ORG`, not in the scripts or `mapping.yaml`.
4. Never commit pull tokens, `registryCredentials`, or other secrets.

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE), the license of this project.
