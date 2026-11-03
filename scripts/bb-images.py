#!/usr/bin/env python3
"""List every image a Big Bang release actually deploys, per package.

Renders the Big Bang chart with every package enabled, reads each HelmRelease's
valuesFrom Secrets (common -> defaults -> overlays, in that order), renders the
package chart from its git tag with those values, and collects every image
reference. Flux images come from base/flux/kustomization.yaml.

  bb-images.py --bigbang <bigbang checkout> --pkgs <dir> [--values f.yaml ...] [-o out.json]

<dir> holds one clone per package, named by its values key (see --clone).
Extra --values files are layered on top (used by bb-verify.py to test overlays).
"""
import argparse
import json
import os
import shutil
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bbcommon import BASE_LOADER, SAFE_LOADER, bigbang_packages, chart_defaults, deep_merge, image_nodes, safe_load  # noqa: E402

KUBE_VERSION = "1.35.8"
# CRD-gated templates (ServiceMonitor, VirtualService, Kiali CR, ...) only render
# when helm believes the API exists.
API_VERSIONS = [
    "monitoring.coreos.com/v1", "monitoring.coreos.com/v1alpha1",
    "networking.istio.io/v1", "networking.istio.io/v1beta1",
    "security.istio.io/v1", "security.istio.io/v1beta1",
    "install.istio.io/v1alpha1", "kiali.io/v1alpha1",
    "cert-manager.io/v1", "kyverno.io/v1", "kyverno.io/v2beta1",
    "wgpolicyk8s.io/v1alpha2", "external-secrets.io/v1",
    "gateway.networking.k8s.io/v1", "policy/v1",
    "autoscaling.k8s.io/v1", "operator.victoriametrics.com/v1beta1",
    "policies.kyverno.io/v1",
]
IMAGE_RE = re.compile(
    r"(?:registry1\.dso\.mil|registry\.dso\.mil|cgr\.dev|ghcr\.io|docker\.io|quay\.io|"
    r"registry\.k8s\.io|gcr\.io|public\.ecr\.aws)/[a-z0-9._/-]+(?::[A-Za-z0-9._-]+)?(?:@sha256:[a-f0-9]{64})?"
)


def helm_template(chart, values, release, namespace):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values, f)
        vf = f.name
    cmd = ["helm", "template", release, chart, "-n", namespace, "-f", vf,
           "--kube-version", KUBE_VERSION, "--include-crds"]
    for a in API_VERSIONS:
        cmd += ["--api-versions", a]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    finally:
        os.unlink(vf)
    return p.returncode, p.stdout, p.stderr


def render_package(chart, values, release, namespace):
    rc, out, err = helm_template(chart, values, release, namespace)
    if rc and "missing in charts/ directory" in err:
        # Some packages (argocd) ship a .helmignore with "/*.tgz", which Helm 3.18
        # also applies to the vendored charts/*.tgz. Render a copy without it.
        with tempfile.TemporaryDirectory() as tmp:
            copy = os.path.join(tmp, "chart")
            shutil.copytree(chart, copy)
            ignore = os.path.join(copy, ".helmignore")
            lines = [l for l in open(ignore).read().splitlines() if l.strip() != "/*.tgz"]
            open(ignore, "w").write("\n".join(lines) + "\n")
            rc, out, err = helm_template(copy, values, release, namespace)
    return rc, out, err


def _looks_like_image(s):
    if not isinstance(s, str) or "/" not in s or any(c in s for c in " *{}\"'\n"):
        return False
    host = s.split("/")[0]
    return "." in host or ":" in host


