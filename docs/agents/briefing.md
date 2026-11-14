# The briefing PDF

Part of the maintainer playbooks. The loop and the rules are in [AGENTS.md](../../AGENTS.md).

`docs/big-bang-on-chainguard-briefing.pdf` is a 19-slide deck. Nothing in this repo
generates it. Do not add reportlab or pypdf to `requirements.txt` or
`requirements-dev.txt`.

When you replace the file:

- Keep 19 slides. The README links it as 19 slides.
- The CVE numbers are the grype snapshot of 2026-11-02, 67 images. Iron Bank's
  bases alone are 840 High and 6,688 Medium. The Chainguard images, application
  included, are 31 High and 29 Medium. Label that scan with its date. Do not
  replace those numbers with the image map.
- Live Big Bang 4.1.0 counts stay in [version-differences.md](../version-differences.md)
  and [images.md](../images.md). Point at those files. Do not copy a count the
  nightly job regenerates into the deck as a new CVE total. `images.md` counts
  image names ("Stays on Iron Bank (18)"). `version-differences.md` counts
  image-and-tag rows (19 of those stay; Cypress is two tags).
- Chainguard's published SLSA Build L3 claim is about Chainguard's own builds.
  This repository writes a SLSA v1 predicate in the build job. It is not a
  SLSA Build L3 attestation.
- GitLab's liveness `failureThreshold` of 15 and the Deployment progress
  deadline of 2400 seconds are in `mapping.yaml`. The next online `update.yml`
  writes them into the overlays. Do not run `bb-convert.py` offline to put them
  there, and do not hand-edit the overlays.
