# Disconnected sites

Part of the maintainer playbooks. The loop and the rules are in [AGENTS.md](../../AGENTS.md).

### Disconnected sites
`scripts/bb-airgap.py` writes the bundle (chart, rendered Flux, bare git clones, images.txt
with each source at the digest the overlays pin, SHA256SUMS);
`bbinstall/images.py` `pinned_refs` reads those digests (post-renderer pins, flux*/, seaweedfs*/),
and `mirror_problems` is how `check`/`install --registry-mirror` look each one up in the site registry;
`bb-install.py --git-mirror/--registry-mirror` installs from the site's copies
(`bbinstall/checkout.py` `mirrored_checkout` rewrites `cgr.dev/<org>` and the build registry,
keeping headers). The
tooling never copies or pushes images: that is the site's step (repo rule 2).

