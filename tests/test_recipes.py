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
jmx = load_script("bb-jmx-exporter")


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
    assert c["go_get"] == [f"{m}@v0.62.0" for m in tester.GO_BUMPS] and "golang.org/x/net@v0.62.0" in c["go_get"]
    assert c["sha256"] == hashlib.sha256(b"tarball").hexdigest()
    spec = {"chainsaw": c, "packages": ["bash"], "user": {"name": "u", "group": "g", "id": 1000},
            "image": {"standard": {"velero": "v", "kyverno": "k"}}}
    args = tester.build_args(spec, "standard")
    assert args["CHAINSAW_SOURCE"].endswith("/v0.2.15") and args["CHAINSAW_VERSION"] == "0.2.15"
    assert args["CHAINSAW_GO_GET"] == " ".join(c["go_get"]) and "CHAINSAW_IMAGE" not in args


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


def test_jmx_exporter_recipe():
    jar, uid = jmx.recipe(*recipe("jmx-exporter"), "1.6.0")
    assert jar == {"url": "https://github.com/prometheus/jmx_exporter/releases/download/v1.6.0/"
                          "jmx_prometheus_javaagent-1.6.0.jar",
                   "sha256": "a95983fd96e865d2bcdf911cc500e7c82808c27ab9fd226bf96732b6c3d8c46e",
                   "dir": "/opt/jmx_exporter", "name": "jmx_prometheus_javaagent-1.6.0.jar"}
    assert uid == 1000   # the last USER of Iron Bank's 1.6.0 Dockerfile


def test_jmx_exporter_recipe_reads_uid_from_arg():
    # Iron Bank's 1.7.0 Dockerfile: `ARG USER_ID=1001` ... `USER ${USER_ID}`.
    manifest = {"resources": [{"filename": "jmx_prometheus_javaagent-1.7.0.jar", "url": "u",
                               "validation": {"type": "sha256", "value": "s"}}]}
    dockerfile = ("ARG USER_ID=1001\nWORKDIR /opt/jmx_exporter\nCOPY jmx_prometheus_javaagent-${VERSION}.jar \\\n"
                  "     LICENSE \\\n     /opt/jmx_exporter/\nUSER root\nUSER ${USER_ID}\n")
    jar, uid = jmx.recipe(manifest, dockerfile, "1.7.0")
    assert (jar["dir"], uid) == ("/opt/jmx_exporter", 1001)


def test_jmx_exporter_recipe_fails_loudly():
    manifest, dockerfile = recipe("jmx-exporter")
    with pytest.raises(SystemExit):   # Big Bang's version is not in this manifest
        jmx.recipe(manifest, dockerfile, "9.9.9")
    with pytest.raises(SystemExit):
        jmx.recipe(manifest, "FROM scratch\nUSER 1000\n", "1.6.0")
    with pytest.raises(SystemExit):
        jmx.recipe(manifest, dockerfile.replace("USER 1000", "USER java"), "1.6.0")


def test_jmx_exporter_finds_the_commit_that_builds_big_bangs_version(monkeypatch):
    manifest, dockerfile = recipe("jmx-exporter")
    newer = {"tags": ["1.7.0", "latest"]}
    monkeypatch.setattr(jmx.bbbuild, "ironbank_recipe", lambda project, ref: ("c3", newer, "FROM x\n"))
    manifests = {"c2": yaml.safe_dump(newer), "c1": yaml.safe_dump(manifest)}

    def fetch(url):
        if "/repository/commits?" in url:
            assert "path=hardening_manifest.yaml" in url and "ref_name=master" in url
            return json.dumps([{"id": "c2"}, {"id": "c1"}]).encode()
        commit, name = url.split("/-/raw/")[1].split("/")
        return (manifests[commit] if name == "hardening_manifest.yaml" else dockerfile).encode()
    monkeypatch.setattr(jmx.bbbuild, "fetch", fetch)
    commit, found, text = jmx.find_recipe("master", "1.6.0")
    assert (commit, found["tags"][0], text) == ("c1", "1.6.0", dockerfile)
    with pytest.raises(SystemExit):
        jmx.find_recipe("master", "1.5.0")


