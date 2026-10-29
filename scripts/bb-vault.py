#!/usr/bin/env python3
"""Build Chainguard Vault with curl for Big Bang's vault package.

Big Bang's vault chart runs a post-start hook that checks Vault and the Kubernetes API
with curl. Chainguard's vault image has no curl, so the hook times out and Kubernetes
kills Vault; this adds it:

  1. read the Vault tag Big Bang deploys (discovery/images.json)
  2. pin cgr.dev/<org>/vault and vault-fips -dev images by digest, on that tag's
     MAJOR.MINOR line, else the newest release (Chainguard carries only the current
     Vault line), or the line --version names
  3. write builds/vault/build.yaml

  bb-vault.py [--org <org>] [--version "*"]       regenerate build.yaml
  bb-vault.py --build [--test] [--push]           also build both images

--test starts `vault server -dev` and checks curl reaches /v1/sys/seal-status inside
the container (what the hook does), and for -fips that OpenSSL defaults to fips=yes.
"""
import io
import os
import re
import subprocess
import sys
import tarfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bbbuild  # noqa: E402

NAME = "vault"
IB_IMAGE = "ironbank/hashicorp/vault"
IMAGE = "bigbang-vault"
PACKAGES = ["curl"]


def generate(a):
    bb_tag = bbbuild.bigbang_tag(IB_IMAGE)
    repos = {"standard": "vault", "fips": "vault-fips"}
    try:
        images = bbbuild.org_images(a.org, repos, a.version or bb_tag)
    except SystemExit as e:
        if a.version:
            raise
        print(f"{e}; using the org's newest Vault instead")
        images = bbbuild.org_images(a.org, repos, "*")
    spec = {
        "ironbank": {"image": f"{IB_IMAGE}:{bb_tag}"},
        "image": {v: {"name": IMAGE + ("-fips" if v == "fips" else ""), "tag": img["tag"], "dev": img["dev"]}
                  for v, img in images.items()},
        "packages": PACKAGES,
        "notes": ["Chainguard vault plus curl, which Big Bang's vault post-start hook needs"],
    }
    bbbuild.write_spec(NAME, spec, "# Chainguard Vault plus curl for Big Bang's vault post-start hook.\n")
    print(f"vault {images['standard']['tag']} / fips {images['fips']['tag']} (Big Bang pins {bb_tag})")
    return spec


def build_args(spec, variant):
    dev = spec["image"][variant]["dev"]
    subprocess.run(["docker", "pull", "-q", dev], check=True, capture_output=True)
    user = subprocess.run(["docker", "image", "inspect", "--format", "{{.Config.User}}", dev],
                          capture_output=True, text=True, check=True).stdout.strip()
    return {"PACKAGES": " ".join(spec["packages"]), "RUN_USER": user or "65532"}


def smoke_test(image, variant, spec):
    name = f"bb-smoke-vault-{os.getpid()}"
    subprocess.run(["docker", "run", "-d", "--name", name, "--cap-add=IPC_LOCK", image, "server", "-dev",
                    "-dev-listen-address=127.0.0.1:8200"], check=True, capture_output=True)
    try:
        out = ""
        for _ in range(30):
            p = subprocess.run(["docker", "exec", name, "curl", "-s", "-f", "http://127.0.0.1:8200/v1/sys/seal-status"],
                               capture_output=True, text=True)
            out = p.stdout
            if '"initialized"' in out:
                break
            time.sleep(2)
        print(f"  seal-status via curl: {out.strip()[:160] or 'no answer'}")
        if not re.search(r'"initialized"\s*:\s*true', out):
            raise SystemExit(f"{image}: curl could not read Vault's seal status\n" + bbbuild.logs(name))
        version = subprocess.run(["docker", "exec", name, "vault", "version"], capture_output=True, text=True)
        print(f"  {version.stdout.strip()}")
        if variant == "fips":
            cnf = subprocess.run(["docker", "cp", "-L", f"{name}:/etc/ssl/openssl.cnf", "-"],
                                 capture_output=True, check=True)
            with tarfile.open(fileobj=io.BytesIO(cnf.stdout)) as t:
                text = t.extractfile(t.getmembers()[0]).read().decode(errors="replace")
            if not re.search(r"^\s*(default_)?properties\s*=\s*fips=yes", text, re.M):
                raise SystemExit(f"{image}: OpenSSL does not default to fips=yes")
            print("  openssl: fips=yes")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


if __name__ == "__main__":
    bbbuild.run(NAME, __doc__, generate, build_args, smoke_test)
