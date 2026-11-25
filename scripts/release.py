#!/usr/bin/env python3
"""Cut a versioned release: the tag, its notes and the archive users install from.

  release.py [--dry-run] [--repo OWNER/REPO] [--require-verify | --after-ci]

Tags are <Big Bang version>-cg.<n>: n counts this repo's releases for that Big Bang version
(3.35.1-cg.1, 3.35.1-cg.2, ... then 3.36.0-cg.1), so a tag says which Big Bang it deploys
and which revision of the overlays. The release holds bigbang-chainguard-<tag>.tar.gz (the
checkout at the tagged commit, with RELEASE set to the tag so bb-install.py can report
it, and RELEASE_REPO naming the repository it was released from), its sha256 and, with
cosign on the PATH (release.yml), a keyless Sigstore signature bundle tied to this
repository's release workflow on main. The notes say how to download, verify, check and
install it, link docs/upgrades/<Big Bang version>.md when there is one, and list the Big
Bang version, the org, and the image changes since the previous release.

release.yml runs this with --after-ci each time CI finishes on a push to main: it cuts a
release only when CI's `verify` passed on that commit and a DEPLOYABLE file changed since the
previous release (otherwise it says why and stops, exit 0). A manual run (workflow_dispatch)
passes --require-verify: it releases main's HEAD whatever changed, but still only after
`verify` passed on it (otherwise exit 1). The repository is --repo, else $GITHUB_REPOSITORY,
else the checkout's GitHub remote. Needs git (with tags fetched) and gh.
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bbcommon import ROOT  # noqa: E402

OVERLAY = "overlays/values-chainguard.yaml"
# What a site deploys or runs. A change to anything else (docs, tests, CI, other scripts)
# deploys nothing new, so it cuts no release. release.yml runs after CI, and workflow_run
# has no path filter, so the list lives here. `dir/**` is everything under dir/; anything
# else is one file. The scripts are the installer and what it imports at runtime
# (tests/test_release.py checks this list against bb-install.py's imports).
DEPLOYABLE = [
    "BIGBANG_VERSION",
    "overlays/**",
    "flux/**",
    "flux-fips/**",
    "seaweedfs/**",
    "seaweedfs-fips/**",
    "discovery/images.json",
    "mapping.yaml",
    "object-storage.yaml",
    "orgs/**",
    "requirements.txt",
    "scripts/bb-install.py",
    "scripts/bbinstall/**",
    "scripts/bbcluster.py",
    "scripts/bbcommon.py",
    "scripts/objstore.py",
    "scripts/orgtags.py",
    "scripts/release.py",
    ".github/workflows/release.yml",
]
# The CI job a release waits for: the one branch protection requires. CI's other jobs
# (results-gate) can be red on main without making the overlays wrong.
VERIFY = "verify"
# In a release archive: the repository it was released from, so the installer's
# upgrade --to finds later releases there without a git remote. A file of its own, not a
# line in RELEASE: installers already deployed compare RELEASE to the tag exactly.
REPO_FILE = "RELEASE_REPO"


# rke2.yml's weekly security report, attached to each release when the repo has it.
REPORT = os.path.join("docs", "security-report.pdf")

def github_remote(root=ROOT):
    """OWNER/REPO of the checkout's GitHub `origin` remote, or None (no git, no remote, or
    not GitHub)."""
    # .git is a file in a worktree or submodule, a directory otherwise.
    if not shutil.which("git") or not os.path.exists(os.path.join(root, ".git")):
        return None
    p = subprocess.run(["git", "-C", root, "remote", "get-url", "origin"], capture_output=True, text=True)
    m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", p.stdout.strip()) if not p.returncode else None
    return m.group(1) if m else None


def release_identity(repo):
    """The certificate identity regexp of `repo`'s release workflow on main. Anchored at both
    ends: a run of release.yml dispatched from another branch doesn't match."""
    escaped = repo.replace(".", r"\.")   # OWNER/REPO holds only [\w.-]; "." is the one regexp character
    return rf"^https://github\.com/{escaped}/\.github/workflows/release\.yml@refs/heads/main$"


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def next_tag(bigbang, tags):
    """The next <bigbang>-cg.<n> after the existing tags."""
    rx = re.compile(re.escape(bigbang) + r"-cg\.(\d+)$")
    n = max([int(m.group(1)) for t in tags for m in [rx.match(t)] if m] or [0])
    return f"{bigbang}-cg.{n + 1}"


def previous_tag(tags):
    """The newest release tag, by Big Bang version then revision."""
    rx = re.compile(r"^(\d+)\.(\d+)\.(\d+)-cg\.(\d+)$")
    ours = [(tuple(map(int, m.groups())), t) for t in tags for m in [rx.match(t)] if m]
    return max(ours)[1] if ours else None


