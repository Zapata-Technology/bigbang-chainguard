# How bigbang-chainguard works

This repo turns upstream Big Bang into a deployment on Chainguard images from **your**
Chainguard org. It never forks Big Bang: everything it produces is a values overlay (or a
Flux kustomization) layered on the unmodified chart. These diagrams show the pieces and how
they fit together.

## 1. The conversion pipeline

```mermaid
flowchart LR
    subgraph inputs[Inputs]
        BB[(Big Bang chart<br/>repo1.dso.mil<br/>BIGBANG_VERSION)]
        PK[(Every package chart<br/>at Big Bang's pinned tag)]
        MAP[mapping.yaml<br/>+ orgs/&lt;org&gt;.yaml]
        ORG[(cgr.dev/&lt;org&gt;<br/>tags + digests)]
        IB[(Iron Bank recipes<br/>dsop/big-bang/*)]
    end

    BB --> F[bb-fetch.py<br/>clone]
    PK --> F
    F --> D[bb-images.py<br/>render everything,<br/>find each image and<br/>the value that sets it]
    D --> DJ[discovery/images.json]

    IB --> BLD["bb-build.py &lt;name&gt;<br/>builds/*/recipe.yaml<br/>+ scripts/buildhooks/<br/>(base, grafana-plugins, devops-tester,<br/>vault, jmx-exporter, mattermost-operator,<br/>tempo-query, minio-operator(-sidecar),<br/>gitaly-init-cgroups, registry-adapter, installer)"]
    ORG --> BLD
    BLD --> BY[builds/*/build.yaml]

    F --> SC[bb-scan.py<br/>sh scripts that need bash,<br/>empty config defaults]
    MAP -- scan_reviewed --> SC

    DJ --> C[bb-convert.py<br/>map + pick org tags,<br/>overrides, post-renderers]
    MAP --> C
    ORG --> C
    BY --> C
    OS[OBJECT_STORAGE<br/>seaweedfs or minio] --> C
    C --> OV[overlays/values-chainguard.yaml<br/>overlays/values-chainguard-fips.yaml]
    C --> OSO[overlays/values-object-storage.yaml<br/>seaweedfs/ + seaweedfs-fips/]
    C --> FX[flux/ + flux-fips/]
    C --> DOC[docs/images.md<br/>docs/required-images.md]

    OV --> V[bb-verify.py<br/>render again: every image<br/>is the org's, a build,<br/>or documented Iron Bank]
    OSO --> V
    FX --> V
```

- **Discovery** renders Big Bang with every package turned on, then each package with the
  values Big Bang gives it. It records each Iron Bank image and the exact value path that
  sets it: a repository/tag pair, a single string, a registry split, or an image inside a list.
- **Conversion** maps each image (`mapping.yaml`, with your org's exceptions in
  `orgs/<org>.yaml`). It then picks a tag from **your** org: the newest patch on the line
  Iron Bank pins. It writes overrides at those same value paths, so the chart's own templates
  produce the Chainguard reference.
- **Verification** renders everything again with the overlays, and fails on any image that
  isn't converted, built here, or documented as staying on Iron Bank. With SeaweedFS it
  also fails if any MinIO is still deployed.
- **The scan** reads every package chart for things a new image name can't fix: shell
  code run by `sh` that needs bash, and empty defaults written into an app's config.
  Each finding must be reviewed in `mapping.yaml` (section 8).

## 2. Picking a tag

```mermaid
flowchart TD
    A[Iron Bank tag, e.g. v1.20.3] --> M{Below min_version?}
    M -- yes --> N[Newest release<br/>noted in docs/images.md]
    M -- no --> B{Org has a 1.20.x patch?}
    B -- yes --> C[Newest 1.20 patch]
    B -- no --> D{strict, or --strict-versions?}
    D -- yes --> E{fallback: ironbank?}
    E -- yes --> F[Stay on Iron Bank<br/>until the org has it]
    E -- no --> G[Unresolved: CI fails]
    D -- no --> H{Newer 1.x in the org?}
    H -- yes --> I[Newest 1.x<br/>flagged MINOR AHEAD<br/>in docs/images.md]
    H -- no --> E
```

