# Security

## Reporting a vulnerability

Please report security problems privately, not in a public issue: use GitHub's
**Report a vulnerability** button (the repository's Security tab, "Private
vulnerability reporting"). Include the file or workflow involved, what an attacker
could do, and how to reproduce it. We aim to acknowledge reports within five business
days.

## What is in scope

- The scripts in `scripts/` and the workflows in `.github/workflows/`.
- The generated overlays and Flux kustomizations: an image reference that pulls from an
  unexpected registry, or a Kyverno allow list that admits more than it should.
- The images this repo builds (`builds/`): their Dockerfiles and smoke tests.

## Out of scope

- Vulnerabilities in the images themselves. Report those to
  [Chainguard](https://www.chainguard.dev/) or to Iron Bank for images that stay there.
- Big Bang's charts: report those to the [Big Bang project](https://repo1.dso.mil/big-bang/bigbang).

## How the repo protects you

[docs/architecture.md](docs/architecture.md#7-trust-boundaries) has a diagram of the trust
boundaries.

**What a cluster runs**
- Every converted image comes from your own Chainguard org.
- **CI checks Chainguard's signature on every one of them** (`scripts/bb-signatures.py`:
  cosign, keyless). Org images must be signed by Chainguard's issuer (`issuer.enforce.dev`),
  under your org's ID (the `CHAINGUARD_ORG_ID` variable). An image Chainguard didn't sign
  for your org fails the pull request.
- The overlays set Big Bang's Kyverno `restrict-image-registries` allow list to exactly your
  org, your build registry and registry1.
- Every image that stays on Iron Bank is documented with its reason in `docs/images.md`.

**What this repo builds**
- Each build starts FROM your org's images, pinned by digest. Iron Bank's recipe is pinned
  to a commit.
- Plugin zips and upstream binaries are pinned by the sha256 or digest in Iron Bank's own
  manifests, and checked on download.
- Each image is smoke-tested (tools, versions, user, and OpenSSL `fips=yes` for `-fips`).
- Each image is scanned with grype before it's pushed. A fixable critical CVE fails the
  build.
- With `SIGN_IMAGES=keyless`, pushed images get a cosign keyless signature and an SPDX SBOM
  attestation.

**The pipeline**
- Workflow actions are pinned to commit SHAs. Dependabot proposes updates, and runners
  are hardened with step-security/harden-runner.
- The Chainguard pull identity CI uses can only pull, and is trusted only for this repo's
  `main` branch and pull requests.
- Pull credentials are never committed: sites supply them through `registryCredentials`.
  The compat tests pass tokens through the environment, mask them, and delete the
  temporary files they write.

**Recommended repository settings** (an admin sets these)
- Protect `main`: require the `verify` and `build` checks, and pull requests.
- Turn on private vulnerability reporting and secret scanning push protection.
- Once the repository is public: OpenSSF Scorecard and CodeQL, which on a private repository
  need GitHub Advanced Security.
