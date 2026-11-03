#!/usr/bin/env python3
"""Bundle what a disconnected site needs to install Big Bang on Chainguard images.

  bb-airgap.py --site my-site.yaml [--fips] --out bundle/

On a connected machine, for the packages the site file turns on:
  bigbang-chainguard/   this checkout (overlays, scripts, ...), with Big Bang's chart in
                        work/charts and Flux rendered to plain manifests (no github.com)
  git/<repo1 path>      a bare clone of Big Bang and of each enabled package at the tag
                        Big Bang pins; push-git.sh pushes them to the site's git server
  images.txt            "<source> <name:tag>" for every image the install runs (Chainguard
                        org images, the images this repo builds, Flux, SeaweedFS)
  README.md             the steps on the disconnected side

The images themselves are not copied: moving them into the site's registry is the site's
step, with its own tooling and under its Chainguard license (images.txt is the list).
Then, inside the site:

  ./push-git.sh https://git.site.example/bigbang
  python3 bigbang-chainguard/scripts/bb-install.py install --site my-site.yaml [--fips] \\
    --git-mirror https://git.site.example/bigbang --registry-mirror registry.site.example/bigbang

Packages that keep Iron Bank images (docs/images.md) also need those images in the site.
"""
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bbcommon import ROOT, bigbang_packages  # noqa: E402

_spec = importlib.util.spec_from_file_location("bb_install", os.path.join(HERE, "bb-install.py"))
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)

COPY_IGNORE = shutil.ignore_patterns(".git", "work", "dist", "tests", "__pycache__", ".pytest_cache", ".tags.json")


def package_repos(defaults, enabled):
    """[(repo1 URL, tag)] of Big Bang's git source for each enabled package."""
    out = []
    for key in enabled:
        node = bigbang_packages(defaults).get(key)
        git = (node or {}).get("git") or {}
        if str(git.get("repo", "")).startswith(install.REPO1) and git.get("tag"):
            out.append((git["repo"], str(git["tag"])))
    return sorted(set(out))


def releases(enabled):
    """Release names (docs/images.md's Package column) for Big Bang keys."""
    found = json.load(open(os.path.join(ROOT, "discovery", "images.json"), encoding="utf-8"))["packages"]
    return {name for name, info in found.items() if info.get("key") in enabled}


def image_list(map_text, names, fips, extra):
    """[(source ref, destination name:tag)]: the replacement images of `names`' rows in
    docs/images.md, plus `extra` refs (Flux, SeaweedFS). Destinations drop the registry
    and org, so one site prefix holds them all."""
    org = next((ln.split("`")[1] for ln in map_text.splitlines() if "are in `cgr.dev/" in ln), "cgr.dev/ORG")
    refs = set(extra)
    for line in map_text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or cells[0] not in names:
            continue
        new = cells[3 if fips else 2]
        if new and new != "Iron Bank":
            refs.add(new if "/" in new.split(":")[0] else f"{org}/{new}")
    return sorted((r, r.rsplit("/", 1)[1]) for r in refs)


def kustomize_images(path):
    out = subprocess.run(["kubectl", "kustomize", path], capture_output=True, text=True, check=True).stdout
    refs = {ln.split("image:", 1)[1].strip().strip("'\"") for ln in out.splitlines() if ln.strip().startswith("image:")}
    return out, sorted(r for r in refs if "/" in r)   # not CRD schemas' bare `image:` keys


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", required=True)
    ap.add_argument("--fips", action="store_true")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if os.path.exists(a.out) and os.listdir(a.out):
        raise SystemExit(f"{a.out} isn't empty")
    co = install.Checkout(ROOT)
    copy = os.path.join(a.out, "bigbang-chainguard")
    shutil.copytree(ROOT, copy, ignore=COPY_IGNORE)

    install.step("Big Bang's chart")
    chart = install.fetch_chart(co.version, cache=os.path.join(copy, "work", "charts"))
    defaults = yaml.safe_load(subprocess.run(["helm", "show", "values", chart], capture_output=True, text=True,
                                             check=True).stdout)
    enabled = install.enabled_packages(install.merged_values(defaults, co.overlays(a.fips) + [a.site]))
    install.say(f"packages: {', '.join(enabled)}")

    install.step("Flux, rendered")
    extra = []
    for name in ("flux", "flux-fips"):
        text, imgs = kustomize_images(os.path.join(ROOT, name))
        if (name == "flux-fips") == a.fips:
            extra += imgs
        with open(os.path.join(copy, name, "install.yaml"), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        with open(os.path.join(copy, name, "kustomization.yaml"), "w", encoding="utf-8", newline="\n") as f:
            f.write("# Rendered by scripts/bb-airgap.py from the connected checkout's kustomization.\n")
            yaml.safe_dump({"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
                            "resources": ["install.yaml"]}, f)
    if co.in_cluster_seaweedfs():
        extra += kustomize_images(os.path.join(ROOT, "seaweedfs-fips" if a.fips else "seaweedfs"))[1]

    install.step("git repositories")
    repos = [(install.BIGBANG_GIT, co.version)] + package_repos(defaults, enabled)
    lines = []
    for url, tag in repos:
        path = url[len(install.REPO1):]
        dest = os.path.join(a.out, "git", path)
        subprocess.run(["git", "clone", "--quiet", "--bare", "--single-branch", "--branch", tag, url, dest],
                       check=True)
        lines.append(f'git -C "git/{path}" push "$1/{path}" "refs/tags/{tag}"')
        install.say(f"  {path} {tag}")
    with open(os.path.join(a.out, "push-git.sh"), "w", encoding="utf-8", newline="\n") as f:
        f.write("#!/bin/sh\n# Push Big Bang and its packages to the site's git server, repo1's paths under $1:\n"
                "#   ./push-git.sh https://git.site.example/bigbang\n"
                "# Create the repositories first if the server doesn't on push.\nset -e\ncd \"$(dirname \"$0\")\"\n"
                + "\n".join(lines) + "\n")
    os.chmod(os.path.join(a.out, "push-git.sh"), 0o755)

    install.step("images")
    images = image_list(open(os.path.join(ROOT, "docs", "images.md"), encoding="utf-8").read(),
                        releases(enabled), a.fips, extra)
    with open(os.path.join(a.out, "images.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("# <source> <name:tag under the site's registry prefix>. Copy them with the site's tooling.\n")
        f.writelines(f"{src} {dst}\n" for src, dst in images)
    with open(os.path.join(a.out, "README.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(f"# Big Bang {co.version} for a disconnected site\n\n```\n{__doc__}```\n")
    left = install.iron_bank_leftovers(set(enabled), co.org)
    with open(os.path.join(a.out, "bundle.json"), "w", encoding="utf-8") as f:
        json.dump({"bigbang": co.version, "release": co.release, "org": co.org, "fips": a.fips,
                   "packages": enabled, "git": [{"repo": u, "tag": t} for u, t in repos],
                   "images": len(images), "iron_bank": {k: sorted(v) for k, v in left.items()}}, f, indent=1)
    install.say(f"{len(images)} images in images.txt, {len(repos)} git repositories"
                + (f"; Iron Bank images still needed for {', '.join(sorted(left))}" if left else ""))
    install.say(f"\nwrote {a.out}: move it into the site, then follow the steps in this script's help")
    return 0


if __name__ == "__main__":
    sys.exit(main())
