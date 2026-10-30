#!/usr/bin/env python3
"""Cut a versioned release: the tag, its notes and the archive users install from.

  release.py [--dry-run]

Tags are <Big Bang version>-cg.<n>: n counts this repo's releases for that Big Bang version
(3.35.1-cg.1, 3.35.1-cg.2, ... then 3.36.0-cg.1), so a tag says which Big Bang it deploys
and which revision of the overlays. The release holds bigbang-chainguard-<tag>.tar.gz (the
checkout at the tagged commit, with RELEASE set to the tag so bb-install.py can report
it), its sha256 and, with cosign on the PATH (release.yml), a keyless Sigstore signature
bundle tied to this repository's release workflow. The notes say how to verify it, list the Big Bang version, the org, and the image changes
since the previous release. release.yml runs this on main when the deployable files change.
Needs git (with tags fetched) and gh.
"""
import argparse
import hashlib
import io
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
REPO = os.environ.get("GITHUB_REPOSITORY", "Zapata-Technology/bigbang-chainguard")


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


def notes(tag, prev, bigbang, org):
    out = [f"Big Bang **{bigbang}** on Chainguard images from `{org}`.", "",
           "```bash",
           f"curl -fsSLO https://github.com/{REPO}/releases/download/{tag}/bigbang-chainguard-{tag}.tar.gz",
           f"tar xzf bigbang-chainguard-{tag}.tar.gz && cd bigbang-chainguard-{tag}",
           "python scripts/bb-install.py init && python scripts/bb-install.py install --site my-site.yaml",
           "```", "",
           "Check the archive came from this repository's release workflow:", "",
           "```bash",
           f"cosign verify-blob --bundle bigbang-chainguard-{tag}.tar.gz.sigstore.json \\",
           "  --certificate-oidc-issuer https://token.actions.githubusercontent.com \\",
           f"  --certificate-identity-regexp '^https://github.com/{REPO}/.github/workflows/release.yml@' \\",
           f"  bigbang-chainguard-{tag}.tar.gz",
           "```", ""]
    if prev:
        old = subprocess.run(["git", "show", f"{prev}:{OVERLAY}"], cwd=ROOT, capture_output=True, text=True).stdout
        changes = image_changes(old, open(os.path.join(ROOT, OVERLAY), encoding="utf-8").read())
        out += [f"### Image changes since {prev}", ""] + (changes or ["- none"]) + [""]
    return "\n".join(out)


def archive(tag, dest):
    """git archive of HEAD under bigbang-chainguard-<tag>/, with RELEASE holding the tag."""
    prefix = f"bigbang-chainguard-{tag}/"
    raw = subprocess.run(["git", "archive", "--format=tar", f"--prefix={prefix}", "HEAD"], cwd=ROOT,
                         capture_output=True, check=True).stdout
    path = os.path.join(dest, f"bigbang-chainguard-{tag}.tar.gz")
    with tarfile.open(fileobj=io.BytesIO(raw)) as src, tarfile.open(path, "w:gz") as out:
        for m in src.getmembers():
            out.addfile(m, src.extractfile(m) if m.isfile() else None)
        data = (tag + "\n").encode()
        info = tarfile.TarInfo(prefix + "RELEASE")
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the tag and notes; create nothing")
    ap.add_argument("--dist", default=os.path.join(ROOT, "dist"))
    a = ap.parse_args()
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
    body = notes(tag, prev, bigbang, org)
    print(f"release {tag} (previous {prev or 'none'})\n\n{body}")
    if a.dry_run:
        return 0
    os.makedirs(a.dist, exist_ok=True)
    assets = archive(tag, a.dist)
    notes_file = os.path.join(a.dist, "notes.md")
    open(notes_file, "w", encoding="utf-8").write(body)
    subprocess.run(["gh", "release", "create", tag, *assets, "--target", git("rev-parse", "HEAD"),
                    "--title", f"{tag} (Big Bang {bigbang})", "--notes-file", notes_file, "--generate-notes"]
                   + (["--notes-start-tag", prev] if prev else []), cwd=ROOT, check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
