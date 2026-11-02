#!/usr/bin/env python3
"""Compare known vulnerabilities: each Iron Bank image Big Bang deploys against the
Chainguard image that replaces it.

  bb-cve.py [--fips] [--ironbank | --ironbank-bases] [--jobs 2] [--limit N] [-o report.md]
  bb-cve.py --changed-from origin/main      (a pull request's gate)

Reads docs/images.md (bb-convert.py's image map), scans every distinct image pair with
grype, and writes a Markdown report: totals by severity, each side's distinct images (how
many have no Critical, or no Critical or High, and their size as grype measured it), then
one row per image with both sides' counts and sizes. Iron Bank images need registry1 credentials in the Docker config;
without --ironbank (or without credentials) only the Chainguard side is scanned and the
Iron Bank columns say so. Images that stay on Iron Bank and the images this repo builds
are listed under their own headings. cve-report.yml runs this weekly.

--ironbank-bases: without an Iron Bank account, a floor for each Iron Bank image that anyone
can check. Iron Bank's recipes are public (repo1.dso.mil/dsop/<image>): hardening_manifest.yaml
names the base the image is built FROM (UBI 9 minimal, distroless, ...), and most bases have a
public twin (registry.access.redhat.com/ubi9/ubi-minimal, gcr.io/distroless, ...). The Iron
Bank side is then that public base alone, before the application is added, from the recipe's
current branch: the image Big Bang deploys has at least those vulnerabilities and that size.

--changed-from <git ref>: only the Chainguard images (standard and FIPS) that docs/images.md
names now and didn't at <ref>, scanned for fixable Critical CVEs; exit 1 if any has one or can't be scanned.
ci.yml runs it on pull requests, so the nightly update can't move to an image with a
known, already-fixed critical vulnerability without someone deciding to.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bbcommon import ROOT  # noqa: E402
import bbbuild  # noqa: E402

SEVERITIES = ["Critical", "High", "Medium", "Low"]
IRON_BANK = "registry1.dso.mil"


def image_pairs(text, fips=False):
    """[(Iron Bank ref, replacement ref)] from docs/images.md, each pair once. Rows that
    stay on Iron Bank have replacement None."""
    org = re.search(r"are in `(cgr\.dev/[^`]+)`", text)
    prefix = org.group(1) if org else "cgr.dev/ORG"
    pairs = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or not cells[1].startswith(("ironbank/", "big-bang/")) and "/" not in cells[1]:
            continue
        if cells[0] in ("Package", "---"):
            continue
        ib, new = cells[1], cells[3 if fips else 2]
        if new == "Iron Bank":
            pairs.setdefault(f"{IRON_BANK}/{ib}", None)
        elif new:
            pairs.setdefault(f"{IRON_BANK}/{ib}", new if "/" in new.split(":")[0] else f"{prefix}/{new}")
    return sorted(pairs.items())


def new_refs(old_text, new_text):
    """Chainguard refs (standard and FIPS) in new_text's image map that old_text lacks."""
    def refs(text):
        out = set()
        for fips in (False, True):
            out |= {new for _, new in image_pairs(text, fips) if new}
        return out
    return sorted(refs(new_text) - refs(old_text))


def gate(base):
    """Fixable Critical CVEs in the images new since `base`: (lines, failed)."""
    old = subprocess.run(["git", "show", f"{base}:docs/images.md"], cwd=ROOT, capture_output=True, text=True).stdout
    refs = new_refs(old, open(os.path.join(ROOT, "docs", "images.md"), encoding="utf-8").read())
    lines, failed = [f"{len(refs)} image(s) new since {base}"], False
    for ref in refs:
        p = subprocess.run(["grype", f"registry:{ref}", "--only-fixed", "-o", "json"], capture_output=True, text=True)
        status, detail = gate_result(p.returncode, p.stdout, p.stderr)
        failed |= status != "ok"
        lines.append(f"{status}  {ref}")
        lines += ["    " + ln for ln in detail[:15]]
    return lines, failed


