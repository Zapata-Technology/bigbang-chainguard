#!/usr/bin/env python3
"""Build Big Bang's MinIO Operator image from upstream's source, FROM the org.

Chainguard publishes no MinIO Operator image. This builds minio/operator's ./cmd/operator
at the tag Big Bang deploys, following Iron Bank's archived recipe; scripts/bbminio.py
has the steps, shared with bb-minio-operator-sidecar.py.

  bb-minio-operator.py [--org <org>] [--ref master]       regenerate build.yaml
  bb-minio-operator.py --build [--test] [--push]          also build both images

--test runs each image: --help lists the `controller` command; `controller` started with
no cluster stops at the missing in-cluster config (so it initialised, and for -fips its
FIPS crypto loaded); it runs as 65532; the binary's build info shows the raised modules
and the FIPS or static build; and for -fips that OpenSSL defaults to fips=yes.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bbbuild  # noqa: E402
import bbminio  # noqa: E402

NAME = "minio-operator"
IB_IMAGE = "ironbank/opensource/minio/operator"
IB_PROJECT = "opensource/minio/operator"
IMAGE = "bigbang-minio-operator"


def generate(a):
    return bbminio.generate(a, NAME, "operator", IB_IMAGE, IB_PROJECT, IMAGE)


def smoke_test(image, variant, spec):
    bbminio.smoke_test(image, variant, spec, ["--help"], r"\bcontroller\b", ["controller"],
                       r"in-cluster|KUBERNETES_SERVICE_HOST|kubeconfig")


if __name__ == "__main__":
    bbbuild.run(NAME, __doc__, generate, bbminio.build_args, smoke_test)
