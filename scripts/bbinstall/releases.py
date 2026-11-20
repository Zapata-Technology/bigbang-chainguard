"""upgrade --to and --list: this repository's published releases (<Big Bang>-cg.<n>), the one
the cluster runs, and a release's archive fetched, checked and extracted so its own
installer can run the upgrade.

A release is a fixed set of overlays, digests and installer that CI tested together, so
`upgrade --to <tag>` hands the upgrade to that release's bb-install.py (with the same
flags) rather than pointing this checkout's installer at its overlays: a newer release can
need installer changes this checkout doesn't have (the Big Bang 4 site file conversion
did)."""
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.request

from bbcluster import kubectl
from bbcommon import ROOT
from bbinstall.common import NAMESPACE, STATE_SECRET, say
from release import REPO_FILE, github_remote, release_identity

API = "https://api.github.com/repos/{repo}/releases?per_page=100&page={page}"
DOWNLOAD = "https://github.com/{repo}/releases/download/{tag}/{name}"
CACHE = os.path.join(ROOT, "work", "releases")
TAG = re.compile(r"^(\d+)\.(\d+)\.(\d+)-cg\.(\d+)$")
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
# upgrade's own options, not passed on to the release's installer: (option, takes a value)
OWN_OPTIONS = {"--to": True, "--allow-downgrade": False, "--list": False, "--repo": True, "--overlays": True,
               "--allow-unverified": False}


def tag_key(tag):
    """(major, minor, patch, n) of a release tag, or None for any other tag."""
    m = TAG.match(tag or "")
    return tuple(int(x) for x in m.groups()) if m else None


def bigbang_of(tag):
    return tag.split("-cg.", 1)[0]


def version_key(version):
    return tuple(int(x) for x in re.findall(r"\d+", str(version))[:3])


def archive_repo(root=ROOT):
    """OWNER/REPO a release archive says it came from (its RELEASE_REPO file), or None."""
    path = os.path.join(root, REPO_FILE)
    text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    m = re.search(r"^repo:\s*([\w.-]+/[\w.-]+)\s*$", text, re.M)
    return m.group(1) if m else None


def release_repo(root=ROOT):
    """OWNER/REPO the releases come from: the repository a release archive was released from,
    else this checkout's GitHub remote (a fork releases its own). Neither (an archive made
    before RELEASE_REPO existed, a copy without .git): --repo names it, rather than a guess."""
    repo = archive_repo(root) or github_remote(root)
    if not repo:
        raise SystemExit("which repository publishes the releases? This copy has no RELEASE_REPO file and no "
                         "GitHub remote: pass --repo OWNER/REPO")
    return repo


def _get_json(url):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "bb-install"}
    # A token only lifts the anonymous rate limit (60 an hour); it goes to api.github.com only.
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
        return json.load(r)


def list_releases(repo, get=_get_json):
    """The repository's published release tags, newest first (drafts, pre-releases and
    other tags left out)."""
    tags, page = [], 1
    while True:
        try:
            batch = get(API.format(repo=repo, page=page))
        except (OSError, ValueError) as e:
            raise SystemExit(f"could not list {repo}'s releases on GitHub ({e}); --repo OWNER/REPO names another "
                             "repository, or download and verify a release yourself (docs/quickstart.md)")
        tags += [r["tag_name"] for r in batch if not r.get("draft") and not r.get("prerelease")
                 and tag_key(r.get("tag_name"))]
        if len(batch) < 100:
            break
        page += 1
    return sorted(set(tags), key=tag_key, reverse=True)


def resolve(spec, tags):
    """The tag `--to` names: `latest` (the newest release), a Big Bang version (its newest
    <version>-cg.<n>) or an exact tag."""
    if not tags:
        raise SystemExit("no published releases found")
    if spec == "latest":
        return tags[0]
    if spec in tags:
        return spec
    if re.fullmatch(r"\d+\.\d+\.\d+", spec or ""):
        for t in tags:   # newest first
            if bigbang_of(t) == spec:
                return t
        versions = sorted({bigbang_of(t) for t in tags}, key=version_key, reverse=True)
        raise SystemExit(f"no release deploys Big Bang {spec}; released versions: {', '.join(versions[:8])}")
    raise SystemExit(f"{spec} isn't a published release: use a tag such as {tags[0]}, a Big Bang version "
                     f"such as {bigbang_of(tags[0])}, or latest (upgrade --list shows them)")


def recorded_release():
    """The release tag install recorded in the bb-install Secret, or None. Only that one key
    is read (the Secret also holds credentials, which must never reach output)."""
    out = kubectl("-n", NAMESPACE, "get", "secret", STATE_SECRET, "-o", "jsonpath={.data.release}", check=False)
    try:
        key = tag_key(base64.b64decode(out or "").decode())
    except ValueError:
        return None
    # Rebuilt from the parsed numbers: nothing but a release tag comes out of the Secret.
    return "{}.{}.{}-cg.{}".format(*key) if key else None


def running_release(tag, deployed_version):
    """The release tag the cluster runs: the one the installer recorded, trusted only while
    Big Bang's chart is still that release's version (a rollback or an install from a
    checkout moves it). None when unknown."""
    if tag_key(tag) and deployed_version and bigbang_of(tag) == deployed_version:
        return tag
    return None


def downgrade_problem(target, running_tag, running_bigbang):
    """Why moving to `target` is a downgrade, or None. Without a recorded release only the
    Big Bang version can be compared."""
    if running_tag and tag_key(target) < tag_key(running_tag):
        return f"the cluster runs release {running_tag}; {target} is older"
    if running_bigbang and version_key(bigbang_of(target)) < version_key(running_bigbang):
        return f"the cluster runs Big Bang {running_bigbang}; {target} deploys the older {bigbang_of(target)}"
    return None


