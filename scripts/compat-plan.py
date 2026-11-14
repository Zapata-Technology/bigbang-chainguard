#!/usr/bin/env python3
"""Pick the kind tests (and modes) a compat.yml run deploys; prints `matrix=<json>` for
$GITHUB_OUTPUT. Standard library only: compat.yml's plan job has no PyYAML.

  compat-plan.py --event pull_request --changed-from origin/main [--fips]
  compat-plan.py --event workflow_dispatch --tests gitlab,gitlab-runner --fips
  compat-plan.py --event workflow_call --bigbang-ref release-4.2.0

Each kind job costs 8-15 runner minutes and a full run is ~70 of them, so a pull request
runs only the tests its change can affect:

  - named tests (a manual run's `tests` input): exactly those;
  - a Big Bang branch (--bigbang-ref: bigbang-preview.yml's untagged release-X.Y.Z): the
    PREVIEW handful, a smoke test in both modes and the release-to-release upgrade;
  - the weekly schedule: every test, both modes;
  - a pull request (the union of):
    - BIGBANG_VERSION changed: every test that isn't `large: true`, both modes;
    - compat/<test>.yaml changed: that test (large ones too), both modes;
    - compat/support/<file> changed: the tests that use that file, standard;
    - an overlay's package changed beyond its digests: the tests that enable that package,
      in that overlay's mode (values-chainguard-fips.yaml: FIPS; values-object-storage.yaml
      or object-storage.yaml: the tests with values_files, standard);
    - the installer (scripts/bb-install.py, scripts/bbinstall/): the installer tests, standard;
    - anything else that reaches a cluster (the harness, flux/, mapping.yaml, orgs/, digest-only
      overlay changes, this workflow): SMOKE, standard;
    - docs, unit tests, everything else: nothing.
  A pull request's FIPS modes need --fips (compat.yml passes it except on manual runs).

It also prints `bigbang-key` (the Actions cache key of the Big Bang clone the test jobs
share) and `bigbang-version` (what they deploy): BIGBANG_VERSION's tag, or with
--bigbang-ref that ref, keyed by its commit so a branch that moved is fetched again.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_FILE = re.compile(r"^compat/([^/]+)\.yaml$")
# Small tests that still deploy Flux, the overlays' post-renderers and Kyverno's policies,
# plus the packages whose last real bugs were runtime-only (Tempo's null receiver, Keycloak,
# Istio/Kiali, Velero). A digest-only change runs these, standard mode. GitLab stays out:
# it is large.
SMOKE = ["kyverno-enforce", "metrics-server", "object-storage", "keycloak", "kiali", "velero"]
# A Big Bang branch before its release: small tests that deploy Flux, Kyverno's policies and
# the overlays' post-renderers (FIPS too), and the upgrade a site will run from the newest
# release. Each is 10-15 minutes on a free standard runner.
PREVIEW = [("kyverno-enforce", "standard"), ("kyverno-enforce", "fips"), ("metrics-server", "standard"),
           ("release-upgrade", "standard")]
BIGBANG_GIT = "https://repo1.dso.mil/big-bang/bigbang.git"
# A branch or tag name: it becomes a file's contents, a cache key and a git ref.
REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
# What a --bigbang-ref run's cached discovery depends on, besides Big Bang's commit.
DISCOVERY_INPUTS = ("discovery/render-inputs.yaml", "scripts/bb-images.py", "scripts/bb-fetch.py",
                    "scripts/bbcommon.py")
OVERLAY_MODES = {"overlays/values-chainguard.yaml": "standard", "overlays/values-chainguard-fips.yaml": "fips"}
INSTALLER = re.compile(r"^scripts/(bb-install\.py|bbinstall/)")
OBJECT_STORAGE = ("overlays/values-object-storage.yaml", "object-storage.yaml")
# Changes that reach a cluster but name no package: the smoke tests.
CLUSTER = re.compile(r"^(flux/|flux-fips/|mapping\.yaml$|orgs/|scripts/(bb-compat|bbcluster|compat-plan|bbcommon)\.py$"
                     r"|\.github/kind-compat\.yaml$|\.github/workflows/compat\.yml$)")


def test_text(name, root=ROOT):
    return open(os.path.join(root, "compat", f"{name}.yaml"), encoding="utf-8").read()


def large(name, root=ROOT):
    """`large: true` under test: (GitLab): weekly and on demand only."""
    return bool(re.search(r"^  large: true", test_text(name, root), re.M))


def enabled(name, root=ROOT):
    """The packages a test turns on: its `enable: [..]` list, last key part (addons.x -> x)."""
    m = re.search(r"^  enable: \[([^\]]*)\]", test_text(name, root), re.M)
    return {p.strip().split(".")[-1] for p in m.group(1).split(",") if p.strip()} if m else set()


def all_tests(root=ROOT):
    return sorted(os.path.basename(p)[:-5] for p in glob.glob(os.path.join(root, "compat", "*.yaml")))


def package_sections(text):
    """{package: its lines} of a generated overlay (`packages:` then two-space keys)."""
    sections, name = {}, None
    for line in (text or "").splitlines():
        m = re.match(r"^  ([A-Za-z0-9]+):", line)
        if m:
            name = m.group(1)
        if name:
            sections.setdefault(name, []).append(line)
    return sections


def changed_packages(old, new):
    """(packages changed beyond digests, packages whose digests alone changed)."""
    a, b = package_sections(old), package_sections(new)
    real, digests = set(), set()
    for name in set(a) | set(b):
        if a.get(name) == b.get(name):
            continue

        def strip(lines):
            return [re.sub(r"sha256:[0-9a-f]{64}", "sha256:", ln) for ln in lines or []]
        (digests if strip(a.get(name)) == strip(b.get(name)) else real).add(name)
    return real, digests


def pull_request_jobs(changed, fips, root=ROOT, read_base=lambda path: ""):
    """{(test, mode)} for a pull request that changes these files."""
    tests = all_tests(root)
    modes = ["standard", "fips"] if fips else ["standard"]
    jobs = set()

    def add(names, run_modes):
        jobs.update((n, m) for n in names for m in run_modes if m in modes)

    if "BIGBANG_VERSION" in changed:
        add([t for t in tests if not large(t, root)], modes)
    smoke = False
    for path in changed:
        m = TEST_FILE.match(path)
        if m and m.group(1) in tests:
            add([m.group(1)], modes)
        elif path.startswith("compat/support/"):
            base = os.path.basename(path)
            add([t for t in tests if base in test_text(t, root)], ["standard"])
        elif path in OVERLAY_MODES:
            new = open(os.path.join(root, path), encoding="utf-8").read() if os.path.exists(os.path.join(root, path)) else ""
            real, digests = changed_packages(read_base(path), new)
            add([t for t in tests if not large(t, root) and enabled(t, root) & real], [OVERLAY_MODES[path]])
            smoke = smoke or bool(digests)
        elif path in OBJECT_STORAGE:
            add([t for t in tests if not large(t, root) and "values_files" in test_text(t, root)], ["standard"])
        elif INSTALLER.match(path):
            add([t for t in tests if re.search(r"^  installer: true", test_text(t, root), re.M)], ["standard"])
        elif CLUSTER.match(path):
            smoke = True
    if smoke:
        add([t for t in SMOKE if t in tests], ["standard"])
    return jobs


def plan(event, named, changed, fips, root=ROOT, read_base=lambda path: "", bigbang_ref=""):
    """[(test, mode, minutes)] for the run, in test order."""
    modes = ["standard", "fips"] if fips else ["standard"]
    if named:
        jobs = {(n, m) for n in named for m in modes}
    elif bigbang_ref:
        tests = all_tests(root)
        jobs = {(n, m) for n, m in PREVIEW if n in tests}
    elif event == "pull_request":
        jobs = pull_request_jobs(changed, fips, root, read_base)
    else:
        jobs = {(n, m) for n in all_tests(root) for m in modes}
    order = {"standard": 0, "fips": 1}
    return [(n, m, 90 if large(n, root) else 45) for n, m in sorted(jobs, key=lambda j: (j[0], order[j[1]]))]


def ref_commit(ref, ls_remote):
    """The commit `ref` names in `git ls-remote` output for refs/heads/<ref> and
    refs/tags/<ref> (an annotated tag's commit, not the tag object), or None."""
    found = {}
    for line in ls_remote.splitlines():
        sha, _, name = line.partition("\t")
        found[name.strip()] = sha.strip()
    for name in (f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}"):
        if found.get(name):
            return found[name]
    return None


def bigbang_key(ref, commit, root=ROOT):
    """The cache key of a --bigbang-ref run's clone and discovery: the ref, its commit and
    the files the discovery depends on."""
    h = hashlib.sha256()
    for path in DISCOVERY_INPUTS:
        full = os.path.join(root, path)
        h.update(open(full, "rb").read() if os.path.exists(full) else b"")
    return f"bigbang-ref-{ref}-{commit[:12]}-{h.hexdigest()[:12]}"


def bigbang_source(ref, root=ROOT, ls_remote=None):
    """(cache key, version to deploy): BIGBANG_VERSION's, or `ref`'s at its current commit."""
    if not ref:
        version = open(os.path.join(root, "BIGBANG_VERSION"), encoding="utf-8").read().strip()
        return f"bigbang-chart-{version}", version
    if not REF.match(ref):
        raise SystemExit(f"--bigbang-ref {ref!r} is not a branch or tag name")
    commit = ref_commit(ref, (ls_remote or remote_refs)(ref))
    if not commit:
        raise SystemExit(f"Big Bang has no branch or tag {ref}")
    return bigbang_key(ref, commit, root), ref


def remote_refs(ref, attempts=4):
    """git ls-remote for one ref; retries: repo1.dso.mil intermittently refuses GitHub runners."""
    err = ""
    for i in range(attempts):
        p = subprocess.run(["git", "ls-remote", BIGBANG_GIT, f"refs/heads/{ref}", f"refs/tags/{ref}",
                            f"refs/tags/{ref}^{{}}"], capture_output=True, text=True)
        if p.returncode == 0:
            return p.stdout
        err = p.stderr.strip()
        if i < attempts - 1:
            time.sleep(30 * (i + 1))
    raise SystemExit(f"git ls-remote {BIGBANG_GIT} failed: {err}")


def changed_files(base):
    out = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], capture_output=True, text=True,
                         cwd=ROOT, check=True).stdout
    return [ln for ln in out.splitlines() if ln]


