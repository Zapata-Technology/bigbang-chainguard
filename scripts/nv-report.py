"""NeuVector's vulnerability scans of every image a running Big Bang deployment runs.

  NV_BOOTSTRAP=... NV_PASSWORD=... python scripts/nv-report.py --api https://127.0.0.1:10443 \
      --out DIR [--mode standard|fips] [--overlay overlays/values-chainguard.yaml --overlay flux/kustomization.yaml]
      [--pod-images FILE] [--timeout 1200]

rke2.yml runs it (input `neuvector_report`) against the deployed cluster's controller,
reached through `kubectl -n neuvector port-forward deploy/neuvector-controller-pod 10443`.
It logs in as `admin` with the chart's bootstrap password (NV_BOOTSTRAP, from the
neuvector-bootstrap-secret Secret) and, because NeuVector makes the first login change it,
sets NV_PASSWORD (random per run). Both come from the environment, never argv, and
appear in no output.

Then it turns workload auto-scan on, waits until every running container is scanned, and
fetches each distinct image's report once (GET /v1/scan/workload/<container id>: NeuVector
5.6's REST API, controller/api/apis.yaml). Writes:

  DIR/nv-report.json   per image: reference, digest, tag (from --overlay's pins), origin,
                       base OS, packages NeuVector found, counts by severity, fixable
                       count, and every CVE (package, installed and fixed version, score, link)
  DIR/nv-report.md     a summary table, the scanner's CVE database version and date

NeuVector leaves its own containers out of its workload list: with --pod-images (every
running container's image, from kubectl), each image the workload scan didn't report is
scanned by reference (POST /v1/scan/repository) at its deployed digest, with the pull
credentials in CGR_USERNAME/CGR_PASSWORD (cgr.dev) and BUILD_REGISTRY_USERNAME/_TOKEN.
A failed scan logs NeuVector's reason and is retried at the image's tag (the overlay's pin,
else docs/images.md): NeuVector 5.6 answers a digest with "Image not found". The row says
when the tag's digest is no longer the deployed one.

`origin` is `chainguard` (cgr.dev/<CHAINGUARD_ORG>), `built-here` (BUILD_REGISTRY: built
FROM the org's images) or `other` (RKE2's own images, the storage provisioner). An image
with no packages found is flagged: no CVEs then means NeuVector couldn't read it, not clean.
"""
import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SEVERITIES = ("Critical", "High", "Medium", "Low")


class Api:
    """The controller's REST API on the loopback port-forward (its certificate is self-signed)."""

    def __init__(self, base):
        self.base = base.rstrip("/")
        self.token = None
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE
        self.sleep = time.sleep

    def call(self, method, path, body=None, tries=5, wait=5, timeout=60):
        """One request; a dropped connection (the port-forward) is retried, an HTTP error isn't."""
        for i in range(tries):
            req = urllib.request.Request(self.base + path, method=method,
                                         data=None if body is None else json.dumps(body).encode())
            req.add_header("Content-Type", "application/json")
            if self.token:
                req.add_header("X-Auth-Token", self.token)
            try:
                with urllib.request.urlopen(req, context=self.ctx, timeout=timeout) as r:
                    raw = r.read()
                return json.loads(raw) if raw else {}
            except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
                if isinstance(e, urllib.error.HTTPError) or i == tries - 1:
                    raise
                print(f"{method} {path}: {getattr(e, 'reason', e)}; retrying", file=sys.stderr)
                self.sleep(wait)

    def login(self, bootstrap, password):
        """admin with the bootstrap password; NeuVector asks for a new one on first login."""
        auth = {"username": "admin", "password": bootstrap}
        try:
            data = self.call("POST", "/v1/auth", {"password": auth})
            if not data.get("need_to_reset_password"):
                self.token = data["token"]["token"]
                return
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            # A re-run on the same cluster: the password was already changed.
            try:
                data = self.call("POST", "/v1/auth", {"password": {"username": "admin", "password": password}})
            except urllib.error.HTTPError as e2:
                if e2.code == 401:
                    raise SystemExit("NeuVector refused admin with both NV_BOOTSTRAP and NV_PASSWORD: the "
                                     "password was changed by an earlier run; pass that one as NV_PASSWORD")
                raise
            self.token = data["token"]["token"]
            return
        data = self.call("POST", "/v1/auth", {"password": dict(auth, new_password=password)})
        self.token = data["token"]["token"]

    def logout(self):
        if self.token:
            try:
                self.call("DELETE", "/v1/auth")
            except (urllib.error.URLError, OSError):
                pass