def gate_result(returncode, stdout, stderr):
    """(status, detail lines) of one --only-fixed scan. A scan that fails (say, a private
    image without a registry login) is SCAN FAILED, not a finding."""
    try:
        found = criticals(json.loads(stdout)) if returncode == 0 else None
    except ValueError:
        found = None
    if found is None:
        return "SCAN FAILED", (stderr.strip().splitlines() or ["grype failed"])[-5:]
    if found:
        return "FIXABLE CRITICAL", [f"{i} {pkg} {ver} (fixed in {fixed})" for i, pkg, ver, fixed in found]
    return "ok", []


# Iron Bank base (hardening_manifest BASE_IMAGE) -> its public twin; the tag carries over
# except where the twin names it differently. "scratch" has nothing in it.
PUBLIC_BASES = {
    "redhat/ubi/ubi8": "registry.access.redhat.com/ubi8/ubi",
    "redhat/ubi/ubi8-minimal": "registry.access.redhat.com/ubi8/ubi-minimal",
    "redhat/ubi/ubi8-micro": "registry.access.redhat.com/ubi8/ubi-micro",
    "redhat/ubi/ubi9": "registry.access.redhat.com/ubi9/ubi",
    "redhat/ubi/ubi9-minimal": "registry.access.redhat.com/ubi9/ubi-minimal",
    "redhat/ubi/ubi9-micro": "registry.access.redhat.com/ubi9/ubi-micro",
    "redhat/ubi/ubi10": "registry.access.redhat.com/ubi10/ubi",
    "redhat/ubi/ubi10-minimal": "registry.access.redhat.com/ubi10/ubi-minimal",
    "redhat/ubi/ubi10-micro": "registry.access.redhat.com/ubi10/ubi-micro",
    "opensource/alpinelinux/alpine": "docker.io/library/alpine",
    "opensource/debian/debian": "docker.io/library/debian",
    "suse/bci/bci-base": "registry.suse.com/bci/bci-base",
    "google/distroless/static": "gcr.io/distroless/static",
    "google/distroless12/static": "gcr.io/distroless/static-debian12",
    "google/distroless12/base": "gcr.io/distroless/base-debian12",
    "google/distroless13/static": "gcr.io/distroless/static-debian13",
    "google/distroless13/base": "gcr.io/distroless/base-debian13",
    "docker/scratch": "scratch",
}


def public_base(args):
    """The public image matching a recipe's BASE_IMAGE/BASE_TAG, "scratch", or None (an Iron
    Bank-only base, such as another Iron Bank image)."""
    repo = PUBLIC_BASES.get(args.get("BASE_IMAGE", ""))
    if repo in (None, "scratch"):
        return repo
    return f"{repo}:{args.get('BASE_TAG') or 'latest'}"


def ironbank_base(ib_ref):
    """(public base or None, why) for an Iron Bank image, from its public recipe."""
    path = ib_ref.removeprefix(IRON_BANK + "/").rsplit(":", 1)[0]
    if not path.startswith("ironbank/"):
        return None, "recipe not on repo1's dsop/"
    try:
        _, manifest, _ = bbbuild.ironbank_recipe(path.removeprefix("ironbank/"), "master")
    except Exception as e:  # noqa: BLE001 - a missing or renamed recipe: report, don't fail
        return None, f"recipe not found ({str(e)[:40]})"
    args = manifest.get("args") or {}
    base = public_base(args)
    return base, base or f"base {args.get('BASE_IMAGE')} has no public twin"


SCRATCH = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "critical": [], "size": 0}


def counts(grype_json):
    """{severity: n} of unique vulnerability IDs in grype's JSON output."""
    seen, out = set(), dict.fromkeys(SEVERITIES, 0)
    for m in grype_json.get("matches", []):
        v = m.get("vulnerability", {})
        key = (v.get("id"), m.get("artifact", {}).get("name"))
        if key in seen:
            continue
        seen.add(key)
        sev = v.get("severity", "")
        if sev in out:
            out[sev] += 1
    return out


