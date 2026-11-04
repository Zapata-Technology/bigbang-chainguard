"""Build the installer image: bb-install.py with kubectl, helm, Python and the overlays.

A user then needs only a container runtime and a kubeconfig:

  docker run --rm -it --network host -v ~/.kube:/home/nonroot/.kube:ro -v "$PWD:/site" \\
    -e CGR_USERNAME -e CGR_PASSWORD <BUILD_REGISTRY>/bigbang-installer:<tag> \\
    install --site /site/my-site.yaml

  1. take the kubectl and kubectl-fips -dev images builds/base pins (same digests, so
     regenerating base moves this too: the recipe's `after: [base]`)
  2. return builds/installer/build.yaml, tagged with the Big Bang version and kubectl's

The smoke test (recipe `smoke`) runs the image: bb-install.py --help, kubectl, helm, git,
PyYAML, the bundled chart, uid 65532.

Recipe keys: packages, context (the repository root: the image carries the scripts and
overlays).
"""
import os
import re

import yaml

import bbbuild
from bbcommon import ROOT


def generate(r, a):
    base = yaml.safe_load(open(os.path.join(ROOT, "builds", "base", "build.yaml"), encoding="utf-8"))
    bigbang = open(os.path.join(ROOT, "BIGBANG_VERSION"), encoding="utf-8").read().strip()
    images = {}
    for variant, img in base["image"].items():
        kubectl = re.search(r":(\d+\.\d+\.\d+)-dev@", img["dev"]).group(1)
        images[variant] = {"name": bbbuild.image_name(r, variant),
                           "tag": f"{bigbang}-kubectl{kubectl}", "dev": img["dev"]}
    # standalone: not a Big Bang image the mapping names, so bb-build.py builds it regardless.
    return {"image": images, "packages": r["packages"], "context": r["context"], "standalone": True}