def deployable(path):
    """Whether a changed file is one a site deploys or runs (DEPLOYABLE)."""
    for p in DEPLOYABLE:
        if path == p or (p.endswith("/**") and path.startswith(p[:-2])):
            return True
    return False


def changed_since(prev):
    """Files that differ between the release `prev` and HEAD (every file without one).
    Measured against the last release, not the last commit: a deployable change whose own
    CI failed is released with the next commit that passes, and a push of several commits
    counts all of them."""
    if not prev:
        return git("ls-files").splitlines()
    return git("diff", "--name-only", prev, "HEAD").splitlines()


def behind(prev):
    """Whether HEAD is an older commit than the release `prev` (CI runs on main finish in any
    order; a late run for an older commit must not release it after a newer one)."""
    if not prev:
        return False
    return subprocess.run(["git", "merge-base", "--is-ancestor", "HEAD", prev], cwd=ROOT).returncode == 0


def verify_result(repo, sha):
    """The conclusion of the newest `verify` check run on `sha` ('success', 'failure', ...),
    'pending' while it runs, or None when there is none. The newest only: a re-run adds a run
    beside the old one."""
    out = subprocess.run(["gh", "api", f"repos/{repo}/commits/{sha}/check-runs?check_name={VERIFY}&per_page=100",
                          "--jq", "[.check_runs[] | {id, status, conclusion}]"],
                         cwd=ROOT, capture_output=True, text=True, check=True).stdout
    runs = json.loads(out or "[]")
    if not runs:
        return None
    newest = max(runs, key=lambda r: r["id"])
    return newest["conclusion"] if newest["status"] == "completed" else "pending"


def image_refs(text):
    """{repository: tag} for each `repository:`/`tag:` pair and `image: repo:tag` line."""
    refs, repo = {}, None
    for line in text.splitlines():
        m = re.match(r"\s*(?:-\s*)?(repository|hub|tag|image):\s*['\"]?([^'\"\s#]+)", line)
        if not m:
            continue
        key, val = m.groups()
        if key in ("repository", "hub"):
            repo = val
        elif key == "tag" and repo:
            refs[repo] = val
            repo = None
        elif key == "image" and ":" in val.rsplit("/", 1)[-1]:
            name, tag = val.rsplit(":", 1)
            refs[name] = tag
    return refs


def image_changes(old_text, new_text):
    old, new = image_refs(old_text), image_refs(new_text)
    lines = [f"- `{r}`: {old[r]} → {new[r]}" for r in sorted(new) if r in old and old[r] != new[r]]
    lines += [f"- `{r}`: new at {new[r]}" for r in sorted(new) if r not in old]
    lines += [f"- `{r}`: removed" for r in sorted(old) if r not in new]
    return lines


def notes(tag, prev, bigbang, org, repo, root=ROOT):
    """The notes, in the order a site follows them: download the archive and its signature,
    verify, extract, check, install."""
    name = f"bigbang-chainguard-{tag}.tar.gz"
    url = f"https://github.com/{repo}/releases/download/{tag}"
    out = [f"Big Bang **{bigbang}** on Chainguard images from `{org}`.", ""]
    if os.path.exists(os.path.join(root, "docs", "upgrades", f"{bigbang}.md")):
        out += [f"Upgrading to Big Bang {bigbang}? Read [docs/upgrades/{bigbang}.md]"
                f"(https://github.com/{repo}/blob/{tag}/docs/upgrades/{bigbang}.md) first.", ""]
    out += ["```bash",
            "# 1. the archive and its Sigstore signature bundle",
            f"curl -fsSLO {url}/{name}",
            f"curl -fsSLO {url}/{name}.sigstore.json",
            "# 2. it came from this repository's release workflow on main: stop unless this prints Verified OK",
            f"cosign verify-blob --bundle {name}.sigstore.json \\",
            "  --certificate-oidc-issuer https://token.actions.githubusercontent.com \\",
            f"  --certificate-identity-regexp '{release_identity(repo)}' \\",
            f"  {name}",
            "# 3. extract",
            f"tar xzf {name} && cd bigbang-chainguard-{tag}",
            "# 4. test every setting, changing nothing (writes a starter my-site.yaml when there is none)",
            "python scripts/bb-install.py check --site my-site.yaml",
            "# 5. install",
            "python scripts/bb-install.py install --site my-site.yaml",
            "```", ""]
    if os.path.exists(os.path.join(root, REPORT)):
        out += [f"The newest weekly [security report]({url}/security-report.pdf) (`{REPORT}`; its first page "
                "has the scan date): each image's CVEs after Chainguard's advisories, KEV, EPSS, signature and SBOM.", ""]
    if prev:
        old = subprocess.run(["git", "show", f"{prev}:{OVERLAY}"], cwd=ROOT, capture_output=True, text=True).stdout
        changes = image_changes(old, open(os.path.join(ROOT, OVERLAY), encoding="utf-8").read())
        out += [f"### Image changes since {prev}", ""] + (changes or ["- none"]) + [""]
    return "\n".join(out)


