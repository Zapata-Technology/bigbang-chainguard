#!/usr/bin/env python3
"""Clone Big Bang at BIGBANG_VERSION and every package at the tag Big Bang pins.

  bb-fetch.py [--version X.Y.Z] [--dir work]

Result: <dir>/bigbang and <dir>/pkgs/<values key> (e.g. pkgs/addons.argocd).
Shallow clones from repo1.dso.mil; no credentials needed.
"""
import argparse
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIGBANG = "https://repo1.dso.mil/big-bang/bigbang.git"


def clone(url, tag, dest):
    if os.path.isdir(dest):
        return None
    p = subprocess.run(["git", "-c", "advice.detachedHead=false", "clone", "-q", "--depth", "1",
                        "--branch", tag, url, dest], capture_output=True, text=True)
    return None if p.returncode == 0 else f"{url}@{tag}: {p.stderr.strip()}"


def packages(values):
    """(values key, git repo, tag) for every package, including addons."""
    for k, x in values.items():
        if k == "wrapper":
            continue
        if isinstance(x, dict) and isinstance(x.get("git"), dict) and "repo" in x["git"]:
            yield k, x["git"]["repo"], x["git"]["tag"]
    for k, x in (values.get("addons") or {}).items():
        if isinstance(x, dict) and isinstance(x.get("git"), dict) and "repo" in x["git"]:
            yield f"addons.{k}", x["git"]["repo"], x["git"]["tag"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", default=open(os.path.join(ROOT, "BIGBANG_VERSION")).read().strip())
    ap.add_argument("--dir", default=os.path.join(ROOT, "work"))
    a = ap.parse_args()

    os.makedirs(os.path.join(a.dir, "pkgs"), exist_ok=True)
    err = clone(BIGBANG, a.version, os.path.join(a.dir, "bigbang"))
    if err:
        raise SystemExit(err)
    values = yaml.safe_load(open(os.path.join(a.dir, "bigbang", "chart", "values.yaml")))
    jobs = [(url, tag, os.path.join(a.dir, "pkgs", key)) for key, url, tag in packages(values)]
    with ThreadPoolExecutor(8) as pool:
        errors = [e for e in pool.map(lambda j: clone(*j), jobs) if e]
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"Big Bang {a.version}: {len(jobs)} packages in {a.dir}")


if __name__ == "__main__":
    main()
