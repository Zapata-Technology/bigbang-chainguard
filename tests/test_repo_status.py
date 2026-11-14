"""repo-status.py: the local facts an agent needs before it edits."""
from conftest import load_script

status = load_script("repo-status")


def test_format_local_says_the_gate_is_expected_red():
    lines = status.format_local("fix/x", [" M AGENTS.md"], "4.1.0", 1, "4.1.0: compat results are missing\n")
    text = "\n".join(lines)
    assert "Branch: fix/x" in text
    assert "Dirty files: 1" in text and "M AGENTS.md" in text
    assert "Big Bang 4.1.0" in text
    assert "EXPECTED RED" in text
    assert "Do not write pass rows" in text
    assert "python scripts/test-results.py update" in text


def test_overlay_lag_names_settings_the_overlays_do_not_have_yet(tmp_path):
    (tmp_path / "mapping.yaml").write_text(
        "upstream:\n  webservice:\n    deployment: {livenessProbe: {failureThreshold: 15}}\n"
        "progress_deadline:\n  packages.gitlab: 2400\n",
        encoding="utf-8")
    overlays = tmp_path / "overlays"
    overlays.mkdir()
    (overlays / "values-chainguard.yaml").write_text("packages: {}\n", encoding="utf-8")
    (overlays / "values-chainguard-fips.yaml").write_text("packages: {}\n", encoding="utf-8")
    notes = "\n".join(status.overlay_lag(str(tmp_path)))
    assert "progress_deadline" in notes
    assert "failureThreshold 15" in notes
    assert "update.yml" in notes
    assert "bb-convert.py offline" in notes
    assert "hand-edit overlays" in notes


def test_overlay_lag_is_quiet_once_either_overlay_has_the_settings(tmp_path):
    (tmp_path / "mapping.yaml").write_text(
        "failureThreshold: 15\nprogress_deadline:\n  packages.gitlab: 2400\n",
        encoding="utf-8")
    overlays = tmp_path / "overlays"
    overlays.mkdir()
    (overlays / "values-chainguard.yaml").write_text(
        "failureThreshold: 15\n- op: add\n  path: /spec/progressDeadlineSeconds\n",
        encoding="utf-8")
    (overlays / "values-chainguard-fips.yaml").write_text("untouched\n", encoding="utf-8")
    assert status.overlay_lag(str(tmp_path)) == []


def test_format_local_says_when_the_matrix_is_recorded_and_when_the_script_fails():
    ok = "\n".join(status.format_local("main", [], "4.1.0", 0, "4.1.0: compat matrix recorded\n"))
    assert "results-gate: recorded for 4.1.0" in ok
    assert "EXPECTED" not in ok
    assert "Dirty files: none" in ok
    bad = "\n".join(status.format_local("main", None, "4.1.0", 2, "Traceback"))
    assert "Dirty files: UNKNOWN" in bad
    assert "results-gate: FAILED (exit 2)" in bad
