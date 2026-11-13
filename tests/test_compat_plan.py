"""compat-plan.py: which kind tests a compat.yml run deploys."""
from conftest import load_script

plan_mod = load_script("compat-plan")

D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64


def _root(tmp_path):
    (tmp_path / "compat").mkdir()
    (tmp_path / "compat" / "loki.yaml").write_text("test:\n  enable: [loki]\n")
    (tmp_path / "compat" / "keycloak.yaml").write_text("test:\n  enable: [addons.keycloak]\n")
    (tmp_path / "compat" / "gitlab.yaml").write_text("test:\n  large: true\n  enable: [addons.gitlab]\n")
    (tmp_path / "compat" / "kyverno-enforce.yaml").write_text(
        "test:\n  enable: [kyverno, kyvernoPolicies]\n  setup:\n    - sh compat/support/kyverno-admission.sh\n")
    (tmp_path / "compat" / "metrics-server.yaml").write_text("test:\n  enable: [metricsServer]\n")
    (tmp_path / "compat" / "installer.yaml").write_text("test:\n  installer: true\n  enable: [tempo]\n")
    (tmp_path / "compat" / "velero.yaml").write_text("test:\n  enable: [velero]\n  values_files:\n    - x\n")
    return str(tmp_path)


def _overlay(loki_tag, loki_digest, keycloak="26.1"):
    return (f"# GENERATED\npackages:\n  keycloak:\n    image: {keycloak}\n"
            f"  loki:\n    tag: {loki_tag}\n    digest: {loki_digest}\n")


def _tests(jobs):
    return [(t, m) for t, m, _ in jobs]


def test_a_pull_request_that_changes_one_test_runs_only_that_test_in_both_modes(tmp_path):
    root = _root(tmp_path)
    assert plan_mod.plan("pull_request", [], ["compat/loki.yaml"], True, root) == [
        ("loki", "standard", 45), ("loki", "fips", 45)]


def test_a_changed_large_test_runs_on_its_pull_request(tmp_path):
    root = _root(tmp_path)
    assert plan_mod.plan("pull_request", [], ["compat/gitlab.yaml"], False, root) == [("gitlab", "standard", 90)]


def test_docs_and_unit_tests_run_nothing(tmp_path):
    root = _root(tmp_path)
    assert plan_mod.plan("pull_request", [], ["README.md", "docs/testing.md", "tests/test_x.py"], True, root) == []
    # ...and don't widen a test file's change to every test.
    assert _tests(plan_mod.plan("pull_request", [], ["compat/loki.yaml", "AGENTS.md"], False, root)) == [
        ("loki", "standard")]


def test_an_overlay_tag_change_runs_that_packages_tests_in_that_overlays_mode(tmp_path):
    root = _root(tmp_path)
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "values-chainguard-fips.yaml").write_text(_overlay("3.6", D2))
    old = {"overlays/values-chainguard-fips.yaml": _overlay("3.5", D1)}
    jobs = plan_mod.plan("pull_request", [], list(old), True, root, read_base=old.get)
    assert _tests(jobs) == [("loki", "fips")]


def test_a_digest_only_overlay_change_runs_the_smoke_tests(tmp_path):
    root = _root(tmp_path)
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "values-chainguard.yaml").write_text(_overlay("3.5", D2))
    old = {"overlays/values-chainguard.yaml": _overlay("3.5", D1)}
    jobs = plan_mod.plan("pull_request", [], list(old), True, root, read_base=old.get)
    assert _tests(jobs) == [("kyverno-enforce", "standard"), ("metrics-server", "standard")]


def test_harness_flux_and_mapping_changes_run_the_smoke_tests_standard_only(tmp_path):
    root = _root(tmp_path)
    for changed in (["scripts/bb-compat.py"], ["flux/kustomization.yaml"], ["mapping.yaml"],
                    [".github/workflows/compat.yml"]):
        assert _tests(plan_mod.plan("pull_request", [], changed, True, root)) == [
            ("kyverno-enforce", "standard"), ("metrics-server", "standard")], changed


def test_installer_support_file_and_object_storage_changes(tmp_path):
    root = _root(tmp_path)
    assert _tests(plan_mod.plan("pull_request", [], ["scripts/bbinstall/tls.py"], True, root)) == [
        ("installer", "standard")]
    assert _tests(plan_mod.plan("pull_request", [], ["compat/support/kyverno-admission.sh"], True, root)) == [
        ("kyverno-enforce", "standard")]
    assert _tests(plan_mod.plan("pull_request", [], ["object-storage.yaml"], True, root)) == [("velero", "standard")]


