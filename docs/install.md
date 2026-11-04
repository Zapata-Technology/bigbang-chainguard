# Installing and upgrading: the details

The [README](../README.md#install) has the short path: `check`, `install`, `upgrade`. This
page has everything behind it: what each step does, the options, the logins, and how to
do it by hand.

The overlays in this repository were generated for `zapatatechnology.com` (each file's
header says which org), and its releases point at Zapata's private org and build registry.
For another org, generate them first ([your own Chainguard org](your-org.md); the quickstart's
[Using this outside Zapata Technology](quickstart.md#using-this-outside-zapata-technology)
has the steps).

## The installer

```bash
chainctl auth login                          # once; the installer makes the pull token with it
python scripts/bb-install.py check           # tests every setting; changes nothing
python scripts/bb-install.py install         # add --fips for FIPS images
python scripts/bb-install.py status          # any time: what's healthy, and the app URLs
python scripts/bb-install.py upgrade         # a newer release: shows the changes, asks, installs
```

**`check`** runs everything `install` checks and changes nothing:

- tools: kubectl and helm;
- the cluster: reachable, Kubernetes 1.35 or newer, a default StorageClass when SeaweedFS
  runs in it, and an upgrade path from the Big Bang it runs now (Big Bang 4 upgrades from
  3.35.1 only, and never to an older major);
- the site file: a domain, packages that need each other (monitoring and Thanos need
  `prometheusOperatorCRDs` and `certManager`), and a full `helm template` of Big Bang with
  the overlays and your site file, so the chart's own schema checks every key and value;
- images: a pull test of every image the enabled packages deploy from your Chainguard org
  and the build registry, with your logins (the environment, the site file, an earlier
  install's, or a short-lived token from your chainctl login; none is stored). A refused
  pull from the org is usually an image your Chainguard plan doesn't cover.

It exits non-zero and lists each problem, so it also works in a pipeline. `install` runs
the same checks before it changes anything.

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
2. **Preflight:** kubectl and helm, Kubernetes 1.35+, a default StorageClass for SeaweedFS,
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
(for example `4.0.0-cg.2`) with an archive to install from and notes listing the image
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

## Registry logins

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

Then restrict who can read Secrets ([SECURITY.md](../SECURITY.md), "Credentials in the cluster").

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

## Vulnerabilities

`cve-report.yml` runs weekly (and on demand): `scripts/bb-cve.py` scans each Iron Bank image
Big Bang deploys and the Chainguard image that replaces it with grype, and writes totals and
per-image counts by severity, image sizes and a distinct-image summary to the run summary and
an artifact. Iron Bank's side is scanned directly with the `REGISTRY1_USERNAME` and
`REGISTRY1_PASSWORD` secrets (an Iron Bank account). Without them it scans the public base each
Iron Bank image's public recipe builds FROM, as a floor (`--ironbank-bases`).

## FIPS notes

- The FIPS overlay swaps each image for its `-fips` twin. Images with no FIPS variant keep
  the standard image, and `docs/images.md` lists them.
- Keycloak FIPS (BC-FIPS) needs an admin password of at least 14 characters and BCFKS
  keystores. Set those in the site's `packages.keycloak.values` before switching.
- Switching an existing deployment is `bb-install.py install --fips` (and back without `--fips`).

## GitLab and GitLab Runner notes

The kind tests found these; each applies to a real cluster too.

- **PostgreSQL:** GitLab loads its schema in one transaction. With PostgreSQL's default
  `max_locks_per_transaction` (64) the migrations fail with `out of shared memory` and
  GitLab never starts. Raise it on your external PostgreSQL (the test uses 256).
- **GitLab Runner needs Kyverno.** Big Bang hands the runner GitLab's registration token
  with a Kyverno policy (`autoRegister`). Kyverno is on by default; if you turn it off, give
  the runner a token yourself, or it logs `The registration-token needs to be entered`
  while its pod still shows Ready.
- **Runner home directory:** handled for you. The overlays set `HOME=/home/gitlab-runner`,
  the directory the chart mounts; the Chainguard image's user has another home, and the
  runner crashed writing `/.gitlab-runner` without it.
- **Small or FIPS clusters:** the migrations can take longer than Kubernetes' 10-minute
  rollout deadline, and Rails longer than the liveness probe's ~3 minutes to boot. If
  Helm stops with `failed early due to stalled resources` or the probe keeps restarting
  webservice and sidekiq, raise `progressDeadlineSeconds` (through
  `packages.gitlab.postRenderers`, after the overlays' digest pins) and
  `upstream.gitlab.webservice.deployment.livenessProbe` /
  `upstream.gitlab.sidekiq.livenessProbe` `failureThreshold`, as `compat/gitlab.yaml` does.

## Other package notes

- **Gatekeeper:** its `allowed-docker-registries` constraint admits only Iron Bank's
  registries by default. The overlays extend it, like Kyverno's `restrict-image-registries`,
  with your org, `BUILD_REGISTRY` and Iron Bank's registries (for the documented leftovers).
  A site that sets its own list must keep those.
- **Thanos:** the overlays run Thanos Query with `--store.sd-dns-resolver=golang`. Its
  default resolver found no stores on nodes with a DNS search domain (Azure and AKS add
  one), so Query answered with no data while its pods were Ready.

## Upgrading from Big Bang 3

Big Bang 4.0 is a breaking release, and this repo follows it: `main` and every `4.0.0-cg.<n>`
release are for Big Bang 4. Releases `3.35.1-cg.<n>` stay on the Releases page for clusters
still on Big Bang 3.

What changed for a site:

- **Site file keys.** Every package moved under `packages:`. `tempo:` is now
  `packages.tempo:`, `addons.keycloak:` is `packages.keycloak:`, and so on. Big Bang 4's
  chart refuses the old keys. `bb-install.py upgrade` converts the site file for you, and
  moves a package's `values.istio`, `values.networkPolicies` and `values.routes` under
  `values.bb-common`, as Big Bang's own `scripts/migrate-values-3-to-4.sh --target 4` does.
  Use Big Bang's script instead for SOPS-encrypted values, HelmRelease patches or legacy
  hardening settings, which the installer doesn't convert.
- **Kubernetes 1.35 or newer.** The chart won't install on an older cluster.
- **Istio ambient mode is the default.** Big Bang 4 turns on ztunnel, Istio CNI and the
  Gateway API with istiod, instead of sidecars. The overlays convert those images like the
  rest. To keep sidecars, set `istio.ambient.enabled: false` in the site file.
- **Kyverno policies are CEL policies** (`ValidatingPolicy`, `kubectl get
  validatingpolicies.policies.kyverno.io`), not `ClusterPolicy`. The overlays' registry allow
  list is at the same value path. Custom policy settings need Big Bang's
  `docs/migration/migrating-kyverno-policies-for-bb4.0.md`.
- **Tempo 3.** Big Bang 4 deploys Tempo 3.x. The Tempo 2 workaround for the `opencensus`
  receiver is gone with it: the Tempo 3 chart no longer lists that receiver.

The order Big Bang requires, which the installer enforces:

1. Upgrade the cluster to Big Bang 3.35.1 with a `3.35.1-cg.<n>` release, and check it.
2. Make sure the cluster runs Kubernetes 1.35 or newer.
3. From a `4.0.0-cg.<n>` release, run `python scripts/bb-install.py upgrade`. It converts
   `my-site.yaml` to Big Bang 4's keys (the original is kept as `my-site.yaml.bigbang3`),
   checks every setting, shows what will change and asks. Big Bang then moves to 4.0 with
   ambient mode on, and Flux rolls each package.

`upgrade` refuses a cluster on any Big Bang 3 other than 3.35.1, and names the release to
install first. `compat/release-upgrade.yaml` goes through steps 1 and 3 on kind for every
pull request: it installs the newest 3.35.1 release with Big Bang 3's keys, then upgrades
to this tree.
