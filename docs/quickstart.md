# Quickstart

From nothing to Big Bang running on your Chainguard org's images, installed from a pinned
release. The [installer details](install.md) have what is behind each step.

## Status: beta

Use it for **development, test and evaluation clusters**. It isn't supported for
production yet. Here is what is and isn't proven:

| Proven | Not yet |
|---|---|
| Every image Big Bang 4.0.0 deploys is converted or listed with a reason (`docs/images.md`); rendering checks both modes on every change | A run on a real, multi-node production-sized cluster: every runtime test is on kind in CI |
| Each package with a kind test (37 tests, most of Big Bang's packages) deploys and passes a functional check, standard and FIPS ([testing](testing.md)) | FIPS on a FIPS-mode kernel for most packages: Kyverno, Grafana and Keycloak pass on one weekly (RHEL 9 with `fips=1`); the rest are tested on a regular kernel |
| Upgrading from the newest release to the next is tested on every change (Kyverno, Headlamp, Grafana) | Upgrading an existing Iron Bank Big Bang in place to Chainguard images (needs an Iron Bank account in CI) |
| Every org image is pinned by digest and signature-checked; images built here are signed and attested | The images that stay on Iron Bank ([Known gaps](../README.md#known-gaps)) |

Support is best effort, through the repository's issues. Report security problems as
[SECURITY.md](../SECURITY.md) says, not in a public issue.

## Using this outside Zapata Technology

This repository's releases are built for Zapata Technology. Every overlay and installer in a
release points at Zapata's Chainguard org (`cgr.dev/zapatatechnology.com`) and Zapata's build
registry (`ghcr.io/zapata-technology`), and both are private. Installing a release as-is
needs pull access to both, which Zapata doesn't give out.

To use the project with your own images:

1. Copy the repository (a fork or a mirror you own).
2. Get your own Chainguard org with the images Big Bang needs
   ([Chainguard entitlements](your-org.md#chainguard-entitlements)).
3. Regenerate the overlays for your org and your registry:
   `python scripts/bb-run.py --org <your org> --build-registry ghcr.io/<your owner> --save`
   ([Use it with your org](../README.md#use-it-with-your-org)).
4. Build and push the images this repo builds into your registry, with `builds.yml` in your
   copy or `scripts/bb-build.py --all --build --push`. Without a build registry those few
   images stay on Iron Bank.
5. Install from your copy's own releases, using the steps below with your repository's name
   in place of `Zapata-Technology/bigbang-chainguard`.

Nothing in your copy pulls from Zapata's org or packages. Each file's header says which org
it was generated for, so you can check.

## Before you start

- [ ] A Chainguard org with the images your packages use, **and a plan that covers them**.
  - `docs/required-images.md` lists the images.
  - `chainctl images entitlements summary --parent <org>` shows what your plan covers,
    with a quota per tier.
  - `python scripts/bb-org-repos.py --org <org> --apply` adds the missing ones and names
    any your plan refuses, which you then request from Chainguard.

  See [Chainguard entitlements](your-org.md#chainguard-entitlements).
- [ ] A **test** cluster, Kubernetes 1.35+ (Big Bang 4; a Big Bang 3 site upgrades as in [Upgrading from Big Bang 3](install.md#upgrading-from-big-bang-3)), with a default StorageClass, and a kubectl
      context for it.
- [ ] `kubectl`, `helm` 3, Python 3.12 with PyYAML, `chainctl`, and `cosign` (to check the
      release).
- [ ] An Iron Bank account only if a package you enable keeps an Iron Bank image. The
      installer's preflight tells you which ones do.
- [ ] Your org is not `zapatatechnology.com`? Do the steps in
      [Using this outside Zapata Technology](#using-this-outside-zapata-technology) first,
      and install from your copy's releases.

## 1. Pick a release and pin it

Install from a release, never from `main`. A release (`<Big Bang version>-cg.<n>`, e.g.
`4.0.0-cg.1`) is a fixed set of overlays, digests and installer that CI tested together.
`main` moves every night.

```bash
TAG=4.0.0-cg.1          # the release you chose: Releases page, newest first
# While the repository is private, download with gh (a login with access to it):
gh release download "$TAG" -R Zapata-Technology/bigbang-chainguard \
  -p "bigbang-chainguard-$TAG.tar.gz" -p "bigbang-chainguard-$TAG.tar.gz.sigstore.json"
```

Write the tag down with your cluster's configuration. Upgrading means choosing a newer tag
on purpose (step 5).

## 2. Check it came from this repository

```bash
cosign verify-blob --bundle "bigbang-chainguard-$TAG.tar.gz.sigstore.json" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github.com/Zapata-Technology/bigbang-chainguard/.github/workflows/release.yml@' \
  "bigbang-chainguard-$TAG.tar.gz"
tar xzf "bigbang-chainguard-$TAG.tar.gz" && cd "bigbang-chainguard-$TAG"
```

Don't install if `Verified OK` isn't printed.

## 3. Install

```bash
pip install -r requirements.txt               # PyYAML, the version CI tests
chainctl auth login                           # the installer makes a pull token with it
python scripts/bb-install.py check            # tests every setting; changes nothing
python scripts/bb-install.py install          # add --fips for FIPS images
```

`check` names anything that would stop the install (an old Kubernetes, a wrong key in the
site file, an image your login or Chainguard plan can't pull) before the cluster changes.
The first run asks for your domain and packages and saves them in `my-site.yaml`. Keep
that file next to the release tag. It shows the kubectl context and asks before changing
the cluster. It also asks for a login to the build registry when the images built here are
private ([Registry logins](install.md#registry-logins)).

## 4. Check it

```bash
python scripts/bb-install.py status
```

`status` lists each package's health, the app URLs, and anything that needs attention: an
expiring pull token, or a cluster that is behind the release you run it from.

## 5. Upgrade to a newer release

Read the new release's notes (image changes since the last one), then:

```bash
# Download and verify the new tag as in steps 1 and 2, then from its directory:
cp ../bigbang-chainguard-$OLD_TAG/my-site.yaml .
python scripts/bb-install.py upgrade         # checks, shows what changes, asks, installs
python scripts/bb-install.py status
```

From a release that has it, `python scripts/bb-install.py upgrade --to <tag>` does the
download, the verification (with cosign installed) and the upgrade in one step, from the
directory holding `my-site.yaml`; `upgrade --list` shows the releases newer than the
cluster's ("Upgrading to a release" in [install.md](install.md)).

From Big Bang 3, `upgrade` also converts `my-site.yaml` to Big Bang 4's keys
([Upgrading from Big Bang 3](install.md#upgrading-from-big-bang-3)).

If something breaks, `python scripts/bb-install.py rollback` returns Big Bang to the
previous revision. A package whose data the new version already migrated may not accept
the old one, so read what `upgrade` shows before you answer.

Upgrade one release at a time when you can. Each release is tested as an upgrade from the
one before it, not from older ones.

## Getting help

- Something fails: run `status` and include its output in an issue, along with the release
  tag, standard or FIPS, and the packages you enabled. Leave out tokens and `my-site.yaml`
  secrets.
- A package-specific problem: check the package notes in [install.md](install.md#fips-notes) first (GitLab, FIPS,
  Keycloak) and `docs/testing.md` ("What the runtime tests found").
