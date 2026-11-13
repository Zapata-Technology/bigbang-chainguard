#!/usr/bin/env python3
"""Run this repo against Big Bang's next release before it ships: Big Bang's development
branch (or any ref), through discovery, conversion, the chart scan and a render of every
kind test's values. Prints a Markdown report of what would break, so a new release is a
known list of work instead of a surprise.

  bb-preview.py [--ref master] [--dir work-preview] [-o report.md]

Needs helm, git and network access to repo1.dso.mil; with chainctl logged in it also
checks that the org has a tag for every new image (otherwise conversion runs offline from
.tags.json and says so). Changes no file in the repo: generated files go under --dir.

What it reports:
  - the chart's kubeVersion against the Kubernetes this repo renders and tests with
  - images added and removed against discovery/images.json, per package
  - packages that don't render (with every package on: discovery/render-inputs.yaml)
  - bb-convert's problems (unmapped images, no org tag, missing digest pins)
  - bb-scan findings not in mapping.yaml scan_reviewed
  - each compat/*.yaml test's values rendered against the new chart and package charts
    (a values key the new charts refuse, like Big Bang 4's kyverno-policies schema)
Exit 0 nothing to do, 1 something would break. .github/workflows/bigbang-preview.yml runs
it daily and keeps one issue per ref up to date with the report.

  bb-preview.py --ref release-4.2.0 --kind-results DIR --kind-outcome failure \\
                --kind-run URL --body issue.md [--have-issue]

For a release branch the workflow also deploys a few kind tests on it (compat.yml with
bigbang_ref); this puts their results in the issue body file (a marked section) and prints
what to do with the issue: create, edit, close or none.
"""
import argparse
import glob
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from bbcommon import bigbang_packages, deep_merge, is_bigbang4  # noqa: E402

PY = sys.executable


