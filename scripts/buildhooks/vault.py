"""Build Chainguard Vault with curl for Big Bang's vault package.

Big Bang's vault chart runs a post-start hook that checks Vault and the Kubernetes API
with curl. Chainguard's vault image has no curl, so the hook times out and Kubernetes
kills Vault; this adds it:

  1. read the Vault tag Big Bang deploys (discovery/images.json)
  2. pin cgr.dev/<org>/vault and vault-fips -dev images by digest, on that tag's
     MAJOR.MINOR line, else the newest release (Chainguard carries only the current
     Vault line), or the line --version names
  3. return builds/vault/build.yaml

--test starts `vault server -dev` and checks curl reaches /v1/sys/seal-status inside
the container (what the hook does), and for -fips that OpenSSL defaults to fips=yes.

Recipe keys: ironbank {image}, org_images, packages, notes.
"""
import os
import re
import subprocess
import time

import bbbuild


def generate(r, a):
    ib_image = r["ironbank"]["image"]
    bb_tag = bbbuild.bigbang_tag(ib_image)
    repos = r["org_images"]
    try:
        images = bbbuild.org_images(a.org, repos, a.version or bb_tag)
    except SystemExit as e:
        if a.version:
            raise
        print(f"{e}; using the org's newest Vault instead")
        images = bbbuild.org_images(a.org, repos, "*")
    spec = {
        "ironbank": {"image": f"{ib_image}:{bb_tag}"},
        "image": {v: {"name": bbbuild.image_name(r, v), "tag": img["tag"], "dev": img["dev"]}
                  for v, img in images.items()},
        "packages": r["packages"],
        "notes": r["notes"],
    }
    print(f"vault {images['standard']['tag']} / fips {images['fips']['tag']} (Big Bang pins {bb_tag})")
    return spec


def build_args(spec, variant, r=None):
    # The final stage switches back to the user the vault image runs as.
    dev = spec["image"][variant]["dev"]
    subprocess.run(["docker", "pull", "-q", dev], check=True, capture_output=True)
    user = subprocess.run(["docker", "image", "inspect", "--format", "{{.Config.User}}", dev],
                          capture_output=True, text=True, check=True).stdout.strip()
    return {"PACKAGES": " ".join(spec["packages"]), "RUN_USER": user or "65532"}


def smoke_test(image, variant, spec, r=None):
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
            bbbuild.require_openssl_fips(name, image)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
