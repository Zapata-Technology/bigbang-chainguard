"""Rebuild Big Bang's grafana-plugins image FROM the Chainguard org's Grafana.

Chainguard does not publish Iron Bank's big-bang/grafana/grafana-plugins (Grafana
with plugins baked in), so this converts Iron Bank's own recipe instead:

  1. read the Iron Bank tag Big Bang deploys (discovery/images.json)
  2. read Iron Bank's hardening_manifest.yaml (plugin zips + sha256) and Dockerfile
     (where each zip is unpacked) from repo1.dso.mil, pinned to a commit
  3. download each zip, check its sha256, and read the plugin id and version
     (a plugin in the recipe's `rebuild` also gets its Go backend rebuilt from source)
  4. pin cgr.dev/<org>/grafana and grafana-fips (and their -dev twins) for Big
     Bang's Grafana version by digest
  5. return builds/grafana-plugins/build.yaml

--test starts each image and checks Grafana loads every plugin, each with a valid
signature (rebuilt ones: unsigned, and allowed), and, for -fips, that OpenSSL defaults
to fips=yes.

`rebuild`: the Redis datasource's newest release (2.2.1) ships Go backends built with Go
1.19 and old grpc, x/net and plugin SDK modules (8 fixable Critical CVEs). Its backend
is rebuilt from the same release's source with Wolfi's Go and those modules raised
(patches/ adapts it to the newer SDK). The rebuilt files no longer match the signed
MANIFEST.txt, which the build removes: Grafana then loads it as unsigned, so
mapping.yaml `overrides.grafana` allows that one plugin id.

Recipe keys: ironbank {image, project}, org_images {standard, fips}, rebuild {plugin id:
{source ({{version}}: the plugin's), go_bumps, patch}}.
"""
import hashlib
import io
import json
import os
import re
import subprocess
import time
import urllib.request
import zipfile

import bbbuild


def plugin_layout(dockerfile):
    """From Iron Bank's Dockerfile: {zip filename: directory} and every directory it creates."""
    unzips = dict(re.findall(r"unzip\s+\S*?([\w.-]+\.zip)\s+-d\s+(\S+?)(?:\s|;|&&|\\|$)", dockerfile))
    dirs = []
    for m in re.finditer(r"mkdir\s+-p\s+(\{[^}]*\}|\S+)", dockerfile):
        dirs += m.group(1).strip("{}").split(",")
    if not unzips:
        raise SystemExit("Iron Bank's Dockerfile changed shape: no `unzip <zip> -d <dir>` found; update plugin_layout()")
    return unzips, [d.strip() for d in dirs if d.strip()]


def plugin(resource, directory, rebuilds):
    """Download one plugin zip, check its sha256, and read its plugin id."""
    data = bbbuild.fetch(resource["url"])
    want = resource["validation"]["value"]
    if resource["validation"]["type"] != "sha256" or hashlib.sha256(data).hexdigest() != want:
        raise SystemExit(f"{resource['url']}: sha256 does not match Iron Bank's manifest")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        meta = sorted((n for n in z.namelist() if n.endswith("plugin.json")), key=lambda n: n.count("/"))
        if not meta:
            raise SystemExit(f"{resource['url']}: no plugin.json in the zip")
        info = json.loads(z.read(meta[0]))
    out = {"id": info["id"], "url": resource["url"], "sha256": want, "dir": directory}
    if info["id"] in rebuilds:
        out["rebuild"] = rebuild(rebuilds[info["id"]], info)
    return out


def rebuild(how, info):
    """How to rebuild a plugin's Go backend: its release's source (by sha256), the
    executable name Grafana runs, the modules to raise and the patch to apply."""
    if not info.get("backend") or not info.get("executable"):
        raise SystemExit(f"{info['id']}: the recipe's `rebuild` lists it, but its plugin.json has no Go backend")
    url = bbbuild.fill(how["source"], {"version": info["info"]["version"]})
    return {"source": url, "sha256": hashlib.sha256(bbbuild.fetch(url)).hexdigest(),
            "executable": info["executable"], "patch": how["patch"],
            "go_get": [bbbuild.go_latest(m) for m in how["go_bumps"]]}


def generate(r, a):
    ib_image, ib_project = r["ironbank"]["image"], r["ironbank"]["project"]
    bb_tag = bbbuild.bigbang_tag(ib_image)
    commit, manifest, dockerfile = bbbuild.ironbank_recipe(ib_project, a.ref)
    unzips, dirs = plugin_layout(dockerfile)
    plugins = []
    for res in manifest.get("resources", []):
        if res["filename"] not in unzips:
            raise SystemExit(f"Iron Bank's Dockerfile does not unpack {res['filename']}; update plugin_layout()")
        plugins.append(plugin(res, unzips[res["filename"]], r.get("rebuild") or {}))
    empty = [d for d in dirs if d not in {p["dir"] for p in plugins} and d.startswith("/var/lib/bb-plugins/")]
    images = bbbuild.org_images(a.org, r["org_images"], bb_tag)
    spec = {
        "ironbank": {"image": f"{ib_image}:{bb_tag}", "source": f"{bbbuild.REPO1}/dsop/{ib_project}",
                     "commit": commit, "manifest_tags": manifest.get("tags", [])},
        # Tagged with the Grafana version each is built FROM.
        "image": {v: {"name": bbbuild.image_name(r, v), **img} for v, img in images.items()},
        "plugins": plugins,
        "empty_dirs": empty,
    }
    print(f"grafana {images['standard']['tag']} / fips {images['fips']['tag']}, plugins "
          + ", ".join(p["id"] for p in plugins))
    return spec


def build_args(spec, variant, r=None):
    rebuilds = [p for p in spec["plugins"] if p.get("rebuild")]
    return {"PLUGINS": ";".join(f"{p['url']} {p['sha256']} {p['dir']}" for p in spec["plugins"]),
            "EMPTY_DIRS": " ".join(spec["empty_dirs"]),
            # "<dir> <source> <sha256> <executable> <patch> <module@version>..." per plugin
            "REBUILDS": ";".join(" ".join([p["dir"], b["source"], b["sha256"], b["executable"], b["patch"],
                                          *b["go_get"]]) for p in rebuilds for b in [p["rebuild"]])}


def smoke_test(image, variant, spec, r=None, port=3300):
    name = f"bb-smoke-{os.getpid()}"
    # The rebuilt plugins allowed the way mapping.yaml's overrides.grafana allows them.
    unsigned = ",".join(p["id"] for p in spec["plugins"] if p.get("rebuild"))
    subprocess.run(["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:3000",
                    "-e", "GF_PATHS_PLUGINS=/var/lib/bb-plugins",
                    "-e", f"GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS={unsigned}", image],
                   check=True, capture_output=True)
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
            want = "unsigned" if p.get("rebuild") else "valid"
            print(f"  {p['id']}: {loaded.get(p['id'], 'missing')}")
            if loaded.get(p["id"]) != want:
                raise SystemExit(f"{image}: plugin {p['id']} did not load ({want} signature)\n" + bbbuild.logs(name))
        # Only ours: Chainguard's Grafana reports its own bundled plugins as signatureMissing.
        ours = {p["id"] for p in spec["plugins"]}
        errors = [e for e in json.load(urllib.request.urlopen(urllib.request.Request(
            f"{base}/api/plugins/errors", headers=req.headers), timeout=10)) if e.get("pluginId") in ours]
        if errors:
            raise SystemExit(f"{image}: plugin errors {errors}\n" + bbbuild.logs(name))
        if variant == "fips":
            bbbuild.require_openssl_fips(name, image)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
