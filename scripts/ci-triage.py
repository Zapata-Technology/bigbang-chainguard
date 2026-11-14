#!/usr/bin/env python3
"""Explain a pull request's (or a run's) failed CI jobs: what failed, the lines that say
why, and whether it is a known flake (repo1.dso.mil refusing the runner, a network
timeout) that a re-run fixes or a real failure to look at. For people and AI agents
maintaining this repo: one command instead of reading job logs by hand.

  python scripts/ci-triage.py 86                  # pull request 86
  python scripts/ci-triage.py --run 41390604271   # one workflow run
  python scripts/ci-triage.py 86 --rerun-flakes   # and re-run the jobs that only flaked
  python scripts/ci-triage.py --flakes            # which jobs needed re-runs lately (30 days)

A kind test's evidence comes from its artifact (compat-<test>-<mode>: compat.log and
diag/, see docs/testing.md); any other job's from its log. Needs `gh` logged in; GitHub is
read through REST only (scripts/ghrest.py), so it works from the scheduled agent's sandbox. A job whose
connections were refused gets the hosts named: in a workflow whose harden-runner blocks egress,
that means a host missing from its allowed-endpoints (a re-run won't help).
Exit code: 0 nothing failed, 1 a real failure, 2 only flakes, 3 GitHub didn't answer,
4 the only failure is results-gate reporting that this Big Bang version has no
recorded compat matrix. That one is expected: do not write pass rows. verify is
the required check. Record real artifacts with scripts/test-results.py update.
A results-gate failure with any other message is a real failure (exit 1).

--flakes counts, per job, the failed attempts of runs that were re-run (by a person or
--rerun-flakes) and passed in the end, over the last --days. A job re-run --threshold times
or more is marked FIX: a flake that keeps coming back costs more than finding its cause.
(compat.yml's own second try after a repo1 refusal happens inside one job and isn't counted.)
Exit 1 when a job reaches the threshold.
"""
import argparse
import datetime
import io
import json
import re
import subprocess
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ghrest  # noqa: E402

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
    # A module download the Go proxy cut off mid-stream during a build.
    (re.compile(r'read "https://proxy\.golang\.org/[^"]+": stream error', re.I),
     "the Go module proxy dropped a download"),
    # Rekor stored the entry, cosign's request timed out, and its retry was refused as a duplicate.
    (re.compile(r"createLogEntryConflict", re.I), "Sigstore's log timed out (duplicate entry on retry)"),
]
# A refused connection. Not a flake: in a workflow whose harden-runner blocks egress, the host
# isn't on its allowed-endpoints list (the runner answers an unlisted name with an address that
# refuses), and a re-run fails the same way.
BLOCKED = re.compile(r'"https?://(?P<host>[A-Za-z0-9.-]+)[^"]*": dial tcp [\d.]+:\d+: connect: connection refused')
# Lines worth showing from a kind test's compat.log.
EVIDENCE = re.compile(r"^(RESULT|PROBLEM|EXITED|CHECK FAILED|FORBIDDEN LOG|IMAGE POLICY|NOT EXERCISED)"
                      r"|^\d\d:\d\d:\d\d LIVE |^\d\d:\d\d:\d\d source: ")
# Lines worth showing from any other job's log.
ERROR = re.compile(r"##\[error\]|^(ERROR|FAIL|FAILED|Error:)\b|Traceback|AssertionError|^E +"
                   # bb-convert / bb-verify findings (CI's "Overlays are current", "Verify ...")
                   r"|^Images not in mapping|^Images with no usable org tag|^ {2}FAIL |is out of date"
                   r"|^ {2}\S+: \S+ -> .*(no .* tag|UNRESOLVED)|^ {2}[\w.-]+/[\w./-]+$", re.I)
ARTIFACT_JOB = re.compile(r"^(?P<test>[\w.-]+) \((?P<mode>standard|fips)\)$")
# ci.yml names the job; the check run uses the `name:` field.
GATE_JOB = re.compile(r"^(results-gate|compat matrix recorded for this Big Bang version)$")
EXPECTED_NOTE = (
    "Do not write pass rows. verify is the required check.",
    "Record real artifacts with: python scripts/test-results.py update",
)


def gh(*args, binary=False):
    """A job log or artifact (None if GitHub has none): the job's evidence is then missing,
    which the report says. Listings that decide what failed go through ghrest, which raises."""
    # gh's output is UTF-8 (job logs carry any byte); Windows' default code page can't decode it.
    p = subprocess.run(["gh", *args], capture_output=True, text=not binary,
                       encoding=None if binary else "utf-8", errors=None if binary else "replace")
    return p.stdout if p.returncode == 0 else None


