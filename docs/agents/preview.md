# The Big Bang preview issue

Part of the maintainer playbooks. The loop and the rules are in [AGENTS.md](../../AGENTS.md).

### The Big Bang preview issue (`bigbang-preview`)
`bigbang-preview.yml` runs this repo daily against Big Bang's development branch (`master`)
and, while one is newer than the newest tag, the `release-X.Y.Z` branch Big Bang cuts before
tagging (the next release as it will ship). It keeps one issue per ref (the ref in its title;
a release branch's closes when its release is tagged) with what would break: a newer Kubernetes requirement, images added or
removed, a package that doesn't render, unmapped images, new chart scan findings, and kind
tests whose values the new charts refuse. For a release branch it also deploys a handful of
kind tests on that ref (`compat.yml` called with `bigbang_ref`: discovery and overlays
regenerated in the job, never committed; `compat-plan.py` `PREVIEW`), and their results join
the issue (a marked section; the issue closes only when both are clean); they never reach
`docs/testing.md`'s table. A failed one is triaged like any kind test
(`ci-triage.py --run <id>`). Work it like a bot PR, ahead of time: each fix
that is also right for the current release (a mapping, an override that both accept) can
land now; the rest waits on a branch for the release. The issue closes itself when nothing
would break. Run it locally with `python scripts/bb-preview.py [--ref <branch or tag>]`.

