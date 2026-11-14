"""ci-triage.py: telling a known flake from a real failure, from lines CI printed."""
from conftest import load_script

triage = load_script("ci-triage")

REPO1 = ("22:03:49 source: repo1.dso.mil: bigbang/neuvector can't fetch (failed to checkout and determine "
         "revision: unable to clone 'https://repo1.dso.mil/big-bang/product/packages/neuvector.git'); retrying")


def test_repo1_refusing_the_runner_is_a_flake():
    lines = triage.evidence(REPO1 + "\nRESULT neuvector.yaml (standard): FAIL; 0 of 5 expected images ran\n")
    assert triage.classify(lines) == "repo1.dso.mil refused the runner"


def test_a_network_timeout_while_building_is_a_flake():
    lines = triage.evidence("urllib.error.URLError: <urlopen error timed out>\n"
                            "RESULT: building jmx-exporter failed (see above); nothing was deployed\n")
    assert triage.classify(lines + ["urllib.error.URLError: <urlopen error timed out>"]) == "a network timeout"


def test_anything_pointing_at_the_repo_is_real_even_beside_a_flake():
    for line in ("CHECK FAILED (1): kubectl ... | grep -q ok",
                 "PROBLEM: stopped early: gitlab/web-1 container webservice was OOMKilled",
                 "IMAGE POLICY: docker.io/library/busybox:1 is not from the org",
                 "12:00:00 LIVE gitlab-runner/r-1 gitlab-runner: PANIC: The registration-token needs to be entered"):
        assert triage.classify([REPO1, line]) is None


def test_evidence_keeps_the_lines_that_explain_a_kind_test():
    log = "\n".join(["== flux", "noise", REPO1, "12:00:01 LIVE ns/p c: panic: boom", "EXITED: ns/p c: Error (exit 1)",
                     "NOT EXERCISED: loki", "RESULT loki.yaml (standard): FAIL; 0 of 1 expected images ran"])
    assert triage.evidence(log) == log.splitlines()[2:]


def test_errors_strips_timestamps_and_skips_the_scripts_own_source():
    log = ("verify\tUnit tests\t2026-11-02T00:00:00.1Z FAILED tests/test_x.py::test_y - AssertionError\n"
           "verify\tUnit tests\t2026-11-02T00:00:00.2Z \x1b[36;1mif ! x; then echo ##[error]; fi\n")
    assert triage.errors(log) == ["FAILED tests/test_x.py::test_y - AssertionError"]


def test_a_download_server_error_during_a_build_is_a_flake():
    log = ("build (grafana-plugins)\tUNKNOWN STEP\t2026-11-02T03:48:38.4Z #10 4.355 curl: (22) The requested URL "
           "returned error: 500\n"
           "build (grafana-plugins)\tUNKNOWN STEP\t2026-11-02T03:48:38.6Z ERROR: failed to build: failed to solve\n"
           "build (grafana-plugins)\tUNKNOWN STEP\t2026-11-02T03:48:38.7Z ##[error]Process completed with exit code 1.\n")
    assert triage.classify(triage.errors(log)) == "a download's server answered 5xx"


def test_cancelled_and_timed_out_jobs_count_as_failed(monkeypatch):
    checks = [{"name": "mimir (standard)", "bucket": "cancel", "link": "https://x/actions/runs/1/job/11"},
              {"name": "loki (fips)", "bucket": "fail", "link": "https://x/actions/runs/1/job/12"},
              {"name": "alloy (fips)", "bucket": "pass", "link": "https://x/actions/runs/1/job/13"}]
    jobs = [{"name": "a", "id": 21, "conclusion": "timed_out"},
            {"name": "b", "id": 22, "conclusion": "success"}]
    monkeypatch.setattr(triage.ghrest, "pr_sha", lambda n: "abc")
    monkeypatch.setattr(triage.ghrest, "checks", lambda sha: checks if sha == "abc" else [])
    monkeypatch.setattr(triage.ghrest, "api_list", lambda path, key=None, **k: jobs if path == "actions/runs/7/jobs" else [])
    assert triage.failed_jobs(pr=5) == [("mimir (standard)", "1", "11"), ("loki (fips)", "1", "12")]
    assert triage.failed_jobs(run=7) == [("a", "7", "21")]


