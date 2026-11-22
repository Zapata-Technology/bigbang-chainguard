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
    assert _tests(jobs) == [("keycloak", "standard"), ("kyverno-enforce", "standard"),
                            ("metrics-server", "standard"), ("velero", "standard")]


def test_harness_flux_and_mapping_changes_run_the_smoke_tests_standard_only(tmp_path):
    root = _root(tmp_path)
    for changed in (["scripts/bb-compat.py"], ["flux/kustomization.yaml"], ["mapping.yaml"],
                    [".github/workflows/compat.yml"]):
        assert _tests(plan_mod.plan("pull_request", [], changed, True, root)) == [
            ("keycloak", "standard"), ("kyverno-enforce", "standard"),
            ("metrics-server", "standard"), ("velero", "standard")], changed


def test_the_converter_and_its_modules_run_the_smoke_tests(tmp_path):
    root = _root(tmp_path)
    for script in ("bb-convert", "bbcommon", "bbmirror", "orgtags", "objstore", "fluxrelease", "release"):
        assert _tests(plan_mod.plan("pull_request", [], [f"scripts/{script}.py"], True, root)) == [
            ("keycloak", "standard"), ("kyverno-enforce", "standard"),
            ("metrics-server", "standard"), ("velero", "standard")], script


def test_a_build_change_runs_the_tests_that_build_it_and_the_smoke_tests(tmp_path):
    root = _root(tmp_path)
    (tmp_path / "compat" / "tempo-query.yaml").write_text(
        "images:\n  ironbank/opensource/grafana/tempo-query: {build: tempo-query}\ntest:\n  enable: [tempo]\n")
    (tmp_path / "compat" / "vault-2.yaml").write_text(
        'images:\n  ironbank/hashicorp/vault: {build: vault, version: "*"}\ntest:\n  enable: [vault]\n')
    smoke = [("keycloak", "standard"), ("kyverno-enforce", "standard"), ("metrics-server", "standard"),
             ("velero", "standard")]
    jobs = _tests(plan_mod.plan("pull_request", [], ["builds/vault/build.yaml"], True, root))
    assert jobs == sorted(smoke + [("vault-2", "standard")])
    jobs = _tests(plan_mod.plan("pull_request", [], ["builds/tempo-query/Dockerfile"], True, root))
    assert jobs == sorted(smoke + [("tempo-query", "standard")])
    # A build no test uses (base): the smoke tests alone.
    assert _tests(plan_mod.plan("pull_request", [], ["builds/base/recipe.yaml"], True, root)) == smoke


def test_compat_yml_paths_cover_everything_bb_compat_imports_or_runs():
    """A change to code a kind test runs must start compat.yml (it missed bb-convert.py,
    which writes every overlay a test deploys), and compat-plan.py must not plan nothing."""
    import ast
    import os
    import re
    import yaml
    from conftest import ROOT, SCRIPTS
    wf = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "compat.yml"), encoding="utf-8"))
    paths = (wf[True] if True in wf else wf["on"])["pull_request"]["paths"]

    def local(name):
        for cand in (os.path.join(SCRIPTS, name + ".py"), os.path.join(SCRIPTS, name, "__init__.py")):
            if os.path.exists(cand):
                return cand
        return None

    def deps(path):
        text = open(path, encoding="utf-8").read()
        found = set()
        for n in ast.walk(ast.parse(text)):
            if isinstance(n, ast.Import):
                found |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                found.add(n.module.split(".")[0])
        # Scripts it runs: os.path.join(HERE, "bb-convert.py").
        found |= {m[:-3] for m in re.findall(r'os\.path\.join\(HERE, "([\w-]+\.py)"\)', text)}
        return {d for d in found if local(d)}

    seen, todo = set(), ["bb-compat"]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        path = local(name)
        if path.endswith("__init__.py"):
            pkg = os.path.dirname(path)
            for f in os.listdir(pkg):
                if f.endswith(".py"):
                    todo += deps(os.path.join(pkg, f))
        else:
            todo += deps(path)
    assert {"bb-convert", "bbcommon", "orgtags", "bbmirror"} <= seen   # the walk works
    for name in sorted(seen):
        is_pkg = local(name).endswith("__init__.py")
        want = f"scripts/{name}/**" if is_pkg else f"scripts/{name}.py"
        assert want in paths, f"compat.yml's pull_request paths miss {want}"
        changed = f"scripts/{name}/x.py" if is_pkg else want
        assert plan_mod.CLUSTER.match(changed) or plan_mod.INSTALLER.match(changed), \
            f"compat-plan.py plans no test for {changed}"
    assert "builds/**" in paths


def test_installer_support_file_and_object_storage_changes(tmp_path):
    root = _root(tmp_path)
    assert _tests(plan_mod.plan("pull_request", [], ["scripts/bbinstall/tls.py"], True, root)) == [
        ("installer", "standard")]
    assert _tests(plan_mod.plan("pull_request", [], ["compat/support/kyverno-admission.sh"], True, root)) == [
        ("kyverno-enforce", "standard")]
    assert _tests(plan_mod.plan("pull_request", [], ["object-storage.yaml"], True, root)) == [("velero", "standard")]


