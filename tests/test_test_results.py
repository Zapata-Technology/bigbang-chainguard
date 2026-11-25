"""test-results.py: docs/testing.md's generated results table."""
import datetime
import json

from conftest import ROOT, load_script

tr = load_script("test-results")


def result(test="loki", mode="standard", res="pass", finished="2026-11-04T03:00:00Z", run=100, attempt=1, **kw):
    r = {"test": test, "mode": mode, "result": res, "bigbang": "4.0.0", "finished": finished,
         "run_id": run, "run_attempt": attempt, "run_url": f"https://github.com/o/r/actions/runs/{run}",
         "commit": "abc", "event": "schedule", "branch": "main"}
    r.update(kw)
    return r


DOC = ("# Testing\n\nProse before.\n\n## Results\n\nIntro prose.\n\n"
       "<!-- results:start -->\nold table\n<!-- results:end -->\n\n"
       "## What the runtime tests found\n\n| Found by | Problem | Fix |\n|---|---|---|\n"
       "| kyverno-reporter | OOM | more memory |\n")


# --- one job's result ---

def test_outcome_maps_to_a_result():
    assert tr.outcome_result("success", "RESULT loki (standard): PASS\n") == "pass"
    assert tr.outcome_result("success", "x\nRESULT vault-upgrade (fips): SKIPPED (no login)\n") == "skipped"
    assert tr.outcome_result("failure", "") == "fail"
    assert tr.outcome_result("cancelled", "") == "error"
    assert tr.outcome_result("skipped", "") == "error"   # an earlier step failed


def test_a_repo1_outage_is_an_error_not_a_package_failure():
    log = ("PROBLEM: repo1.dso.mil: bigbang/keycloak can't fetch (failed to checkout ...)\n"
           "PROBLEM: none of the expected images ran: the test deployed nothing it is about\n"
           "RESULT keycloak.yaml (FIPS): FAIL; 0 of 2 expected images ran\n")
    assert tr.outcome_result("failure", log) == "error"
    # Only bb-compat.py's own PROBLEM line counts, not the host named somewhere else.
    assert tr.outcome_result("failure", "pulling repo1.dso.mil/x can't fetch\nRESULT x: FAIL\n") == "fail"


def test_a_branch_keeps_only_its_version_and_main_only_its_own_results():
    pr = {"test": "loki", "branch": "259/merge", "bigbang": "4.1.1"}
    old = {"test": "loki", "branch": "259/merge", "bigbang": "4.1.0"}
    assert tr.keep(pr, bigbang="4.1.1") and not tr.keep(old, bigbang="4.1.1")
    assert not tr.keep(pr, branches=("main", ""))
    assert tr.keep({"branch": "main"}, branches=("main", "")) and tr.keep(pr)


def test_pull_request_runs_are_listed_only_when_asked(monkeypatch):
    out = '[{"databaseId": 1, "event": "pull_request"}, {"databaseId": 2, "event": "schedule"}]'
    monkeypatch.setattr(tr, "gh", lambda *a: (0, out, ""))
    assert tr.main_runs(5, workflows=("compat.yml",)) == [2]
    assert tr.main_runs(5, "bot/bigbang-release", pull_requests=True, workflows=("compat.yml",)) == [1, 2]


def test_runs_are_read_from_the_kind_and_the_vm_workflows(monkeypatch):
    """rke2.yml and fips-kernel.yml record rke2-default and fips-kernel-<test> results
    too: --from-main and --from-branch list their runs beside compat.yml's."""
    runs = {"compat.yml": '[{"databaseId": 1, "event": "schedule"}]',
            "rke2.yml": '[{"databaseId": 2, "event": "schedule"}, {"databaseId": 3, "event": "pull_request"}]',
            "fips-kernel.yml": '[{"databaseId": 4, "event": "workflow_dispatch"}]'}
    asked = []

    def gh(*args):
        workflow = args[args.index("--workflow") + 1]
        asked.append(workflow)
        return 0, runs[workflow], ""
    monkeypatch.setattr(tr, "gh", gh)
    assert tr.main_runs(5) == [1, 2, 4]
    assert asked == ["compat.yml", "rke2.yml", "fips-kernel.yml"]
    assert tr.main_runs(5, "bot/bigbang-release", pull_requests=True) == [1, 2, 3, 4]


def test_vm_rows_and_flagged_tests_are_labelled(tmp_path):
    (tmp_path / "compat").mkdir()
    (tmp_path / "compat" / "release-upgrade-oldest.yaml").write_text("test:\n  weekly_and_release: true\n")
    assert tr.label("release-upgrade-oldest", str(tmp_path)) == "release-upgrade-oldest (weekly, release PRs)"
    assert tr.label("rke2-default", str(tmp_path)) == "rke2-default"   # no compat file: no flag


