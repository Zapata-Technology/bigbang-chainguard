"""ghrest.py: REST-only GitHub reads that fail loudly (the scheduled agent's sandbox proxy)."""
import os
import re
import subprocess
import sys

import pytest

from conftest import ROOT

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import ghrest  # noqa: E402


@pytest.mark.parametrize("url", [
    "https://github.com/Zapata-Technology/bigbang-chainguard.git",
    "git@github.com:Zapata-Technology/bigbang-chainguard.git",
    "http://local_proxy@127.0.0.1:39301/git/Zapata-Technology/bigbang-chainguard",
    "https://github.com/Zapata-Technology/bigbang-chainguard/",
])
def test_repo_from_any_remote_url(url):
    assert ghrest.repo_from_url(url) == "Zapata-Technology/bigbang-chainguard"


def test_a_failed_call_raises_instead_of_reading_as_empty(monkeypatch):
    def fail(*a, **k):
        return subprocess.CompletedProcess(a, 1, stdout="", stderr="HTTP 403: not supported through this proxy")
    monkeypatch.setattr(ghrest.subprocess, "run", fail)
    with pytest.raises(ghrest.GhError, match="403"):
        ghrest.run(["api", "repos/o/r/pulls"])


def test_lists_page_by_number_not_by_link_headers(monkeypatch):
    calls = []

    def api(path):
        calls.append(path)
        page = int(re.search(r"page=(\d+)$", path).group(1))
        return {"jobs": [{"n": i} for i in range(100 if page < 3 else 7)]}
    monkeypatch.setattr(ghrest, "api", api)
    assert len(ghrest.api_list("actions/runs/1/jobs", key="jobs")) == 207
    assert calls == [f"actions/runs/1/jobs?per_page=100&page={n}" for n in (1, 2, 3)]


def test_checks_keep_the_newest_run_of_each_name_and_bucket_like_gh(monkeypatch):
    runs = [
        {"id": 1, "name": "verify", "status": "completed", "conclusion": "failure", "html_url": "u1"},
        {"id": 5, "name": "verify", "status": "completed", "conclusion": "success", "html_url": "u5"},  # re-run
        {"id": 2, "name": "grafana (fips)", "status": "completed", "conclusion": "timed_out", "html_url": "u2"},
        {"id": 3, "name": "results", "status": "completed", "conclusion": "skipped", "html_url": "u3"},
        {"id": 4, "name": "build (base)", "status": "in_progress", "conclusion": None, "html_url": "u4"},
    ]
    monkeypatch.setattr(ghrest, "api_list", lambda path, key=None, **k: runs)
    got = {c["name"]: (c["bucket"], c["link"]) for c in ghrest.checks("sha")}
    assert got == {"verify": ("pass", "u5"), "grafana (fips)": ("cancel", "u2"),
                   "results": ("skipping", "u3"), "build (base)": ("pending", "u4")}


def test_no_maintenance_script_uses_graphql_or_numeric_paths():
    """gh pr list/checks/view and gh repo view are GraphQL; gh run download and --paginate
    follow repositories/<id> links. The cloud sandbox's proxy refuses both."""
    for name in ("repo-status.py", "ci-triage.py", "ghrest.py"):
        code = open(os.path.join(ROOT, "scripts", name), encoding="utf-8").read()
        code = re.sub(r'""".*?"""', "", code, flags=re.S)       # docstrings may name them
        code = "\n".join(ln.split("#")[0] for ln in code.splitlines())
        for bad in ('"pr", "list"', '"pr", "checks"', '"pr", "view"', '"repo", "view"',
                    '"run", "download"', '"--paginate"', '"graphql"'):
            assert bad not in code, f"{name} uses {bad}"
