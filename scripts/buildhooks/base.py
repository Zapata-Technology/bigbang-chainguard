"""Rebuild Big Bang's job base image (Iron Bank big-bang/base) FROM the org's kubectl.

Iron Bank's big-bang/base is UBI minimal plus bash, curl, jq, yq, kubectl and a few
utilities, run as uid 1000. Big Bang's upgrade, init and test jobs use it in about
25 packages. Chainguard publishes no equivalent, so this converts Iron Bank's own
recipe:

  1. read the Iron Bank tag Big Bang deploys (discovery/images.json)
  2. read Iron Bank's hardening_manifest.yaml (kubectl and yq versions) and
     Dockerfile (microdnf packages, the user it creates) from repo1.dso.mil,
     pinned to a commit
  3. pin cgr.dev/<org>/kubectl and kubectl-fips -dev images on Iron Bank's
     kubectl MAJOR.MINOR line by digest
  4. map the packages to Wolfi, with yq held to Iron Bank's MAJOR.MINOR
  5. return builds/base/build.yaml

Iron Bank's CCE-* scripts are RHEL STIG settings for UBI; they don't apply to
Wolfi and are listed in build.yaml as not ported. The smoke test (recipe `smoke`) runs
each image: uid, bash, kubectl, yq, jq, curl and the other tools, sh is bash.

Recipe keys: ironbank {image, project}, org_images, ubi_userland (what UBI minimal ships
that the jobs rely on), ubi_to_wolfi (UBI package -> Wolfi package; null: busybox in the
-dev image provides it).
"""
import re

import bbbuild


def recipe(manifest, dockerfile):
    """Iron Bank's packages, user and binary versions."""
    pkgs = []
    for m in re.finditer(r"microdnf\s+install\s+((?:\\\n|[^&;\n\\])+)", dockerfile):
        pkgs += [w for w in m.group(1).replace("\\", " ").split() if not w.startswith("-")]
    user = re.search(r"useradd\b[^\n&]*?-u\s+(\d+)[^\n&]*?\s(\w[\w-]*)\s*(?:&&|\\|$)", dockerfile, re.M)
    if not (pkgs and user):
        raise SystemExit("Iron Bank's Dockerfile changed shape (microdnf install / useradd); update recipe()")
    urls = [r["url"] for r in manifest.get("resources", [])]
    kubectl = next((m.group(1) for u in urls if (m := re.search(r"dl\.k8s\.io/(?:release/)?v(\d+\.\d+\.\d+)/", u))), None)
    yq = next((m.group(1) for u in urls if (m := re.search(r"/yq/releases/download/v(\d+\.\d+\.\d+)/", u))), None)
    if not (kubectl and yq):
        raise SystemExit("Iron Bank's manifest no longer lists kubectl and yq downloads; update recipe()")
    cce = sorted(set(re.findall(r"(CCE-[\d-]+)\.sh", dockerfile)))
    return pkgs, {"name": user.group(2), "id": int(user.group(1))}, kubectl, yq, cce


def wolfi_packages(r, pkgs, yq):
    """Iron Bank's microdnf packages as Wolfi packages, plus UBI's userland, plus yq on its line."""
    wolfi = {r["ubi_to_wolfi"].get(p, p) for p in pkgs} - {None}
    return sorted(set(r["ubi_userland"]) | wolfi) + [f"yq~{bbbuild.FILTERS['line'](yq)}"]


def generate(r, a):
    ib_image, ib_project = r["ironbank"]["image"], r["ironbank"]["project"]
    bb_tag = bbbuild.bigbang_tag(ib_image)
    commit, manifest, dockerfile = bbbuild.ironbank_recipe(ib_project, a.ref)
    pkgs, user, kubectl, yq, cce = recipe(manifest, dockerfile)
    images = bbbuild.org_images(a.org, r["org_images"], kubectl)
    packages = wolfi_packages(r, pkgs, yq)
    spec = {
        "ironbank": {"image": f"{ib_image}:{bb_tag}", "source": f"{bbbuild.REPO1}/dsop/{ib_project}",
                     "commit": commit, "manifest_tags": manifest.get("tags", []),
                     "kubectl": kubectl, "yq": yq, "microdnf": pkgs},
        # Tagged <Iron Bank tag>-kubectl<version>, so a kubectl patch gets a new tag.
        "image": {v: {"name": bbbuild.image_name(r, v), "tag": f"{bb_tag}-kubectl{img['tag']}",
                      "dev": img["dev"]} for v, img in images.items()},
        "packages": packages,
        "user": user,
        "not_ported": [f"{c}.sh: RHEL STIG setting for UBI; not applicable to Wolfi" for c in cce],
    }
    print(f"kubectl {images['standard']['tag']} / fips {images['fips']['tag']} (Iron Bank {kubectl}), "
          f"yq {bbbuild.FILTERS['line'](yq)}.x, packages {' '.join(packages)}")
    return spec
