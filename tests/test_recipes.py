"""Parsing Iron Bank's own recipes (fixtures are copies of repo1.dso.mil/dsop/...).

If Iron Bank reshapes a Dockerfile, the builder fails loudly instead of building
something different; these tests pin the shapes the parsers accept.
"""
import hashlib
import json

import pytest
import yaml

from conftest import fixture_text, load_script

base = load_script("bb-base")
grafana = load_script("bb-grafana-plugins")
tester = load_script("bb-devops-tester")


def recipe(name):
    return (yaml.safe_load(fixture_text(f"ironbank/{name}.hardening_manifest.yaml")),
            fixture_text(f"ironbank/{name}.Dockerfile"))


def test_base_recipe():
    pkgs, user, kubectl, yq, cce = base.recipe(*recipe("base"))
    assert pkgs == ["jq", "pinentry", "findutils", "which"]
    assert user == {"name": "base", "id": 1000}
    assert (kubectl, yq) == ("1.36.0", "4.54.1")
    assert len(cce) == 5 and all(c.startswith("CCE-") for c in cce)


def test_base_maps_ubi_packages_to_wolfi():
    pkgs = {base.UBI_TO_WOLFI.get(p, p) for p in ["curl-minimal", "which", "jq"]} - {None}
    assert pkgs == {"curl", "jq"}


def test_grafana_plugin_layout():
    _, dockerfile = recipe("grafana-plugins")
    unzips, dirs = grafana.plugin_layout(dockerfile)
    assert unzips == {"grafana-polystat-panel.zip": "/var/lib/bb-plugins/polystat-panel",
                      "redis-datasource.zip": "/var/lib/bb-plugins/redis-datasource"}
    assert "/var/lib/bb-plugins/piechart-panel" in dirs


def test_grafana_manifest_lists_every_unpacked_zip():
    manifest, dockerfile = recipe("grafana-plugins")
    unzips, _ = grafana.plugin_layout(dockerfile)
    assert {r["filename"] for r in manifest["resources"]} == set(unzips)


def test_devops_tester_recipe():
    versions, chainsaw, user = tester.recipe(*recipe("devops-tester"))
    assert versions == {"velero": "1.18.4", "kyverno": "1.19.1", "kubectl": "1.37.1", "chainsaw": "0.2.15"}
    assert chainsaw.startswith("ghcr.io/kyverno/chainsaw@sha256:")
    assert user == {"name": "devops-user", "group": "devops", "id": 1000}


def test_reshaped_dockerfile_fails_loudly():
    with pytest.raises(SystemExit):
        base.recipe({"resources": []}, "FROM scratch\n")
    with pytest.raises(SystemExit):
        grafana.plugin_layout("FROM scratch\nCOPY a b\n")
    with pytest.raises(SystemExit):
        tester.recipe({"resources": []}, "FROM scratch\n")


def test_chainsaw_is_built_from_source(monkeypatch):
    def fetch(url):
        if url.endswith("/@latest"):
            return b'{"Version": "v0.62.0"}'
        assert url == "https://codeload.github.com/kyverno/chainsaw/tar.gz/refs/tags/v0.2.15"
        return b"tarball"
    monkeypatch.setattr(tester.bbbuild, "fetch", fetch)
    c = tester.chainsaw_source("0.2.15")
    assert c["go_get"] == ["golang.org/x/net@v0.62.0"]
    assert c["sha256"] == hashlib.sha256(b"tarball").hexdigest()
    spec = {"chainsaw": c, "packages": ["bash"], "user": {"name": "u", "group": "g", "id": 1000},
            "image": {"standard": {"velero": "v", "kyverno": "k"}}}
    args = tester.build_args(spec, "standard")
    assert args["CHAINSAW_SOURCE"].endswith("/v0.2.15") and args["CHAINSAW_VERSION"] == "0.2.15"
    assert args["CHAINSAW_GO_GET"] == "golang.org/x/net@v0.62.0" and "CHAINSAW_IMAGE" not in args


def test_redis_datasource_backend_is_rebuilt(monkeypatch):
    def fetch(url):
        if url.endswith("/@latest"):
            return json.dumps({"Version": "v9.9.9"}).encode()
        assert url == "https://codeload.github.com/RedisGrafana/grafana-redis-datasource/tar.gz/refs/tags/v2.2.1"
        return b"source"
    monkeypatch.setattr(grafana.bbbuild, "fetch", fetch)
    info = {"id": "redis-datasource", "backend": True, "executable": "redis-datasource", "info": {"version": "2.2.1"}}
    r = grafana.rebuild(grafana.REBUILD["redis-datasource"], info)
    assert r["sha256"] == hashlib.sha256(b"source").hexdigest() and r["executable"] == "redis-datasource"
    assert "golang.org/x/net@v9.9.9" in r["go_get"]
    spec = {"plugins": [{"id": "a", "url": "u1", "sha256": "s1", "dir": "/d1"},
                        {"id": "redis-datasource", "url": "u2", "sha256": "s2", "dir": "/d2", "rebuild": r}],
            "empty_dirs": []}
    rebuilds = grafana.build_args(spec, "standard")["REBUILDS"].split(";")
    assert len(rebuilds) == 1 and rebuilds[0].startswith(f"/d2 {r['source']} {r['sha256']} redis-datasource patches/")
    with pytest.raises(SystemExit):   # REBUILD lists a frontend-only plugin
        grafana.rebuild(grafana.REBUILD["redis-datasource"], {"id": "x", "info": {"version": "1"}})


def test_rebuild_patches_exist():
    import os
    from conftest import ROOT
    for how in grafana.REBUILD.values():
        assert os.path.exists(os.path.join(ROOT, "builds", "grafana-plugins", how["patch"]))
