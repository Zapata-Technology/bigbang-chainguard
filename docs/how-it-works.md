# How it works

For maintainers and the curious: what the pipeline does, the version rules, the images
this repo builds, and how releases and updates happen. To install, the
[README](../README.md#install) is all you need.

## Version compatibility

Tags follow one rule: the newest patch on the MAJOR.MINOR line that Iron Bank pins, and
never `latest`. Chainguard orgs carry Chainguard's *current* release lines, so sometimes
the org has nothing on Big Bang's line:

- **Newer minor ("MINOR AHEAD").** The newest release of the same MAJOR is used. Big Bang
  never tested that combination.
- **Newest release.** Images Iron Bank tags by date (MinIO) have no version line to match.

`docs/images.md` ends with a **Version differences** section listing every such image, and
[`docs/version-differences.md`](version-differences.md) compares every image's version with
Iron Bank's, newest-line changes first.
Test those packages before production, or run with `--strict-versions` (on `bb-run.py`
or `bb-convert.py`). Strict mode never moves to a newer minor; those images then fail
conversion and you decide case by case.

### Flux runs a newer release than Big Bang's

Big Bang 4.0.0, like 3.35.1, installs Flux 2.9 from Iron Bank. That release pins notification-controller
v1.9.4, source-controller and kustomize-controller v1.9.5, and helm-controller v1.6.4.
Chainguard builds only Flux's current release line, so the org has no 1.9.x tags. The oldest
it has are 1.10.x, and 1.7.x for helm-controller (`docs/images.md` lists them as
`MINOR AHEAD`).

Swapping in the newer images alone doesn't work, because Big Bang's Flux CRDs are too old for
them; notification-controller 1.10, for example, doesn't start on Flux 2.9's CRDs. So when the
org's controllers are on a newer minor, `bb-convert.py` (with `scripts/fluxrelease.py`) does
the following:

1. Finds the upstream Flux release those controllers belong to. Today that's v2.10.0, which
   is also the newest Flux release.
2. Installs that release's `install.yaml`, CRDs included, instead of Big Bang's `base/flux`.
3. Applies Big Bang's own Flux patches: the `private-registry` pull secret, the security
   context and the autoscaler annotation.
4. Removes the controllers Big Bang doesn't run: image-automation, image-reflector and
   source-watcher.
5. Points the four controllers at the org's images, pinned by digest, or at the `-fips`
   images in `flux-fips/`.

`flux/kustomization.yaml` and `flux-fips/kustomization.yaml` name the release in their header.
The nightly update moves to a new Flux release when Chainguard's controllers do, so you always
run the newest Flux, which Chainguard keeps patched. If the org's controllers don't match any
one Flux release, for example halfway through Chainguard publishing a new line, the generator
writes `UNRESOLVED` and CI fails. It doesn't ship a mismatched set.

Every kind test, the release-upgrade test and the FIPS-kernel test install Flux this way, so
Big Bang's GitRepositories and HelmReleases are tested on it. The cost is
that sites run a Flux newer than the one Big Bang 4.0.0 was released with. When Big Bang moves
to Flux 2.10, the overlay goes back to Big Bang's `base/flux` with only the images swapped.

## Images this repo builds

Big Bang ships three images of its own that Chainguard doesn't publish. Instead of
depending on anyone's custom build, `scripts/bb-build.py` converts **Iron Bank's own recipe**
(`hardening_manifest.yaml` and Dockerfile, pinned to a repo1 commit) into a build FROM
your org's images. Vault is the fourth: Chainguard publishes it, but Big Bang's
post-start hook needs curl, which the Chainguard image lacks, so the `vault` build adds it.
The JMX exporter is the fifth: SonarQube's init container copies Iron Bank's jar with
sh and curl, and Chainguard's `jmx-exporter-iamguarded` has neither (Bitnami layout).
The Mattermost Operator, tempo-query, the MinIO Operator with its sidecar, GitLab's
gitaly-init-cgroups and NeuVector's registry adapter are the sixth to eleventh: Chainguard
publishes no image of them, so
their builds compile them from upstream's source. Each build is a directory
`builds/<name>/`: a Dockerfile, a hand-written `recipe.yaml` (what the build is) and the
generated `build.yaml`; the recipe names a hook in `scripts/buildhooks/` for the Python it
needs (the two MinIO builds share one):

| Iron Bank image | Build (`builds/<name>`) | Built FROM | Image |
|---|---|---|---|
| `big-bang/grafana/grafana-plugins`: Grafana with plugins baked in | `grafana-plugins` | `grafana`, `grafana-fips` | `bigbang-grafana-plugins` |
| `big-bang/base`: bash, kubectl, yq and jq for Big Bang's upgrade, init and test jobs (33 value paths in ~25 packages) | `base` | `kubectl-dev`, `kubectl-fips-dev` | `bigbang-base` |
| `big-bang/devops-tester`: Big Bang's test runner (kubectl, velero, kyverno, chainsaw, mc) | `devops-tester` | `kubectl-dev`, `kubectl-fips-dev` | `bigbang-devops-tester` |
| `hashicorp/vault`: not Big Bang's own image, but its post-start hook needs curl, which Chainguard's vault lacks | `vault` | `vault-dev`, `vault-fips-dev` | `bigbang-vault` |
| `opensource/prometheus/jmx-exporter`: SonarQube's init container copies its javaagent jar (`/opt/jmx_exporter`) with sh and curl | `jmx-exporter` | `busybox-dev`, `busybox-fips-dev` | `bigbang-jmx-exporter` |
| `opensource/mattermost/mattermost-operator`: no Chainguard image; built from upstream's source at Iron Bank's tag | `mattermost-operator` | build: `go-dev`, `go-fips-dev` (else `kubectl(-fips)-dev` + Wolfi Go); run: `glibc-openssl(-fips)` (else `kubectl(-fips)`) | `bigbang-mattermost-operator` |
| `opensource/grafana/tempo-query`: no Chainguard image; Tempo's Jaeger storage backend, built from grafana/tempo's source at Big Bang's tag | `tempo-query` | as the Mattermost Operator | `bigbang-tempo-query` |
| `opensource/minio/operator` and `operator-sidecar` (both archived on Iron Bank): no Chainguard images; built from minio/operator's source (archived upstream) at Big Bang's tags | `minio-operator`, `minio-operator-sidecar` (one hook, `minio`) | as the Mattermost Operator | `bigbang-minio-operator`, `bigbang-minio-operator-sidecar` |
| `gitlab/gitlab-org/build/cng/gitaly-init-cgroups` (GitLab's CNG build on registry1; no Iron Bank recipe): Gitaly's cgroups init container, built from Gitaly's source as CNG builds it | `gitaly-init-cgroups` | as the Mattermost Operator | `bigbang-gitaly-init-cgroups` |
| `opensource/neuvector/registry-adapter`: no Chainguard image; NeuVector's Harbor scanner adapter, built from upstream's source at Big Bang's tag | `registry-adapter` | as the Mattermost Operator | `bigbang-registry-adapter` |

You don't run these to install: CI (`builds.yml`) builds, tests, signs and pushes the
images when a change merges to main. The rest of this section is for maintainers.

`scripts/bb-build.py <name>`, through the build's hook:

1. reads the tag Big Bang deploys
2. reads Iron Bank's recipe from [repo1](https://repo1.dso.mil/dsop):
   - the plugin zips and their sha256, which are checked on download
   - the package and tool versions
   - the non-root user
3. pins the org images it builds FROM by digest, on Iron Bank's MAJOR.MINOR line
4. writes `builds/<name>/build.yaml`, the input for the Dockerfile

```bash
python scripts/bb-build.py base                         # regenerate builds/base/build.yaml
python scripts/bb-build.py base --build --test          # build standard + FIPS, run the smoke test
python scripts/bb-build.py base --build --test --push   # and push to BUILD_REGISTRY
python scripts/bb-build.py --all                        # regenerate every build (--list names them)
```

Constants live in the recipe (the Iron Bank project, the org images, the modules to raise,
the notes), and so do the build arguments and, for the images with a shell, the smoke
test (`smoke`: commands and the regexes their output must match). Adding a build is a new
`builds/<name>/` with a recipe, plus a hook only when no existing one fits.

The smoke tests:

- **grafana-plugins:** Grafana loads every plugin with a valid signature, except a
  rebuilt one, which loads as an allowed unsigned plugin; no plugin reports an error.
- **base and devops-tester:** the uid, and each tool at Iron Bank's version line.
- **jmx-exporter:** SonarQube's init container command, run as the chart runs it (uid
  1000, gid 0, read-only root), copies a jar with Iron Bank's sha256 into `/data`.
- **Every `-fips` image:** OpenSSL defaults to `fips=yes`.

Each image also has a `-fips` twin.

Differences from Iron Bank:

- **base and devops-tester run on the `-dev` image**, because Big Bang's jobs need a shell.
  Tools are in `/usr/bin`, and no Big Bang chart calls them by full path. Iron Bank's
  `CCE-*.sh` RHEL STIG scripts don't apply to Wolfi, and `build.yaml` lists them as not
  ported.
- **Tags:** base and devops-tester are tagged `<Iron Bank tag>-kubectl<version>`, so a
  kubectl patch gets a new tag; jmx-exporter `<Iron Bank tag>-busybox<version>`.
- **jmx-exporter** carries only the javaagent jar, at Big Bang's version (the chart's
  `downloadURL` names it), from the newest Iron Bank commit that builds that version.
  Iron Bank's Java runtime and standalone exporter entrypoint are not ported: Big Bang
  runs the image only as SonarQube's init container. Iron Bank's 1.6.0 manifest URL has a
  `v` upstream's release tag doesn't; the build takes the file from the tag without it,
  pinned by the manifest's sha256.
