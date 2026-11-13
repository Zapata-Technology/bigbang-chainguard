#!/usr/bin/env python3
"""Hold back the images this repo builds whose new tag isn't published yet (nightly update).

  bb-held-builds.py hold <dir> [--release]       set them aside in <dir>, put back HEAD's build.yaml
  bb-held-builds.py restore <dir>                put the set-aside files back
  bb-held-builds.py drop-release [--keep <dir>]  remove builds/*/release.yaml (but those held in <dir>)

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

The release track (a newer Big Bang release) holds with --release: a held build goes to
builds/<name>/release.yaml instead (same schema), in bot/builds-release. builds.yml
publishes release.yaml from main next to build.yaml, which stays pinned for the deployed
release (the routine track never touches release.yaml), so main's build.yaml doesn't flip
between the two releases. Once published, the release PR carries the new build.yaml, and
`drop-release --keep <dir>` removes each release.yaml not held again. The routine track
drops them all only once no newer release is out (it merged, or was withdrawn).

A standalone build (the installer) is never held: the overlays don't use it, and it
bundles BIGBANG_VERSION's chart, so it ships with the Big Bang PR that moves it.

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
RELEASE = "release.yaml"   # the release track's held build, next to build.yaml


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
        spec = yaml.safe_load(open(path, encoding="utf-8"))
        if spec.get("standalone"):
            # Not in the overlays (the installer): nothing waits on it being published, and
            # it bundles BIGBANG_VERSION's chart, so it must ship with that version's PR.
            continue
        new = refs(spec, registry)
        old = head(rel, root)
        old = refs(old, registry) if old else {}
        moved = [ref for v, ref in new.items() if ref != old.get(v)]
        if any(not exists(ref) for ref in moved):
            held.append(rel)
    return held


def hold(out, registry, root=ROOT, head=head_spec, exists=published, release=False):
    """Set each held build.yaml aside in <out> (as release.yaml, with release) and put back
    HEAD's. Returns the paths they take in the builds pull request."""
    paths = []
    for rel in to_hold(registry, root, head, exists):
        dest_rel = f"{os.path.dirname(rel)}/{RELEASE}" if release else rel
        dest = os.path.join(out, dest_rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(os.path.join(root, rel), dest)
        if subprocess.run(["git", "cat-file", "-e", f"HEAD:{rel}"], cwd=root).returncode == 0:
            subprocess.run(["git", "checkout", "HEAD", "--", rel], cwd=root, check=True)
        else:   # a new build: not on main at all yet
            os.remove(os.path.join(root, rel))
        paths.append(dest_rel)
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


def drop_release(root=ROOT, keep=None):
    """Remove each builds/<name>/release.yaml, except one held again (in <keep>). Returns
    the removed paths."""
    dropped = []
    builds = os.path.join(root, "builds")
    for name in sorted(os.listdir(builds)) if os.path.isdir(builds) else []:
        rel = f"builds/{name}/{RELEASE}"
        if os.path.exists(os.path.join(root, rel)) and not (keep and os.path.exists(os.path.join(keep, rel))):
            os.remove(os.path.join(root, rel))
            dropped.append(rel)
    return dropped


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("hold", "restore", "drop-release"))
    ap.add_argument("dir", nargs="?", help="where held build files are kept (hold, restore)")
    ap.add_argument("--release", action="store_true",
                    help="hold for the release track: a held build goes to builds/<name>/release.yaml")
    ap.add_argument("--keep", help="drop-release: keep each release.yaml held in this directory")
    ap.add_argument("--build-registry", help="default: $BUILD_REGISTRY, else the BUILD_REGISTRY file")
    a = ap.parse_args()
    if a.action != "drop-release" and not a.dir:
        ap.error(f"{a.action} needs <dir>")
    if a.action == "restore":
        paths = restore(a.dir)
    elif a.action == "drop-release":
        paths = drop_release(keep=a.keep)
    else:
        registry = build_registry(a.build_registry)
        paths = hold(a.dir, registry, release=a.release) if registry else []
    print(*paths, sep="\n")


if __name__ == "__main__":
    main()
