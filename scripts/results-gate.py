#!/usr/bin/env python3
"""Fail when this Big Bang version has no recorded compat matrix.

  results-gate.py                     every pull request and push to main (ci.yml)
  results-gate.py --release           a release PR: the large tests too
  results-gate.py --base origin/main  --release when BIGBANG_VERSION differs from that ref's

Reads BIGBANG_VERSION and docs/test-results.json. Exits 1 when a described test has
no standard-mode pass on that version, or when one of the release-upgrade and migration
tests has no pass. A skipped run is not a pass. Preview results (a Big Bang branch
before its release) do not count. Large tests (`large: true`, GitLab) and
`weekly_and_release: true` tests (an upgrade from an old release) are required only with
--release: a release PR runs them (compat-plan.py) and records them before it merges, while
every other pull request, which never runs them, stays mergeable on main's matrix. A
`weekly_and_release` test with no standard pass on any version yet is still bedding in and
is not required even then. `registry1: true` tests (vault-upgrade) need Iron Bank
credentials this repo does not store, so they stay outside this gate and do not count as a
pass. Nor do the VM tests' rows (`rke2-*` from rke2.yml, `fips-kernel-*` from
fips-kernel.yml): they run on their own schedule and runner, not in compat.yml's matrix.

Two reasons, printed apart: "compat results are missing" (a test has no result on this
version yet: expected until the matrix is recorded; scripts/ci-triage.py says EXPECTED) and
"compat results FAILED: <test> (<run>)" (a test's newest standard result on this version is a
failure: a real failure, never expected). test-results.py keeps only the newest result per
test and mode, so a failure that replaced a pass shows up here, not as a gap.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIRED = (
    "release-upgrade-keycloak",
    "release-upgrade-mattermost",
    "installer-upgrade-to",
    "keycloak-migration",
    "mattermost-migration",
)
# Rows test-results.py records from the VM workflows (rke2.yml: rke2-default; fips-kernel.yml:
# fips-kernel-<test>). They describe a weekly VM run on a billed runner, never this gate's matrix.
VM_ROWS = re.compile(r"^(rke2|fips-kernel)-")


def flagged(flag, root=ROOT):
    """Compat test names whose `test:` block sets `flag: true`."""
    found = set()
    for path in glob.glob(os.path.join(root, "compat", "*.yaml")):
        text = open(path, encoding="utf-8").read()
        if re.search(rf"^  {re.escape(flag)}: true", text, re.M):
            found.add(os.path.basename(path)[:-5])
    return found


def large_tests(root=ROOT):
    """Compat tests marked `large: true` (GitLab): weekly, not part of the version gate."""
    return flagged("large", root)


def release_only(root=ROOT):
    """Tests required only on a release PR (--release): `large: true` and
    `weekly_and_release: true`. Other pull requests don't run them (compat-plan.py)."""
    return flagged("large", root) | flagged("weekly_and_release", root)


def bedding_in(data, root=ROOT):
    """`weekly_and_release` tests with no standard pass on any Big Bang version yet: a new
    test is not required until it has passed once, so adding one can't turn the gate red."""
    ever = {r.get("test") for r in data.get("results", [])
            if not r.get("preview") and r.get("result") == "pass" and r.get("mode") == "standard"}
    return flagged("weekly_and_release", root) - ever


def passed_standard(version, data):
    """Test names with a standard-mode pass on `version`. Skips and previews do not count."""
    return {r["test"] for r in data.get("results", [])
            if r.get("bigbang") == version and not r.get("preview")
            and r.get("result") == "pass" and r.get("mode") == "standard"}


def failed_standard(version, data, tests):
    """[(test, run_url)] for each of `tests` whose newest standard result on `version` is a
    failure. Previews do not count; a skip is not a failure (it stays a gap)."""
    newest = {}
    for r in data.get("results", []):
        if (r.get("bigbang") == version and not r.get("preview") and r.get("mode") == "standard"
                and r.get("test") in tests):
            if r["test"] not in newest or r.get("finished", "") > newest[r["test"]].get("finished", ""):
                newest[r["test"]] = r
    return [(t, r.get("run_url", "")) for t, r in sorted(newest.items()) if r.get("result") == "fail"]


def gaps(version, data, large, outside=()):
    """(required tests with no pass, other described tests with no pass).

    `large` and `outside` (registry1 tests) are not required, nor are the VM tests' rows
    (VM_ROWS). A skip is not a pass for anything that remains.
    """
    got = passed_standard(version, data)
    required = [t for t in REQUIRED if t not in got]
    skip = set(large) | set(outside)
    described = [t["test"] for t in data.get("tests", [])
                 if t.get("test") not in skip and not VM_ROWS.match(t.get("test") or "")]
    matrix = [t for t in described if t not in got and t not in REQUIRED]
    return required, matrix


def missing(version, data, root=ROOT, release=False):
    """gaps() as the gate applies it: registry1 tests outside; large and weekly_and_release
    tests outside too unless `release` (then only a weekly_and_release test bedding in)."""
    outside = flagged("registry1", root) | (bedding_in(data, root) if release else set())
    return gaps(version, data, set() if release else release_only(root), outside)


def base_version(ref, root=ROOT):
    """BIGBANG_VERSION at git `ref`, or None when git can't read it."""
    p = subprocess.run(["git", "-C", root, "show", f"{ref}:BIGBANG_VERSION"], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def main(argv=()):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--release", action="store_true",
                    help="a release PR: also require the large and weekly_and_release tests")
    ap.add_argument("--base", help="a pull request's base ref: --release when its BIGBANG_VERSION differs")
    a = ap.parse_args(list(argv))
    version = open(os.path.join(ROOT, "BIGBANG_VERSION"), encoding="utf-8").read().strip()
    data = json.load(open(os.path.join(ROOT, "docs", "test-results.json"), encoding="utf-8"))
    release = a.release
    if a.base and not release:
        base = base_version(a.base, ROOT)
        if base is None:
            # Never a reason to fail: the ordinary gate still applies.
            print(f"can't read BIGBANG_VERSION at {a.base}: checking as an ordinary pull request", file=sys.stderr)
        release = base is not None and base != version
    if release:
        print(f"{version}: a new Big Bang version: the large and weekly_and_release tests are required too")
    required, matrix = missing(version, data, ROOT, release)
    if not required and not matrix:
        print(f"{version}: compat matrix recorded")
        return 0
    # A failed test has no pass either. It is reported as FAILED, not as missing, so
    # ci-triage.py never reads a real failure as the expected missing matrix.
    failures = failed_standard(version, data, set(required) | set(matrix))
    for test, url in failures:
        print(f"{version}: compat results FAILED: {test} ({url or 'no run recorded'})", file=sys.stderr)
    bad = {t for t, _ in failures}
    required = [t for t in required if t not in bad]
    matrix = [t for t in matrix if t not in bad]
    if not required and not matrix:
        return 1
    print(f"{version}: compat results are missing", file=sys.stderr)
    if required:
        print("  no standard pass for: " + ", ".join(required), file=sys.stderr)
    if matrix:
        shown = ", ".join(matrix[:25])
        more = f" (+{len(matrix) - 25} more)" if len(matrix) > 25 else ""
        print(f"  no standard pass for {len(matrix)} other tests: {shown}{more}", file=sys.stderr)
    print("  a SKIPPED run is not a pass. Record results with scripts/test-results.py update.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
