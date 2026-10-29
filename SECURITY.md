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

- Every converted image comes from your own Chainguard org. The images this repo
  builds start FROM your org's images, pinned by digest.
- Plugin zips and upstream binaries are pinned by sha256 or digest, taken from Iron
  Bank's own manifests, and checked on download.
- Workflow actions are pinned to commit SHAs, and runners are hardened with
  step-security/harden-runner.
- Pull credentials are never committed. Sites supply them through `registryCredentials`.
