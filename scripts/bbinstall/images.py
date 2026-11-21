"""The images an install deploys, and a read-only pull test of each with the site's logins."""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import yaml

from bbinstall.checkout import _build_registry
from bbinstall.sitefile import merged_values

def image_problems(co, fips, enabled, logins):
    """A pull test of every image the enabled packages deploy from cgr.dev/<org> and the
    build registry, with `logins` ({registry host: (user, password) or None}). A refused pull
    from the org is most often an image your Chainguard plan doesn't cover."""
    refs = [(r, logins.get(r.split("/", 1)[0])) for prefix in (co.registry, _build_registry(co)) if prefix
            for r in deployed_images(co, fips, prefix, enabled)]
    with ThreadPoolExecutor(8) as pool:
        found = list(pool.map(lambda r: pull_problem(*r), refs))
    return len(refs), [p for p in found if p]


def build_images(co, fips):
    """Every image reference of this repo's builds (<build registry>/bigbang-*) the overlays deploy."""
    prefix = _build_registry(co)
    return [r for r in deployed_images(co, fips, prefix) if r.startswith(prefix + "/bigbang-")] if prefix else []


def pull_check(refs):
    """A Credentials.registry check: the first of `refs` a login can't pull, and why."""
    return lambda login: next((p for r in refs if (p := pull_problem(r, login))), None)


def deployed_images(co, fips, prefix, enabled=None):
    """Every image reference under `prefix` (host[/path]) the merged overlays deploy; with
    `enabled` (Big Bang keys, packages.<name>), only those packages'."""
    refs = set()
    if not prefix:
        return []
    values = merged_values({}, co.overlays(fips))   # what deploys: FIPS replaces the standard refs
    if enabled is not None:
        values = {k: v for k, v in (values.get("packages") or {}).items() if f"packages.{k}" in enabled}
    _walk_refs(values, prefix, refs)
    return sorted(refs)


def split_repo(rest):
    """(repository, tag or digest) of "repo:tag", "repo@sha256:..." or "repo:tag@sha256:...";
    a digest wins over the tag, as it does for the kubelet."""
    if "@" in rest:
        repo, digest = rest.split("@", 1)
        if ":" in repo.rsplit("/", 1)[-1]:
            repo = repo.rsplit(":", 1)[0]
        return repo, digest
    return tuple(rest.rsplit(":", 1))


def _walk_refs(node, prefix, refs):
    """Every host/repo:tag under `prefix` in a values tree, into `refs`."""
    if isinstance(node, dict):
        repo, tag = node.get("repository"), node.get("tag")
        if isinstance(repo, str) and tag is not None:
            full = f"{node['registry']}/{repo}" if isinstance(node.get("registry"), str) else repo
            if full.startswith(prefix + "/"):
                refs.add(f"{full}:{tag}")
        for v in node.values():
            _walk_refs(v, prefix, refs)
    elif isinstance(node, list):
        for v in node:
            _walk_refs(v, prefix, refs)
    elif isinstance(node, str) and re.fullmatch(re.escape(prefix) + r"/[\w./-]+:[\w.-]+", node):
        refs.add(node)


def pinned_refs(co, fips, prefix, enabled=None):
    """[(host/repo:tag, digest or None)] of every image under `prefix` the install runs: each
    package's images with the digest its post-renderer pins (Converter.digest_pins; values
    keep the tag), Flux's controllers, and SeaweedFS when it runs in the cluster. With
    `enabled` (packages.<name> keys), only those packages'."""
    out = set()
    values = merged_values({}, co.overlays(fips))
    for name, pkg in (values.get("packages") or {}).items():
        if not isinstance(pkg, dict) or (enabled is not None and f"packages.{name}" not in enabled):
            continue
        pins = {i["name"]: i["digest"] for r in pkg.get("postRenderers") or []
                for i in ((r or {}).get("kustomize") or {}).get("images") or [] if i.get("digest")}
        refs = set()
        _walk_refs(pkg.get("values") or {}, prefix, refs)
        out |= {(r, pins.get(r.rsplit(":", 1)[0])) for r in refs}
    flux = os.path.join(co.root, "flux-fips" if fips else "flux", "kustomization.yaml")
    if os.path.exists(flux):
        for i in (yaml.safe_load(open(flux, encoding="utf-8")) or {}).get("images") or []:
            name = i.get("newName") or i.get("name", "")
            if name.startswith(prefix + "/") and i.get("newTag"):
                out.add((f"{name}:{i['newTag']}", i.get("digest")))
    if co.in_cluster_seaweedfs():
        d = co.path("seaweedfs-fips" if fips else "seaweedfs")
        for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            text = open(os.path.join(d, f), encoding="utf-8").read()
            for ref, digest in re.findall(re.escape(prefix) + r"/([\w./-]+:[\w.-]+)(?:@(sha256:[0-9a-f]{64}))?", text):
                out.add((f"{prefix}/{ref}", digest or None))
    return sorted(out, key=lambda x: (x[0], x[1] or ""))


def mirror_problems(co, fips, enabled, mirror, login):
    """(count, missing): every image the install pulls from the site registry `mirror`, each
    looked up by its pinned digest (by tag when nothing pins it). Manifest HEAD requests only,
    in parallel, with the site registry's `login` ((user, password) or None)."""
    refs = [f"{r}@{d}" if d else r for r, d in pinned_refs(co, fips, mirror.rstrip("/"), enabled)]
    with ThreadPoolExecutor(8) as pool:
        found = list(pool.map(lambda r: pull_problem(r, login), refs))
    return len(refs), [p for p in found if p]


def pull_problem(ref, login=None):
    """None if `ref` (host/repo:tag, or @sha256:<digest>) can be pulled with `login` ((user, password) or None),
    else why not. The registry's v2 token flow, read-only; an unreachable registry isn't a
    problem here (a proxy may allow the cluster what it denies this machine)."""
    host, rest = ref.split("/", 1)
    repo, tag = split_repo(rest)
    url = f"https://{host}/v2/{repo}/manifests/{tag}"
    accept = ", ".join(["application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json",
                        "application/vnd.docker.distribution.manifest.list.v2+json",
                        "application/vnd.docker.distribution.manifest.v2+json"])
    basic = None
    if login:
        import base64
        basic = "Basic " + base64.b64encode(f"{login[0]}:{login[1]}".encode()).decode()

    def head(headers):
        req = urllib.request.Request(url, method="HEAD", headers={"Accept": accept, **headers})
        try:
            urllib.request.urlopen(req, timeout=20).close()
            return 200, None
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("WWW-Authenticate", "")
    try:
        code, challenge = head({})
        if code == 401 and challenge.lower().startswith("bearer "):
            params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
            q = urllib.parse.urlencode({"service": params.get("service", host),
                                        "scope": f"repository:{repo}:pull"})
            req = urllib.request.Request(f"{params['realm']}?{q}", headers={"Authorization": basic} if basic else {})
            try:
                body = json.load(urllib.request.urlopen(req, timeout=20))
            except urllib.error.HTTPError as e:
                return f"{ref}: {'the registry refused this login' if login else 'needs a login'} (HTTP {e.code})"
            code, _ = head({"Authorization": "Bearer " + (body.get("token") or body.get("access_token", ""))})
        elif code == 401 and basic:
            code, _ = head({"Authorization": basic})
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None
    if code == 200:
        return None
    if code == 404:
        return f"{ref}: not found (not pushed yet, or {'this login' if login else 'no login'} can't see it)"
    return f"{ref}: {'denied with this login' if login else 'needs a login'} (HTTP {code})"