def login(api, bootstrap, password, tries=36, wait=5, sleep=time.sleep):
    """Log in, retrying while the controller's API (or the port-forward to it) comes up."""
    for i in range(tries):
        try:
            return api.login(bootstrap, password)
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            if isinstance(e, urllib.error.HTTPError) or i == tries - 1:
                raise
            print(f"controller API not answering yet ({getattr(e, 'reason', e)}); retrying")
            sleep(wait)


def containers(workloads):
    """Every running container, children included, once each; not the pause containers."""
    seen, out = set(), []

    def walk(items):
        for w in items or []:
            walk(w.get("children"))
            image = w.get("image") or ""
            if w.get("id") in seen or not image or not w.get("running", True):
                continue
            if re.search(r"/pause[:@]", image) or w.get("platform_role") == "pause":
                continue
            seen.add(w["id"])
            out.append(w)
    walk(workloads)
    return out


def scanned(c):
    return (c.get("scan_summary") or {}).get("status") in ("finished", "failed")


def wait_for_scans(api, timeout, poll=20, log=print, clock=time.time, sleep=time.sleep):
    """Auto-scan on, then wait until every running container has a finished (or failed) scan."""
    start = clock()
    api.call("PATCH", "/v1/scan/config", {"config": {"auto_scan": True}})
    nudged = False
    while True:
        cs = containers(api.call("GET", "/v1/workload").get("workloads"))
        left = [c for c in cs if not scanned(c)]
        log(f"scans: {len(cs) - len(left)} of {len(cs)} containers done")
        if cs and not left:
            return cs
        if clock() - start > timeout:
            scanners = api.call("GET", "/v1/scan/scanner").get("scanners") or []
            why = "" if scanners else " (no scanner registered with the controller: is neuvector-scanner-pod running?)"
            raise SystemExit(f"timed out with {len(left)} container(s) unscanned{why}: "
                             + ", ".join(sorted({c['image'] for c in left})[:10]))
        if not nudged and clock() - start > timeout / 2:
            # Auto-scan should have queued them all; ask once more for any it missed.
            for c in left:
                try:
                    api.call("POST", f"/v1/scan/workload/{c['id']}")
                except urllib.error.HTTPError:
                    pass
            nudged = True
        sleep(poll)


def overlay_tags(path):
    """digest -> tag, from the overlay's kustomize image pins (newTag next to a digest)."""
    if not path:
        return {}
    text = open(path, encoding="utf-8").read()
    tags = {}
    for entry in re.split(r"\n\s*- (?=digest:|name:)", text):
        d = re.search(r"digest: (sha256:[0-9a-f]{64})", entry)
        t = re.search(r"newTag: ['\"]?([\w.+-]+)", entry)
        if d and t:
            tags[d.group(1)] = t.group(1)
    return tags


def images_md_tags(path, org):
    """{cgr.dev/<org>/<repo>: tag} from docs/images.md, standard and FIPS columns: the tag of
    an image whose pin carries no newTag (NeuVector's, set by one chart-wide tag)."""
    out = {}
    if not (path and org and os.path.exists(path)):
        return out
    for line in open(path, encoding="utf-8"):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        for cell in cells[2:4] if len(cells) >= 4 else []:
            m = re.fullmatch(r"([a-z0-9][\w.-]*):([\w.+-]+)", cell)
            if m:
                out.setdefault(f"cgr.dev/{org}/{m.group(1)}", m.group(2))
    return out


