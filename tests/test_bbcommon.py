"""Settings, mapping overrides, image references and value-path discovery."""
import os

import pytest

import bbcommon


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A scratch repo root for settings and org files."""
    monkeypatch.setattr(bbcommon, "ROOT", str(tmp_path))
    monkeypatch.delenv("CHAINGUARD_ORG", raising=False)
    monkeypatch.delenv("BUILD_REGISTRY", raising=False)
    return tmp_path


def test_setting_precedence(repo, monkeypatch):
    (repo / "CHAINGUARD_ORG").write_text("# comment\nfile.example\n")
    assert bbcommon.setting("CHAINGUARD_ORG") == "file.example"
    monkeypatch.setenv("CHAINGUARD_ORG", "env.example")
    assert bbcommon.setting("CHAINGUARD_ORG") == "env.example"
    assert bbcommon.setting("CHAINGUARD_ORG", "flag.example") == "flag.example"


def test_setting_with_only_comments_is_unset(repo):
    (repo / "CHAINGUARD_ORG").write_text("# put your org here\n\n")
    assert bbcommon.setting("CHAINGUARD_ORG") == ""
    assert bbcommon.setting("MISSING_FILE") == ""


def test_build_registry_refuses_cgr_dev(repo):
    with pytest.raises(SystemExit):
        bbcommon.build_registry("cgr.dev/example.com")
    assert bbcommon.build_registry("ghcr.io/owner/") == "ghcr.io/owner"


def test_org_file_replaces_image_entries_and_merges_the_rest(repo):
    (repo / "mapping.yaml").write_text(
        "images:\n  ironbank/a: a\n  ironbank/b: {cg: b, fips: null}\n"
        "overrides:\n  pkg: {x: 1, y: 2}\n")
    (repo / "orgs").mkdir()
    (repo / "orgs" / "me.example.yaml").write_text(
        "images:\n  ironbank/b: {stays: vendor}\noverrides:\n  pkg: {y: 3}\n")
    m = bbcommon.load_mapping("me.example")
    assert m["images"] == {"ironbank/a": "a", "ironbank/b": {"stays": "vendor"}}
    assert m["overrides"]["pkg"] == {"x": 1, "y": 3}
    assert bbcommon.load_mapping("other.example")["images"]["ironbank/b"] == {"cg": "b", "fips": None}


def test_build_target(repo):
    os.makedirs(repo / "builds" / "img")
    (repo / "builds" / "img" / "build.yaml").write_text(
        "image:\n  standard: {name: bigbang-img, tag: '1.0'}\n  fips: {name: bigbang-img-fips, tag: '1.1'}\n")
    assert bbcommon.build_target({"build": "img"}, "ghcr.io/o") == ("ghcr.io/o/bigbang-img:1.0",
                                                                    "ghcr.io/o/bigbang-img-fips:1.1")
    assert bbcommon.build_target({"build": "img"}, "") is None
    assert bbcommon.build_target({"build": "absent"}, "ghcr.io/o") is None


def test_split_ref():
    assert bbcommon.split_ref("registry1.dso.mil/ironbank/x/y:1.2") == ("ironbank/x/y", "1.2")
    assert bbcommon.split_ref("registry1.dso.mil/ironbank/x/y@sha256:ab") == ("ironbank/x/y", None)
    assert bbcommon.split_ref("localhost:5000/x") == ("localhost:5000/x", None)


def test_ironbank_refs_reads_rendered_and_value_only_images():
    info = {"rendered": ["registry1.dso.mil/ironbank/a:1"],
            "values": [{"shape": "repo", "value": {"repository": "registry1.dso.mil/ironbank/b", "tag": "2"}},
                       {"shape": "name", "value": {"registry": "registry1.dso.mil", "image": "ironbank/c",
                                                   "tag": "3"}},
                       {"shape": "string", "value": "registry1.dso.mil/ironbank/d", "tag": "4"}]}
    assert bbcommon.ironbank_refs(info) == {("ironbank/a", "1"), ("ironbank/b", "2"),
                                            ("ironbank/c", "3"), ("ironbank/d", "4")}


def test_image_nodes_shapes():
    values = {
        "global": {"imageRegistry": "registry1.dso.mil"},
        "app": {"image": {"repository": "registry1.dso.mil/ironbank/app", "tag": "1"}},
        "split": {"image": {"registry": "registry1.dso.mil", "repository": "ironbank/split", "tag": "2"}},
        "istio": {"hub": "registry1.dso.mil/ironbank/opensource/istio", "tag": "1.31"},
        "job": {"image": "registry1.dso.mil/ironbank/job:3"},
        "list": [{"image": "registry1.dso.mil/ironbank/inlist:4"}],
        "other": {"image": {"repository": "docker.io/library/nginx", "tag": "1"}},
    }
    found = {(".".join(map(str, p)), shape) for p, shape, _ in bbcommon.image_nodes(values)}
    assert found == {("global.imageRegistry", "registry"), ("app.image", "repo"), ("split.image", "repo"),
                     ("istio", "hub"), ("job.image", "string"), ("list.0.image", "string")}


def test_mapping_overlays_apply_after_the_org_file_and_drop_test(repo):
    (repo / "mapping.yaml").write_text("images:\n  ironbank/v: {stays: major}\n")
    trial = repo / "trial.yaml"
    trial.write_text("images:\n  ironbank/v: {cg: v, version: '*'}\ntest:\n  enable: [addons.v]\n")
    m = bbcommon.load_mapping("me.example", overlays=[str(trial)])
    assert m["images"]["ironbank/v"] == {"cg": "v", "version": "*"}
    assert "test" not in m


MAP = {"images": {"ironbank/vendor": {"stays": "proprietary"}, "ironbank/app": "app",
                  "ironbank/mgr": {"cg": "mgr", "fips": None}}}


def test_running_images_from_allowed_registries_pass():
    imgs = ["cgr.dev/me.example/app:1.0", "ghcr.io/me/bigbang-base:2", "registry1.dso.mil/ironbank/vendor:3",
            "registry.k8s.io/coredns/coredns:v1.12.1", "docker.io/kindest/kindnetd:v1"]
    assert bbcommon.running_image_problems(imgs, "me.example", "ghcr.io/me", MAP) == []


def test_running_image_from_elsewhere_or_unconverted_fails():
    probs = bbcommon.running_image_problems(
        ["docker.io/library/nginx:1", "registry1.dso.mil/ironbank/app:1.0", "cgr.dev/other.example/app:1"],
        "me.example", "ghcr.io/me", MAP)
    assert len(probs) == 3
    assert any("Iron Bank image the mapping converts" in p for p in probs)


def test_fips_mode_needs_fips_images_except_documented_ones():
    imgs = ["cgr.dev/me.example/app-fips:1", "cgr.dev/me.example/app:1", "cgr.dev/me.example/mgr:5.6",
            "ghcr.io/me/bigbang-base:2"]
    probs = bbcommon.running_image_problems(imgs, "me.example", "ghcr.io/me", MAP, fips=True)
    assert probs == ["cgr.dev/me.example/app:1: not a FIPS image", "ghcr.io/me/bigbang-base:2: not a FIPS image"]
