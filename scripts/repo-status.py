#!/usr/bin/env python3
"""Where this repository stands, in one screen: what to look at first when you (or an AI
agent) start maintaining it. Needs `gh` logged in.

  python scripts/repo-status.py

  - open pull requests, each with its checks (passed / failed / running) and labels;
    the nightly bot PR (bot/bigbang-update) first
  - the latest run of each workflow on main (CI, builds, release, the nightly update, the
    weekly kind tests, automerge) and whether it passed
  - the latest release and the Big Bang version
Then: a failed check -> python scripts/ci-triage.py <pr>; AGENTS.md has the playbooks.
"""
import json
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = ["ci.yml", "builds.yml", "release.yml", "update.yml", "compat.yml", "automerge.yml", "cve-report.yml"]


def gh(*args):
    p = subprocess.run(["gh", *args], capture_output=True, text=True, cwd=ROOT)
    return p.stdout if p.returncode == 0 else ""


def check_summary(checks):
    """'12 passed, 1 failed (verify), 3 running' from `gh pr checks --json bucket,name`."""
    by = {}
    for c in checks:
        by.setdefault(c.get("bucket", "?"), []).append(c.get("name", "?"))
    parts = [f"{len(by.get('pass', []))} passed"]
    if by.get("fail"):
        parts.append(f"{len(by['fail'])} failed ({', '.join(sorted(by['fail'])[:4])})")
    if by.get("pending"):
        parts.append(f"{len(by['pending'])} running")
    return ", ".join(parts) if checks else "no checks yet"


def latest_runs(runs):
    """The newest run per workflow (runs as `gh run list --json` returns them, newest first)."""
    seen = {}
    for r in runs:
        seen.setdefault(r["workflowName"], r)
    return seen


def main():
    version = open(os.path.join(ROOT, "BIGBANG_VERSION"), encoding="utf-8").read().strip()
    release = gh("release", "view", "--json", "tagName,publishedAt", "--jq", '.tagName + " (" + .publishedAt + ")"')
    print(f"Big Bang {version}; latest release {release.strip() or '(none)'}")

    prs = json.loads(gh("pr", "list", "--state", "open", "--json", "number,title,headRefName,labels") or "[]")
    prs.sort(key=lambda p: p["headRefName"] != "bot/bigbang-update")
    print(f"\nOpen pull requests: {len(prs)}")
    for p in prs:
        checks = json.loads(gh("pr", "checks", str(p["number"]), "--json", "bucket,name") or "[]")
        labels = "".join(f" [{lb['name']}]" for lb in p.get("labels", []))
        print(f"  #{p['number']} {p['title'][:70]}{labels}\n      {check_summary(checks)}")

    print("\nLatest run on main per workflow:")
    for wf in WORKFLOWS:
        runs = json.loads(gh("run", "list", "--workflow", wf, "--branch", "main", "--limit", "1",
                             "--json", "workflowName,status,conclusion,event,createdAt,databaseId") or "[]")
        for name, r in latest_runs(runs).items():
            state = r["conclusion"] or r["status"]
            print(f"  {name:38} {state:12} {r['event']:18} {r['createdAt']}  run {r['databaseId']}")
    print("\nA failed check: python scripts/ci-triage.py <pr>   (AGENTS.md: playbooks)")


if __name__ == "__main__":
    main()