def test_a_new_big_bang_runs_every_test_in_both_modes_and_the_large_ones_standard(tmp_path):
    root = _root(tmp_path)
    jobs = _tests(plan_mod.plan("pull_request", [], ["BIGBANG_VERSION"], True, root))
    assert len(jobs) == 13
    assert ("gitlab", "standard") in jobs and ("gitlab", "fips") not in jobs
    assert ("loki", "fips") in jobs


def test_a_large_test_runs_standard_when_its_package_changes_in_the_standard_overlay(tmp_path):
    root = _root(tmp_path)
    (tmp_path / "overlays").mkdir()
    old = "packages:\n  gitlab:\n    tag: 18.5\n"
    new = "packages:\n  gitlab:\n    tag: 18.6\n"
    for name in ("values-chainguard.yaml", "values-chainguard-fips.yaml"):
        (tmp_path / "overlays" / name).write_text(new)
    std = ["overlays/values-chainguard.yaml"]
    assert _tests(plan_mod.plan("pull_request", [], std, True, root, read_base=lambda p: old)) == [
        ("gitlab", "standard")]
    # A FIPS-only change: the weekly run, not a ~90-minute job on the pull request.
    assert plan_mod.plan("pull_request", [], ["overlays/values-chainguard-fips.yaml"], True, root,
                         read_base=lambda p: old) == []
    # Digests alone: the smoke tests, never GitLab.
    digest_old = f"packages:\n  gitlab:\n    tag: 18.5\n    digest: {D1}\n"
    digest_new = f"packages:\n  gitlab:\n    tag: 18.5\n    digest: {D2}\n"
    (tmp_path / "overlays" / "values-chainguard.yaml").write_text(digest_new)
    jobs = _tests(plan_mod.plan("pull_request", [], std, True, root, read_base=lambda p: digest_old))
    assert "gitlab" not in {t for t, _ in jobs} and ("kyverno-enforce", "standard") in jobs


def test_a_recorded_release_pr_still_runs_the_large_tests_it_has_not_passed(tmp_path):
    root = _root(tmp_path)
    jobs = _tests(plan_mod.plan("pull_request", [], ["BIGBANG_VERSION"], True, root, recorded=True,
                                pending={"gitlab"}))
    assert jobs == [("gitlab", "standard"), ("keycloak", "standard"), ("kyverno-enforce", "standard"),
                    ("metrics-server", "standard"), ("velero", "standard")]


def _weekly_root(tmp_path):
    root = _root(tmp_path)
    (tmp_path / "compat" / "release-upgrade-oldest.yaml").write_text(
        "test:\n  weekly_and_release: true\n  enable: [loki]\n  setup:\n    - sh compat/support/kyverno-admission.sh\n")
    return root


def test_a_weekly_and_release_test_runs_only_for_its_file_and_a_new_big_bang(tmp_path):
    root = _weekly_root(tmp_path)
    name = "release-upgrade-oldest"
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "values-chainguard.yaml").write_text(_overlay("3.6", D2))
    old = {"overlays/values-chainguard.yaml": _overlay("3.5", D1)}
    for changed in (list(old), ["compat/support/kyverno-admission.sh"], ["mapping.yaml"]):
        jobs = plan_mod.plan("pull_request", [], changed, True, root, read_base=old.get)
        assert name not in {t for t, _, _ in jobs}, changed
    assert _tests(plan_mod.plan("pull_request", [], [f"compat/{name}.yaml"], True, root)) == [
        (name, "standard"), (name, "fips")]
    assert (name, "fips") in _tests(plan_mod.plan("pull_request", [], ["BIGBANG_VERSION"], True, root))
    assert name in {t for t, _, _ in plan_mod.plan("schedule", [], [], False, root)}


def test_a_release_pr_with_its_matrix_recorded_runs_the_smoke_tests_only(tmp_path):
    root = _root(tmp_path)
    old, new = _overlay("3.0", "sha256:" + "a" * 64), _overlay("3.1", "sha256:" + "b" * 64)
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "values-chainguard.yaml").write_text(new)
    changed = ["BIGBANG_VERSION", "overlays/values-chainguard.yaml"]
    jobs = _tests(plan_mod.plan("pull_request", [], changed, True, root, read_base=lambda p: old, recorded=True))
    assert jobs == [("keycloak", "standard"), ("kyverno-enforce", "standard"),
                    ("metrics-server", "standard"), ("velero", "standard")]
    # A changed test file still runs that test.
    jobs = _tests(plan_mod.plan("pull_request", [], [*changed, "compat/loki.yaml"], True, root,
                                read_base=lambda p: old, recorded=True))
    assert ("loki", "fips") in jobs
    # Not recorded yet: the whole matrix (GitLab standard only).
    assert len(plan_mod.plan("pull_request", [], changed, True, root, read_base=lambda p: old)) == 13


