# Change an image this repo builds

Part of the maintainer playbooks. The loop and the rules are in [AGENTS.md](../../AGENTS.md).

### Change an image this repo builds
Edit `builds/<name>/Dockerfile`, `builds/<name>/recipe.yaml` (what the build is: names,
Iron Bank project, modules to raise, notes, build arguments, a declarative smoke test) or
its hook `scripts/buildhooks/<hook>.py` (the Python it needs); regenerate `build.yaml` with
`python scripts/bb-build.py <name>` (`--all` for every build); the smoke test (the hook's,
or the recipe's `smoke`) must cover the change. `builds.yml` builds on the PR and pushes on
merge.

