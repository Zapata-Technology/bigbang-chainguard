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
    assert "github.event.workflow_run.head_branch == 'bot/security-report'" in cond
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
        assert steps[name]["with"]["token"] == "${{ steps.app.outputs.token || secrets.RELEASE_PAT }}"


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
    (0 nothing failed, 1 a real failure, 2 only flakes, 3 GitHub didn't answer,
    4 results-gate is red because the compat matrix is not recorded yet)."""
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


def test_exit_4_is_the_missing_matrix_and_does_not_call_attention():
    run = STEP["run"]
    arm = run.split("case \"$rc\" in", 1)[1].split("esac", 1)[0]
    assert "4)" in arm and "no recorded standard compat matrix" in arm
    assert "Not labeling needs-attention" in arm
    assert arm.index("4)") < arm.index("1)")


def test_a_missing_matrix_sits_without_a_rerun_or_a_label(tmp_path):
    p, log = run_merge(tmp_path, green(), conclusion="failure", attempt="1", triage_rc=4)
    assert p.returncode == 0, p.stderr
    assert "run rerun" not in log and "pr merge" not in log and "needs-attention" not in log
    assert "no recorded standard compat matrix" in p.stdout


def test_a_real_first_failure_asks_for_a_person_without_a_re_run(tmp_path):
    p, log = run_merge(tmp_path, green(), conclusion="failure", attempt="1", triage_rc=1)
    assert p.returncode == 0, p.stderr
    assert "run rerun" not in log and "pr merge" not in log
    assert "labels[]=needs-attention" in log and "issues/7/comments" in log


def test_a_triage_that_couldnt_read_github_fails_closed(tmp_path):
    p, log = run_merge(tmp_path, green(), conclusion="cancelled", attempt="1", triage_rc=3)
    assert "run rerun" not in log and "pr merge" not in log and "needs-attention" in log


def test_each_bot_branch_has_its_own_concurrency_group():
    """GitHub keeps one pending run per group: a shared group dropped one branch's event."""
    assert WF["concurrency"]["group"] == "automerge-${{ github.event.workflow_run.head_branch }}"
    assert WF["concurrency"]["cancel-in-progress"] is False


def test_no_merge_token_fails_the_run():
    guard = next(ln for ln in STEP["run"].splitlines() if '[ -n "$MERGE_TOKEN" ]' in ln)
    assert "exit 1" in guard and "::error::" in guard


def failed_check(**over):
    return green(**{"commits/abc/check-runs?per_page=100&page=1": {"check_runs": [
        {"status": "completed", "conclusion": "success", "name": "verify"},
        {"status": "completed", "conclusion": "failure", "name": "loki (standard)"}]}, **over})


def test_a_failed_check_whose_event_was_dropped_asks_for_a_person(tmp_path):
    """This run passed, but another run's failure on the head commit was never triaged."""
    p, log = run_merge(tmp_path, failed_check(), conclusion="success", triage_rc=1)
    assert p.returncode == 0, p.stderr
    assert "triage 7" in log and "labels[]=needs-attention" in log and "issues/7/comments" in log
    assert "pr merge" not in log
    for rc in ("2", "3"):
        (tmp_path / rc).mkdir()
        p, log = run_merge(tmp_path / rc, failed_check(), conclusion="success", triage_rc=int(rc))
        assert "labels[]=needs-attention" in log and "pr merge" not in log, rc


def test_a_failed_check_that_is_only_the_missing_matrix_stays_quiet(tmp_path):
    p, log = run_merge(tmp_path, failed_check(), conclusion="success", triage_rc=4)
    assert p.returncode == 0, p.stderr
    assert "triage 7" in log and "needs-attention" not in log and "pr merge" not in log


def test_a_failed_check_already_labeled_is_not_triaged_again(tmp_path):
    p, log = run_merge(tmp_path, failed_check(**{"issues/7/labels": [{"name": "needs-attention"}]}),
                       conclusion="success", triage_rc=1)
    assert p.returncode == 0, p.stderr
    assert "triage" not in log and "-X POST" not in log and "pr merge" not in log


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


def test_a_release_pr_for_the_version_main_already_has_is_never_merged(tmp_path):
    """#268: opened by an update run that started before #259 brought 4.1.1 to main."""
    p, log = run_merge(tmp_path, release("4.1.1"), branch="bot/bigbang-release", patches="true",
                       main_version="4.1.1")
    assert p.returncode == 0, p.stderr
    assert "pr merge" not in log and "needs-attention" in log


