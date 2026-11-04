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
  for your org fails the pull request. Images built here must carry `builds.yml`'s keyless
  signature from this repository's `main` (`BUILD_SIGNER`, default the repository itself).
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
  on every org image and `builds.yml`'s on every image built here (enforce by default when
  Kyverno is on: an unsigned image's Pod is refused; `--verify-signatures audit` only
  reports; `--org-id` requires your org). It needs Sigstore's public services, so it's off
  for disconnected sites.

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
- Give each registry login the least it needs: a read-only bot account's token for the build
  registry, a cgr.dev pull token (docs/install.md, "Registry logins"). Enter them at the installer's
  hidden prompt, not in an `export` line that lands in shell history.
- A pull token the installer made expires after a year (`status` warns 30 days ahead;
  `install --new-pull-token` replaces it), and `uninstall --delete-data` deletes it.

**What this repo builds**
- Each build starts FROM your org's images, pinned by digest. Iron Bank's recipe is pinned
  to a commit.
- Plugin zips and upstream binaries are pinned by the sha256 or digest in Iron Bank's own
  manifests, and checked on download. Upstream source built here (chainsaw, the Redis
  datasource backend, the Mattermost Operator) is pinned by the sha256 of its tag's
  tarball in `build.yaml`, and modules raised past upstream's go.mod come from the Go
  module proxy, checked against sum.golang.org.
- Each image is smoke-tested (tools, versions, user, and OpenSSL `fips=yes` for `-fips`).
- Each image is scanned with grype before it's pushed. A fixable critical CVE fails the
  build.
- With `SIGN_IMAGES=keyless`, pushed images get a cosign keyless signature (both cosign
  formats), signed before the release tag moves to them (a `<tag>-staging` push first), so
  the enforcing signature policy never sees an unsigned tag; then SPDX SBOM and SLSA
  provenance attestations. Provenance names the workflow run,
  the commit, and every pinned input (base images by digest, downloads by sha256).

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

## Known vulnerabilities

From the weekly `cve-report.yml` run of 2026-10-30 (grype, unique CVE and package). Totals
for 118 converted images: standard 29 Critical / 265 High, FIPS 48 Critical / 383 High.
The Criticals come from a few images:

| Image | Critical (std / FIPS) | Notes |
|---|---|---|
| `grafana-plugins` (built here) | 8 / 8 | All in the Redis datasource's Go 1.19 backend (no newer release). Fixed: the backend is rebuilt from source, and loads as an allowed unsigned plugin |
| `commercial-gitlab-*-ee` (sidekiq, toolbox, webservice, geo-logcursor) | 3 each | Chainguard's image of GitLab's 19.4 release; the nightly run takes its rebuilds |
| `bigbang-devops-tester` (built here) | 2 / 2 | All from upstream's chainsaw image (Go 1.26.0, x/net v0.52.0). Fixed: chainsaw is now built from source with a current Go, x/net, grpc and go-jose (the last two cleared 4 fixable Highs) |
| `mattermost-operator` (Iron Bank, now built here) | - | Iron Bank copies upstream's binary, whose go.mod pins `golang.org/x/net` v0.58.0 (GO-2026-6612, Critical). Fixed: `bigbang-mattermost-operator` is built from upstream's source with a current Go and x/net |
| `tempo-query` (Iron Bank, now built here) | - | Tempo 2.10.5's go.mod pins `golang.org/x/net` v0.52.0 (GO-2026-6612, Critical) and grpc, thrift and x/text with fixable Highs. Fixed: `bigbang-tempo-query` is built from upstream's source with those raised (grype: none fixable) |
| `minio-operator`, `minio-operator-sidecar` (Iron Bank, archived; now built here) | - | Upstream's go.mod pins `golang.org/x/crypto` v0.36.0 with a `replace` (8 Criticals) and x/net v0.38.0 (GO-2026-6612, Critical). Fixed: built from source with the replace dropped and both raised. Upstream is archived: no newer release will come |
| `renovate` | 1 / 1 | The nightly run takes Chainguard's rebuilds |
| `kubectl-fips:1.30.14-dev` (Vault's job) | - / 19 | Kubernetes 1.30 is end of life and the org stopped rebuilding it. Fixed: `min_version: "1.34"` moves Vault's kubectl to the newest release |

The pull request gate (`bb-cve.py --changed-from`) fails only on a *fixable* Critical in an
image the change adds, so these images were already in place or have no fix yet. Check the
latest run's summary for current numbers.

## Residual risks

- **Sigstore's public log** records each release and image signature, with this
  repository's name and image names, even while the repository is private.
- **Tag-only images** (Keycloak in Big Bang 4.0.0, custom resources) pick up a Chainguard
  rebuild without the nightly tests; the README's "Known gaps" lists them.
- **Iron Bank leftovers** need registry1 credentials at the site and follow Iron Bank's
  patch schedule, not Chainguard's.
- **A cluster installed before enforce became the default keeps audit mode** (it remembers its
  mode): re-run `install --verify-signatures enforce` on it. `bb-install.py status` says so
  on any cluster that only audits.
- **The Redis datasource plugin loads unsigned**: its backend is built here (to drop Go
  1.19), so Grafana can't check it against the publisher's signature. The image build
  pins its source by sha256, and the image itself is signed when `SIGN_IMAGES` is set.
- **The JMX exporter jar** (`bigbang-jmx-exporter`) is upstream's release jar, pinned by
  Iron Bank's sha256 and run inside SonarQube's JVM; it is not a FIPS build (Chainguard
  has no FIPS SonarQube either). Its download URL differs from Iron Bank's manifest by a
  `v` in the release tag; the sha256 is what pins it.
