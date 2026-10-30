## What changes

<!-- Which package or image, and why. -->

## Checks

- [ ] I changed `mapping.yaml`, `orgs/`, `scripts/` or `builds/*/Dockerfile`, not the generated files (`overlays/`, `flux*/`, `docs/images.md`, `docs/required-images.md`, `builds/*/build.yaml`) by hand
- [ ] `python scripts/check.py --online` passes (unit tests, chart scan, overlays current, verify standard and `--fips`)
- [ ] Org-specific choices are in `orgs/<org>.yaml`, not `mapping.yaml`
- [ ] No pull tokens, `registryCredentials` or other secrets

## Deployed?

<!-- Rendered only, or deployed to a cluster? Which packages? -->
