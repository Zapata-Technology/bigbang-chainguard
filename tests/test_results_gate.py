"""results-gate.py: a Big Bang version with no recorded compat matrix fails CI."""
from conftest import load_script

gate = load_script("results-gate")


def test_a_version_with_every_described_pass_and_the_required_tests_is_clear():
    data = {"tests": [{"test": "loki"}, {"test": "gitlab"}],
            "results": [{"test": t, "mode": "standard", "result": "pass", "bigbang": "4.1.0"}
                        for t in ("loki", *gate.REQUIRED)]}
    assert gate.gaps("4.1.0", data, {"gitlab"}) == ([], [])


def test_a_skip_and_an_older_version_and_a_preview_do_not_count():
    data = {"tests": [{"test": "loki"}], "results": [
        {"test": "loki", "mode": "standard", "result": "skipped", "bigbang": "4.1.0"},
        {"test": "loki", "mode": "standard", "result": "pass", "bigbang": "4.0.0"},
        {"test": "loki", "mode": "standard", "result": "pass", "bigbang": "4.1.0", "preview": True},
        {"test": "release-upgrade-keycloak", "mode": "fips", "result": "pass", "bigbang": "4.1.0"},
    ]}
    required, matrix = gate.gaps("4.1.0", data, set())
    assert "loki" in matrix
    assert "release-upgrade-keycloak" in required


def test_a_registry1_skip_does_not_block_and_does_not_count_as_a_pass():
    data = {"tests": [{"test": "vault-upgrade"}, {"test": "loki"}],
            "results": [{"test": "loki", "mode": "standard", "result": "pass", "bigbang": "4.1.0"},
                        {"test": "vault-upgrade", "mode": "standard", "result": "skipped", "bigbang": "4.1.0"},
                        *[{"test": t, "mode": "standard", "result": "pass", "bigbang": "4.1.0"}
                          for t in gate.REQUIRED]]}
    assert gate.gaps("4.1.0", data, set(), {"vault-upgrade"}) == ([], [])
    # The same skip, if the test were inside the gate, would still be a gap.
    _required, matrix = gate.gaps("4.1.0", data, set())
    assert "vault-upgrade" in matrix


def test_large_tests_are_read_from_compat_files(tmp_path):
    (tmp_path / "compat").mkdir()
    (tmp_path / "compat" / "gitlab.yaml").write_text("test:\n  large: true\n")
    (tmp_path / "compat" / "loki.yaml").write_text("test:\n  enable: [loki]\n")
    assert gate.large_tests(str(tmp_path)) == {"gitlab"}


def std(test, result, finished="2026-11-20T00:00:00Z", **extra):
    return {"test": test, "mode": "standard", "result": result, "bigbang": "4.1.0",
            "finished": finished, "run_url": f"https://example/runs/{test}", **extra}


def run_gate(tmp_path, monkeypatch, capsys, results, tests=("loki", "mimir")):
    """main() on a fake checkout: (exit code, stderr)."""
    import json
    (tmp_path / "docs").mkdir()
    (tmp_path / "BIGBANG_VERSION").write_text("4.1.0\n")
    (tmp_path / "docs" / "test-results.json").write_text(json.dumps(
        {"tests": [{"test": t} for t in tests], "results": results}))
    monkeypatch.setattr(gate, "ROOT", str(tmp_path))
    rc = gate.main()
    return rc, capsys.readouterr().err


def test_a_newest_failure_is_failed_and_an_older_one_is_not():
    data = {"results": [std("loki", "pass", "2026-11-19T00:00:00Z"), std("loki", "fail", "2026-11-20T00:00:00Z"),
                        std("mimir", "fail", "2026-11-19T00:00:00Z"), std("mimir", "pass", "2026-11-20T00:00:00Z"),
                        std("tempo", "fail", preview=True), std("velero", "skipped")]}
    assert gate.failed_standard("4.1.0", data, {"loki", "mimir", "tempo", "velero"}) == \
        [("loki", "https://example/runs/loki")]


def test_main_prints_a_failure_apart_from_the_missing_results(tmp_path, monkeypatch, capsys):
    """A failure that replaced a pass (test-results.py keeps the newest) is not 'missing':
    ci-triage.py would have called it EXPECTED and automerge stayed silent."""
    passes = [std(t, "pass") for t in gate.REQUIRED]
    rc, err = run_gate(tmp_path, monkeypatch, capsys, passes + [std("loki", "fail")])
    assert rc == 1
    assert "4.1.0: compat results FAILED: loki (https://example/runs/loki)" in err
    missing = err.split("compat results are missing", 1)[1]
    assert "mimir" in missing and "loki" not in missing


def test_main_with_only_a_failure_never_says_missing(tmp_path, monkeypatch, capsys):
    passes = [std(t, "pass") for t in gate.REQUIRED]
    rc, err = run_gate(tmp_path, monkeypatch, capsys, passes + [std("loki", "fail"), std("mimir", "pass")])
    assert rc == 1
    assert "compat results FAILED: loki" in err and "compat results are missing" not in err


def test_main_with_only_gaps_keeps_the_missing_wording(tmp_path, monkeypatch, capsys):
    rc, err = run_gate(tmp_path, monkeypatch, capsys, [std("loki", "pass")])
    assert rc == 1
    assert "4.1.0: compat results are missing" in err and "FAILED" not in err
