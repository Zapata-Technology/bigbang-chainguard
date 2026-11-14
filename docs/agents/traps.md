# Traps already hit

The five that stop the expensive mistakes are at the bottom of [AGENTS.md](../../AGENTS.md). This file is the rest, plus those five in full, so a playbook can point here.

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
  request then changes only digests, and the smoke kind tests decide whether it merges
  (the marked smoke list in [AGENTS.md](../../AGENTS.md); the weekly run deploys every
  package). `ci.yml`'s `results-gate` job fails while the current `BIGBANG_VERSION` has
  no recorded standard-mode compat matrix. `ci-triage.py` prints EXPECTED and exits 4.
  `automerge.yml` leaves that pull request open: it does not re-run, merge, or add
  `needs-attention`. Do not write pass rows. A skipped run is not a pass. `registry1: true`
  tests stay outside that gate until Iron Bank credentials exist. `verify` is the required
  check. A red gate after the matrix is recorded is a real failure.
- **Wolfi's `helm` package is Helm 4**; `helm-3` is Helm 3, which CI tests (installer image).
- **Velero's placeholder snapshot location** has an empty credential Flux keeps; with S3
  object storage the overlay sets no snapshot locations.
- **Keycloak FIPS** rejects Big Bang's default admin password (`password`) and database
  password (`keycloak`, via the PostgreSQL driver's SCRAM hashing): both need 14+ characters.
  The database password can't change on a running Keycloak (PostgreSQL keeps it).
- **cgr.dev serves layers and signatures from `*.r2.cloudflarestorage.com`**: harden-runner
  must allow it.
- **harden-runner's block let unlisted hosts through until 2026-11-11**, then refused them:
  the runner answers the name with an address that isn't the host's (54.185.253.63) and the
  call fails with `connect: connection refused`. Hosts that had worked unlisted:
  `timestamp.sigstore.dev` (cosign 3's `sign-blob` timestamps there), `storage.googleapis.com`
  (proxy.golang.org serves module zips from it), `grafana.com`, Docker Hub's
  `production.cloudfront.docker.com` and `index.docker.io`, SUSE's registry storage. A passing
  run doesn't prove its list: compare the `domain resolved:` lines in its harden-runner post
  step with the workflow's `allowed-endpoints`, for a run of each event (a pull request's
  build never signs or pushes; `builds.yml` on main does, and needs `timestamp.sigstore.dev`).
  `scripts/egress-audit.py` does that comparison; `ci-triage.py` names the host that refused
  a job's connections.
- Chainguard's Flux controllers can be a minor ahead of Big Bang's CRDs: `flux/` installs
  the matching upstream Flux release (`fluxrelease.py`).
- GHCR refuses the first push to a new package from Actions: `builds.yml` retries the
  push three times; re-run the job if all three fail. repo1.dso.mil intermittently refuses
  GitHub runners: the scripts retry, and `compat.yml` reruns a kind test once.
- **chainctl and cosign are pinned**, one version each in every workflow
  (`tests/test_repo_rules.py`): `setup-chainctl`'s `version` (it installs `latest`
  otherwise, from dl.enforce.dev, checked against that release's `chainctl_checksums.txt`)
  and `cosign-installer`'s `cosign-release`. To raise chainctl: find the newest release
  (`curl -fsSL https://dl.enforce.dev/chainctl/latest/chainctl_checksums.txt` and match
  its checksum against `https://dl.enforce.dev/chainctl/<version>/chainctl_checksums.txt`),
  then change every `version:` at once. A Dependabot bump of either action leaves the
  pinned tool alone.
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
- **GitLab on a small cluster** needs values under `upstream:` (the package chart ignores
  top-level ones). PostgreSQL `max_locks_per_transaction` 256 is the site database: the
  installer notes it, and the kind test sets it. `progress_deadline` sets Deployment
  `progressDeadlineSeconds` to 2400 as a post-renderer patch beside the digest pins.
  `overrides.gitlab` sets the Rails liveness `failureThreshold` to 15. Both reach the
  overlay on the next online `bb-convert.py` (do not run it offline). The kind test still
  patches the deadline. GitLab Runner needs Kyverno for its token and
  `HOME=/home/gitlab-runner` (`overrides`). Leave `GODEBUG=tlsmlkem=0`.
- **A Ready pod isn't a working app**: GitLab Runner stayed Ready while it panicked without a
  token. Give a test a `check` that proves the app did its job, and a `forbid_log`.
- **The test watcher's thresholds are tuned**: Harbor's jobservice crashes 3 times while core
  starts, so a crash loop stops a test only at 6 restarts; GitLab's `dependencies` init
  container exits 1 while it waits, without an error line. Iron Bank images can't be pulled
  without `REGISTRY1_*` and don't stop a test. Check a new rule against a full PR run.
- **The scheduled agent's cloud sandbox reaches GitHub through a proxy that passes only
  REST `repos/<owner>/<repo>/...` paths.** GraphQL (`gh pr list`, `gh pr checks`,
  `gh pr view`, `gh repo view`) and numeric-ID paths (`gh run download`, `gh api --paginate`'s
  next-page links) fail there. `repo-status.py` once turned that failure into "Open pull
  requests: 0", and the daily agent reported all green for a week while bot PRs sat red.
  Maintenance scripts go through `scripts/ghrest.py` (REST only, raises on any failure, pages
  with `page=N`); `tests/test_ghrest.py` refuses the rest. In that sandbox, do the same by
  hand: `gh api repos/<owner>/<repo>/pulls`, `.../commits/<sha>/check-runs`,
  `.../actions/runs/<run>/artifacts` then `.../actions/artifacts/<id>/zip`. An empty answer
  is not "nothing to do" unless the call succeeded.
- On Windows Git Bash, `git show origin/main:.github/...` fails ("ambiguous argument
  'origin\main;.github\...'"): MSYS rewrites the argument as a path. Prefix
  `MSYS_NO_PATHCONV=1`.
- **`RELEASE_PAT` can't read check runs** (403): `automerge.yml` reads with the workflow
  token and uses the bot token only for `gh pr merge`. The bot token is a GitHub App's
  (repository variable `BOT_APP_CLIENT_ID`, secret `BOT_APP_PRIVATE_KEY`; the App needs
  Contents, Pull requests and Workflows read/write on this repo) when set, else `RELEASE_PAT`
  (which then needs the `workflow` scope: the bot branch is rebuilt from main, and a push
  that brings main's workflow-file changes is refused without it).
  GITHUB_TOKEN can't stand in: its pull requests and merges start no workflows.
- **A shell guard must not read a failed call as "no"**: `if gh ... | grep -q x` under
  `set -o pipefail` is false when gh fails, so automerge once would have merged a held PR on
  an API error. Capture first (`labels=$(gh api ...)`, which `set -e` stops on), then test.

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
