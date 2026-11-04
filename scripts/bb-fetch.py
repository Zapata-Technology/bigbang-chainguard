#!/usr/bin/env python3
"""Clone Big Bang at BIGBANG_VERSION and every package at the tag Big Bang pins.

  bb-fetch.py [--version X.Y.Z] [--dir work]

Result: <dir>/bigbang and <dir>/pkgs/<values key> (e.g. pkgs/packages.argocd).
Shallow clones from repo1.dso.mil; no credentials needed.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bbcommon import bigbang_packages  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIGBANG = "https://repo1.dso.mil/big-bang/bigbang.git"


def clone(url, tag, dest, attempts=4):
    """Shallow clone with retries: repo1.dso.mil intermittently refuses GitHub runners."""
    if os.path.isdir(dest):
        return None
    err = ""
    for i in range(attempts):
        p = subprocess.run(["git", "-c", "advice.detachedHead=false", "clone", "-q", "--depth", "1",
                            "--branch", tag, url, dest], capture_output=True, text=True)
        if p.returncode == 0:
            return None
        err = p.stderr.strip()
        shutil.rmtree(dest, ignore_errors=True)
        if i < attempts - 1:
            time.sleep(30 * (i + 1))
    return f"{url}@{tag}: {err}"


def packages(values):
    """(values key, git repo, tag) for every package."""
    for k, x in bigbang_packages(values).items():
        if (x.get("git") or {}).get("repo"):
            yield k, x["git"]["repo"], x["git"]["tag"]


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
