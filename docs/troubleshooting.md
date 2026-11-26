# Troubleshooting

By symptom: what you see, why, and what fixes it. Start with
`python scripts/bb-install.py status` (each package's health, the pods that aren't, their last
log lines, and any running image that isn't the conversion's), and `bb-install.py check`,
which tests every setting without changing anything. Most of these were found by the kind,
RKE2 and FIPS-kernel tests ([testing.md](testing.md), "What the runtime tests found").

## Pods stuck in ImagePullBackOff or ErrImagePull

**An image your Chainguard plan doesn't cover.** cgr.dev refuses a pull of a repository your
org isn't entitled to the same way it refuses a bad login, so the pod only says it can't pull.
`bb-install.py check` pulls every image the enabled packages deploy and names each one that
fails; `python scripts/bb-org-repos.py` names the repositories your org lacks, and
[required-images.md](required-images.md) lists every one Big Bang needs. Add them to your
plan, or turn the package off (`init --disable <pkg>`, or `packages.<pkg>.enabled: false`).

**The pull login is wrong or gone.** The installer keeps the cgr.dev pull token, and any
build registry login, in the `bigbang/bb-install` Secret and in a `private-registry` Secret in
each package namespace. A token someone deleted in Chainguard, or a GHCR token that expired,
fails every pull from that registry. Set the new login in the environment (`CGR_USERNAME` and
`CGR_PASSWORD`, `BUILD_REGISTRY_TOKEN`) and re-run `install`: a variable replaces the stored
login. For the one-year token the installer made itself, see
[An expired token](#an-expired-chainctl-login-or-pull-token).

**The registry itself is failing.** `check` asks again when a registry answers 5xx, and
then reports "the registry failed (HTTP 5xx)" instead of blaming the login (cgr.dev answered
500 for about an hour on 2026-11-23). Wait, and run `check` again; Kyverno's signature check
fails pods the same way meanwhile ([Kyverno refuses pods](#kyverno-refuses-pods-image-signature-checks)).

**Images built here are private.** GHCR packages are private by default. The pods need a
login with `read:packages` for the `bigbang-*` images; `install` asks for it and tests it
([Registry logins](install.md#registry-logins)).

**A disconnected site's copy has another digest.** The overlays pull every org image by the
digest it had when they were generated; Chainguard rebuilds images under the same tag. A
copy made by tag after a rebuild has another digest, so the node can't find the one it asks
for. Copy from `images.txt` (its sources carry the digests) with the whole index (`crane copy`,
`skopeo copy --all`), then `bb-install.py check --registry-mirror <prefix>` lists every image
still missing.

**Iron Bank images.** A few packages keep Iron Bank images ([images.md](images.md), "Iron
Bank"); they need `REGISTRY1_USERNAME`/`REGISTRY1_PASSWORD`. `check` names the packages.

## Kyverno refuses pods: image signature checks

With Kyverno on, `install` adds a policy (enforce by default) requiring Chainguard's keyless
signature on every `cgr.dev/<org>` image and `builds.yml`'s on every `bigbang-*` image. It is
an `ImageValidatingPolicy` named `bb-install-chainguard-signatures`:
`kubectl get imagevalidatingpolicies` shows it, and `status.conditionStatus.ready` must be
`true`.

- **Nothing refused, even an unsigned image:** the policy isn't Ready, so Kyverno enforces
  nothing. `kubectl get imagevalidatingpolicies bb-install-chainguard-signatures -o yaml`
  says why. A legacy `ClusterPolicy` of the same name (an installer from before Big Bang
  4.2.0) is accepted by Kyverno 1.20 but never enforced; `install` and `upgrade` delete it and
  write the new policy, so re-run `upgrade`.

- **Every pod refused, or admission timing out:** Kyverno couldn't reach Sigstore
  (rekor.sigstore.dev and its TUF mirror). Big Bang's network policies let Kyverno reach only
  the API server and registry1; on a CNI that enforces them (RKE2's Canal) every check timed
  out and the enforced policy refused istiod, ztunnel and cert-manager. The installer adds the
  egress Kyverno needs; a site with its own egress rules (a proxy, a firewall) must allow it
  too. Disconnected sites can't reach Sigstore: install with `--verify-signatures off`.
- **Only `bigbang-*` images refused:** your copy doesn't sign its builds, or signs them from
  another repository. Pass `--build-signer <owner/repo>` (or `$BUILD_SIGNER`), or
  `--verify-signatures audit`.
- **Your org's signature required:** `--org-id` names it; an image signed under another org
  fails. `chainctl iam organizations list` shows the ID.
- **Not a signature:** Kyverno's `restrict-image-registries` admits only the org, the build
  registry and Iron Bank's registries. An image from anywhere else (a site's own sidecar) is
  refused until it's added to the policy's list in the site file.

`kubectl get policyreports -A` shows what audit mode would have refused.

## HelmReleases stuck: "chart is not ready", repo1 timeouts

Flux fetches every package's chart from repo1.dso.mil git while it installs and upgrades, so
a repo1 outage or a firewall shows up as HelmReleases that never become Ready. The installer
names each source that can't fetch, asks Flux to retry it every two minutes while it waits,
and ends a timeout with the host that failed. If repo1 is down, re-run later; if the cluster
can't reach it, run a git mirror and install with `--git-mirror https://git.example.com/bigbang`
(repo1's paths under that URL; [When repo1 doesn't answer](install.md)). Big Bang's chart
itself comes from repo1's git archive too; `--chart` takes a local copy or an `oci://` ref.
registry1's OCI copy of the chart needs an Iron Bank login; the git archive doesn't.

## NeuVector's enforcer exits: inotify limits

NeuVector's enforcer exits with "couldn't initialize inotify: too many open files" at the
common default of 128 inotify instances per user, and its exporter then can't log in to the
controller. Each node needs `fs.inotify.max_user_instances=8192` and
`fs.inotify.max_user_watches=524288` (a sysctl file in `/etc/sysctl.d/`, applied on boot).
`bb-install.py check` reads them on each node with a short-lived pod; nothing in the
Kubernetes API shows them.

## NeuVector's registry scans fail: "Image not found"

A registry scan in NeuVector (Assets > Registries, or `POST /v1/scan/repository`) waits 60 s
and fails with "Image not found", while every pod runs. Big Bang's default NetworkPolicies let
NeuVector's scanner reach only the Kubernetes API, so it can't pull from any registry (the
kubelet pulls the running images from the node, outside the policies). To scan registries,
let the scanner and the controller reach them on 443 in your site file:

```yaml
neuvector:
  values:
    networkPolicies:
      egress:
        from:
          scanner:
            to:
              cidr:
                0.0.0.0/0:443: true
          controller:
            to:
              cidr:
                0.0.0.0/0:443: true
```

Narrow `0.0.0.0/0` to your registries' addresses if you can. bb-common leaves the cloud
metadata address (169.254.169.254) out. Running workloads are scanned without this.

## An expired chainctl login or pull token

- **`check` says "no cgr.dev login"** although you logged in before: chainctl's session
  expired. `chainctl auth login` again; `check` uses a short-lived token from that session
  and stores nothing.
- **`status` says the pull token expires (or expired)**: the token `install` made with your
  chainctl login lasts a year. `chainctl auth login`, then `bb-install.py install
  --new-pull-token` makes a new one and replaces it in the cluster. Without chainctl, make one
  elsewhere (`chainctl auth pull-token create --repository=oci --parent=<org> --ttl=8760h`)
  and pass it in `CGR_USERNAME`/`CGR_PASSWORD`.
- **The installer image** has no chainctl: give it the token in the environment
  (`scripts/bb-docker.sh` passes `CGR_*` through).

## Keycloak won't start in FIPS mode: password too short

Keycloak FIPS (BC-FIPS) rejects Big Bang's default admin password (`password`) and its
database password (`keycloak`, through the PostgreSQL driver's SCRAM hashing): "password must
be at least 112 bits". Both need 14 characters or more. The installer generates long ones on
a fresh Keycloak, unless your site file sets them. A Keycloak that already runs on the default
database password can't take a new one from the site file (PostgreSQL keeps the password it
started with): change it in PostgreSQL and in the site file before switching to `--fips`.

## GitLab never starts: "out of shared memory", locks

GitLab loads its schema in one transaction. With PostgreSQL's default
`max_locks_per_transaction` (64) the migrations fail with `out of shared memory` and GitLab
never starts. Raise it to 256 on the database GitLab uses; the chart doesn't configure that
server. On a small or FIPS cluster the migrations can also outlast the Deployments' rollout
deadline and Rails the liveness probe; the overlays raise both ([GitLab notes](install.md#gitlab-and-gitlab-runner-notes)).
GitLab Runner needs Kyverno to receive its registration token, and stays Ready without one.

## Pods Pending, or the gateway has no address

- **Pending pods:** the nodes don't have the memory the packages request. Big Bang's default
  packages need about 16 GiB of allocatable memory (on 12 GB they scheduled but starved the
  control plane); `check` warns when the nodes have less than the enabled packages need.
- **No external address:** Big Bang's gateways are LoadBalancer Services. Without a cloud
  provider, MetalLB, kube-vip or k3s's ServiceLB they stay pending; `check` warns, and
  `status` shows each gateway's address.

## An upgrade went wrong

`bb-install.py rollback` (or `rollback --dry-run` first) returns Big Bang to the previous
revision; a package whose data was migrated forward may not run on the older version. The
Velero backup `upgrade` took restores data only if it copied pod volumes, which `upgrade`
reports ([Upgrading](install.md)). It lives in the same cluster's object storage.

## Still stuck

Open an issue with the output of `status`, the release tag, standard or FIPS, and the packages
you enabled. Leave out tokens and secrets in `my-site.yaml`.
