"""The images an install deploys, and a read-only pull test of each with the site's logins."""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

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

    def walk(node):
        if isinstance(node, dict):
            repo, tag = node.get("repository"), node.get("tag")
            if isinstance(repo, str) and tag is not None:
                full = f"{node['registry']}/{repo}" if isinstance(node.get("registry"), str) else repo
                if prefix and full.startswith(prefix + "/"):
                    refs.add(f"{full}:{tag}")
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str) and prefix and re.fullmatch(re.escape(prefix) + r"/[\w./-]+:[\w.-]+", node):
            refs.add(node)
    values = merged_values({}, co.overlays(fips))   # what deploys: FIPS replaces the standard refs
    if enabled is not None:
        values = {k: v for k, v in (values.get("packages") or {}).items() if f"packages.{k}" in enabled}
    walk(values)
    return sorted(refs)


def pull_problem(ref, login=None):
    """None if `ref` (host/repo:tag) can be pulled with `login` ((user, password) or None),
    else why not. The registry's v2 token flow, read-only; an unreachable registry isn't a
    problem here (a proxy may allow the cluster what it denies this machine)."""
    host, rest = ref.split("/", 1)
    repo, tag = rest.rsplit(":", 1)
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