def _collect(node, found):
    if isinstance(node, dict):
        img = node.get("image")
        if _looks_like_image(img):
            found.add(img)
        repo = node.get("repository")
        if _looks_like_image(repo) or (isinstance(repo, str) and isinstance(node.get("registry"), str)):
            ref = f'{node["registry"]}/{repo}' if isinstance(node.get("registry"), str) and node["registry"] else repo
            tag = node.get("tag")
            if _looks_like_image(ref):
                found.add(f"{ref}:{tag}" if isinstance(tag, (str, int, float)) and str(tag) else ref)
        if _looks_like_image(node.get("image_name")):  # Kiali CR
            v = node.get("image_version")
            found.add(f'{node["image_name"]}:{v}' if v else node["image_name"])
        for k, v in node.items():
            if k in ("args", "command") and isinstance(v, list):
                for a in v:
                    for m in IMAGE_RE.finditer(str(a)):
                        found.add(m.group(0))
            _collect(v, found)
    elif isinstance(node, list):
        for v in node:
            _collect(v, found)
    elif isinstance(node, str) and len(node) > 40 and ("image" in node) and node.lstrip()[:1] in "{-a":
        # JSON/YAML embedded in a ConfigMap (Istio injector values, operator configs)
        try:
            inner = json.loads(node)
        except ValueError:
            try:
                inner = yaml.load(node, Loader=BASE_LOADER)
            except yaml.YAMLError:
                return
        if isinstance(inner, (dict, list)):
            _collect(inner, found)


def images_in(text):
    """Every image a rendered release deploys: container image fields, split
    registry/repository/tag blocks in CRs, Kiali's image_name, image refs in
    container args, and the same inside JSON/YAML embedded in ConfigMaps."""
    found = set()
    for doc in yaml.load_all(text, Loader=BASE_LOADER):  # BaseLoader: CRDs use odd tags like "="
        if doc:
            _collect(doc, found)
    return {f.strip('"') for f in found}


def _get(tree, path):
    for k in path:
        tree = tree[k]
    return tree


def set_key(values, key, value):
    """values[a][b] = value for the Big Bang key "a.b"."""
    *parents, last = key.split(".")
    for k in parents:
        values = values.setdefault(k, {})
    values[last] = value


def bigbang_releases(bigbang, extra_values):
    values = {}
    for key, x in bigbang_packages(yaml.safe_load(open(os.path.join(bigbang, "chart", "values.yaml")))).items():
        if (x.get("git") or {}).get("repo"):
            set_key(values, key, {"enabled": True})
    values["registryCredentials"] = {"registry": "registry1.dso.mil", "username": "x", "password": "x"}
    for f in extra_values:
        deep_merge(values, yaml.safe_load(open(f)) or {})
    rc, out, err = helm_template(os.path.join(bigbang, "chart"), values, "bigbang", "bigbang")
    if rc:
        sys.exit(f"Big Bang render failed:\n{err}")
    docs = [d for d in yaml.load_all(out, Loader=SAFE_LOADER) if d]
    secrets = {d["metadata"]["name"]: d.get("stringData", {}) for d in docs if d["kind"] == "Secret"}
    repos = {d["metadata"]["name"]: d["spec"]["url"] for d in docs if d["kind"] == "GitRepository"}
    for hr in (d for d in docs if d["kind"] == "HelmRelease"):
        spec = hr["spec"]
        src = spec["chart"]["spec"]["sourceRef"]["name"]
        merged = {}
        for vf in spec.get("valuesFrom", []):
            raw = secrets.get(vf["name"], {}).get(vf.get("valuesKey", "values.yaml"))
            if raw:
                deep_merge(merged, safe_load(raw) or {})
        deep_merge(merged, spec.get("values") or {})
        renames = {}
        for pr in spec.get("postRenderers") or []:
            for img in (pr.get("kustomize") or {}).get("images") or []:
                renames[img["name"]] = img
        yield {
            "renames": renames,
            "release": spec.get("releaseName", hr["metadata"]["name"]),
            "namespace": spec.get("targetNamespace", "default"),
            "repo": repos.get(src, src),
            "path": spec["chart"]["spec"]["chart"],
            "values": merged,
        }


def kustomize_image(name, tag, entry):
    """What a Flux post-renderer's kustomize images entry makes of name[:tag]: newName
    replaces the name; a digest replaces the tag (bb-convert's digest pins), else newTag
    does; otherwise the tag stays. None without an entry."""
    if not entry:
        return None
    name = entry.get("newName", name)
    if entry.get("digest"):
        return f"{name}@{entry['digest']}"
    tag = entry.get("newTag", tag)
    return f"{name}:{tag}" if tag else name


def flux_images(bigbang):
    k = yaml.safe_load(open(os.path.join(bigbang, "base", "flux", "kustomization.yaml")))
    return kustomization_images(k)


