"""automerge.yml: the nightly bot PR re-runs a failed first attempt once, never more."""
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


def test_only_the_bot_branch_from_this_repository_is_handled():
    cond = " ".join(JOB["if"].split())
    assert "github.event.workflow_run.head_branch == 'bot/bigbang-update'" in cond
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in cond


def test_the_merge_script_is_valid_bash(tmp_path):
    import shutil
    import subprocess
    import pytest
    if not shutil.which("bash"):
        pytest.skip("no bash")
    script = tmp_path / "merge.sh"
    script.write_text(STEP["run"], encoding="utf-8")
    p = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
