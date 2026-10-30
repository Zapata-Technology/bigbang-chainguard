#!/usr/bin/env python3
"""Run every check this repo has, in the order CI runs them, and print one summary.

  check.py            offline: unit tests (which include mapping validation and the
                      repo rules), and the chart scan when work/ has the charts
  check.py --online   also what needs Chainguard and the Big Bang charts: the overlays
                      are current (bb-convert --check) and render-verify, standard and
                      FIPS. Needs chainctl logged in and work/ from bb-fetch.py.

Run it before every commit. Exit 0 only if every check that ran passed; a check that
can't run (no work/, no chainctl) is reported as skipped, not passed.
"""
import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "work")
PY = sys.executable


def checks(online):
    have_charts = os.path.isdir(os.path.join(WORK, "pkgs")) and os.path.isdir(os.path.join(WORK, "bigbang"))
    no_charts = "no work/ (run scripts/bb-fetch.py)"
    yield "unit tests", [PY, "-m", "pytest", "-q", "tests"], None
    yield "chart scan", [PY, "scripts/bb-scan.py", "--pkgs", "work/pkgs", "--bigbang", "work/bigbang"], \
        None if have_charts else no_charts
    if not online:
        return
    no_chainctl = None if shutil.which("chainctl") else "chainctl not installed"
    yield "overlays current", [PY, "scripts/bb-convert.py", "--check"], no_chainctl
    for mode in ([], ["--fips"]):
        yield "verify " + ("FIPS" if mode else "standard"), \
            [PY, "scripts/bb-verify.py", "--bigbang", "work/bigbang", "--pkgs", "work/pkgs", *mode], \
            None if have_charts else no_charts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--online", action="store_true")
    a = ap.parse_args()
    results = []
    for name, cmd, skip in checks(a.online):
        if skip:
            results.append((name, "SKIP", skip))
            continue
        print(f"\n== {name}: {' '.join(cmd[1:])}", flush=True)
        rc = subprocess.run(cmd, cwd=ROOT).returncode
        # bb-convert --check exits 3 when only newer org patches exist: the nightly's job.
        status = "PASS" if rc == 0 or (name == "overlays current" and rc == 3) else "FAIL"
        results.append((name, status, f"exit {rc}" + (" (newer org patches only)" if rc == 3 else "")))
    print("\n== summary")
    for name, status, note in results:
        print(f"  {status:5} {name:18} {note}")
    return 1 if any(s == "FAIL" for _, s, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main())
