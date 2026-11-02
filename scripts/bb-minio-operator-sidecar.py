#!/usr/bin/env python3
"""Build Big Bang's MinIO Operator sidecar image from upstream's source, FROM the org.

Chainguard publishes no MinIO Operator sidecar image. The operator runs it in every
MinIO Tenant pod (the `validate-arguments` init container and the `sidecar` container).
This builds minio/operator's sidecar module (./cmd/sidecar) at the tag Big Bang deploys,
following Iron Bank's archived recipe; scripts/bbminio.py has the steps, shared with
bb-minio-operator.py.

  bb-minio-operator-sidecar.py [--org <org>] [--ref master]       regenerate build.yaml
  bb-minio-operator-sidecar.py --build [--test] [--push]          also build both images

--test runs each image: --help lists the `validate` command; `validate` with no cluster
stops at the missing in-cluster config (so it initialised, and for -fips its FIPS crypto
loaded); it runs as 65532; the binary's build info shows the raised modules and the FIPS
or static build; and for -fips that OpenSSL defaults to fips=yes.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bbbuild  # noqa: E402
import bbminio  # noqa: E402

NAME = "minio-operator-sidecar"
IB_IMAGE = "ironbank/opensource/minio/operator-sidecar"
IB_PROJECT = "opensource/minio/operator-sidecar"
IMAGE = "bigbang-minio-operator-sidecar"


def generate(a):
    return bbminio.generate(a, NAME, "sidecar", IB_IMAGE, IB_PROJECT, IMAGE)


def smoke_test(image, variant, spec):
    bbminio.smoke_test(image, variant, spec, ["--help"], r"\bvalidate\b",
                       ["validate", "--tenant", "smoke"], r"in-cluster|KUBERNETES_SERVICE_HOST|kubeconfig")


if __name__ == "__main__":
    bbbuild.run(NAME, __doc__, generate, bbminio.build_args, smoke_test)
