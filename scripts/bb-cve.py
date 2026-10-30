#!/usr/bin/env python3
"""Compare known vulnerabilities: each Iron Bank image Big Bang deploys against the
Chainguard image that replaces it.

  bb-cve.py [--fips] [--ironbank] [--jobs 4] [--limit N] [-o report.md]

Reads docs/images.md (bb-convert.py's image map), scans every distinct image pair with
grype, and writes a Markdown report: totals by severity, then one row per image with
both sides' counts. Iron Bank images need registry1 credentials in the Docker config;
without --ironbank (or without credentials) only the Chainguard side is scanned and the
Iron Bank columns say so. Images that stay on Iron Bank and the images this repo builds
are listed under their own headings. cve-report.yml runs this weekly.
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


def scan(ref):
    """{severity: n}, or an error string."""
    p = subprocess.run(["grype", f"registry:{ref}", "-o", "json", "--quiet"], capture_output=True, text=True,
                       timeout=1800)
    if p.returncode:
        return (p.stderr.strip().splitlines() or ["grype failed"])[-1][:120]
    return counts(json.loads(p.stdout))


def cell(result):
    if result is None:
        return "not scanned"
    if isinstance(result, str):
        return f"error: {result}"
    return " / ".join(str(result[s]) for s in SEVERITIES)


def report(pairs, results, fips, ironbank):
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
    lines = [f"# Known vulnerabilities: Iron Bank vs Chainguard ({'FIPS' if fips else 'standard'})", "",
             f"{len(converted)} converted images, scanned with grype. Counts are {head} (unique CVE and package).",
             "Chainguard images are often a newer patch than Iron Bank's (docs/images.md says where).", "",
             f"| | {head} |", "|---|" + "---|" * len(SEVERITIES),
             f"| Iron Bank | {' | '.join(str(totals['ib'][s]) for s in SEVERITIES) if ironbank else 'not scanned'} |",
             f"| Chainguard | {' | '.join(str(totals['cg'][s]) for s in SEVERITIES)} |", "",
             "## Per image", "", "| Iron Bank image | Iron Bank | Chainguard image | Chainguard |", "|---|---|---|---|"]
    for ib, new in converted:
        lines.append(f"| {ib.removeprefix(IRON_BANK + '/')} | {cell(results.get(ib))} | {new} | {cell(results.get(new))} |")
    if kept:
        lines += ["", "## Still on Iron Bank", ""] + [f"- {ib.removeprefix(IRON_BANK + '/')}" for ib in kept]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fips", action="store_true", help="compare against the FIPS images")
    ap.add_argument("--ironbank", action="store_true", help="also scan the Iron Bank images (registry1 login)")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--limit", type=int, help="only the first N pairs (a quick try)")
    ap.add_argument("-o", "--out", help="write the report here (default: stdout)")
    a = ap.parse_args()
    pairs = image_pairs(open(os.path.join(ROOT, "docs", "images.md"), encoding="utf-8").read(), a.fips)
    if a.limit:
        pairs = pairs[:a.limit]
    refs = sorted({new for _, new in pairs if new} | ({ib for ib, new in pairs if new} if a.ironbank else set()))
    print(f"scanning {len(refs)} images with {a.jobs} jobs", file=sys.stderr, flush=True)
    with ThreadPoolExecutor(a.jobs) as pool:
        results = dict(zip(refs, pool.map(scan, refs)))
    text = report(pairs, results, a.fips, a.ironbank)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(text)
    else:
        print(text)
    errors = [r for r in refs if isinstance(results[r], str)]
    print(f"{len(refs) - len(errors)} scanned, {len(errors)} errors", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
