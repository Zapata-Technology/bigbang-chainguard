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
- **Org images are pinned by digest.** Chainguard rebuilds images under the same tag; the
  overlays pin each to the digest it had when they were generated (a Flux post-renderer),
  so a rebuild reaches a cluster only through the nightly pull request, after the kind
  tests run it. Images set inside custom resources (Kiali's CR, Istio's injected proxy)
  and a repository a package uses at two tags stay on their tags.
- **A pull request can't add an image with a fixable Critical CVE**: `ci.yml` scans every
  image a change introduces (`bb-cve.py --changed-from`). `cve-report.yml` compares the
  whole set against Iron Bank weekly.
- **In the cluster**, `bb-install.py` adds a Kyverno policy checking Chainguard's signature
  on every org image (audit by default when Kyverno is on; `--verify-signatures enforce`
  refuses unsigned ones; `--org-id` requires your org). It needs Sigstore's public
  services, so it's off for disconnected sites.

**What you install**
- Each release archive comes with its sha256 and a Sigstore bundle signed keylessly by
  `release.yml`; the release notes show how to verify it with `cosign verify-blob`. The
  signature is recorded in Sigstore's public transparency log.
- The installer image is built FROM the org's images like the others, scanned, and (with
  `SIGN_IMAGES=keyless`) signed with an SBOM.
- The installer serves a self-signed certificate until you choose one, and `status` warns
  about it: use `--tls-issuer` or `--tls-cert/--tls-key` in production.

**Credentials in the cluster**
- `bb-install.py` keeps the pull token, S3 keys and generated passwords (Keycloak admin and
  database) in the Secret `bigbang/bb-install`, so re-runs don't ask again. Big Bang itself
  copies the pull credentials into a `private-registry` Secret in every package namespace.
- Limit who can read Secrets in `bigbang` (and the package namespaces) with RBAC, and turn
  on encryption at rest for Secrets in your cluster's API server.
- A pull token the installer made expires after a year (`status` warns 30 days ahead;
  `install --new-pull-token` replaces it), and `uninstall --delete-data` deletes it.

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
- Workflow actions are pinned to commit SHAs. Dependabot proposes updates.
- step-security/harden-runner blocks every outbound call the CI, build and nightly
  update workflows don't list (GitHub, PyPI, Chainguard, repo1/registry1, Sigstore, the
  scanners' databases). The kind tests run it in audit mode: their cluster pulls images
  from whichever registries a test needs.
- `scripts/bb-scan.py` fails CI on shell code run by `sh` that needs bash, and on empty
  chart defaults written into an app's config, until each is reviewed in `mapping.yaml`.
- The Chainguard pull identity CI uses can only pull, and is trusted only for this repo's
  `main` branch and pull requests.
- Pull credentials are never committed: sites supply them through `registryCredentials`.
  The compat tests pass tokens through the environment, mask them, and delete the
  temporary files they write.

**Repository settings**
- `main` is protected: pull requests and the `verify` check are required, force pushes and
  deletion are refused (when the GitHub plan allows branch protection).
- `SIGN_IMAGES=keyless` is set: pushed images are signed and carry an SBOM attestation.
- Turn on private vulnerability reporting and secret scanning push protection.
- `codeql.yml` and `scorecard.yml` are in place and skip themselves while the repository is
  private (CodeQL results need GitHub code security there; Scorecard scores public
  repositories only). They start on their own once it is public.