def jar_bytes(manifest):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("META-INF/MANIFEST.MF", manifest)
    return buf.getvalue()


def test_jmx_exporter_jar_is_checked(monkeypatch):
    import urllib.error
    agent = jar_bytes("Manifest-Version: 1.0\nPremain-Class: io.prometheus.jmx.JavaAgent\n")
    ib_url = "https://github.com/prometheus/jmx_exporter/releases/download/v1.6.0/jmx_prometheus_javaagent-1.6.0.jar"
    good = ib_url.replace("/download/v1.6.0/", "/download/1.6.0/")

    def fetch(url, attempts=4):
        if url != good:   # upstream tags its releases without the `v` Iron Bank's manifest has
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return agent
    monkeypatch.setattr(jmx.bbbuild, "fetch", fetch)
    jar = {"url": ib_url, "sha256": hashlib.sha256(agent).hexdigest()}
    assert jmx.check_jar(jar) == good
    with pytest.raises(SystemExit):   # sha256 differs from Iron Bank's
        jmx.check_jar({**jar, "sha256": "0" * 64})
    plain = jar_bytes("Manifest-Version: 1.0\n")
    monkeypatch.setattr(jmx.bbbuild, "fetch", lambda url, attempts=4: plain)
    with pytest.raises(SystemExit):   # not a Java agent
        jmx.check_jar({"url": good, "sha256": hashlib.sha256(plain).hexdigest()})


def test_jmx_exporter_build_args_and_smoke_test(monkeypatch):
    spec = {"jar": {"url": "https://example/j.jar", "sha256": "ab" * 32, "dir": "/opt/jmx_exporter",
                    "name": "jmx_prometheus_javaagent-1.6.0.jar"},
            "download_url": "file:///opt/jmx_exporter/jmx_prometheus_javaagent-1.6.0.jar",
            "packages": ["curl"], "user": {"id": 1000}}
    assert jmx.build_args(spec, "fips") == {
        "PACKAGES": "curl", "JAR_URL": "https://example/j.jar", "JAR_SHA256": "ab" * 32,
        "JAR_DIR": "/opt/jmx_exporter", "JAR_NAME": "jmx_prometheus_javaagent-1.6.0.jar", "USER_ID": "1000"}
    seen = {}
    monkeypatch.setattr(jmx.bbbuild, "run_checks",
                        lambda image, script, checks, **kw: seen.update(script=script, checks=checks, **kw))
    jmx.smoke_test("img", "fips", spec)
    # SonarQube's init container command, run as its chart runs it.
    assert seen["script"].startswith("curl -s 'file:///opt/jmx_exporter/jmx_prometheus_javaagent-1.6.0.jar'  "
                                     "--output /data/jmx_prometheus_javaagent.jar -v")
    assert seen["shell"] == "/bin/sh" and "--read-only" in seen["docker_args"]
    assert seen["docker_args"][seen["docker_args"].index("--user") + 1] == "1000:0"
    assert set(seen["checks"]) == {"jar copied with Iron Bank's sha256", "openssl fips=yes"}
operator = load_script("bb-mattermost-operator")


def test_mattermost_operator_recipe():
    ib = operator.recipe(*recipe("mattermost-operator"))
    assert ib == {"upstream": "v1.25.9", "binary": "/mattermost-operator", "user": "nonroot:nonroot"}
    with pytest.raises(SystemExit):
        operator.recipe({"resources": []}, "FROM scratch\n")


def operator_tarball(gomod, commit="07a736bbb8d6c1e8cf76bdc935d1f08c47307a69"):
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": commit}) as t:
        for name, body in (("mattermost-operator-1.25.9/go.mod", gomod), ("mattermost-operator-1.25.9/sub/go.mod", "x")):
            info = tarfile.TarInfo(name)
            info.size, info.mtime = len(body), 1787140394   # 2026-08-19 11:53:14 UTC
            t.addfile(info, io.BytesIO(body.encode()))
    return buf.getvalue()


