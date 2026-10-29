#!/usr/bin/env python3
"""Point Big Bang at external S3 object storage (e.g. SeaweedFS) instead of MinIO.

Big Bang deploys MinIO for some packages (its minio packages, and MinIO bundled in
Thanos, Mattermost and GitLab when no external storage is set). This writes a values
overlay, layered after the Chainguard overlays, that turns all of it off and gives
every package that can use S3 your endpoint. object-storage.yaml lists where each
package takes those settings.

  bb-object-storage.py --endpoint http://seaweedfs-s3.seaweedfs.svc.cluster.local:8333 \\
      [--region us-east-1] [--bucket-prefix bigbang] [--with loki] \\
      [-o overlays/values-object-storage.yaml] [--credentials-template site-object-storage.yaml] \\
      [--verify --bigbang work/bigbang --pkgs work/pkgs]

Credentials are never written to the overlay. --credentials-template writes a
skeleton of every access key / secret value for your (uncommitted) site file.
It prints the buckets to create; SeaweedFS does not create them on first use.
--verify renders Big Bang with every package on and the overlay applied, and fails
if a MinIO server or operator image is still deployed.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bbcommon import ROOT, deep_merge, split_ref  # noqa: E402

MINIO_SERVERS = ("ironbank/opensource/minio/minio", "ironbank/opensource/minio/operator",
                 "ironbank/opensource/minio/operator-sidecar")


def tokens(endpoint, region, prefix):
    u = urllib.parse.urlparse(endpoint)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise SystemExit(f"--endpoint must be http(s)://host[:port], not {endpoint!r}")
    return {"endpoint": endpoint.rstrip("/"), "host": u.netloc, "region": region, "prefix": prefix,
            "insecure": u.scheme == "http"}


def fill(value, t):
    """Replace the {tokens} in a value tree; a value that is exactly "{insecure}" becomes a boolean."""
    if isinstance(value, dict):
        return {k: fill(v, t) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, t) for v in value]
    if not isinstance(value, str):
        return value
    if value == "{insecure}":
        return t["insecure"]
    out = value
    while "{bucket:" in out:
        start = out.index("{bucket:")
        end = out.index("}", start)
        out = out[:start] + f"{t['prefix']}-{out[start + 8:end]}" + out[end + 1:]
    for k in ("endpoint", "host", "region", "prefix"):
        out = out.replace("{" + k + "}", str(t[k]))
    return out


def set_dotted(tree, dotted, value):
    keys = dotted.split(".")
    for k in keys[:-1]:
        tree = tree.setdefault(k, {})
    if isinstance(value, dict) and isinstance(tree.get(keys[-1]), dict):
        deep_merge(tree[keys[-1]], value)
    else:
        tree[keys[-1]] = value


def build(spec, t, extra):
    """(overlay, credential value paths, buckets, packages) for the chosen packages."""
    overlay, creds, buckets, chosen = {}, [], [], []
    for dotted, v in spec.get("disable", {}).items():
        set_dotted(overlay, dotted, v)
    for name, pkg in spec["packages"].items():
        if pkg.get("optional") and name not in extra:
            continue
        chosen.append(name)
        for dotted, v in pkg["values"].items():
            set_dotted(overlay, dotted, fill(v, t))
        creds += pkg.get("credentials", [])
        buckets += [f"{t['prefix']}-{b}" for b in pkg.get("buckets", [])]
    return overlay, creds, buckets, chosen


def credentials_template(paths):
    tree = {}
    for p in paths:
        set_dotted(tree, p, "<aws_access_key_id/secret file contents>" if p.endswith(".cloud")
                   else "<access key>" if p.endswith(("accessKey",)) else "<secret key>")
    return tree


def verify(bigbang, pkgs, overlay_path, creds_tree):
    """Render every package with the overlay; fail if MinIO servers/operators are still deployed."""
    with tempfile.TemporaryDirectory() as d:
        fake = os.path.join(d, "creds.yaml")
        yaml.safe_dump(creds_tree, open(fake, "w"))
        cmd = [sys.executable, os.path.join(HERE, "bb-images.py"), "--bigbang", bigbang, "--pkgs", pkgs,
               "--values", os.path.join(ROOT, "discovery", "render-inputs.yaml"),
               "--values", overlay_path, "--values", fake]
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if p.returncode:
        raise SystemExit(p.stderr)
    found = json.loads(p.stdout)
    left = sorted({(rel, ref) for rel, info in found["packages"].items() for ref in info["rendered"]
                   if split_ref(ref)[0] in MINIO_SERVERS})
    errors = found.get("errors", {})
    for rel, err in errors.items():
        print(f"render failed: {rel}: {err}")
    for rel, ref in left:
        print(f"still deploys MinIO: {rel}: {ref}")
    if left or errors:
        raise SystemExit(1)
    print(f"verified: {len(found['packages'])} releases render, none deploys a MinIO server or operator")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--endpoint", required=True, help="S3 endpoint URL, e.g. http://seaweedfs-s3.seaweedfs.svc:8333")
    ap.add_argument("--region", default="us-east-1", help="S3 region (SeaweedFS accepts any; default us-east-1)")
    ap.add_argument("--bucket-prefix", default="bigbang", help="bucket names are <prefix>-<name> (default bigbang)")
    ap.add_argument("--with", dest="extra", action="append", default=[], metavar="PACKAGE",
                    help="also configure an optional package (loki)")
    ap.add_argument("-o", "--output", default=os.path.join(ROOT, "overlays", "values-object-storage.yaml"))
    ap.add_argument("--credentials-template", help="write a skeleton of the credential values here")
    ap.add_argument("--spec", default=os.path.join(ROOT, "object-storage.yaml"))
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--bigbang", default=os.path.join(ROOT, "work", "bigbang"))
    ap.add_argument("--pkgs", default=os.path.join(ROOT, "work", "pkgs"))
    a = ap.parse_args()

    spec = yaml.safe_load(open(a.spec))
    unknown = set(a.extra) - set(spec["packages"])
    if unknown:
        raise SystemExit(f"unknown --with package(s): {sorted(unknown)}")
    t = tokens(a.endpoint, a.region, a.bucket_prefix)
    overlay, creds, buckets, chosen = build(spec, t, set(a.extra))

    header = ("# GENERATED by scripts/bb-object-storage.py from object-storage.yaml - do not edit by hand.\n"
              f"# External S3 object storage at {t['endpoint']} instead of MinIO, for: {', '.join(chosen)}.\n"
              "# Layer it after the Chainguard overlays; credentials go in the site file:\n"
              "# ... -f overlays/values-chainguard.yaml -f overlays/values-object-storage.yaml -f <site>.yaml\n")
    os.makedirs(os.path.dirname(os.path.abspath(a.output)), exist_ok=True)
    with open(a.output, "w", encoding="utf-8", newline="\n") as f:
        f.write(header + yaml.safe_dump(overlay, sort_keys=True, default_flow_style=False, width=120))
    print(f"wrote {os.path.relpath(a.output, ROOT)}")
    if a.credentials_template:
        with open(a.credentials_template, "w", encoding="utf-8", newline="\n") as f:
            f.write("# Object storage credentials for the site file. Do not commit real values.\n"
                    + yaml.safe_dump(credentials_template(creds), sort_keys=True, width=120))
        print(f"wrote {a.credentials_template}")
    print("\nBuckets to create (SeaweedFS: echo 's3.bucket.create -name <bucket>' | weed shell):")
    print("  " + "\n  ".join(buckets))
    print("\nCredential values for the site file:\n  " + "\n  ".join(creds))
    if a.verify:
        verify(a.bigbang, a.pkgs, a.output, credentials_template(creds))


if __name__ == "__main__":
    main()
