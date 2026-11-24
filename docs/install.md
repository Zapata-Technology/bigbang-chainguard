# Installing and upgrading: the details

The [README](../README.md#install) has the short path: `check`, `install`, `upgrade`. This
page has everything behind it: what each step does, the options, the logins, and
[how to do it by hand](#doing-it-by-hand). Something went wrong: [troubleshooting](troubleshooting.md).

The overlays in a copy of this repository are generated for one Chainguard org (each file's
header says which), and its releases point at that org and its build registry. For another
org, generate them first ([your own Chainguard org](your-org.md); the quickstart's
[Using it with your own org](quickstart.md#using-it-with-your-own-org) has the steps).

## The installer

```bash
chainctl auth login                          # once; the installer makes the pull token with it
python scripts/bb-install.py check           # tests every setting; changes nothing
python scripts/bb-install.py install         # add --fips for FIPS images
python scripts/bb-install.py status          # any time: what's healthy, and the app URLs
python scripts/bb-install.py upgrade         # a newer release: shows the changes, asks, installs
python scripts/bb-install.py upgrade --list  # the releases newer than the cluster's
python scripts/bb-install.py upgrade --to latest   # or --to 4.1.0, --to 4.1.0-cg.3: no checkout needed
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
  pull from the org is usually an image your Chainguard plan doesn't cover. A registry that
  answers 5xx is asked again (after 5 s, then 20 s) and then reported as the registry
  failing, not as your login;
- the nodes' memory: their total allocatable memory against what the enabled packages need
  (about 16 GiB for Big Bang's defaults, more for GitLab, Elasticsearch, Anchore and the
  like); a warning, not a problem;
- a load balancer: Big Bang's gateways are LoadBalancer Services, so it warns when nothing in
  the cluster seems to give one an address (no cloud provider, MetalLB, kube-vip or k3s's
  ServiceLB, and no existing LoadBalancer Service with an address);
- object storage: `OBJECT_STORAGE_ENDPOINT` set in the environment but different from the
  endpoint the overlays were generated for is a problem (regenerate with `bb-run.py`);
- the nodes, when NeuVector is on: each node's inotify limits (`fs.inotify.max_user_instances`
  8192 and `fs.inotify.max_user_watches` 524288 or more; NeuVector's enforcer exits at the
  common default of 128 instances). Nothing in the Kubernetes API shows them, so a pod on each
  node, in a `bb-install-check` namespace the check deletes after, reads them with the org's
  busybox image. A node it can't read is a note, not a problem; `--no-node-check` skips it.

NeuVector scans running workloads with Big Bang's defaults. Its registry scans (an image
by name, before it runs) also need its scanner to reach the registry, which Big Bang's
default NetworkPolicies don't allow: [Troubleshooting](troubleshooting.md#neuvectors-registry-scans-fail-image-not-found)
has the site-file values.

It exits non-zero and lists each problem, so it also works in a pipeline. `install` runs
the same checks before it changes anything.

The first `install` asks for your domain and packages and writes `my-site.yaml` (or pass
`--domain`, `--enable` and `--disable`, which turns default packages off; `init` does only
that step). Later runs read it; `--site` names
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

Re-run `install` to switch to FIPS (`--fips`) or back, or to apply a changed site file. To
move to newer overlays use `upgrade`, which takes the Velero backup first. `uninstall` removes Big Bang, SeaweedFS and Flux; SeaweedFS's
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
(for example `4.1.1-cg.2`) with an archive to install from and notes listing the image
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

**Upgrades.** `install --plan` (or `--dry-run`; same flags) shows what a run would change
and stops: the Big Bang version, standard to FIPS or back, packages turned on or off, and
each image tag that moves. `rollback` returns the release to the previous revision (or
`--to N`) and waits up to `--timeout` seconds; `rollback --dry-run` shows the revision it
would go to, and `uninstall --dry-run` what it would remove. Flux then downgrades the
packages, which a package whose data has moved forward (a database migration) may not
accept, so check `--plan` before upgrading.

**Upgrading.** `upgrade` takes a Velero backup of the cluster before it changes anything,
when Velero runs with an Available backup location (`bb-upgrade-<time>`, kept 30 days); a
backup that fails stops the upgrade. `--no-backup` skips it. What the backup holds:

- **Volume data only when Velero's node agent runs.** With the object-storage overlay (the
  default) Velero has no volume snapshot location; volumes are copied by the node agent's
  file-system backup, which the overlay turns on and `upgrade` asks for
  (`defaultVolumesToFsBackup`). `upgrade` reports how many pod volumes it copied, and warns
  when the agent isn't running or copied none: such a backup brings back Kubernetes objects,
  not a database. Restoring a namespace from a backup that has its volumes (`velero restore
  create --from-backup <name> --include-namespaces <ns>`, after deleting the namespace)
  undoes a database migration; `rollback` doesn't.
- **Only this cluster's object storage.** The backup goes to the same SeaweedFS (or S3) the
  cluster uses, so it doesn't protect against losing the cluster or that storage. Copy it
  elsewhere, or point Velero's location at storage outside the cluster, for that. Each Big
Bang release this repo moves to has upgrade notes in [docs/upgrades/](upgrades/): the
package charts and Iron Bank images that change, new major versions first. Read them, and
Big Bang's own release notes, before upgrading.

**Upgrading to a release.** `upgrade` on its own deploys the checkout (or release directory)
you run it from. `upgrade --to <release>` moves the cluster to a published release without
downloading it yourself: a tag (`--to 4.1.0-cg.3`), a Big Bang version (`--to 4.1.0`, its
newest `-cg.<n>`) or `--to latest` (the newest release). The installer:

1. lists the repository's GitHub releases (a release directory's `RELEASE_REPO` file names
   the repository it came from; a checkout uses its GitHub remote, so a fork uses its own;
   with neither, `--repo OWNER/REPO` is required; `GH_TOKEN` lifts GitHub's anonymous limit
   of 60 calls an hour);
2. refuses a release older than the one the cluster runs unless `--allow-downgrade`
   (`rollback` is the usual way back);
3. downloads the release archive into `work/releases/<tag>/` (under the current directory
   with `bb-docker.sh`), checks it against the release's sha256 file and verifies its
   signature with `cosign` exactly as the
   [quickstart](quickstart.md#2-check-it-came-from-this-repository) does (the release
   workflow on main only). A digest or signature that doesn't match stops the upgrade, and so
   does a missing sha256 file, signature bundle or `cosign`, unless `--allow-unverified`:
   then it says `NOT VERIFIED` and asks before going on (`--yes` answers; with no terminal
   and no `--yes` it refuses);
4. runs that release's own `bb-install.py upgrade` with your other flags (`--site`, `--fips`,
   `--yes`, `--plan`, ...), so the plan, the question, the Velero backup and the install are
   the release's, with the installer CI tested its overlays with. A flag that release's
   installer doesn't have stops it before anything changes;
5. records the release in the `bigbang/bb-install` Secret (`install` from a release does the
   same).

`upgrade --list` shows the releases newer than the cluster's. The cluster's release is known
once it was installed or upgraded from a release by an installer that records it; before
that (and after a `rollback`) only its Big Bang version is, so `--list` shows every release
of that version and newer, and only an older Big Bang version counts as a downgrade.
A release from before `--to` existed doesn't have it: download a
newer release once ([quickstart](quickstart.md#5-upgrade-to-a-newer-release)), or run `--to`
from a clone of this repository. A target release older than 4.1.0-cg.3 takes no Velero
backup (its installer predates it), and the installer says so.

**When repo1 doesn't answer.** Flux fetches every package's chart from repo1.dso.mil git
while it installs and upgrades. The installer names any source that can't fetch, asks Flux
to retry it every two minutes while it waits, and ends a timeout with the host that failed.
`--git-mirror https://git.example.com/bigbang` takes Big Bang's chart and every package from
a git server you run with repo1's paths under that URL (kept in sync with repo1, for
example by a scheduled `git fetch` of each package repository); the mirror is remembered.

**Disconnected sites.** On a connected machine, `scripts/bb-airgap.py --site my-site.yaml
[--fips] --out bundle/` gathers what the site's packages need: this checkout with Big Bang's
chart and Flux rendered to plain manifests, a bare git clone of Big Bang and of each enabled
package at its pinned tag, `images.txt` and `SHA256SUMS` (a checksum of every file in the
bundle; `sha256sum -c SHA256SUMS` on the other side). `images.txt` has every image the
install runs as `<source> <name:tag>`, the source at the digest the overlays pin
(`cgr.dev/<org>/tempo@sha256:...`), then the installer image, then, commented out, the Iron
Bank images enabled packages keep. Copy each whole index (`crane copy`, `skopeo copy --all`)
so the digest stays the same: the overlays pull by digest, and Chainguard rebuilds images
under the same tag, so a copy made by tag later has another digest and the pods fail with
ImagePullBackOff. It doesn't copy images: moving them into the site's registry is the site's
step, with its own tooling and under its Chainguard license. Inside the site:

```bash
./push-git.sh https://git.site.example/bigbang      # the package repositories, repo1's paths
python3 bigbang-chainguard/scripts/bb-install.py install --site my-site.yaml   --git-mirror https://git.site.example/bigbang --registry-mirror registry.site.example/bigbang
```

`--registry-mirror` points every Chainguard and built image (and Kyverno's allow list) at the
site registry, takes its credentials from `MIRROR_USERNAME`/`MIRROR_PASSWORD` if it needs
them, and is remembered. `check --registry-mirror ...` (and `install`, before it changes
anything) looks up every image the enabled packages pull in the site registry by its pinned
digest, with manifest requests only, and lists each one that's missing. `bundle.json` counts
the images and repositories the site's packages need; packages that keep Iron Bank images
need those too (it lists them). Iron Bank images aren't rewritten by `--registry-mirror`:
mirror `registry1.dso.mil` in the nodes' container runtime for those. Signature checks need
Sigstore's public services, so leave `--verify-signatures` off inside a disconnected site.

**GitOps.** `install --gitops DIR` does everything except the `helm` step: it writes `DIR`
(Big Bang's HelmRelease, its chart from repo1, and the values files as ConfigMaps) and
applies it. Commit `DIR` to your own git repository and point a Flux Kustomization at it
(`DIR/README.md` has the YAML); the credentials stay in the cluster Secret
`bigbang/bb-install-values`, out of git.

## Doing it by hand

What `install` runs, in order, for a site that can't run the installer. The installer adds
the checks, the prompts and the cluster Secret; by hand those are yours. `<overlays>` is a
checkout or release directory, `site.yaml` your site file, `creds.yaml` a values file that
holds only credentials (keep it out of git).

1. **Chart.** Big Bang's chart at the version in the overlays' header, from repo1's git
   archive: `python scripts/bb-install.py chart` writes it to `work/charts/`.
2. **Check** the cluster: Kubernetes 1.35+, a default StorageClass (for SeaweedFS), and
   `helm template bigbang <chart> -n bigbang -f overlays/values-chainguard.yaml
   [-f overlays/values-chainguard-fips.yaml] -f overlays/values-object-storage.yaml -f site.yaml`
   renders without errors.
3. **Flux's pull secret:** `kubectl create namespace flux-system`, then
   `kubectl -n flux-system create secret docker-registry private-registry
   --docker-server=cgr.dev --docker-username=... --docker-password=...` (a pull token; add the
   build registry's login to the same dockerconfigjson when its images are private).
4. **Flux:** `kubectl apply --server-side --force-conflicts -k flux/` (or `flux-fips/`), then wait for the
   four controllers in `flux-system` to be Available.
5. **SeaweedFS** (when the overlays' object storage is the in-cluster SeaweedFS):
   `kubectl create namespace seaweedfs`; the same `private-registry` Secret there; the
   `seaweedfs-s3` Secret with key `s3.json` (S3 identities with an access key and secret you
   generate; `scripts/objstore.py` `s3_config` writes the format); `kubectl -n seaweedfs
   delete job seaweedfs-buckets --ignore-not-found`; `kubectl apply -k seaweedfs/` (or
   `seaweedfs-fips/`); wait for `deploy/seaweedfs` and `job/seaweedfs-buckets`.
6. **Big Bang:** `helm upgrade -i bigbang <chart> -n bigbang --create-namespace
   -f overlays/values-chainguard.yaml [-f overlays/values-chainguard-fips.yaml]
   [-f overlays/values-object-storage.yaml] -f site.yaml -f creds.yaml --timeout 15m`.
   `creds.yaml` holds `registryCredentials` (each registry's login), the object-storage keys
   of the packages you enabled only (`scripts/bb-object-storage.py --credentials-template`
   writes the skeleton), and Keycloak's admin and database passwords (14+ characters for
   FIPS). Then ask Flux to apply the new values now:
   `kubectl annotate --overwrite helmrelease --all -A reconcile.fluxcd.io/requestedAt="$(date +%s)"`.
7. **TLS:** Big Bang's public gateway serves the Secret `istio-gateway/public-cert`: a
   cert-manager Certificate for `*.<domain>` from your ClusterIssuer, or your own certificate
   in the site file's `gatewayCerts`.
8. **Signatures** (with Kyverno): the installer's Kyverno ClusterPolicy is built by
   `signature_policy` in `scripts/bbinstall/signatures.py`; leave it out, or apply it in
   Audit first.
9. **Wait:** `kubectl get helmrelease -A` until every one is Ready, then the pods in each
   package namespace; point DNS for the domain at the `istio-gateway` LoadBalancer address.

Upgrading by hand is the same steps with the new release's files, after a Velero backup
(`velero backup create <name> --default-volumes-to-fs-backup`).

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
- Kibana on the FIPS overlay (`kibana-fips`) refuses to start unless
  `xpack.security.fipsMode.enabled` is `true`, and Kibana allows that setting only with an
  Elastic Platinum or Enterprise license (or a trial). A FIPS site running Kibana needs that
  license and `packages.elasticsearchKibana.values.kibana.config.xpack.security.fipsMode.enabled: true`
  in its site file.
- Switching an existing deployment is `bb-install.py install --fips` (and back without `--fips`).

## GitLab and GitLab Runner notes

The kind tests found these; each applies to a real cluster too.

- **PostgreSQL:** GitLab loads its schema in one transaction. With PostgreSQL's default
  `max_locks_per_transaction` (64) the migrations fail with `out of shared memory` and
  GitLab never starts. Raise it to 256 on the database GitLab uses. The chart does not
  configure that server. `bb-install.py` notes this when GitLab is enabled. The kind
  test sets it in `compat/support/app-deps.py`.
- **GitLab Runner needs Kyverno.** Big Bang hands the runner GitLab's registration token
  with a Kyverno policy (`autoRegister`). Kyverno is on by default; if you turn it off, give
  the runner a token yourself, or it logs `The registration-token needs to be entered`
  while its pod still shows Ready.
- **Runner home directory:** handled for you. The overlays set `HOME=/home/gitlab-runner`,
  the directory the chart mounts; the Chainguard image's user has another home, and the
  runner crashed writing `/.gitlab-runner` without it.
- **Small or FIPS clusters:** the migrations can take longer than Kubernetes' 10-minute
  rollout deadline, and Rails longer than the liveness probe's ~3 minutes to boot.
  `mapping.yaml` sets both. `progress_deadline` patches every GitLab Deployment to
  `progressDeadlineSeconds` 2400, beside the digest pins. `overrides.gitlab` sets
  webservice and sidekiq liveness `failureThreshold` to 15. Those values reach the
  overlay on the next online `bb-convert.py`. A release cut before that conversion
  still needs the same settings the kind test applies. Leave `GODEBUG=tlsmlkem=0`.

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
