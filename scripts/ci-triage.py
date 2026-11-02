#!/usr/bin/env python3
"""Explain a pull request's (or a run's) failed CI jobs: what failed, the lines that say
why, and whether it is a known flake (repo1.dso.mil refusing the runner, a network
timeout) that a re-run fixes or a real failure to look at. For people and AI agents
maintaining this repo: one command instead of reading job logs by hand.

  python scripts/ci-triage.py 86                  # pull request 86
  python scripts/ci-triage.py --run 41390604271   # one workflow run
  python scripts/ci-triage.py 86 --rerun-flakes   # and re-run the jobs that only flaked

A kind test's evidence comes from its artifact (compat-<test>-<mode>: compat.log and
diag/, see docs/testing.md); any other job's from its log. Needs `gh` logged in.
Exit code: 0 nothing failed, 1 a real failure, 2 only flakes.
"""
import argparse
import io
import json
import re
import subprocess
import sys
import zipfile

# Known flakes: outside this repo, and a re-run fixes them.
FLAKES = [
    (re.compile(r"repo1\.dso\.mil.*(can't fetch|unable to clone|Failed to connect|Could not resolve|"
                r"returned error: 5\d\d|HTTP 5\d\d)", re.I), "repo1.dso.mil refused the runner"),
    (re.compile(r"Could not load chart: failed to download archive", re.I), "Flux couldn't download a chart"),
    (re.compile(r"(urlopen error timed out|TimeoutError: timed out|Connection reset by peer|"
                r"TLS handshake timeout|i/o timeout)", re.I), "a network timeout"),
    (re.compile(r"(toomanyrequests|rate limit exceeded|secondary rate limit)", re.I), "a rate limit"),
    (re.compile(r"(curl: \(22\) The requested URL returned error: 5\d\d|HTTP/[\d.]+ 50[234])", re.I),
     "a download's server answered 5xx"),
]
# Lines worth showing from a kind test's compat.log.
EVIDENCE = re.compile(r"^(RESULT|PROBLEM|EXITED|CHECK FAILED|FORBIDDEN LOG|IMAGE POLICY|NOT EXERCISED)"
                      r"|^\d\d:\d\d:\d\d LIVE |^\d\d:\d\d:\d\d source: ")
# Lines worth showing from any other job's log.
ERROR = re.compile(r"##\[error\]|^(ERROR|FAIL|FAILED|Error:)\b|Traceback|AssertionError|^E +", re.I)
ARTIFACT_JOB = re.compile(r"^(?P<test>[\w.-]+) \((?P<mode>standard|fips)\)$")


def gh(*args, binary=False):
    p = subprocess.run(["gh", *args], capture_output=True, text=not binary)
    return p.stdout if p.returncode == 0 else None


def evidence(text, limit=25):
    """The lines that say why a kind test failed."""
    return [ln for ln in text.splitlines() if EVIDENCE.search(ln)][-limit:]


def errors(log, limit=15):
    """The error lines of a job log, timestamps and step names stripped."""
    out = []
    for ln in log.splitlines():
        # `gh run view --log` prefixes "job<TAB>step<TAB>"; the API's job log doesn't.
        ln = re.sub(r"^(?:[^\t]*\t[^\t]*\t)?﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ", "", ln)
        # Skip the workflow's own script, echoed in cyan (raw escape, or ^[ from `gh run view`).
        # Keep known-flake lines too: they decide FLAKE vs FAILED (a curl 500 isn't an "error" line).
        if (ERROR.search(ln) or any(rx.search(ln) for rx, _ in FLAKES)) \
                and "Harden the runner" not in ln and "[36;1m" not in ln:
            out.append(ln[:300])
    return out[-limit:]


def classify(lines):
    """(flake reason or None). A failure is a flake only when a known flake explains it and
    nothing in it points at the repo itself (a failed check, a policy problem, a crash)."""
    text = "\n".join(lines)
    if re.search(r"CHECK FAILED|IMAGE POLICY|FORBIDDEN LOG|OOMKilled|LIVE |stopped early", text):
        return None
    for rx, why in FLAKES:
        if rx.search(text):
            return why
    return None


def failed_jobs(pr=None, run=None):
    """[(job name, run id, job id)] of failed jobs."""
    if pr:
        checks = json.loads(gh("pr", "checks", str(pr), "--json", "name,bucket,link") or "[]")
        found = []
        for c in checks:
            m = re.search(r"/runs/(\d+)/job/(\d+)", c.get("link", ""))
            if c.get("bucket") == "fail" and m:
                found.append((c["name"], m.group(1), m.group(2)))
        return found
    jobs = json.loads(gh("run", "view", str(run), "--json", "jobs") or '{"jobs": []}')["jobs"]
    return [(j["name"], str(run), str(j["databaseId"])) for j in jobs if j.get("conclusion") == "failure"]


def artifact_log(repo, run, name):
    """compat.log from a kind test's artifact, or None."""
    listing = gh("api", f"repos/{repo}/actions/runs/{run}/artifacts?name={name}")
    arts = json.loads(listing)["artifacts"] if listing else []
    if not arts:
        return None
    blob = subprocess.run(["gh", "api", f"repos/{repo}/actions/artifacts/{arts[0]['id']}/zip"],
                          capture_output=True).stdout
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            return z.read("compat.log").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError):
        return None


def triage(repo, jobs):
    """[(job, run, flake reason or None, lines)]."""
    report = []
    for name, run, job in jobs:
        m = ARTIFACT_JOB.match(name)
        log = artifact_log(repo, run, f"compat-{m['test']}-{m['mode']}") if m else None
        lines = evidence(log) if log else errors(gh("api", "--allow-escape-sequences", f"repos/{repo}/actions/jobs/{job}/logs") or "")
        report.append((name, run, classify(lines), lines))
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pr", nargs="?", type=int, help="pull request number")
    ap.add_argument("--run", type=int, help="a workflow run id instead of a pull request")
    ap.add_argument("--rerun-flakes", action="store_true", help="re-run the failed jobs of runs that only flaked")
    a = ap.parse_args()
    if not (a.pr or a.run):
        ap.error("give a pull request number or --run")
    repo = (gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner") or "").strip()
    report = triage(repo, failed_jobs(a.pr, a.run))
    if not report:
        print("no failed jobs")
        return 0
    for name, run, flake, lines in report:
        print(f"\n== {name} (run {run}): {'FLAKE: ' + flake if flake else 'FAILED'}")
        for ln in lines or ["(no evidence found: open the job's log)"]:
            print("   " + ln)
    real = [r for r in report if not r[2]]
    if a.rerun_flakes:
        runs_with_real = {run for _, run, flake, _ in real}
        for run in sorted({run for _, run, flake, _ in report if flake} - runs_with_real):
            print(f"\nre-running the failed jobs of run {run}")
            subprocess.run(["gh", "run", "rerun", run, "--failed"])
    print(f"\n{len(real)} real failure(s), {len(report) - len(real)} flake(s)")
    return 1 if real else 2


if __name__ == "__main__":
    sys.exit(main())
