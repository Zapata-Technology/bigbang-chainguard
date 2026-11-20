#!/usr/bin/env python3
"""Setup for compat tests of licensed packages: scale the workloads that run only the
vendor's licensed image (Iron Bank, `stays` in mapping.yaml) to zero replicas, so the test
runs the package's org images (its database, cache, exporters) without a license or an
Iron Bank login.

Fortify SSC's StatefulSet and Anchore Enterprise's UI and component catalog Deployments
have their replica count fixed in the chart, so this adds a Flux post-renderer patch. Run
by bb-compat.py (a `setup` command) with COMPAT_OUT and COMPAT_FIPS:

  python3 compat/support/no-replicas.py packages.fortify StatefulSet 'fortify-ssc-webapp'
  python3 compat/support/no-replicas.py packages.anchoreEnterprise Deployment '.*'

(the last argument is a name regex, as kustomize targets take). Writes
$COMPAT_OUT/site-<package>-no-replicas.yaml (name it in `values_files`). Helm replaces
lists, so the file repeats the package's postRenderers from this run's overlays (the digest
pins, FIPS's when COMPAT_FIPS is set) and appends the patch.
"""
import os
import sys

import yaml


def post_renderers(values, keys):
    node = values
    for k in keys:
        node = (node or {}).get(k)
    return list((node or {}).get("postRenderers") or [])


def main():
    if len(sys.argv) != 4:
        sys.exit("usage: no-replicas.py <package values key, e.g. packages.fortify> <kind> <name regex>")
    keys, kind, name = sys.argv[1].split("."), sys.argv[2], sys.argv[3]
    out, fips = os.environ["COMPAT_OUT"], bool(os.environ.get("COMPAT_FIPS"))
    # The last overlay that sets the list wins, as with helm -f.
    renderers = []
    for f in ["values-chainguard.yaml"] + (["values-chainguard-fips.yaml"] if fips else []):
        found = post_renderers(yaml.safe_load(open(os.path.join(out, "overlays", f))), keys)
        renderers = found or renderers
    patch = [{"op": "replace", "path": "/spec/replicas", "value": 0}]
    renderers.append({"kustomize": {"patches": [{"target": {"kind": kind, "name": name},
                                                 "patch": yaml.safe_dump(patch)}]}})
    site = {}
    node = site
    for k in keys:
        node = node.setdefault(k, {})
    node["postRenderers"] = renderers
    path = os.path.join(out, f"site-{keys[-1]}-no-replicas.yaml")
    yaml.safe_dump(site, open(path, "w"))
    print(f"wrote {path}: {kind} {name} scaled to 0, after {len(renderers) - 1} post-renderer(s) from the overlays")


if __name__ == "__main__":
    main()
