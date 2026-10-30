#!/usr/bin/env python3
"""Build the installer image: bb-install.py with kubectl, helm, Python and the overlays.

A user then needs only a container runtime and a kubeconfig:

  docker run --rm -it --network host -v ~/.kube:/home/nonroot/.kube:ro -v "$PWD:/site" \
    -e CGR_USERNAME -e CGR_PASSWORD <BUILD_REGISTRY>/bigbang-installer:<tag> \
    install --site /site/my-site.yaml

  1. take the kubectl and kubectl-fips -dev images builds/base pins (same digests, so the
     nightly update that moves them moves this too)
  2. write builds/installer/build.yaml, tagged with the Big Bang version and kubectl's

  bb-installer.py                       regenerate build.yaml
  bb-installer.py --build [--test] [--push]

--test runs the image: bb-install.py --help, kubectl, helm, git, PyYAML, the bundled
chart, uid 65532, and for -fips that OpenSSL defaults to fips=yes.
"""
import os
import re
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bbbuild  # noqa: E402
from bbcommon import ROOT  # noqa: E402

NAME = "installer"
IMAGE = "bigbang-installer"
PACKAGES = ["bash", "curl", "git", "helm", "python-3.13", "py3.13-pyyaml"]


def generate(a):
    base = yaml.safe_load(open(os.path.join(ROOT, "builds", "base", "build.yaml"), encoding="utf-8"))
    bigbang = open(os.path.join(ROOT, "BIGBANG_VERSION"), encoding="utf-8").read().strip()
    images = {}
    for variant, img in base["image"].items():
        kubectl = re.search(r":(\d+\.\d+\.\d+)-dev@", img["dev"]).group(1)
        images[variant] = {"name": IMAGE + ("-fips" if variant == "fips" else ""),
                           "tag": f"{bigbang}-kubectl{kubectl}", "dev": img["dev"]}
    spec = {"image": images, "packages": PACKAGES, "context": "..", "standalone": True}
    bbbuild.write_spec(NAME, spec, "# The installer image: bb-install.py and its tools, FROM the kubectl -dev images\n"
                                   "# builds/base pins.\n")
    return spec


def build_args(spec, variant):
    return {"PACKAGES": " ".join(spec["packages"])}


def smoke_test(image, variant, spec):
    bigbang = spec["image"][variant]["tag"].split("-kubectl")[0]
    script = "; ".join([
        "echo uid=$(id -u)",
        "python3 /bigbang-chainguard/scripts/bb-install.py --help | head -3",
        "python3 -c 'import yaml; print(\"pyyaml\", yaml.__version__)'",
        "kubectl version --client -o json | grep -o '\"gitVersion\": *\"[^\"]*\"'",
        "helm version --short", "git --version",
        f"test -f /bigbang-chainguard/work/charts/bigbang-{bigbang}/chart/Chart.yaml && echo chart=bundled",
        "grep -hE '^ *(default_)?properties *= *fips=yes' /etc/ssl/openssl.cnf || true",
    ])
    checks = {"uid 65532": r"^uid=65532$", "bb-install.py": r"usage: bb-install\.py", "pyyaml": r"^pyyaml \d",
              "kubectl": r'"gitVersion": *"v1\.', "helm": r"^v3\.", "git": r"^git version", "chart": r"^chart=bundled$"}
    if variant == "fips":
        checks["openssl fips=yes"] = r"properties *= *fips=yes"
    bbbuild.run_checks(image, script, checks)


if __name__ == "__main__":
    bbbuild.run(NAME, __doc__, generate, build_args, smoke_test)