def test_matrix_recorded_follows_the_results_gate(tmp_path):
    import json
    root = _root(tmp_path)
    (tmp_path / "BIGBANG_VERSION").write_text("4.1.1\n")
    assert not plan_mod.matrix_recorded(root)   # no docs/test-results.json
    (tmp_path / "docs").mkdir()
    gate = load_script("results-gate")
    names = ["loki", "keycloak", *gate.REQUIRED]
    data = {"tests": [{"test": t} for t in names],
            "results": [{"test": t, "mode": "standard", "result": "pass", "bigbang": "4.1.1"} for t in names]}
    (tmp_path / "docs" / "test-results.json").write_text(json.dumps(data))
    assert plan_mod.matrix_recorded(root)
    (tmp_path / "BIGBANG_VERSION").write_text("4.1.2\n")
    assert not plan_mod.matrix_recorded(root)


def test_release_pending_is_what_the_release_gate_still_needs(tmp_path):
    import json
    root = _weekly_root(tmp_path)
    (tmp_path / "BIGBANG_VERSION").write_text("4.1.1\n")
    assert plan_mod.release_pending(root) == {"gitlab", "release-upgrade-oldest"}   # nothing recorded
    (tmp_path / "docs").mkdir()
    gate = load_script("results-gate")
    names = ["loki", "keycloak", "gitlab", "release-upgrade-oldest", *gate.REQUIRED]

    def results(passed, version="4.1.1"):
        return [{"test": t, "mode": "standard", "result": "pass", "bigbang": version} for t in passed]
    data = {"tests": [{"test": t} for t in names], "results": results(["loki", "keycloak", *gate.REQUIRED])}
    (tmp_path / "docs" / "test-results.json").write_text(json.dumps(data))
    # The ordinary matrix is recorded; GitLab isn't, and the new test has never passed (not required yet).
    assert plan_mod.matrix_recorded(root)
    assert plan_mod.release_pending(root) == {"gitlab"}
    # Once it passed on an older version, the new one needs it too.
    data["results"] += results(["release-upgrade-oldest", "gitlab"], "4.1.0")
    (tmp_path / "docs" / "test-results.json").write_text(json.dumps(data))
    assert plan_mod.release_pending(root) == {"gitlab", "release-upgrade-oldest"}
    data["results"] += results(["release-upgrade-oldest", "gitlab"])
    (tmp_path / "docs" / "test-results.json").write_text(json.dumps(data))
    assert plan_mod.release_pending(root) == set()


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


def test_a_digest_change_runs_the_widened_smoke_list_when_those_files_exist(tmp_path):
    root = _root(tmp_path)
    (tmp_path / "compat" / "object-storage.yaml").write_text("test:\n  enable: [tempo]\n")
    (tmp_path / "compat" / "kiali.yaml").write_text("test:\n  enable: [kiali]\n")
    (tmp_path / "overlays").mkdir()
    (tmp_path / "overlays" / "values-chainguard.yaml").write_text(_overlay("3.5", D2))
    old = {"overlays/values-chainguard.yaml": _overlay("3.5", D1)}
    jobs = plan_mod.plan("pull_request", [], list(old), True, root, read_base=old.get)
    assert _tests(jobs) == [(t, "standard") for t in sorted(plan_mod.SMOKE)]


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
    for name in ("release-upgrade", "release-upgrade-keycloak", "release-upgrade-mattermost",
                 "installer-upgrade-to"):
        (tmp_path / "compat" / f"{name}.yaml").write_text("test:\n  enable: [kyverno]\n")
    assert _tests(plan_mod.plan("workflow_call", [], [], False, root, bigbang_ref="release-4.2.0")) == [
        ("installer-upgrade-to", "standard"), ("kyverno-enforce", "standard"), ("kyverno-enforce", "fips"),
        ("metrics-server", "standard"), ("release-upgrade", "standard"), ("release-upgrade-keycloak", "standard"),
        ("release-upgrade-mattermost", "standard")]
    # Every PREVIEW test exists in this repo, standard runners only (FIPS just for the smallest).
    assert {n for n, _ in plan_mod.PREVIEW} <= set(plan_mod.all_tests())
    assert not any(plan_mod.large(n) for n, _ in plan_mod.PREVIEW)
    # Named tests still win.
    assert _tests(plan_mod.plan("workflow_dispatch", ["loki"], [], False, root, bigbang_ref="master")) == [
        ("loki", "standard")]


def test_the_big_bang_cache_key_follows_the_version_or_the_refs_commit(tmp_path):
    (tmp_path / "BIGBANG_VERSION").write_text("4.1.0\n")
    root = str(tmp_path)
    assert plan_mod.bigbang_source("", root) == ("bigbang-mirror-4.1.0", "4.1.0")
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
