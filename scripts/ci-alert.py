#!/usr/bin/env python3
"""Keep one issue per scheduled workflow that is failing: open it (or comment on the open
one) when a scheduled run fails, close it when the next scheduled run passes.

  python3 scripts/ci-alert.py --workflow "RKE2 - ..." --result failure --run-url URL
  python3 scripts/ci-alert.py --workflow "RKE2 - ..." --result success --run-url URL

Why: a scheduled run (compat.yml weekly, rke2.yml, fips-kernel.yml, cve-report.yml,
update.yml nightly) has no pull request, so its failure reached no one until someone opened
the Actions tab. Each of those workflows ends with an `alert` job that runs
this on schedule events only. The issue carries the `ci-scheduled-failure` label and the
workflow's name in its title (one per workflow, like bigbang-preview.yml's one per ref);
the ALERT_MENTION environment variable (the repository variable) is put in a new issue.

GitHub is read and written through REST only (scripts/ghrest.py). Exit 0 when the issue is
as it should be, 1 when GitHub didn't answer (the run then shows that too).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ghrest  # noqa: E402

LABEL = "ci-scheduled-failure"


def title(workflow):
    return f"Scheduled run failing: {workflow}"


def open_issue(workflow):
    """The open issue's number for this workflow, or None. Pull requests are skipped
    (the issues listing returns them too)."""
    for i in ghrest.api_list(f"issues?labels={LABEL}&state=open"):
        if "pull_request" not in i and i.get("title") == title(workflow):
            return i["number"]
    return None


def ensure_label():
    """Create the label once; GitHub answers 422 when it exists, which is fine."""
    try:
        ghrest.api("labels", "-X", "POST", "-f", f"name={LABEL}", "-f", "color=D93F0B",
                   "-f", "description=A scheduled workflow's last run failed (scripts/ci-alert.py)")
    except ghrest.GhError as e:
        if "already_exists" not in str(e) and "422" not in str(e):
            raise


def action(result, issue):
    """What to do: 'create', 'comment', 'close' or None. A cancelled or timed-out run is a
    failure: a hang is as broken as a red step."""
    failed = result != "success"
    if failed:
        return "comment" if issue else "create"
    return "close" if issue else None


def alert(workflow, result, run_url, mention=""):
    issue = open_issue(workflow)
    todo = action(result, issue)
    if todo == "create":
        ensure_label()
        body = (f"The scheduled run of **{workflow}** ended `{result}`: {run_url}\n\n"
                "Triage: `python scripts/ci-triage.py --run <id>` (AGENTS.md has the playbooks). "
                "This issue gets a comment on each scheduled failure and closes itself on the next "
                "scheduled success.")
        if mention:
            body = f"{mention} {body}"
        ghrest.api("issues", "-X", "POST", "-f", f"title={title(workflow)}", "-f", f"body={body}",
                   "-f", f"labels[]={LABEL}")
    elif todo == "comment":
        ghrest.api(f"issues/{issue}/comments", "-X", "POST", "-f",
                   f"body=Failed again (`{result}`): {run_url}")
    elif todo == "close":
        ghrest.api(f"issues/{issue}/comments", "-X", "POST", "-f",
                   f"body=The scheduled run passed: {run_url}")
        ghrest.api(f"issues/{issue}", "-X", "PATCH", "-f", "state=closed")
    print(f"{workflow}: {result}; issue {issue or 'none'}: {todo or 'nothing to do'}")
    return todo


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workflow", required=True, help="the workflow's name (github.workflow)")
    ap.add_argument("--result", required=True, help="success, or failure / cancelled")
    ap.add_argument("--run-url", required=True)
    a = ap.parse_args()
    try:
        alert(a.workflow, a.result, a.run_url, os.environ.get("ALERT_MENTION", ""))
    except ghrest.GhError as e:
        print(f"::error::GitHub didn't answer, so the {LABEL} issue wasn't updated: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
