# Disconnected sites

Part of the maintainer playbooks. The loop and the rules are in [AGENTS.md](../../AGENTS.md).

### Disconnected sites
`scripts/bb-airgap.py` writes the bundle (chart, rendered Flux, bare git clones, images.txt);
`bb-install.py --git-mirror/--registry-mirror` installs from the site's copies
(`bbinstall/checkout.py` `mirrored_checkout` rewrites `cgr.dev/<org>` and the build registry,
keeping headers). The
tooling never copies or pushes images: that is the site's step (repo rule 2).