def origin(image, org, build_registry):
    if org and image.startswith(f"cgr.dev/{org}/"):
        return "chainguard"
    if build_registry and image.startswith(build_registry.rstrip("/") + "/"):
        return "built-here"
    return "other"


def digest_of(c):
    m = re.search(r"(sha256:[0-9a-f]{64})", c.get("image_id") or "") or \
        re.search(r"@(sha256:[0-9a-f]{64})", c.get("image") or "")
    return m.group(1) if m else ""


def summarize(cs, report_of, org="", build_registry="", tags=None):
    """One entry per distinct image (by digest, else reference), from its first container's report."""
    tags, images = tags or {}, {}
    for c in cs:
        key = digest_of(c) or c["image"]
        if key in images:
            images[key]["containers"] += 1
            continue
        summary = c.get("scan_summary") or {}
        images[key] = entry(c["image"], digest_of(c), report_of(c["id"]), summary.get("base_os", ""),
                            summary.get("status", ""), org, build_registry, tags)
    return ordered(images.values())


def ordered(images):
    return sorted(images, key=lambda i: ({"chainguard": 0, "built-here": 1}.get(i["origin"], 2), i["image"]))


def entry(ref, digest, report, base_os, how, org, build_registry, tags):
    """One image's row: counts by severity, fixable count, and every CVE worst first."""
    vulns = sorted(report.get("vulnerabilities") or [],
                   key=lambda v: (SEVERITIES.index(v["severity"]) if v.get("severity") in SEVERITIES else 9,
                                  -(v.get("score_v3") or v.get("score") or 0), v.get("name", "")))
    return {
        "image": re.sub(r"@sha256:[0-9a-f]{64}$", "", ref),
        "digest": digest,
        "tag": tags.get(digest, ""),
        "origin": origin(ref, org, build_registry),
        "base_os": base_os,
        "scan_status": how,
        "packages": len(report.get("modules") or []),
        "containers": 1,
        "deployed": True,
        "counts": {s: sum(1 for v in vulns if v.get("severity") == s) for s in SEVERITIES},
        "fixable": sum(1 for v in vulns if v.get("fixed_version")),
        "vulnerabilities": [{k: v.get(k) for k in ("name", "severity", "score", "score_v3", "package_name",
                                                  "package_version", "fixed_version", "link")}
                            for v in vulns],
    }


def split_ref(ref):
    """registry host, repository, tag-or-digest; Docker Hub's short names made explicit."""
    name, digest = (ref.split("@", 1) + [""])[:2]
    first = name.split("/", 1)[0]
    if "/" not in name or not ("." in first or ":" in first or first == "localhost"):
        name = "docker.io/" + (name if "/" in name else "library/" + name)
    host, repo = name.split("/", 1)
    tag = ""
    if ":" in repo.rsplit("/", 1)[-1]:
        repo, tag = repo.rsplit(":", 1)
    return host, repo, digest or tag or "latest"


def scan_repository(api, req, timeout=900, poll=10, clock=time.time, sleep=time.sleep):
    """POST /v1/scan/repository until it answers: NeuVector says 304 while the scan still runs."""
    start = clock()
    while True:
        try:
            return api.call("POST", "/v1/scan/repository", {"request": req}, tries=2, timeout=300).get("report") or {}
        except urllib.error.HTTPError as e:
            if e.code != 304 or clock() - start > timeout:
                raise
        sleep(poll)


def error_text(e, secret=""):
    """The first 300 characters of an HTTP error's body (NeuVector says why in its JSON
    `message`), with the registry password taken out in case it is echoed back."""
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:
        return ""
    if secret:
        body = body.replace(secret, "***")
    return " ".join(body.split())[:300]


