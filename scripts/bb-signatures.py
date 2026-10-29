#!/usr/bin/env python3
"""Check Chainguard's signature on every org image the overlays deploy.

  bb-signatures.py rendered.json [rendered-fips.json ...]

Each input is the image list bb-verify.py writes with -o (Big Bang rendered with the
overlays). Every image from cgr.dev/<org> must carry a valid keyless cosign signature
from Chainguard: org images are signed by Chainguard's issuer (issuer.enforce.dev) with
a subject under the org's ID, so a tag re-pointed to something Chainguard didn't sign for
your org fails CI. Set the org ID (`chainctl iam organizations list`) in
$CHAINGUARD_ORG_ID or --org-id to require your org's subject; without it any Chainguard
signer is accepted. Needs cosign, and registry credentials for cgr.dev in the Docker
config (setup-chainctl provides both in CI).

Images are checked once each; tags are resolved to digests so the result is about the
exact image a cluster would pull.
"""
import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from orgtags import REGISTRY, org_name  # noqa: E402

# Org images are signed by Chainguard's issuer, under the org's ID; Chainguard's public
# images are signed from GitHub Actions in its chainguard-images organization.
ISSUERS = r"^https://(issuer\.enforce\.dev|token\.actions\.githubusercontent\.com)$"


def identity(org_id):
    org = re.escape(org_id) if org_id else r"[0-9a-f]+"
    return rf"^(https://issuer\.enforce\.dev/{org}/|https://github\.com/chainguard-images/)"


def images(paths, prefix):
    found = set()
    for p in paths:
        for info in json.load(open(p))["packages"].values():
            found |= {ref for ref in info["rendered"] if ref.startswith(prefix)}
    return sorted(found)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rendered", nargs="+", help="bb-verify.py -o output(s)")
    ap.add_argument("--org", help="Chainguard org (default: $CHAINGUARD_ORG, else the CHAINGUARD_ORG file)")
    ap.add_argument("--org-id", default=os.environ.get("CHAINGUARD_ORG_ID"),
                    help="the org's ID, to require signatures under it (default: $CHAINGUARD_ORG_ID)")
    a = ap.parse_args()

    refs = images(a.rendered, f"{REGISTRY}/{org_name(a.org)}/")
    print(f"checking Chainguard signatures on {len(refs)} images"
          + (f" (org ID {a.org_id})" if a.org_id else " (any Chainguard signer: set CHAINGUARD_ORG_ID)"), flush=True)
    bad = []
    for ref in refs:
        p = subprocess.run(["cosign", "verify", "--certificate-oidc-issuer-regexp", ISSUERS,
                            "--certificate-identity-regexp", identity(a.org_id), ref],
                           capture_output=True, text=True)
        if p.returncode:
            bad.append(ref)
            print(f"  UNSIGNED or invalid: {ref}\n    {p.stderr.strip().splitlines()[-1] if p.stderr else ''}")
        else:
            print(f"  ok {ref}")
    if bad:
        raise SystemExit(f"{len(bad)} of {len(refs)} images failed signature verification")
    print(f"all {len(refs)} images carry Chainguard's signature")


if __name__ == "__main__":
    main()
