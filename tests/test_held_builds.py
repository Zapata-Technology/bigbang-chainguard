"""bb-held-builds.py: a build whose new tag isn't published stays off the nightly PR's overlays."""
import importlib.util
import os

import yaml

from conftest import ROOT

spec = importlib.util.spec_from_file_location("held", os.path.join(ROOT, "scripts", "bb-held-builds.py"))
held = importlib.util.module_from_spec(spec)
spec.loader.exec_module(held)

REG = "ghcr.io/example"


def build(tag, name="bigbang-grafana-plugins"):
    return {"image": {"standard": {"name": name, "tag": tag}, "fips": {"name": f"{name}-fips", "tag": tag}}}


def tree(tmp_path, specs):
    for name, s in specs.items():
        d = tmp_path / "builds" / name
        d.mkdir(parents=True)
        (d / "build.yaml").write_text(yaml.safe_dump(s), encoding="utf-8")
    return str(tmp_path)


def test_only_moved_unpublished_tags_are_held(tmp_path):
    root = tree(tmp_path, {"grafana-plugins": build("13.3.1"), "base": build("2.1.0", "bigbang-base"),
                           "vault": build("v2.2.2", "bigbang-vault")})
    head = {"builds/grafana-plugins/build.yaml": build("13.3.0"),
            "builds/base/build.yaml": build("2.1.0", "bigbang-base"),          # unchanged (re-pin)
            "builds/vault/build.yaml": build("v2.2.1", "bigbang-vault")}
    published = {f"{REG}/bigbang-vault:v2.2.2", f"{REG}/bigbang-vault-fips:v2.2.2"}
    got = held.to_hold(REG, root, head=lambda rel, _root: head.get(rel), exists=published.__contains__)
    # base's tag didn't move (even if a lookup would fail); vault's moved but is out already.
    assert got == ["builds/grafana-plugins/build.yaml"]


def test_a_build_new_to_main_is_held_until_published(tmp_path):
    root = tree(tmp_path, {"newthing": build("1.0.0", "bigbang-newthing")})
    got = held.to_hold(REG, root, head=lambda rel, _root: None, exists=lambda ref: False)
    assert got == ["builds/newthing/build.yaml"]


def test_only_one_variant_missing_still_holds(tmp_path):
    root = tree(tmp_path, {"grafana-plugins": build("13.3.1")})
    out = {f"{REG}/bigbang-grafana-plugins:13.3.1"}   # the FIPS one failed to publish
    got = held.to_hold(REG, root, head=lambda rel, _root: build("13.3.0"), exists=out.__contains__)
    assert got == ["builds/grafana-plugins/build.yaml"]


def test_hold_restores_heads_build_in_a_real_checkout_and_restore_undoes_it(tmp_path):
    import subprocess
    repo, out = tmp_path / "repo", tmp_path / "held"
    root = tree(repo, {"grafana-plugins": build("13.3.0")})
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false"]
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(git + ["add", "."], cwd=root, check=True)
    subprocess.run(git + ["commit", "-qm", "main"], cwd=root, check=True)
    rel = "builds/grafana-plugins/build.yaml"
    (repo / rel).write_text(yaml.safe_dump(build("13.3.1")), encoding="utf-8")   # the builder's rewrite
    tree(repo, {"newthing": build("1.0.0", "bigbang-newthing")})                  # a build new to main

    assert held.hold(str(out), REG, root, exists=lambda ref: False) == [
        rel, "builds/newthing/build.yaml"]
    assert yaml.safe_load((repo / rel).read_text(encoding="utf-8")) == build("13.3.0")
    assert not (repo / "builds" / "newthing" / "build.yaml").exists()

    assert held.restore(str(out), root) == sorted([rel, "builds/newthing/build.yaml"])
    assert yaml.safe_load((repo / rel).read_text(encoding="utf-8")) == build("13.3.1")


def test_restore_puts_held_files_back(tmp_path):
    out, root = tmp_path / "held", tmp_path / "repo"
    (out / "builds" / "grafana-plugins").mkdir(parents=True)
    (out / "builds" / "grafana-plugins" / "build.yaml").write_text("tag: new\n", encoding="utf-8")
    (root / "builds" / "grafana-plugins").mkdir(parents=True)
    (root / "builds" / "grafana-plugins" / "build.yaml").write_text("tag: old\n", encoding="utf-8")
    assert held.restore(str(out), str(root)) == ["builds/grafana-plugins/build.yaml"]
    assert (root / "builds" / "grafana-plugins" / "build.yaml").read_text(encoding="utf-8") == "tag: new\n"


def test_a_standalone_build_is_never_held(tmp_path):
    root = tree(tmp_path, {"installer": {**build("4.1.0-kubectl1.36.6", "bigbang-installer"), "standalone": True}})
    got = held.to_hold(REG, root, head=lambda rel, _root: build("4.0.0-kubectl1.36.6", "bigbang-installer"),
                       exists=lambda ref: False)
    assert got == []
