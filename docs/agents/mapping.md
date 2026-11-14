# Add or fix an image mapping

Part of the maintainer playbooks. The loop and the rules are in [AGENTS.md](../../AGENTS.md).

### Add or fix an image mapping
Follow [docs/adding-a-package.md](../adding-a-package.md). In short: find the image in
`discovery/images.json`, find the Chainguard name (`chainctl images catalog --name <n>`),
add the entry, `bb-convert.py` online (a chainctl session and a fresh tag list; do not run it
offline), `check.py --online`. If the org lacks the repository
(`bb-convert.py --missing` lists them), add it as in [add-repository.md](add-repository.md).

