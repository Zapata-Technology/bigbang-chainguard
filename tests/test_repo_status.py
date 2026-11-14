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


def test_format_local_says_when_the_matrix_is_recorded_and_when_the_script_fails():
    ok = "\n".join(status.format_local("main", [], "4.1.0", 0, "4.1.0: compat matrix recorded\n"))
    assert "results-gate: recorded for 4.1.0" in ok
    assert "EXPECTED" not in ok
    assert "Dirty files: none" in ok
    bad = "\n".join(status.format_local("main", None, "4.1.0", 2, "Traceback"))
    assert "Dirty files: UNKNOWN" in bad
    assert "results-gate: FAILED (exit 2)" in bad