- **chainsaw** (devops-tester) has no Chainguard package. It is built from upstream's
  source at Iron Bank's version, using Wolfi's Go with `golang.org/x/net`, `grpc` and
  `go-jose` raised to their newest releases. Iron Bank copies upstream's image, whose old Go
  and x/net carry a fixable Critical CVE (and grpc and go-jose fixable Highs). The FIPS image's chainsaw is not FIPS-built.
- **The Redis datasource's Go backend** (grafana-plugins) is rebuilt from its release's
  source. Its latest release (2.2.1) ships binaries built with Go 1.19 and old grpc,
  x/net and plugin SDK modules, with 8 fixable Critical CVEs. The build raises those
  modules, patches the plugin for the newer SDK (`builds/grafana-plugins/patches/`) and
  keeps one linux binary. It no longer matches the plugin's signed `MANIFEST.txt`, so the
  build drops the file, and `overrides.grafana` in `mapping.yaml` lets Grafana load
  `redis-datasource` unsigned. Big Bang's provisioned Redis datasources (Argo CD,
  AuthService, GitLab) use it. Its backend is not FIPS-built.
- **mattermost-operator** is built from upstream's source at the tag Iron Bank ships
  (Iron Bank copies upstream's binary onto distroless static), with `golang.org/x/net`
  raised past upstream's v0.58.0 (a Critical CVE). The standard binary is static. With a
  `go-fips` image in the org, the FIPS one is built like upstream's `Dockerfile.fips` (cgo,
  `-tags=requirefips`) with Chainguard's FIPS Go, on OpenSSL's FIPS provider. Without
  one (kubectl-fips -dev has no FIPS Go package), it is built with Wolfi's Go against
  Go's own FIPS 140-3 module (`GOFIPS140`, `GODEBUG=fips140=on` by default); add
  `go`/`go-fips` to the org to switch. `build.yaml` records which (`image.fips.fips`). It runs as
  65532 with Iron Bank's entrypoint `/mattermost-operator`, tagged with Iron Bank's tag.
  The builder takes the first image pair the org has: `go`/`go-fips` and
  `glibc-openssl`/`glibc-openssl-fips` when you add them, else `kubectl`/`kubectl-fips`.
  Its smoke test runs `--help`, starts it with no cluster (it must log its Go version and
  stop at the missing kubeconfig) and reads the binary's build info (raised modules; for
  `-fips`, a cgo build with a FIPS crypto backend).
