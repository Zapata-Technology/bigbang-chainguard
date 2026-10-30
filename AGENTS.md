# AGENTS.md: maintaining bigbang-chainguard

Read this first if you are an AI agent (or a person new to the repo). It says what is
source and what is generated, the rules that must hold, the commands, and step-by-step
playbooks for the jobs that recur. Details live in the docs it links.

## What this repo is

Values overlays that run **unmodified upstream Big Bang** (`BIGBANG_VERSION`) on
Chainguard images from one Chainguard org, with FIPS as an extra overlay. Nothing is
forked: `scripts/` discover every image Big Bang deploys, map each one (`mapping.yaml`),
pick a tag from the org, and write overlays. Diagrams: [docs/architecture.md](docs/architecture.md).

## Source vs generated

| Edit these | Never hand-edit these (regenerate instead) |
|---|---|
| `mapping.yaml`: image map and every escape hatch | `overlays/*.yaml`, `flux/`, `flux-fips/` (`bb-convert.py`) |
| `orgs/<org>.yaml`: one org's exceptions | `seaweedfs/`, `seaweedfs-fips/` (`bb-convert.py`, from `object-storage.yaml`) |
| `object-storage.yaml`: S3 settings per package | `docs/images.md`, `docs/required-images.md` (`bb-convert.py`) |
| `scripts/*.py`, `builds/*/Dockerfile` | `builds/*/build.yaml` (`bb-<name>.py`) |
| `compat/*.yaml`, `compat/support/*` | `discovery/images.json` (`bb-images.py`) |
| `tests/`, docs other than the two above | |

Settings (`CHAINGUARD_ORG`, `BUILD_REGISTRY`, `OBJECT_STORAGE`) resolve flag > env or repo
variable > the file of that name; the files are comment-only here, so the repo stays
org-neutral.

## Rules (CI or tests enforce most of them)

1. **Images come from the Chainguard org**, from `BUILD_REGISTRY` (images built here FROM
   the org), or stay on Iron Bank with a reason in `mapping.yaml`. Nothing else.
2. **Exact tags**, never `latest`. No `global.imageRegistry` pointing at cgr.dev. Never
   push to cgr.dev or mirror Chainguard images elsewhere.
3. **Never hand-edit generated files** (`tests/test_repo_rules.py` checks their headers).
4. **mapping.yaml is validated** (`bbcommon.validate_mapping`): an unknown section or
   option fails. A new option or section means adding it to `MAPPING_SECTIONS` /
   `IMAGE_OPTIONS` in `scripts/bbcommon.py`, implementing it in `bb-convert.py`, giving it a
   commented block in `mapping.yaml`, a line in the README's escape-hatch list, and a test.
5. **Org-neutral shared files.** Anything true for one org only goes in `orgs/<org>.yaml`.
6. **No secrets** in the repo, logs or test output. Pull credentials come from the site
   file (`registryCredentials`) or CI secrets.
7. **Workflows:** every action pinned to a full commit SHA with the version as a comment;
   top-level `permissions`; harden-runner blocks unlisted hosts in ci/builds/update (add a
   host to `allowed-endpoints` when a new tool needs it).
8. **Every `bb-scan.py` finding is reviewed** in `mapping.yaml` `scan_reviewed`.
9. Python: standard library + PyYAML only; each script starts with a docstring saying what
   it does and how to run it; match the surrounding style (short functions, comments that
   say *why*).

## Commands

```bash
python scripts/check.py              # before every commit: unit tests (+ chart scan if work/ exists)
python scripts/check.py --online     # + overlays current + verify std/FIPS (chainctl, work/)
python scripts/bb-run.py             # fetch, discover, scan, convert, verify: the whole pipeline
python scripts/bb-convert.py         # regenerate overlays after editing mapping.yaml
python scripts/bb-compat.py compat/<test>.yaml [--fips]   # one package on a kind cluster
```

CI: `ci.yml` (every PR), `builds.yml` (images built here), `compat.yml` (kind, PRs touching
`compat/`), `update.yml` (nightly bot PR), `codeql.yml` / `scorecard.yml` (public repo only).

## Playbooks

### The nightly bot PR (`bot/bigbang-update`) fails or reports something
1. Read the PR body: it holds the bb-convert, bb-verify, bb-scan and bb-leftovers output.
2. *Unmapped image* → "Add or fix an image mapping" below.
3. *bb-scan NEW finding* → "A chart scan finding" below.
4. *Leftover PUBLISHED* → "Chainguard published an Iron Bank leftover" below.
5. *Version differences / MINOR AHEAD* are expected and documented; act only if a compat
   test fails.
