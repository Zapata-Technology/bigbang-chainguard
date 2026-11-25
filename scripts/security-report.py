#!/usr/bin/env python3
"""The security report of an RKE2 run: NeuVector's scans, checked against grype, Trivy,
Chainguard's advisories, CISA's KEV list and EPSS, with each image's signature and SBOM.

  python scripts/security-report.py pins --mode standard|fips
  python scripts/security-report.py scan --nv DIR [--nv DIR] --out SCANS [--jobs 2]
  python scripts/security-report.py build --nv DIR [--nv DIR] --scans SCANS --out OUT

rke2.yml runs all three (input `neuvector_report`, and its weekly schedule):

`pins` prints every image reference the overlays pin (cgr.dev/<org> and the images built
here, by digest), including the images of Big Bang's optional packages. nv-report.py
--optional-images scans those no pod runs by reference, so the report covers what a site
would run with those packages on, without the cluster it would take to deploy them.

`scan` runs, for each image in nv-report.json (deployed or not): grype and Trivy against
the registry (both read Chainguard's security data; Trivy also lists the image's apk
packages), and, for the org's and the built images, `cosign verify` (Chainguard's keyless
signature, or builds.yml's) and `cosign verify-attestation --type spdxjson` (the signed
SBOM). One JSON file per image in SCANS, kept between runs (an image is scanned once).
Needs grype, trivy and cosign on the PATH (or GRYPE_CMD, TRIVY_CMD, COSIGN_CMD, e.g. a
`docker run` of the org's images) and the registries' credentials in the Docker config.

`build` downloads Chainguard's and Wolfi's security databases (packages.cgr.dev,
packages.wolfi.dev), CISA's Known Exploited Vulnerabilities list and FIRST's EPSS scores,
and writes to OUT:

  images.json   one row per image and mode: NeuVector's, grype's and Trivy's counts, the
                Critical and High still open after Chainguard's advisories, signature, SBOM
  cves.json     one row per finding (image, CVE, package): which scanners report it,
                Chainguard's advisory (not affected, fixed in this build, fixed in a newer
                build, none), KEV, EPSS
  scan.json     the run, cluster and databases
  report.html   the report, laid out for print (rke2.yml prints it to report.pdf with Chrome)
  summary.md    the headline numbers, for the job summary

A finding is open unless Chainguard's advisory for a package in that image says the CVE
does not affect it or is fixed at or below the installed version. RKE2's own images have no
Chainguard advisories: their findings stay as the scanners report them.
"""
import argparse
import html
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bbcommon import (BUILD_SIGNATURE_ISSUER, SIGNATURE_ISSUERS, build_registry, build_signature_identity,  # noqa: E402
                      build_signer, signature_identity)

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SEVERITIES = ("Critical", "High", "Medium", "Low")
SECDBS = ("https://packages.cgr.dev/chainguard/security.json", "https://packages.wolfi.dev/os/security.json")
KEV = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
EPSS = "https://api.first.org/data/v1/epss?cve="
TRIVY_TRIES, TRIVY_WAIT = 4, 15
ORIGIN = {"chainguard": "Chainguard", "built-here": "Built here (FROM Chainguard)", "other": "RKE2 / other"}


# ---- pins ----------------------------------------------------------------------------------