- **tempo-query** is built the same way (shared helpers in `scripts/bbbuild.py`) from
  grafana/tempo's source at the tag Big Bang deploys (2.10.5; Iron Bank's recipe has
  moved on to 3.x), `./cmd/tempo-query`, with `golang.org/x/net` (a Critical CVE),
  `x/text`, `grpc` and `thrift` raised to their newest releases. Iron Bank puts the binary
  on its jaeger-query image, but the entrypoint is `/tempo-query` alone (a gRPC server on
  7777 that answers Jaeger's storage API from Tempo's), so no Jaeger is carried over. It
  runs as 10001, Iron Bank's uid. Big Bang leaves the chart's `tempoQuery` off; turn it
  on with `tempo.values.upstream.tempoQuery.enabled`. Tempo is AGPL-3.0: the image
  carries its LICENSE, and `build.yaml` names the source tarball. Its smoke test calls
  the gRPC health service (curl over HTTP/2) and reads the build info.
- **minio-operator and minio-operator-sidecar** are built from minio/operator's source at
  Big Bang's tags (operator v7.1.1, sidecar v7.1.0: the root module's `./cmd/operator`
  and the `sidecar` module's `./cmd/sidecar`), with upstream's version ldflags, static.
  Upstream archived the repository in 2026 and Iron Bank moved both recipes to
  `iron-bank-archives`, so no newer release will come; the builders read the archived
  recipes. Upstream's go.mod pins `golang.org/x/crypto` v0.36.0 with a `replace` (8
  Critical CVEs) and x/net v0.38.0 (Critical): the build drops the replace and raises
  both. Iron Bank sets no user (root); these run as 65532, and Big Bang's charts run them
  as their own non-root uids. Both share `builds/minio-operator/Dockerfile` (one final
  stage per image, `build.yaml` `target`) and the hook `scripts/buildhooks/minio.py`. MinIO Operator is
  AGPL-3.0; the images carry its LICENSE and CREDITS.
