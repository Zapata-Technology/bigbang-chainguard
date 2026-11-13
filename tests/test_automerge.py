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
    assert "github.event.workflow_run.head_branch == 'bot/bigbang-release'" in cond
    assert "github.event.workflow_run.head_branch == 'bot/builds-release'" in cond
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
    assert "bb-release-notes.py" in run
    assert run.index("bb-held-builds.py hold") < run.index("bb-convert.py")
    names = [s.get("name") for s in steps]
    assert names.index("Open the pull request") < names.index("Put the held-back builds back") \
        < names.index("Open the builds pull request")
    builds_pr = next(s for s in steps if s.get("name") == "Open the builds pull request")
    assert builds_pr["with"]["branch"] == "${{ matrix.builds_branch }}"
    assert builds_pr["with"]["add-paths"] == "${{ steps.run.outputs.held }}"


UPDATE = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "update.yml"), encoding="utf-8"))


def test_no_guard_reads_a_failed_call_as_no():
    """`if gh ... | grep -q x` is false when gh fails (pipefail): a held PR would merge."""
    import re
    for ln in STEP["run"].splitlines():
        assert not re.search(r"\bif\b.*\bgh\b.*\|", ln), f"capture gh's output first: {ln.strip()}"


def test_automerge_accepts_exactly_the_files_update_writes():
    pr_step = next(s for s in UPDATE["jobs"]["update"]["steps"] if s.get("name") == "Open the pull request")
    paths = pr_step["with"]["add-paths"].splitlines()
    for path in (p.strip() for p in paths):
        if path.startswith("${{"):
            continue    # steps.run.outputs.dropped: removed builds/*/release.yaml, below
        assert f"bot/bigbang-*:{path}" in STEP["run"] or f"bot/bigbang-release:{path}" in STEP["run"], \
            f"automerge refuses update.yml's {path}"
    assert "${{ steps.run.outputs.dropped }}" in [p.strip() for p in paths]
    assert "bot/bigbang-*:builds/*/release.yaml) [ \"$status\" = removed ]" in STEP["run"]
    assert "bot/builds-update:builds/*/build.yaml" in STEP["run"]
    assert "bot/builds-release:builds/*/build.yaml" in STEP["run"]
    assert "bot/builds-release:builds/*/release.yaml)" in STEP["run"]
    assert "bot/builds-update:builds/*/release.yaml" not in STEP["run"]


def test_update_runs_a_routine_track_and_a_release_track_with_their_own_branches():
    plan = next(s for s in UPDATE["jobs"]["plan"]["steps"] if s.get("id") == "bb")["run"]
    assert '"branch":"bot/bigbang-update","builds_branch":"bot/builds-update"' in plan
    assert '"branch":"bot/bigbang-release","builds_branch":"bot/builds-release"' in plan
    job = UPDATE["jobs"]["update"]
    assert job["needs"] == "plan" and job["strategy"]["fail-fast"] is False
    steps = {s.get("name"): s for s in job["steps"]}
    assert steps["Open the pull request"]["with"]["branch"] == "${{ matrix.branch }}"
    assert "docs/upgrades/*.md" in steps["Open the pull request"]["with"]["add-paths"].split()


def test_update_leaves_a_held_bot_pr_alone_and_has_a_time_limit():
    job = UPDATE["jobs"]["update"]
    assert job["timeout-minutes"] <= 90
    steps = {s.get("name"): s for s in job["steps"]}
    for track in ("update", "release"):
        assert f"steps.hold.outputs.bigbang-{track} != 'true'" in steps["Open the pull request"]["if"]
        assert f"steps.hold.outputs.builds-{track} != 'true'" in steps["Open the builds pull request"]["if"]
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


def run_merge(tmp_path, data, conclusion="success", attempt="1", triage_rc=1, branch="bot/bigbang-update",
              patches="", main_version="4.1.0"):
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
    (tmp_path / "BIGBANG_VERSION").write_text(main_version + "\n", encoding="utf-8")   # main's checkout
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}", FAKE_GH_DATA=str(tmp_path / "data.json"),
               FAKE_GH_LOG=str(log), GH_TOKEN="t", MERGE_TOKEN="m", GH_REPO="o/r", SHA="abc",
               BRANCH=branch, PATCHES=patches, RUN="9", RUN_NAME="CI", RUN_URL="u", ATTEMPT=attempt,
               CONCLUSION=conclusion, MENTION="", CI_TRIAGE=str(triage))
    p = subprocess.run(["bash", "-c", STEP["run"]], env=env, capture_output=True, text=True, cwd=tmp_path)
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


def release(version, **over):
    import base64
    data = green(**{
        "pulls?state=open&per_page=100": [{"number": 7, "head": {"ref": "bot/bigbang-release", "sha": "abc"}}],
        "pulls/7/files?per_page=100&page=1": [{"filename": "BIGBANG_VERSION"},
                                              {"filename": f"docs/upgrades/{version}.md"},
                                              {"filename": "overlays/values-chainguard.yaml"}],
        "contents/BIGBANG_VERSION?ref=abc": {"content": base64.b64encode(f"{version}\n".encode()).decode()}})
    data.update(over)
    return data


def test_a_new_big_bang_release_waits_for_a_person(tmp_path):
    p, log = run_merge(tmp_path, release("4.1.1"), branch="bot/bigbang-release")
    assert p.returncode == 0, p.stderr
    assert "pr merge" not in log and "labels[]=needs-attention" in log


