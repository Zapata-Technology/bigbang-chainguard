"""Tag lists for repositories in the Chainguard org, read from the registry API.

Credentials, in order:
  CGR_USERNAME / CGR_PASSWORD   a Chainguard pull token (CI)
  chainctl auth token           an interactive chainctl login (laptop)
Tags are cached in a JSON file so one run asks the registry once per repo; prefetch()
fills that cache for many repos and digests at once, in parallel.

The org is, in order: --org, $CHAINGUARD_ORG, the CHAINGUARD_ORG file at the repo root.
"""
import base64
import http.client
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from bbcommon import setting

REGISTRY = "cgr.dev"


def org_name(arg=None):
    """The Chainguard org: --org, else $CHAINGUARD_ORG, else the CHAINGUARD_ORG file."""
    org = setting("CHAINGUARD_ORG", arg).removeprefix(f"{REGISTRY}/").strip("/")
    if not org:
        raise SystemExit("No Chainguard org: pass --org, set CHAINGUARD_ORG, or fill the CHAINGUARD_ORG file")
    return org


def _basic_auth():
    user, pw = os.environ.get("CGR_USERNAME"), os.environ.get("CGR_PASSWORD")
    if not (user and pw):
        p = subprocess.run(["chainctl", "auth", "token", "--audience", REGISTRY],
                           capture_output=True, text=True)
        if p.returncode or not p.stdout.strip():
            raise SystemExit("No registry credentials: set CGR_USERNAME/CGR_PASSWORD or run "
                             "`chainctl auth login --audience cgr.dev`")
        user, pw = "_token", p.stdout.strip()
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def _open(req, timeout, attempts=4):
    """urlopen with retries for timeouts, connection errors, 429 and 5xx (not other 4xx)."""
    for i in range(attempts):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500 or i == attempts - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if i == attempts - 1:
                raise
        time.sleep(5 * (i + 1))


def _read_json(req, timeout, attempts=4):
    """(JSON body, headers) of a request, retrying a body cut off mid-read as _open retries
    a failed request (cgr.dev drops chunked tag lists now and then)."""
    for i in range(attempts):
        try:
            with _open(req, timeout) as r:
                return json.load(r), r.headers
        except (http.client.IncompleteRead, ConnectionError, TimeoutError):
            if i == attempts - 1:
                raise
            time.sleep(5 * (i + 1))


class _KeepRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None  # surface the 3xx as an HTTPError, Location and all


_NO_REDIRECT = urllib.request.build_opener(_KeepRedirect)


