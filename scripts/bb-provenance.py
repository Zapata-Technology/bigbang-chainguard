#!/usr/bin/env python3
"""Write a SLSA v1 provenance predicate for one image this repo builds.

  bb-provenance.py <name> <image@digest> -o predicate.json

builds.yml attests it to each pushed image (cosign attest --type slsaprovenance1), next to
the signature and the SBOM: which workflow run built the image, from which commit, and
every pinned input in builds/<name>/build.yaml (base and -dev images by digest, downloads
by sha256). The variant (standard or FIPS) comes from the image name. Reads the GitHub
Actions environment (GITHUB_REPOSITORY, GITHUB_SHA, GITHUB_REF, GITHUB_RUN_ID, ...).

`ironbank` in build.yaml describes the Iron Bank image being replaced, not an input, so it
is left out.
"""
import argparse
import json
import os
import re
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIGEST = re.compile(r"^([^@\s]+)@sha256:([0-9a-f]{64})$")


def inputs(node):
    """[(uri, sha256)] of the pinned inputs under `node`: image@sha256 strings, and
    mappings with a url (or source) and a sha256."""
    out = []
    if isinstance(node, dict):
        url = node.get("url") or node.get("source")
        if isinstance(url, str) and isinstance(node.get("sha256"), str):
            out.append((url, node["sha256"]))
        for v in node.values():
            out += inputs(v)
    elif isinstance(node, list):
        for v in node:
            out += inputs(v)
    elif isinstance(node, str) and (m := DIGEST.match(node)):
        out.append((f"oci://{m.group(1)}", m.group(2)))
    return out


def predicate(name, image, spec, env):
    variant = "fips" if image.split("@")[0].rsplit(":", 1)[0].endswith("-fips") else "standard"
    spec = {k: v for k, v in spec.items() if k != "ironbank"}
    images = spec.pop("image", {})
    deps = inputs(images.get(variant, {})) + inputs(spec)
    server, repo = env.get("GITHUB_SERVER_URL", "https://github.com"), env["GITHUB_REPOSITORY"]
    workflow = ".github/workflows/builds.yml"
    seen, resolved = set(), [{"uri": f"git+{server}/{repo}@{env['GITHUB_REF']}",
                              "digest": {"gitCommit": env["GITHUB_SHA"]}}]
    for uri, sha in deps:
        if (uri, sha) not in seen:
            seen.add((uri, sha))
            resolved.append({"uri": uri, "digest": {"sha256": sha}})
    run = f"{server}/{repo}/actions/runs/{env['GITHUB_RUN_ID']}/attempts/{env.get('GITHUB_RUN_ATTEMPT', '1')}"
    return {"buildDefinition": {
                "buildType": f"{server}/{repo}/{workflow}",
                "externalParameters": {"workflow": {"repository": f"{server}/{repo}", "path": workflow,
                                                    "ref": env["GITHUB_REF"]},
                                       "builder": f"scripts/bb-{name}.py", "variant": variant},
                "resolvedDependencies": resolved},
            "runDetails": {"builder": {"id": f"{server}/actions/runner/github-hosted"},
                           "metadata": {"invocationId": run}}}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="the builder: builds/<name>/build.yaml")
    ap.add_argument("image", help="the pushed image, <repo>@sha256:<digest>")
    ap.add_argument("-o", "--output", required=True)
    a = ap.parse_args()
    spec = yaml.safe_load(open(os.path.join(ROOT, "builds", a.name, "build.yaml"), encoding="utf-8")) or {}
    missing = [k for k in ("GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_REF", "GITHUB_RUN_ID") if k not in os.environ]
    if missing:
        sys.exit(f"not in GitHub Actions: {', '.join(missing)} unset")
    json.dump(predicate(a.name, a.image, spec, os.environ), open(a.output, "w", encoding="utf-8"), indent=1)


if __name__ == "__main__":
    main()
