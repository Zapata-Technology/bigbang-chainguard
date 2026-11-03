#!/usr/bin/env python3
"""Setup for compat tests whose package needs longer than Kubernetes' default 10 minutes
to roll out on a CI runner's kind cluster.

GitLab's Deployments wait in an init container for the database migrations, which take
about 10 minutes on a 2-CPU runner. A Deployment that makes no progress for
progressDeadlineSeconds (600 by default) turns Failed, and Helm then stops the install
("failed early due to stalled resources") long before Flux's timeout. GitLab's chart has
no setting for it, so this adds a Flux post-renderer patch. Run by bb-compat.py (a
`setup` command) with COMPAT_OUT and COMPAT_FIPS:

  python3 compat/support/progress-deadline.py packages.gitlab 2400

writes $COMPAT_OUT/site-<package>-deadline.yaml (name it in `values_files`). Helm
replaces lists, so the file repeats the package's postRenderers from this run's
overlays (the digest pins, FIPS's when COMPAT_FIPS is set) and appends the patch.
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
    if len(sys.argv) != 3:
        sys.exit("usage: progress-deadline.py <package values key, e.g. packages.gitlab> <seconds>")
    keys, seconds = sys.argv[1].split("."), int(sys.argv[2])
    out, fips = os.environ["COMPAT_OUT"], bool(os.environ.get("COMPAT_FIPS"))
    # The last overlay that sets the list wins, as with helm -f.
    renderers = []
    for name in ["values-chainguard.yaml"] + (["values-chainguard-fips.yaml"] if fips else []):
        found = post_renderers(yaml.safe_load(open(os.path.join(out, "overlays", name))), keys)
        renderers = found or renderers
    patch = [{"op": "add", "path": "/spec/progressDeadlineSeconds", "value": seconds}]
    renderers.append({"kustomize": {"patches": [{"target": {"kind": "Deployment"}, "patch": yaml.safe_dump(patch)}]}})
    site = {}
    node = site
    for k in keys:
        node = node.setdefault(k, {})
    node["postRenderers"] = renderers
    path = os.path.join(out, f"site-{keys[-1]}-deadline.yaml")
    yaml.safe_dump(site, open(path, "w"))
    print(f"wrote {path}: progressDeadlineSeconds {seconds} on every Deployment, after {len(renderers) - 1} "
          "post-renderer(s) from the overlays")


if __name__ == "__main__":
    main()
