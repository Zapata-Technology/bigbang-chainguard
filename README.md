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

## What you need

- **A Chainguard subscription with the images Big Bang uses.**
  - [`docs/required-images.md`](docs/required-images.md) lists every repository and which
    packages use it. It is generated and the same for every org: about 90 standard images,
    plus their `-fips` twins if you want FIPS.
  - If you enable fewer packages, you need fewer images.
  - `scripts/bb-convert.py --missing` lists the ones your org lacks.
- **A registry you can push to** (e.g. `ghcr.io/<owner>`). Three of Big Bang's images are
  Big Bang's own builds, and Vault needs curl added; this repo builds those FROM your
  Chainguard images; see
  [Images this repo builds](#images-this-repo-builds). This is optional: without a
  registry, those images stay on Iron Bank.
- **Iron Bank credentials, for the images with no Chainguard equivalent.** These are
  vendor products and the GitLab image set. [`docs/images.md`](docs/images.md) lists them
  under "Stays on Iron Bank", with the reasons.
- **Tools:** `helm` 3, Python 3.12 with `pyyaml`, and `chainctl`.

## Use it with your org

```bash
pip install pyyaml
chainctl auth login --audience cgr.dev        # a login that can pull from your org
python scripts/bb-run.py --org <your org> --build-registry ghcr.io/<owner> --save
```

`bb-run.py` runs the whole pipeline:

1. fetches Big Bang
2. discovers images
3. stops if your org is missing repositories, and lists them
4. generates the build specs
5. writes the overlays
6. verifies them, standard and FIPS

`--save` writes the org and registry into the `CHAINGUARD_ORG` and `BUILD_REGISTRY` files,
so later runs need no flags. Then build and push this repo's images:

```bash
for b in grafana-plugins base devops-tester vault jmx-exporter; do
  python scripts/bb-$b.py --no-generate --build --test --push
done
```

Each setting is read in this order: the command-line flag (`--org`, `--build-registry`),
then the environment variable or GitHub repository variable (`CHAINGUARD_ORG`,
`BUILD_REGISTRY`), then the file of the same name at the repo root.

**Your own exceptions go in `orgs/<org>.yaml`.** It is applied over `mapping.yaml` for your
org only. Each `images` entry there replaces the shared entry for that Iron Bank image. Use
it for things like keeping a package you don't license on Iron Bank, or using an image you
already build. `orgs/zapatatechnology.com.yaml` is an example.

### CI for your copy

- `CGR_ACTIONS_ID` secret: an assumable Chainguard identity in your org with
  `registry.pull`, trusted for your repo's `main` branch and pull requests.
- `CHAINGUARD_ORG` and `BUILD_REGISTRY` repository variables, unless they're in the files.
- `CHAINGUARD_ORG_ID` repository variable (`chainctl iam organizations list`): the signature
  check then requires Chainguard's signature under your org.
- `RELEASE_PAT` secret (optional): lets CI run on the nightly bot's pull requests.
- `SIGN_IMAGES=keyless` repository variable (optional): signs the images this repo pushes
  with cosign keyless (in both cosign's bundle format and the classic `.sig` one Kyverno
  reads) and attaches an SPDX SBOM and SLSA provenance (`scripts/bb-provenance.py`: the run,
  the commit and every pinned input). The signatures go to the public Sigstore log.

## Deploy

The overlays in this repo were generated for `zapatatechnology.com` (each file's header says
which org). Regenerate them for yours with `bb-run.py` before deploying.

### The installer (recommended)

```bash
chainctl auth login                          # once; the installer makes the pull token with it
python scripts/bb-install.py install         # add --fips for FIPS images
python scripts/bb-install.py status          # any time: what's healthy, and the app URLs
```

The first `install` asks for your domain and packages and writes `my-site.yaml` (or pass
`--domain` and `--enable`; `init` does only that step). Later runs read it; `--site` names
another file.

**The images this repo builds are private** when the build registry is (GHCR packages are
by default), so the first install asks for a login that can read them, with the password
hidden. The installer checks that it can pull every one of them before changing the
cluster, and keeps the login in the cluster for later runs. See
[Registry logins](#registry-logins) for what kind of login to give it.

You need kubectl, helm, Python 3 with PyYAML, and a kubectl context for the cluster. No
Iron Bank account is needed unless a package you enable keeps an Iron Bank image. Without
chainctl, give the installer a pull token in `CGR_USERNAME` and `CGR_PASSWORD` instead
(`chainctl auth pull-token create --repository=oci --parent=<org> --ttl=8760h` on another
machine).

`install` does every step below for you, in order, and is safe to re-run:

1. **Chart:** Big Bang's chart at the overlays' version, from repo1's public git archive
   (registry1's OCI copy needs an Iron Bank login). `--chart` takes a local chart or an
   `oci://` ref instead.
2. **Preflight:** kubectl and helm, Kubernetes 1.34+, a default StorageClass for SeaweedFS,
   the site file's domain, and which of your packages still need Iron Bank credentials. On a
   first install it shows the kubectl context and asks before touching it.
3. **Credentials:** the pull token from the environment, the site file, or the cluster (a
   previous run); failing those, a new one-year token made with your chainctl login (it asks
   first; `--yes` doesn't). Kept in the cluster's `bigbang/bb-install` Secret so later runs
   don't ask, never printed or put on a command line. `status` warns 30 days before a token
   it made expires; `install --new-pull-token` replaces it.
4. **Flux**, with its pull secret.
5. **SeaweedFS**, with random S3 keys generated once and reused, its two Secrets, and the
   bucket Job (skipped with `OBJECT_STORAGE=minio`).
6. **Big Bang**, with the overlays in the right order. Object-storage credentials go only to
   the packages you enabled. Keycloak gets random admin and database passwords long
   enough for FIPS (Big Bang's defaults are too short), unless your site file sets them;
   the database one only on a fresh Keycloak, since PostgreSQL keeps the password it
   started with. Read the admin one with
   `kubectl -n bigbang get secret bb-install -o jsonpath='{.data.keycloak-admin-password}' | base64 -d`.
7. **TLS** for `*.<domain>` on the public gateway (below).
8. **Wait and report:** every HelmRelease and pod, any running image that isn't the
   conversion's, the gateway addresses to point DNS at, and each app's URL.

Re-run `install` to upgrade after pulling new overlays, to switch to FIPS (`--fips`) or back,
or to apply a changed site file. `uninstall` removes Big Bang, SeaweedFS and Flux; SeaweedFS's
volume, the stored keys and a pull token the installer made stay unless you add
`--delete-data`. `compat/installer*.yaml` run the installer end to end on kind in CI.

**TLS.** Big Bang's public gateway serves the Secret `istio-gateway/public-cert`, which
Big Bang doesn't create on its own. The installer picks one way and remembers it:

| Option | What happens |
|---|---|
| (nothing) | A self-signed CA made by cert-manager issues `*.<domain>`. `status` prints how to save the CA to trust it. Good for test sites. |
| `--tls-issuer <ClusterIssuer>` | Your cert-manager ClusterIssuer (ACME/Let's Encrypt, Vault, a company CA) issues it; cert-manager renews it. |
| `--tls-cert cert.pem --tls-key key.pem` | Your certificate, passed to Big Bang's `gatewayCerts`. Kept in the cluster Secret, not in a file. |
| `gatewayCerts` in the site file | Left alone. |

**Releases.** Each change to what gets deployed is released as `<Big Bang version>-cg.<n>`
(for example `3.35.1-cg.2`) with an archive to install from and notes listing the image
changes. Install from a release rather than `main` to know exactly what you run; `install`
prints the release it deploys. Each archive is signed keylessly by the release workflow; the
release notes show the `cosign verify-blob` command that checks it.

**Pinned images.** Every Chainguard image is pinned to the digest its tag had when the
overlays were generated, so a cluster runs exactly what CI tested even though Chainguard
rebuilds images under the same tag. New digests arrive in the nightly pull request, which
deploys them on kind before it merges.

**Image signatures.** With Kyverno on, the installer adds a Kyverno policy, in enforce mode
unless you pass `--verify-signatures audit` or `off`, with the same signer rules CI's
`bb-signatures.py` uses:

- every `cgr.dev/<org>` image a Pod runs needs Chainguard's keyless signature (add
  `--org-id <id>`, from `chainctl iam organizations list`, to require your org's);
- every image this repo builds (`<build registry>/bigbang-*`) needs the keyless signature
  `builds.yml` makes on `main` (with `SIGN_IMAGES=keyless`): from the repository
  `--build-signer <owner/repo>` names (remembered; or `$BUILD_SIGNER`), else from any
  repository of the `ghcr.io/<owner>` account.

Enforce refuses a Pod whose image fails; audit only records it in Kyverno's policy reports.
A cluster keeps the mode it was first installed with until you pass the flag. If your
copy doesn't sign its builds, pass `--verify-signatures audit`. The cluster needs to reach
rekor.sigstore.dev and Sigstore's TUF mirror. Iron Bank leftovers aren't covered.

**Credentials in the cluster.** The installer keeps the pull token, S3 keys and generated
passwords in the Secret `bigbang/bb-install`: restrict who can read Secrets there with RBAC
and turn on encryption at rest for Secrets (SECURITY.md).

**Installer image.** Instead of installing kubectl, helm and Python, run the installer from
its image (Chainguard's kubectl -dev image plus the tools; Big Bang's chart is inside).
`scripts/bb-docker.sh` picks the image this checkout built and runs any `bb-install.py`
command in it, with your kubeconfig and the current directory (for `my-site.yaml`):

```bash
docker login ghcr.io                          # if the installer image is private
scripts/bb-docker.sh install                  # BB_INSTALLER_FIPS=1 for the -fips image
```

The image has no chainctl, so the first install asks for a cgr.dev pull token
(`chainctl auth pull-token create --repository=oci --parent=<org> --ttl=8760h` on a
machine that has it) as well as the build registry login.

A kubeconfig that logs in through a plugin (aws, az, gke-gcloud-auth-plugin) needs a
self-contained context instead (`kubectl config view --minify --flatten`).

#### Registry logins

The installer asks for each registry login it needs, with the password hidden, and stores
it in the cluster, so you type it once per cluster. It tests every login with a pull of an
image the overlays deploy (each image it builds, one from cgr.dev) before changing the
cluster: a login typed at the prompt that can't pull is asked for again, up to three times,
and one from the environment, the site file or an earlier run that can't stops the install
with the reason. Images that pull without a login aren't asked about. For the build
registry, a token alone is enough: give it as the password and press Enter at the username
(or set `BUILD_REGISTRY_TOKEN`). `--no-pull-check` skips the tests (below).

Prefer that prompt to environment
variables: an `export NAME=<token>` line lands in your shell history and every program you
start from that shell inherits it. Use the variables (`CGR_*`, `BUILD_REGISTRY_*` or `BUILD_REGISTRY_TOKEN`,
`REGISTRY1_*`) when nothing can answer a prompt, as in a pipeline, and fill them from your
secret store or `read -rs`, not a literal on the command line:

```bash
read -rs BUILD_REGISTRY_TOKEN && export BUILD_REGISTRY_TOKEN
```

A variable replaces a login stored by an earlier run (that's how you rotate one).

Every login ends up in the cluster for as long as Big Bang runs: in the `bigbang/bb-install`
Secret, and in a `private-registry` Secret in each package namespace. So give it the least
it needs:

- **Build registry on ghcr.io:** a token for a dedicated bot account that only has read
  access to the `bigbang-*` packages, not your own token. GHCR pulls need a classic token,
  which reaches every package its account can see; give it only `read:packages` and an
  expiry date.
- **cgr.dev:** a pull token (what the installer makes with your chainctl login), which can
  only pull, and expires after a year.
- **No long-lived token at all:** push the images this repo builds to a registry the nodes
  reach with their own cloud identity (ECR, ACR, Artifact Registry) by setting
  `BUILD_REGISTRY` to it (skip the login at the prompt, and add `--no-pull-check`: the
  workstation can't pull what only the nodes may), or copy every image into a site registry and install with
  `--registry-mirror` (**Disconnected sites** below).

Then restrict who can read Secrets ([SECURITY.md](SECURITY.md), "Credentials in the cluster").

**Upgrades.** `install --plan` (same flags) shows what a run would change and stops: the
Big Bang version, standard to FIPS or back, packages turned on or off, and each image tag
that moves. `rollback` returns the release to the previous revision (or `--to N`) and waits;
Flux then downgrades the packages, which a package whose data has moved forward (a
database migration) may not accept, so check `--plan` before upgrading.

**When repo1 doesn't answer.** Flux fetches every package's chart from repo1.dso.mil git
while it installs and upgrades. The installer names any source that can't fetch, asks Flux
to retry it every two minutes while it waits, and ends a timeout with the host that failed.
`--git-mirror https://git.example.com/bigbang` takes Big Bang's chart and every package from
a git server you run with repo1's paths under that URL (kept in sync with repo1, for
example by a scheduled `git fetch` of each package repository); the mirror is remembered.

**Disconnected sites.** On a connected machine, `scripts/bb-airgap.py --site my-site.yaml
[--fips] --out bundle/` gathers what the site's packages need: this checkout with Big Bang's
chart and Flux rendered to plain manifests, a bare git clone of Big Bang and of each enabled
package at its pinned tag, and `images.txt` (every image the install runs, as `<source>
<name:tag>`). It doesn't copy images: moving them into the site's registry is the site's
step, with its own tooling and under its Chainguard license. Inside the site:

```bash
./push-git.sh https://git.site.example/bigbang      # the package repositories, repo1's paths
python3 bigbang-chainguard/scripts/bb-install.py install --site my-site.yaml   --git-mirror https://git.site.example/bigbang --registry-mirror registry.site.example/bigbang
```

`--registry-mirror` points every Chainguard and built image (and Kyverno's allow list) at the
site registry, takes its credentials from `MIRROR_USERNAME`/`MIRROR_PASSWORD` if it needs
them, and is remembered. The default packages need 54 images and 17 repositories; packages
that keep Iron Bank images need those too (`bundle.json` lists them). Signature checks need
Sigstore's public services, so leave `--verify-signatures` off inside a disconnected site.

**GitOps.** `install --gitops DIR` does everything except the `helm` step: it writes `DIR`
(Big Bang's HelmRelease, its chart from repo1, and the values files as ConfigMaps) and
applies it. Commit `DIR` to your own git repository and point a Flux Kustomization at it
(`DIR/README.md` has the YAML); the credentials stay in the cluster Secret
`bigbang/bb-install-values`, out of git.

### Vulnerabilities

`cve-report.yml` runs weekly (and on demand): `scripts/bb-cve.py` scans each Iron Bank image
Big Bang deploys and the Chainguard image that replaces it with grype, and writes totals and
per-image counts by severity to the run summary and an artifact. Iron Bank's side needs the
`REGISTRY1_USERNAME` and `REGISTRY1_PASSWORD` secrets (an Iron Bank account).

### Doing it by hand

What the installer does, as commands:

```bash
BB=$(cat BIGBANG_VERSION)

# Flux (standard or FIPS)
kubectl apply --server-side -k flux/            # or: kubectl apply --server-side -k flux-fips/

# Object storage (OBJECT_STORAGE=seaweedfs, the default): SeaweedFS first. The two
# Secrets it needs are in "Object storage" below.
kubectl apply -k seaweedfs/                     # or: kubectl apply -k seaweedfs-fips/

# Big Bang, FIPS off
helm upgrade -i bigbang oci://registry1.dso.mil/bigbang/bigbang --version "$BB" -n bigbang --create-namespace \
  -f overlays/values-chainguard.yaml -f overlays/values-object-storage.yaml -f my-site.yaml

# Big Bang, FIPS on
helm upgrade -i bigbang oci://registry1.dso.mil/bigbang/bigbang --version "$BB" -n bigbang --create-namespace \
  -f overlays/values-chainguard.yaml -f overlays/values-chainguard-fips.yaml \
  -f overlays/values-object-storage.yaml -f my-site.yaml
```

With `OBJECT_STORAGE=minio`, skip `seaweedfs/` and `values-object-storage.yaml`: Big Bang
deploys MinIO itself.

The `oci://registry1.dso.mil` chart needs `helm registry login registry1.dso.mil` first.
Without an Iron Bank account, use the chart from repo1's git archive
(`https://repo1.dso.mil/big-bang/bigbang/-/archive/$BB/bigbang-$BB.tar.gz?path=chart`,
unpacked; pass its `chart/` directory instead of the `oci://` ref and drop `--version`).

The site file supplies pull credentials. Big Bang turns them into the `private-registry`
secret in every package namespace. Never commit them:

```yaml
registryCredentials:
  - registry: cgr.dev
    username: <Chainguard pull token id>
    password: <Chainguard pull token secret>
  - registry: ghcr.io                 # your BUILD_REGISTRY, if it is private
    username: <read-only bot account>
    password: <its read:packages token>
  - registry: registry1.dso.mil       # only if a package in docs/images.md "Stays on Iron Bank" is enabled
    username: <Iron Bank user>
    password: <Iron Bank CLI secret>
```

Create a pull token with
`chainctl auth pull-token create --repository=oci --parent=<org> --ttl=8760h` (a year, the
maximum).
Flux needs the same `private-registry` secret in `flux-system` before `kubectl apply -k flux/`.

The overlay also adds `cgr.dev/<org>` and your `BUILD_REGISTRY` to Big Bang's Kyverno
`restrict-image-registries` policy. Without that, Kyverno blocks every pod.

### FIPS notes

- The FIPS overlay swaps each image for its `-fips` twin. Images with no FIPS variant keep
  the standard image, and `docs/images.md` lists them.
- Keycloak FIPS (BC-FIPS) needs an admin password of at least 14 characters and BCFKS
  keystores. Set those in the site's `addons.keycloak.values` before switching.
- Switching an existing deployment is an in-place `helm upgrade` with the FIPS overlay added.

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

## Images this repo builds

Big Bang ships three images of its own that Chainguard doesn't publish. Instead of
depending on anyone's custom build, a script converts **Iron Bank's own recipe**
(`hardening_manifest.yaml` and Dockerfile, pinned to a repo1 commit) into a build FROM
your org's images. Vault is the fourth: Chainguard publishes it, but Big Bang's
post-start hook needs curl, which the Chainguard image lacks, so `bb-vault.py` adds it.
The JMX exporter is the fifth: SonarQube's init container copies Iron Bank's jar with
sh and curl, and Chainguard's `jmx-exporter-iamguarded` has neither (Bitnami layout):

| Iron Bank image | Script | Built FROM | Image |
|---|---|---|---|
| `big-bang/grafana/grafana-plugins`: Grafana with plugins baked in | `bb-grafana-plugins.py` | `grafana`, `grafana-fips` | `bigbang-grafana-plugins` |
| `big-bang/base`: bash, kubectl, yq and jq for Big Bang's upgrade, init and test jobs (33 value paths in ~25 packages) | `bb-base.py` | `kubectl-dev`, `kubectl-fips-dev` | `bigbang-base` |
| `big-bang/devops-tester`: Big Bang's test runner (kubectl, velero, kyverno, chainsaw, mc) | `bb-devops-tester.py` | `kubectl-dev`, `kubectl-fips-dev` | `bigbang-devops-tester` |
| `hashicorp/vault`: not Big Bang's own image, but its post-start hook needs curl, which Chainguard's vault lacks | `bb-vault.py` | `vault-dev`, `vault-fips-dev` | `bigbang-vault` |
| `opensource/prometheus/jmx-exporter`: SonarQube's init container copies its javaagent jar (`/opt/jmx_exporter`) with sh and curl | `bb-jmx-exporter.py` | `busybox-dev`, `busybox-fips-dev` | `bigbang-jmx-exporter` |

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

**`BUILD_REGISTRY`** can't be `cgr.dev`, because Chainguard registries don't accept pushes.
**Leave it empty to keep Iron Bank's images.** The overlays then leave those values alone,
and verification accepts them.

**How images get published (`builds.yml` in CI):**

- Every change builds and smoke-tests the images.
- A merge to `main` pushes them to `BUILD_REGISTRY` (signed when `SIGN_IMAGES=keyless`).
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
  (Keycloak in 3.35.1); their images stay on tags instead of digests.
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
  recipes, and opens a PR.
- **New Big Bang release**: the same job bumps `BIGBANG_VERSION` and re-runs discovery.
  If the release adds an image that `mapping.yaml` doesn't cover, CI fails on that PR and
  names it. Add the mapping (or a `stays` entry with the reason) and push to the bot branch.
- **CI** (`ci.yml`): unit tests, then discovery, conversion and verification for both modes.

`discovery/render-inputs.yaml` holds render-only placeholders (fake database and Redis
endpoints) so that GitLab and Anchore render during discovery. Never deploy with it.

## Known gaps

What this repo doesn't do yet, and what each needs. `docs/images.md` has the per-image detail.

| Gap | Why | What closes it |
|---|---|---|
| 28 images stay on Iron Bank (none in the default packages) | Vendor products (Twistlock, Fortify, Anchore Enterprise, ...), Red Hat UBI bases, and GitLab images Chainguard doesn't build | Chainguard publishing them: the nightly run reports each one (`until:`) when the org has tags |
| GitLab's kind test doesn't finish | GitLab, its PostgreSQL and Redis need more than the 2 CPUs and 7 GB of a private repository's runner; it times out with nothing crashing | A public repository (4 CPUs, 16 GB runners) or a larger runner group. Render and signature checks cover GitLab meanwhile |
| Keycloak's images stay on tags, not digests | Big Bang 3.35.1's Keycloak template can't take a post-renderer (`no_post_renderers`) | A Big Bang release that fixes the template; `bb-scan.py` reports when it does |
| Images in custom resources stay on tags | Kiali's CR and Istio's injected proxy are set outside pod specs, where kustomize can't pin | Nothing planned: their tags are exact |
| Some images are a newer version than Iron Bank's | The org keeps only current release lines (`MINOR AHEAD`, `min_version`) | Each is listed in `docs/images.md`; packages with a compat test run them |
| Iron Bank's side of the weekly CVE report is empty | It needs an Iron Bank account | The `REGISTRY1_USERNAME` and `REGISTRY1_PASSWORD` secrets |
| Some images have open Critical CVEs | See [SECURITY.md](SECURITY.md#known-vulnerabilities) | Chainguard rebuilds, or a rebuild of the images built here |

## Contributing and support

- Changes are welcome as pull requests: see [CONTRIBUTING.md](CONTRIBUTING.md).
- Security issues and the controls in place: see [SECURITY.md](SECURITY.md).
- For the Big Bang team: [`docs/for-big-bang.md`](docs/for-big-bang.md).
- How it's tested, and what the runtime tests found: [`docs/testing.md`](docs/testing.md).

## License

[Apache License 2.0](LICENSE). Copyright 2026 Zapata Technology; see [NOTICE](NOTICE).
