#!/usr/bin/env python3
"""Where this repository stands, in one screen: what to look at first when you (or an AI
agent) start maintaining it. Needs `gh` logged in.

  python scripts/repo-status.py

  - open pull requests, each with its checks (passed / failed / running) and labels;
    the nightly bot PRs (bot/bigbang-update, bot/builds-update) first
  - the latest run of each workflow on main (CI, builds, release, the nightly update, the
    weekly kind tests, automerge) and whether it passed
  - the latest release and the Big Bang version
  - jobs re-run often lately (ci-triage.py --flakes), and, with chainctl logged in, the
    Chainguard plan's room per tier (bb-org-repos.py --quota has the detail)
Then: a failed check -> python scripts/ci-triage.py <pr>; AGENTS.md has the playbooks.

GitHub is read through REST only (scripts/ghrest.py), so this works from the scheduled
agent's cloud sandbox too. A section GitHub didn't answer says ERROR, and the exit code is
then 2: never read a missing answer as "nothing open".
"""
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ghrest  # noqa: E402

ROOT = ghrest.ROOT
WORKFLOWS = ["ci.yml", "builds.yml", "release.yml", "update.yml", "compat.yml", "automerge.yml", "cve-report.yml"]
BOT_BRANCHES = ("bot/bigbang-update", "bot/builds-update")


def check_summary(checks):
    """'12 passed, 1 failed (verify), 3 running' from [{bucket, name}]."""
    by = {}
    for c in checks:
        by.setdefault(c.get("bucket", "?"), []).append(c.get("name", "?"))
    parts = [f"{len(by.get('pass', []))} passed"]
    bad = by.get("fail", []) + by.get("cancel", [])
    if bad:
        parts.append(f"{len(bad)} failed ({', '.join(sorted(bad)[:4])})")
    if by.get("pending"):
        parts.append(f"{len(by['pending'])} running")
    return ", ".join(parts) if checks else "no checks yet"


def plan_line(summary):
    """'APPLICATION 6/6 FULL, BASE 1/2' from `chainctl images entitlements summary -o json`."""
    parts = []
    for tier, q in sorted((summary.get("quota") or {}).items()):
        cur, top = q.get("current", 0), q.get("max", 0)
        if top is None or top < 0:
            parts.append(f"{tier} {cur}/unlimited")
        else:
            parts.append(f"{tier} {cur}/{top}" + (" FULL" if cur >= top else ""))
    return ", ".join(parts) or "no per-tier quotas"


def org_name():
    org = os.environ.get("CHAINGUARD_ORG", "")
    if not org:
        path = os.path.join(ROOT, "CHAINGUARD_ORG")
        lines = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
        org = next((ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")), "")
    return org


def main():
    errors = []

    def attempt(what, fn, default=None):
        try:
            return fn()
        except ghrest.GhError as e:
            errors.append(what)
            print(f"  ERROR reading {what}: {e}")
            return default

    version = open(os.path.join(ROOT, "BIGBANG_VERSION"), encoding="utf-8").read().strip()
    rel = attempt("the latest release", lambda: ghrest.api("releases/latest"), {})
    print(f"Big Bang {version}; latest release "
          f"{rel.get('tag_name', '(unknown)')} ({rel.get('published_at', '?')})" if rel else
          f"Big Bang {version}; latest release (unknown)")

    prs = attempt("open pull requests", ghrest.open_prs)
    if prs is None:
        print("\nOpen pull requests: UNKNOWN (GitHub didn't answer; don't assume none)")
    else:
        prs.sort(key=lambda p: p["branch"] not in BOT_BRANCHES)
        print(f"\nOpen pull requests: {len(prs)}")
        for p in prs:
            checks = attempt(f"#{p['number']}'s checks", lambda: ghrest.checks(p["sha"]))
            labels = "".join(f" [{lb}]" for lb in p["labels"])
            state = check_summary(checks) if checks is not None else "checks UNKNOWN"
            print(f"  #{p['number']} {p['title'][:70]}{labels}\n      {state}")

    print("\nLatest run on main per workflow:")
    for wf in WORKFLOWS:
        data = attempt(f"{wf}'s runs", lambda: ghrest.api(f"actions/workflows/{wf}/runs?branch=main&per_page=1"))
        for r in (data or {}).get("workflow_runs", [])[:1]:
            state = r["conclusion"] or r["status"]
            print(f"  {r['name']:38} {state:12} {r['event']:18} {r['created_at']}  run {r['id']}")

    p = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "ci-triage.py"), "--flakes", "--days", "14"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=ROOT)
    if "ERROR" in p.stdout + p.stderr or p.returncode not in (0, 1):
        errors.append("re-run jobs")
        print("\nRe-run jobs, last 14 days: ERROR (ci-triage.py --flakes failed)")
    else:
        fix = [ln.strip() for ln in p.stdout.splitlines() if ln.strip().startswith("FIX")]
        reruns = [ln for ln in p.stdout.splitlines() if ln.startswith("  ") and "no re-run jobs" not in ln]
        print(f"\nRe-run jobs, last 14 days: {len(reruns)}" + (f"; flaky enough to fix: {len(fix)}" if fix else ""))
        for ln in fix:
            print("  " + ln)

    org = org_name()
    if org and shutil.which("chainctl"):
        q = subprocess.run(["chainctl", "images", "entitlements", "summary", "--parent", org, "-o", "json"],
                           capture_output=True, text=True, timeout=120)
        out = q.stdout
        if q.returncode == 0 and "{" in out:
            print(f"\nChainguard plan ({org}): {plan_line(json.loads(out[out.find('{'):]))}"
                  "   (bb-org-repos.py --quota: what's needed, what's unused)")
    print("\nA failed check: python scripts/ci-triage.py <pr>   (AGENTS.md: playbooks)")
    if errors:
        print(f"\nINCOMPLETE: GitHub didn't answer for {', '.join(errors)}. This is not a clean bill of health.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
