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
2. **Exact tags**, never `latest`, and org images pinned by digest (`Converter.digest_pins`:
   a Flux post-renderer, so values keep the tag). No `global.imageRegistry` pointing at
   cgr.dev. Never push to cgr.dev or mirror Chainguard images elsewhere.
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
python scripts/bb-install.py install --site my-site.yaml [--fips]   # what a user runs to deploy
scripts/bb-docker.sh install                                       # the same, from the installer image
python scripts/bb-install.py status                              # health of a deployed cluster
python scripts/repo-status.py         # where the repo stands: open PRs + checks, last run per workflow
python scripts/ci-triage.py <pr>      # why a PR's checks failed; FLAKE or FAILED (--rerun-flakes)
```

CI: `ci.yml` (every PR), `builds.yml` (images built here), `compat.yml` (kind, PRs touching
`compat/` or the installer), `update.yml` (nightly bot PR), `automerge.yml` (merges that PR
when every check passed; not a new Big Bang release or a `hold` label), `release.yml` (tags
`<bigbang>-cg.<n>` on main when deployable files change), `cve-report.yml` (weekly Iron Bank
vs Chainguard grype counts, with each Critical and its fix), `fips-kernel.yml` (weekly: the FIPS overlay
on a FIPS-mode kernel, k3s in a KVM VM; see docs/testing.md), `codeql.yml` / `scorecard.yml` (public repo only).

## Maintaining this repo with an AI agent

The loop, for a person or an agent picking the repo up:

1. **Start:** `gh api user --jq .login` (the account that may push), then
   `python scripts/repo-status.py`. An open `bot/bigbang-update` PR or a failed run on main
   comes first.
2. **A failed check:** `python scripts/ci-triage.py <pr>` (or `--run <id>`). It prints each
   failed job's evidence (a kind test's from its artifact) and says FLAKE (repo1.dso.mil,
   a network timeout, a 5xx download, a rate limit) or FAILED. Re-run flakes once
   (`--rerun-flakes`); a second failure is real. For FAILED, follow the playbook below that
   matches, with the artifact's `diag/` logs (`gh run download <run> -n compat-<test>-<mode>`).
3. **Change:** a branch from `origin/main`, the smallest fix in source files (never the
   generated ones), `python scripts/check.py`, a PR. A change to one `compat/<test>.yaml`
   runs just that kind test (`scripts/compat-plan.py`); a mapping or harness change runs them
   all (20-45 minutes).
4. **Wait without polling hard:** check every 3 minutes or more; parallel agents share the
   GitHub API rate limit. GitHub releases a job's log only when the job ends, so a kind test
   reports its own progress (`LIVE` lines, an early stop) in its log and artifact.
5. **Merge** when every check passed (`gh pr merge <n> --squash --delete-branch`); branch
   protection requires only `verify`, so check the rest yourself. The nightly bot PR merges
   itself (`automerge.yml`) unless it moves Big Bang or carries `hold`.
6. **Record:** a runtime finding goes in `docs/testing.md` ("What the runtime tests found")
   and, if a site must act, the README; a new trap goes below. Keep `docs/testing.md`'s
   results current.

Parallel agents: give each its own worktree and its own files (one per `compat/*.yaml`
test, say); keep shared files (the README, `docs/testing.md`, this file) with one agent.
Never print or copy secrets, and never change git config.

## Playbooks

### The nightly bot PR (`bot/bigbang-update`) fails or reports something
A green bot PR merges itself (`automerge.yml`), so one still open has a failed check, a
`hold` label, or a new `BIGBANG_VERSION`. Label it `hold` while you work on it.
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
1. `python scripts/ci-triage.py <pr>` shows each failure's evidence and whether it's a flake.
   For more, download the evidence: `gh run download <run id> -n compat-<test>-<mode>` (pass or fail;
   `docs/testing.md` "Reading a test's logs" lists the files). Start with `compat.log`
   (`PROBLEM` (a `stopped early:` one names the cause), `LIVE` (each container's first
   error line, when it happened), `RESULT`, `EXITED`), then `diag/exits.txt` and the crashed container's
   `diag/logs/<ns>/<pod>.<container>.previous.log`; Pending pods: `diag/cluster/nodes.txt`.
2. Flux uninstalls a release whose install fails, so `kubectl logs` after the fact finds
   nothing; the harness keeps every container's log while the test runs. For anything
   else (a CR's status, a file in a pod), add a `setup:` line
   `sh compat/support/watch.sh <name> "<kubectl command>"` and a `debug:` line
   `cat $COMPAT_OUT/watch-<name>.log`.
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
`scripts/bb-org-repos.py --apply [names]` does both steps for each name (default: what
`bb-convert.py --missing` lists). If the sync fails with `PermissionDenied ... does not
have an active entitlement`, the plan doesn't cover the image. Don't retry; report it for
a person (`chainctl images entitlements summary` shows the per-tier quotas). Only
Chainguard can add it.

### Chainguard published an Iron Bank leftover
Add the repository (above, standard and `-fips`), wait for tags,
replace the `{stays, until}` entry with a `cg:` entry, `bb-convert.py`, run the package's
compat test if it has one.

### Change how Big Bang is deployed
The user-facing path is `scripts/bb-install.py`; keep it and docs/install.md's "Doing it by hand"
commands in step. Cluster helpers it shares with the kind harness live in
`scripts/bbcluster.py`. `compat/installer.yaml` (`installer: true`) runs it end to end on
kind; `tests/test_install.py` covers its decisions without a cluster. A new credential or
generated value goes in the `bigbang/bb-install` Secret through `Credentials` (a pull token it
makes with chainctl too: `chainctl_pull_token` captures the output), never on a
command line or in output.

### Write a compat test
`compat/<name>.yaml`: `enable` (package names, e.g. `[kyverno, headlamp]`), `namespaces`, `expect` (image fragments that
must run; none running fails the test), `check` (required: commands that prove the app does
its job, not just that pods are Ready), and as needed `setup`, `values`, `values_files`, `forbid_log`, `debug`, `check`
(commands that must exit 0), `installer: true` with `installer_args`, `large: true` and
`timeout` (weekly, slow packages), `registry1: true` (pulls from Iron Bank: SKIPPED, exit 0,
without `REGISTRY1_USERNAME`/`REGISTRY1_PASSWORD`), and `upgrade_from` (`images`: the mapping
to install first; `check`: commands on that install; `upgrade`: commands after Big Bang is
upgraded to the test's mapping, before the wait; `values`: values for the starting install
only; see `compat/vault-upgrade.yaml`). Unknown
keys fail `tests/test_compat_files.py`; a new key needs code in `bb-compat.py` and a line here.

### Disconnected sites
`scripts/bb-airgap.py` writes the bundle (chart, rendered Flux, bare git clones, images.txt);
`bb-install.py --git-mirror/--registry-mirror` installs from the site's copies
(`mirrored_checkout` rewrites `cgr.dev/<org>` and the build registry, keeping headers). The
tooling never copies or pushes images: that is the site's step (repo rule 2).

### Add a cluster-side setting to the installer
Settings applied after Big Bang (TLS, the signature policy) follow one pattern: a
`*_choice` that reads the flag or the remembered value from `Credentials`, a manifest
function, `apply_when_ready(docs, crd, namespace, check, wait)` (waits for the package's
CRD and namespace and its admission webhook) and a `*_problem` that `status` reports.

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
- **Big Bang 3's Kyverno policies render `preconditions.all: null`** unless some container is
  excluded (`excludeContainers`); Big Bang 4's CEL policies don't, and its chart refuses that key.
- **Big Bang's chart creates every package namespace itself** (`monitoring` comes from the
  `prometheusOperatorCRDs` package): never pre-create one, Helm refuses to adopt it. With
  monitoring or Thanos on, keep `prometheusOperatorCRDs` and `certManager` on (the installer checks).
- **Flux fetches package charts from repo1 git at runtime**, so a repo1 outage shows up as
  a HelmRelease "chart is not ready". `bbcluster.source_problems` names the host and
  nudges Flux to retry; `--git-mirror` moves every package `git.repo` to a site mirror.
- **registry1's OCI copy of the Big Bang chart needs an Iron Bank login**; repo1's git
  archive of the same tag doesn't. `bb-install.py` downloads the archive (`fetch_chart`).
- **Big Bang charts pin Iron Bank's uid** (Keycloak: 1000). Chainguard images run as their
  own user (65532) and may keep files unreadable to others: Keycloak 26.8.1's
  `quarkus-application.dat` is 0770 65532/65532, so `overrides.keycloak` unsets runAsUser.
  A crash on the first file read after a Chainguard rebuild is this.
- **Go refuses removed GODEBUG settings**: GitLab's chart sets `tlskyber=0`, which the Go
  Chainguard builds with removed, so Gitaly, Shell and the registry died at start;
  `overrides.gitlab` sets `GODEBUG=tlsmlkem=0`. Expect this for any chart that pins Go knobs.
- **Nightly digest changes are normal**: Chainguard rebuilds images daily; the bot's pull
  request then changes only digests, and the kind tests decide whether it merges.
- **Wolfi's `helm` package is Helm 4**; `helm-3` is Helm 3, which CI tests (installer image).
- **Velero's placeholder snapshot location** has an empty credential Flux keeps; with S3
  object storage the overlay sets no snapshot locations.
- **Keycloak FIPS** rejects Big Bang's default admin password (`password`) and database
  password (`keycloak`, via the PostgreSQL driver's SCRAM hashing): both need 14+ characters.
  The database password can't change on a running Keycloak (PostgreSQL keeps it).
- **cgr.dev serves layers and signatures from `*.r2.cloudflarestorage.com`**: harden-runner
  must allow it.
- Chainguard's Flux controllers can be a minor ahead of Big Bang's CRDs: `flux/` installs
  the matching upstream Flux release (`fluxrelease.py`).
- GHCR refuses the first push to a new package from Actions: `builds.yml` retries the
  push three times; re-run the job if all three fail. repo1.dso.mil intermittently refuses
  GitHub runners: the scripts retry, and `compat.yml` reruns a kind test once.
- **Sign before the tag moves.** With enforce the default, a tag that points at an unsigned
  digest even for a minute refuses Pods. `builds.yml` pushes `<tag>-staging`, signs, then
  pushes `<tag>`; keep that order in any new publish step.
- On Windows: git checks out with CRLF (render charts from a Linux checkout if line endings
  matter), `docker-credential-cgr` may be blocked (no local cgr.dev pulls; debug in CI),
  Git Bash heredocs collapse backslashes (`\n` becomes a newline, `\b` a backspace, `\\(`
  becomes `\(`; write a Python script with the Write tool and run the file), and paths
  past 260 characters fail (Big Bang's chart has deep test paths: build bundles under a
  short directory). Offline `bb-convert.py` drops images
  without `work/`; the update workflow can't log into Chainguard from a branch (the
  identity trusts main and pull requests), so a mapping change lands first and
  `update.yml` on main regenerates the overlays.
- **An org image stops being rebuilt** when Chainguard drops its line (Vault's kubectl 1.30):
  `bb-age.py` fails CI after 30 days. Raise the line with `min_version` or `version`.
- `bb-convert.py --offline` with a stale `.tags.json` rewrites overlays with old tags:
  regenerate online.
- **Large kind tests (GitLab) run on a PR that changes only their `compat/<test>.yaml`**, or
  by hand from main (`gh workflow run compat.yml --ref main -f tests=gitlab`). A manual run
  from any other branch fails at "Log into Chainguard" (the identity trusts main and pull
  requests), and other pull requests skip large tests.
- **GitLab on a small cluster** needs, in its test and possibly at a site: values under
  `upstream:` (the package chart ignores top-level ones), PostgreSQL
  `max_locks_per_transaction` 256, a Deployment `progressDeadlineSeconds` past the
  migrations (`compat/support/progress-deadline.py`: Helm replaces lists, so it repeats the
  overlays' digest-pin post-renderer), and a liveness allowance for Rails' boot. GitLab
  Runner needs Kyverno for its token and `HOME=/home/gitlab-runner` (`overrides`).
- **A Ready pod isn't a working app**: GitLab Runner stayed Ready while it panicked without a
  token. Give a test a `check` that proves the app did its job, and a `forbid_log`.
- **The test watcher's thresholds are tuned**: Harbor's jobservice crashes 3 times while core
  starts, so a crash loop stops a test only at 6 restarts; GitLab's `dependencies` init
  container exits 1 while it waits, without an error line. Iron Bank images can't be pulled
  without `REGISTRY1_*` and don't stop a test. Check a new rule against a full PR run.
- **`RELEASE_PAT` can't read check runs** (403): `automerge.yml` reads with the workflow
  token and uses `RELEASE_PAT` only for `gh pr merge`.

- **Writing a check:** `kubectl get --raw` (the API server proxy) for reads; `kubectl create
  --raw` posts without a JSON content type (Loki 400, Tempo and Mimir 415, Argo CD
  UnsupportedMediaType), so POST JSON through `kubectl port-forward` and curl or the
  runner's Python. STRICT mTLS (Kiali) refuses the API server proxy: port-forward. The
  org's `busybox` has no httpd or wget: use its `nginx` and the `kubectl` `-dev` image. Big
  Bang's Keycloak serves under `/auth`. Pass secrets on stdin, never print them.
- **Big Bang's `istioEgressGateway` renders only in ambient mode** (`istioCNI`, `ztunnel`).
- **The log keeper polls every 30 s**: a pod that lives less (cert-manager's
  startupapicheck, Helm wait jobs) may leave no log in the artifact.
- **Sigstore's log can answer late**: Rekor stores the entry, cosign times out, and its own
  retry gets `409 createLogEntryConflict`. `builds.yml` re-runs cosign (new ephemeral keys,
  a new entry); `ci-triage.py` calls it a flake, as it does a Go proxy `stream error`.
- **Parallel `cosign verify` races on its Sigstore cache** ("trusted root is required"):
  `bb-signatures.py --jobs` verifies a failed image again alone before reporting it.
- **Big Bang 4 keys:** every package is `packages.<name>` (3.x had `<name>` and
  `addons.<name>`, which 4's chart refuses). mapping.yaml's per-package sections use that
  key; `bbcommon.bigbang_packages` reads either layout, so a release-upgrade test can start
  from a 3.x release (`bb-compat.legacy_values`). Big Bang 4 needs Kubernetes 1.35+ (kind
  node image, `bb-images.KUBE_VERSION`, the FIPS VM's k3s) and turns Istio ambient on by
  default; a test without istiod turns it off (`test_values`).
- **Big Bang 4's Kyverno policies are CEL `ValidatingPolicy`** objects
  (`validatingpolicies.policies.kyverno.io`); the installer's signature policy is still a
  `ClusterPolicy` (Kyverno 1.19 runs both).
- **YAML parsing:** use `bbcommon.safe_load` / `SAFE_LOADER` / `BASE_LOADER` (libyaml when
  PyYAML has it): the pure-Python parser was most of `bb-images`' 75 s.

## Where to look

[README.md](README.md) (using it), [docs/architecture.md](docs/architecture.md) (how it
works), [docs/testing.md](docs/testing.md) (test layers, results, findings),
[docs/adding-a-package.md](docs/adding-a-package.md), [SECURITY.md](SECURITY.md),
[CONTRIBUTING.md](CONTRIBUTING.md).