def test_the_release_track_asks_main_again_before_opening_its_prs():
    steps = UPDATE["jobs"]["update"]["steps"]
    names = [s.get("name") for s in steps]
    ahead = steps[names.index("Release still ahead of main")]
    assert ahead["if"] == "matrix.track == 'release'" and "BIGBANG_VERSION?ref=main" in ahead["run"]
    assert names.index("Release still ahead of main") < names.index("Open the pull request")
    for name in ("Open the pull request", "Open the builds pull request"):
        assert "steps.ahead.outputs.stale != 'true'" in steps[names.index(name)]["if"]


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


def test_the_security_report_pr_merges_only_its_two_files(tmp_path):
    pulls = {"pulls?state=open&per_page=100": [{"number": 7, "head": {"ref": "bot/security-report", "sha": "abc"}}]}
    report = [{"status": "modified", "filename": "docs/security-report.pdf"},
              {"status": "modified", "filename": "docs/security-report.md"}]
    (tmp_path / "ok").mkdir()
    p, log = run_merge(tmp_path / "ok", green(**pulls, **{"pulls/7/files?per_page=100&page=1": report}), branch="bot/security-report")
    assert p.returncode == 0, p.stderr
    assert "pr merge 7 --squash" in log and "needs-attention" not in log
    other = report + [{"status": "modified", "filename": "overlays/values-chainguard.yaml"}]
    (tmp_path / "other").mkdir()
    p, log = run_merge(tmp_path / "other", green(**pulls, **{"pulls/7/files?per_page=100&page=1": other}), branch="bot/security-report")
    assert "pr merge" not in log and "needs-attention" in log


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


def test_a_push_that_builds_yml_also_runs_for_waits_for_builds_yml():
    """A merged bot PR starts builds.yml and this workflow; only the run after builds.yml
    (workflow_run) rebuilds the bot branches, or each merge rebuilt them twice."""
    on = UPDATE[True] if True in UPDATE else UPDATE["on"]   # PyYAML reads `on:` as True
    assert on["workflow_run"]["workflows"] == ["Build - Big Bang images"]
    plan = UPDATE["jobs"]["plan"]
    defer = next(s for s in plan["steps"] if s.get("id") == "defer")
    assert defer["if"] == "github.event_name == 'push'" and "builds.yml/runs?head_sha=" in defer["run"]
    assert plan["permissions"]["actions"] == "read"
    assert plan["steps"][0]["with"]["allowed-endpoints"].split().count("api.github.com:443") == 1
    assert UPDATE["jobs"]["update"]["if"] == "needs.plan.outputs.defer != 'true'"


def test_update_fails_on_a_builder_failure_after_opening_its_prs():
    """`bb-build.py ... || true` turned a broken build into a green run."""
    steps = UPDATE["jobs"]["update"]["steps"]
    run = next(s for s in steps if s.get("id") == "run")["run"]
    for ln in run.splitlines():
        if "bb-build.py" in ln and "scripts/" in ln:
            assert "|| true" not in ln, ln.strip()
    assert run.count('>> "${RUNNER_TEMP}/failed-builds"') == 2     # the parallel builders and the installer
    assert "failed_builds=" in run
    names = [s.get("name") for s in steps]
    last = steps[-1]
    assert last["name"] == "Builders that failed"
    assert names.index("Open the builds pull request") < names.index("Builders that failed")
    assert last["if"] == "steps.run.outputs.failed_builds != ''"
    assert "::error::" in last["run"] and "exit 1" in last["run"]


def test_update_stops_when_the_held_builds_cant_be_read():
    run = next(s for s in UPDATE["jobs"]["update"]["steps"] if s.get("id") == "run")["run"]
    hold = run[run.index("held=$(python3 scripts/bb-held-builds.py hold"):run.index('if [ -n "$held" ]')]
    assert '|| held=""' not in run
    assert "exit 1" in hold and "::error::" in hold


def test_update_needs_the_bot_token_before_doing_any_work():
    steps = UPDATE["jobs"]["update"]["steps"]
    names = [s.get("name") for s in steps]
    check = steps[names.index("A bot token is configured")]
    assert names.index("A bot token is configured") < names.index("Discover, convert, verify")
    assert check["env"] == {"APP": "${{ vars.BOT_APP_CLIENT_ID }}", "PAT": "${{ secrets.RELEASE_PAT }}"}
    assert "no bot token (BOT_APP_CLIENT_ID or RELEASE_PAT)" in check["run"] and "exit 1" in check["run"]
    assert "github.token }}" not in str([s.get("with", {}).get("token") for s in steps])


