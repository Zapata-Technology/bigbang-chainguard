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
for b in grafana-plugins base devops-tester vault; do
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
  with cosign keyless and attaches an SBOM. The signatures go to the public Sigstore log.

## Deploy

The overlays in this repo were generated for `zapatatechnology.com` (each file's header says
which org). Regenerate them for yours with `bb-run.py` before deploying.

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

The site file supplies pull credentials. Big Bang turns them into the `private-registry`
secret in every package namespace. Never commit them:

```yaml
registryCredentials:
  - registry: cgr.dev
    username: <Chainguard pull token id>
    password: <Chainguard pull token secret>
  - registry: ghcr.io                 # your BUILD_REGISTRY, if it is private
    username: <GitHub user>
    password: <read:packages token>
  - registry: registry1.dso.mil       # only if a package in docs/images.md "Stays on Iron Bank" is enabled
    username: <Iron Bank user>
    password: <Iron Bank CLI secret>
```

Create a pull token with `chainctl auth pull-token --repository=cgr.dev --parent=<org>`.
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
post-start hook needs curl, which the Chainguard image lacks, so `bb-vault.py` adds it:

| Iron Bank image | Script | Built FROM | Image |
|---|---|---|---|
| `big-bang/grafana/grafana-plugins`: Grafana with plugins baked in | `bb-grafana-plugins.py` | `grafana`, `grafana-fips` | `bigbang-grafana-plugins` |
| `big-bang/base`: bash, kubectl, yq and jq for Big Bang's upgrade, init and test jobs (33 value paths in ~25 packages) | `bb-base.py` | `kubectl-dev`, `kubectl-fips-dev` | `bigbang-base` |
| `big-bang/devops-tester`: Big Bang's test runner (kubectl, velero, kyverno, chainsaw, mc) | `bb-devops-tester.py` | `kubectl-dev`, `kubectl-fips-dev` | `bigbang-devops-tester` |
| `hashicorp/vault`: not Big Bang's own image, but its post-start hook needs curl, which Chainguard's vault lacks | `bb-vault.py` | `vault-dev`, `vault-fips-dev` | `bigbang-vault` |

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

- **grafana-plugins:** Grafana loads every plugin with a valid signature.
- **base and devops-tester:** the uid, and each tool at Iron Bank's version line.
- **Every `-fips` image:** OpenSSL defaults to `fips=yes`.

Each image also has a `-fips` twin.

Differences from Iron Bank:

- **base and devops-tester run on the `-dev` image**, because Big Bang's jobs need a shell.
  Tools are in `/usr/bin`, and no Big Bang chart calls them by full path. Iron Bank's
  `CCE-*.sh` RHEL STIG scripts don't apply to Wolfi, and `build.yaml` lists them as not
  ported.
- **Tags:** base and devops-tester are tagged `<Iron Bank tag>-kubectl<version>`, so a
  kubectl patch gets a new tag.
- **chainsaw** (devops-tester) has no Chainguard package. It is copied from upstream at
  the digest Iron Bank pins, as Iron Bank does, and is not FIPS-built.

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

## Contributing and support

- Changes are welcome as pull requests: see [CONTRIBUTING.md](CONTRIBUTING.md).
- Security issues and the controls in place: see [SECURITY.md](SECURITY.md).
- For the Big Bang team: [`docs/for-big-bang.md`](docs/for-big-bang.md).
- How it's tested, and what the runtime tests found: [`docs/testing.md`](docs/testing.md).

## License

[Apache License 2.0](LICENSE). Copyright 2026 Zapata Technology; see [NOTICE](NOTICE).