- **gitaly-init-cgroups** has no Iron Bank recipe: Big Bang pulls GitLab's own CNG build
  from registry1. It is built as CNG builds it, from Gitaly's `tools/gitaly-init-cgroups`
  module at the Gitaly release of Big Bang's tag (v19.4.2-ubi: Gitaly v19.4.2), static,
  `/setup_cgroups` as the command, run as root (CNG's user; the chart runs it as root to
  chown the Gitaly pod's cgroup to uid 1000). Source and recipe come from gitlab.com
  (GitLab's archive of the module's directory, pinned by sha256). It runs only with
  `gitlab.gitaly.cgroups.enabled` (off in Big Bang). Its smoke test runs it as the chart
  does against a pod cgroup tree in a temporary directory.
- **registry-adapter** (NeuVector's Harbor scanner adapter) is built as Iron Bank builds
  it, with upstream's `make build` at the tag Big Bang deploys (v0.2.13), the tag stamped
  into `version.go`, and `golang.org/x/net` (a Critical CVE) and go-jose raised. It needs
  cgo (NeuVector's shared system package), so both binaries link the runtime image's
  glibc; the FIPS one uses the same FIPS modes as the Go builds above
  (`go_build_problems(..., cgo=True)` checks a cgo build). It runs
  `/usr/local/bin/adapter` as 65534 (Iron Bank's `nobody`). Big Bang leaves the chart's
  `cve.adapter` off; turn it on with `neuvector.values.upstream.cve.adapter.enabled` and
  give it Harbor's credentials in a Secret named by `cve.adapter.harbor.secretName` (keys
  `username`, `password`); not `cve.adapter.env`, which NeuVector core 2.11.4 renders
  into invalid YAML. Its smoke test starts it
  with no cluster (it must log its version and stop at the missing internal certificate)
  and reads the build info.

**`BUILD_REGISTRY`** can't be `cgr.dev`, because Chainguard registries don't accept pushes.
**Leave it empty to keep Iron Bank's images.** The overlays then leave those values alone,
and verification accepts them.

**How images get published (`builds.yml` in CI):**

- Every change builds and smoke-tests the images.
- A merge to `main` pushes them to `BUILD_REGISTRY`. With `SIGN_IMAGES=keyless` each
  image is signed before its tag moves to it (pushed as `<tag>-staging`, signed, then
  tagged), then gets an SBOM and a SLSA v1 provenance predicate the build job writes
  (not a SLSA Build L3 attestation). GHCR pushes are retried.
- The nightly job regenerates the build specs, so a new Grafana or kubectl patch, or a
  plugin added by Iron Bank, arrives as a pull request.

## How the conversion works

[docs/architecture.md](architecture.md) has diagrams of the pipeline, the builds,
how the overlays layer at deploy time, the nightly update, object storage, the trust
boundaries, and where Iron Bank's and Chainguard's images behave differently.

```
scripts/bb-fetch.py     clone Big Bang at BIGBANG_VERSION + every package at its pinned tag
scripts/bb-images.py    render Big Bang with every package on, render each package with the
                        values Big Bang gives it, and record every deployed image and the
                        value path that sets it            -> discovery/images.json
scripts/bb-convert.py   map each Iron Bank image (mapping.yaml + orgs/<org>.yaml), resolve the
                        org tag, write the overlays at those same value paths
                                                           -> overlays/, flux*/, docs/
scripts/bb-verify.py    render again with the overlays: every image must be an org image
                        (a -fips one with --fips) or a documented Iron Bank image
scripts/bb-scan.py      read every package chart for sh scripts that need bash and empty
                        defaults written into an app's config; each must be in mapping.yaml
                        scan_reviewed
scripts/bb-run.py       all of the above, in order
scripts/bb-leftovers.py which `stays ... until` images Chainguard now publishes (nightly report)
scripts/bb-install.py   deploy it: init, install (re-run to upgrade or switch FIPS), status, uninstall
```

Value paths are found automatically from each chart's computed values:

- repository/tag pairs
- registry+repository splits
- single image strings
- tags in sibling keys (`release`, `crdRelease`, `image_version`, `version`)
- images inside lists

It also blanks Big Bang's chart-wide registry1 registry settings (`global.imageRegistry`,
Kyverno's `defaultRegistry`), which would otherwise win. For the few packages these rules
can't express, `mapping.yaml` has these escape hatches:

- `hubs`: Istio builds names from `hub`; the overlay sets full image references instead.
- `overrides`: hand-written values with live tag tokens (NeuVector's shared registry/tag).
- `template_defaults`: images a template defaults with no value behind it (Big Bang's wait job);
  `beside` sets values next to it in the same releases (the wait job's seccomp profile, which
  Pod Security Admission's `restricted` level requires); `patch_keys` get `patch` as a
  post-renderer instead, where the chart's schema refuses the values (Istio's gateway).
- `post_renderers`: images hardcoded in a template. A Flux post-renderer rewrites them.
- `chart_registry_builds`: images this repo builds in a chart that prefixes every image
  with one chart-wide registry (NeuVector's registry adapter). A Flux post-renderer moves
  `<org registry>/<built image>` to `BUILD_REGISTRY`, or, with no `BUILD_REGISTRY`,
  `<org registry>/ironbank/...` back to registry1. `bb-verify.py` fails on any nested path
  under the org (org repositories are flat).
- `version_labels`: workload labels that must state the version actually deployed (istiod).
- `bash_wait_jobs`: wait jobs whose `#!/bin/sh` script is really bash. Iron Bank's UBI
  images run `sh` as bash; Chainguard's `-dev` images run busybox `sh`. A Flux
  post-renderer runs those scripts with bash (Kyverno policies' wait job).
- `bash_commands`: containers whose `/bin/sh -c` script is really bash; a post-renderer
  makes the command `/bin/bash` (Fortify's keystore job).
- `progress_deadline`: Deployments that must outlast Kubernetes' 600s progress deadline
  (GitLab's migrations, 2400s). A post-renderer patch, so it does not replace the digest pins.
- `unpinned`: org images that stay on their exact tag, with why (Istio's injected proxy;
  a repository a package uses at two tags). `bb-verify.py` fails on any other unpinned image.
- `no_post_renderers`: packages whose Big Bang template can't take a post-renderer
  (Keycloak in Big Bang 4, still true on 4.1.0); their images stay on tags instead of digests.
- `min_version` (on an image): an Iron Bank tag on an older line takes the newest release
  (kubectl `1.34`: Vault pins the end-of-life 1.30).
- `scan_reviewed`: what `scripts/bb-scan.py` found in the package charts and how each is
  handled. CI fails on a new finding.

An Iron Bank image with no Chainguard equivalent yet can be marked
`{stays: <reason>, until: <chainguard repo>}`. The conversion then reports when your org's
repo has tags, so you know when to switch it over.

[`docs/adding-a-package.md`](adding-a-package.md) walks through mapping a new image.

## Upgrades

- **Nightly** (`update.yml`): regenerates against the newest org patches and Iron Bank
  recipes, and opens a PR. `automerge.yml` merges it once every check on it has passed
  (CI, the builds and every kind test). It leaves a new Big Bang release, or a PR labeled
  `hold`, for a person to merge.
- **New Big Bang release**: the same job bumps `BIGBANG_VERSION` and re-runs discovery.
  If the release adds an image that `mapping.yaml` doesn't cover, CI fails on that PR and
  names it. Add the mapping (or a `stays` entry with the reason) and push to the bot branch.
- **CI** (`ci.yml`): unit tests, then discovery, conversion and verification for both modes.
- **Releases** (`release.yml`): every merge that changes what you deploy (overlays, Flux,
  SeaweedFS, the installer) is a release `<Big Bang version>-cg.<n>`, for example
  `4.0.0-cg.40`; `n` starts again at 1 with each Big Bang version. A release holds the
  install archive, its sha256, a Sigstore signature and notes listing the image changes
  since the previous release (configuration changes are in its list of merged pull
  requests). Nightly digest re-pins make releases frequent.
- **Upgrading a cluster:** `bb-install.py upgrade` from the newer release (from Big Bang 3
  too: see [Upgrading from Big Bang 3](install.md#upgrading-from-big-bang-3)).
  `install --plan` shows what would change (version, FIPS, packages, images) without
  changing anything, `status` says when the cluster is behind the release you run it
  from, and `rollback` returns Big Bang to an earlier revision.


`discovery/render-inputs.yaml` holds render-only placeholders (fake database and Redis
endpoints) so that GitLab and Anchore render during discovery. Never deploy with it.

## Object storage

Several packages need S3 storage:

- Big Bang's `minio` and `minio-operator` packages
- Thanos (scalable), Mattermost and GitLab, which bundle MinIO when no external storage is set
- Tempo, Mimir and Velero, which use S3 when given an endpoint

The **`OBJECT_STORAGE`** setting picks the provider. Like the other settings, it comes from
`--object-storage`, then the environment or repository variable, then the
`OBJECT_STORAGE` file.

| `OBJECT_STORAGE` | What you get |
|---|---|
| **`seaweedfs`** (default) | `bb-convert.py` writes `overlays/values-object-storage.yaml`, which turns every MinIO off and points each package that can use S3 at SeaweedFS. It also writes `seaweedfs/` and `seaweedfs-fips/`, which run SeaweedFS from your org's image. |
| `minio` | Big Bang deploys MinIO itself, on Chainguard's `minio` images. No extra files. |

Set **`OBJECT_STORAGE_ENDPOINT`** (same rules) to use an S3 service of your own instead of the
in-cluster SeaweedFS. The default is `http://seaweedfs-s3.seaweedfs.svc.cluster.local:8333`.

### What uses S3


| Package | Stores |
|---|---|
| Tempo | traces |
| Thanos | metric blocks; no bundled MinIO |
| Mimir | blocks, ruler, alertmanager |
| Mattermost | files; no bundled MinIO |
| GitLab | registry, LFS, artifacts, uploads, packages, backups (`bigbang-gitlab-*`) |
| Velero | backups, through the AWS plugin |
| Loki | only with `bb-object-storage.py --with loki`. Big Bang's default Loki keeps logs on local disk; S3 needs Loki's `scalable` strategy |

- **Requests are path-style.** Every package is configured for it, which SeaweedFS requires.
- **The generated `seaweedfs/` is a single server** (master, volume, filer and S3 in one pod)
  on one volume. For high availability, run SeaweedFS's own Helm chart and set
  `OBJECT_STORAGE_ENDPOINT` to it.
- **Verification follows the setting.** `bb-verify.py` renders with the overlay and fails if
  any MinIO is still deployed. The compat test `object-storage` deploys the generated
  `seaweedfs/` and runs Tempo on it.
- **Package settings:** `object-storage.yaml` records where each package takes its S3
  settings. Recheck it when `BIGBANG_VERSION` changes.