def test_mattermost_operator_is_built_from_source(monkeypatch):
    tarball = operator_tarball("module github.com/mattermost/mattermost-operator\n\ngo 1.26.6\n")

    def fetch(url):
        if url.endswith("/@latest"):
            return b'{"Version": "v0.62.0"}'
        assert url == "https://codeload.github.com/mattermost/mattermost-operator/tar.gz/refs/tags/v1.25.9"
        return tarball
    monkeypatch.setattr(operator.bbbuild, "fetch", fetch)
    src = operator.source("v1.25.9")
    assert src["go"] == "1.26.6" and src["go_get"] == ["golang.org/x/net@v0.62.0"]
    assert src["commit"] == "07a736bbb8d6c1e8cf76bdc935d1f08c47307a69" and src["build_time"] == "20260819.115314"
    assert src["sha256"] == hashlib.sha256(tarball).hexdigest()
    spec = {"operator": src, "ironbank": {"binary": "/mattermost-operator"}, "user": "65532:65532",
            "image": {"standard": {"go_package": "go"}, "fips": {"go_package": "go", "fips": "native"}}}
    std, fips = operator.build_args(spec, "standard"), operator.build_args(spec, "fips")
    assert std["FIPS"] == "" and fips["FIPS"] == "native" and fips["GO_PACKAGE"] == "go"
    assert std["OPERATOR_GO_GET"] == "golang.org/x/net@v0.62.0" and std["RUN_USER"] == "65532:65532"
    monkeypatch.setattr(operator.bbbuild, "fetch", lambda url: operator_tarball("go 1.26.6\n", commit=""))
    with pytest.raises(SystemExit):   # no commit id: the version ldflags would be empty
        operator.source("v1.25.9")


def test_mattermost_operator_prefers_the_go_images():
    from conftest import FakeTags
    tags = FakeTags({"go": ["1.26.6"], "go-fips": ["1.26.6"], "kubectl": ["1.36.6"], "kubectl-fips": ["1.36.6"]})
    assert operator.first_pair(tags, operator.BUILD_IMAGES) == {"standard": "go", "fips": "go-fips"}
    # A pair counts only when the org has both: no go-fips means kubectl -dev plus Wolfi's Go.
    tags = FakeTags({"go": ["1.26.6"], "kubectl": ["1.36.6"], "kubectl-fips": ["1.36.6"]})
    assert operator.first_pair(tags, operator.BUILD_IMAGES)["fips"] == "kubectl-fips"
    with pytest.raises(SystemExit):
        operator.first_pair(FakeTags({}), operator.RUNTIME_IMAGES)


def test_mattermost_operator_build_info():
    blob = b"\x00junk\npath\tgithub.com/mattermost/mattermost-operator\ndep\tgolang.org/x/net\tv0.62.0\th1:x=\n" \
           b"build\tCGO_ENABLED=1\nbuild\tGOEXPERIMENT=systemcrypto\n\x00"
    info = operator.build_info(blob)
    assert "dep\tgolang.org/x/net\tv0.62.0\th1:x=" in info

    def fips_build(mode, settings):
        return all(operator.re.search(rx, settings, operator.re.M) for rx in operator.FIPS_BUILDS[mode])
    assert fips_build("toolchain", "\n".join(info))
    assert not fips_build("toolchain", "build\tCGO_ENABLED=1\nbuild\tGOEXPERIMENT=greenteagc")
    native = "build\tCGO_ENABLED=0\nbuild\tGOFIPS140=v1.0.0\nbuild\tDefaultGODEBUG=fips140=on,tracebacklabels=0"
    assert fips_build("native", native)
    assert not fips_build("native", native.replace("fips140=on,", ""))   # module linked but not enabled
