# AGENTS.md: maintaining bigbang-chainguard

Read this first if you are an AI agent (or a person new to the repo). It says what is
source and what is generated, the rules that must hold, the commands, and the loop.
One playbook per job lives in [docs/agents/](docs/agents/). The long trap list is
[docs/agents/traps.md](docs/agents/traps.md). Details live in the docs this file links.

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
| `object-storage.yaml`: S3 settings per package | `docs/images.md`, `docs/required-images.md`, `docs/version-differences.md` (`bb-convert.py`) |
| `scripts/*.py`, `scripts/bbinstall/`, `builds/*/Dockerfile`, `builds/*/recipe.yaml` | `builds/*/build.yaml` (`bb-build.py <name>`) |
| `compat/*.yaml`, `compat/support/*` | `discovery/images.json` (`bb-images.py`) |
| `docs/test-results.json` `tests`: each kind test's description | `docs/testing.md`'s results table, between its `results:start`/`end` markers, and `docs/test-results.json` `results` (`test-results.py`, from compat.yml's result artifacts) |
| `tests/`, docs other than the ones above (and the rest of `docs/testing.md`) | |

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
   commented block in `mapping.yaml`, a line in docs/how-it-works.md's escape-hatch list, and a test.
5. **Org-neutral shared files.** Anything true for one org only goes in `orgs/<org>.yaml`.
6. **No secrets** in the repo, logs or test output. Pull credentials come from the site
   file (`registryCredentials`) or CI secrets.
7. **Workflows:** every action pinned to a full commit SHA with the version as a comment;
   top-level `permissions`; harden-runner blocks unlisted hosts in ci/builds/update/rke2 (add a
   host to `allowed-endpoints` when a new tool needs it). A VM test's downloads are pinned by
   SHA256 and its hosts allowed (`tests/test_repo_rules.py`).
8. **Every `bb-scan.py` finding is reviewed** in `mapping.yaml` `scan_reviewed`.
9. Python: standard library + PyYAML only (pinned in `requirements.txt`; pytest and ruff,
   for CI only, in `requirements-dev.txt`); each script starts with a docstring saying what
   it does and how to run it; match the surrounding style (short functions, comments that
   say *why*). `ruff check .` must pass (`pyproject.toml`: unused or undefined names,
   statement errors; no formatter).

## Commands

```bash
python scripts/check.py              # before every commit: unit tests, ruff (+ chart scan if work/ exists)
python scripts/check.py --online     # + overlays current + verify std/FIPS (chainctl, work/)
python scripts/bb-run.py             # fetch, discover, scan, convert, verify: the whole pipeline
python scripts/bb-convert.py         # regenerate overlays after editing mapping.yaml (online only; see traps)
python scripts/bb-compat.py compat/<test>.yaml [--fips]   # one package on a kind cluster
python scripts/bb-install.py check --site my-site.yaml [--fips]     # every setting tested, nothing changed
python scripts/bb-install.py install --site my-site.yaml [--fips]   # what a user runs to deploy
python scripts/bb-install.py upgrade --site my-site.yaml [--fips]   # a running cluster; converts a Big Bang 3 site file
scripts/bb-docker.sh install                                       # the same, from the installer image
python scripts/bb-install.py status                              # health of a deployed cluster
python scripts/repo-status.py         # branch, dirty files, version, results-gate, open PRs
python scripts/bb-preview.py          # Big Bang's development branch against this repo: what would break
python scripts/ci-triage.py <pr>      # why a PR's checks failed; FLAKE, FAILED, or EXPECTED
python scripts/test-results.py update # docs/testing.md's results table (--from-main N: main's runs)
python scripts/ci-triage.py --flakes  # jobs re-run in the last 30 days; FIX at 3+ re-runs
python scripts/egress-audit.py        # hosts a workflow reached that its harden-runner list doesn't name
python scripts/bb-org-repos.py --quota   # Chainguard plan: room per tier, what's needed, what's unused
```

CI: `ci.yml` (every PR), `builds.yml` (images built here), `compat.yml` (kind, PRs touching
`compat/` or the installer), `update.yml` (nightly bot PR), `automerge.yml` (merges that PR
when every check passed; not a new Big Bang release, a `hold` label, or an expected red
`results-gate`), `release.yml` (tags
`<bigbang>-cg.<n>` on main when deployable files change), `cve-report.yml` (weekly Iron Bank
vs Chainguard grype counts, with each Critical and its fix and the most common Highs), `fips-kernel.yml` (weekly: the FIPS overlay
on a FIPS-mode kernel, k3s in a KVM VM; see docs/testing.md), `rke2.yml` (weekly: Big Bang's default
packages with `bb-install.py` on RKE2's CIS profile, PSA `restricted`, in a KVM VM), `bigbang-preview.yml` (daily:
`bb-preview.py` against Big Bang's development branch and its untagged `release-X.Y.Z`
branch, which it also deploys on a few kind tests; keeps one `bigbang-preview` issue per ref current), `codeql.yml` / `scorecard.yml` (public repo only).

## Maintaining this repo with an AI agent

Start with `python scripts/repo-status.py`. It prints the branch, dirty files, `BIGBANG_VERSION`,
and whether `results-gate` is expected red on this checkout, before it asks GitHub. An empty
GitHub answer is not "nothing to do" unless the call succeeded.

1. **Start:** `gh api user --jq .login` (the account that may push), then
   `python scripts/repo-status.py`. An open bot PR (`bot/bigbang-update`, `bot/bigbang-release`,
   `bot/builds-*`) or a failed run on main comes first.
2. **A failed check:** `python scripts/ci-triage.py <pr>` (or `--run <id>`). It prints each
   failed job's evidence and says FLAKE (repo1.dso.mil, a network timeout, a 5xx download, a
   rate limit), FAILED, or EXPECTED (the compat matrix is not recorded yet). Re-run flakes once
   (`--rerun-flakes`); a second failure is real. A job that `ci-triage.py --flakes` marks
   FIX (re-run 3+ times in 30 days) needs its cause found, not another re-run. For FAILED, open
   the matching file in [docs/agents/](docs/agents/), with the artifact's `diag/` logs
   (`gh run download <run> -n compat-<test>-<mode>`).