# cgr.dev sometimes answers the first manifest request for a digest with HTTP 503
# "policy evaluation unavailable" (reproduced with NeuVector's standalone scanner and curl,
# 2026-11-23); a request a little later gets 200. NeuVector doesn't retry a 503 and reports
# HTTP 500 "Image not found", so every by-reference scan on RKE2 failed. Retry it.
NOT_FOUND_RETRIES, NOT_FOUND_WAIT = 4, 20


def scan_retrying(api, req, scan, log, secret, sleep, retries=NOT_FOUND_RETRIES, wait=NOT_FOUND_WAIT):
    """scan(api, req), repeated while NeuVector says "Image not found" (a transient registry
    error behind it); raises the last HTTPError, its reason already logged."""
    for attempt in range(retries + 1):
        try:
            return scan(api, req)
        except urllib.error.HTTPError as e:
            text = error_text(e, secret)
            log(f"  failed: HTTP {e.code}: {text}")
            if attempt == retries or e.code != 500 or "Image not found" not in text:
                raise
            log(f"  retrying in {wait} s ({attempt + 1} of {retries}; a registry 503, or the scanner has no egress)")
            sleep(wait)


def repository_scans(api, pod_images, images, creds, org="", build_registry="", tags=None, log=print,
                     scan=scan_repository, repo_tags=None, sleep=time.sleep):
    """Scan, by reference, each image the pods run that the workload scan didn't report.

    NeuVector leaves its own containers out of /v1/workload, so its controller, enforcer,
    manager and scanner images come from here: POST /v1/scan/repository at the deployed
    digest, with the registry's pull credentials (creds: host -> (username, password)).
    """
    tags, out, repo_tags = tags or {}, {}, repo_tags or {}
    seen = {i["digest"] or i["image"] for i in images}
    counts = {}
    for ref in pod_images:
        counts[ref] = counts.get(ref, 0) + 1
    for ref, n in sorted(counts.items()):
        key = ref_key(ref)
        if key in seen:
            continue
        seen.add(key)
        row = scan_ref(api, ref, creds, org, build_registry, tags, repo_tags, log, scan, sleep)
        if row is not None:
            row["containers"] = n
            out[key] = row
    return list(out.values())


def ref_key(ref):
    """An image's identity: its digest when the reference pins one, else the reference."""
    at = split_ref(ref)[2]
    return at if at.startswith("sha256:") else ref


def scan_ref(api, ref, creds, org="", build_registry="", tags=None, repo_tags=None, log=print,
             scan=scan_repository, sleep=time.sleep):
    """One image's row from a by-reference scan, or None for an image from another registry."""
    tags, repo_tags = tags or {}, repo_tags or {}
    host, repo, at = split_ref(ref)
    digest = at if at.startswith("sha256:") else ""
    if origin(ref, org, build_registry) == "other":
        # Only the org's and BUILD_REGISTRY's images: another registry may be one the
        # runner's egress policy blocks, and a blocked pull waits out the whole timeout.
        log(f"not scanned by reference (not a Chainguard or built image): {ref}")
        return None
    user, password = creds.get(host, ("", ""))
    req = {"registry": f"https://{'registry-1.docker.io' if host == 'docker.io' else host}",
           "repository": repo, "tag": at, "username": user, "password": password}
    log(f"repository scan: {host}/{repo}@{at[:19]}")
    how = "repository"
    try:
        report = scan_retrying(api, req, scan, log, password, sleep)
    except urllib.error.HTTPError:
        report = None
        # NeuVector 5.6 answers a digest with HTTP 500 "Image not found" (RKE2 run
        # 45097553250); the same images scanned by tag on another cluster. The tag may
        # have moved since (a rebuild), so the row says which digest NeuVector scanned.
        # NeuVector's own pins carry no newTag: their tag comes from docs/images.md.
        tag = tags.get(digest) or repo_tags.get(f"{host}/{repo}")
        if digest and tag:
            log(f"  retrying by tag {tag}")
            try:
                report = scan_retrying(api, dict(req, tag=tag), scan, log, password, sleep, retries=1)
                scanned = report.get("digest", "")
                how = "repository (by tag)" if scanned in ("", digest) else \
                    f"repository (by tag; scanned {scanned[:19]}, not the deployed digest)"
            except urllib.error.HTTPError:
                log("  failed again")
    return entry(ref, digest or (report or {}).get("digest", ""), report or {},
                 (report or {}).get("base_os", ""), how if report is not None else "failed",
                 org, build_registry, tags)


