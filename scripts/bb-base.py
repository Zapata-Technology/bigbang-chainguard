#!/usr/bin/env python3
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
  5. write builds/base/build.yaml

Iron Bank's CCE-* scripts are RHEL STIG settings for UBI; they don't apply to
Wolfi and are listed in build.yaml as not ported.

  bb-base.py [--org <org>] [--ref master]       regenerate build.yaml
  bb-base.py --build [--test] [--push]          also build both images

--build    docker build <BUILD_REGISTRY>/bigbang-base:<tag> and -fips:<tag>
--test     run each image: uid, bash, kubectl, yq, jq, curl and the other tools,
           and for -fips that OpenSSL defaults to fips=yes
--push     docker push both images to BUILD_REGISTRY (never cgr.dev)
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bbbuild  # noqa: E402

NAME = "base"
IB_IMAGE = "ironbank/big-bang/base"
IB_PROJECT = "big-bang/base"
IMAGE = "bigbang-base"

# What UBI minimal ships that Big Bang's job scripts rely on (bash, curl-minimal, ...).
UBI_USERLAND = ["bash", "coreutils", "curl", "grep", "sed", "ca-certificates"]
# UBI package name -> Wolfi package name, where they differ. None: busybox in the
# -dev image already provides it (Wolfi has no separate package).
UBI_TO_WOLFI = {"curl-minimal": "curl", "coreutils-single": "coreutils", "which": None}


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


def generate(a):
    bb_tag = bbbuild.bigbang_tag(IB_IMAGE)
    commit, manifest, dockerfile = bbbuild.ironbank_recipe(IB_PROJECT, a.ref)
    pkgs, user, kubectl, yq, cce = recipe(manifest, dockerfile)
    images = bbbuild.org_images(a.org, {"standard": "kubectl", "fips": "kubectl-fips"}, kubectl)
    yq_line = ".".join(yq.split(".")[:2])
    wolfi = {UBI_TO_WOLFI.get(p, p) for p in pkgs} - {None}
    packages = sorted(set(UBI_USERLAND) | wolfi) + [f"yq~{yq_line}"]
    spec = {
        "ironbank": {"image": f"{IB_IMAGE}:{bb_tag}", "source": f"{bbbuild.REPO1}/dsop/{IB_PROJECT}",
                     "commit": commit, "manifest_tags": manifest.get("tags", []),
                     "kubectl": kubectl, "yq": yq, "microdnf": pkgs},
        # Tagged <Iron Bank tag>-kubectl<version>, so a kubectl patch gets a new tag.
        "image": {v: {"name": IMAGE + ("-fips" if v == "fips" else ""), "tag": f"{bb_tag}-kubectl{img['tag']}",
                      "dev": img["dev"]} for v, img in images.items()},
        "packages": packages,
        "user": user,
        "not_ported": [f"{c}.sh: RHEL STIG setting for UBI; not applicable to Wolfi" for c in cce],
    }
    bbbuild.write_spec(NAME, spec, "# Big Bang's base job image rebuilt FROM the Chainguard org's kubectl -dev images,\n"
                                   "# with the contents of Iron Bank's big-bang/base at `ironbank.commit`.\n")
    print(f"kubectl {images['standard']['tag']} / fips {images['fips']['tag']} (Iron Bank {kubectl}), "
          f"yq {yq_line}.x, packages {' '.join(packages)}")
    return spec


def build_args(spec, variant):
    return {"PACKAGES": " ".join(spec["packages"]), "USER_NAME": spec["user"]["name"],
            "USER_ID": str(spec["user"]["id"])}


def smoke_test(image, variant, spec):
    kubectl_line = ".".join(spec["ironbank"]["kubectl"].split(".")[:2])
    yq_line = ".".join(spec["ironbank"]["yq"].split(".")[:2])
    script = "; ".join([
        "echo uid=$(id -u)",
        "kubectl version --client -o json | grep -o '\"gitVersion\": *\"[^\"]*\"'",
        "yq --version", "jq --version", "curl --version | head -1",
        "for t in find which grep sed; do command -v $t >/dev/null || { echo missing $t; exit 1; }; done",
        "echo tools=ok",
        # UBI's sh is bash; Big Bang job scripts run as sh use bash features.
        "sh -c 'a=(x y); echo sh=${a[1]}'",
        "grep -hE '^ *(default_)?properties *= *fips=yes' /etc/ssl/openssl.cnf || true",
    ])
    checks = {
        f"uid {spec['user']['id']}": rf"^uid={spec['user']['id']}$",
        f"kubectl {kubectl_line}": rf'"v{re.escape(kubectl_line)}\.',
        f"yq {yq_line}": rf"version v?{re.escape(yq_line)}\.",
        "tools": r"^tools=ok$",
        "sh is bash": r"^sh=y$",
    }
    if variant == "fips":
        checks["openssl fips=yes"] = r"properties *= *fips=yes"
    bbbuild.run_checks(image, script, checks)


if __name__ == "__main__":
    bbbuild.run(NAME, __doc__, generate, build_args, smoke_test)
