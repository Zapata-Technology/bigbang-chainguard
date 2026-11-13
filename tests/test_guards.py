"""Guards for fixes the kind tests found: each would regress without a visible error.

- every mapping.yaml `overrides` block names a release Big Bang deploys (one under a
  wrong name changes nothing, silently);
- GitLab Runner's HOME, which its chart's mounted config directory needs;
- the test PostgreSQL's lock limit, which GitLab's schema load needs;
- automerge.yml reading with the workflow token and merging with RELEASE_PAT.
"""
import importlib.util
import json
import os

import pytest
import yaml

from conftest import ROOT

MAPPING = yaml.safe_load(open(os.path.join(ROOT, "mapping.yaml"), encoding="utf-8"))
PACKAGES = json.load(open(os.path.join(ROOT, "discovery", "images.json"), encoding="utf-8"))["packages"]


@pytest.mark.parametrize("release", sorted(MAPPING.get("overrides", {})))
def test_every_override_names_a_release_big_bang_deploys(release):
    # bb-convert applies `overrides.<release>` only when a package by that name exists;
    # a misspelled or renamed release would change nothing, without an error.
    assert release in PACKAGES, f"overrides.{release}: no such release in discovery/images.json"


def test_gitlab_runner_home_is_the_directory_its_chart_mounts():
    env = MAPPING["overrides"]["gitlab-runner"]["upstream"]["extraEnv"]
    assert env["HOME"] == "/home/gitlab-runner"


def test_the_test_postgresql_allows_gitlabs_schema_load(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("app_deps", os.path.join(ROOT, "compat", "support", "app-deps.py"))
    app_deps = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_deps)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "images.md").write_text(
        "Images are in `cgr.dev/example.org`.\n"
        "| gitlab | ironbank/opensource/postgres/postgresql:18.6 | postgres:18.6 | postgres-fips:18.6 |  |\n"
        "| gitlab | ironbank/bitnami/redis:8.2 | redis:8.2 | redis-fips:8.2 |  |\n")
    applied = []
    monkeypatch.setattr(app_deps, "run", lambda *cmd, stdin=None: applied.append(stdin) or "")
    monkeypatch.setattr(app_deps.sys, "argv", ["app-deps.py", "gitlab"])
    monkeypatch.setenv("COMPAT_OUT", str(tmp_path))
    monkeypatch.setenv("COMPAT_DOCKERCFG", str(tmp_path / "config.json"))
    monkeypatch.delenv("COMPAT_FIPS", raising=False)
    app_deps.main()
    docs = [d for s in applied if s for d in yaml.safe_load_all(s) if d]
    [postgres] = [d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "postgres"]
    args = postgres["spec"]["template"]["spec"]["containers"][0]["args"]
    assert args == ["-c", "max_locks_per_transaction=256"]


def test_automerge_reads_with_the_workflow_token_and_merges_with_the_bot_token():
    wf = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "automerge.yml"), encoding="utf-8"))
    [step] = [s for s in wf["jobs"]["merge"]["steps"] if "gh pr merge" in s.get("run", "")]
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"     # RELEASE_PAT can't read check runs
    # The GitHub App's token when configured, else RELEASE_PAT; never GITHUB_TOKEN (a merge
    # with it starts no push workflows on main).
    assert step["env"]["MERGE_TOKEN"] == "${{ steps.app.outputs.token || secrets.RELEASE_PAT }}"
    [app] = [s for s in wf["jobs"]["merge"]["steps"] if s.get("id") == "app"]
    assert app["uses"].startswith("actions/create-github-app-token@")
    assert app["with"]["permission-contents"] == "write" and app["with"]["permission-pull-requests"] == "write"
    merge_lines = [ln for ln in step["run"].splitlines() if "gh pr merge" in ln]
    assert merge_lines and all(ln.strip().startswith('GH_TOKEN="$MERGE_TOKEN"') for ln in merge_lines)
