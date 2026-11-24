# Running it on your own Chainguard org

The overlays in this repository are generated for one Chainguard org. To run Big Bang on
your own org's images, set up your copy of the repository once with this checklist; after
that, installing and upgrading are the same three commands as in the
[README](../README.md#install).

`scripts/bb-onboard.py` walks the whole list. It changes nothing until you add `--apply`,
and then asks before each change (`--yes` doesn't ask):

```bash
python scripts/bb-onboard.py --org <your org> --repo <owner>/<repo>            # dry run: every step and command
python scripts/bb-onboard.py --org <your org> --repo <owner>/<repo> --apply    # do it
```

## Checklist

1. **Copy the repository** (a fork or a mirror you own) and clone it.
2. **Tools and logins.**
   - `chainctl` logged in with a role in your org: `chainctl auth login`, and
     `chainctl auth login --audience cgr.dev` for pulls and tag listing.
   - `gh` logged in with admin on your copy (variables and secrets need it).
   - Python 3.12 with PyYAML (`pip install -r requirements.txt`) and `helm` 3.
   - `docker`: optional, only to build this repo's images on your machine.
3. **Entitlements.** `python scripts/bb-org-repos.py --org <your org> --quota` shows your
   plan's room per tier and what this repo still needs. See
   [Chainguard entitlements](#chainguard-entitlements).
4. **Org repositories.** `python scripts/bb-org-repos.py --org <your org> --build-bases --apply`
   creates and syncs the repositories Big Bang needs
   ([`docs/required-images.md`](required-images.md) lists them). Ask Chainguard for any it
   reports as **not entitled**.
5. **CI identity.** An assumable Chainguard identity your copy's workflows log in as:
   - issuer `https://token.actions.githubusercontent.com`, subject pattern
     `^repo:<owner>(@\d+)?/<repo>(@\d+)?:(ref:refs/heads/main|pull_request)$` (your
     `main` branch and pull requests only; a fork's pull request gets no token);
   - bound to `registry.pull`, and to a custom role `entitlements-reader` with only
     `registry.entitlements.list` (the quota report in the nightly pull request);
   - its id is the `CGR_ACTIONS_ID` secret.

   `bb-onboard.py` prints (and with `--apply` runs) the `chainctl iam identities create`,
   `chainctl iam roles create` and `chainctl iam role-bindings create` commands.
6. **GitHub secrets and variables** (Settings > Secrets and variables > Actions). Every one
   the workflows read is in the tables below. `bb-onboard.py --apply` sets the three
   variables it knows; set the secrets yourself (`gh secret set <name> --repo <owner>/<repo>`
   reads the value from standard input). It sets a secret only when the value is already in
   its environment.
7. **Your org's exceptions**, if any, in `orgs/<your org>.yaml`: applied over `mapping.yaml`
   for your org only, and validated like it. Each `images` entry replaces the shared entry
   for that Iron Bank image (keep a package you don't license on Iron Bank, say).
   `bb-onboard.py` writes an empty, commented one. Any file already in `orgs/` is an
   example.
8. **Generate the overlays:**

   ```bash
   python scripts/bb-run.py --org <your org> --build-registry ghcr.io/<owner> --save
   ```

   It fetches Big Bang, discovers its images, stops if your org lacks repositories (and
   lists them), regenerates the build specs, writes the overlays and verifies them,
   standard and FIPS. `--save` writes the org and registry into the `CHAINGUARD_ORG` and
   `BUILD_REGISTRY` files. Commit the result on a branch and open a pull request; your
   copy's CI checks it, and `builds.yml` builds, signs and pushes the
   [images this repo builds](how-it-works.md#images-this-repo-builds) when it merges
   (`--push` does it from your machine after `docker login`).
9. **Install** from your copy's releases ([quickstart](quickstart.md)).

Each setting is read in this order: the command-line flag (`--org`, `--build-registry`),
then the environment variable or GitHub repository variable (`CHAINGUARD_ORG`,
`BUILD_REGISTRY`), then the file of the same name at the repo root.

### Secrets

| Secret | Need | What it does |
|---|---|---|
| `CGR_ACTIONS_ID` | required | The CI identity's id (step 5). Every workflow that pulls from the org logs in with it. |
| `BOT_APP_PRIVATE_KEY` | optional | A GitHub App's private key (with the `BOT_APP_CLIENT_ID` variable). The nightly bot (`update.yml`) pushes as the App, and `automerge.yml` merges with it, so CI runs on the bot's pull requests. The App needs contents, pull requests and workflows write. |
| `RELEASE_PAT` | optional | A token used instead of the App. With neither, the bot uses `GITHUB_TOKEN`: CI doesn't run on its pull requests and `automerge.yml` doesn't merge. |
| `REGISTRY1_USERNAME` | optional | Iron Bank login for the kind tests (`compat.yml`), `rke2.yml` and the CVE report. Without it, tests that need an Iron Bank image skip and the report's Iron Bank column is empty. |
| `REGISTRY1_PASSWORD` | optional | The password (CLI secret) for `REGISTRY1_USERNAME`. |

### Variables

| Variable | Need | What it does |
|---|---|---|
| `CHAINGUARD_ORG` | required | Your Chainguard org, unless the `CHAINGUARD_ORG` file names it. |
| `CHAINGUARD_ORG_ID` | recommended | Your org's id (`chainctl iam organizations list`). The signature check then requires Chainguard's signature under your org, not any Chainguard org. |
| `BUILD_REGISTRY` | recommended | Where `builds.yml` pushes the images this repo builds (e.g. `ghcr.io/<owner>`), unless the `BUILD_REGISTRY` file names it. Empty: those images stay on Iron Bank. Never `cgr.dev`. |
| `BOT_APP_CLIENT_ID` | optional | The GitHub App's client id, with the `BOT_APP_PRIVATE_KEY` secret. |
| `SIGN_IMAGES` | optional | `keyless`: sign the images this repo pushes with cosign keyless (bundle and classic `.sig` formats) and attach an SPDX SBOM and a SLSA v1 provenance predicate (`scripts/bb-provenance.py`), not a SLSA Build L3 attestation. The signatures go to the public Sigstore log. |
| `BUILD_SIGNER` | optional | `owner/repo` whose `builds.yml` signs the built images; the signature check requires it. Default: this repository. |
| `AUTOMERGE_BIGBANG_PATCHES` | optional | `true`: a Big Bang patch release merges itself when every check passed. |
| `ALERT_MENTION` | optional | Who `automerge.yml` mentions when a bot pull request needs a person (e.g. a maintainer's `@login`). |
| `RKE2_RUNNER` | optional | A larger runner's label for `rke2.yml` (16 GB or more); default `ubuntu-latest`. |

## Chainguard entitlements

Every image has to be in your org **and** covered by your Chainguard plan. A repository
that isn't covered can still be created, but its sync is refused, so it never gets tags:

```
Error: [PermissionDenied] source repo either doesn't exist or organization does not have an active entitlement
```

The console shows the same as **Contact us for access** next to the image's tags.

- **What your plan covers:** `chainctl images entitlements summary --parent <your org>`
  prints the plan, a quota per tier (APPLICATION, BASE, FIPS) and the images counted
  against it. An "unlimited" plan can still count **base images** (toolchains and runtimes:
  `go`, `glibc-openssl`, `jdk`, `node`, `python`) against a small per-tier quota, and FIPS
  images are often a separate entitlement. A `CATALOG` plan entitles every image in its
  `activeTiers`; the per-tier counts it still shows don't limit anything.
  `python scripts/bb-org-repos.py --org <your org> --quota` adds each tier's room, the
  counted images Big Bang doesn't use, and the repositories this repo still needs. The
  nightly update prints it in its pull request.
- **What Big Bang needs:** [`docs/required-images.md`](required-images.md), standard and
  FIPS, with the packages that use each. Fewer packages need fewer images.
  `python scripts/bb-org-repos.py --org <your org>` prints what discovery needs; `--apply`
  adds it. `--optional` adds the repositories whose images otherwise stay on Iron Bank, and
  `--build-bases` adds `go`, `go-fips`, `glibc-openssl` and `glibc-openssl-fips`, which the
  [images this repo builds](how-it-works.md#images-this-repo-builds) from upstream's Go
  source use: the Mattermost Operator, tempo-query, and the MinIO Operator and its sidecar
  (without them they build with Wolfi's Go).
- **What's not entitled:** ask your Chainguard account team, or "Contact us for access" on
  the image in the console, with the image names and catalog IDs the script prints. Then
  run the script again: the repositories exist and only need the sync. A plan with a swap
  quota can instead drop an entitled image for another; that removes the dropped image from
  your org, so check nothing else uses it.
- **Iron Bank credentials** are still needed for the images with no Chainguard equivalent
  (vendor products and the GitLab image set; [`docs/images.md`](images.md) lists them under
  "Stays on Iron Bank").