def criticals(grype_json):
    """Sorted unique (id, package, version, fixed-in or "") of the Critical matches."""
    out = set()
    for m in grype_json.get("matches", []):
        v, a = m.get("vulnerability", {}), m.get("artifact", {})
        if v.get("severity") == "Critical":
            fixed = ", ".join(v.get("fix", {}).get("versions") or [])
            out.add((v.get("id", "?"), a.get("name", "?"), a.get("version", "?"), fixed))
    return sorted(out)


def scan(ref):
    """{severity: n}, or an error string."""
    try:
        p = subprocess.run(["grype", f"registry:{ref}", "-o", "json", "--quiet"], capture_output=True, text=True,
                           timeout=1800)
    except subprocess.TimeoutExpired:
        return "timed out"
    if p.returncode:
        return (p.stderr.strip().splitlines() or ["grype failed"])[-1][:120]
    found = json.loads(p.stdout)
    return {**counts(found), "critical": criticals(found), "size": image_size(found)}


def image_size(grype_json):
    """The image's size in bytes (uncompressed, as grype unpacked it), or None."""
    target = grype_json.get("source", {}).get("target")
    return target.get("imageSize") if isinstance(target, dict) else None


def mb(size):
    return f"{size / 1e6:,.0f} MB" if size else "?"


def distinct(refs, results):
    """[images, no Critical, no Critical or High, total bytes] over the scanned refs."""
    done = [results[r] for r in sorted(set(refs)) if isinstance(results.get(r), dict)]
    return [len(done), sum(1 for r in done if not r["Critical"]),
            sum(1 for r in done if not r["Critical"] and not r["High"]), sum(r.get("size") or 0 for r in done)]


def cell(result):
    if result is None:
        return "not scanned"
    if isinstance(result, str):
        return f"error: {result}"
    text = " / ".join(str(result[s]) for s in SEVERITIES)
    # A base-only floor (--ironbank-bases): at least this, and which base it is.
    return f"≥ {text} ({result['base'].rsplit('/', 1)[-1]})" if result.get("base") else text