def evidence(text, limit=25):
    """The lines that say why a kind test failed. A test that stopped before deploying
    (cloning the release to upgrade from while repo1.dso.mil refused the runner) prints no
    PROBLEM or RESULT line: then its error and flake lines, so a repo1 outage reads as a
    FLAKE (automerge re-runs only those) instead of a failure with no evidence."""
    found = [ln for ln in text.splitlines() if EVIDENCE.search(ln)][-limit:]
    return found or errors(text, limit)


def errors(log, limit=15):
    """The error lines of a job log, timestamps and step names stripped."""
    out = []
    for ln in log.splitlines():
        # `gh run view --log` prefixes "job<TAB>step<TAB>"; the API's job log doesn't.
        ln = re.sub(r"^(?:[^\t]*\t[^\t]*\t)?﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ", "", ln)
        # Shorten long URLs (the Go proxy's signed links), so the cut below keeps the error.
        ln = re.sub(r'"(https?://[^/"]+)/[^"]{120,}"', r'"\1/..."', ln)
        # Skip the workflow's own script, echoed in cyan (raw escape, or ^[ from `gh run view`),
        # and harden-runner's own telemetry errors (agent.api.stepsecurity.io), which fail no job.
        # Keep known-flake lines too: they decide FLAKE vs FAILED (a curl 500 isn't an "error" line).
        if (ERROR.search(ln) or BLOCKED.search(ln) or any(rx.search(ln) for rx, _ in FLAKES)) \
                and "Harden the runner" not in ln and "[36;1m" not in ln and "stepsecurity.io" not in ln:
            out.append(ln[:300])
    return out[-limit:]


def blocked(lines):
    """Hosts that refused a job's connections: with egress blocked, the missing allowed-endpoints."""
    return sorted({m["host"] for ln in lines for m in BLOCKED.finditer(ln)})


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


def outcome(name, raw, lines):
    """'expected' when results-gate says the compat matrix is missing, else classify().

    A red gate after the matrix exists, or a crash of the gate script, stays a real
    failure: the log does not contain 'compat results are missing'."""
    if GATE_JOB.match(name) and "compat results are missing" in (raw or ""):
        return "expected"
    return classify(lines)


def gate_lines(raw, lines):
    """The gate's own sentences (errors() drops them) plus what not to do about them."""
    kept = []
    for ln in (raw or "").splitlines():
        ln = ln.strip()[:300]
        if "compat results are missing" in ln or "no standard pass" in ln or "SKIPPED run is not a pass" in ln:
            if ln not in lines and ln not in kept:
                kept.append(ln)
    return lines + kept[:6] + list(EXPECTED_NOTE)


def failed_jobs(pr=None, run=None):
    """[(job name, run id, job id)] of failed jobs."""
    if pr:
        checks = ghrest.checks(ghrest.pr_sha(pr))
        found = []
        for c in checks:
            m = re.search(r"/runs/(\d+)/job/(\d+)", c.get("link", ""))
            # A job cut off by its time limit is "cancel", not "fail": a kind test waiting
            # on repo1.dso.mil ends that way.
            if c.get("bucket") in ("fail", "cancel") and m:
                found.append((c["name"], m.group(1), m.group(2)))
        return found
    jobs = ghrest.api_list(f"actions/runs/{run}/jobs", key="jobs")
    return [(j["name"], str(run), str(j["id"])) for j in jobs
            if j.get("conclusion") in ("failure", "cancelled", "timed_out")]


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


def failed_steps(job):
    """Names of a job's steps that failed or were cancelled ([] if GitHub doesn't say)."""
    try:
        steps = (ghrest.api(f"actions/jobs/{job}") or {}).get("steps") or []
    except ghrest.GhError:
        return []
    return [s["name"] for s in steps if s.get("conclusion") in ("failure", "cancelled", "timed_out")]


def triage(repo, jobs):
    """[(job, run, flake reason or 'expected' or None, lines)]."""
    report = []
    for name, run, job in jobs:
        m = ARTIFACT_JOB.match(name)
        artifact = artifact_log(repo, run, f"compat-{m['test']}-{m['mode']}") if m else None
        if artifact:
            raw, lines = artifact, evidence(artifact)
        else:
            raw = gh("api", "--allow-escape-sequences", f"repos/{repo}/actions/jobs/{job}/logs") or ""
            lines = errors(raw)
        # The failed step's name, from the job itself: it says where to look even when the log
        # has no recognisable line (setup-chainctl and cosign-installer echo their error
        # templates on every run, which reads like a failed login but isn't one).
        steps = failed_steps(job)
        if steps:
            lines = [f"failed step: {', '.join(steps)}"] + lines
        code = outcome(name, raw, lines)
        if code == "expected":
            lines = gate_lines(raw, lines)
        report.append((name, run, code, lines))
    return report


