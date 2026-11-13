"""automerge.yml and update.yml: the nightly bot PRs merge themselves only when green, the
bot's own and unheld; anything else asks for a person once."""
import os

import yaml

from conftest import ROOT

WF = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "automerge.yml"), encoding="utf-8"))
JOB = WF["jobs"]["merge"]
STEP = next(s for s in JOB["steps"] if "gh pr merge" in s.get("run", ""))


def test_a_failed_first_attempt_is_re_run_once():
    run = STEP["run"]
    assert STEP["env"]["ATTEMPT"] == "${{ github.event.workflow_run.run_attempt }}"
    assert STEP["env"]["CONCLUSION"] == "${{ github.event.workflow_run.conclusion }}"
    guard = '[ "$CONCLUSION" = cancelled ]'
    assert '[ "$CONCLUSION" = failure ]' in run and '[ "$ATTEMPT" = 1 ]' in run
    assert guard in run
    # The re-run comes after the hold and new-Big-Bang checks, and before any merge.
    assert run.index("hold") < run.index(guard) < run.index('gh run rerun "$RUN" --failed') < run.index("gh pr merge")
    assert JOB["permissions"]["actions"] == "write"


def test_only_the_bot_branches_from_this_repository_are_handled():
    cond = " ".join(JOB["if"].split())
    assert "github.event.workflow_run.head_branch == 'bot/bigbang-update'" in cond
    assert "github.event.workflow_run.head_branch == 'bot/builds-update'" in cond
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in cond
    # The branch reaches the script as an env var, never interpolated into it.
    assert STEP["env"]["BRANCH"] == "${{ github.event.workflow_run.head_branch }}"
    assert "${{" not in STEP["run"]


def test_update_holds_unpublished_builds_and_reruns_after_publishing():
    wf = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "update.yml"), encoding="utf-8"))
    on = wf[True] if True in wf else wf["on"]   # PyYAML reads the key `on` as True
    assert on["workflow_run"]["workflows"] == ["Build - Big Bang images"]
    assert on["workflow_run"]["branches"] == ["main"]
    steps = wf["jobs"]["update"]["steps"]
    run = next(s for s in steps if s.get("id") == "run")["run"]
    assert run.index("bb-held-builds.py hold") < run.index("bb-convert.py")
    names = [s.get("name") for s in steps]
    assert names.index("Open the pull request") < names.index("Put the held-back builds back") \
        < names.index("Open the builds pull request")
    builds_pr = next(s for s in steps if s.get("name") == "Open the builds pull request")
    assert builds_pr["with"]["branch"] == "bot/builds-update"
    assert builds_pr["with"]["add-paths"] == "${{ steps.run.outputs.held }}"


UPDATE = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "update.yml"), encoding="utf-8"))


def test_no_guard_reads_a_failed_call_as_no():
    """`if gh ... | grep -q x` is false when gh fails (pipefail): a held PR would merge."""
    import re
    for ln in STEP["run"].splitlines():
        assert not re.search(r"\bif\b.*\bgh\b.*\|", ln), f"capture gh's output first: {ln.strip()}"


def test_automerge_accepts_exactly_the_files_update_writes():
    pr_step = next(s for s in UPDATE["jobs"]["update"]["steps"] if s.get("name") == "Open the pull request")
    for path in pr_step["with"]["add-paths"].split():
        assert f"bot/bigbang-update:{path}" in STEP["run"], f"automerge refuses update.yml's {path}"
    assert "bot/builds-update:builds/*/build.yaml" in STEP["run"]


def test_update_leaves_a_held_bot_pr_alone_and_has_a_time_limit():
    job = UPDATE["jobs"]["update"]
    assert job["timeout-minutes"] <= 90
    steps = {s.get("name"): s for s in job["steps"]}
    assert "steps.hold.outputs.bigbang-update != 'true'" in steps["Open the pull request"]["if"]
    assert "steps.hold.outputs.builds-update != 'true'" in steps["Open the builds pull request"]["if"]
    on = UPDATE[True] if True in UPDATE else UPDATE["on"]
    assert "mapping.yaml" in on["push"]["paths"] and on["push"]["branches"] == ["main"]
    for name in ("Open the pull request", "Open the builds pull request"):
        assert steps[name]["with"]["token"].startswith("${{ steps.app.outputs.token ||")


FAKE_GH = r'''#!/usr/bin/env python3
"""A stand-in for gh: canned REST answers from $FAKE_GH_DATA, --jq through the real jq."""
import json, os, subprocess, sys
args = sys.argv[1:]
open(os.environ["FAKE_GH_LOG"], "a").write(" ".join(args) + "\n")
if args[0] != "api":
    sys.exit(0)                      # gh pr merge / gh run rerun: logged only
data = json.load(open(os.environ["FAKE_GH_DATA"]))
path = next(a for a in args[1:] if a.startswith("repos/"))
if "-X" in args:
    sys.exit(0)                      # POST label / comment: logged only
answer = data.get(path.split("repos/o/r/", 1)[1], [])
if answer == "FAIL":
    sys.stderr.write("HTTP 502: Bad Gateway\n")
    sys.exit(1)
text = json.dumps(answer)
if "--jq" in args:
    text = subprocess.run(["jq", "-r", args[args.index("--jq") + 1]], input=text,
                          capture_output=True, text=True, check=True).stdout
sys.stdout.write(text)
'''