3. **Change:** a branch from `origin/main`, the smallest fix in source files (never the
   generated ones), `python scripts/check.py`, a PR. A pull request runs only the kind tests
   its change reaches (`scripts/compat-plan.py`: a changed `compat/<test>.yaml`, a package
   whose overlay tag changed, the installer tests for `scripts/bbinstall/`, the smoke tests
   in the marked list below for harness, Flux, mapping and digest-only changes, nothing for
   docs); the weekly run and a new `BIGBANG_VERSION` run them all. Runner minutes are billed:
   push a branch once it is ready, not after every edit (a new push cancels the PR's older
   runs), and don't run several agents that each push PRs touching the overlays at once.
4. **Wait without polling hard:** check every 3 minutes or more; parallel agents share the
   GitHub API rate limit. GitHub releases a job's log only when the job ends, so a kind test
   reports its own progress (`LIVE` lines, an early stop) in its log and artifact.
5. **Merge** when every check passed (`gh pr merge <n> --squash --delete-branch`), with one
   exception. `results-gate` (the check named `compat matrix recorded for this Big Bang version`)
   stays red while this `BIGBANG_VERSION` has no recorded standard-mode matrix. `ci-triage.py`
   prints EXPECTED when the log says `compat results are missing`, and exits 4. Do not write
   pass rows. `verify` is the required check. Record real artifacts with
   `python scripts/test-results.py update` after a compat run. A red gate after that matrix
   exists is a real failure. Branch protection requires only `verify`, so check the rest
   yourself. The nightly bot PR merges itself (`automerge.yml`) unless it moves Big Bang,
   carries `hold`, or the gate is still red. A new Big Bang release comes in its own PR
   (`bot/bigbang-release`); a person merges it after reading `docs/upgrades/<version>.md`
   (a patch release merges itself when the `AUTOMERGE_BIGBANG_PATCHES` variable is `true`).
6. **Record:** a runtime finding goes in `docs/testing.md` ("What the runtime tests found")
   and, if a site must act, the README; a new trap goes in [docs/agents/traps.md](docs/agents/traps.md).
   `docs/testing.md`'s results table is generated: the nightly bot PR brings main's newest
   compat.yml results (`scripts/test-results.py update --from-main 20` does the same by hand).