def kustomization_images(k):
    """What a kustomization's images entries deploy: newName with its digest (bb-convert's
    pins), else its newTag."""
    return sorted(kustomize_image(i["name"], None, i) for i in k.get("images", []))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bigbang", required=True)
    ap.add_argument("--pkgs", required=True)
    ap.add_argument("--values", action="append", default=[])
    ap.add_argument("--flux-kustomization", help="flux kustomization to read instead of Big Bang's base/flux")
    ap.add_argument("-o", "--output")
    a = ap.parse_args()

    by_repo = {}
    for name in os.listdir(a.pkgs):
        cfg = os.path.join(a.pkgs, name, ".git", "config")
        if os.path.exists(cfg):
            url = re.search(r"url = (\S+)", open(cfg).read()).group(1)
            by_repo[url.rstrip("/").removesuffix(".git")] = os.path.join(a.pkgs, name)

    result = {"packages": {}, "errors": {}}
    if a.flux_kustomization:
        k = yaml.safe_load(open(a.flux_kustomization))
        flux = kustomization_images(k)
    else:
        flux = flux_images(a.bigbang)
    result["packages"]["flux"] = {"key": "flux", "rendered": flux, "values": []}
    # Each package renders on its own (helm template, then parsing its output): in
    # parallel, ~2x faster. Results are taken in Big Bang's order, as a loop would.
    with ThreadPoolExecutor(8) as pool:
        for release, info, error in pool.map(lambda r: package(r, by_repo), bigbang_releases(a.bigbang, a.values)):
            if error:
                result["errors"][release] = error
            else:
                result["packages"][release] = info
    text = json.dumps(result, indent=2, sort_keys=True)
    if a.output:
        open(a.output, "w", newline="\n").write(text + "\n")
    else:
        print(text)


def package(r, by_repo):
    """(release, discovery entry, None), or (release, None, error) when it can't render."""
    d = by_repo.get(r["repo"].rstrip("/").removesuffix(".git"))
    if not d:
        return r["release"], None, f"no clone for {r['repo']}"
    chart = os.path.join(d, r["path"])
    rc, out, err = render_package(chart, r["values"], r["release"], r["namespace"])
    if rc:
        return r["release"], None, err.strip().splitlines()[-1] if err.strip() else "render failed"
    merged = deep_merge(chart_defaults(chart), r["values"])
    nodes = []
    for path, shape, node in image_nodes(merged):
        if shape == "registry":
            entry = {"path": list(path), "shape": shape, "value": node}
        elif shape == "string":
            entry = {"path": list(path), "shape": shape, "value": node}
            parent = _get(merged, path[:-1])
            if ":" not in node.split("/")[-1] and isinstance(parent, dict):
                # tag kept in a sibling key (Kiali repo/tag, Kiali CR image_version, Mattermost version)
                key = str(path[-1])
                pre = key[:-len("Repository")] if key.endswith("Repository") else None
                keys = ([pre + "Release", pre + "Tag"] if pre else []) + ["tag", "image_version", "version"]
                for k in keys:
                    if isinstance(parent.get(k), (str, int, float)) and str(parent.get(k)):
                        entry["tag_key"], entry["tag"] = k, str(parent[k])
                        break
        else:
            entry = {"path": list(path), "shape": shape,
                     "value": {k: str(node[k]) for k in ("registry", "repository", "hub", "image", "tag", "release", "digest")
                               if isinstance(node.get(k), (str, int, float))}}
        ints = [i for i, x in enumerate(path) if isinstance(x, int)]
        if ints:
            # Helm replaces lists wholesale: the overlay has to carry the whole list.
            entry["list_path"] = list(path[:ints[0]])
            entry["list_value"] = _get(merged, path[:ints[0]])
        nodes.append(entry)
    rendered = set()
    for ref in images_in(out):
        name, _, tag = ref.rpartition(":") if ":" in ref.rsplit("/", 1)[-1] else (ref, "", "")
        rendered.add(kustomize_image(name, tag, r["renames"].get(name)) or ref)
    return r["release"], {"key": os.path.basename(d), "rendered": sorted(rendered), "values": nodes}, None


if __name__ == "__main__":
    main()
