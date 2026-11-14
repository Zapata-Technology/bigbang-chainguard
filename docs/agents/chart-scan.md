# A chart scan finding

Part of the maintainer playbooks. The loop and the rules are in [AGENTS.md](../../AGENTS.md).

### A chart scan finding (`bb-scan.py` NEW)
- *sh script needs bash* on a Chainguard `-dev` image (its `sh` is busybox; UBI's is bash):
  a wait job → `bash_wait_jobs`; a `/bin/sh -c` container → `bash_commands`; an image built
  here → it already links `sh` to bash.
- *Empty default written with toYaml* into an app's config: check whether the app rejects
  it (the Tempo case). Fix with `overrides`.
- Then add the finding's key to `scan_reviewed` with one line saying how it is handled.

