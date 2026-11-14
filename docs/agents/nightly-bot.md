# The nightly bot PR

Part of the maintainer playbooks. The loop and the rules are in [AGENTS.md](../../AGENTS.md).

### The nightly bot PR (`bot/bigbang-update` or `bot/bigbang-release`) fails or reports something
`update.yml` has two tracks: `bot/bigbang-update` keeps the deployed Big Bang release on
the newest org patches and builds, and `bot/bigbang-release` (only while repo1 has a newer
release) moves to the new release, with upgrade notes (`scripts/bb-release-notes.py`,
`docs/upgrades/<version>.md`) in its body. Work on a release never holds back the routine
track. A green routine PR merges itself (`automerge.yml`), so one still open has a failed
check or a `hold` label; a release PR waits for a person. `automerge.yml` triages a failed first attempt
with main's `ci-triage.py --run` and re-runs it only when every failure is a known flake; a
real failure isn't re-run. A real failure, a failure that survived the re-run, a triage
that couldn't read GitHub, a new release, or a commit that isn't the bot's gets the
`needs-attention` label and a comment saying which (remove the label once handled). A flake
pattern `ci-triage.py` doesn't know yet shows up here as a real failure: add it to its
`FLAKES` (with a test) rather than re-running by hand each time. Label the PR `hold` while you work on it:
`update.yml` then leaves its branch alone.

A red `results-gate` (the check `compat matrix recorded for this Big Bang version`) while
this `BIGBANG_VERSION` has no recorded standard matrix is expected. `ci-triage.py` prints
EXPECTED and exits 4. `automerge.yml` then leaves the pull request open: it does not merge,
does not re-run the job, and does not add `needs-attention`. Do not label the pull request
`hold`, do not push to the bot branch, and do not write pass rows. The weekly `compat.yml`
records the rest of the matrix. `python scripts/test-results.py update --from-main 20`
(or `update --run <id>`) copies real artifacts into `docs/test-results.json`. A red gate
after that matrix is recorded is a real failure.

1. Read the PR body: it holds the bb-convert, bb-verify, bb-scan and bb-leftovers output.
2. *Unmapped image* → [mapping.md](mapping.md).
3. *bb-scan NEW finding* → [chart-scan.md](chart-scan.md).
4. *Leftover PUBLISHED* → [iron-bank-leftover.md](iron-bank-leftover.md).
5. *Version differences / MINOR AHEAD* are expected and documented; act only if a compat
   test fails.
6. Fix on main (a PR from a new branch), never on the bot branch: `update.yml` rebuilds
   that branch from main, and `automerge.yml` refuses a bot PR with any commit but the
   bot's. A merged change to `mapping.yaml`, `orgs/`, the converter scripts or a build
   recipe re-runs `update.yml` on main; otherwise dispatch it (`gh workflow run update.yml`).
   Remove `hold` afterwards.
7. *A built image's tag moved* (Grafana 13.3.0 → 13.3.1): builds.yml publishes a tag only
   from main, so the overlays can't use it yet. `scripts/bb-held-builds.py` holds that
   build.yaml back and update.yml opens it alone as `bot/builds-update` (`bot/builds-release`
   for the release track, as `builds/<name>/release.yaml`, so main's build.yaml stays the
   deployed release's), which merges itself when green; builds.yml then publishes from main and update.yml runs again to move the
   overlays (the release PR then deletes release.yaml). A bot PR failing on `MANIFEST_UNKNOWN` / `ImagePullBackOff` for
   `<BUILD_REGISTRY>/bigbang-*` means that didn't happen: check builds.yml's last run on main.

