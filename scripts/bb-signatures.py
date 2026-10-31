#!/usr/bin/env python3
"""Check Chainguard's signature on every org image the overlays deploy, and builds.yml's on
every image this repo builds.

  bb-signatures.py rendered.json [rendered-fips.json ...]

Each input is the image list bb-verify.py writes with -o (Big Bang rendered with the
overlays). Every image from cgr.dev/<org> must carry a valid keyless cosign signature
from Chainguard: org images are signed by Chainguard's issuer (issuer.enforce.dev) with
a subject under the org's ID, so a tag re-pointed to something Chainguard didn't sign for
your org fails CI. Set the org ID (`chainctl iam organizations list`) in
$CHAINGUARD_ORG_ID or --org-id to require your org's subject; without it any Chainguard
signer is accepted. Needs cosign, and registry credentials for cgr.dev in the Docker
config (setup-chainctl provides both in CI).

Images in BUILD_REGISTRY named bigbang-* (built here) must carry the keyless signature
builds.yml makes on main: from BUILD_SIGNER's repo (owner/repo; in CI $GITHUB_REPOSITORY),
else, on ghcr.io/<owner>, from any repo of that owner. That registry's credentials must be
in the Docker config too.

Images are checked once each; tags are resolved to digests so the result is about the
exact image a cluster would pull.
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from orgtags import REGISTRY, org_name  # noqa: E402

from bbcommon import BUILD_SIGNATURE_ISSUER, build_registry, build_signature_identity, build_signer  # noqa: E402
from bbcommon import SIGNATURE_ISSUERS as ISSUERS  # noqa: E402
from bbcommon import signature_identity as identity  # noqa: E402


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
    ap.add_argument("--build-registry", help="registry of the images this repo builds (default: $BUILD_REGISTRY, "
                                             "else the BUILD_REGISTRY file)")
    ap.add_argument("--build-signer", help="owner/repo whose builds.yml signs them (default: $BUILD_SIGNER, "
                                           "else $GITHUB_REPOSITORY)")
    a = ap.parse_args()

    refs = images(a.rendered, f"{REGISTRY}/{org_name(a.org)}/")
    print(f"checking Chainguard signatures on {len(refs)} images"
          + (f" (org ID {a.org_id})" if a.org_id else " (any Chainguard signer: set CHAINGUARD_ORG_ID)"), flush=True)
    bad = [ref for ref in refs if not verify(ref, ISSUERS, identity(a.org_id))]
    built, registry = [], build_registry(a.build_registry)
    if registry:
        subject = build_signature_identity(registry, build_signer(a.build_signer))
        built = images(a.rendered, f"{registry}/bigbang-")
        if built and not subject:
            raise SystemExit(f"{registry}: set BUILD_SIGNER (owner/repo) to check the signatures of the images built here")
        if built:
            print(f"checking builds.yml's signature on {len(built)} images built here ({subject})", flush=True)
            bad += [ref for ref in built if not verify(ref, BUILD_SIGNATURE_ISSUER, subject)]
    total = len(refs) + len(built)
    if bad:
        raise SystemExit(f"{len(bad)} of {total} images failed signature verification")
    print(f"all {total} images carry the expected signature")


def verify(ref, issuer, subject):
    p = subprocess.run(["cosign", "verify", "--certificate-oidc-issuer-regexp", issuer,
                        "--certificate-identity-regexp", subject, ref], capture_output=True, text=True)
    if p.returncode:
        print(f"  UNSIGNED or invalid: {ref}\n    {p.stderr.strip().splitlines()[-1] if p.stderr else ''}")
        return False
    print(f"  ok {ref}")
    return True


if __name__ == "__main__":
    main()