class OrgTags:
    def __init__(self, org, cache=None, offline=False):
        self.org = org
        self.offline = offline
        self.cache_path = cache
        cached = json.load(open(cache)) if cache and os.path.exists(cache) else {}
        # A cache written for another org is useless: its tag lists are not this org's.
        self.cache = cached.get("tags", {}) if cached.get("org") == org else {}
        self.digests = cached.get("digests", {}) if cached.get("org") == org else {}
        self._auth = None
        self._auth_lock = threading.Lock()   # callers may run in threads (bb-age.py, prefetch)
        # A set while a dry run collects the digests it would pin (bb-convert's prefetch):
        # pinned() then records each uncached (repo, tag) instead of asking the registry.
        self.record_digests = None

    def _token(self, repo):
        with self._auth_lock:
            if self._auth is None:
                self._auth = _basic_auth()
        url = f"https://{REGISTRY}/token?scope=repository:{self.org}/{repo}:pull&service={REGISTRY}"
        req = urllib.request.Request(url, headers={"Authorization": self._auth})
        with _open(req, 30) as r:
            return json.load(r)["token"]

    def pinned(self, repo, tag):
        """digest() through the cache; None offline when it isn't cached."""
        key = f"{repo}:{tag}"
        if key not in self.digests:
            if self.offline:
                return None
            if self.record_digests is not None:
                self.record_digests.add((repo, tag))
                return None
            self.digests[key] = self.digest(repo, tag)
        return self.digests[key]

    def prefetch(self, repos=(), refs=(), jobs=8):
        """Fill the cache with the tags of `repos` and the digests of `refs` ((repo, tag)),
        `jobs` requests at a time; tags() and pinned() then answer from it. A lookup that
        fails here is left out, so the caller asks again (and sees the error) only if it
        needs that one."""
        if self.offline:
            return
        repos = sorted({r for r in repos if r not in self.cache})
        refs = sorted({r for r in refs if f"{r[0]}:{r[1]}" not in self.digests})

        def quietly(fn, *args):
            try:
                return True, fn(*args)
            except Exception:   # noqa: BLE001 - asked again, and raised, by the caller that needs it
                return False, None
        with ThreadPoolExecutor(jobs) as pool:
            for repo, (ok, tags) in zip(repos, pool.map(lambda r: quietly(self._fetch_tags, r), repos)):
                if ok:
                    self.cache[repo] = tags
            for (repo, tag), (ok, d) in zip(refs, pool.map(lambda r: quietly(self.digest, *r), refs)):
                if ok:
                    self.digests[f"{repo}:{tag}"] = d

    def digest(self, repo, tag):
        """Manifest digest of <org>/<repo>:<tag> (the multi-arch index when there is one)."""
        accept = ", ".join(["application/vnd.oci.image.index.v1+json",
                            "application/vnd.docker.distribution.manifest.list.v2+json",
                            "application/vnd.oci.image.manifest.v1+json",
                            "application/vnd.docker.distribution.manifest.v2+json"])
        req = urllib.request.Request(f"https://{REGISTRY}/v2/{self.org}/{repo}/manifests/{tag}", method="HEAD",
                                     headers={"Authorization": f"Bearer {self._token(repo)}", "Accept": accept})
        with _open(req, 30) as r:
            return r.headers["Docker-Content-Digest"]

    def created(self, repo, tag):
        """Build time (the config's `created`, ISO 8601) of <org>/<repo>:<tag>, linux/amd64."""
        token = self._token(repo)

        def get(path, accept):
            req = urllib.request.Request(f"https://{REGISTRY}/v2/{self.org}/{repo}/{path}",
                                         headers={"Authorization": f"Bearer {token}", "Accept": accept})
            return _read_json(req, 60)[0]
        doc = get(f"manifests/{tag}", ", ".join(["application/vnd.oci.image.index.v1+json",
                                                 "application/vnd.oci.image.manifest.v1+json"]))
        if "manifests" in doc:
            amd = next(m for m in doc["manifests"] if m.get("platform", {}).get("architecture") == "amd64")
            doc = get(f"manifests/{amd['digest']}", "application/vnd.oci.image.manifest.v1+json")
        # Blobs redirect to presigned storage URLs (Cloudflare R2), which refuse a second
        # Authorization header: follow the redirect without it.
        req = urllib.request.Request(f"https://{REGISTRY}/v2/{self.org}/{repo}/blobs/{doc['config']['digest']}",
                                     headers={"Authorization": f"Bearer {token}"})
        try:
            with _NO_REDIRECT.open(req, timeout=60) as r:
                return json.load(r).get("created")
        except urllib.error.HTTPError as e:
            if e.code not in (301, 302, 303, 307, 308):
                raise
            return _read_json(urllib.request.Request(e.headers["Location"]), 60)[0].get("created")

    def tags(self, repo):
        """Every tag of <org>/<repo>, or None if the org has no such repository."""
        if repo in self.cache:
            return self.cache[repo]
        if self.offline:
            return None
        self.cache[repo] = self._fetch_tags(repo)
        return self.cache[repo]

    def _fetch_tags(self, repo):
        """tags() from the registry, without the cache."""
        tags = []
        try:
            token = self._token(repo)
            url = f"https://{REGISTRY}/v2/{self.org}/{repo}/tags/list?n=1000"
            while url:
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
                body, headers = _read_json(req, 60)
                tags += body.get("tags") or []
                link = headers.get("Link", "")
                m = re.search(r"<([^>]+)>", link)
                url = (f"https://{REGISTRY}{m.group(1)}" if m.group(1).startswith("/") else m.group(1)) if m else None
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403, 404):
                raise
            tags = None
        return tags

    def save(self):
        if self.cache_path:
            with open(self.cache_path, "w", newline="\n") as f:
                json.dump({"org": self.org, "tags": self.cache, "digests": self.digests}, f, indent=1,
                          sort_keys=True)
                f.write("\n")


