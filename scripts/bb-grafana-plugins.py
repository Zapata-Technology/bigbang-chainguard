#!/usr/bin/env python3
"""Rebuild Big Bang's grafana-plugins image FROM the Chainguard org's Grafana.

Chainguard does not publish Iron Bank's big-bang/grafana/grafana-plugins (Grafana
with plugins baked in), so this converts Iron Bank's own recipe instead:

  1. read the Iron Bank tag Big Bang deploys (discovery/images.json)
  2. read Iron Bank's hardening_manifest.yaml (plugin zips + sha256) and Dockerfile
     (where each zip is unpacked) from repo1.dso.mil, pinned to a commit
  3. download each zip, check its sha256, and read the plugin id
  4. pin cgr.dev/<org>/grafana and grafana-fips (and their -dev twins) for Big
     Bang's Grafana version by digest
  5. write builds/grafana-plugins/build.yaml

  bb-grafana-plugins.py [--org <org>] [--ref master]       regenerate build.yaml
  bb-grafana-plugins.py --build [--test] [--push]          also build both images

--build    docker build <BUILD_REGISTRY>/grafana-plugins:<tag> and -fips:<tag>
           (bb-local/... when BUILD_REGISTRY is empty)
--test     start each image and check Grafana loads every plugin with a valid
           signature (and, for -fips, that OpenSSL defaults to fips=yes)
--push     docker push both images to BUILD_REGISTRY (never cgr.dev)

Then run scripts/bb-convert.py: with BUILD_REGISTRY set, the overlays use the
built images; with it empty, Grafana stays on Iron Bank's image.
"""
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bbbuild  # noqa: E402

NAME = "grafana-plugins"
IB_IMAGE = "ironbank/big-bang/grafana/grafana-plugins"
IB_PROJECT = "big-bang/grafana/grafana-plugins"


def plugin_layout(dockerfile):
    """From Iron Bank's Dockerfile: {zip filename: directory} and every directory it creates."""
    unzips = dict(re.findall(r"unzip\s+\S*?([\w.-]+\.zip)\s+-d\s+(\S+?)(?:\s|;|&&|\\|$)", dockerfile))
    dirs = []
    for m in re.finditer(r"mkdir\s+-p\s+(\{[^}]*\}|\S+)", dockerfile):
        dirs += m.group(1).strip("{}").split(",")
    if not unzips:
        raise SystemExit("Iron Bank's Dockerfile changed shape: no `unzip <zip> -d <dir>` found; update plugin_layout()")
    return unzips, [d.strip() for d in dirs if d.strip()]


def plugin(resource, directory):
    """Download one plugin zip, check its sha256, and read its plugin id."""
    data = bbbuild.fetch(resource["url"])
    want = resource["validation"]["value"]
    if resource["validation"]["type"] != "sha256" or hashlib.sha256(data).hexdigest() != want:
        raise SystemExit(f"{resource['url']}: sha256 does not match Iron Bank's manifest")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        meta = sorted((n for n in z.namelist() if n.endswith("plugin.json")), key=lambda n: n.count("/"))
        if not meta:
            raise SystemExit(f"{resource['url']}: no plugin.json in the zip")
        pid = json.loads(z.read(meta[0]))["id"]
    return {"id": pid, "url": resource["url"], "sha256": want, "dir": directory}


def generate(a):
    bb_tag = bbbuild.bigbang_tag(IB_IMAGE)
    commit, manifest, dockerfile = bbbuild.ironbank_recipe(IB_PROJECT, a.ref)
    unzips, dirs = plugin_layout(dockerfile)
    plugins = []
    for res in manifest.get("resources", []):
        if res["filename"] not in unzips:
            raise SystemExit(f"Iron Bank's Dockerfile does not unpack {res['filename']}; update plugin_layout()")
        plugins.append(plugin(res, unzips[res["filename"]]))
    empty = [d for d in dirs if d not in {p["dir"] for p in plugins} and d.startswith("/var/lib/bb-plugins/")]
    tag, images = bbbuild.org_images(a.org, {"standard": "grafana", "fips": "grafana-fips"}, bb_tag)
    spec = {
        "tag": tag,
        "ironbank": {"image": f"{IB_IMAGE}:{bb_tag}", "source": f"{bbbuild.REPO1}/dsop/{IB_PROJECT}",
                     "commit": commit, "manifest_tags": manifest.get("tags", [])},
        "image": {v: {"name": NAME + ("-fips" if v == "fips" else ""), **img} for v, img in images.items()},
        "plugins": plugins,
        "empty_dirs": empty,
    }
    bbbuild.write_spec(NAME, spec, "# Big Bang's grafana-plugins image rebuilt FROM the Chainguard org's grafana images,\n"
                                   "# with the plugin set from Iron Bank's hardening_manifest.yaml at `ironbank.commit`.\n")
    print(f"grafana {tag}, plugins " + ", ".join(p["id"] for p in plugins))
    return spec


def build_args(spec, variant):
    return {"PLUGINS": ";".join(f"{p['url']} {p['sha256']} {p['dir']}" for p in spec["plugins"]),
            "EMPTY_DIRS": " ".join(spec["empty_dirs"])}


def smoke_test(image, variant, spec, port=3300):
    name = f"bb-smoke-{os.getpid()}"
    subprocess.run(["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:3000",
                    "-e", "GF_PATHS_PLUGINS=/var/lib/bb-plugins", image], check=True, capture_output=True)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{base}/api/health", timeout=5).read()
                break
            except OSError:
                time.sleep(2)
        else:
            raise SystemExit(f"{image}: Grafana never became healthy\n" + bbbuild.logs(name))
        req = urllib.request.Request(f"{base}/api/plugins?embedded=0",
                                     headers={"Authorization": "Basic YWRtaW46YWRtaW4="})  # admin:admin, fresh container
        loaded = {p["id"]: p.get("signature") for p in json.load(urllib.request.urlopen(req, timeout=10))}
        for p in spec["plugins"]:
            print(f"  {p['id']}: {loaded.get(p['id'], 'missing')}")
            if loaded.get(p["id"]) != "valid":
                raise SystemExit(f"{image}: plugin {p['id']} did not load with a valid signature\n" + bbbuild.logs(name))
        if variant == "fips":
            # The runtime image has no shell: copy the file out (docker cp - writes a tar stream).
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