def run(*cmd, timeout=3600):
    p = subprocess.run([PY, *cmd], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=ROOT, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def kube_problem(chart_yaml, kube_version):
    """A sentence when the chart's kubeVersion needs a newer Kubernetes than kube_version."""
    want = (chart_yaml or {}).get("kubeVersion") or ""
    m = re.search(r">=\s*v?(\d+)\.(\d+)", want)
    have = tuple(int(x) for x in kube_version.split(".")[:2])
    if m and (int(m.group(1)), int(m.group(2))) > have:
        return (f"The chart needs Kubernetes {want}; this repo renders and tests with {kube_version} "
                "(bb-images.KUBE_VERSION, the kind node image, the FIPS VM's k3s, bb-install.MIN_KUBERNETES).")
    return None


def image_changes(old, new):
    """{package: (added, removed)} between two discovery results (bb-images.py output)."""
    out = {}
    pk_old, pk_new = old.get("packages", {}), new.get("packages", {})
    for name in sorted(set(pk_old) | set(pk_new)):
        a = set((pk_old.get(name) or {}).get("rendered", []))
        b = set((pk_new.get(name) or {}).get("rendered", []))
        strip = lambda s: {re.sub(r"[:@].*$", "", x) for x in s}  # noqa: E731
        added, removed = sorted(strip(b) - strip(a)), sorted(strip(a) - strip(b))
        if name not in pk_old:
            added = ["(new package) " + x for x in sorted(strip(b))] or ["(new package)"]
        if name not in pk_new:
            removed = ["(package gone)"]
        if added or removed:
            out[name] = (added, removed)
    return out


def problem_lines(text, pattern):
    return [ln.rstrip() for ln in text.splitlines() if re.search(pattern, ln)]


# bb-convert.py's failing blocks: a heading, then one indented line per image.
CONVERT_PROBLEMS = ("Images not in mapping.yaml", "Images with no usable org tag")


def conversion_problems(text):
    """bb-convert.py's reasons for failing: its unmapped and no-org-tag blocks (heading and
    images), and any traceback or error line. Its `wrote <file>` lines are not problems."""
    out, block = [], False
    for ln in text.splitlines():
        if ln.startswith(CONVERT_PROBLEMS):
            block = True
        elif not ln.startswith((" ", "	")):
            block = False
        if block or re.search(r"(?i)traceback|error", ln):
            out.append(ln.rstrip())
    return out


def test_values_for(test, chart_values, test_values):
    """The values a compat test installs with: every package off (bb-images starts with all
    of them on), then the test's enable list (bb-compat.test_values), then its values."""
    vals = {}
    for key in bigbang_packages(chart_values):
        node = vals
        for k in key.split("."):
            node = node.setdefault(k, {})
        node["enabled"] = False
    deep_merge(vals, test_values(chart_values, test.get("enable", [])))
    return deep_merge(vals, test.get("values") or {})


def render_tests(bigbang, pkgs, overlay, chart_values, tests, test_values, jobs=4):
    """{test: errors} from rendering each test's values over the overlay with bb-images."""
    tmp = tempfile.mkdtemp(prefix="bb-preview-")

    def one(item):
        name, test = item
        path = os.path.join(tmp, f"{name}.yaml")
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(test_values_for(test, chart_values, test_values), f)
        out = os.path.join(tmp, f"{name}.json")
        rc, text = run("scripts/bb-images.py", "--bigbang", bigbang, "--pkgs", pkgs,
                       "--values", overlay, "--values", path, "-o", out, timeout=1800)
        if rc or not os.path.exists(out):
            lines = [ln.strip() for ln in text.strip().splitlines()
                     if ln.strip() and "--debug" not in ln and "render failed" not in ln]
            return name, lines[-2:] or [f"bb-images exited {rc}"]
        errors = json.load(open(out, encoding="utf-8")).get("errors") or {}
        return name, [f"{rel}: {str(err).strip().splitlines()[-1][:300]}" for rel, err in sorted(errors.items())]

    with ThreadPoolExecutor(jobs) as pool:
        return {n: e for n, e in pool.map(one, sorted(tests.items())) if e}


def report(ref, sections):
    """Markdown from [(title, lines)]; a section with no lines reads 'nothing'."""
    out = [f"## Big Bang `{ref}` against this repo", ""]
    for title, lines in sections:
        out.append(f"### {title}")
        out += ["```", *lines, "```"] if lines else ["nothing", ""]
        if lines:
            out.append("")
    return "\n".join(out)


# A release branch's issue also carries its kind tests (compat.yml on that ref, called by
# bigbang-preview.yml), between these markers so each day's run replaces its own.
KIND_START, KIND_END = "<!-- kind:start -->", "<!-- kind:end -->"
NOTHING = "**Nothing would break.**"


def kind_results(path):
    """The compat-result JSONs (test-results.py record) under path, errors included."""
    found = []
    for f in sorted(glob.glob(os.path.join(path, "**", "*.json"), recursive=True)):
        try:
            doc = json.load(open(f, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        found += [r for r in (doc if isinstance(doc, list) else [doc])
                  if isinstance(r, dict) and r.get("test") and r.get("mode")]
    return sorted(found, key=lambda r: (r["test"], r["mode"]))


def kind_section(ref, results, outcome, run_url):
    """(Markdown between the kind markers, whether every test passed). `outcome` is the
    called compat.yml's result: a run that failed outside a test (its plan) fails too."""
    failed = [r for r in results if r.get("result") not in ("pass", "skipped")]
    ok = bool(results) and not failed and outcome == "success"
    lines = [KIND_START, f"### Kind tests on `{ref}`", ""]
    if results:
        lines += ["| Test | Mode | Result |", "|---|---|---|"]
        lines += [f"| {r['test']} | {r['mode']} | {str(r.get('result', '?')).upper()} |" for r in results]
    else:
        lines.append(f"No test recorded a result (the kind run's result: {outcome or 'unknown'}).")
    verdict = ("Every kind test passed on this ref." if ok else
               "**A kind test failed on this ref**: `python scripts/ci-triage.py --run <run>`, then the "
               "artifact `compat-<test>-<mode>` (AGENTS.md, \"A compat (kind) test fails\").")
    lines += ["", f"{verdict} Run: {run_url}", KIND_END]
    return "\n".join(lines), ok


def with_kind_section(body, section):
    """body with its kind section replaced by `section`, or `section` appended."""
    i, j = body.find(KIND_START), body.find(KIND_END)
    if 0 <= i < j:
        return body[:i] + section + body[j + len(KIND_END):]
    return (body.rstrip() + "\n\n" if body.strip() else "") + section + "\n"


def kind_action(have_issue, body, ok):
    """What the workflow does with the ref's issue: create, edit, close (the preview found
    nothing and the kind tests passed) or none."""
    if not ok:
        return "edit" if have_issue else "create"
    if not have_issue:
        return "none"
    return "close" if NOTHING in body else "edit"


def kind_report(a):
    """--kind-results: put the kind section in the issue body file; print the action."""
    body = open(a.body, encoding="utf-8").read() if os.path.exists(a.body) else ""
    section, ok = kind_section(a.ref, kind_results(a.kind_results), a.kind_outcome, a.kind_run)
    new = with_kind_section(body, section)
    with open(a.body, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)
    print(section, file=sys.stderr)
    print(kind_action(a.have_issue, body, ok))
    return 0


def load(script):
    """A sibling script (bb-images, bb-compat) as a module: its name has a dash."""
    spec = importlib.util.spec_from_file_location(script.replace("-", "_"), os.path.join(HERE, f"{script}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", default="master", help="Big Bang branch or tag (default: master)")
    ap.add_argument("--dir", default=os.path.join(ROOT, "work-preview"))
    ap.add_argument("-o", "--output", help="also write the report here")
    ap.add_argument("--skip-tests", action="store_true", help="don't render the compat tests' values")
    kind = ap.add_argument_group("kind results (bigbang-preview.yml, after compat.yml ran on --ref)")
    kind.add_argument("--kind-results", metavar="DIR", help="the run's compat-result-* artifacts")
    kind.add_argument("--kind-outcome", default="", help="the compat.yml call's result")
    kind.add_argument("--kind-run", default="", help="the run's URL")
    kind.add_argument("--body", help="the ref's issue body: its kind section is replaced in place")
    kind.add_argument("--have-issue", action="store_true", help="the ref has an open issue")
    a = ap.parse_args()
    if a.kind_results:
        if not a.body:
            ap.error("--kind-results needs --body")
        return kind_report(a)
    d = os.path.abspath(a.dir)
    bigbang, pkgs = os.path.join(d, "bigbang"), os.path.join(d, "pkgs")
    sections, broken = [], False

    rc, text = run("scripts/bb-fetch.py", "--version", a.ref, "--dir", d)
    if rc:
        print(report(a.ref, [("Fetch failed", text.strip().splitlines()[-15:])]))
        return 1
    chart_yaml = yaml.safe_load(open(os.path.join(bigbang, "chart", "Chart.yaml"), encoding="utf-8"))
    chart_values = yaml.safe_load(open(os.path.join(bigbang, "chart", "values.yaml"), encoding="utf-8"))
    k = kube_problem(chart_yaml, load("bb-images").KUBE_VERSION)
    sections.append(("Kubernetes version", [k] if k else []))
    broken |= bool(k)
    layout = "Big Bang 4 layout (packages.<name>)" if is_bigbang4(chart_values) else "Big Bang 3 layout"
    sections.append((f"Chart {chart_yaml.get('version', '?')}, {layout}, "
                     f"{len(bigbang_packages(chart_values))} packages", []))

    discovered = os.path.join(d, "images.json")
    rc, text = run("scripts/bb-images.py", "--bigbang", bigbang, "--pkgs", pkgs,
                   "--values", "discovery/render-inputs.yaml", "-o", discovered, timeout=3600)
    if rc or not os.path.exists(discovered):
        sections.append(("Discovery failed", text.strip().splitlines()[-15:]))
        print(report(a.ref, sections))
        return 1
    new = json.load(open(discovered, encoding="utf-8"))
    old = json.load(open(os.path.join(ROOT, "discovery", "images.json"), encoding="utf-8"))
    changes = image_changes(old, new)
    lines = []
    for pkg, (added, removed) in changes.items():
        lines += [f"{pkg}: + {x}" for x in added] + [f"{pkg}: - {x}" for x in removed]
    sections.append(("Images added or removed", lines))
    errs = [f"{rel}: {str(e).strip().splitlines()[-1][:300]}" for rel, e in sorted((new.get("errors") or {}).items())]
    sections.append(("Packages that don't render", errs))
    broken |= bool(errs)

    out = os.path.join(d, "generated")
    rc, text = run("scripts/bb-convert.py", "--discovered", discovered, "--out", out, timeout=3600)
    conv = conversion_problems(text)
    if rc and not conv:   # failed without saying why: its last lines, minus the files it wrote
        conv = [ln for ln in text.strip().splitlines() if not ln.startswith(("wrote ", "removed "))][-10:]             or [f"bb-convert.py exited {rc}"]
    sections.append(("Conversion problems", conv if rc else []))
    broken |= bool(rc)

    rc, text = run("scripts/bb-scan.py", "--pkgs", pkgs, "--bigbang", bigbang)
    scan = problem_lines(text, r"^(NEW|gone) ")
    sections.append(("Chart scan findings to review", scan))
    broken |= any(s.startswith("NEW") for s in scan)

    overlay = os.path.join(out, "overlays", "values-chainguard.yaml")
    if not a.skip_tests and os.path.exists(overlay):
        compat = load("bb-compat")
        tests, skipped = {}, []
        for path in sorted(glob.glob(os.path.join(ROOT, "compat", "*.yaml"))):
            t = (yaml.safe_load(open(path, encoding="utf-8")) or {}).get("test") or {}
            name = os.path.basename(path)[:-5]
            if t.get("installer"):   # installer tests take a site file, not test values
                continue
            if t.get("values_files"):   # written by the test's setup on the cluster
                skipped.append(name)
                continue
            tests[name] = t
        failed = render_tests(bigbang, pkgs, overlay, chart_values, tests, compat.test_values)
        lines = [f"{name}: {e}" for name, errs in failed.items() for e in errs]
        title = f"Kind tests whose values the new charts refuse ({len(tests)} rendered"
        title += f"; not rendered, their setup writes values files: {', '.join(skipped)})" if skipped else ")"
        sections.append((title, lines))
        broken |= bool(lines)

    text = report(a.ref, sections)
    text += ("\n**Something would break.** Fix it on a branch before the release ships." if broken
             else "\n**Nothing would break.**") + "\n"
    print(text)
    if a.output:
        with open(a.output, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