def test_update_keeps_one_concurrency_group():
    """Two update runs at once would race on the bot branches; record-release re-dispatches."""
    assert UPDATE["concurrency"] == {"group": "update", "cancel-in-progress": False}


COMPAT = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "compat.yml"), encoding="utf-8"))
RECORD = next(s for s in COMPAT["jobs"]["record-release"]["steps"] if "gh workflow run update.yml" in s.get("run", ""))


def run_record(tmp_path, states, gate_rc=1, release_rc=None, tests="success"):
    """record-release's step with fake gh (each `api` call answers the next of `states`),
    python3 (the results gate exits gate_rc; with --release, release_rc, default gate_rc)
    and sleep; TESTS is the test job's result. Returns (process, gh log)."""
    import shutil
    import subprocess
    import pytest
    if os.name == "nt" or not shutil.which("bash"):
        pytest.skip("needs bash (CI's runner has it)")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "states").write_text("".join(s + "\n" for s in states), encoding="utf-8")
    log = tmp_path / "gh.log"
    log.write_text("", encoding="utf-8")
    scripts = {
        "gh": '#!/bin/sh\necho "$*" >> "$FAKE_LOG"\n[ "$1" = api ] || exit 0\n'
              'head -1 "$FAKE_STATES"; sed -i 1d "$FAKE_STATES"\n',
        "python3": (f'#!/bin/sh\ncase "$*" in *--release*) exit {gate_rc if release_rc is None else release_rc};; '
                    f'esac\nexit {gate_rc}\n'),
        "sleep": "#!/bin/sh\n",
    }
    for name, body in scripts.items():
        (bindir / name).write_text(body, encoding="utf-8")
        (bindir / name).chmod(0o755)
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}", FAKE_LOG=str(log),
               FAKE_STATES=str(tmp_path / "states"), GITHUB_REPOSITORY="o/r", GH_TOKEN="t", TESTS=tests)
    p = subprocess.run(["bash", "-c", RECORD["run"]], env=env, capture_output=True, text=True, cwd=tmp_path)
    return p, log.read_text(encoding="utf-8")


def test_record_release_waits_until_its_dispatch_started(tmp_path):
    p, log = run_record(tmp_path, ["null - null", "queued - u", "in_progress - u"])
    assert p.returncode == 0, p.stdout + p.stderr
    assert log.count("workflow run update.yml") == 1 and "started" in p.stdout


def test_record_release_dispatches_again_when_its_run_was_replaced(tmp_path):
    p, log = run_record(tmp_path, ["queued - u", "completed cancelled u", "completed success u"])
    assert p.returncode == 0, p.stdout + p.stderr
    assert log.count("workflow run update.yml") == 2


def test_record_release_fails_when_update_never_starts(tmp_path):
    p, log = run_record(tmp_path, ["completed cancelled u"] * 3)
    assert p.returncode == 1 and log.count("workflow run update.yml") == 3
    assert "::error::update.yml never started" in p.stdout


def test_record_release_does_nothing_once_the_matrix_is_recorded(tmp_path):
    p, log = run_record(tmp_path, [], gate_rc=0)
    assert p.returncode == 0 and "workflow run" not in log


def test_record_release_records_large_tests_only_when_they_passed(tmp_path):
    """The ordinary matrix is recorded; GitLab (or a weekly_and_release test) is not. A pass
    is recorded; a failure is not, since recording it pushes the PR and re-runs the same
    ~90-minute job in a loop."""
    p, log = run_record(tmp_path, ["in_progress - u"], gate_rc=0, release_rc=1)
    assert p.returncode == 0 and log.count("workflow run update.yml") == 1, p.stdout + p.stderr
    (tmp_path / "failed").mkdir()
    p, log = run_record(tmp_path / "failed", [], gate_rc=0, release_rc=1, tests="failure")
    assert p.returncode == 0 and "workflow run" not in log
    assert "::error::a large or weekly_and_release test failed" in p.stdout
    assert "--release" in RECORD["run"] and "needs.test.result" in str(RECORD["env"])
