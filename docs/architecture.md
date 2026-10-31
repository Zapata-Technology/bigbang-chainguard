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

    IB --> BLD[bb-base.py<br/>bb-grafana-plugins.py<br/>bb-devops-tester.py<br/>bb-vault.py<br/>bb-jmx-exporter.py]
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
- `min_version:` raises an end-of-life line to the newest release. kubectl uses `1.34`, the
  oldest Kubernetes Big Bang supports, so Vault's kubectl 1.30 pin doesn't pull an
  unmaintained image.

## 3. Images this repo builds

Chainguard doesn't publish Big Bang's own images. For those, a script converts **Iron Bank's
own recipe** into a build FROM your org's images. Vault also gets a build, to add the curl
that Big Bang's post-start hook needs.

```mermaid
flowchart LR
    R[Iron Bank recipe<br/>hardening_manifest.yaml + Dockerfile<br/>pinned to a repo1 commit] --> S[bb-&lt;name&gt;.py]
    O[cgr.dev/&lt;org&gt;/&lt;base&gt;-dev<br/>pinned by digest] --> S
    S --> Y[builds/&lt;name&gt;/build.yaml]
    Y --> K[docker build<br/>builds/&lt;name&gt;/Dockerfile]
    K --> T{smoke test<br/>tools, versions, uid,<br/>OpenSSL fips=yes}
    T -- pass --> P[(BUILD_REGISTRY<br/>ghcr.io/&lt;owner&gt;)]
    T -- fail --> X[CI fails; nothing pushed]
    P --> SIG[optional: cosign keyless<br/>signature + SBOM]
```

| Image | Iron Bank source | Built FROM |
|---|---|---|
| `bigbang-base` | `big-bang/base` | `kubectl(-fips)-dev` |
| `bigbang-grafana-plugins` | `big-bang/grafana/grafana-plugins` | `grafana(-fips)` |
| `bigbang-devops-tester` | `big-bang/devops-tester` | `kubectl(-fips)-dev` |
| `bigbang-vault` | `hashicorp/vault` (adds curl) | `vault(-fips)-dev` |
| `bigbang-jmx-exporter` | `opensource/prometheus/jmx-exporter` (the javaagent jar, plus curl) | `busybox(-fips)-dev` |
| `bigbang-installer` | none (the installer itself: bb-install.py, kubectl, helm, Python, the chart) | `kubectl(-fips)-dev` |

## 4. Deploying: how the overlays layer

`scripts/bb-install.py install --site my-site.yaml [--fips]` does all of this in order:

```mermaid
flowchart LR
    CH[Chart<br/>repo1 git archive,<br/>no Iron Bank login] --> PF[Preflight<br/>tools, Kubernetes 1.34+,<br/>StorageClass, domain,<br/>Iron Bank needs, context]
    PF --> CR[Credentials<br/>env, site, cluster, or a<br/>chainctl pull token;<br/>kept in bigbang/bb-install]
    CR --> FX[Flux<br/>pull secret + flux/]
    FX --> SW[SeaweedFS<br/>generated S3 keys,<br/>Secrets, buckets]
    SW --> BB[Big Bang<br/>helm upgrade -i with the overlays,<br/>or --gitops: a Flux HelmRelease]
    BB --> TL[TLS<br/>cert-manager self-signed CA,<br/>your ClusterIssuer, or your cert]
    TL --> W[Wait and report<br/>HelmReleases, pods,<br/>image policy, URLs]
```

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
  standard images, and add it for FIPS. Switching an existing install is an in-place
  `helm upgrade`.
- **Credentials live only in your site file:** `cgr.dev`, your build registry, and registry1
  for the images that stay on Iron Bank.

## 5. Keeping it current

```mermaid
sequenceDiagram
    autonumber
    participant N as update.yml (nightly 03:00 UTC)
    participant PR as Pull request
    participant CI as ci.yml + builds.yml + compat.yml
    participant M as main
    participant B as builds.yml + release.yml (push)
    participant G as BUILD_REGISTRY + GitHub release

    N->>N: newest Big Bang tag? fetch, discover
    N->>N: re-read Iron Bank recipes, re-pin org digests
    N->>N: bb-convert (tags and today's digests) + bb-verify (standard, FIPS)
    N->>N: bb-scan: new sh-needs-bash or empty defaults<br/>bb-leftovers: Iron Bank leftovers now published
    N->>PR: open bot/bigbang-update, report in the body
    PR->>CI: unit tests, discovery, scan, verify, signatures,<br/>no fixable Critical CVE in new images;<br/>build + smoke-test images; every package on kind
    CI-->>PR: checks
    PR->>M: merge (you)
    M->>B: push to main
    B->>G: build, scan, push (+ sign); tag &lt;bigbang&gt;-cg.&lt;n&gt;,<br/>signed archive and notes
```

Every org image is pinned to the digest its tag had when the overlays were generated
(a Flux post-renderer swaps each pod image to `repo@sha256:...`; values keep the tag,
which charts reuse in labels). Chainguard rebuilds images under the same tag, so without
the pin a rebuild would reach clusters on the next pod restart, untested. With it, a
rebuild arrives only as a new digest in the nightly pull request, which `compat.yml`
deploys on kind before it merges.

`compat.yml` runs on pull requests that change what clusters run (`overlays/`, `flux*/`,
`mapping.yaml`, `orgs/`, `compat/`, the installer) and weekly with the large tests
(GitLab), and deploys packages on kind to prove they **run**, not just render, checking
every running image against the image policy. See [testing.md](testing.md).
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
        BR[(BUILD_REGISTRY<br/>built here, smoke-tested,<br/>scanned, signed)]
        IBR[(registry1.dso.mil<br/>documented leftovers)]
        SR[(site registry<br/>disconnected sites:<br/>copies of the above)]
    end
    K[Kyverno restrict-image-registries<br/>allow list = exactly these] --> trusted
    S[Kyverno signature policy<br/>Chainguard's signature on<br/>cgr.dev/&lt;org&gt; images<br/>audit by default] --> CG
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
  With `bb-install.py --verify-signatures` (audit by default when Kyverno is on), Kyverno
  also checks Chainguard's signature on every `cgr.dev/<org>` image a Pod runs.
- Org images are pinned by digest (section 5), so what runs is what CI tested.
- Releases are signed keylessly by `release.yml`; the release notes show the `cosign
  verify-blob` command.
- The installer keeps credentials in one Secret, `bigbang/bb-install`. Anyone who can read
  Secrets in the `bigbang` namespace can read them (SECURITY.md, "Credentials in the cluster").
- Every input to a build is pinned: Iron Bank's recipe by commit, each download by sha256 or
  digest, and each base image by digest.
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
    Q -->|"empty default written into the app config"| B4["overrides: the chart config<br/>template without it.<br/>Tempo opencensus receiver"]
    B1 & B2 & B3 & B4 --> R["mapping.yaml scan_reviewed:<br/>each finding and how it is handled"]
    R --> CI{CI: any finding<br/>not reviewed?}
    CI -- yes --> FAIL[fail: review it]
    CI -- no --> OK[pass]
```

A new Big Bang release that adds one of these fails CI on the nightly pull request, before
any cluster runs it. The Tempo entry names the chart version its config template was copied
from, so a new Tempo chart forces a fresh look at that copy.
