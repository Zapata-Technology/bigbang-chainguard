#!/usr/bin/env python3
"""Rebuild Big Bang's test-runner image (Iron Bank big-bang/devops-tester) FROM the org.

Iron Bank's devops-tester is an Alpine pipeline runner plus kubectl, velero, the
kyverno CLI, chainsaw and mc, run as devops-user (uid 1000). Big Bang's bbtests
script jobs use it. This converts Iron Bank's own recipe:

  1. read the Iron Bank tag Big Bang deploys (discovery/images.json)
  2. read Iron Bank's Dockerfile (the image and version of each tool it copies in,
     the user it creates) and hardening_manifest.yaml (pinned digests) from
     repo1.dso.mil, pinned to a commit
  3. pin cgr.dev/<org>/kubectl and kubectl-fips -dev images on Iron Bank's kubectl
     MAJOR.MINOR line by digest
  4. velero and the kyverno CLI are copied from the org's velero(-fips) and
     kyverno-cli(-fips) images on Iron Bank's MAJOR.MINOR lines (so the FIPS image
     gets FIPS builds); mc comes from Wolfi. chainsaw has no Chainguard package, so
     it is copied from upstream's image at the digest Iron Bank pins (as Iron Bank does).
  5. write builds/devops-tester/build.yaml

  bb-devops-tester.py [--org <org>] [--ref master]       regenerate build.yaml
  bb-devops-tester.py --build [--test] [--push]          also build both images
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bbbuild  # noqa: E402

NAME = "devops-tester"
IB_IMAGE = "ironbank/big-bang/devops-tester"
IB_PROJECT = "big-bang/devops-tester"
IMAGE = "bigbang-devops-tester"


def line(version):
    return ".".join(version.split(".")[:2])


def recipe(manifest, dockerfile):
    """Iron Bank's tool versions (from its FROM ... AS <stage> lines), chainsaw digest, user."""
    stages = {m.group(3): (m.group(1), m.group(2))
              for m in re.finditer(r"^FROM\s+(\S+?):v?(\d+\.\d+\.\d+)\S*\s+AS\s+(\w+)", dockerfile, re.M | re.I)}
    missing = {"kubectl", "velero", "kyverno", "chainsaw"} - set(stages)
    if missing:
        raise SystemExit(f"Iron Bank's Dockerfile no longer has stages {sorted(missing)}; update recipe()")
    chainsaw = next((r["url"].removeprefix("docker://") for r in manifest.get("resources", [])
                     if "chainsaw@sha256:" in r.get("url", "")), None)
    if not chainsaw:
        raise SystemExit("Iron Bank's manifest no longer pins chainsaw by digest; update recipe()")
    group = re.search(r"addgroup\s+(?:-g\s+\d+\s+)?([\w-]+)", dockerfile)
    user = re.search(r"adduser\s+([\w-]+)\s+.*?-u\s+(\d+)", dockerfile)
    if not (group and user):
        raise SystemExit("Iron Bank's Dockerfile changed shape (addgroup / adduser); update recipe()")
    return ({k: v[1] for k, v in stages.items()}, chainsaw,
            {"name": user.group(1), "group": group.group(1), "id": int(user.group(2))})


def generate(a):
    bb_tag = bbbuild.bigbang_tag(IB_IMAGE)
    commit, manifest, dockerfile = bbbuild.ironbank_recipe(IB_PROJECT, a.ref)
    versions, chainsaw, user = recipe(manifest, dockerfile)
    images = bbbuild.org_images(a.org, {"standard": "kubectl", "fips": "kubectl-fips"}, versions["kubectl"])
    velero = bbbuild.org_images(a.org, {"standard": "velero", "fips": "velero-fips"}, versions["velero"], dev=False)
    kyverno = bbbuild.org_images(a.org, {"standard": "kyverno-cli", "fips": "kyverno-cli-fips"},
                                 versions["kyverno"], dev=False)
    packages = ["bash", "curl", "mc"]
    spec = {
        "ironbank": {"image": f"{IB_IMAGE}:{bb_tag}", "source": f"{bbbuild.REPO1}/dsop/{IB_PROJECT}",
                     "commit": commit, "versions": versions},
        "image": {v: {"name": IMAGE + ("-fips" if v == "fips" else ""), "tag": f"{bb_tag}-kubectl{img['tag']}",
                      "dev": img["dev"], "velero": velero[v]["base"], "kyverno": kyverno[v]["base"]}
                  for v, img in images.items()},
        "chainsaw": chainsaw,
        "packages": packages,
        "user": user,
        "notes": ["chainsaw is upstream's build (ghcr.io/kyverno/chainsaw at Iron Bank's digest): "
                  "Chainguard has no chainsaw package, and the FIPS image's chainsaw is not FIPS-built"],
    }
    bbbuild.write_spec(NAME, spec, "# Big Bang's devops-tester rebuilt FROM the Chainguard org's kubectl -dev images,\n"
                                   "# with the tools of Iron Bank's big-bang/devops-tester at `ironbank.commit`.\n")
    print(f"kubectl {images['standard']['tag']} / fips {images['fips']['tag']}, packages {' '.join(packages)}, "
          f"chainsaw {versions['chainsaw']}")
    return spec


def build_args(spec, variant):
    img = spec["image"][variant]
    return {"CHAINSAW_IMAGE": spec["chainsaw"], "VELERO_IMAGE": img["velero"], "KYVERNO_IMAGE": img["kyverno"],
            "PACKAGES": " ".join(spec["packages"]),
            "USER_NAME": spec["user"]["name"], "GROUP_NAME": spec["user"]["group"],
            "USER_ID": str(spec["user"]["id"])}


def smoke_test(image, variant, spec):
    v = spec["ironbank"]["versions"]
    script = "; ".join([
        "echo uid=$(id -u)",
        "kubectl version --client -o json | grep -o '\"gitVersion\": *\"[^\"]*\"'",
        "echo velero=$(velero version --client-only | grep -o 'v[0-9.]*' | head -1)",
        "echo kyverno=$(kyverno version | grep -o 'v\\?[0-9]\\+\\.[0-9]\\+\\.[0-9]\\+' | head -1)",
        "echo chainsaw=$(chainsaw version 2>&1 | grep -oE '[0-9]+\\.[0-9]+\\.[0-9]+' | head -1)",
        "mc --version | head -1", "curl --version | head -1",
        "grep -hE '^ *(default_)?properties *= *fips=yes' /etc/ssl/openssl.cnf || true",
    ])
    checks = {
        f"uid {spec['user']['id']}": rf"^uid={spec['user']['id']}$",
        f"kubectl {line(v['kubectl'])}": rf'"v{re.escape(line(v["kubectl"]))}\.',
        f"velero {line(v['velero'])}": rf"^velero=v{re.escape(line(v['velero']))}\.",
        f"kyverno {line(v['kyverno'])}": rf"^kyverno=v?{re.escape(line(v['kyverno']))}\.",
        f"chainsaw {line(v['chainsaw'])}": rf"^chainsaw={re.escape(line(v['chainsaw']))}\.",
        "mc": r"^mc version",
    }
    if variant == "fips":
        checks["openssl fips=yes"] = r"properties *= *fips=yes"
    bbbuild.run_checks(image, script, checks)


if __name__ == "__main__":
    bbbuild.run(NAME, __doc__, generate, build_args, smoke_test)