def test_a_go_proxy_stream_error_and_a_sigstore_conflict_are_flakes():
    go = ('#13 399.1 pkg/x.go:11:2: github.com/kudobuilder/kuttl@v0.25.0: read '
          '"https://proxy.golang.org/github.com/kudobuilder/kuttl/@v/v0.25.0.zip": stream error: stream ID 1; INTERNAL_ERROR')
    assert triage.classify([go]) == "the Go module proxy dropped a download"
    rekor = ('Error: signing ghcr.io/x/y@sha256:1: signing bundle: error signing bundle: '
             '[POST /api/v1/log/entries][409] createLogEntryConflict {"code":409}')
    assert triage.classify([rekor]) == "Sigstore's log timed out (duplicate entry on retry)"


def test_a_refused_connection_names_the_host_to_allow_and_is_no_flake():
    # 2026-11-11: harden-runner began refusing unlisted hosts; cosign's timestamp and a Go
    # module zip (behind a signed URL longer than the evidence cut) failed this way.
    refused = ": dial tcp 54.185.253.63:443: connect: connection refused"
    log = ("2026-11-11T18:06:09.83Z Error: signing [ghcr.io/o/vault@sha256:a96e]: signing bundle: "
           'error signing bundle: Post "https://timestamp.sigstore.dev/api/v1/timestamp"' + refused + "\n"
           "2026-11-11T17:13:35.94Z #12 12.55 x.go:9:2: sigs.k8s.io/y@v0.0.5: Get "
           '"https://storage.googleapis.com/proxy-golang-org-prod/' + "a" * 300 + '"' + refused + "\n"
           "2026-11-11T17:13:39.01Z error in connecting to https://agent.api.stepsecurity.io/v1: "
           "TimeoutError: The operation was aborted due to timeout\n")
    lines = triage.errors(log)
    assert triage.blocked(lines) == ["storage.googleapis.com", "timestamp.sigstore.dev"]
    assert triage.classify(lines) is None                     # a re-run fails the same way
    assert not any("stepsecurity" in ln for ln in lines)      # harden-runner's own telemetry


def test_flakes_count_failed_attempts_of_runs_that_passed_after_a_rerun():
    runs = [
        {"databaseId": 1, "attempt": 3, "workflowName": "Compat", "createdAt": "2026-11-03T00:00:00Z", "conclusion": "success"},
        {"databaseId": 2, "attempt": 2, "workflowName": "Compat", "createdAt": "2026-11-04T00:00:00Z", "conclusion": "success"},
        # Still failing: a real failure, not a flake.
        {"databaseId": 3, "attempt": 2, "workflowName": "Compat", "createdAt": "2026-11-04T00:00:00Z", "conclusion": "failure"},
        # Never re-run, or too old.
        {"databaseId": 4, "attempt": 1, "workflowName": "CI", "createdAt": "2026-11-04T00:00:00Z", "conclusion": "success"},
        {"databaseId": 5, "attempt": 2, "workflowName": "CI", "createdAt": "2026-09-01T00:00:00Z", "conclusion": "success"},
    ]
    failed = {(1, 1): ["tempo-query (standard)"], (1, 2): ["tempo-query (standard)", "mimir (fips)"],
              (2, 1): ["tempo-query (standard)"], (3, 1): ["gitlab (fips)"], (5, 1): ["verify"]}
    counts = triage.rerun_counts(runs, lambda r, n: failed.get((r, n), []), "2026-10-05T00:00:00Z")
    assert counts == {"Compat / tempo-query (standard)": [3, "2026-11-04T00:00:00Z"],
                      "Compat / mimir (fips)": [1, "2026-11-03T00:00:00Z"]}
    lines, fix = triage.flake_report(counts, threshold=3)
    assert fix == ["Compat / tempo-query (standard)"]
    assert lines[0].strip().startswith("FIX") and "3x" in lines[0]
    assert triage.flake_report({}, 3) == (["no re-run jobs in this period"], [])


