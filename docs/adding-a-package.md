# Adding or fixing an image mapping

You'll need this when:

- a new Big Bang release adds an image (CI on the nightly pull request names it), or
- you find an image that should map differently.

## 1. Find the image

`bb-convert.py` names every Iron Bank image that `mapping.yaml` doesn't cover:

```
Images not in mapping.yaml (add them):
  ironbank/opensource/example/example-operator
```

`discovery/images.json` shows which package deploys it and at which value path.

## 2. Find the Chainguard image

```bash
chainctl images catalog --name example-operator     # does Chainguard publish it?
chainctl images catalog --name example-operator-fips
```

Names often differ from Iron Bank's. Exporters are often `prometheus-<name>`, for example.
Check what the image runs (entrypoint, user, paths) against Iron Bank's: a drop-in
replacement must start the same way with the chart's values.

## 3. Add the entry to `mapping.yaml`

| Situation | Entry |
|---|---|
| Same name pattern, FIPS twin is `<name>-fips` | `ironbank/x/example: example` |
| Different FIPS name, or no FIPS image | `{cg: example, fips: example-fips-x}` / `{cg: example, fips: null}` |
| Chainguard versions it differently | `{cg: example, version: "2.1"}` (match against 2.1.x) or `version: "*"` (newest) |
| No Chainguard equivalent | `{stays: "why"}`, plus `until: <repo>` if one is expected |
| Big Bang's own build (base image plus files) | a builder; see `scripts/bb-base.py` |
| Your org may not have it yet | add `fallback: ironbank`: it stays on Iron Bank until the org has a usable tag |
| Must match Iron Bank's MAJOR.MINOR exactly | add `strict: true` |
| Several images that must move together (GitLab) | the same `group: <name>` on each: all convert or all stay |

Keep entries org-neutral. An exception that only applies to your org goes in
`orgs/<org>.yaml`.

## 4. Regenerate, verify and test

```bash
python scripts/bb-convert.py
python scripts/bb-verify.py --bigbang work/bigbang --pkgs work/pkgs
python scripts/bb-verify.py --bigbang work/bigbang --pkgs work/pkgs --fips
python -m pytest -q tests
```

If verification fails because the chart builds the image name in a way the generic rules
miss, use one of the escape hatches in `mapping.yaml` (`hubs`, `overrides`,
`template_defaults`, `post_renderers`). Each one has an example there.

## 5. Open a pull request

Say which package and image it is, how you confirmed the Chainguard image is a drop-in
replacement, and whether you deployed it.
