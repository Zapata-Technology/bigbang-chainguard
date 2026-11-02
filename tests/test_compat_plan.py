"""compat-plan.py: which kind tests a compat.yml run deploys."""
from conftest import load_script

plan_mod = load_script("compat-plan")


def _root(tmp_path):
    (tmp_path / "compat").mkdir()
    (tmp_path / "compat" / "loki.yaml").write_text("test:\n  enable: [loki]\n")
    (tmp_path / "compat" / "keycloak.yaml").write_text("test:\n  enable: [addons.keycloak]\n")
    (tmp_path / "compat" / "gitlab.yaml").write_text("test:\n  large: true\n  enable: [addons.gitlab]\n")
    return str(tmp_path)


def test_a_pull_request_that_changes_one_test_runs_only_that_test_in_both_modes(tmp_path):
    root = _root(tmp_path)
    assert plan_mod.plan("pull_request", [], ["compat/loki.yaml"], True, root) == [
        ("loki", "standard", 45), ("loki", "fips", 45)]


def test_a_changed_large_test_runs_on_its_pull_request(tmp_path):
    root = _root(tmp_path)
    assert plan_mod.plan("pull_request", [], ["compat/gitlab.yaml"], False, root) == [("gitlab", "standard", 90)]


def test_any_other_change_runs_every_test_that_is_not_large(tmp_path):
    root = _root(tmp_path)
    for changed in (["compat/loki.yaml", "mapping.yaml"], ["compat/support/app-deps.py"], ["overlays/x.yaml"], []):
        assert [t for t, _, _ in plan_mod.plan("pull_request", [], changed, False, root)] == ["keycloak", "loki"]


def test_a_deleted_test_file_alone_falls_back_to_every_test(tmp_path):
    root = _root(tmp_path)
    assert [t for t, _, _ in plan_mod.plan("pull_request", [], ["compat/removed.yaml"], False, root)] == [
        "keycloak", "loki"]


def test_named_tests_and_the_schedule(tmp_path):
    root = _root(tmp_path)
    assert plan_mod.plan("workflow_dispatch", ["gitlab"], [], True, root) == [
        ("gitlab", "standard", 90), ("gitlab", "fips", 90)]
    assert [t for t, _, _ in plan_mod.plan("schedule", [], [], False, root)] == ["gitlab", "keycloak", "loki"]
