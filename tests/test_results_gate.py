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