def archive(tag, dest, repo):
    """git archive of HEAD under bigbang-chainguard-<tag>/, with RELEASE holding the tag and
    RELEASE_REPO the repository."""
    prefix = f"bigbang-chainguard-{tag}/"
    raw = subprocess.run(["git", "archive", "--format=tar", f"--prefix={prefix}", "HEAD"], cwd=ROOT,
                         capture_output=True, check=True).stdout
    path = os.path.join(dest, f"bigbang-chainguard-{tag}.tar.gz")
    with tarfile.open(fileobj=io.BytesIO(raw)) as src, tarfile.open(path, "w:gz") as out:
        for m in src.getmembers():
            out.addfile(m, src.extractfile(m) if m.isfile() else None)
        for name, text in (("RELEASE", tag + "\n"), (REPO_FILE, f"repo: {repo}\n")):
            data = text.encode()
            info = tarfile.TarInfo(prefix + name)
            info.size, info.mode = len(data), 0o644
            out.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
    with open(path + ".sha256", "w", encoding="utf-8", newline="\n") as f:
        f.write(f"{digest}  {os.path.basename(path)}\n")
    assets = [path, path + ".sha256"]
    if shutil.which("cosign"):
        subprocess.run(["cosign", "sign-blob", "--yes", "--bundle", path + ".sigstore.json", path], check=True)
        assets.append(path + ".sigstore.json")
    return assets


def gate(a, repo, prev):
    """Why HEAD must not be released now (None: release it). --after-ci checks the cheap,
    local reasons first, so a docs-only commit costs no API call."""
    if not (a.require_verify or a.after_ci):
        return None
    if a.after_ci:
        if behind(prev):
            return f"HEAD is older than the latest release {prev}: nothing to release"
        changed = [f for f in changed_since(prev) if deployable(f)]
        if not changed:
            return f"no deployable file changed since {prev}: nothing to release"
    sha = git("rev-parse", "HEAD")
    result = verify_result(repo, sha)
    if result != "success":
        return f"CI's {VERIFY} on {sha} is {result or 'missing'}, not success: no release"
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the tag and notes; create nothing")
    ap.add_argument("--dist", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--repo", metavar="OWNER/REPO",
                    help="the repository releasing (default: $GITHUB_REPOSITORY, else the git remote)")
    when = ap.add_mutually_exclusive_group()
    when.add_argument("--require-verify", action="store_true",
                      help=f"fail unless CI's `{VERIFY}` passed on HEAD (a manual release)")
    when.add_argument("--after-ci", action="store_true",
                      help=f"release only if `{VERIFY}` passed on HEAD and a deployable file changed since "
                           "the previous release; otherwise stop with exit 0 (release.yml after CI)")
    a = ap.parse_args()
    repo = a.repo or os.environ.get("GITHUB_REPOSITORY") or github_remote()
    if not repo:
        raise SystemExit("which repository is releasing? Pass --repo OWNER/REPO")
    bigbang = open(os.path.join(ROOT, "BIGBANG_VERSION"), encoding="utf-8").read().strip()
    head = open(os.path.join(ROOT, OVERLAY), encoding="utf-8").read(800)
    m = re.search(r"Generated for cgr\.dev/([^\s:]+)", head)
    org = m.group(1) if m else "?"
    tags = git("tag", "--list").split()
    released = [t for t in git("tag", "--points-at", "HEAD").split() if re.match(r"^\d+\.\d+\.\d+-cg\.\d+$", t)]
    if released:
        print(f"HEAD is already released as {released[0]}")
        return 0
    tag, prev = next_tag(bigbang, tags), previous_tag(tags)
    stop = gate(a, repo, prev)
    if stop:
        print(stop)
        return 1 if a.require_verify else 0
    body = notes(tag, prev, bigbang, org, repo)
    print(f"release {tag} (previous {prev or 'none'})\n\n{body}")
    if a.dry_run:
        return 0
    os.makedirs(a.dist, exist_ok=True)
    assets = archive(tag, a.dist, repo)
    if os.path.exists(os.path.join(ROOT, REPORT)):
        shutil.copy(os.path.join(ROOT, REPORT), os.path.join(a.dist, "security-report.pdf"))
        assets.append(os.path.join(a.dist, "security-report.pdf"))
    notes_file = os.path.join(a.dist, "notes.md")
    open(notes_file, "w", encoding="utf-8").write(body)
    subprocess.run(["gh", "release", "create", tag, *assets, "--target", git("rev-parse", "HEAD"),
                    "--title", f"{tag} (Big Bang {bigbang})", "--notes-file", notes_file, "--generate-notes"]
                   + (["--notes-start-tag", prev] if prev else []), cwd=ROOT, check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