def newer_releases(tags, running_tag, running_bigbang):
    """Releases an upgrade can move to: newer than the recorded release, else every release of
    the running Big Bang version or newer (which revision runs isn't known), else all."""
    if running_tag:
        return [t for t in tags if tag_key(t) > tag_key(running_tag)]
    if running_bigbang:
        return [t for t in tags if version_key(bigbang_of(t)) >= version_key(running_bigbang)]
    return list(tags)


def _download(url, path):
    error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "bb-install"}),
                                        timeout=120) as r, open(path, "wb") as f:
                shutil.copyfileobj(r, f)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            error = e
        except OSError as e:
            error = e
        time.sleep(5 * (attempt + 1))
    raise SystemExit(f"could not download {url} ({error})")


def verify_archive(repo, archive, which=shutil.which, run=subprocess.run, allow_unverified=False):
    """(verified, note). The release's sha256 file must match, and its Sigstore bundle must
    verify with cosign as the repository's release workflow on main (docs/quickstart.md's
    check). A bad digest or signature always stops the upgrade. A missing sha256, bundle or
    cosign stops it too, unless allow_unverified (--allow-unverified): then the note says
    NOT VERIFIED, because the code about to run is unchecked."""
    sums, bundle, name = archive + ".sha256", archive + ".sigstore.json", os.path.basename(archive)
    missing = []
    if os.path.exists(sums):
        want = open(sums, encoding="utf-8").read().split()[0]
        got = hashlib.sha256(open(archive, "rb").read()).hexdigest()
        if want != got:
            raise SystemExit(f"{name}'s sha256 is {got}, the release says {want}: not using it")
    else:
        missing.append("the release has no sha256 file")
    if not os.path.exists(bundle):
        missing.append("the release has no Sigstore signature")
    elif not which("cosign"):
        missing.append("cosign isn't installed, so the signature can't be checked")
    if missing:
        why = "; ".join(missing)
        if not allow_unverified:
            raise SystemExit(f"{name} can't be verified ({why}): not running its installer. Install cosign "
                             "(docs/quickstart.md), or pass --allow-unverified to run it anyway")
        return False, f"NOT VERIFIED: {why}"
    p = run(["cosign", "verify-blob", "--bundle", bundle, "--certificate-oidc-issuer", OIDC_ISSUER,
             "--certificate-identity-regexp", release_identity(repo), archive], capture_output=True, text=True)
    if p.returncode:
        raise SystemExit(f"{os.path.basename(archive)}'s signature doesn't verify as {repo}'s release workflow; "
                         f"not using it:\n{(p.stderr or p.stdout)[-600:]}")
    return True, f"signature verified: built by {repo}'s release workflow on main"


def fetch_release(repo, tag, cache=CACHE, download=_download, which=shutil.which, run=subprocess.run,
                  allow_unverified=False):
    """Download release `tag`'s archive with its sha256 and signature into cache/<tag>,
    verify it (verify_archive) and extract it. Returns (the release's directory, verified,
    note)."""
    dest = os.path.join(cache, tag)
    shutil.rmtree(dest, ignore_errors=True)    # always fresh: a cached tree could have been edited
    os.makedirs(dest)
    name = f"bigbang-chainguard-{tag}.tar.gz"
    archive = os.path.join(dest, name)
    say(f"downloading release {tag} from github.com/{repo}")
    if not download(DOWNLOAD.format(repo=repo, tag=tag, name=name), archive):
        raise SystemExit(f"release {tag} has no {name}")
    for suffix in (".sha256", ".sigstore.json"):
        download(DOWNLOAD.format(repo=repo, tag=tag, name=name + suffix), archive + suffix)
    verified, note = verify_archive(repo, archive, which, run, allow_unverified)
    with tarfile.open(archive) as t:
        t.extractall(dest, filter="data")     # no absolute paths, links out or devices
    root = os.path.join(dest, f"bigbang-chainguard-{tag}")
    release = os.path.join(root, "RELEASE")
    if not os.path.exists(os.path.join(root, "scripts", "bb-install.py")) or not os.path.exists(release) \
            or open(release, encoding="utf-8").read().strip() != tag:
        raise SystemExit(f"{name} isn't release {tag}'s archive (no bigbang-chainguard-{tag}/RELEASE saying so)")
    return root, verified, note


def handoff_args(argv):
    """The command line for the release's installer: this one's, less upgrade's own options."""
    out, skip = [], False
    for arg in argv:
        if skip:
            skip = False
            continue
        opt = arg.split("=", 1)[0]
        if opt in OWN_OPTIONS:
            skip = OWN_OPTIONS[opt] and "=" not in arg
            continue
        out.append(arg)
    return out


def unknown_options(args, installer_text):
    """Options the release's installer doesn't have (it may be older than this one)."""
    return [a.split("=", 1)[0] for a in args if a.startswith("--") and a.split("=", 1)[0] not in installer_text]


def record_release(tag):
    """Note in the bb-install Secret which release the cluster now runs (upgrade --list and
    the downgrade check read it); a release's installer older than this one doesn't."""
    kubectl("-n", NAMESPACE, "patch", "secret", STATE_SECRET, "--type", "merge",
            "-p", json.dumps({"stringData": {"release": tag}}), check=False)


def forget_release():
    """After a rollback the recorded release is no longer what runs."""
    kubectl("-n", NAMESPACE, "patch", "secret", STATE_SECRET, "--type", "json",
            "-p", json.dumps([{"op": "remove", "path": "/data/release"}]), check=False)