def test_a_new_big_bang_runs_every_test_that_is_not_large_in_both_modes(tmp_path):
    root = _root(tmp_path)
    jobs = plan_mod.plan("pull_request", [], ["BIGBANG_VERSION"], True, root)
    assert len(jobs) == 12 and "gitlab" not in {t for t, _, _ in jobs}


def test_a_deleted_test_file_alone_runs_nothing(tmp_path):
    root = _root(tmp_path)
    assert plan_mod.plan("pull_request", [], ["compat/removed.yaml"], False, root) == []


def test_package_sections_split_a_generated_overlay():
    real, digests = plan_mod.changed_packages(_overlay("3.5", D1), _overlay("3.5", D2, keycloak="26.2"))
    assert real == {"keycloak"} and digests == {"loki"}


def test_named_tests_and_the_schedule(tmp_path):
    root = _root(tmp_path)
    assert plan_mod.plan("workflow_dispatch", ["gitlab"], [], True, root) == [
        ("gitlab", "standard", 90), ("gitlab", "fips", 90)]
    assert [t for t, _, _ in plan_mod.plan("schedule", [], [], False, root)] == [
        "gitlab", "installer", "keycloak", "kyverno-enforce", "loki", "metrics-server", "velero"]


def test_the_release_upgrade_data_tests_follow_their_packages_and_run_weekly():
    # A Keycloak or Mattermost tag change is when a schema migration happens: that
    # package's release-upgrade data test runs with the package's own test.
    assert {"keycloak", "release-upgrade-keycloak"} <= {t for t in plan_mod.all_tests()
                                                         if "keycloak" in plan_mod.enabled(t)}
    assert "release-upgrade-mattermost" in {t for t in plan_mod.all_tests() if "mattermost" in plan_mod.enabled(t)}
    for t in ("release-upgrade-keycloak", "release-upgrade-mattermost"):
        assert not plan_mod.large(t)   # pull requests run them, not only the weekly run
    weekly = {t for t, _, _ in plan_mod.plan("schedule", [], [], True)}
    assert {"release-upgrade", "release-upgrade-keycloak", "release-upgrade-mattermost"} <= weekly
    jobs = _tests(plan_mod.plan("pull_request", [], ["compat/support/upgrade-data.py"], True))
    assert jobs == [(t, "standard") for t in ("installer-upgrade-to", "keycloak-migration", "mattermost-migration",
                                              "release-upgrade-keycloak", "release-upgrade-mattermost")]


def test_a_big_bang_ref_runs_the_preview_handful(tmp_path):
    root = _root(tmp_path)
    (tmp_path / "compat" / "release-upgrade.yaml").write_text("test:\n  enable: [kyverno]\n")
    assert _tests(plan_mod.plan("workflow_call", [], [], False, root, bigbang_ref="release-4.2.0")) == [
        ("kyverno-enforce", "standard"), ("kyverno-enforce", "fips"), ("metrics-server", "standard"),
        ("release-upgrade", "standard")]
    # Named tests still win.
    assert _tests(plan_mod.plan("workflow_dispatch", ["loki"], [], False, root, bigbang_ref="master")) == [
        ("loki", "standard")]


def test_the_big_bang_cache_key_follows_the_version_or_the_refs_commit(tmp_path):
    (tmp_path / "BIGBANG_VERSION").write_text("4.1.0\n")
    root = str(tmp_path)
    assert plan_mod.bigbang_source("", root) == ("bigbang-chart-4.1.0", "4.1.0")
    remote = "1111111111111111\trefs/heads/release-4.2.0\n"
    key, version = plan_mod.bigbang_source("release-4.2.0", root, ls_remote=lambda ref: remote)
    assert version == "release-4.2.0" and key.startswith("bigbang-ref-release-4.2.0-111111111111-")
    moved = "2222222222222222\trefs/heads/release-4.2.0\n"
    assert plan_mod.bigbang_source("release-4.2.0", root, ls_remote=lambda ref: moved)[0] != key


def test_a_refs_commit_prefers_the_branch_then_an_annotated_tags_commit():
    out = "aaa\trefs/tags/4.2.0\nbbb\trefs/tags/4.2.0^{}\n"
    assert plan_mod.ref_commit("4.2.0", out) == "bbb"
    assert plan_mod.ref_commit("master", "ccc\trefs/heads/master\n") == "ccc"
    assert plan_mod.ref_commit("nope", "") is None


def test_a_bad_or_unknown_ref_stops_the_plan(tmp_path):
    import pytest
    for bad in ("a b", "x;rm", "../etc", "-x", "a/b"):
        with pytest.raises(SystemExit):
            plan_mod.bigbang_source(bad, str(tmp_path), ls_remote=lambda ref: "")
    with pytest.raises(SystemExit):
        plan_mod.bigbang_source("release-9.9.9", str(tmp_path), ls_remote=lambda ref: "")
