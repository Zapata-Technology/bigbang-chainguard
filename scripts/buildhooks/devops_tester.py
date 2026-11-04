"""Rebuild Big Bang's test-runner image (Iron Bank big-bang/devops-tester) FROM the org.

Iron Bank's devops-tester is an Alpine pipeline runner plus kubectl, velero, the
kyverno CLI, chainsaw and mc, run as devops-user (uid 1000). Big Bang's bbtests
script jobs use it. This converts Iron Bank's own recipe:

  1. read the Iron Bank tag Big Bang deploys (discovery/images.json)
  2. read Iron Bank's Dockerfile (the image and version of each tool it copies in,
     the user it creates) and hardening_manifest.yaml (pinned digests) from
     repo1.dso.mil, pinned to a commit
  3. pin cgr.dev/<org>/kubectl and kubectl-fips -dev images on Iron Bank's kubectl
     MAJOR.MINOR line by digest
  4. velero and the kyverno CLI are copied from the org's velero(-fips) and
     kyverno-cli(-fips) images on Iron Bank's MAJOR.MINOR lines (so the FIPS image
     gets FIPS builds); mc comes from Wolfi. chainsaw has no Chainguard package, so
     it is built from source at Iron Bank's version with the Wolfi Go toolchain, with
     the recipe's chainsaw.go_bumps raised to their newest release. Upstream's image (the
     one Iron Bank copies) is built with an old Go and x/net, which carry a fixable
     Critical CVE.
  5. return builds/devops-tester/build.yaml

Recipe keys: ironbank {image, project}, org_images (the -dev image to build in),
tool_images ({Iron Bank stage: {standard, fips}}, copied in at Iron Bank's line),
chainsaw {source ({{version}}), go_bumps}, packages, notes.
"""
import hashlib
import re

import bbbuild

STAGES = {"kubectl", "velero", "kyverno", "chainsaw"}


def recipe(manifest, dockerfile):
    """Iron Bank's tool versions (from its FROM ... AS <stage> lines), chainsaw digest, user."""
    stages = {m.group(3): (m.group(1), m.group(2))
              for m in re.finditer(r"^FROM\s+(\S+?):v?(\d+\.\d+\.\d+)\S*\s+AS\s+(\w+)", dockerfile, re.M | re.I)}
    missing = STAGES - set(stages)
    if missing:
        raise SystemExit(f"Iron Bank's Dockerfile no longer has stages {sorted(missing)}; update recipe()")
    chainsaw = next((r["url"].removeprefix("docker://") for r in manifest.get("resources", [])
                     if "chainsaw@sha256:" in r.get("url", "")), None)
    if not chainsaw:
        raise SystemExit("Iron Bank's manifest no longer pins chainsaw by digest; update recipe()")
    group = re.search(r"addgroup\s+(?:-g\s+\d+\s+)?([\w-]+)", dockerfile)
    user = re.search(r"adduser\s+([\w-]+)\s+.*?-u\s+(\d+)", dockerfile)
    if not (group and user):
        raise SystemExit("Iron Bank's Dockerfile changed shape (addgroup / adduser); update recipe()")
    return ({k: v[1] for k, v in stages.items()}, chainsaw,
            {"name": user.group(1), "group": group.group(1), "id": int(user.group(2))})


def chainsaw_source(how, version):
    url = bbbuild.fill(how["source"], {"version": version})
    return {"version": version, "source": url, "sha256": hashlib.sha256(bbbuild.fetch(url)).hexdigest(),
            "go_get": [bbbuild.go_latest(m) for m in how["go_bumps"]]}


def generate(r, a):
    ib_image, ib_project = r["ironbank"]["image"], r["ironbank"]["project"]
    bb_tag = bbbuild.bigbang_tag(ib_image)
    commit, manifest, dockerfile = bbbuild.ironbank_recipe(ib_project, a.ref)
    versions, chainsaw, user = recipe(manifest, dockerfile)
    images = bbbuild.org_images(a.org, r["org_images"], versions["kubectl"])
    tools = {tool: bbbuild.org_images(a.org, repos, versions[tool], dev=False)
             for tool, repos in r["tool_images"].items()}
    spec = {
        "ironbank": {"image": f"{ib_image}:{bb_tag}", "source": f"{bbbuild.REPO1}/dsop/{ib_project}",
                     "commit": commit, "versions": versions, "chainsaw": chainsaw},
        "image": {v: {"name": bbbuild.image_name(r, v), "tag": f"{bb_tag}-kubectl{img['tag']}",
                      "dev": img["dev"], **{tool: pinned[v]["base"] for tool, pinned in tools.items()}}
                  for v, img in images.items()},
        "chainsaw": chainsaw_source(r["chainsaw"], versions["chainsaw"]),
        "packages": r["packages"],
        "user": user,
        "notes": r["notes"],
    }
    print(f"kubectl {images['standard']['tag']} / fips {images['fips']['tag']}, packages {' '.join(r['packages'])}, "
          f"chainsaw {versions['chainsaw']}")
    return spec