Two more options change this per image:

- `group:` makes images move together. GitLab's images all convert, or all stay, so they
  stay on one GitLab release.
- `version_from:` matches another image's Iron Bank tag. GitLab's registry, shell and
  exporter use this, because Chainguard tags them with the GitLab release.
- `min_version:` raises an end-of-life line to the newest release. kubectl uses `1.34`, so a
  chart that pins an end-of-life kubectl (Vault's 1.30, in Big Bang 3) doesn't pull an
  image Chainguard stopped rebuilding.

## 3. Images this repo builds

Chainguard doesn't publish Big Bang's own images (base, grafana-plugins, devops-tester).
For those, a script converts **Iron Bank's own recipe** into a build FROM your org's images.
Eight more get a build for what Chainguard's image lacks or doesn't publish: Vault (the
curl Big Bang's post-start hook needs), the JMX exporter (SonarQube's init container copies
its jar with sh and curl), and, built from upstream's source, the Mattermost Operator,
tempo-query, the MinIO Operator and its sidecar, GitLab's gitaly-init-cgroups and NeuVector's
registry adapter. The table
lists them all.

```mermaid
flowchart LR
    R[Iron Bank recipe<br/>hardening_manifest.yaml + Dockerfile<br/>pinned to a repo1 commit] --> S["bb-build.py &lt;name&gt;<br/>recipe.yaml + its hook"]
    O[cgr.dev/&lt;org&gt;/&lt;base&gt;-dev<br/>pinned by digest] --> S
    S --> Y[builds/&lt;name&gt;/build.yaml]
    Y --> K[docker build<br/>builds/&lt;name&gt;/Dockerfile<br/>retried once on a<br/>transient fetch error]
    K --> T{smoke test<br/>tools, versions, uid,<br/>OpenSSL fips=yes}
    T -- fail --> X[CI fails; nothing pushed]
    T -- pass --> G{grype: a fixable<br/>Critical CVE?}
    G -- yes --> X
    G -- "no, on main" --> ST[push &lt;tag&gt;-staging]
    ST --> SIG[cosign keyless sign<br/>the digest, bundle +<br/>classic .sig formats]
    SIG --> P[(push &lt;tag&gt;<br/>BUILD_REGISTRY<br/>ghcr.io/&lt;owner&gt;)]
    P --> AT["attest SPDX SBOM +<br/>SLSA v1 predicate<br/>written by the build job"]
```

With `SIGN_IMAGES=keyless`, each image is signed **before its release tag moves to it**:
`builds.yml` pushes `<tag>-staging`, signs that digest, then pushes `<tag>`. A cluster
enforcing signatures never sees the tag on an unsigned image. Pushes are retried (GHCR
refuses a new package's first push now and then). The provenance record
(`scripts/bb-provenance.py`) is a SLSA v1 predicate the build job writes. It is not a
SLSA Build L3 attestation. It names the workflow run, the commit and every pinned input
from `build.yaml`.

| Image | Iron Bank source | Built FROM |
|---|---|---|
| `bigbang-base` | `big-bang/base` | `kubectl(-fips)-dev` |
| `bigbang-grafana-plugins` | `big-bang/grafana/grafana-plugins` | `grafana(-fips)` |
| `bigbang-devops-tester` | `big-bang/devops-tester` | `kubectl(-fips)-dev` |
| `bigbang-vault` | `hashicorp/vault` (adds curl) | `vault(-fips)-dev` |
| `bigbang-jmx-exporter` | `opensource/prometheus/jmx-exporter` (the javaagent jar, plus curl) | `busybox(-fips)-dev` |
| `bigbang-mattermost-operator` | `opensource/mattermost/mattermost-operator` (built from upstream's source) | `go(-fips)-dev` or `kubectl(-fips)-dev`, onto `glibc-openssl(-fips)` or `kubectl(-fips)` |
| `bigbang-tempo-query` | `opensource/grafana/tempo-query` (built from upstream's source) | as the Mattermost Operator |
| `bigbang-minio-operator`, `bigbang-minio-operator-sidecar` | `opensource/minio/operator`, `operator-sidecar` (archived; built from upstream's archived source) | as the Mattermost Operator |
| `bigbang-gitaly-init-cgroups` | none: GitLab's CNG `gitaly-init-cgroups` (built from Gitaly's source as CNG builds it) | as the Mattermost Operator |
| `bigbang-registry-adapter` | `opensource/neuvector/registry-adapter` (built from upstream's source, cgo) | as the Mattermost Operator |
| `bigbang-installer` | none (the installer itself: bb-install.py, kubectl, helm, Python, the chart) | `kubectl(-fips)-dev` |

The Go builds (the Mattermost Operator, tempo-query, the MinIO Operator) use the `go`/`glibc-openssl` images when the org has them. Without
them it builds in `kubectl(-fips)-dev` with Wolfi's Go and runs on `kubectl(-fips)`, and the
FIPS image uses Go's own FIPS 140-3 module (`GOFIPS140`) instead of `go-fips`. The next
build switches over once the org has those images. Chainguard's `go-fips` itself changed with
Go 1.27: through 1.26 it uses an OpenSSL backend (`GOEXPERIMENT=systemcrypto`), from 1.27 Go's
validated FIPS module with cgo (`GOFIPS140=v1.0.0-…`, `fips140=on`); the build's smoke test
accepts either (`bbbuild.GO_FIPS_BUILDS`).

## 4. Deploying: how the overlays layer

Three commands share one set of checks. `check` stops after them and changes nothing;
`install` and `upgrade` go on only if every one passes. (`scripts/bb-docker.sh` runs the
same commands from the installer image.)

```mermaid
flowchart TB
    subgraph UPG["upgrade only"]
        direction LR
        MG["A Big Bang 3 site file<br/>converted to packages.*,<br/>original kept"] --> PL["The plan shown<br/>and a question asked"]
    end

    subgraph CHK["Checks: bb-install.py check (nothing changes)"]
        direction LR
        SF["Site file<br/>written first<br/>if missing"] --> PF["Preflight<br/>tools, Kubernetes 1.35+,<br/>StorageClass, domain,<br/>package dependencies"]
        PF --> UP{"Upgrade path<br/>4 needs 3.35.1 first,<br/>never an older major"}
        UP --> RD["Render<br/>helm template with the<br/>overlays + site file"]
        RD --> PC["Pull test<br/>every Chainguard and<br/>built image, your logins"]
    end

    subgraph INS["install / upgrade: only when every check passed"]
        direction LR
        CR["Credentials<br/>kept in<br/>bigbang/bb-install"] --> FX["Flux"]
        FX --> SW["SeaweedFS<br/>S3 keys, buckets"]
        SW --> BB["Big Bang<br/>helm, or --gitops"]
        BB --> TL["TLS"]
        TL --> SG["Image<br/>signatures"]
        SG --> W["Wait and report<br/>health, URLs"]
    end

    UPG --> CHK --> INS

    classDef check fill:#ecfdf5,stroke:#10b981,color:#064e3b
    classDef act fill:#fff7ed,stroke:#f97316,color:#7c2d12
    class SF,PF,UP,RD,PC check
    class CR,FX,SW,BB,TL,SG,W,MG,PL act
```

- **The render** catches a misspelled key, a value of the wrong type, or a Big Bang 3 key
  (`tempo:`, `addons.keycloak:`), with the chart's own error, before any HelmRelease fails.
- **GitLab's database.** When GitLab is enabled, preflight adds a note: PostgreSQL
  `max_locks_per_transaction` must be 256. The chart does not set that on the external
  server. The note does not stop the install.
- **The pull test** asks each registry for every image's manifest with the login it was
  given. A refused pull from the org is usually an image the Chainguard plan doesn't cover.
  `check` uses a short-lived token from your chainctl login and stores nothing; `install`
  asks again for a refused login, up to three tries. `--no-pull-check` skips it for a
  registry only the nodes can reach.
- **`upgrade`** also shows what will change (version, FIPS, packages, image tags) and asks
  before it installs.

The layering it uses:

```mermaid
flowchart TB
    subgraph helm[helm upgrade -i bigbang ... -f each file, last one wins]
        direction TB
        U[Big Bang chart defaults<br/>Iron Bank images] --> O1[values-chainguard.yaml<br/>Chainguard images, registry1 blanks,<br/>Kyverno allow list]
        O1 --> O2[values-chainguard-fips.yaml<br/>optional: -fips images]
        O2 --> O3[values-object-storage.yaml<br/>OBJECT_STORAGE=seaweedfs, the default:<br/>SeaweedFS / S3, no MinIO]
        O3 --> O4[my-site.yaml<br/>domain, registryCredentials, SSO, ...]
    end
    FL[kubectl apply --server-side -k flux/<br/>or flux-fips/] --> helm
```

- **Flux comes first.** When your org's Flux controllers are newer than Big Bang's, `flux/`
  installs the upstream Flux release they belong to, with Big Bang's patches. Big Bang's own
  CRDs would be too old for them.
- **The FIPS switch is one extra file.** Leave out `values-chainguard-fips.yaml` for
  standard images, and add it for FIPS. Switching an existing install is
  `bb-install.py install --fips` (and back without it).
- **Credentials live only in your site file:** `cgr.dev`, your build registry, and registry1
  for the images that stay on Iron Bank.

## 5. Keeping it current

```mermaid
sequenceDiagram
    autonumber
    participant N as update.yml (nightly, 03:17 UTC)
    participant PR as Pull request
    participant CI as ci.yml + builds.yml + compat.yml
    participant M as main
    participant B as builds.yml + release.yml (push)
    participant G as BUILD_REGISTRY + GitHub release

    N->>N: newest Big Bang tag? fetch, discover
    N->>N: re-read Iron Bank recipes, re-pin org digests
    N->>N: bb-convert (tags and today's digests) + bb-verify (standard, FIPS)
    N->>N: bb-scan: new sh-needs-bash or empty defaults<br/>bb-leftovers: Iron Bank leftovers now published
    N->>PR: open bot/bigbang-update (deployed release, newest patches)<br/>and, for a newer Big Bang, bot/bigbang-release<br/>with upgrade notes (docs/upgrades/)
    PR->>CI: unit tests, discovery, scan, verify, signatures,<br/>no fixable Critical CVE in new images,<br/>build + smoke-test changed images,<br/>kind tests the change reaches, or the smoke set<br/>for a digest-only change
    CI-->>PR: checks, including results-gate<br/>(a recorded standard matrix for this Big Bang version)
    PR->>M: automerge.yml when every check passed<br/>(bot/bigbang-release: you, or itself for a patch<br/>release with AUTOMERGE_BIGBANG_PATCHES)
    M->>B: push to main
    B->>G: build, scan, sign, then move the tag,<br/>SBOM + SLSA v1 predicate, tag &lt;bigbang&gt;-cg.&lt;n&gt;,<br/>signed archive and notes
```

Every org image is pinned to the digest its tag had when the overlays were generated
(a Flux post-renderer swaps each pod image to `repo@sha256:...`; values keep the tag,
which charts reuse in labels). Chainguard rebuilds images under the same tag, so without
the pin a rebuild would reach clusters on the next pod restart, untested. With it, a
rebuild arrives only as a new digest in the nightly pull request. `compat.yml` then
runs the smoke kind tests (Kyverno, metrics-server, Tempo, Keycloak, Kiali, Velero),
not every package. The weekly run deploys every package, including GitLab.
`results-gate` fails the pull request while this Big Bang version has no recorded
standard-mode matrix. A skipped run is not a pass. Tests marked `registry1: true`
stay outside that gate.

`compat.yml` runs on pull requests that change what clusters run (`overlays/`, `flux*/`,
`mapping.yaml`, `orgs/`, `compat/`, the installer) and weekly with the large tests
(GitLab), and deploys packages on kind to prove they **run**, not just render, checking
every running image against the image policy. While each test waits, a watcher reads
every container's log: it prints errors as they happen, stops the test early on a
failure that won't recover, and fails it on an OOM kill. Every job uploads its logs
and the cluster's state as an artifact. See [testing.md](testing.md).

`automerge.yml` reads the pull request and its check runs with the workflow's own
token and merges with `RELEASE_PAT`, so the merge starts `builds.yml` and `release.yml`
on main (a merge made with the workflow token wouldn't). Each such merge that changes
what users deploy becomes a release `<bigbang>-cg.<n>`.
`cve-report.yml` compares Iron Bank's and Chainguard's images with grype every week.

`bb-leftovers.py` checks each `{stays: ..., until: <name>}` image against Chainguard's
catalog and your org. When Chainguard publishes one, the nightly report says so; add the
repository to your org, and the next run converts it.

## 6. Object storage (SeaweedFS by default)

```mermaid
flowchart LR
    SET{"OBJECT_STORAGE<br/>flag, env or repo var, file"} -->|"seaweedfs (default)"| GEN[bb-convert.py writes<br/>values-object-storage.yaml<br/>seaweedfs/ + seaweedfs-fips/]
    SET -->|"minio"| MIN["no files: Big Bang<br/>MinIO stays"]
    subgraph site[Site, before Big Bang]
        SEC["Secrets: private-registry,<br/>seaweedfs-s3 with s3.json identities"] --> KZ[kubectl apply -k seaweedfs/]
        KZ --> SW[(SeaweedFS server on a PVC<br/>S3 on port 8333)]
        KZ --> JOB[bucket Job<br/>bigbang-tempo, bigbang-thanos, ...]
        JOB --> SW
    end
    GEN --> KZ
    subgraph bb[Big Bang packages]
        T[Tempo] & TH[Thanos] & MI[Mimir] & MM[Mattermost] & GL[GitLab] & VE[Velero]
        L[Loki<br/>with --with loki]
    end
    T & TH & MI & MM & GL & VE & L --> SW
    X[minio, minio-operator,<br/>bundled MinIO] -. turned off .- bb
```

With `OBJECT_STORAGE=seaweedfs` (the default), `bb-convert.py` writes the overlay from
`object-storage.yaml`, plus `seaweedfs/` (SeaweedFS from your org's image, S3 auth from a
Secret, a bucket Job). `OBJECT_STORAGE=minio` keeps Big Bang's own MinIO and deletes those
files. Credentials stay in the site file, and `bb-verify.py` fails if any MinIO is still
deployed.

Set credentials only for the packages you enable. Big Bang creates Mimir's credential
Secret whenever its keys are set, even with Mimir off, and the install then fails on the
missing `mimir` namespace.

## 7. Trust boundaries

```mermaid
flowchart LR
    subgraph trusted[What a cluster pulls]
        CG[(cgr.dev/&lt;org&gt;<br/>Chainguard, signed,<br/>pinned by digest)]
        BR["BUILD_REGISTRY<br/>built here, smoke-tested,<br/>scanned, signed,<br/>SBOM + SLSA v1 predicate"]
        IBR[(registry1.dso.mil<br/>documented leftovers)]
        SR[(site registry<br/>disconnected sites:<br/>copies of the above)]
    end
    K[Kyverno restrict-image-registries<br/>allow list = exactly these] --> trusted
    S[Kyverno signature policy<br/>enforce by default]
    S --"Chainguard's signature" --> CG
    S -- "builds.yml on main" --> BR
    subgraph build[What a build reads]
        IBRec[Iron Bank recipes<br/>pinned commit]
        Z[plugin zips / binaries<br/>sha256 or digest from<br/>Iron Bank's manifest]
        CGd[org -dev images<br/>pinned by digest]
    end
    build --> BR
    subgraph user[What a user installs]
        REL[release archive<br/>sha256 + Sigstore bundle<br/>naming release.yml]
        INS[bigbang-installer image<br/>built FROM the org's images]
    end
    subgraph state[What the installer keeps]
        SEC[Secret bigbang/bb-install<br/>pull token, S3 keys,<br/>generated passwords]
    end
```

- A cluster only pulls from the three registries Kyverno allows. The overlay sets that list.
- With Kyverno on, the installer's signature policy is in enforce mode by default
  (`--verify-signatures audit` or `off` to change it). Every `cgr.dev/<org>` image needs
  Chainguard's signature, and every `<build registry>/bigbang-*` image needs the keyless
  signature `builds.yml` makes on this repo's `main` (`--build-signer` names another repo).
  CI's `bb-signatures.py` checks the same two rules on every pull request.
- Org images are pinned by digest (section 5), so what runs is what CI tested.
- Releases are signed keylessly by `release.yml`; the release notes show the `cosign
  verify-blob` command.
- The installer keeps credentials in one Secret, `bigbang/bb-install`. Anyone who can read
  Secrets in the `bigbang` namespace can read them (SECURITY.md, "Credentials in the cluster").
- Every input to a build is pinned: Iron Bank's recipe by commit, each download by sha256 or
  digest, and each base image by digest. The SLSA v1 provenance predicate the build job
  writes (not a SLSA Build L3 attestation) lists them.
- CI identities are scoped. The Chainguard pull identity is trusted only for this repo's
  `main` branch and pull requests. GitHub Actions are pinned to commit SHAs.
- The CI, build and nightly runners block every outbound call they don't list
  (harden-runner); the kind tests audit instead. CodeQL and Scorecard start once the repo
  is public. See [SECURITY.md](../SECURITY.md).

## 8. Where Iron Bank's and Chainguard's images behave differently

Iron Bank builds most images on Red Hat UBI, where `/bin/sh` is bash. A Chainguard `-dev`
image's `/bin/sh` is busybox. Big Bang's scripts sometimes say `sh` and use bash (arrays,
here-strings), and those die on their first bash line. Separately, Flux renders some chart
defaults differently from Helm 3. Neither shows up when rendering. `bb-scan.py` finds them
in the charts; the kind tests found the first ones.

```mermaid
flowchart TD
    SC[bb-scan.py over every package chart] --> Q{finding}
    Q -->|"sh script needs bash, on an image we build"| B1["bigbang-base links sh to bash,<br/>as UBI does; smoke test checks it.<br/>Alloy upgrade job"]
    Q -->|"wait script is #35;!/bin/sh with arrays"| B2[bash_wait_jobs: a Flux post-renderer<br/>runs each script with bash.<br/>Kyverno policies]
    Q -->|"/bin/sh -c bash script on a Chainguard image"| B3["bash_commands: a post-renderer<br/>makes the command /bin/bash.<br/>Fortify keystore job"]
    Q -->|"empty default written into the app config"| B4["overrides: the chart's config<br/>without it. Tempo 2's opencensus<br/>receiver, until Big Bang 4"]
    B1 & B2 & B3 & B4 --> R["mapping.yaml scan_reviewed:<br/>each finding and how it is handled"]
    R --> CI{CI: any finding<br/>not reviewed?}
    CI -- yes --> FAIL[fail: review it]
    CI -- no --> OK[pass]
```

A new Big Bang release that adds one of these fails CI on the nightly pull request, before
any cluster runs it. A finding that goes away is reported too (`gone`), so a workaround is
removed when it isn't needed: Big Bang 4's Tempo 3 chart dropped the `opencensus` receiver,
and the Tempo override went with it.
