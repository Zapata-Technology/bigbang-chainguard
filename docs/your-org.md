# Running it on your own Chainguard org

The overlays in this repository are generated for Zapata Technology's org. To run Big Bang
on your own Chainguard images, set this up once in your copy of the repository; after
that, installing and upgrading are the same three commands as in the
[README](../README.md#install).

## What you need

- **A Chainguard subscription with the images Big Bang uses.**
  - [`docs/required-images.md`](required-images.md) lists every repository and which
    packages use it. It is generated and the same for every org: about 90 standard images,
    plus their `-fips` twins if you want FIPS.
  - If you enable fewer packages, you need fewer images.
  - `scripts/bb-convert.py --missing` lists the ones your org lacks, and
    `scripts/bb-org-repos.py` adds them. Check your **entitlement** first; see
    [Chainguard entitlements](#chainguard-entitlements).
- **A registry you can push to** (e.g. `ghcr.io/<owner>`). Three of Big Bang's images are
  Big Bang's own builds, and seven more need what Chainguard's image lacks or has no image
  of (Vault, the JMX exporter, the Mattermost Operator, tempo-query, the MinIO Operator and
  its sidecar, GitLab's gitaly-init-cgroups); this repo builds
  those FROM your Chainguard images; see
  [Images this repo builds](how-it-works.md#images-this-repo-builds). This is optional: without a
  registry, those images stay on Iron Bank.
- **Iron Bank credentials, for the images with no Chainguard equivalent.** These are
  vendor products and the GitLab image set. [`docs/images.md`](images.md) lists them
  under "Stays on Iron Bank", with the reasons.
- **Tools:** `helm` 3, Python 3.12 with `pyyaml`, and `chainctl`.

## Generate the overlays for your org

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
so later runs need no flags.

**The images this repo builds** are built, tested, signed and pushed by your copy's CI
(`builds.yml`) when the change merges to main; there is nothing to run. To push them from
your machine instead, `docker login` to your registry and add `--push` to the same command.

Each setting is read in this order: the command-line flag (`--org`, `--build-registry`),
then the environment variable or GitHub repository variable (`CHAINGUARD_ORG`,
`BUILD_REGISTRY`), then the file of the same name at the repo root.

## Chainguard entitlements

Every image has to be in your org **and** covered by your Chainguard plan. A repository
that isn't covered can still be created, but its sync is refused, so it never gets tags:

```
Error: [PermissionDenied] source repo either doesn't exist or organization does not have an active entitlement
```

The console shows the same as **Contact us for access** next to the image's tags. Before you
start:

1. **See what your plan covers.**

   ```bash
   chainctl images entitlements summary --parent <your org>
   ```

   It prints the plan, a quota per tier (APPLICATION, BASE, FIPS) and the images counted
   against it. An "unlimited" plan can still count **base images** (toolchains and runtimes:
   `go`, `glibc-openssl`, `jdk`, `node`, `python`) against a small per-tier quota, and FIPS
   images are often a separate entitlement. A full tier means Chainguard must raise it.
2. **See what Big Bang needs.** [`docs/required-images.md`](required-images.md) lists
   every repository, standard and FIPS, and which packages use it.
3. **Add them.** This prints the commands; `--apply` runs them:

   ```bash
   python scripts/bb-org-repos.py --org <your org>                   # what discovery needs
   python scripts/bb-org-repos.py --org <your org> --build-bases --apply
   ```

   - `--optional` adds the repositories whose images otherwise stay on Iron Bank.
   - `--build-bases` adds `go`, `go-fips`, `glibc-openssl` and `glibc-openssl-fips`, which
     two of the [images this repo builds](how-it-works.md#images-this-repo-builds) use when your org has
     them:
     - The Mattermost Operator builds with Wolfi's Go on `kubectl` without `go`/`go-fips`.
     - NeuVector's registry adapter needs `glibc-openssl`(`-fips`) and stays on Iron Bank
       without it.
   - The script reports each image it couldn't sync as **not entitled**.
4. **Ask Chainguard for what's not entitled.** Use your account team, or "Contact us for
   access" on the image in the console. Give them the image names and catalog IDs; the
   script prints them. Then run the script again: the repositories already exist and only
   need the sync.
   - A plan with a swap quota can instead drop an entitled image for another (`chainctl
     images entitlements summary` shows the quota). That removes the dropped image from
     your org, so check nothing else uses it.

**Your own exceptions go in `orgs/<org>.yaml`.** It is applied over `mapping.yaml` for your
org only. Each `images` entry there replaces the shared entry for that Iron Bank image. Use
it for things like keeping a package you don't license on Iron Bank, or using an image you
already build. `orgs/zapatatechnology.com.yaml` is an example.

## CI for your copy

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