def test_a_patch_release_merges_itself_only_when_turned_on(tmp_path):
    p, log = run_merge(tmp_path, release("4.1.1"), branch="bot/bigbang-release", patches="true")
    assert p.returncode == 0, p.stderr
    assert "pr merge 7 --squash" in log and "needs-attention" not in log
    (tmp_path / "minor").mkdir()
    p, log = run_merge(tmp_path / "minor", release("4.2.0"), branch="bot/bigbang-release", patches="true")
    assert "pr merge" not in log and "needs-attention" in log       # a minor release: a person


def test_the_routine_track_never_moves_big_bang(tmp_path):
    data = release("4.1.1", **{"pulls?state=open&per_page=100": [
        {"number": 7, "head": {"ref": "bot/bigbang-update", "sha": "abc"}}]})
    data["pulls/7/files?per_page=100&page=1"] = [{"filename": "BIGBANG_VERSION"}]
    p, log = run_merge(tmp_path, data, patches="true")
    assert "pr merge" not in log and "needs-attention" in log


RELEASE_YAML = "builds/grafana-plugins/release.yaml"


def test_the_release_tracks_held_build_merges_as_release_yaml(tmp_path):
    data = green(**{"pulls?state=open&per_page=100": [{"number": 7, "head": {"ref": "bot/builds-release", "sha": "abc"}}],
                    "pulls/7/files?per_page=100&page=1": [{"status": "added", "filename": RELEASE_YAML}]})
    p, log = run_merge(tmp_path, data, branch="bot/builds-release")
    assert p.returncode == 0, p.stderr
    assert "pr merge 7 --squash" in log and "needs-attention" not in log


def test_a_big_bang_pr_may_only_delete_a_release_yaml(tmp_path):
    files = [{"status": "modified", "filename": "builds/grafana-plugins/build.yaml"},
             {"status": "removed", "filename": RELEASE_YAML}]
    p, log = run_merge(tmp_path, green(**{"pulls/7/files?per_page=100&page=1": files}))
    assert p.returncode == 0, p.stderr
    assert "pr merge 7 --squash" in log
    for status in ("added", "modified"):
        (tmp_path / status).mkdir()
        files = [{"status": status, "filename": RELEASE_YAML}]
        p, log = run_merge(tmp_path / status, green(**{"pulls/7/files?per_page=100&page=1": files}))
        assert "pr merge" not in log and "needs-attention" in log, status


def test_the_routine_builds_pr_never_carries_a_release_yaml(tmp_path):
    data = green(**{"pulls?state=open&per_page=100": [{"number": 7, "head": {"ref": "bot/builds-update", "sha": "abc"}}],
                    "pulls/7/files?per_page=100&page=1": [{"status": "added", "filename": RELEASE_YAML}]})
    p, log = run_merge(tmp_path, data, branch="bot/builds-update")
    assert "pr merge" not in log and "needs-attention" in log


def test_update_puts_the_release_tracks_held_builds_in_release_yaml():
    steps = UPDATE["jobs"]["update"]["steps"]
    run_step = next(s for s in steps if s.get("id") == "run")
    run = run_step["run"]
    assert run_step["env"]["TRACK"] == "${{ matrix.track }}"
    assert run_step["env"]["DROP_RELEASE"] == "${{ matrix.drop_release }}"
    assert '$([ "$TRACK" = release ] && echo --release)' in run
    assert run.index("bb-held-builds.py hold") < run.index("bb-held-builds.py drop-release --keep") \
        < run.index("bb-convert.py")
    plan = next(s for s in UPDATE["jobs"]["plan"]["steps"] if s.get("id") == "bb")["run"]
    # The release track always drops what it no longer holds; the routine track only once
    # no newer release is out, and never when repo1 didn't answer (latest unknown).
    assert '"drop_release":"true"}' in plan and '"drop_release":"\'"$drop"\'"}' in plan
    assert '[ -n "$latest" ] && [ "$newer" = false ] && drop=true' in plan


def test_update_plan_tracks(tmp_path):
    """The plan step's bash, with git ls-remote faked: which tracks run, and which drop."""
    import json
    import shutil
    import subprocess
    import pytest
    if os.name == "nt" or not shutil.which("bash"):
        pytest.skip("needs bash (CI's runner has it)")
    plan = next(s for s in UPDATE["jobs"]["plan"]["steps"] if s.get("id") == "bb")["run"]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "git").write_text('#!/bin/sh\nprintf "%s" "$FAKE_TAGS"\n', encoding="utf-8")
    (bindir / "git").chmod(0o755)
    (bindir / "sleep").write_text("#!/bin/sh\n", encoding="utf-8")
    (bindir / "sleep").chmod(0o755)
    (tmp_path / "BIGBANG_VERSION").write_text("4.1.0\n", encoding="utf-8")

    def tracks(tags):
        out = tmp_path / "out"
        out.write_text("", encoding="utf-8")
        env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}", GITHUB_OUTPUT=str(out),
                   FAKE_TAGS="".join(f"x\trefs/tags/{t}\n" for t in tags))
        p = subprocess.run(["bash", "-e", "-c", plan], env=env, capture_output=True, text=True, cwd=tmp_path)
        assert p.returncode == 0, p.stderr
        line = out.read_text(encoding="utf-8").strip()
        return {t["track"]: t["drop_release"] for t in json.loads(line.split("=", 1)[1])}

    assert tracks(["4.0.0", "4.1.0", "4.2.0"]) == {"routine": "false", "release": "true"}
    assert tracks(["4.0.0", "4.1.0"]) == {"routine": "true"}
    assert tracks([]) == {"routine": "false"}       # repo1 didn't answer: keep release.yaml
