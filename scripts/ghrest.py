"""GitHub reads and writes for the maintenance scripts (repo-status.py, ci-triage.py), through
`gh api` on REST paths of the form repos/<owner>/<repo>/... only.

Why: the cloud sandbox scheduled agents run in reaches GitHub through a proxy that passes
only those paths. GraphQL (`gh pr list`, `gh pr checks`, `gh repo view`) and numeric-ID
paths (repositories/<id>/..., which `gh run download` and `gh api --paginate`'s next-page
links use) fail there. A script that turned such a failure into "nothing" told the daily
agent there were no open pull requests while the nightly bot PR sat red for days. So:
every call here raises GhError on failure, and lists page with page=N, not Link headers.
"""
import json
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class GhError(RuntimeError):
    pass


def run(args, binary=False):
    # gh's output is UTF-8 (job logs carry any byte); Windows' default code page can't decode it.
    p = subprocess.run(["gh", *args], capture_output=True, cwd=ROOT, text=not binary,
                       encoding=None if binary else "utf-8", errors=None if binary else "replace")
    if p.returncode != 0:
        err = p.stderr if isinstance(p.stderr, str) else p.stderr.decode("utf-8", "replace")
        raise GhError(f"gh {' '.join(args)}: {err.strip() or 'exit ' + str(p.returncode)}")
    return p.stdout


def repo_from_url(url):
    """'owner/repo' from a git remote URL: https, ssh, or a proxy's http://host/git/owner/repo."""
    path = re.sub(r"\.git$", "", url.strip().rstrip("/"))
    parts = re.split(r"[/:]", path)
    if len(parts) < 2 or not parts[-1] or not parts[-2]:
        raise GhError(f"can't tell the GitHub repository from the remote {url!r}")
    return f"{parts[-2]}/{parts[-1]}"


def repo():
    """owner/repo: $GITHUB_REPOSITORY (Actions), else the origin remote (no API call)."""
    if os.environ.get("GITHUB_REPOSITORY"):
        return os.environ["GITHUB_REPOSITORY"]
    p = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True, cwd=ROOT)
    if p.returncode != 0:
        raise GhError("no origin remote; set GITHUB_REPOSITORY=owner/repo")
    return repo_from_url(p.stdout)


def api(path, *args, binary=False):
    """`gh api repos/<owner>/<repo>/<path>`; JSON parsed unless binary or raw args are given."""
    out = run(["api", f"repos/{repo()}/{path}", *args], binary=binary)
    return out if binary or args else json.loads(out or "null")


def api_list(path, key=None, per_page=100, limit=1000):
    """Every item of a listing, page by page (key: the list inside each page, e.g. 'jobs')."""
    sep = "&" if "?" in path else "?"
    items, page = [], 1
    while len(items) < limit:
        data = api(f"{path}{sep}per_page={per_page}&page={page}")
        batch = data[key] if key else data
        items += batch
        if len(batch) < per_page:
            break
        page += 1
    return items[:limit]


def open_prs():
    """[{number, title, branch, sha, labels}] of open pull requests, oldest first."""
    return [{"number": p["number"], "title": p["title"], "branch": p["head"]["ref"],
             "sha": p["head"]["sha"], "labels": [lb["name"] for lb in p.get("labels", [])]}
            for p in api_list("pulls?state=open&sort=created&direction=asc")]


def pr_sha(number):
    return api(f"pulls/{number}")["head"]["sha"]


def bucket(check):
    """`gh pr checks`' buckets for a check run: pass, fail, cancel, skipping or pending."""
    if check.get("status") != "completed":
        return "pending"
    c = check.get("conclusion")
    if c in ("success", "neutral"):
        return "pass"
    if c == "skipped":
        return "skipping"
    if c in ("cancelled", "timed_out"):
        return "cancel"
    return "fail"


def checks(sha):
    """[{name, bucket, link}] for a commit: the newest check run of each name (a re-run adds
    a new one beside the old, which `gh pr checks` hides too)."""
    newest = {}
    for c in api_list(f"commits/{sha}/check-runs", key="check_runs"):
        if c["name"] not in newest or c["id"] > newest[c["name"]]["id"]:
            newest[c["name"]] = c
    return [{"name": c["name"], "bucket": bucket(c), "link": c.get("html_url") or c.get("details_url") or ""}
            for c in sorted(newest.values(), key=lambda c: c["name"])]


def rerun_failed(run_id):
    run(["api", "-X", "POST", f"repos/{repo()}/actions/runs/{run_id}/rerun-failed-jobs"])
