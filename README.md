# Big Bang on Chainguard images

Run [Big Bang](https://repo1.dso.mil/big-bang/bigbang) with images from a Chainguard
org (`cgr.dev/<org>`, set in [`CHAINGUARD_ORG`](#using-another-chainguard-org); this repo uses
`zapatatechnology.com`) instead of Iron Bank, with FIPS as an option. Big Bang itself is not forked: this repo produces values overlays for the
unmodified upstream chart, plus a Flux kustomization.

| File | Use |
|---|---|
| `overlays/values-chainguard.yaml` | Standard Chainguard images for every package (FIPS off) |
| `overlays/values-chainguard-fips.yaml` | `-fips` images; layer it **after** the standard overlay |
| `flux/`, `flux-fips/` | Big Bang's `base/flux` with Chainguard images |
| `docs/images.md` | Every image each package deploys and what replaces it, including what stays on Iron Bank and why |

All four are generated. Don't edit them by hand; change `mapping.yaml` and regenerate.

## Deploy

```bash
BB=$(cat BIGBANG_VERSION)

# Flux (standard or FIPS)
kubectl apply -k flux/            # or: kubectl apply -k flux-fips/

# Big Bang, FIPS off
helm upgrade -i bigbang oci://registry1.dso.mil/bigbang/bigbang --version "$BB" -n bigbang --create-namespace \
  -f overlays/values-chainguard.yaml -f my-site.yaml

# Big Bang, FIPS on
helm upgrade -i bigbang oci://registry1.dso.mil/bigbang/bigbang --version "$BB" -n bigbang --create-namespace \
  -f overlays/values-chainguard.yaml -f overlays/values-chainguard-fips.yaml -f my-site.yaml
```

The site file supplies pull credentials. Big Bang turns them into the `private-registry`
secret in every package namespace. Never commit them:

```yaml
registryCredentials:
  - registry: cgr.dev
    username: <Chainguard pull token id>
    password: <Chainguard pull token secret>
  - registry: ghcr.io                 # only if Grafana is enabled (grafana-plugins image)
    username: <GitHub user>
    password: <read:packages token>
  - registry: registry1.dso.mil       # only if a package in docs/images.md "Stays on Iron Bank" is enabled
    username: <Iron Bank user>
    password: <Iron Bank CLI secret>
```

Create a pull token with `chainctl auth pull-token --repository=cgr.dev --parent=<org>`.
Flux needs the same `private-registry` secret in `flux-system` before `kubectl apply -k flux/`.

The overlay also adds `cgr.dev/<org>`, and the registry of every `ref` image in
`mapping.yaml` (`ghcr.io/zapata-technology`), to Big Bang's Kyverno `restrict-image-registries` policy. Without that, Kyverno blocks every pod.

### FIPS notes

- The FIPS overlay swaps each image for its `-fips` twin. Images with no FIPS variant are
  listed in `docs/images.md`.
- Keycloak FIPS (BC-FIPS) needs an admin password of at least 14 characters and BCFKS
  keystores. Set those in the site's `addons.keycloak.values` before switching.
- Switching an existing deployment is an in-place `helm upgrade` with the FIPS overlay added.

## Using another Chainguard org

The org is a setting, not part of the code. In order of precedence:

1. `--org <org>` on `bb-convert.py` and `bb-verify.py`
2. the `CHAINGUARD_ORG` environment variable (in CI: the `CHAINGUARD_ORG` repository variable)
3. the `CHAINGUARD_ORG` file at the repo root

The overlays carry the org's own tags, so a different org means regenerating them, not
editing them:

```bash
echo example.com > CHAINGUARD_ORG
chainctl auth login --audience cgr.dev      # a login that can pull from that org
python scripts/bb-convert.py --missing      # repos the org must add (and their -fips twins)
python scripts/bb-convert.py
python scripts/bb-verify.py --bigbang work/bigbang --pkgs work/pkgs
python scripts/bb-verify.py --bigbang work/bigbang --pkgs work/pkgs --fips
```

Then:

- In GitHub, set `CGR_ACTIONS_ID` to an assumable identity in that org with the
  `registry.pull` role. Leave `CHAINGUARD_ORG` unset unless you want CI to override the file.
- The `grafana-plugins` image is our own build on `ghcr.io/zapata-technology`. Point its
  `ref` in `mapping.yaml` at your build, or mark it `stays`.
- `--tags-cache` files record their org. A cache written for another org is ignored.

## How the conversion works

```
scripts/bb-fetch.py     clone Big Bang at BIGBANG_VERSION + every package at its pinned tag
scripts/bb-images.py    render Big Bang with every package on, render each package with the
                        values Big Bang gives it, and record every deployed image and the
                        value path that sets it            -> discovery/images.json
scripts/bb-convert.py   map each Iron Bank image (mapping.yaml), resolve the org tag, write
                        the overlays at those same value paths -> overlays/, flux*/, docs/images.md
scripts/bb-verify.py    render again with the overlays: every image must be an org image
                        (a -fips one with --fips) or a documented Iron Bank image
```

Value paths are found automatically from each chart's computed values. That covers
repository/tag pairs, registry+repository splits, single image strings, tags in sibling
keys (`release`, `crdRelease`, `image_version`, `version`), and images inside lists. It
also blanks Big Bang's chart-wide registry1 registry settings (`global.imageRegistry`,
Kyverno's `defaultRegistry`), which would otherwise win. For the few packages these
rules can't express, `mapping.yaml` has four escape hatches:

- `hubs`: Istio builds names from `hub`; the overlay sets full image references instead.
- `overrides`: hand-written values with live tag tokens (NeuVector's shared registry/tag).
- `template_defaults`: images a template defaults with no value behind it (Big Bang's wait job).
- `post_renderers`: images hardcoded in a template. A Flux post-renderer rewrites them.

Tags follow the helm-charts pin policy: the newest patch on the Iron Bank tag's
MAJOR.MINOR line, and never `latest`.

## Upgrades

- **Nightly** (`update.yml`): regenerates against the newest org patches and opens a PR.
- **New Big Bang release**: the same job bumps `BIGBANG_VERSION` and re-runs discovery.
  If the release adds an image that `mapping.yaml` doesn't cover, CI fails on that PR and
  names it. Add the mapping (or a `stays` entry with the reason) and push to the bot branch.
- **CI** (`ci.yml`): every PR re-runs discovery, conversion and verification for both modes.

Run it locally:

```bash
pip install pyyaml
chainctl auth login --audience https://console-api.enforce.dev,cgr.dev
python scripts/bb-fetch.py
python scripts/bb-images.py --bigbang work/bigbang --pkgs work/pkgs --values discovery/render-inputs.yaml -o discovery/images.json
python scripts/bb-convert.py --missing     # org repos the conversion needs but the org lacks
python scripts/bb-convert.py
python scripts/bb-verify.py --bigbang work/bigbang --pkgs work/pkgs
python scripts/bb-verify.py --bigbang work/bigbang --pkgs work/pkgs --fips
```

`discovery/render-inputs.yaml` holds render-only placeholders (fake database and Redis
endpoints) so that GitLab and Anchore render during discovery. Never deploy with it.

## License

[Apache License 2.0](LICENSE). Copyright 2026 Zapata Technology; see [NOTICE](NOTICE).
Improvements are welcome back as pull requests: see [CONTRIBUTING.md](CONTRIBUTING.md).