Parallel agents: give each its own worktree and its own files (one per `compat/*.yaml`
test, say); keep shared files (the README, `docs/testing.md`, this file) with one agent.
Never print or copy secrets, and never change git config. Do not commit `.claude/`.

## Playbooks

| Job | File |
|---|---|
| Nightly bot PR (`bot/bigbang-update`, `bot/bigbang-release`) | [docs/agents/nightly-bot.md](docs/agents/nightly-bot.md) |
| Big Bang preview issue | [docs/agents/preview.md](docs/agents/preview.md) |
| Add or fix an image mapping | [docs/agents/mapping.md](docs/agents/mapping.md) |
| A kind test fails | [docs/agents/compat-failure.md](docs/agents/compat-failure.md) |
| A chart scan finding | [docs/agents/chart-scan.md](docs/agents/chart-scan.md) |
| Add a repository to the org | [docs/agents/add-repository.md](docs/agents/add-repository.md) |
| Chainguard published an Iron Bank leftover | [docs/agents/iron-bank-leftover.md](docs/agents/iron-bank-leftover.md) |
| Change how Big Bang is deployed | [docs/agents/installer.md](docs/agents/installer.md) |
| Write a compat test | [docs/agents/compat-test.md](docs/agents/compat-test.md) |
| Disconnected sites | [docs/agents/airgap.md](docs/agents/airgap.md) |
| Add a cluster-side setting | [docs/agents/cluster-setting.md](docs/agents/cluster-setting.md) |
| Change an image this repo builds | [docs/agents/change-build.md](docs/agents/change-build.md) |
| Add an image this repo builds | [docs/agents/add-build.md](docs/agents/add-build.md) |
| Traps already hit | [docs/agents/traps.md](docs/agents/traps.md) |

## Traps that stop the expensive mistakes

The rest are in [docs/agents/traps.md](docs/agents/traps.md). These five are the ones an
agent hits before it finishes reading that file.

- **Do not run `bb-convert.py` offline, and do not hand-edit generated files.** Offline
  conversion rewrites overlays with a stale tag list. A mapping change lands in source;
  `update.yml` on main regenerates the overlays. The identity trusts main and pull requests,
  not an arbitrary branch.
- **Push a branch once.** A new push cancels that pull request's kind, build, and RKE2
  jobs. Those runs are billed. Put the docs change in the same commit as the code, or on
  a second branch.
- **`results-gate` red is expected until the matrix exists.** The smoke set is
  <!-- smoke-tests -->
  `kyverno-enforce`, `metrics-server`, `object-storage`, `keycloak`, `kiali`, `velero`
  <!-- /smoke-tests -->
  . The gate also requires a standard pass for
  <!-- required-tests -->
  `release-upgrade-keycloak`, `release-upgrade-mattermost`, `installer-upgrade-to`, `keycloak-migration`, `mattermost-migration`
  <!-- /required-tests -->
  . Large tests and `registry1: true` tests stay outside the gate (the marked list in
  [docs/testing.md](docs/testing.md)). A skip is not a pass. Do not write pass rows.
  `verify` is the required check. `python scripts/test-results.py update` records real
  artifacts. A red gate after the matrix is recorded is a real failure.
- **The scheduled agent's sandbox reaches GitHub only through REST**
  `repos/<owner>/<repo>/...`, via `scripts/ghrest.py`. GraphQL (`gh pr list`, `gh pr checks`)
  and an empty answer are not "nothing to do."
- **On Windows,** git checks out with CRLF, `C:\Windows\System32\bash.exe` is not `/bin/bash`
  (two tests fail locally; CI is Ubuntu; do not change product code for that), Git Bash
  rewrites `origin/main:path` unless `MSYS_NO_PATHCONV=1`, a heredoc eats backslashes (write
  a `.py` file and run it), and paths past 260 characters fail. Do not commit `.claude/`.

## Where to look

[README.md](README.md) (using it), [docs/architecture.md](docs/architecture.md) (how it
works), [docs/testing.md](docs/testing.md) (test layers, results, findings),
[docs/adding-a-package.md](docs/adding-a-package.md), [SECURITY.md](SECURITY.md),
[CONTRIBUTING.md](CONTRIBUTING.md), [docs/agents/](docs/agents/) (playbooks).