def job_key(name):
    """A job name without the parts that change between runs of the same job."""
    return re.sub(r"\s+\(attempt \d+\)$", "", name).strip()


def rerun_counts(runs, failed_in_attempt, since):
    """{job: [count, last seen]} from runs (`gh run list --json` with attempt, createdAt,
    databaseId, workflowName, conclusion) and failed_in_attempt(run id, attempt) -> job names.
    Only runs that passed after a re-run count: a run still failing is a real failure."""
    counts = {}
    for r in runs:
        if r.get("attempt", 1) < 2 or r.get("conclusion") != "success" or r["createdAt"] < since:
            continue
        for n in range(1, r["attempt"]):
            for job in failed_in_attempt(r["databaseId"], n):
                key = f"{r['workflowName']} / {job_key(job)}"
                c = counts.setdefault(key, [0, r["createdAt"]])
                c[0] += 1
                c[1] = max(c[1], r["createdAt"])
    return counts


def flake_report(counts, threshold):
    """(lines, jobs at or over the threshold)."""
    if not counts:
        return ["no re-run jobs in this period"], []
    lines, fix = [], []
    for key, (n, last) in sorted(counts.items(), key=lambda kv: (-kv[1][0], kv[0])):
        mark = "FIX " if n >= threshold else "    "
        if n >= threshold:
            fix.append(key)
        lines.append(f"  {mark}{n:3}x  {key}  (last {last[:10]})")
    return lines, fix


def flakes(repo, days, threshold):
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = [{"databaseId": r["id"], "attempt": r.get("run_attempt", 1), "workflowName": r["name"],
             "createdAt": r["created_at"], "conclusion": r.get("conclusion")}
            for r in ghrest.api_list(f"actions/runs?created=%3E%3D{since}", key="workflow_runs", limit=500)]

    def failed_in_attempt(run, n):
        jobs = ghrest.api_list(f"actions/runs/{run}/attempts/{n}/jobs", key="jobs")
        return [j["name"] for j in jobs if j.get("conclusion") in ("failure", "cancelled", "timed_out")]

    lines, fix = flake_report(rerun_counts(runs, failed_in_attempt, since), threshold)
    print(f"Jobs re-run in the last {days} days (FIX: {threshold} or more):")
    print("\n".join(lines))
    return 1 if fix else 0


def main():
    try:
        return run_main()
    except ghrest.GhError as e:
        print(f"ERROR: GitHub didn't answer, so nothing was triaged: {e}")
        return 3


def run_main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pr", nargs="?", type=int, help="pull request number")
    ap.add_argument("--run", type=int, help="a workflow run id instead of a pull request")
    ap.add_argument("--rerun-flakes", action="store_true", help="re-run the failed jobs of runs that only flaked")
    ap.add_argument("--flakes", action="store_true", help="which jobs needed re-runs lately")
    ap.add_argument("--days", type=int, default=30, help="--flakes: how far back (default 30)")
    ap.add_argument("--threshold", type=int, default=3, help="--flakes: re-runs that mark a job FIX (default 3)")
    a = ap.parse_args()
    if a.flakes:
        return flakes(ghrest.repo(), a.days, a.threshold)
    if not (a.pr or a.run):
        ap.error("give a pull request number or --run")
    report = triage(ghrest.repo(), failed_jobs(a.pr, a.run))
    if not report:
        print("no failed jobs")
        return 0
    for name, run, code, lines in report:
        if code == "expected":
            status = "EXPECTED"
        elif code:
            status = "FLAKE: " + code
        else:
            status = "FAILED"
        print(f"\n== {name} (run {run}): {status}")
        for ln in lines or ["(no evidence found: open the job's log)"]:
            print("   " + ln)
        hosts = blocked(lines)
        if hosts:
            print(f"   refused: {', '.join(hosts)}. If the workflow's harden-runner blocks egress, add "
                  f"{', '.join(h + ':443' for h in hosts)} to its allowed-endpoints "
                  "(AGENTS.md, the harden-runner trap; scripts/egress-audit.py)")
    real = [r for r in report if r[2] is None]
    expected = [r for r in report if r[2] == "expected"]
    flake_jobs = [r for r in report if r[2] not in (None, "expected")]
    # A missing matrix will not go green on a re-run, so leave those runs alone.
    if a.rerun_flakes and not real and not expected:
        for run in sorted({run for _, run, _, _ in flake_jobs}):
            print(f"\nre-running the failed jobs of run {run}")
            ghrest.rerun_failed(run)
    print(f"\n{len(real)} real failure(s), {len(flake_jobs)} flake(s), "
          f"{len(expected)} expected missing-matrix failure(s)")
    if real:
        return 1
    if expected:
        return 4
    return 2


if __name__ == "__main__":
    sys.exit(main())
