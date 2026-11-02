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
