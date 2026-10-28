"""Tag lists for repositories in the Chainguard org, read from the registry API.

Credentials, in order:
  CGR_USERNAME / CGR_PASSWORD   a Chainguard pull token (CI)
  chainctl auth token           an interactive chainctl login (laptop)
Tags are cached in a JSON file so one run asks the registry once per repo.

The org is, in order: --org, $CHAINGUARD_ORG, the CHAINGUARD_ORG file at the repo root.
"""
import base64
import json
import os
import re
import subprocess
import urllib.error
import urllib.request

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
                             "`chainctl auth login --audience https://console-api.enforce.dev,cgr.dev`")
        user, pw = "_token", p.stdout.strip()
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


class OrgTags:
    def __init__(self, org, cache=None, offline=False):
        self.org = org
        self.offline = offline
        self.cache_path = cache
        cached = json.load(open(cache)) if cache and os.path.exists(cache) else {}
        # A cache written for another org is useless: its tag lists are not this org's.
        self.cache = cached.get("tags", {}) if cached.get("org") == org else {}
        self._auth = None

    def _token(self, repo):
        if self._auth is None:
            self._auth = _basic_auth()
        url = f"https://{REGISTRY}/token?scope=repository:{self.org}/{repo}:pull&service={REGISTRY}"
        req = urllib.request.Request(url, headers={"Authorization": self._auth})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["token"]

    def digest(self, repo, tag):
        """Manifest digest of <org>/<repo>:<tag> (the multi-arch index when there is one)."""
        accept = ", ".join(["application/vnd.oci.image.index.v1+json",
                            "application/vnd.docker.distribution.manifest.list.v2+json",
                            "application/vnd.oci.image.manifest.v1+json",
                            "application/vnd.docker.distribution.manifest.v2+json"])
        req = urllib.request.Request(f"https://{REGISTRY}/v2/{self.org}/{repo}/manifests/{tag}", method="HEAD",
                                     headers={"Authorization": f"Bearer {self._token(repo)}", "Accept": accept})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.headers["Docker-Content-Digest"]

    def tags(self, repo):
        """Every tag of <org>/<repo>, or None if the org has no such repository."""
        if repo in self.cache:
            return self.cache[repo]
        if self.offline:
            return None
        tags = []
        try:
            token = self._token(repo)
            url = f"https://{REGISTRY}/v2/{self.org}/{repo}/tags/list?n=1000"
            while url:
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    tags += json.load(r).get("tags") or []
                    link = r.headers.get("Link", "")
                m = re.search(r"<([^>]+)>", link)
                url = (f"https://{REGISTRY}{m.group(1)}" if m.group(1).startswith("/") else m.group(1)) if m else None
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403, 404):
                raise
            tags = None
        self.cache[repo] = tags
        return tags

    def save(self):
        if self.cache_path:
            with open(self.cache_path, "w", newline="\n") as f:
                json.dump({"org": self.org, "tags": self.cache}, f, indent=1, sort_keys=True)
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


def resolve(ib_tag, tags, suffix=None):
    """Pick the org tag for an Iron Bank tag.

    If the org has nothing on that MAJOR.MINOR line, the newest release of the same
    MAJOR above it is used and the note starts with "MINOR AHEAD".

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
    if ahead:
        n, t = max(ahead, key=lambda x: (x[0], not x[1].startswith("v")))
        return t, f"MINOR AHEAD: org has no {'.'.join(map(str, line))}.x; using {t} (Iron Bank {ib_tag})"
    return None, f"no {nums[0]}.x tag at or above {'.'.join(map(str, line))} in org (Iron Bank {ib_tag})"