def run_merge(tmp_path, data, conclusion="success", attempt="1", triage_rc=1):
    """Run the merge step with a fake gh, and a stub ci-triage.py that exits triage_rc
    (0 nothing failed, 1 a real failure, 2 only flakes, 3 GitHub didn't answer)."""
    import shutil
    import subprocess
    import pytest
    if os.name == "nt" or not shutil.which("jq") or not shutil.which("bash"):
        pytest.skip("needs bash and jq (CI's runner has both)")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    triage = bindir / "triage"
    triage.write_text(f'#!/bin/sh\necho "triage $*" >> "$FAKE_GH_LOG"\nexit {triage_rc}\n', encoding="utf-8")
    triage.chmod(0o755)
    (tmp_path / "data.json").write_text(__import__("json").dumps(data), encoding="utf-8")
    log = tmp_path / "gh.log"
    log.write_text("", encoding="utf-8")
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}", FAKE_GH_DATA=str(tmp_path / "data.json"),
               FAKE_GH_LOG=str(log), GH_TOKEN="t", MERGE_TOKEN="m", GH_REPO="o/r", SHA="abc",
               BRANCH="bot/bigbang-update", RUN="9", RUN_NAME="CI", RUN_URL="u", ATTEMPT=attempt,
               CONCLUSION=conclusion, MENTION="", CI_TRIAGE=str(triage))
    p = subprocess.run(["bash", "-c", STEP["run"]], env=env, capture_output=True, text=True)
    return p, log.read_text(encoding="utf-8")


def green(**over):
    data = {
        "pulls?state=open&per_page=100": [{"number": 7, "head": {"ref": "bot/bigbang-update", "sha": "abc"}}],
        "issues/7/labels": [],
        "pulls/7/files?per_page=100&page=1": [{"filename": "overlays/values-chainguard.yaml"}],
        "pulls/7/commits?per_page=100": [{"sha": "abc"}],
        "commits/abc/check-runs?per_page=100&page=1": {"check_runs": [
            {"status": "completed", "conclusion": "success", "name": "verify"},
            {"status": "completed", "conclusion": "skipped", "name": "results"}]},
    }
    data.update(over)
    return data


def test_a_green_bot_pr_merges(tmp_path):
    p, log = run_merge(tmp_path, green())
    assert p.returncode == 0, p.stderr
    assert "pr merge 7 --squash" in log


def test_a_failed_read_stops_the_run_instead_of_merging(tmp_path):
    p, log = run_merge(tmp_path, green(**{"issues/7/labels": "FAIL"}))
    assert p.returncode != 0
    assert "pr merge" not in log


def test_a_held_pr_is_left_alone(tmp_path):
    p, log = run_merge(tmp_path, green(**{"issues/7/labels": [{"name": "hold"}]}))
    assert p.returncode == 0 and "pr merge" not in log and "-X POST" not in log


def test_a_persons_commit_on_the_bot_branch_is_flagged_not_merged(tmp_path):
    data = green(**{"pulls/7/files?per_page=100&page=1": [{"filename": "overlays/values-chainguard.yaml"},
                                                         {"filename": ".github/workflows/release.yml"}],
                    "pulls/7/commits?per_page=100": [{"sha": "a"}, {"sha": "b"}]})
    p, log = run_merge(tmp_path, data)
    assert p.returncode == 0, p.stderr
    assert "pr merge" not in log
    assert "issues/7/labels" in log and "labels[]=needs-attention" in log and "issues/7/comments" in log


def test_a_first_failure_is_re_run_only_when_triage_finds_only_flakes(tmp_path):
    p, log = run_merge(tmp_path, green(), conclusion="failure", attempt="1", triage_rc=2)
    assert p.returncode == 0, p.stderr
    assert "triage --run 9" in log and "run rerun 9 --failed" in log
    assert "needs-attention" not in log and "pr merge" not in log


def test_a_real_first_failure_asks_for_a_person_without_a_re_run(tmp_path):
    p, log = run_merge(tmp_path, green(), conclusion="failure", attempt="1", triage_rc=1)
    assert p.returncode == 0, p.stderr
    assert "run rerun" not in log and "pr merge" not in log
    assert "labels[]=needs-attention" in log and "issues/7/comments" in log


def test_a_triage_that_couldnt_read_github_fails_closed(tmp_path):
    p, log = run_merge(tmp_path, green(), conclusion="cancelled", attempt="1", triage_rc=3)
    assert "run rerun" not in log and "pr merge" not in log and "needs-attention" in log


def test_the_re_run_uses_mains_triage_not_the_pull_requests_code():
    [checkout] = [s for s in JOB["steps"] if s.get("uses", "").startswith("actions/checkout@")]
    assert checkout["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    assert checkout["with"]["persist-credentials"] is False
    assert "${CI_TRIAGE:-python3 scripts/ci-triage.py} --run" in STEP["run"]


def test_a_second_failure_asks_for_a_person_once(tmp_path):
    p, log = run_merge(tmp_path, green(), conclusion="failure", attempt="2")
    assert "needs-attention" in log and "run rerun" not in log and "pr merge" not in log
    again = tmp_path / "again"
    again.mkdir()
    p, log = run_merge(again, green(**{"issues/7/labels": [{"name": "needs-attention"}]}),
                       conclusion="failure", attempt="2")
    assert "issues/7/comments" not in log   # already said


def test_the_merge_script_is_valid_bash(tmp_path):
    import shutil
    import subprocess
    import pytest
    # On Windows `bash` may be WSL's launcher with no distribution behind it.
    if not shutil.which("bash") or subprocess.run(["bash", "-c", "true"], capture_output=True).returncode:
        pytest.skip("no working bash")
    script = tmp_path / "merge.sh"
    script.write_text(STEP["run"], encoding="utf-8")
    p = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