6. Push fixes to the bot branch or a new branch; never edit its generated files by hand.

### Add or fix an image mapping
Follow [docs/adding-a-package.md](docs/adding-a-package.md). In short: find the image in
`discovery/images.json`, find the Chainguard name (`chainctl images catalog --name <n>`),
add the entry, `bb-convert.py`, `check.py --online`. If the org lacks the repository
(`bb-convert.py --missing` lists them), add it as below ("Add a repository to the org").

### A compat (kind) test fails
1. Get the log: `gh api --allow-escape-sequences repos/<owner>/<repo>/actions/jobs/<id>/logs`
   (find `<id>` with `gh pr checks <pr>`), then search for `PROBLEM`, `RESULT`, `== debug`.
2. Flux uninstalls a release whose install fails, so `kubectl logs` after the fact finds
   nothing. Add a `setup:` line `sh compat/support/watch.sh <name> "<kubectl command>"`
   and a `debug:` line `cat $COMPAT_OUT/watch-<name>.log` to keep the evidence.
3. Fix the cause in `mapping.yaml` (or a builder), not in the test, unless the test itself
   is wrong. Record what you found in `docs/testing.md` ("What the runtime tests found").

### A chart scan finding (`bb-scan.py` NEW)
- *sh script needs bash* on a Chainguard `-dev` image (its `sh` is busybox; UBI's is bash):
  a wait job → `bash_wait_jobs`; a `/bin/sh -c` container → `bash_commands`; an image built
  here → it already links `sh` to bash.
- *Empty default written with toYaml* into an app's config: check whether the app rejects
  it (the Tempo case). Fix with `overrides`.
- Then add the finding's key to `scan_reviewed` with one line saying how it is handled.

### Add a repository to the org
Big Bang orgs have a Chainguard license with unlimited repositories, so add what the
conversion needs. For each name, standard and `-fips`:

```bash
chainctl images catalog --name <name> -o json          # its id and catalog_tier
chainctl images repos create <name> --parent <org> --tier <catalog_tier>
chainctl images repos update <name> --parent <org> --source <id>   # start syncing tags
```

Tags appear after the sync; `bb-convert.py` converts the image on its next run.

### Chainguard published an Iron Bank leftover
Add the repository (above, standard and `-fips`), wait for tags,
replace the `{stays, until}` entry with a `cg:` entry, `bb-convert.py`, run the package's
compat test if it has one.

### Change an image this repo builds
Edit `builds/<name>/Dockerfile` or `scripts/bb-<name>.py`; regenerate `build.yaml` with
`scripts/bb-<name>.py`; the smoke test in that script must cover the change. `builds.yml`
builds on the PR and pushes on merge.

## Traps already hit (don't rediscover them)

- **Chainguard `-dev` images' `/bin/sh` is busybox**; Iron Bank's UBI `sh` is bash. Scripts
  that say `sh` but use arrays or `<<<` die. `bb-scan.py` finds them.
- **Flux's render keeps null chart defaults** that local Helm 3 drops, even when our values
  set them to null (Tempo's `opencensus` receiver). Test with Flux, not only `helm template`.
- **Big Bang creates Mimir's credential Secret whenever its keys are set**, even with Mimir
  off: set object-storage credentials only for enabled packages.
- **Kyverno policies render `preconditions.all: null`** unless some container is excluded.
- Chainguard's Flux controllers can be a minor ahead of Big Bang's CRDs: `flux/` installs
  the matching upstream Flux release (`fluxrelease.py`).
- GHCR refuses the first push to a new package from Actions: re-run the failed job.
  repo1.dso.mil intermittently refuses GitHub runners: the scripts retry; re-run if not.
- On Windows: git checks out with CRLF (render charts from a Linux checkout if line endings
  matter), `docker-credential-cgr` may be blocked (no local cgr.dev pulls; debug in CI),
  and shell heredocs turn `\n` in Python strings into real newlines (write the script
  to a file instead).
- `bb-convert.py --offline` with a stale `.tags.json` rewrites overlays with old tags:
  regenerate online.

## Where to look

[README.md](README.md) (using it), [docs/architecture.md](docs/architecture.md) (how it
works), [docs/testing.md](docs/testing.md) (test layers, results, findings),
[docs/adding-a-package.md](docs/adding-a-package.md), [SECURITY.md](SECURITY.md),
[CONTRIBUTING.md](CONTRIBUTING.md).