def test_bb_convert_findings_are_evidence_and_the_failed_step_is_named(monkeypatch):
    log = "\n".join([
        '2026-11-13T05:16:12.0Z echo "::error Unable to assume the identity ${IDENTITY}."',
        "2026-11-13T05:17:07.1Z Images not in mapping.yaml (add them):",
        "2026-11-13T05:17:07.1Z   gitlab-org/gitlab-runner",
        "2026-11-13T05:17:07.1Z Images with no usable org tag:",
        "2026-11-13T05:17:07.2Z   sonarqube: ironbank/sonarsource/sonarqube/x:26.11 -> sonarqube: no 26.x tag at or above 26.11",
        "2026-11-13T05:17:07.2Z ##[error]Process completed with exit code 1.",
    ])
    lines = triage.errors(log)
    assert "Images not in mapping.yaml (add them):" in lines and "  gitlab-org/gitlab-runner" in lines
    assert any("no 26.x tag" in ln for ln in lines)
    assert triage.classify(lines) is None
    monkeypatch.setattr(triage, "gh", lambda *a, **k: log)
    monkeypatch.setattr(triage.ghrest, "api", lambda path: {"steps": [
        {"name": "Log into Chainguard", "conclusion": "success"},
        {"name": "Overlays are current", "conclusion": "failure"}]})
    [(name, run, flake, out)] = triage.triage("o/r", [("verify", "1", "2")])
    assert out[0] == "failed step: Overlays are current" and flake is None


def test_a_missing_compat_matrix_is_expected_and_a_gate_crash_is_real():
    raw = "4.1.0: compat results are missing\n  no standard pass for: release-upgrade-keycloak\n  a SKIPPED run is not a pass.\n"
    assert triage.outcome("compat matrix recorded for this Big Bang version", raw, []) == "expected"
    assert triage.outcome("results-gate", raw, []) == "expected"
    lines = triage.gate_lines(raw, ["failed step: This Big Bang version has a recorded compat matrix"])
    assert "Do not write pass rows. verify is the required check." in lines
    assert "python scripts/test-results.py update" in lines[-1]
    assert triage.outcome("compat matrix recorded for this Big Bang version", "Traceback (most recent call last):\n", ["Traceback"]) is None
    assert triage.outcome("verify", raw, []) is None


def test_triage_marks_the_gate_expected_and_counts_it_apart_from_flakes(monkeypatch):
    raw = "4.1.0: compat results are missing\n"
    monkeypatch.setattr(triage, "gh", lambda *a, **k: raw)
    monkeypatch.setattr(triage, "failed_steps", lambda job: ["This Big Bang version has a recorded compat matrix"])
    [(name, run, code, lines)] = triage.triage("o/r", [("compat matrix recorded for this Big Bang version", "9", "3")])
    assert name.startswith("compat matrix") and run == "9" and code == "expected"
    assert lines[0].startswith("failed step:")
    assert any("Do not write pass rows" in ln for ln in lines)


def test_a_kind_test_that_stopped_before_deploying_on_repo1_is_a_flake():
    log = "\n".join([
        "== release-upgrade.yaml: trial mapping {}",
        "wrote overlays/values-chainguard.yaml",
        "fatal: unable to access 'https://repo1.dso.mil/big-bang/bigbang.git/': Failed to connect to "
        "repo1.dso.mil port 443 after 133453 ms: Could not connect to server",
        "upgrade_from.release: cloning Big Bang 4.0.0 (what 4.0.0-cg.28 deploys) failed",
    ])
    lines = triage.evidence(log)
    assert any("repo1.dso.mil" in ln for ln in lines)
    assert triage.classify(lines) == "repo1.dso.mil refused the runner"