def _workflow(name):
    import os
    import yaml
    return yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8"))


def test_the_vm_workflows_record_and_upload_a_result_test_results_reads():
    import fnmatch
    for name, test in (("rke2.yml", "rke2-default"), ("fips-kernel.yml", "fips-kernel-")):
        wf = _workflow(name)
        assert name in tr.WORKFLOWS
        [job] = [j for n, j in wf["jobs"].items() if n not in ("alert", "plan", "security-report")]
        steps = {s.get("name"): s for s in job["steps"]}
        record = steps["Record the result"]
        assert "scripts/test-results.py record" in record["run"] and test in record["run"] + str(record["env"])
        assert "${{" not in record["run"]   # step outputs reach the script as env vars
        upload = steps["Upload the result"]
        assert fnmatch.fnmatch(upload["with"]["name"].split("$")[0] + "x", tr.ARTIFACTS)
        assert upload["with"]["path"] == "${{ runner.temp }}/result"


def test_rke2_runs_on_a_release_pr_and_needs_its_runner():
    wf = _workflow("rke2.yml")
    paths = wf[True]["pull_request"]["paths"]
    assert "BIGBANG_VERSION" in paths
    assert not any(p.startswith("overlays") for p in paths)   # digest refreshes would run it nightly
    job = wf["jobs"]["rke2"]
    guard = next(s for s in job["steps"] if "RKE2_RUNNER" in s.get("name", ""))
    assert guard["env"]["RKE2_RUNNER"] == "${{ vars.RKE2_RUNNER }}"
    assert "::error::" in guard["run"] and "workflow_dispatch" in guard["run"]
    assert job["steps"].index(guard) == 1   # before anything slow: right after harden-runner
    # automerge.yml hears its runs (a release PR's RKE2 run can finish last).
    assert wf["name"] in _workflow("automerge.yml")[True]["workflow_run"]["workflows"]


def test_record_reads_the_jobs_environment():
    env = {"GITHUB_RUN_ID": "41770338111", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_REPOSITORY": "o/r",
           "GITHUB_SERVER_URL": "https://github.com", "GITHUB_SHA": "deadbeef",
           "GITHUB_EVENT_NAME": "schedule", "GITHUB_REF_NAME": "main"}
    now = datetime.datetime(2026, 11, 4, 4, 21, 29, tzinfo=datetime.timezone.utc)
    r = tr.record("gitlab", "fips", "success", "", env, "4.0.0", now)
    assert r == {"test": "gitlab", "mode": "fips", "result": "pass", "bigbang": "4.0.0",
                 "finished": "2026-11-04T04:21:29Z", "run_id": 41770338111, "run_attempt": 2,
                 "run_url": "https://github.com/o/r/actions/runs/41770338111", "commit": "deadbeef",
                 "event": "schedule", "branch": "main"}
    assert tr.valid(r)


# --- parsing and merging ---

def test_read_dir_finds_artifact_jsons_and_skips_junk(tmp_path):
    (tmp_path / "compat-result-loki-fips").mkdir()
    (tmp_path / "compat-result-loki-fips" / "loki-fips.json").write_text(json.dumps(result(mode="fips")))
    (tmp_path / "compat-results").mkdir()
    (tmp_path / "compat-results" / "all.json").write_text(json.dumps([result(), result(test="mimir")]))
    (tmp_path / "broken.json").write_text("{not json")
    (tmp_path / "other.json").write_text(json.dumps({"hello": 1}))
    (tmp_path / "error.json").write_text(json.dumps(result(res="error")))
    got = tr.read_dir(str(tmp_path))
    assert sorted((r["test"], r["mode"]) for r in got) == [("loki", "fips"), ("loki", "standard"), ("mimir", "standard")]


def test_newest_result_per_test_and_mode_wins():
    old = [result(res="pass", finished="2026-11-01T00:00:00Z", run=1)]
    new = [result(res="fail", finished="2026-11-04T00:00:00Z", run=3),
           result(res="pass", finished="2026-11-02T00:00:00Z", run=2),
           result(mode="fips", run=3)]
    merged = tr.merge(old, new)
    assert [(r["mode"], r["result"], r["run_id"]) for r in merged] == [("fips", "pass", 3), ("standard", "fail", 3)]
    # An older download never replaces a newer recorded result.
    assert tr.merge(merged, old) == merged


def test_a_rerun_attempt_beats_the_first_and_errors_never_count():
    first = result(res="fail", run=5, attempt=1)
    second = result(res="pass", run=5, attempt=2)
    assert tr.merge([first], [second]) == [second]
    assert tr.merge([second], [result(res="error", finished="2026-12-01T00:00:00Z")]) == [second]


# --- rendering into docs/testing.md ---