def base_reader(base):
    """A function returning a file's text at the merge base with `base` ('' if it didn't exist)."""
    mb = subprocess.run(["git", "merge-base", base, "HEAD"], capture_output=True, text=True, cwd=ROOT,
                        check=True).stdout.strip()

    def read(path):
        p = subprocess.run(["git", "show", f"{mb}:{path}"], capture_output=True, text=True, cwd=ROOT)
        return p.stdout if p.returncode == 0 else ""
    return read


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--event", required=True)
    ap.add_argument("--tests", default="", help="comma-separated compat/<test> names")
    ap.add_argument("--fips", action="store_true", help="also run each test with the FIPS overlay")
    ap.add_argument("--changed-from", help="base ref of a pull request (git diff <ref>...HEAD)")
    ap.add_argument("--bigbang-ref", default="", help="deploy this Big Bang branch or tag, not BIGBANG_VERSION")
    a = ap.parse_args()
    key, version = bigbang_source(a.bigbang_ref)
    named = [t.strip() for t in a.tests.split(",") if t.strip()]
    changed = changed_files(a.changed_from) if a.changed_from else []
    read_base = base_reader(a.changed_from) if a.changed_from else (lambda path: "")
    jobs = plan(a.event, named, changed, a.fips, read_base=read_base, bigbang_ref=a.bigbang_ref)
    print("matrix=" + json.dumps({"include": [{"test": t, "mode": m, "minutes": mins} for t, m, mins in jobs]}))
    print("count=" + str(len(jobs)))
    print("bigbang-key=" + key)
    print("bigbang-version=" + version)


if __name__ == "__main__":
    main()