def optional_scans(api, refs, images, creds, org="", build_registry="", tags=None, repo_tags=None, jobs=3,
                   log=print, scan=scan_repository, sleep=time.sleep):
    """Rows for the pinned images the deployment doesn't run (Big Bang's optional packages),
    each scanned by reference at its pinned digest, `jobs` at a time, marked deployed: false.

    One NeuVector scans what a site would run with those packages on, without the cluster
    that would take: the scan reads the image, not a container.
    """
    seen = {i["digest"] or i["image"] for i in images}
    todo = []
    for ref in sorted(set(refs)):
        if ref_key(ref) not in seen:
            seen.add(ref_key(ref))
            todo.append(ref)
    log(f"{len(todo)} pinned image(s) the deployment doesn't run: scanning each by reference, {jobs} at a time")
    with ThreadPoolExecutor(max(1, jobs)) as pool:
        rows = list(pool.map(lambda ref: scan_ref(api, ref, creds, org, build_registry, tags, repo_tags, log,
                                                  scan, sleep), todo))
    out = []
    for row in rows:
        if row is not None:
            row["containers"], row["deployed"] = 0, False
            out.append(row)
    return out


def markdown(result):
    db, every = result["cvedb"], result["images"]
    rows = [r for r in every if r.get("deployed", True)]
    lines = [f"# NeuVector scans: Big Bang {result['bigbang']} ({result['mode']})", "",
             f"NeuVector CVE database {db.get('version', '?')}, built {db.get('created', '?')}."
             f" {len(rows)} distinct images from {sum(r['containers'] for r in rows)} running containers.",
             f" Run: {result['run']}" if result.get("run") else "", ""]
    lines += table(rows)
    by = {}
    for r in rows:
        t = by.setdefault(r["origin"], dict.fromkeys(SEVERITIES, 0))
        for s in SEVERITIES:
            t[s] += r["counts"][s]
    lines += ["", "Totals by origin: " + "; ".join(
        f"{o}: " + ", ".join(f"{t[s]} {s}" for s in SEVERITIES) for o, t in sorted(by.items())), ""]
    optional = [r for r in every if not r.get("deployed", True)]
    if optional:
        lines += [f"## Not deployed: {len(optional)} pinned images of Big Bang's optional packages, scanned by reference", ""]
        lines += table(optional)
        lines += ["", "Totals: " + ", ".join(f"{sum(r['counts'][s] for r in optional)} {s}" for s in SEVERITIES), ""]
    return "\n".join(line for line in lines if line is not None)