def data():
    return {"tests": [{"test": "loki", "runs": "Loki 3.7", "notes": "Check: a log line"},
                      {"test": "vault-upgrade", "runs": "Vault 1.21 to 2.x", "notes": "Needs Iron Bank"}],
            "results": [result(), result(test="vault-upgrade", mode="fips", res="skipped"),
                        result(test="newtest", run=7)]}


def test_table_keeps_seeded_rows_without_runs_and_adds_new_tests():
    t = tr.table(data(), root=str(ROOT))
    lines = t.splitlines()
    assert lines[0].startswith("<!-- GENERATED by scripts/test-results.py")
    rows = [ln for ln in lines if ln.startswith("| ") and not ln.startswith("| Test")]
    assert [r.split(" | ")[0] for r in rows] == ["| loki", "| vault-upgrade", "| newtest"]
    assert "**PASS** on 4.0.0, 2026-11-04 ([run 100](https://github.com/o/r/actions/runs/100))" in rows[0]
    assert "no run recorded" in rows[0]            # loki fips: never ran, row kept
    assert "Needs Iron Bank" in rows[1] and "**SKIPPED**" in rows[1]
    assert rows[2].endswith("| - |")               # no description yet


def test_large_tests_are_labelled(tmp_path):
    (tmp_path / "compat").mkdir()
    (tmp_path / "compat" / "loki.yaml").write_text("test:\n  large: true\n")
    assert "| loki (large) |" in tr.table(data(), root=str(tmp_path))


def test_replace_leaves_prose_outside_the_markers_untouched():
    block = tr.table(data(), root=str(ROOT))
    out = tr.replace(DOC, block)
    before, after = DOC.split("<!-- results:start -->")[0], DOC.split("<!-- results:end -->")[1]
    assert out.startswith(before + "<!-- results:start -->\n")
    assert out.endswith("<!-- results:end -->" + after)
    assert "old table" not in out
    assert tr.current(out) == block
    assert tr.replace(out, block) == out           # idempotent


def test_replace_keeps_crlf_line_endings():
    doc = DOC.replace("\n", "\r\n")
    out = tr.replace(doc, "| a |\n| b |")
    assert "\n" not in out.replace("\r\n", "")
    assert tr.current(out) == "| a |\n| b |"


def test_missing_markers_are_an_error():
    import pytest
    with pytest.raises(SystemExit, match="results:start"):
        tr.replace("# no markers\n", "x")


def test_update_from_a_directory_writes_both_files(tmp_path, monkeypatch):
    data_file, doc = tmp_path / "test-results.json", tmp_path / "testing.md"
    data_file.write_text(json.dumps(data()))
    doc.write_text(DOC)
    res = tmp_path / "results"
    res.mkdir()
    (res / "loki-fips.json").write_text(json.dumps(result(mode="fips", res="fail", run=200)))
    monkeypatch.setattr(tr, "DATA", str(data_file))
    monkeypatch.setattr(tr, "DOC", str(doc))
    monkeypatch.setattr("sys.argv", ["test-results.py", "update", "--results", str(res)])
    assert tr.main() == 0
    saved = json.loads(data_file.read_text())
    assert {(r["test"], r["mode"]) for r in saved["results"]} == {
        ("loki", "standard"), ("loki", "fips"), ("vault-upgrade", "fips"), ("newtest", "standard")}
    assert saved["tests"] == data()["tests"]       # descriptions untouched
    text = doc.read_text()
    assert "**FAIL** on 4.0.0" in text and "Prose before." in text and "| kyverno-reporter | OOM |" in text
    monkeypatch.setattr("sys.argv", ["test-results.py", "check"])
    assert tr.main() == 0


def test_the_seed_keeps_every_test_the_hand_table_had():
    # The committed file, which update.yml refreshes nightly: a test's newest run and its outcome
    # change whenever it reruns, so only what the seed promised is checked, never a run ID.
    seeded = tr.load()
    names = [t["test"] for t in seeded["tests"]]
    for name in ("vault-2", "release-upgrade", "vault-upgrade", "gitlab", "gitlab-runner", "minio-operator"):
        assert name in names
    assert {r["mode"] for r in seeded["results"] if r["test"] == "gitlab"} == {"standard", "fips"}
    assert all(tr.valid(r) for r in seeded["results"])


def test_a_big_bang_ref_result_is_marked_and_never_enters_the_table():
    env = {"GITHUB_RUN_ID": "7", "GITHUB_REPOSITORY": "o/r"}
    r = tr.record("kyverno-enforce", "standard", "success", "", env, "release-4.2.0", preview=True)
    assert r["preview"] is True and tr.valid(r)
    assert "preview" not in tr.record("kyverno-enforce", "standard", "success", "", env, "4.1.0")
    real = result(test="kyverno-enforce")
    newer = dict(r, finished="2027-01-01T00:00:00Z")
    assert tr.merge([real], [newer]) == [real]
