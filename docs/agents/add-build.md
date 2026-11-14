# Add an image this repo builds

Part of the maintainer playbooks. The loop and the rules are in [AGENTS.md](../../AGENTS.md).

### Add an image this repo builds
A new `builds/<name>/` with a `Dockerfile` and a `recipe.yaml` (`scripts/bb-build.py`'s
docstring lists the keys), and a hook in `scripts/buildhooks/` only if no existing one
fits (`buildhooks/__init__.py` says what a hook has). Add `<name>` to `builds.yml`'s matrix
and `update.yml`'s two builder loops (`tests/test_recipes.py` checks both), a
`{build: <name>}` entry in `mapping.yaml`, and a row in docs/how-it-works.md's table.

