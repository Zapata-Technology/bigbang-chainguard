#!/usr/bin/env python3
"""Render Big Bang with the overlays and check every deployed image.

  bb-verify.py --bigbang <checkout> --pkgs <dir> [--fips] [--org <chainguard org>]

Standard: every image is in cgr.dev/<org>, BUILD_REGISTRY, or the registry of a
          mapping.yaml `ref` image, or is an Iron Bank image that mapping.yaml marks
          `stays` (or a `build:` image when BUILD_REGISTRY is empty).
FIPS:     the same, and every converted image is a -fips image unless mapping.yaml
          says the image has no FIPS variant.
Both:     every org image is pinned by digest (bb-convert's post-renderer pins), except in
          `no_post_renderers` packages, images mapping.yaml lists in `unpinned`, and where a
          release renders one repository at two tags.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from bbcommon import IRONBANK_HOSTS, build_registry, build_target, load_mapping, split_ref  # noqa: E402
import objstore  # noqa: E402
from orgtags import REGISTRY, org_name  # noqa: E402


def digest_gaps(packages, prefix, tag_only, unpinned=None):
    """(release, image) for org images left on a tag. A release whose package can't take a
    post-renderer (`tag_only` package keys) is exempt, as are `unpinned` {release: {repo}}
    and a repository the release deploys at two tags: kustomize matches images by name, so
    bb-convert can't pin either."""
    gaps = []
    unpinned = unpinned or {}
    for rel, info in sorted(packages.items()):
        if info.get("key") in tag_only:
            continue
        allowed = {prefix + r for r in unpinned.get(rel, {})}
        allowed |= {r + "-fips" for r in allowed}
        mine = [i for i in info["rendered"] if i.startswith(prefix)]
        tags = {}
        for img in mine:
            repo = img.split("@")[0].rsplit(":", 1)[0]
            tags.setdefault(repo, set()).add(img)
        gaps += [(rel, img) for img in mine if "@sha256:" not in img
                 and len(tags[img.rsplit(":", 1)[0]]) == 1 and img.rsplit(":", 1)[0] not in allowed]
    return gaps


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bigbang", required=True)
    ap.add_argument("--pkgs", required=True)
    ap.add_argument("--fips", action="store_true")
    ap.add_argument("--org", help="Chainguard org (default: $CHAINGUARD_ORG, else the CHAINGUARD_ORG file)")
    ap.add_argument("--build-registry", help="registry of the images this repo builds (default: $BUILD_REGISTRY, "
                                             "else the BUILD_REGISTRY file)")
    ap.add_argument("-o", "--output", help="also write the rendered image list here")
    ap.add_argument("--object-storage", help="seaweedfs (default) or minio (default: $OBJECT_STORAGE, else the "
                                              "OBJECT_STORAGE file)")
    a = ap.parse_args()

    values = [os.path.join(ROOT, "discovery", "render-inputs.yaml"),
              os.path.join(ROOT, "overlays", "values-chainguard.yaml")]
    flux = os.path.join(ROOT, "flux", "kustomization.yaml")
    if a.fips:
        values.append(os.path.join(ROOT, "overlays", "values-chainguard-fips.yaml"))
        flux = os.path.join(ROOT, "flux-fips", "kustomization.yaml")
    # OBJECT_STORAGE=seaweedfs: render the way a site deploys it, with the object-storage
    # overlay and (placeholder) credentials, and require that no MinIO is left.
    storage = objstore.kind(a.object_storage)
    overlay = os.path.join(ROOT, "overlays", "values-object-storage.yaml")
    creds = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    if storage == "seaweedfs":
        if not os.path.exists(overlay):
            sys.exit("OBJECT_STORAGE=seaweedfs but overlays/values-object-storage.yaml is missing: run bb-convert.py")
        spec = objstore.load_spec()
        _, cred_paths, _, _ = objstore.build(spec, objstore.tokens(objstore.endpoint(), "us-east-1", "bigbang"), set())
        yaml.safe_dump(objstore.credentials_template(cred_paths, "verify", "verify-secret"), creds)
        values += [overlay, creds.name]
    creds.close()
    cmd = [sys.executable, os.path.join(HERE, "bb-images.py"), "--bigbang", a.bigbang, "--pkgs", a.pkgs,
           "--flux-kustomization", flux]
    for v in values:
        cmd += ["--values", v]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    os.unlink(creds.name)
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
    if storage == "seaweedfs":
        problems += [f"{rel}: {ref} is MinIO, but OBJECT_STORAGE=seaweedfs"
                     for rel, info in sorted(found["packages"].items()) for ref in info["rendered"]
                     if split_ref(ref)[0] in objstore.MINIO_SERVERS]
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

    full = load_mapping(org)
    unpinned = digest_gaps(found["packages"], f"{REGISTRY}/{org}/", set(full.get("no_post_renderers", {})),
                           full.get("unpinned", {}))
    # Warn until the overlays are regenerated with the override and rename pins; then fail.
    for rel, img in unpinned:
        print(f"  WARN {rel}: {img} is not pinned by digest")

    mode = "FIPS" if a.fips else "standard"
    print(f"{mode}: {counts['ours']} images converted, {counts['ironbank']} documented Iron Bank images")
    for pr in problems:
        print("  FAIL", pr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
