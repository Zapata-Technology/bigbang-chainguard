#!/usr/bin/env python3
"""Setup for compat/object-storage.yaml: SeaweedFS in the test cluster, plus the overlay.

Run by bb-compat.py (a `setup` command) with COMPAT_OUT, COMPAT_DOCKERCFG and
COMPAT_FIPS set. It:
  1. deploys SeaweedFS (server + S3 gateway, no auth) from the org's seaweedfs(-fips)
     image, newest release, in namespace seaweedfs
  2. writes $COMPAT_OUT/values-object-storage.yaml and site-object-storage.yaml with
     scripts/bb-object-storage.py, pointed at it
  3. creates every bucket that overlay names (SeaweedFS doesn't create them on first use)
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from orgtags import REGISTRY, OrgTags, org_name, resolve  # noqa: E402

NS = "seaweedfs"
ENDPOINT = f"http://seaweedfs.{NS}.svc.cluster.local:8333"


def run(*cmd, stdin=None):
    p = subprocess.run(cmd, input=stdin, text=True, capture_output=True)
    if p.returncode:
        raise SystemExit(f"{' '.join(cmd[:4])} failed:\n{p.stderr[-1500:]}")
    return p.stdout


def manifest(image):
    return json.dumps({"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "seaweedfs", "namespace": NS},
         "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "seaweedfs"}},
                  "template": {"metadata": {"labels": {"app": "seaweedfs"}}, "spec": {
                      "imagePullSecrets": [{"name": "private-registry"}],
                      "containers": [{"name": "seaweedfs", "image": image,
                                      "args": ["server", "-dir=/data", "-s3", "-s3.port=8333",
                                               "-master.volumeSizeLimitMB=64", "-volume.max=20"],
                                      "ports": [{"containerPort": 8333}, {"containerPort": 9333}],
                                      "readinessProbe": {"tcpSocket": {"port": 8333}, "periodSeconds": 5},
                                      "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
                      "volumes": [{"name": "data", "emptyDir": {}}]}}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "seaweedfs", "namespace": NS},
         "spec": {"selector": {"app": "seaweedfs"}, "ports": [{"name": "s3", "port": 8333}]}},
    ]})


def main():
    out = os.environ["COMPAT_OUT"]
    repo = "seaweedfs-fips" if os.environ.get("COMPAT_FIPS") else "seaweedfs"
    org = org_name()
    tag, _ = resolve("*", OrgTags(org).tags(repo) or [])
    if not tag:
        raise SystemExit(f"{REGISTRY}/{org}/{repo} has no release tags")
    image = f"{REGISTRY}/{org}/{repo}:{tag}"
    print(f"seaweedfs: {image}")

    run("kubectl", "create", "namespace", NS)
    secret = run("kubectl", "-n", NS, "create", "secret", "generic", "private-registry",
                 "--type=kubernetes.io/dockerconfigjson", f"--from-file=.dockerconfigjson={os.environ['COMPAT_DOCKERCFG']}",
                 "--dry-run=client", "-o", "yaml")
    run("kubectl", "apply", "-f", "-", stdin=secret)
    run("kubectl", "apply", "-f", "-", stdin=manifest(image))
    run("kubectl", "-n", NS, "rollout", "status", "deploy/seaweedfs", "--timeout=300s")

    listing = run(sys.executable, os.path.join(ROOT, "scripts", "bb-object-storage.py"), "--endpoint", ENDPOINT,
                  "-o", os.path.join(out, "values-object-storage.yaml"),
                  "--credentials-template", os.path.join(out, "site-object-storage.yaml"))
    buckets = [ln.strip() for ln in listing.split("Buckets to create")[1].split("Credential values")[0].splitlines()
               if ln.strip().startswith("bigbang-")]
    commands = "".join(f"s3.bucket.create -name {b}\n" for b in buckets) + "s3.bucket.list\n"
    print(run("kubectl", "-n", NS, "exec", "-i", "deploy/seaweedfs", "--", "weed", "shell",
              "-master=localhost:9333", stdin=commands))
    print(f"created {len(buckets)} buckets for {ENDPOINT}")


if __name__ == "__main__":
    main()