_VER = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?(?:-(.+))?$")


def parse(tag):
    """'v1.20.3' -> ((1, 20, 3), None); '8.10.2-slim' -> ((8, 10, 2), 'slim')."""
    m = _VER.match(str(tag))
    if not m:
        return None
    nums = tuple(int(x) for x in m.groups()[:4] if x is not None)
    return nums, m.group(5)


# Iron Bank tag suffixes that say nothing about the version.
_IB_SUFFIXES = re.compile(r"-(amd64|arm64|ubi\d*|ubi-fips|fips)$")


def resolve(ib_tag, tags, suffix=None, allow_ahead=True):
    """Pick the org tag for an Iron Bank tag.

    If the org has nothing on that MAJOR.MINOR line, the newest release of the same
    MAJOR above it is used and the note starts with "MINOR AHEAD" (unless
    allow_ahead is False: then the image is unresolved).

    The newest patch on the Iron Bank tag's MAJOR.MINOR line (the helm-charts pin
    policy: Chainguard rebuilds patches for CVEs, so the newest patch is the one to
    run). A MAJOR.MINOR-only Iron Bank tag (kubectl:v1.37) takes the org's stream
    tag of that line when it has one. "*" takes the newest release. Returns
    (tag, note) or (None, reason). `suffix` selects a variant such as "slim".
    """
    if str(ib_tag) == "*":
        best = max(((p[0], t) for t in tags or [] if (p := parse(t)) and p[1] == suffix), default=None)
        return (best[1], None) if best else (None, "org has no release tags")
    want = parse(_IB_SUFFIXES.sub("", str(ib_tag)))
    if not want:
        m = re.search(r"v?(\d+(?:\.\d+)+)", str(ib_tag))  # ubi-fips-v19.2.2 -> 19.2.2
        want = parse(m.group(1)) if m else None
    if not want:
        return None, f"cannot read version from Iron Bank tag {ib_tag!r}"
    nums, _ = want
    line = nums[:2]
    candidates = [(p[0], t) for t in tags or [] if (p := parse(t)) and p[1] == suffix]
    if len(nums) == 2:
        stream = [t for n, t in candidates if n == nums]
        if stream:
            return sorted(stream, key=lambda t: t.startswith("v"))[0], None
    patches = [(n, t) for n, t in candidates if n[:2] == line and len(n) >= 3 and n >= nums]
    if patches:
        n, t = max(patches, key=lambda x: (x[0], not x[1].startswith("v")))
        return t, (None if n == nums else f"Iron Bank {ib_tag} -> newest {'.'.join(map(str, line))} patch")
    older = [(n, t) for n, t in candidates if n[:2] == line and len(n) >= 3]
    if older:
        n, t = max(older)
        return t, f"org's newest {'.'.join(map(str, line))} patch {t} is older than Iron Bank {ib_tag}"
    # The org syncs only the current release stream, so Big Bang can pin a line
    # the org no longer has. Take the newest release of the same MAJOR and flag it.
    ahead = [(n, t) for n, t in candidates if n[0] == nums[0] and n[:2] > line and len(n) >= 3]
    if ahead and not allow_ahead:
        return None, f"org has no {'.'.join(map(str, line))}.x (--strict-versions; newer {nums[0]}.x exists)"
    if ahead:
        n, t = max(ahead, key=lambda x: (x[0], not x[1].startswith("v")))
        return t, f"MINOR AHEAD: org has no {'.'.join(map(str, line))}.x; using {t} (Iron Bank {ib_tag})"
    return None, f"no {nums[0]}.x tag at or above {'.'.join(map(str, line))} in org (Iron Bank {ib_tag})"
