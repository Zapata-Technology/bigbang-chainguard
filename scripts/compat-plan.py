#!/usr/bin/env python3
"""Pick the kind tests (and modes) a compat.yml run deploys; prints `matrix=<json>` for
$GITHUB_OUTPUT. Standard library only: compat.yml's plan job has no PyYAML.

  compat-plan.py --event pull_request --changed-from origin/main [--fips]
  compat-plan.py --event workflow_dispatch --tests gitlab,gitlab-runner --fips

  - named tests (a manual run's `tests` input): exactly those;
  - a pull request that changes only compat/<test>.yaml files: only those tests, large
    ones included (a pull request can log into Chainguard; a branch's manual run can't);
  - any other pull request: every test that isn't `large: true`;
  - the weekly schedule: every test.
"""
import argparse
import glob
import json
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_FILE = re.compile(r"^compat/([^/]+)\.yaml$")


def large(name, root=ROOT):
    """`large: true` under test: (GitLab): weekly and on demand only."""
    text = open(os.path.join(root, "compat", f"{name}.yaml"), encoding="utf-8").read()
    return bool(re.search(r"^  large: true", text, re.M))


def only_changed_tests(changed, root=ROOT):
    """The tests a change touches when it touches nothing but compat/<test>.yaml files
    that still exist, else None (the change can affect every test)."""
    names = []
    for path in changed:
        m = TEST_FILE.match(path)
        if not m:
            return None
        if os.path.exists(os.path.join(root, path)):
            names.append(m.group(1))
    return sorted(names) or None


def plan(event, named, changed, fips, root=ROOT):
    """[(test, mode, minutes)] for the run."""
    names = named or (only_changed_tests(changed, root) if event == "pull_request" else None)
    if not names:
        names = sorted(os.path.basename(p)[:-5] for p in glob.glob(os.path.join(root, "compat", "*.yaml")))
        if event == "pull_request":
            names = [n for n in names if not large(n, root)]
    modes = ["standard", "fips"] if fips else ["standard"]
    return [(n, m, 90 if large(n, root) else 45) for n in names for m in modes]


def changed_files(base):
    out = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], capture_output=True, text=True,
                         cwd=ROOT, check=True).stdout
    return [ln for ln in out.splitlines() if ln]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--event", required=True)
    ap.add_argument("--tests", default="", help="comma-separated compat/<test> names")
    ap.add_argument("--fips", action="store_true", help="also run each test with the FIPS overlay")
    ap.add_argument("--changed-from", help="base ref of a pull request (git diff <ref>...HEAD)")
    a = ap.parse_args()
    named = [t.strip() for t in a.tests.split(",") if t.strip()]
    changed = changed_files(a.changed_from) if a.changed_from else []
    jobs = plan(a.event, named, changed, a.fips)
    print("matrix=" + json.dumps({"include": [{"test": t, "mode": m, "minutes": mins} for t, m, mins in jobs]}))


if __name__ == "__main__":
    main()
