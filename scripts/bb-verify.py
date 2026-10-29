#!/usr/bin/env python3
"""Render Big Bang with the overlays and check every deployed image.

  bb-verify.py --bigbang <checkout> --pkgs <dir> [--fips] [--org <chainguard org>]

Standard: every image is in cgr.dev/<org>, BUILD_REGISTRY, or the registry of a
          mapping.yaml `ref` image, or is an Iron Bank image that mapping.yaml marks
          `stays` (or a `build:` image when BUILD_REGISTRY is empty).
FIPS:     the same, and every converted image is a -fips image unless mapping.yaml
          says the image has no FIPS variant.
"""
import argparse
import json
import os
import subprocess
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from bbcommon import IRONBANK_HOSTS, build_registry, build_target, load_mapping  # noqa: E402
from orgtags import REGISTRY, org_name  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bigbang", required=True)
    ap.add_argument("--pkgs", required=True)
    ap.add_argument("--fips", action="store_true")
    ap.add_argument("--org", help="Chainguard org (default: $CHAINGUARD_ORG, else the CHAINGUARD_ORG file)")
    ap.add_argument("--build-registry", help="registry of the images this repo builds (default: $BUILD_REGISTRY, "
                                             "else the BUILD_REGISTRY file)")
    ap.add_argument("-o", "--output", help="also write the rendered image list here")
    a = ap.parse_args()

    values = [os.path.join(ROOT, "discovery", "render-inputs.yaml"),
              os.path.join(ROOT, "overlays", "values-chainguard.yaml")]
    flux = os.path.join(ROOT, "flux", "kustomization.yaml")
    if a.fips:
        values.append(os.path.join(ROOT, "overlays", "values-chainguard-fips.yaml"))
        flux = os.path.join(ROOT, "flux-fips", "kustomization.yaml")
    cmd = [sys.executable, os.path.join(HERE, "bb-images.py"), "--bigbang", a.bigbang, "--pkgs", a.pkgs,
           "--flux-kustomization", flux]
    for v in values:
        cmd += ["--values", v]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if p.returncode:
        sys.exit(p.stderr)
    found = json.loads(p.stdout)
    if a.output:
        open(a.output, "w", newline="\n").write(p.stdout)

    org = org_name(a.org)
    mapping = load_mapping(org)["images"]
    refs = [v[k] for v in mapping.values() if isinstance(v, dict) and "ref" in v for k in ("ref", "fips") if v.get(k)]
    registry = build_registry(a.build_registry)
    ours = tuple({f"{REGISTRY}/{org}/"} | {r.rsplit("/", 1)[0] + "/" for r in refs}
                 | ({registry + "/"} if registry else set()))
    stays = {k for k, v in mapping.items() if isinstance(v, dict)
             and ("stays" in v or v.get("fallback") == "ironbank" or v.get("group")
                  or ("build" in v and not build_target(v, registry)))}
    no_fips = set()
    for v in mapping.values():
        if isinstance(v, dict) and "cg" in v and v.get("fips", "x") is None:
            no_fips.add(v["cg"])

    problems = [f"{rel}: render failed: {err}" for rel, err in found["errors"].items()]
    counts = {"ours": 0, "ironbank": 0}
    for rel, info in sorted(found["packages"].items()):
        for img in info["rendered"]:
            if img.startswith(ours):
                counts["ours"] += 1
                name = img.split("/")[-1].split(":")[0].split("@")[0]
                if a.fips and not name.endswith("-fips") and name not in no_fips:
                    problems.append(f"{rel}: {img} is not a FIPS image")
                continue
            path = img
            for h in IRONBANK_HOSTS:
                path = path.removeprefix(h + "/")
            repo = path.split("@")[0]
            repo = repo.rsplit(":", 1)[0] if ":" in repo.rsplit("/", 1)[-1] else repo
            if img.startswith(IRONBANK_HOSTS) and repo in stays:
                counts["ironbank"] += 1
                continue
            problems.append(f"{rel}: {img} is not converted and not marked `stays`")

    mode = "FIPS" if a.fips else "standard"
    print(f"{mode}: {counts['ours']} images converted, {counts['ironbank']} documented Iron Bank images")
    for pr in problems:
        print("  FAIL", pr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