def pinned_refs(texts):
    """Every name@digest an overlay, Flux kustomization or SeaweedFS manifest pins: kustomize
    image pins (name or newName with digest), chart values (registry, repository and digest,
    or a repository that names its registry) and plain name@digest references."""
    refs = set()

    def walk(node):
        if isinstance(node, dict):
            d = node.get("digest")
            if isinstance(d, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", d):
                name = node.get("newName") or node.get("name") if ("newName" in node or "name" in node) else None
                repo = node.get("repository")
                if not name and isinstance(repo, str):
                    name = f"{node['registry'].rstrip('/')}/{repo}" if isinstance(node.get("registry"), str) else repo
                if isinstance(name, str) and "/" in name and "." in name.split("/", 1)[0]:
                    refs.add(f"{name}@{d}")
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            for m in re.finditer(r"([a-z0-9.-]+\.[a-z]{2,}/[\w./-]+?)(?::[\w.+-]+)?@(sha256:[0-9a-f]{64})", node):
                refs.add(f"{m.group(1)}@{m.group(2)}")

    for text in texts:
        for doc in yaml.safe_load_all(text):
            walk(doc)
    return sorted(refs)


def pin_tags():
    """digest -> tag for every pin that names both (overlays, Flux, SeaweedFS, the test VM's
    storage provisioner); NeuVector's rows carry the digest, the report shows the tag."""
    tags = {}

    def walk(node):
        if isinstance(node, dict):
            d, t = node.get("digest"), node.get("tag") or node.get("newTag")
            if isinstance(d, str) and d.startswith("sha256:") and t:
                tags.setdefault(d, str(t))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    files = pin_files("standard") + pin_files("fips")
    vm = os.path.join(ROOT, ".github", "rke2-vm.sh")
    for f in files + ([vm] if os.path.exists(vm) else []):
        text = open(f, encoding="utf-8").read()
        for m in re.finditer(r":([\w.+-]+)@(sha256:[0-9a-f]{64})", text):
            tags.setdefault(m.group(2), m.group(1))
        if f.endswith((".yaml", ".yml")):
            for doc in yaml.safe_load_all(text):
                walk(doc)
    return tags


def images_md_tags():
    """repository name -> tag from docs/images.md, when the name has one tag in a mode column."""
    seen = {}
    path = os.path.join(ROOT, "docs", "images.md")
    for line in open(path, encoding="utf-8") if os.path.exists(path) else []:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        for cell in cells[2:4] if len(cells) >= 4 else []:
            m = re.fullmatch(r"([\w./-]+):([\w.+-]+)", cell)
            if m:
                seen.setdefault(m.group(1).rsplit("/", 1)[-1], set()).add(m.group(2))
    return {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}


def pin_files(mode):
    fips = mode == "fips"
    files = [os.path.join(ROOT, "overlays", "values-chainguard-fips.yaml" if fips else "values-chainguard.yaml"),
             os.path.join(ROOT, "flux-fips" if fips else "flux", "kustomization.yaml")]
    sw = os.path.join(ROOT, "seaweedfs-fips" if fips else "seaweedfs")
    if os.path.isdir(sw):
        files += [os.path.join(sw, f) for f in sorted(os.listdir(sw)) if f.endswith((".yaml", ".yml"))]
    return [f for f in files if os.path.exists(f)]


def cmd_pins(a):
    org = os.environ.get("CHAINGUARD_ORG", "")
    build = build_registry() or ""
    texts = [open(f, encoding="utf-8").read() for f in pin_files(a.mode)]
    for ref in pinned_refs(texts):
        if (org and ref.startswith(f"cgr.dev/{org}/")) or (build and ref.startswith(build.rstrip("/") + "/")):
            print(ref)


# ---- scan ----------------------------------------------------------------------------------

def load_nv(dirs):
    """[(mode, nv-report.json)] for each --nv directory that has one."""
    out = []
    for d in dirs:
        p = os.path.join(d, "nv-report.json")
        if os.path.exists(p):
            r = json.load(open(p, encoding="utf-8"))
            out.append((r.get("mode", "standard"), r))
    return out


def image_ref(row):
    """The reference to scan: name@digest when the row has a digest, else the reference."""
    name = row["image"]
    if row.get("digest"):
        last = name.rsplit("/", 1)[-1]
        if ":" in last:
            name = name.rsplit(":", 1)[0]
        return f"{name}@{row['digest']}"
    return name


def scan_key(ref):
    m = re.search(r"sha256:([0-9a-f]{64})", ref)
    return m.group(1)[:24] if m else re.sub(r"[^\w.-]+", "_", ref)[-80:]


def tool(name):
    """The command for grype, trivy or cosign: $GRYPE_CMD (etc.) when set, e.g. a `docker run`."""
    return shlex.split(os.environ.get(f"{name.upper()}_CMD", name))


def run(argv, timeout=3600):
    argv = tool(argv[0]) + argv[1:]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def grype_findings(doc):
    out = []
    for m in doc.get("matches", []):
        v, art = m.get("vulnerability", {}), m.get("artifact", {})
        ids = {v.get("id")} | {r.get("id") for r in m.get("relatedVulnerabilities", [])}
        out.append({"id": v.get("id"), "aliases": sorted(i for i in ids if i), "severity": v.get("severity"),
                    "package": art.get("name"), "installed": art.get("version"), "type": art.get("type"),
                    "fixed_in": ", ".join(v.get("fix", {}).get("versions") or [])})
    return out


def trivy_findings(doc):
    vulns, pkgs = [], {}
    for r in doc.get("Results") or []:
        for p in r.get("Packages") or []:
            if r.get("Class") == "os-pkgs":
                pkgs[p.get("Name")] = p.get("Version")
        for v in r.get("Vulnerabilities") or []:
            vulns.append({"id": v.get("VulnerabilityID"), "aliases": [v.get("VulnerabilityID")],
                          "severity": (v.get("Severity") or "").title(), "package": v.get("PkgName"),
                          "installed": v.get("InstalledVersion"), "fixed_in": v.get("FixedVersion") or ""})
    return vulns, pkgs


def signer_of(stdout, stderr):
    """The certificate subject cosign verify reports (its JSON on stdout, else its log)."""
    try:
        for sig in json.loads(stdout or "[]"):
            subject = (sig.get("optional") or {}).get("Subject")
            if subject:
                return subject
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass
    return next((line.split(":", 1)[1].strip() for line in (stderr or "").splitlines()
                 if line.startswith("Certificate subject:")), "")


def scan_one(ref, origin, signers):
    """grype, Trivy and (for the org's and built images) cosign, for one image."""
    out = {"ref": ref, "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    rc, so, se = run(["grype", f"registry:{ref}", "-o", "json", "--quiet"])
    if rc == 0:
        doc = json.loads(so)
        out["grype"] = grype_findings(doc)
        out["grype_db"] = (doc.get("descriptor", {}).get("db", {}) or {}).get("built") or \
            (doc.get("descriptor", {}).get("db", {}).get("status", {}) or {}).get("built", "")
    else:
        out["grype_error"] = (se.strip().splitlines() or ["grype failed"])[-1][:300]
    for attempt in range(TRIVY_TRIES):
        rc, so, se = run(["trivy", "image", "--quiet", "--skip-version-check", "--image-src", "remote",
                          "--scanners", "vuln", "--list-all-pkgs", "--format", "json", ref])
        # Two Trivys share one cache; the second waits a while for its lock, then gives up
        # ("cache may be in use by another process": 3 of 270 images, run 45558189314).
        if rc == 0 or "cache may be in use" not in se:
            break
        time.sleep(TRIVY_WAIT * (attempt + 1))
    if rc == 0:
        out["trivy"], out["apk"] = trivy_findings(json.loads(so))
    else:
        out["trivy_error"] = (se.strip().splitlines() or ["trivy failed"])[-1][:300]
    signer = signers.get(origin)
    if signer:
        issuer, subject = signer
        rc, so, se = run(["cosign", "verify", "--certificate-oidc-issuer-regexp", issuer,
                          "--certificate-identity-regexp", subject, ref], timeout=600)
        out["signed"] = rc == 0
        out["signer"] = signer_of(so, se)
        rc, _, _ = run(["cosign", "verify-attestation", "--type", "spdxjson", "--certificate-oidc-issuer-regexp", issuer,
                        "--certificate-identity-regexp", subject, ref], timeout=600)
        out["sbom"] = rc == 0
    return out


def cmd_scan(a):
    org = os.environ.get("CHAINGUARD_ORG", "")
    build = build_registry() or ""
    signers = {"chainguard": (SIGNATURE_ISSUERS, signature_identity(os.environ.get("CHAINGUARD_ORG_ID")))}
    if build:
        subject = build_signature_identity(build, build_signer())
        if subject:
            signers["built-here"] = (BUILD_SIGNATURE_ISSUER, subject)
    os.makedirs(a.out, exist_ok=True)
    todo = {}
    for _, report in load_nv(a.nv):
        for row in report["images"]:
            ref = image_ref(row)
            todo.setdefault(ref, row.get("origin", "other"))
    todo = {ref: o for ref, o in todo.items() if not os.path.exists(os.path.join(a.out, scan_key(ref) + ".json"))}
    print(f"{len(todo)} image(s) to scan with grype, Trivy{' and cosign' if signers else ''} ({org or 'no org'})",
          flush=True)

    def one(item):
        ref, origin = item
        result = scan_one(ref, origin, signers)
        with open(os.path.join(a.out, scan_key(ref) + ".json"), "w", encoding="utf-8") as f:
            json.dump(result, f)
        errs = [k for k in ("grype_error", "trivy_error") if k in result]
        print(f"  {'ok' if not errs else 'PARTLY FAILED'} {ref}"
              + "".join(f"\n    {k}: {result[k]}" for k in errs), flush=True)

    with ThreadPoolExecutor(max(1, a.jobs)) as pool:
        list(pool.map(one, sorted(todo.items())))


# ---- build ---------------------------------------------------------------------------------

def fetch_json(url, timeout=120, attempts=4):
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "bigbang-chainguard-security-report"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if i == attempts - 1:
                raise
            time.sleep(5 * (i + 1))


def secfixes(dbs):
    """{package: {cve-or-alias: fixed version ("0" = not affected)}}, Chainguard's first."""
    out = {}
    for db in dbs:
        for p in db.get("packages", []):
            fixes = out.setdefault(p["pkg"]["name"], {})
            for version, ids in (p["pkg"].get("secfixes") or {}).items():
                for i in ids or []:
                    for one in str(i).split():
                        fixes.setdefault(one, version)
    return out


def _part(s):
    return [(0, int(x), "") if x.isdigit() else (1, 0, x) for x in re.findall(r"\d+|[a-z]+", s.lower())]


def apk_key(version):
    """A sort key for apk versions (1.12.1-r10 < 1.12.1-r11 < 1.13.0-r0)."""
    v = str(version or "")
    base, _, rel = v.partition("-r")
    pre = 0 if re.search(r"_(alpha|beta|pre|rc)", base) else 1
    base = re.sub(r"_(alpha|beta|pre|rc)\d*", "", base)
    return (_part(base), pre, int(rel) if rel.isdigit() else 0)


def advisory(ids, apk, fixes):
    """Chainguard's word on a finding: (status, detail).

    status: not-affected | fixed-here (fixed at or below the installed package: the scanner
    is behind) | fixed-newer (a newer build has the fix) | none (no advisory) | n/a (not an
    apk-based Chainguard image)."""
    if not apk:
        return "n/a", ""
    best = None
    for pkg, installed in sorted(apk.items()):
        known = fixes.get(pkg) or {}
        for i in ids:
            if i not in known:
                continue
            fixed = known[i]
            if fixed == "0":
                return "not-affected", f"{pkg}: Chainguard advisory, not affected"
            if apk_key(installed) >= apk_key(fixed):
                best = best or ("fixed-here", f"{pkg} {installed}: fixed in {fixed}")
            else:
                best = ("fixed-newer", f"{pkg} {installed}: fixed in {fixed} (a newer build)")
    return best or ("none", "")


def epss_scores(cves, batch=80):
    out = {}
    ids = sorted({c for c in cves if c.startswith("CVE-")})
    for i in range(0, len(ids), batch):
        try:
            doc = fetch_json(EPSS + ",".join(ids[i:i + batch]))
        except Exception as e:  # noqa: BLE001 - EPSS is context, not the result
            print(f"EPSS unavailable for a batch: {e}", file=sys.stderr)
            continue
        for d in doc.get("data", []):
            out[d["cve"]] = (float(d.get("epss", 0)), float(d.get("percentile", 0)))
    return out


def norm_pkg(name):
    """One name for a package across scanners: NeuVector says go:golang.org/x/net, the others golang.org/x/net."""
    return re.sub(r"^(go|npm|pypi|maven|gem|cargo|nuget):", "", str(name or "")).lower()


def norm_ver(version):
    return str(version or "").lstrip("v")


def counts(findings, key="severity"):
    return {s: len({(f["id"], norm_pkg(f.get("package"))) for f in findings if f.get(key) == s}) for s in SEVERITIES}


def short_name(image):
    parts = image.split("/")
    return "/".join(parts[2:]) if parts[0] in ("cgr.dev", "ghcr.io") and len(parts) > 2 else image


def assemble(nv, scans, fixes, kev, epss_of, tags=None, md_tags=None):
    """images.json and cves.json rows."""
    images, cves, tags, md_tags = [], [], tags or {}, md_tags or {}
    for mode, report in nv:
        for row in report["images"]:
            ref = image_ref(row)
            sc = scans.get(scan_key(ref), {})
            apk = sc.get("apk") or {}
            last = row["image"].rsplit("/", 1)[-1]
            ref_tag = last.rsplit(":", 1)[1] if ":" in last else ""
            name = short_name(row["image"].rsplit(":", 1)[0] if ref_tag else row["image"])
            tag = row.get("tag") or ref_tag or tags.get(row.get("digest", "")) or md_tags.get(name.rsplit("/", 1)[-1], "")
            deployed = row.get("deployed", True)
            key = f"{mode}/{'deployed' if deployed else 'optional'}/{name}:{tag or row.get('digest', '')[7:19]}"
            origin = ORIGIN.get(row.get("origin"), "RKE2 / other")
            findings = {}

            def add(source, f):
                ids = sorted(set([f["id"]] + list(f.get("aliases") or [])))
                cve = next((i for i in ids if i.startswith("CVE-")), f["id"])
                k = (cve, norm_pkg(f.get("package")), norm_ver(f.get("installed")))
                e = findings.setdefault(k, {"cve": cve, "ids": set(), "package": f.get("package"),
                                            "installed": f.get("installed"), "fixed_in": f.get("fixed_in") or "",
                                            "severity": {}, "link": f.get("link") or ""})
                e["ids"].update(ids)
                e["severity"][source] = f.get("severity")
                e["fixed_in"] = e["fixed_in"] or f.get("fixed_in") or ""

            for v in row.get("vulnerabilities", []):
                add("neuvector", {"id": v["name"], "severity": v.get("severity"), "package": v.get("package_name"),
                                  "installed": v.get("package_version"), "fixed_in": v.get("fixed_version"),
                                  "link": v.get("link")})
            for f in sc.get("grype", []):
                add("grype", f)
            for f in sc.get("trivy", []):
                add("trivy", f)
            open_ch = {"Critical": 0, "High": 0}
            for (cve, _, _), e in sorted(findings.items()):
                pkg = e["package"]
                sev = e["severity"].get("neuvector") or e["severity"].get("grype") or e["severity"].get("trivy")
                status, detail = advisory(sorted(e["ids"]), apk, fixes) if origin != "RKE2 / other" else ("n/a", "")
                is_open = status not in ("not-affected", "fixed-here")
                if is_open and sev in open_ch:
                    open_ch[sev] += 1
                score = epss_of.get(cve)
                cves.append({
                    "id": f"{key}/{cve}/{pkg}", "mode": mode, "deployed": deployed, "image": name, "tag": tag, "origin": origin,
                    "cve": cve, "severity": sev, "package": pkg, "installed": e["installed"], "fixed_in": e["fixed_in"],
                    "neuvector": "yes" if "neuvector" in e["severity"] else "no",
                    "grype": ("yes" if "grype" in e["severity"] else "no") if "grype" in sc else "not scanned",
                    "trivy": ("yes" if "trivy" in e["severity"] else "no") if "trivy" in sc else "not scanned",
                    "chainguard_advisory": status, "advisory_detail": detail, "open": is_open,
                    "kev": cve in kev, "epss": score[0] if score else None, "epss_percentile": score[1] if score else None,
                    "link": e["link"] or (f"https://nvd.nist.gov/vuln/detail/{cve}" if cve.startswith("CVE-") else ""),
                })
            nvc, gc, tc = row["counts"], counts(sc.get("grype", [])), counts(sc.get("trivy", []))
            images.append({
                "key": key, "mode": mode, "deployed": deployed, "image": name, "origin": origin,
                "registry": row["image"].split("/")[0], "tag": tag, "digest": row.get("digest", ""),
                "scanned_by": "NeuVector" if row.get("scan_status") != "failed" else "not scanned by NeuVector",
                "scan_status": row.get("scan_status", ""), "base_os": row.get("base_os", ""),
                "packages_found": row.get("packages"), "containers": row.get("containers", 0),
                "critical": nvc["Critical"], "high": nvc["High"], "medium": nvc["Medium"], "low": nvc["Low"],
                "fixable": row.get("fixable"),
                "grype_critical": gc["Critical"] if "grype" in sc else None, "grype_high": gc["High"] if "grype" in sc else None,
                "trivy_critical": tc["Critical"] if "trivy" in sc else None, "trivy_high": tc["High"] if "trivy" in sc else None,
                "open_critical": open_ch["Critical"], "open_high": open_ch["High"],
                "signed": sc.get("signed"), "signer": sc.get("signer", ""), "sbom": sc.get("sbom"),
                "pinned_by_digest": bool(row.get("digest")) and "@sha256:" in ref,
                "kev": sum(1 for c in cves if c["id"].startswith(key + "/") and c["kev"]),
            })
    return images, cves


# ---- report.html ---------------------------------------------------------------------------

CSS = """
@page { size: Letter; margin: 0.6in 0.55in; }
body { font: 10pt/1.4 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; color: #1b1b1b; margin: 0; }
h1 { font-size: 20pt; margin: 0 0 4pt; } h2 { font-size: 13pt; margin: 18pt 0 6pt; border-bottom: 1px solid #ccc; }
h3 { font-size: 11pt; margin: 12pt 0 4pt; } .sub { color: #555; font-size: 9pt; }
.cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6pt; margin: 8pt 0; }
.card { border: 1px solid #ccc; border-radius: 4pt; padding: 6pt 8pt; } .card b { display: block; font-size: 17pt; }
.card span { color: #555; font-size: 8pt; } .good b { color: #1a7f37; } .bad b { color: #b42318; }
table { border-collapse: collapse; width: 100%; font-size: 8pt; margin: 4pt 0 8pt; page-break-inside: auto; }
th, td { text-align: left; padding: 2pt 4pt; border-bottom: 1px solid #e3e3e3; vertical-align: top; }
th { color: #555; font-weight: 600; } td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; }
tr { page-break-inside: avoid; } .crit { color: #b42318; font-weight: 600; } .yes { color: #b42318; }
.ok { color: #1a7f37; } ul { margin: 2pt 0 6pt 14pt; padding: 0; } li { margin: 2pt 0; }
.break { page-break-before: always; } code { font-size: 8pt; }
"""


def label(r):
    """image and tag, as one cell."""
    return f"{r['image']} {r['tag']}".strip() if r.get("tag") else r["image"]


def esc(v):
    return html.escape("" if v is None else str(v))


def num(v):
    return "–" if v is None else str(v)


def svg_bars(rows, width=640, bar=9):
    """Paired bars per image: NeuVector C+H against C+H still open after Chainguard's advisories."""
    rows = [r for r in rows if (r["critical"] or 0) + (r["high"] or 0) or r["open_critical"] + r["open_high"]]
    rows.sort(key=lambda r: -((r["critical"] or 0) + (r["high"] or 0)))
    if not rows:
        return "<p>No image has a Critical or High finding.</p>"
    top = max((r["critical"] or 0) + (r["high"] or 0) for r in rows) or 1
    left, h = 230, len(rows) * (2 * bar + 8) + 24
    out = [f'<svg width="{width}" height="{h}" xmlns="http://www.w3.org/2000/svg" font-size="8">']
    for i, r in enumerate(rows):
        y = 4 + i * (2 * bar + 8)
        nv, op = (r["critical"] or 0) + (r["high"] or 0), r["open_critical"] + r["open_high"]
        w1, w2 = (width - left - 40) * nv / top, (width - left - 40) * op / top
        out.append(f'<text x="{left - 6}" y="{y + bar + 3}" text-anchor="end">{esc(label(r))}</text>')
        out.append(f'<rect x="{left}" y="{y}" width="{max(w1, 1):.1f}" height="{bar}" fill="#3b6fb6"/>'
                   f'<text x="{left + w1 + 4:.1f}" y="{y + bar - 1}">{nv}</text>')
        out.append(f'<rect x="{left}" y="{y + bar + 1}" width="{max(w2, 1):.1f}" height="{bar}" fill="#c0392b"/>'
                   f'<text x="{left + w2 + 4:.1f}" y="{y + 2 * bar}">{op}</text>')
    out.append(f'<rect x="{left}" y="{h - 14}" width="9" height="9" fill="#3b6fb6"/>'
               f'<text x="{left + 13}" y="{h - 6}">NeuVector Critical + High</text>'
               f'<rect x="{left + 140}" y="{h - 14}" width="9" height="9" fill="#c0392b"/>'
               f'<text x="{left + 153}" y="{h - 6}">still open after Chainguard\'s advisories</text></svg>')
    return "".join(out)


def totals(rows):
    t = {k: 0 for k in ("images", "critical", "high", "open_critical", "open_high", "grype_critical", "grype_high",
                        "trivy_critical", "trivy_high", "signed", "sbom", "checked", "kev")}
    for r in rows:
        t["images"] += 1
        for k in ("critical", "high", "open_critical", "open_high", "grype_critical", "grype_high",
                  "trivy_critical", "trivy_high", "kev"):
            t[k] += r[k] or 0
        if r["signed"] is not None:
            t["checked"] += 1
            t["signed"] += bool(r["signed"])
            t["sbom"] += bool(r["sbom"])
    return t


def image_table(rows):
    out = ["<table><tr><th>Image</th><th>Tag</th><th>Origin</th><th class=n>NV Crit</th><th class=n>NV High</th>"
           "<th class=n>grype C/H</th><th class=n>Trivy C/H</th><th class=n>Open C/H</th><th>Signed</th><th>SBOM</th></tr>"]
    for r in sorted(rows, key=lambda r: (r["origin"] != "Chainguard", -(r["open_critical"] * 100 + r["open_high"]),
                                         -((r["critical"] or 0) * 100 + (r["high"] or 0)), r["image"])):
        sig = "–" if r["signed"] is None else ("yes" if r["signed"] else "<span class=yes>NO</span>")
        sb = "–" if r["sbom"] is None else ("yes" if r["sbom"] else "<span class=yes>NO</span>")
        crit = f'<span class=crit>{r["critical"]}</span>' if r["critical"] else num(r["critical"])
        out.append(f"<tr><td>{esc(r['image'])}</td><td>{esc(r['tag'])}</td><td>{esc(r['origin'])}</td>"
                   f"<td class=n>{crit}</td><td class=n>{num(r['high'])}</td>"
                   f"<td class=n>{num(r['grype_critical'])}/{num(r['grype_high'])}</td>"
                   f"<td class=n>{num(r['trivy_critical'])}/{num(r['trivy_high'])}</td>"
                   f"<td class=n>{r['open_critical']}/{r['open_high']}</td><td>{sig}</td><td>{sb}</td></tr>")
    return "".join(out) + "</table>"


ADVISORY = {"not-affected": "not affected", "fixed-here": "fixed in this build", "fixed-newer": "fix in a newer build",
            "none": "no advisory", "n/a": "–"}


def finding_table(rows):
    out = ["<table><tr><th>Image</th><th>CVE</th><th>Sev</th><th>Package</th><th>Installed</th><th>NV</th>"
           "<th>grype</th><th>Trivy</th><th>Chainguard advisory</th><th>KEV</th><th class=n>EPSS</th></tr>"]
    for c in rows:
        epss = "–" if c["epss"] is None else f"{c['epss'] * 100:.2f}%"
        out.append(f"<tr><td>{esc(label(c))}</td><td>{esc(c['cve'])}</td>"
                   f"<td class={'crit' if c['severity'] == 'Critical' else ''}>{esc(c['severity'])}</td>"
                   f"<td>{esc(c['package'])}</td><td>{esc(c['installed'])}</td><td>{c['neuvector']}</td>"
                   f"<td>{c['grype']}</td><td>{c['trivy']}</td><td>{esc(ADVISORY.get(c['chainguard_advisory'], ''))}</td>"
                   f"<td>{'<span class=yes>YES</span>' if c['kev'] else 'no'}</td><td class=n>{epss}</td></tr>")
    return "".join(out) + "</table>"


def card(value, label, good=None):
    cls = "" if good is None else (" good" if good else " bad")
    return f'<div class="card{cls}"><b>{esc(value)}</b><span>{esc(label)}</span></div>'


def report_html(images, cves, scan):
    s = scan
    parts = [f"<!DOCTYPE html><html><head><meta charset=utf-8><title>Security report: Big Bang {esc(s['bigbang'])} "
             f"on Chainguard images</title><style>{CSS}</style></head><body>",
             f"<h1>Security report: Big Bang {esc(s['bigbang'])} on Chainguard images</h1>",
             f"<p class=sub>Scanned {esc(s['scanned'])}. {esc(s['cluster'])}. NeuVector CVE database "
             f"{esc(s['neuvector_db_version'])} ({esc(s['neuvector_db_built'])}); grype database {esc(s['grype_db_built'])}. "
             f"Run: {esc(s['run'])}</p>"]
    for mode in ("standard", "fips"):
        dep = [r for r in images if r["mode"] == mode and r["deployed"] and r["origin"] != "RKE2 / other"]
        if not dep:
            continue
        t = totals(dep)
        ch = [c for c in cves if c["mode"] == mode and c["deployed"] and c["origin"] != "RKE2 / other"
              and c["severity"] in ("Critical", "High")]
        open_ch = [c for c in ch if c["open"]]
        kev = [c for c in ch if c["kev"]]
        parts.append(f"<h2>{'Standard' if mode == 'standard' else 'FIPS'} images: what the deployment ran</h2>")
        parts.append('<div class="cards">' + card(t["images"], "Chainguard and built images running")
                     + card(f"{t['critical']} / {t['high']}", "NeuVector Critical / High")
                     + card(f"{t['open_critical']} / {t['open_high']}", "Critical / High still open after Chainguard's advisories",
                            t["open_critical"] + t["open_high"] == 0)
                     + card(len(kev), "on CISA's Known Exploited list", len(kev) == 0)
                     + card(f"{t['grype_critical']} / {t['grype_high']}", "grype Critical / High")
                     + card(f"{t['trivy_critical']} / {t['trivy_high']}", "Trivy Critical / High")
                     + card(f"{t['signed']} of {t['checked']}", "carry the expected signature", t["signed"] == t["checked"])
                     + card(f"{t['sbom']} of {t['checked']}", "carry a signed SBOM", t["sbom"] == t["checked"]) + "</div>")
        by = {}
        for c in ch:
            by[c["chainguard_advisory"]] = by.get(c["chainguard_advisory"], 0) + 1
        parts.append("<ul><li><b>Critical and High findings from any scanner, by Chainguard's advisory:</b> "
                     + ", ".join(f"{n} {ADVISORY.get(k, k)}" for k, n in sorted(by.items(), key=lambda x: -x[1]))
                     + ".</li>")
        top_epss = max((c["epss"] or 0 for c in ch), default=0)
        parts.append(f"<li><b>Exploitation:</b> {len(kev)} of {len(ch)} Critical and High findings are on CISA's KEV list; "
                     f"the highest EPSS score among them is {top_epss * 100:.2f}% (the chance of exploitation in the next "
                     "30 days, FIRST.org).</li>")
        rk = totals([r for r in images if r["mode"] == mode and r["deployed"] and r["origin"] == "RKE2 / other"])
        parts.append(f"<li><b>For contrast,</b> RKE2's own {rk['images']} image(s) (the Kubernetes distribution, not Big Bang "
                     f"or Chainguard) carry {rk['critical']} Critical and {rk['high']} High in NeuVector.</li></ul>")
        parts.append("<h3>Critical and High per image: NeuVector against what is still open</h3>")
        parts.append(svg_bars(dep))
        parts.append("<h3>Findings still open</h3>")
        parts.append(finding_table(sorted(open_ch, key=lambda c: (c["severity"] != "Critical", c["image"], c["cve"])))
                     if open_ch else "<p class=ok>None: Chainguard's advisories cover every Critical and High finding.</p>")
    # optional packages
    for mode in ("standard", "fips"):
        opt = [r for r in images if r["mode"] == mode and not r["deployed"]]
        if not opt:
            continue
        t = totals(opt)
        och = [c for c in cves if c["mode"] == mode and not c["deployed"] and c["open"] and c["severity"] in ("Critical", "High")]
        parts.append(f"<h2 class=break>{'Standard' if mode == 'standard' else 'FIPS'}: Big Bang's optional packages "
                     "(not deployed, scanned by reference)</h2>")
        parts.append(f"<p>{t['images']} pinned images that the default packages don't run: GitLab, Vault, Mattermost, "
                     "Harbor, SonarQube and the rest. The same NeuVector scanned each one by reference at its pinned digest. "
                     f"NeuVector: {t['critical']} Critical, {t['high']} High; still open after Chainguard's advisories: "
                     f"{t['open_critical']} Critical, {t['open_high']} High; {t['kev']} on the KEV list.</p>")
        parts.append("<h3>Findings still open</h3>")
        parts.append(finding_table(sorted(och, key=lambda c: (c["severity"] != "Critical", c["image"], c["cve"])))
                     if och else "<p class=ok>None.</p>")
    # supply chain
    dep = [r for r in images if r["deployed"] and r["origin"] != "RKE2 / other"]
    t = totals(dep + [r for r in images if not r["deployed"]])
    pinned = sum(1 for r in dep if r["pinned_by_digest"])
    parts.append("<h2>Supply chain</h2><ul>"
                 f"<li><b>Signatures:</b> {t['signed']} of {t['checked']} Chainguard and built images carry the expected keyless "
                 "cosign signature (Chainguard's, under the org's identity; builds.yml's for images built here).</li>"
                 f"<li><b>SBOMs:</b> {t['sbom']} of {t['checked']} carry a signed SPDX SBOM attestation.</li>"
                 f"<li><b>Pinning:</b> {pinned} of {len(dep)} deployed Chainguard and built images ran by digest. "
                 "Only RKE2's own images run by tag.</li>"
                 "<li><b>Admission:</b> the installer's Kyverno policy admits only images with those signatures.</li></ul>")
    unsigned = [r for r in images if r["signed"] is False or r["sbom"] is False]
    if unsigned:
        parts.append("<h3>Without the expected signature or SBOM</h3><table><tr><th>Mode</th><th>Image</th><th>Signed</th>"
                     "<th>SBOM</th></tr>" + "".join(f"<tr><td>{r['mode']}</td><td>{esc(r['image'])}</td><td>{r['signed']}</td>"
                                                   f"<td>{r['sbom']}</td></tr>" for r in unsigned) + "</table>")
    # per image
    for mode in ("standard", "fips"):
        rows = [r for r in images if r["mode"] == mode and r["deployed"]]
        if rows:
            parts.append(f"<h2 class=break>Every image the {'standard' if mode == 'standard' else 'FIPS'} deployment ran</h2>"
                         "<p class=sub>Open C/H: Critical / High still open after Chainguard's advisories. "
                         "A dash means that check does not apply.</p>" + image_table(rows))
    for mode in ("standard", "fips"):
        rows = [r for r in images if r["mode"] == mode and not r["deployed"]]
        if rows:
            parts.append(f"<h2 class=break>Every optional-package image ({mode})</h2>" + image_table(rows))
    parts.append("<h2 class=break>How this was measured</h2><ul>"
                 "<li><b>Deployment.</b> Big Bang's default packages, installed with bb-install.py on RKE2 with the CIS profile "
                 "(Pod Security restricted) in one VM; standard images in one job and FIPS in another.</li>"
                 "<li><b>NeuVector.</b> The NeuVector that Big Bang deploys scanned every running container; its own images, "
                 "and every pinned image of the optional packages, were scanned by reference at the pinned digest.</li>"
                 "<li><b>grype and Trivy</b> scanned the same digests from the registry. Both read Chainguard's security data; "
                 "NeuVector does not.</li>"
                 "<li><b>Chainguard's advisories</b> (packages.cgr.dev and packages.wolfi.dev security databases) say, per "
                 "package, which CVEs do not affect Chainguard's build and which build fixed each one. A finding counts as "
                 "open unless the advisory for a package in that image says not affected, or fixed at or below the "
                 "installed version.</li>"
                 "<li><b>KEV</b> is CISA's Known Exploited Vulnerabilities catalog; <b>EPSS</b> is FIRST.org's estimate of "
                 "the chance a CVE is exploited in the next 30 days.</li>"
                 "<li><b>Signatures and SBOMs</b> were checked with cosign verify and verify-attestation against the "
                 "expected keyless identities.</li>"
                 "<li>RKE2's own images (rancher/hardened-*) come with the Kubernetes distribution and are shown for "
                 "contrast; Chainguard's advisories don't cover them.</li></ul>")
    parts.append("</body></html>")
    return "\n".join(parts)


def summary_md(images, cves):
    lines = ["# Security report", "", "| Mode | Images | NeuVector C/H | Open C/H | grype C/H | Trivy C/H | KEV | Signed | SBOM |",
             "|---|---|---|---|---|---|---|---|---|"]
    for mode in ("standard", "fips"):
        for deployed in (True, False):
            rows = [r for r in images if r["mode"] == mode and r["deployed"] == deployed and r["origin"] != "RKE2 / other"]
            if not rows:
                continue
            t = totals(rows)
            lines.append(f"| {mode}{'' if deployed else ' (optional, not deployed)'} | {t['images']} | {t['critical']}/{t['high']} "
                         f"| {t['open_critical']}/{t['open_high']} | {t['grype_critical']}/{t['grype_high']} "
                         f"| {t['trivy_critical']}/{t['trivy_high']} | {t['kev']} | {t['signed']}/{t['checked']} "
                         f"| {t['sbom']}/{t['checked']} |")
    return "\n".join(lines) + "\n"


def cmd_build(a):
    nv = load_nv(a.nv)
    if not nv:
        raise SystemExit("no nv-report.json in " + ", ".join(a.nv))
    scans = {}
    for f in os.listdir(a.scans) if os.path.isdir(a.scans) else []:
        if f.endswith(".json"):
            scans[f[:-5]] = json.load(open(os.path.join(a.scans, f), encoding="utf-8"))
    print("Chainguard and Wolfi security databases", flush=True)
    fixes = secfixes([fetch_json(u, timeout=300) for u in SECDBS])
    print("CISA KEV", flush=True)
    kev = {v["cveID"] for v in fetch_json(KEV).get("vulnerabilities", [])}
    ids = set()
    for _, r in nv:
        for row in r["images"]:
            ids.update(v["name"] for v in row.get("vulnerabilities", []))
    for sc in scans.values():
        for f in sc.get("grype", []) + sc.get("trivy", []):
            ids.update(f.get("aliases") or [f["id"]])
    print(f"EPSS for {len(ids)} IDs", flush=True)
    epss = epss_scores(ids)
    images, cves = assemble(nv, scans, fixes, kev, epss, pin_tags(), images_md_tags())
    first = nv[0][1]
    scan = [{
        "bigbang": first.get("bigbang"), "run": first.get("run", ""),
        "cluster": "RKE2 with the CIS profile (Pod Security restricted), one KVM VM, Big Bang's default packages "
                   "installed with bb-install.py",
        "neuvector_db_version": first.get("cvedb", {}).get("version"), "neuvector_db_built": first.get("cvedb", {}).get("created"),
        "grype_db_built": next((s.get("grype_db") for s in scans.values() if s.get("grype_db")), ""),
        "scanned": time.strftime("%Y-%m-%d", time.gmtime()),
        "kev_entries": len(kev),
    }]
    os.makedirs(a.out, exist_ok=True)
    for name, rows in (("images", images), ("cves", cves), ("scan", scan)):
        with open(os.path.join(a.out, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=0)
    with open(os.path.join(a.out, "report.html"), "w", encoding="utf-8") as f:
        f.write(report_html(images, cves, scan[0]))
    md = summary_md(images, cves)
    with open(os.path.join(a.out, "summary.md"), "w", encoding="utf-8") as f:
        f.write(md)
    print(md)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pins", help="every pinned org and built image reference")
    p.add_argument("--mode", choices=("standard", "fips"), default="standard")
    p.set_defaults(fn=cmd_pins)
    p = sub.add_parser("scan", help="grype, Trivy and cosign for each image in the NeuVector reports")
    p.add_argument("--nv", action="append", required=True, help="a directory holding nv-report.json")
    p.add_argument("--out", required=True)
    p.add_argument("--jobs", type=int, default=2)
    p.set_defaults(fn=cmd_scan)
    p = sub.add_parser("build", help="images.json, cves.json, scan.json, report.html, summary.md")
    p.add_argument("--nv", action="append", required=True)
    p.add_argument("--scans", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_build)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