def report(pairs, results, fips, ironbank, bases=False):
    converted = [(ib, new) for ib, new in pairs if new]
    kept = [ib for ib, new in pairs if not new]
    totals = {side: dict.fromkeys(SEVERITIES, 0) for side in ("ib", "cg")}
    for ib, new in converted:
        for side, ref in (("ib", ib), ("cg", new)):
            r = results.get(ref)
            if isinstance(r, dict):
                for s in SEVERITIES:
                    totals[side][s] += r[s]
    head = " | ".join(SEVERITIES)
    ib_label = "Iron Bank (public base only: a floor)" if bases else "Iron Bank"
    lines = [f"# Known vulnerabilities: Iron Bank vs Chainguard ({'FIPS' if fips else 'standard'})", "",
             f"{len(converted)} converted images, scanned with grype. Counts are {head} (unique CVE and package).",
             "Chainguard images are often a newer patch than Iron Bank's (docs/images.md says where).", ""]
    if bases:
        resolved = sum(1 for ib, _ in converted if isinstance(results.get(ib), dict))
        lines += [f"Iron Bank side: the public twin of the base each image is built FROM, named in its public "
                  f"recipe (repo1.dso.mil/dsop), scanned alone, before the application: a floor ({resolved} of "
                  f"{len(converted)} images resolved; the rest say why). Totals cover those {resolved} only.", ""]
    lines += [
             f"| | {head} |", "|---|" + "---|" * len(SEVERITIES),
             f"| {ib_label} | {' | '.join(str(totals['ib'][s]) for s in SEVERITIES) if ironbank else 'not scanned'} |",
             f"| Chainguard | {' | '.join(str(totals['cg'][s]) for s in SEVERITIES)} |", ""]
    # Per distinct image: several Iron Bank images can map to one Chainguard image (the four
    # devops-tester tags), which the totals above count once per Iron Bank image.
    lines += ["Distinct images scanned:", "",
              "| | Images | No Critical | No Critical or High | Total size |", "|---|---|---|---|---|"]
    for name, side, on in ((ib_label, 0, ironbank), ("Chainguard", 1, True)):
        if on:
            n, nc, nch, size = distinct([p[side] for p in converted], results)
            lines.append(f"| {name} | {n} | {nc} | {nch} | {mb(size)} |")
    lines += ["", "## Per image", "",
              "| Iron Bank image | Iron Bank | Chainguard image | Chainguard | Size (Iron Bank / Chainguard) |",
              "|---|---|---|---|---|"]
    for ib, new in converted:
        sizes = " / ".join(mb(r.get("size")) if isinstance(r, dict) else "-" for r in (results.get(ib), results.get(new)))
        lines.append(f"| {ib.removeprefix(IRON_BANK + '/')} | {cell(results.get(ib))} | {new} | {cell(results.get(new))} "
                     f"| {sizes} |")
    crit = [(new, c) for _, new in converted if isinstance(results.get(new), dict)
            for c in results[new].get("critical", [])]
    if crit:
        # What to fix: a fixed-in version means a rebuild or a newer tag clears it.
        lines += ["", "## Critical in the Chainguard images", "",
                  "| Image | CVE | Package | Installed | Fixed in |", "|---|---|---|---|---|"]
        lines += [f"| {ref} | {i} | {pkg} | {ver} | {fixed or 'no fix yet'} |" for ref, (i, pkg, ver, fixed) in crit]
    if kept:
        lines += ["", "## Still on Iron Bank", ""] + [f"- {ib.removeprefix(IRON_BANK + '/')}" for ib in kept]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fips", action="store_true", help="compare against the FIPS images")
    ap.add_argument("--ironbank", action="store_true", help="also scan the Iron Bank images (registry1 login)")
    ap.add_argument("--ironbank-bases", action="store_true",
                    help="Iron Bank side from each image's public base (no login; a floor)")
    ap.add_argument("--jobs", type=int, default=2, help="scans at once (each unpacks an image)")
    ap.add_argument("--limit", type=int, help="only the first N pairs (a quick try)")
    ap.add_argument("-o", "--out", help="write the report here (default: stdout)")
    ap.add_argument("--changed-from", metavar="REF", help="gate: fixable Critical CVEs in images new since REF")
    a = ap.parse_args()
    if a.changed_from:
        lines, failed = gate(a.changed_from)
        print("\n".join(lines))
        return 1 if failed else 0
    pairs = image_pairs(open(os.path.join(ROOT, "docs", "images.md"), encoding="utf-8").read(), a.fips)
    if a.limit:
        pairs = pairs[:a.limit]
    refs = sorted({new for _, new in pairs if new} | ({ib for ib, new in pairs if new} if a.ironbank else set()))
    bases = {}
    if a.ironbank_bases and not a.ironbank:
        with ThreadPoolExecutor(8) as pool:
            ibs = [ib for ib, new in pairs if new]
            bases = dict(zip(ibs, pool.map(ironbank_base, ibs)))
        refs = sorted(set(refs) | {b for b, _ in bases.values() if b and b != "scratch"})
    print(f"scanning {len(refs)} images with {a.jobs} jobs", file=sys.stderr, flush=True)
    results = {}
    with ThreadPoolExecutor(a.jobs) as pool:
        for i, (ref, r) in enumerate(zip(refs, pool.map(scan, refs)), 1):
            results[ref] = r
            print(f"[{i}/{len(refs)}] {ref}: {cell(r)}", file=sys.stderr, flush=True)
    for ib, (base, why) in bases.items():
        found = SCRATCH if base == "scratch" else results.get(base)
        results[ib] = {**found, "base": base} if isinstance(found, dict) else (why if not base else found)
    text = report(pairs, results, a.fips, a.ironbank or bool(bases), bases=bool(bases))
    if a.out:
        open(a.out, "w", encoding="utf-8").write(text)
    else:
        print(text)
    errors = [r for r in refs if isinstance(results[r], str)]
    print(f"{len(refs) - len(errors)} scanned, {len(errors)} errors", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
