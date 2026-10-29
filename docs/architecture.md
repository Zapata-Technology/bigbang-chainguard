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

    IB --> BLD[bb-base.py<br/>bb-grafana-plugins.py<br/>bb-devops-tester.py<br/>bb-vault.py]
    ORG --> BLD
    BLD --> BY[builds/*/build.yaml]

    DJ --> C[bb-convert.py<br/>map + pick org tags]
    MAP --> C
    ORG --> C
    BY --> C
    C --> OV[overlays/values-chainguard.yaml<br/>overlays/values-chainguard-fips.yaml]
    C --> FX[flux/ + flux-fips/]
    C --> DOC[docs/images.md<br/>docs/required-images.md]

    OV --> V[bb-verify.py<br/>render again: every image<br/>is the org's, a build,<br/>or documented Iron Bank]
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
  isn't converted, built here, or documented as staying on Iron Bank.

## 2. Picking a tag

```mermaid
flowchart TD
    A[Iron Bank tag, e.g. v1.20.3] --> B{Org has a 1.20.x patch?}
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

## 4. Deploying: how the overlays layer

```mermaid
flowchart TB
    subgraph helm[helm upgrade -i bigbang ... -f each file, last one wins]
        direction TB
        U[Big Bang chart defaults<br/>Iron Bank images] --> O1[values-chainguard.yaml<br/>Chainguard images, registry1 blanks,<br/>Kyverno allow list]
        O1 --> O2[values-chainguard-fips.yaml<br/>optional: -fips images]
        O2 --> O3[values-object-storage.yaml<br/>optional: SeaweedFS / S3, no MinIO]
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
    participant CI as ci.yml + builds.yml
    participant M as main
    participant B as builds.yml (push)
    participant G as BUILD_REGISTRY

    N->>N: newest Big Bang tag? fetch, discover
    N->>N: re-read Iron Bank recipes, re-pin org digests
    N->>N: bb-convert + bb-verify (standard, FIPS)
    N->>PR: open bot/bigbang-update
    PR->>CI: unit tests, discovery, verify; build + smoke-test images
    CI-->>PR: checks
    PR->>M: merge (you)
    M->>B: push to main
    B->>G: build, test, push (+ sign)
```

`compat.yml` is separate. It runs on pull requests that change `compat/`, or by hand, and
deploys single packages on kind to prove they **run**, not just render, and checks
every running image against the image policy. See [testing.md](testing.md).

## 6. Object storage (optional)

```mermaid
flowchart LR
    S3[(SeaweedFS S3<br/>or any S3 service)]
    subgraph bb[Big Bang packages]
        T[Tempo] & TH[Thanos] & MI[Mimir] & MM[Mattermost] & GL[GitLab] & VE[Velero]
        L[Loki<br/>with --with loki]
    end
    T & TH & MI & MM & GL & VE & L --> S3
    X[minio, minio-operator,<br/>bundled MinIO] -. turned off .- bb
```

`scripts/bb-object-storage.py` writes this overlay from `object-storage.yaml`. Credentials
stay in the site file, and `--verify` fails if any MinIO server is still deployed.

## 7. Trust boundaries

```mermaid
flowchart LR
    subgraph trusted[What a cluster pulls]
        CG[(cgr.dev/&lt;org&gt;<br/>Chainguard, signed)]
        BR[(BUILD_REGISTRY<br/>built here, smoke-tested,<br/>optionally signed)]
        IBR[(registry1.dso.mil<br/>documented leftovers)]
    end
    K[Kyverno restrict-image-registries<br/>allow list = exactly these] --> trusted
    subgraph build[What a build reads]
        IBRec[Iron Bank recipes<br/>pinned commit]
        Z[plugin zips / binaries<br/>sha256 or digest from<br/>Iron Bank's manifest]
        CGd[org -dev images<br/>pinned by digest]
    end
    build --> BR
```

- A cluster only pulls from the three registries Kyverno allows. The overlay sets that list.
- Every input to a build is pinned: Iron Bank's recipe by commit, each download by sha256 or
  digest, and each base image by digest.
- CI identities are scoped. The Chainguard pull identity is trusted only for this repo's
  `main` branch and pull requests. GitHub Actions are pinned to commit SHAs, and runners are
  hardened. See [SECURITY.md](../SECURITY.md).
