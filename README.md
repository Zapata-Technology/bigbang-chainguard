# Big Bang on Chainguard images

Run [Big Bang](https://repo1.dso.mil/big-bang/bigbang) with images from **your own
Chainguard org** (`cgr.dev/<org>`) instead of Iron Bank, with FIPS as an option.

Big Bang itself is not forked. This repo generates values overlays for the unmodified
upstream chart, plus a Flux kustomization:

- It discovers every image Big Bang deploys.
- It maps each image to its Chainguard equivalent.
- It pins your org's tags.
- It verifies the result by rendering Big Bang again.

A nightly job keeps the overlays current with your org's patches and with new Big Bang
releases.

## Install

Three commands:

```bash
chainctl auth login                          # once: the installer makes your pull token with it
python scripts/bb-install.py check           # tests every setting; changes nothing
python scripts/bb-install.py install         # add --fips for FIPS images
```

The first run asks for your domain and the packages you want, and saves them in
`my-site.yaml`. `install` then does everything in order: pull secrets, Flux, object
storage, Big Bang, TLS and image-signature checks. It waits until every package is healthy
and prints each app's URL. It is safe to run again.

**`check` tests every setting before anything changes:**

- your tools and cluster: kubectl, helm, Kubernetes 1.35 or newer, a default StorageClass;
- your site file: Big Bang renders with it, so a misspelled or outdated key is caught;
- your logins: every Chainguard image your packages use, and every image this repo builds,
  is test-pulled, which also proves your Chainguard plan covers them;
- the upgrade path from what the cluster runs now.

`install` and `upgrade` run the same checks first and stop before touching the cluster if
one fails.

**After that:**

```bash
python scripts/bb-install.py status          # what's healthy, and the app URLs
python scripts/bb-install.py upgrade         # move to a newer release, including from Big Bang 3
python scripts/bb-install.py rollback        # back to the previous revision
```

`upgrade` shows what will change and asks before applying it. From Big Bang 3 it also
converts your site file to Big Bang 4's keys and keeps the original.

**You need** a Kubernetes 1.35+ cluster with a default StorageClass, and `kubectl`, `helm` 3,
Python 3 with PyYAML and `chainctl`. To skip installing the tools, run the same commands
from the installer image: `scripts/bb-docker.sh check`, then `scripts/bb-docker.sh install`.

More: [every installer option](docs/install.md) (TLS, signatures, GitOps, air-gapped sites,
doing it by hand) · [Quickstart](docs/quickstart.md) (pinning and verifying a release) ·
[your own Chainguard org](docs/your-org.md).

## Why Chainguard images instead of Iron Bank's

**Run the same, unmodified Big Bang on images with almost no known vulnerabilities.**
Chainguard rebuilds every image from source, keeps it minimal, and patches it continuously.
This repo swaps those images in for Iron Bank's across every package, with a FIPS twin for
almost every one and signatures you can check. Every change is deployed and tested on
Kubernetes (kind in CI) before it reaches you. You get a smaller attack surface, far less CVE
triage, and FIPS with one switch, without forking Big Bang.

Security first. Every number below comes from the repo's own scans and tests; the CVE
counts are from grype on **2026-11-02**, and the [weekly CVE report](.github/workflows/cve-report.yml)
refreshes them.

- **A fraction of Iron Bank's known vulnerabilities.** Iron Bank's images need an
  account to scan, but their recipes are public, and each one names the base image it is
  built FROM (Red Hat UBI 9 for most). We scanned the public copy of each base. For the 67
  images whose base resolves:
  - **Iron Bank's bases alone**, before any application is added, carry **840 High and
    6,688 Medium** CVEs.
  - **The complete Chainguard replacements**, application included, carry **31 High and
    29 Medium**.
  - In **53 of the 67**, the whole Chainguard image has fewer Critical and High CVEs than
    the Iron Bank image's base alone.
  - Counts are summed per image; same scanner (grype) and same day for both sides. The
    Iron Bank figures are a floor: the real images also carry their applications.
- **Almost no known vulnerabilities.** Big Bang's Iron Bank images are replaced by 111
  Chainguard images. Of those:
  - **105 (95%) have no Critical CVE**;
  - **83 have no Critical or High** (88 of the 111 FIPS images);
  - **68 have no known CVE at all**.

  Across all 111 there are **6 distinct Critical CVEs**. All are in third-party code:
  GitLab EE's Rails images (GraphicsMagick and Python), Gitaly (containerd) and Grafana's
  image renderer (Chromium). Upstream has published a fix for only one of them so far.
- **A fixable Critical can't sneak in.** Every pull request scans the images it adds, and
  CI fails if one has a Critical CVE that already has a fix (`bb-cve.py --changed-from`).
  That includes the nightly update.
- **Patched continuously.** Every night the overlays move to
  the newest patch in your org, pinned by digest. CI rejects any image Chainguard hasn't
  rebuilt in 30 days, so an abandoned release line can't stay deployed.
  - Where your org doesn't have Iron Bank's minor version, the replacement is a newer
    minor (31 image references), never an older one. Each case is listed in `docs/images.md`.
- **A supply chain you can verify.**
  - Every Chainguard image's signature is checked on every pull request.
  - The images this repo builds are signed keylessly, with an SBOM and SLSA provenance
    attached.
  - Every image is pinned by digest, so a cluster runs exactly what CI tested.
  - The installer adds a Kyverno policy that refuses unsigned images (enforce mode by
    default).
  - Each release archive is signed by the release workflow.
- **FIPS with one switch.** 177 of the 183 converted image references have a `-fips`
  twin; the other 6 are listed with the reason. `--fips` (or the FIPS overlay) turns
  them on.
  - Each kind test runs in both standard and FIPS mode ([docs/testing.md](docs/testing.md)
    lists the exceptions).
  - Kyverno, Grafana and Keycloak also pass on a RHEL 9 node whose kernel is in FIPS mode.
- **Minimal images.** Chainguard builds each image with only the application and what it
  needs to run. By Chainguard's design, the runtime images ship without a package manager,
  so there is less to patch and less to exploit.
  - For the 67 images compared above, the median Chainguard image, application
    included, is **83 MB** standard and 116 MB FIPS.
  - The median Iron Bank base image alone, before the application, is **107 MB**.
- **Tested on Kubernetes, not just rendered.**
  - **More than 35 runtime tests**, each with a functional check of the app: a Loki query, a
    Velero backup, a GitLab readiness check, and so on.
  - A **release-to-release upgrade test** on every change.
  - **More than 430 unit tests**.
- **Problems found and fixed before you hit them.** The runtime tests found **34 problems**
  that rendering alone would miss, each fixed in the overlays and listed in
  [docs/testing.md](docs/testing.md#what-the-runtime-tests-found). Examples:
  - an Istio sidecar still pulled from Iron Bank;
  - Gatekeeper refusing every non-Iron Bank image;
  - GitLab's migrations running out of PostgreSQL locks;
  - Thanos with no stores.
- **No fork, no lock-in.** Big Bang is unchanged. The repo writes values overlays, and
  [one command](docs/quickstart.md) installs, upgrades or rolls back.

> **Status: beta.** Use it for development, test and evaluation clusters; it isn't
> supported for production yet. Install from a pinned release, not `main`.
> **[Quickstart](docs/quickstart.md)** has the steps and what is and isn't proven yet.




## Use it with your org

The overlays in this repository point at Zapata Technology's Chainguard org. For your own
org, generate them once in your copy of the repository, then install as above:

```bash
python scripts/bb-run.py --org <your org> --build-registry ghcr.io/<owner> --save
```

[Running it on your own Chainguard org](docs/your-org.md) covers what your Chainguard plan
needs, building this repo's images into your registry, and CI for your copy.

## Object storage: SeaweedFS (default) or MinIO

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

### Deploying SeaweedFS (the default)

`bb-install.py install` does all of this. By hand:

```bash
kubectl create namespace seaweedfs
# 1. Pull credentials for cgr.dev, as for flux-system:
kubectl -n seaweedfs create secret docker-registry private-registry --docker-server=cgr.dev   --docker-username=<pull token id> --docker-password=<pull token secret>
# 2. S3 identities: SeaweedFS requires these keys, and the site file uses the same ones.
cat > s3.json <<'EOF'
{"identities": [{"name": "bigbang", "credentials": [{"accessKey": "<access key>", "secretKey": "<secret key>"}],
                 "actions": ["Admin", "Read", "List", "Tagging", "Write"]}]}
EOF
kubectl -n seaweedfs create secret generic seaweedfs-s3 --from-file=s3.json && rm s3.json
# 3. SeaweedFS (a PersistentVolumeClaim of 50Gi), plus a Job that creates every bucket:
kubectl apply -k seaweedfs/            # or: kubectl apply -k seaweedfs-fips/
kubectl -n seaweedfs wait job/seaweedfs-buckets --for=condition=complete --timeout=10m
```

Then add the overlay to the Big Bang install, and put the same keys in the site file:

```bash
helm upgrade -i bigbang ... -f overlays/values-chainguard.yaml [-f overlays/values-chainguard-fips.yaml]   -f overlays/values-object-storage.yaml -f my-site.yaml
```

`scripts/bb-object-storage.py --credentials-template site-object-storage.yaml` writes a
skeleton of every credential value the site file needs.
Keep only the entries for packages you enable: Big Bang creates Mimir's credential Secret
whenever its keys are set, even with Mimir off, and the install fails on the missing
`mimir` namespace.

What uses S3:

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

## Version compatibility

Tags follow one rule: the newest patch on the MAJOR.MINOR line that Iron Bank pins, and
never `latest`. Chainguard orgs carry Chainguard's *current* release lines, so sometimes
the org has nothing on Big Bang's line:

- **Newer minor ("MINOR AHEAD").** The newest release of the same MAJOR is used. Big Bang
  never tested that combination.
- **Newest release.** Images Iron Bank tags by date (MinIO) have no version line to match.

`docs/images.md` ends with a **Version differences** section listing every such image.
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
depending on anyone's custom build, a script converts **Iron Bank's own recipe**
(`hardening_manifest.yaml` and Dockerfile, pinned to a repo1 commit) into a build FROM
your org's images. Vault is the fourth: Chainguard publishes it, but Big Bang's
post-start hook needs curl, which the Chainguard image lacks, so `bb-vault.py` adds it.
The JMX exporter is the fifth: SonarQube's init container copies Iron Bank's jar with
sh and curl, and Chainguard's `jmx-exporter-iamguarded` has neither (Bitnami layout).
The Mattermost Operator, tempo-query, the MinIO Operator with its sidecar, and GitLab's
gitaly-init-cgroups are the sixth to tenth: Chainguard publishes no image of them, so
`bb-mattermost-operator.py`, `bb-tempo-query.py`, `bb-minio-operator.py`,
`bb-minio-operator-sidecar.py` and `bb-gitaly-init-cgroups.py` build them from upstream's
source:

| Iron Bank image | Script | Built FROM | Image |
|---|---|---|---|
| `big-bang/grafana/grafana-plugins`: Grafana with plugins baked in | `bb-grafana-plugins.py` | `grafana`, `grafana-fips` | `bigbang-grafana-plugins` |
| `big-bang/base`: bash, kubectl, yq and jq for Big Bang's upgrade, init and test jobs (33 value paths in ~25 packages) | `bb-base.py` | `kubectl-dev`, `kubectl-fips-dev` | `bigbang-base` |
| `big-bang/devops-tester`: Big Bang's test runner (kubectl, velero, kyverno, chainsaw, mc) | `bb-devops-tester.py` | `kubectl-dev`, `kubectl-fips-dev` | `bigbang-devops-tester` |
| `hashicorp/vault`: not Big Bang's own image, but its post-start hook needs curl, which Chainguard's vault lacks | `bb-vault.py` | `vault-dev`, `vault-fips-dev` | `bigbang-vault` |
| `opensource/prometheus/jmx-exporter`: SonarQube's init container copies its javaagent jar (`/opt/jmx_exporter`) with sh and curl | `bb-jmx-exporter.py` | `busybox-dev`, `busybox-fips-dev` | `bigbang-jmx-exporter` |
| `opensource/mattermost/mattermost-operator`: no Chainguard image; built from upstream's source at Iron Bank's tag | `bb-mattermost-operator.py` | build: `go-dev`, `go-fips-dev` (else `kubectl(-fips)-dev` + Wolfi Go); run: `glibc-openssl(-fips)` (else `kubectl(-fips)`) | `bigbang-mattermost-operator` |
| `opensource/grafana/tempo-query`: no Chainguard image; Tempo's Jaeger storage backend, built from grafana/tempo's source at Big Bang's tag | `bb-tempo-query.py` | as the Mattermost Operator | `bigbang-tempo-query` |
| `opensource/minio/operator` and `operator-sidecar` (both archived on Iron Bank): no Chainguard images; built from minio/operator's source (archived upstream) at Big Bang's tags | `bb-minio-operator.py`, `bb-minio-operator-sidecar.py` | as the Mattermost Operator | `bigbang-minio-operator`, `bigbang-minio-operator-sidecar` |
| `gitlab/gitlab-org/build/cng/gitaly-init-cgroups` (GitLab's CNG build on registry1; no Iron Bank recipe): Gitaly's cgroups init container, built from Gitaly's source as CNG builds it | `bb-gitaly-init-cgroups.py` | as the Mattermost Operator | `bigbang-gitaly-init-cgroups` |

You don't run these to install: CI (`builds.yml`) builds, tests, signs and pushes the
images when a change merges to main. The rest of this section is for maintainers.

Each script:

1. reads the tag Big Bang deploys
2. reads Iron Bank's recipe from [repo1](https://repo1.dso.mil/dsop):
   - the plugin zips and their sha256, which are checked on download
   - the package and tool versions
   - the non-root user
3. pins the org images it builds FROM by digest, on Iron Bank's MAJOR.MINOR line
4. writes `builds/<name>/build.yaml`, the input for the Dockerfile

```bash
python scripts/bb-base.py                           # regenerate builds/base/build.yaml
python scripts/bb-base.py --build --test            # build standard + FIPS, run the smoke test
python scripts/bb-base.py --build --test --push     # and push to BUILD_REGISTRY
```

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
- **tempo-query** is built the same way (shared helpers in `bbbuild.py`) from
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
  stage per image, `build.yaml` `target`) and `scripts/bbminio.py`. MinIO Operator is
  AGPL-3.0; the images carry its LICENSE and CREDITS.
- **gitaly-init-cgroups** has no Iron Bank recipe: Big Bang pulls GitLab's own CNG build
  from registry1. It is built as CNG builds it, from Gitaly's `tools/gitaly-init-cgroups`
  module at the Gitaly release of Big Bang's tag (v19.4.2-ubi: Gitaly v19.4.2), static,
  `/setup_cgroups` as the command, run as root (CNG's user; the chart runs it as root to
  chown the Gitaly pod's cgroup to uid 1000). Source and recipe come from gitlab.com
  (GitLab's archive of the module's directory, pinned by sha256). It runs only with
  `gitlab.gitaly.cgroups.enabled` (off in Big Bang). Its smoke test runs it as the chart
  does against a pod cgroup tree in a temporary directory.

**`BUILD_REGISTRY`** can't be `cgr.dev`, because Chainguard registries don't accept pushes.
**Leave it empty to keep Iron Bank's images.** The overlays then leave those values alone,
and verification accepts them.

**How images get published (`builds.yml` in CI):**

- Every change builds and smoke-tests the images.
- A merge to `main` pushes them to `BUILD_REGISTRY`. With `SIGN_IMAGES=keyless` each
  image is signed before its tag moves to it (pushed as `<tag>-staging`, signed, then
  tagged), then gets an SBOM and SLSA provenance. GHCR pushes are retried.
- The nightly job regenerates the build specs, so a new Grafana or kubectl patch, or a
  plugin added by Iron Bank, arrives as a pull request.

## How the conversion works

[docs/architecture.md](docs/architecture.md) has diagrams of the pipeline, the builds,
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
- `template_defaults`: images a template defaults with no value behind it (Big Bang's wait job).
- `post_renderers`: images hardcoded in a template. A Flux post-renderer rewrites them.
- `version_labels`: workload labels that must state the version actually deployed (istiod).
- `bash_wait_jobs`: wait jobs whose `#!/bin/sh` script is really bash. Iron Bank's UBI
  images run `sh` as bash; Chainguard's `-dev` images run busybox `sh`. A Flux
  post-renderer runs those scripts with bash (Kyverno policies' wait job).
- `bash_commands`: containers whose `/bin/sh -c` script is really bash; a post-renderer
  makes the command `/bin/bash` (Fortify's keystore job).
- `unpinned`: org images that stay on their exact tag, with why (Istio's injected proxy;
  a repository a package uses at two tags). `bb-verify.py` fails on any other unpinned image.
- `no_post_renderers`: packages whose Big Bang template can't take a post-renderer
  (Keycloak in 4.0.0); their images stay on tags instead of digests.
- `min_version` (on an image): an Iron Bank tag on an older line takes the newest release
  (kubectl `1.34`: Vault pins the end-of-life 1.30).
- `scan_reviewed`: what `scripts/bb-scan.py` found in the package charts and how each is
  handled. CI fails on a new finding.

An Iron Bank image with no Chainguard equivalent yet can be marked
`{stays: <reason>, until: <chainguard repo>}`. The conversion then reports when your org's
repo has tags, so you know when to switch it over.

[`docs/adding-a-package.md`](docs/adding-a-package.md) walks through mapping a new image.

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
  too: see [Upgrading from Big Bang 3](docs/install.md#upgrading-from-big-bang-3)).
  `install --plan` shows what would change (version, FIPS, packages, images) without
  changing anything, `status` says when the cluster is behind the release you run it
  from, and `rollback` returns Big Bang to an earlier revision.


`discovery/render-inputs.yaml` holds render-only placeholders (fake database and Redis
endpoints) so that GitLab and Anchore render during discovery. Never deploy with it.

## Known gaps

What this repo doesn't do yet, and what each needs. `docs/images.md` has the per-image detail.

| Gap | Why | What closes it |
|---|---|---|
| 19 images stay on Iron Bank (none in the default packages) | Vendor products (Twistlock, Fortify, Anchore Enterprise, ...), Red Hat UBI bases, and GitLab images Chainguard doesn't build | Chainguard publishing them: the nightly run reports each one (`until:`) when the org has tags |
| Release-to-release upgrades are tested for a few packages only | `compat/release-upgrade.yaml` upgrades Kyverno, Headlamp and Grafana from the newest release on every pull request; other packages' upgrades rely on Flux and Helm rolling new digests, which that test checks | More packages in that test as kind's runner allows; a staging cluster that follows releases |
| Keycloak's images stay on tags, not digests | Big Bang 4.0.0's Keycloak template can't take a post-renderer (`no_post_renderers`) | A Big Bang release that fixes the template; `bb-scan.py` reports when it does |
| Images in custom resources stay on tags | Kiali's CR and Istio's injected proxy are set outside pod specs, where kustomize can't pin | Nothing planned: their tags are exact |
| Some images are a newer version than Iron Bank's | The org keeps only current release lines (`MINOR AHEAD`, `min_version`) | Each is listed in `docs/images.md`; packages with a compat test run them |
| Iron Bank's side of the weekly CVE report is empty, and the Vault 1.21 to 2.x upgrade test (`compat/vault-upgrade.yaml`) is skipped | Both need an Iron Bank account | The `REGISTRY1_USERNAME` and `REGISTRY1_PASSWORD` secrets |
| Some images have open Critical CVEs | See [SECURITY.md](SECURITY.md#known-vulnerabilities) | Chainguard rebuilds, or a rebuild of the images built here |

## Contributing and support

- Getting started: [`docs/quickstart.md`](docs/quickstart.md) (status, install from a release, upgrade).
- Changes are welcome as pull requests: see [CONTRIBUTING.md](CONTRIBUTING.md).
- Security issues and the controls in place: see [SECURITY.md](SECURITY.md).
- For the Big Bang team: [`docs/for-big-bang.md`](docs/for-big-bang.md).
- How it's tested, and what the runtime tests found: [`docs/testing.md`](docs/testing.md).

## License

[Apache License 2.0](LICENSE). Copyright 2026 Zapata Technology; see [NOTICE](NOTICE).
