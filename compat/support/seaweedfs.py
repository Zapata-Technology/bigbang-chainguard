#!/usr/bin/env python3
"""Setup for compat/object-storage.yaml: deploy SeaweedFS the way a site does.

Run by bb-compat.py (a `setup` command) with COMPAT_OUT (bb-convert's output for the
test, including seaweedfs/ and seaweedfs-fips/), COMPAT_DOCKERCFG and COMPAT_FIPS set:
  1. create the seaweedfs namespace, its private-registry Secret, and the seaweedfs-s3
     Secret (S3 identities) with throwaway test keys
  2. kubectl apply -k $COMPAT_OUT/seaweedfs (or seaweedfs-fips) and wait for the server
     and the bucket Job
  3. write $COMPAT_OUT/site-object-storage.yaml: the site credentials, same test keys, for
     the packages named on the command line only (Big Bang creates Mimir's credential
     Secret whenever its keys are set, even with Mimir off, and the namespace is missing)
"""
import os
import subprocess
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import objstore  # noqa: E402

NS = objstore.NAMESPACE
# Test-only keys for a throwaway kind cluster.
ACCESS, SECRET = "compat-access", "compat-secret-not-for-production"


def run(*cmd, stdin=None, check=True):
    p = subprocess.run(cmd, input=stdin, text=True, capture_output=True)
    if check and p.returncode:
        raise SystemExit(f"{' '.join(cmd[:5])} failed:\n{p.stdout[-1500:]}\n{p.stderr[-1500:]}")
    return p


def apply_secret(*args):
    manifest = run("kubectl", "-n", NS, "create", "secret", *args, "--dry-run=client", "-o", "yaml").stdout
    run("kubectl", "apply", "-f", "-", stdin=manifest)


def main():
    out = os.environ["COMPAT_OUT"]
    kustomization = os.path.join(out, "seaweedfs-fips" if os.environ.get("COMPAT_FIPS") else "seaweedfs")
    if not os.path.exists(os.path.join(kustomization, "kustomization.yaml")):
        raise SystemExit(f"{kustomization} was not generated (OBJECT_STORAGE must be seaweedfs)")

    run("kubectl", "create", "namespace", NS, check=False)
    apply_secret("generic", "private-registry", "--type=kubernetes.io/dockerconfigjson",
                 f"--from-file=.dockerconfigjson={os.environ['COMPAT_DOCKERCFG']}")
    apply_secret("generic", "seaweedfs-s3", f"--from-literal=s3.json={objstore.s3_config(ACCESS, SECRET)}")
    run("kubectl", "apply", "-k", kustomization)
    run("kubectl", "-n", NS, "rollout", "status", "deploy/seaweedfs", "--timeout=300s")
    job = run("kubectl", "-n", NS, "wait", "job/seaweedfs-buckets", "--for=condition=complete", "--timeout=300s",
              check=False)
    print(run("kubectl", "-n", NS, "logs", "job/seaweedfs-buckets", "--tail=40", check=False).stdout)
    if job.returncode:
        raise SystemExit("the bucket Job did not complete")

    spec = objstore.load_spec()
    enabled = sys.argv[1:]
    _, cred_paths, _, _ = objstore.build(spec, objstore.tokens(objstore.endpoint(), "us-east-1", "bigbang"), set())
    cred_paths = objstore.credentials_for(cred_paths, enabled)
    with open(os.path.join(out, "site-object-storage.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(objstore.credentials_template(cred_paths, ACCESS, SECRET), f)
    print(f"SeaweedFS ready at {objstore.endpoint()}")


if __name__ == "__main__":
    main()
