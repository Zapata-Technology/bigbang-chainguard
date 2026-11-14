# Chainguard published an Iron Bank leftover

Part of the maintainer playbooks. The loop and the rules are in [AGENTS.md](../../AGENTS.md).

### Chainguard published an Iron Bank leftover
Add the repository ([add-repository.md](add-repository.md), standard and `-fips`), wait for tags,
replace the `{stays, until}` entry with a `cg:` entry, `bb-convert.py`, run the package's
compat test if it has one.

