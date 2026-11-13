#!/usr/bin/env python3
"""Hold back the images this repo builds whose new tag isn't published yet (nightly update).

  bb-held-builds.py hold <dir>      set them aside in <dir>, put back HEAD's build.yaml
  bb-held-builds.py restore <dir>   put the set-aside build.yaml files back

The builders (scripts/bb-<name>.py) rewrite builds/<name>/build.yaml; bb-convert.py points
the overlays at its tag. builds.yml pushes and signs that tag only from main, so a nightly
pull request that moves a tag (Grafana 13.3.0 -> 13.3.1) can never pass: its signature
check and kind tests can't pull an image that doesn't exist yet.

`hold` finds each build.yaml whose image tag (standard or FIPS) differs from HEAD's and
isn't in BUILD_REGISTRY, copies it to <dir>/builds/<name>/build.yaml and restores HEAD's,
so bb-convert keeps the overlays on the published tag. It prints the held paths, one per
line. update.yml sends them in their own pull request (bot/builds-update): it passes, as
the overlays don't change, and once it merges builds.yml publishes the new tag and
update.yml runs again to move the overlays.

A registry it can't ask counts as not published: holding back is the safe side. Needs
`docker` and, for a private registry, its credentials in the Docker config.
"""
import argparse
import os
import shutil
import subprocess
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bbcommon import ROOT, build_registry  # noqa: E402

VARIANTS = ("standard", "fips")


def refs(spec, registry):
    """The image refs a build.yaml spec publishes, {variant: registry/name:tag}."""
    return {v: f"{registry}/{spec['image'][v]['name']}:{spec['image'][v]['tag']}"
            for v in VARIANTS if v in spec.get("image", {})}


def head_spec(rel, root=ROOT):
    """HEAD's version of a repo file, parsed, or None if HEAD doesn't have it."""
    p = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=root, capture_output=True, text=True)
    return yaml.safe_load(p.stdout) if p.returncode == 0 else None


def published(ref):
    """Whether the registry has ref (any failure to ask counts as no)."""
    p = subprocess.run(["docker", "manifest", "inspect", ref], capture_output=True, text=True)
    return p.returncode == 0


def to_hold(registry, root=ROOT, head=head_spec, exists=published):
    """builds/<name>/build.yaml paths whose changed tag the registry doesn't have yet."""
    held = []
    builds = os.path.join(root, "builds")
    for name in sorted(os.listdir(builds)) if os.path.isdir(builds) else []:
        rel = f"builds/{name}/build.yaml"
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            continue
        new = refs(yaml.safe_load(open(path, encoding="utf-8")), registry)
        old = head(rel, root)
        old = refs(old, registry) if old else {}
        moved = [ref for v, ref in new.items() if ref != old.get(v)]
        if any(not exists(ref) for ref in moved):
            held.append(rel)
    return held


def hold(out, registry, root=ROOT, head=head_spec, exists=published):
    paths = to_hold(registry, root, head, exists)
    for rel in paths:
        dest = os.path.join(out, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(os.path.join(root, rel), dest)
        if subprocess.run(["git", "cat-file", "-e", f"HEAD:{rel}"], cwd=root).returncode == 0:
            subprocess.run(["git", "checkout", "HEAD", "--", rel], cwd=root, check=True)
        else:   # a new build: not on main at all yet
            os.remove(os.path.join(root, rel))
    return paths


def restore(out, root=ROOT):
    paths = []
    for dirpath, _, files in os.walk(os.path.join(out, "builds")):
        for f in files:
            src = os.path.join(dirpath, f)
            rel = os.path.relpath(src, out).replace(os.sep, "/")
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            shutil.copyfile(src, os.path.join(root, rel))
            paths.append(rel)
    return sorted(paths)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("hold", "restore"))
    ap.add_argument("dir", help="where held build.yaml files are kept")
    ap.add_argument("--build-registry", help="default: $BUILD_REGISTRY, else the BUILD_REGISTRY file")
    a = ap.parse_args()
    if a.action == "restore":
        paths = restore(a.dir)
    else:
        registry = build_registry(a.build_registry)
        paths = hold(a.dir, registry) if registry else []
    print(*paths, sep="\n")


if __name__ == "__main__":
    main()