def table(rows):
    lines = ["| Origin | Image | Tag | Critical | High | Medium | Low | Fixable | Packages | Base OS |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        c = r["counts"]
        flag = " (none found: not read)" if r["packages"] == 0 else ""
        if r.get("scan_status") == "repository":
            flag += " (scanned by reference)"
        elif str(r.get("scan_status", "")).startswith("repository (by tag"):
            flag += " (scanned by tag" + ("; not the deployed digest)" if "not the deployed" in r["scan_status"] else ")")
        elif r.get("scan_status") == "failed":
            flag = " (scan failed)"
        lines.append(f"| {r['origin']} | `{r['image']}` | {r['tag'] or r['digest'][7:19]} | {c['Critical']} | {c['High']} "
                     f"| {c['Medium']} | {c['Low']} | {r['fixable']} | {r['packages']}{flag} | {r['base_os']} |")
    return lines


def registry_creds():
    """host -> (username, password), from CGR_USERNAME/CGR_PASSWORD and BUILD_REGISTRY_USERNAME/_TOKEN."""
    creds = {}
    if os.environ.get("CGR_PASSWORD"):
        creds["cgr.dev"] = (os.environ.get("CGR_USERNAME", "_token"), os.environ["CGR_PASSWORD"])
    build = os.environ.get("BUILD_REGISTRY", "")
    if build and os.environ.get("BUILD_REGISTRY_TOKEN"):
        creds[build.split("/", 1)[0]] = (os.environ.get("BUILD_REGISTRY_USERNAME", "token"), os.environ["BUILD_REGISTRY_TOKEN"])
    return creds


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--api", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", default="standard")
    ap.add_argument("--overlay", action="append", default=[],
                    help="a file with the cluster's image pins (overlay, flux kustomization), for each digest's tag")
    ap.add_argument("--pod-images", help="a file: every running container's image reference, one per line "
                                         "(kubectl); those the workload scan missed are scanned by reference")
    ap.add_argument("--timeout", type=int, default=1200, help="seconds to wait for the scans")
    ap.add_argument("--optional-images", help="a file: every pinned image reference, one per line "
                                              "(security-report.py pins); those no pod runs are scanned by "
                                              "reference and reported as not deployed")
    ap.add_argument("--jobs", type=int, default=3, help="by-reference scans of --optional-images at once")
    a = ap.parse_args()
    bootstrap, password = os.environ.get("NV_BOOTSTRAP"), os.environ.get("NV_PASSWORD")
    if not bootstrap or not password:
        raise SystemExit("NV_BOOTSTRAP and NV_PASSWORD must be set")
    api = Api(a.api)
    login(api, bootstrap, password)
    try:
        cs = wait_for_scans(api, a.timeout)
        scanners = api.call("GET", "/v1/scan/scanner").get("scanners") or [{}]
        org, build = os.environ.get("CHAINGUARD_ORG", ""), os.environ.get("BUILD_REGISTRY", "")
        tags = {}
        for path in a.overlay:
            tags.update(overlay_tags(path))
        images = summarize(cs, lambda cid: api.call("GET", f"/v1/scan/workload/{cid}").get("report") or {},
                           org, build, tags)
        if a.pod_images:
            refs = [line.strip() for line in open(a.pod_images, encoding="utf-8") if line.strip()]
            md = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "images.md")
            images = ordered(images + repository_scans(api, refs, images, registry_creds(), org, build, tags,
                                                       repo_tags=images_md_tags(md, org)))
        if a.optional_images:
            md = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "images.md")
            refs = [line.strip() for line in open(a.optional_images, encoding="utf-8") if line.strip()]
            images = ordered(images + optional_scans(api, refs, images, registry_creds(), org, build, tags,
                                                     images_md_tags(md, org), a.jobs))
    finally:
        api.logout()
    run = ""
    if os.environ.get("GITHUB_RUN_ID"):
        run = f"{os.environ.get('GITHUB_SERVER_URL')}/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    bigbang = open(os.path.join(os.path.dirname(__file__), "..", "BIGBANG_VERSION"), encoding="utf-8").read().strip()
    result = {"bigbang": bigbang, "mode": a.mode, "run": run,
              "cvedb": {"version": scanners[0].get("cvedb_version"), "created": scanners[0].get("cvedb_create_time")},
              "images": images}
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "nv-report.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    md = markdown(result)
    with open(os.path.join(a.out, "nv-report.md"), "w", encoding="utf-8") as f:
        f.write(md)
    print(md)
    unread = [r["image"] for r in images if r["packages"] == 0]
    if unread:
        print(f"NOTE: NeuVector found no packages in {len(unread)} image(s): " + ", ".join(unread), file=sys.stderr)


if __name__ == "__main__":
    main()
