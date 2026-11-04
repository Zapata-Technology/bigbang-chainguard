"""The builds this repo makes (builds/<name>/recipe.yaml + scripts/buildhooks/), and parsing
Iron Bank's own recipes (fixtures are copies of repo1.dso.mil/dsop/...).

If Iron Bank reshapes a Dockerfile, the build fails loudly instead of building
something different; these tests pin the shapes the parsers accept.
"""
import glob
import hashlib
import json
import os
import re

import pytest
import yaml

import bbbuild
from conftest import ROOT, fixture_text, load_script


def build(name):
    """(recipe, hook module) of builds/<name>."""
    r = bbbuild.load_recipe(name)
    return r, bbbuild.load_hook(r)


BASE, base = build("base")
GRAFANA, grafana = build("grafana-plugins")
TESTER, tester = build("devops-tester")
JMX, jmx = build("jmx-exporter")


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
    pkgs = base.wolfi_packages(BASE, ["curl-minimal", "which", "jq"], "4.54.1")
    assert "jq" in pkgs and "which" not in pkgs and "curl-minimal" not in pkgs
    assert pkgs[-1] == "yq~4.54" and set(BASE["ubi_userland"]) <= set(pkgs)


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
    monkeypatch.setattr(bbbuild, "fetch", fetch)
    c = tester.chainsaw_source(TESTER["chainsaw"], "0.2.15")
    assert c["go_get"] == [f"{m}@v0.62.0" for m in TESTER["chainsaw"]["go_bumps"]]
    assert "golang.org/x/net@v0.62.0" in c["go_get"]
    assert c["sha256"] == hashlib.sha256(b"tarball").hexdigest()
    spec = {"chainsaw": c, "packages": ["bash"], "user": {"name": "u", "group": "g", "id": 1000},
            "image": {"standard": {"velero": "v", "kyverno": "k"}}}
    args = bbbuild.recipe_build_args(TESTER, spec, "standard")
    assert args["CHAINSAW_SOURCE"].endswith("/v0.2.15") and args["CHAINSAW_VERSION"] == "0.2.15"
    assert args["CHAINSAW_GO_GET"] == " ".join(c["go_get"]) and "CHAINSAW_IMAGE" not in args


def test_redis_datasource_backend_is_rebuilt(monkeypatch):
    def fetch(url):
        if url.endswith("/@latest"):
            return json.dumps({"Version": "v9.9.9"}).encode()
        assert url == "https://codeload.github.com/RedisGrafana/grafana-redis-datasource/tar.gz/refs/tags/v2.2.1"
        return b"source"
    monkeypatch.setattr(bbbuild, "fetch", fetch)
    info = {"id": "redis-datasource", "backend": True, "executable": "redis-datasource", "info": {"version": "2.2.1"}}
    r = grafana.rebuild(GRAFANA["rebuild"]["redis-datasource"], info)
    assert r["sha256"] == hashlib.sha256(b"source").hexdigest() and r["executable"] == "redis-datasource"
    assert "golang.org/x/net@v9.9.9" in r["go_get"]
    spec = {"plugins": [{"id": "a", "url": "u1", "sha256": "s1", "dir": "/d1"},
                        {"id": "redis-datasource", "url": "u2", "sha256": "s2", "dir": "/d2", "rebuild": r}],
            "empty_dirs": []}
    rebuilds = grafana.build_args(spec, "standard")["REBUILDS"].split(";")
    assert len(rebuilds) == 1 and rebuilds[0].startswith(f"/d2 {r['source']} {r['sha256']} redis-datasource patches/")
    with pytest.raises(SystemExit):   # REBUILD lists a frontend-only plugin
        grafana.rebuild(GRAFANA["rebuild"]["redis-datasource"], {"id": "x", "info": {"version": "1"}})


def test_rebuild_patches_exist():
    for how in GRAFANA["rebuild"].values():
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
    monkeypatch.setattr(bbbuild, "ironbank_recipe", lambda project, ref: ("c3", newer, "FROM x\n"))
    manifests = {"c2": yaml.safe_dump(newer), "c1": yaml.safe_dump(manifest)}

    def fetch(url):
        if "/repository/commits?" in url:
            assert "path=hardening_manifest.yaml" in url and "ref_name=master" in url
            return json.dumps([{"id": "c2"}, {"id": "c1"}]).encode()
        commit, name = url.split("/-/raw/")[1].split("/")
        return (manifests[commit] if name == "hardening_manifest.yaml" else dockerfile).encode()
    monkeypatch.setattr(bbbuild, "fetch", fetch)
    project = JMX["ironbank"]["project"]
    commit, found, text = jmx.find_recipe(project, "master", "1.6.0")
    assert (commit, found["tags"][0], text) == ("c1", "1.6.0", dockerfile)
    with pytest.raises(SystemExit):
        jmx.find_recipe(project, "master", "1.5.0")


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
    monkeypatch.setattr(bbbuild, "fetch", fetch)
    jar = {"url": ib_url, "sha256": hashlib.sha256(agent).hexdigest()}
    assert jmx.check_jar(jar) == good
    with pytest.raises(SystemExit):   # sha256 differs from Iron Bank's
        jmx.check_jar({**jar, "sha256": "0" * 64})
    plain = jar_bytes("Manifest-Version: 1.0\n")
    monkeypatch.setattr(bbbuild, "fetch", lambda url, attempts=4: plain)
    with pytest.raises(SystemExit):   # not a Java agent
        jmx.check_jar({"url": good, "sha256": hashlib.sha256(plain).hexdigest()})


def test_jmx_exporter_build_args_and_smoke_test(monkeypatch):
    spec = {"jar": {"url": "https://example/j.jar", "sha256": "ab" * 32, "dir": "/opt/jmx_exporter",
                    "name": "jmx_prometheus_javaagent-1.6.0.jar"},
            "download_url": "file:///opt/jmx_exporter/jmx_prometheus_javaagent-1.6.0.jar",
            "packages": ["curl"], "user": {"id": 1000}}
    assert bbbuild.recipe_build_args(JMX, spec, "fips") == {
        "PACKAGES": "curl", "JAR_URL": "https://example/j.jar", "JAR_SHA256": "ab" * 32,
        "JAR_DIR": "/opt/jmx_exporter", "JAR_NAME": "jmx_prometheus_javaagent-1.6.0.jar", "USER_ID": "1000"}
    seen = {}
    monkeypatch.setattr(bbbuild, "run_checks",
                        lambda image, script, checks, **kw: seen.update(script=script, checks=checks, **kw))
    bbbuild.recipe_smoke_test(JMX, "img", "fips", spec)
    # SonarQube's init container command, run as its chart runs it.
    assert seen["script"].startswith("curl -s 'file:///opt/jmx_exporter/jmx_prometheus_javaagent-1.6.0.jar'  "
                                     "--output /data/jmx_prometheus_javaagent.jar -v")
    assert seen["shell"] == "/bin/sh" and "--read-only" in seen["docker_args"]
    assert seen["docker_args"][seen["docker_args"].index("--user") + 1] == "1000:0"
    assert set(seen["checks"]) == {"jar copied with Iron Bank's sha256", "openssl fips=yes"}
    assert re.search(seen["checks"]["jar copied with Iron Bank's sha256"],
                     "ab" * 32 + "  /data/jmx_prometheus_javaagent.jar", re.M)


OPERATOR, operator = build("mattermost-operator")


def test_mattermost_operator_recipe():
    ib = operator.recipe(*recipe("mattermost-operator"), OPERATOR["upstream"])
    assert ib == {"upstream": "v1.25.9", "binary": "/mattermost-operator", "user": "nonroot:nonroot"}
    with pytest.raises(SystemExit):
        operator.recipe({"resources": []}, "FROM scratch\n", OPERATOR["upstream"])


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
    monkeypatch.setattr(bbbuild, "fetch", fetch)
    src = operator.source(OPERATOR, "v1.25.9")
    assert src["go"] == "1.26.6" and src["go_get"] == ["golang.org/x/net@v0.62.0"]
    assert src["commit"] == "07a736bbb8d6c1e8cf76bdc935d1f08c47307a69" and src["build_time"] == "20260819.115314"
    assert src["sha256"] == hashlib.sha256(tarball).hexdigest()
    spec = {"operator": src, "ironbank": {"binary": "/mattermost-operator"}, "user": "65532:65532",
            "image": {"standard": {"go_package": "go"}, "fips": {"go_package": "go", "fips": "native"}}}
    std, fips = bbbuild.recipe_build_args(OPERATOR, spec, "standard"), bbbuild.recipe_build_args(OPERATOR, spec, "fips")
    assert std["FIPS"] == "" and fips["FIPS"] == "native" and fips["GO_PACKAGE"] == "go"
    assert std["OPERATOR_GO_GET"] == "golang.org/x/net@v0.62.0" and std["RUN_USER"] == "65532:65532"
    monkeypatch.setattr(bbbuild, "fetch", lambda url: operator_tarball("go 1.26.6\n", commit=""))
    with pytest.raises(SystemExit):   # no commit id: the version ldflags would be empty
        operator.source(OPERATOR, "v1.25.9")


def test_mattermost_operator_prefers_the_go_images():
    from conftest import FakeTags
    tags = FakeTags({"go": ["1.26.6"], "go-fips": ["1.26.6"], "kubectl": ["1.36.6"], "kubectl-fips": ["1.36.6"]})
    assert bbbuild.first_pair(tags, bbbuild.GO_BUILD_IMAGES) == {"standard": "go", "fips": "go-fips"}
    # A pair counts only when the org has both: no go-fips means kubectl -dev plus Wolfi's Go.
    tags = FakeTags({"go": ["1.26.6"], "kubectl": ["1.36.6"], "kubectl-fips": ["1.36.6"]})
    assert bbbuild.first_pair(tags, bbbuild.GO_BUILD_IMAGES)["fips"] == "kubectl-fips"
    with pytest.raises(SystemExit):
        bbbuild.first_pair(FakeTags({}), bbbuild.GO_RUNTIME_IMAGES)


def test_mattermost_operator_build_info():
    blob = b"\x00junk\npath\tgithub.com/mattermost/mattermost-operator\ndep\tgolang.org/x/net\tv0.62.0\th1:x=\n" \
           b"build\tCGO_ENABLED=1\nbuild\tGOEXPERIMENT=systemcrypto\n\x00"
    info = bbbuild.build_info(blob)
    assert "dep\tgolang.org/x/net\tv0.62.0\th1:x=" in info

    def fips_build(mode, settings):
        return all(re.search(rx, settings, re.M) for rx in bbbuild.GO_FIPS_BUILDS[mode])
    assert fips_build("toolchain", "\n".join(info))
    assert not fips_build("toolchain", "build\tCGO_ENABLED=1\nbuild\tGOEXPERIMENT=greenteagc")
    native = "build\tCGO_ENABLED=0\nbuild\tGOFIPS140=v1.0.0\nbuild\tDefaultGODEBUG=fips140=on,tracebacklabels=0"
    assert fips_build("native", native)
    assert not fips_build("native", native.replace("fips140=on,", ""))   # module linked but not enabled


TEMPO, tempo_query = build("tempo-query")


def test_tempo_query_recipe_both_shapes():
    # Tempo 2.x (what Big Bang deploys): Iron Bank copies upstream's binary, USER 10001:10001.
    ib = tempo_query.recipe(*recipe("tempo-query-2.10.5"))
    assert ib == {"upstream": "2.10.5", "binary": "/tempo-query", "user": "10001:10001", "uid": "10001:10001"}
    # Tempo 3.x: Iron Bank builds it and runs as a named user its useradd/groupadd create.
    ib = tempo_query.recipe(*recipe("tempo-query"))
    assert ib == {"upstream": "3.1.1", "binary": "/tempo-query", "user": "tempo-query:tempo-query",
                  "uid": "10001:10001"}
    with pytest.raises(SystemExit):
        tempo_query.recipe({"labels": {"org.opencontainers.image.version": "1"}}, "FROM scratch\n")
    with pytest.raises(SystemExit):   # a named user nothing creates
        tempo_query.numeric_user("nobody", "RUN useradd -u 10001 tempo\n")


def test_tempo_query_is_built_from_source(monkeypatch):
    tarball = operator_tarball("module github.com/grafana/tempo\n\ngo 1.26.2\n")
    latest = {"golang.org/x/net": "v0.62.0", "golang.org/x/text": "v0.43.0",
              "google.golang.org/grpc": "v1.85.0", "github.com/apache/thrift": "v0.25.0"}

    def fetch(url):
        if url.endswith("/@latest"):
            return json.dumps({"Version": latest[url.split("/", 3)[3].rsplit("/@", 1)[0]]}).encode()
        assert url == "https://codeload.github.com/grafana/tempo/tar.gz/refs/tags/v2.10.5"
        return tarball
    monkeypatch.setattr(bbbuild, "fetch", fetch)
    src = tempo_query.source(TEMPO, "2.10.5")
    assert src["go"] == "1.26.2" and src["package"] == "./cmd/tempo-query"
    assert src["go_get"] == [f"{m}@{v}" for m, v in latest.items()]
    assert src["sha256"] == hashlib.sha256(tarball).hexdigest()
    spec = {"tempo": src, "ironbank": {"binary": "/tempo-query"}, "user": "10001:10001",
            "image": {"standard": {"go_package": "go"}, "fips": {"go_package": "go", "fips": "native"}}}
    std, fips = bbbuild.recipe_build_args(TEMPO, spec, "standard"), bbbuild.recipe_build_args(TEMPO, spec, "fips")
    assert std["FIPS"] == "" and fips["FIPS"] == "native" and std["RUN_USER"] == "10001:10001"
    assert std["TEMPO_GO_GET"].split() == src["go_get"] and std["TEMPO_PACKAGE"] == "./cmd/tempo-query"


def test_tempo_query_grpc_health_frames():
    # gRPC's length-prefixed messages: an empty HealthCheckRequest, and status SERVING (1).
    assert tempo_query.HEALTH_REQUEST == b"\x00" + (0).to_bytes(4, "big")
    assert tempo_query.HEALTH_SERVING == b"\x00" + (2).to_bytes(4, "big") + bytes([0x08, 0x01])


def test_go_build_problems(monkeypatch):
    blobs = {"/tempo-query": b"\npath\tgithub.com/grafana/tempo/cmd/tempo-query\ndep\tgolang.org/x/net\tv0.62.0\th1:x=\n"
                             b"build\tCGO_ENABLED=0\nbuild\tGOFIPS140=v1.0.0-c2097c7c\n"
                             b"build\tDefaultGODEBUG=fips140=on\n",
             "/etc/ssl/openssl.cnf": b"[algorithm_sect]\ndefault_properties = fips=yes\n"}
    monkeypatch.setattr(bbbuild, "file_from", lambda image, path: blobs[path])
    problems = bbbuild.go_build_problems
    assert problems("img", "/tempo-query", "standard", {}, ["golang.org/x/net@v0.62.0"]) == []
    assert problems("img", "/tempo-query", "fips", {"fips": "native"}, ["golang.org/x/net@v0.62.0"]) == []
    assert problems("img", "/tempo-query", "standard", {}, ["golang.org/x/net@v0.63.0"]) == \
        ["golang.org/x/net is not v0.63.0"]
    assert problems("img", "/tempo-query", "fips", {"fips": "toolchain"}, [])[0].startswith("FIPS Go build (toolchain)")


MINIO, bbminio = build("minio-operator")
SIDECAR, _ = build("minio-operator-sidecar")


def test_minio_recipes():
    # Iron Bank's archived recipes copy each binary out of upstream's image; no USER.
    ib = bbminio.recipe(*recipe("minio-operator"), "operator")
    assert ib == {"upstream": "v7.1.1", "binary": "/minio-operator", "user": "root (none set)"}
    ib = bbminio.recipe(*recipe("minio-operator-sidecar"), "operator-sidecar")
    assert ib == {"upstream": "v7.1.0", "binary": "/minio-operator-sidecar", "user": "root (none set)"}
    with pytest.raises(SystemExit):   # the operator's manifest names no sidecar image
        bbminio.recipe(*recipe("minio-operator"), "operator-sidecar")


def minio_tarball():
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT,
                      pax_headers={"comment": "0037830" + "0" * 33}) as t:
        for name, body in (("operator-7.1.0/go.mod", "module github.com/minio/operator\n\ngo 1.23.0\n"),
                           ("operator-7.1.0/sidecar/go.mod", "module github.com/minio/operator/sidecar\n\ngo 1.24.0\n")):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            t.addfile(info, io.BytesIO(body.encode()))
    return buf.getvalue()


def test_minio_sidecar_is_built_from_its_module(monkeypatch):
    def fetch(url):
        if url.endswith("/@latest"):
            return b'{"Version": "v0.58.0"}' if "crypto" in url else b'{"Version": "v0.62.0"}'
        assert url == "https://codeload.github.com/minio/operator/tar.gz/refs/tags/v7.1.0"
        return minio_tarball()
    monkeypatch.setattr(bbbuild, "fetch", fetch)
    src = bbminio.source(SIDECAR, "v7.1.0")
    assert src["go"] == "1.24.0"   # the sidecar module's go.mod, not the root's
    assert src["module"] == "sidecar" and src["package"] == "./cmd/sidecar"
    assert src["drop_replace"] == ["golang.org/x/crypto"]
    assert src["go_get"] == ["golang.org/x/crypto@v0.58.0", "golang.org/x/net@v0.62.0"]
    assert "-X github.com/minio/operator/sidecar/pkg.Version=v7.1.0" in src["ldflags"]
    assert "sidecar/pkg.ReleaseTag=7.1.0 " in src["ldflags"] and "ShortCommitID=0037830" in src["ldflags"]
    assert bbminio.source(MINIO, "v7.1.0")["go"] == "1.23.0"
    spec = {"minio": src, "ironbank": {"binary": "/minio-operator-sidecar"}, "user": "65532:65532",
            "image": {"standard": {"go_package": "go"}, "fips": {"go_package": "go", "fips": "native"}}}
    args = bbminio.build_args(spec, "fips")
    assert args["MINIO_MODULE"] == "sidecar" and args["MINIO_DROP_REPLACE"] == "golang.org/x/crypto"
    assert args["FIPS"] == "native" and args["BINARY"] == "/minio-operator-sidecar"
    with pytest.raises(SystemExit):   # no such module in the tarball
        bbbuild.go_source("https://codeload.github.com/minio/operator/tar.gz/refs/tags/v7.1.0",
                                  gomod="nope/go.mod")


def test_minio_builders_share_one_dockerfile():
    dockerfile = open(os.path.join(ROOT, "builds", "minio-operator", "Dockerfile"), encoding="utf-8").read()
    # One final stage per program (build.yaml's `target`), each with Iron Bank's entrypoint.
    for r, binary in ((MINIO, "/minio-operator"), (SIDECAR, "/minio-operator-sidecar")):
        assert r["hook"] == "minio" and r["dockerfile"] == "minio-operator"
        assert f"FROM runtime AS {r['program']}\nENTRYPOINT [\"{binary}\"]" in dockerfile


GITALY, gitaly_init = build("gitaly-init-cgroups")


def test_gitaly_init_cgroups_recipe():
    # GitLab's CNG recipe: gitaly/Dockerfile builds the module, gitaly-init-cgroups/Dockerfile copies it.
    init_df, gitaly_df = fixture_text("cng/gitaly-init-cgroups.Dockerfile"), fixture_text("cng/gitaly.Dockerfile")
    assert gitaly_init.recipe(init_df, gitaly_df) == {"binary": "/setup_cgroups", "module": "tools/gitaly-init-cgroups"}
    with pytest.raises(SystemExit):   # copied somewhere the command doesn't run
        gitaly_init.recipe(init_df.replace('CMD ["/setup_cgroups"]', 'CMD ["/bin/setup"]'), gitaly_df)
    with pytest.raises(SystemExit):   # CNG stops building it in the gitaly image
        gitaly_init.recipe(init_df, gitaly_df.replace("go build -C", "make -C"))


def test_gitaly_init_cgroups_tag():
    assert gitaly_init.gitaly_tag("v19.4.2-ubi") == "v19.4.2"
    assert gitaly_init.gitaly_tag("v19.4.2") == "v19.4.2"
    with pytest.raises(SystemExit):
        gitaly_init.gitaly_tag("19.4")


def test_gitaly_init_cgroups_source(monkeypatch):
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": "5" * 40}) as t:
        body = b"module gitlab.com/gitlab-org/gitaly/tools/gitaly-init-cgroups\n\ngo 1.25.0\n"
        info = tarfile.TarInfo("gitaly-v19.4.2-tools-gitaly-init-cgroups/tools/gitaly-init-cgroups/go.mod")
        info.size = len(body)
        t.addfile(info, io.BytesIO(body))

    def fetch(url):
        if url.endswith("/LICENSE"):
            assert url == "https://gitlab.com/gitlab-org/gitaly/-/raw/v19.4.2/LICENSE"
            return b"The MIT License (MIT)\n"
        assert url == ("https://gitlab.com/gitlab-org/gitaly/-/archive/v19.4.2/gitaly-v19.4.2.tar.gz"
                       "?path=tools/gitaly-init-cgroups")
        return buf.getvalue()
    monkeypatch.setattr(bbbuild, "fetch", fetch)
    src = gitaly_init.source(GITALY, "v19.4.2", "tools/gitaly-init-cgroups")
    assert src["go"] == "1.25.0" and src["commit"] == "5" * 40 and src["go_get"] == []
    assert src["license_sha256"] == hashlib.sha256(b"The MIT License (MIT)\n").hexdigest()
    spec = {"gitaly": src, "cng": {"binary": "/setup_cgroups"}, "user": "0:0",
            "image": {"standard": {"go_package": "go"}, "fips": {"go_package": "go", "fips": "native"}}}
    args = bbbuild.recipe_build_args(GITALY, spec, "standard")
    assert args["GITALY_MODULE"] == "tools/gitaly-init-cgroups" and args["RUN_USER"] == "0:0"
    assert args["BINARY"] == "/setup_cgroups" and args["GITALY_LICENSE"].endswith("/v19.4.2/LICENSE")


# --- every build has a recipe; the recipes, bb-build.py and the workflows agree ---

BUILD_DIRS = sorted(os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(ROOT, "builds", "*", "")))
RECIPES = bbbuild.recipe_names()
bb_build = load_script("bb-build")


def test_every_build_has_a_recipe_and_every_recipe_a_build():
    assert BUILD_DIRS, "no builds/ directories"
    assert sorted(RECIPES) == BUILD_DIRS, "each builds/<name>/ needs a recipe.yaml (and nothing else holds one)"
    for name in RECIPES:
        r, hook = build(name)
        # Its Dockerfile is its own or another build's (`dockerfile`: the MinIO sidecar).
        assert os.path.exists(os.path.join(ROOT, "builds", r.get("dockerfile", name), "Dockerfile")), name
        assert callable(getattr(hook, "generate", None)), f"{name}: hook {r['hook']} has no generate()"
        assert hasattr(hook, "smoke_test") or r.get("smoke"), f"{name}: no smoke test"
        assert hasattr(hook, "build_args") or r.get("build_args") is not None or name == "installer", name


def test_recipe_order_puts_the_installer_after_base():
    assert RECIPES.index("base") < RECIPES.index("installer")


def test_mapping_builds_have_recipes():
    mapping = yaml.safe_load(open(os.path.join(ROOT, "mapping.yaml"), encoding="utf-8"))
    used = {m["build"] for m in mapping["images"].values() if isinstance(m, dict) and m.get("build")}
    assert used and used <= set(RECIPES)


@pytest.mark.parametrize("name", [n for n in RECIPES])
def test_recipe_templates_fill_from_the_committed_build_yaml(name):
    """Every {{path}} in a recipe's header, build_args and smoke resolves (a typo fails here,
    not in CI's docker build)."""
    r = bbbuild.load_recipe(name)
    assert bbbuild.header(r).startswith("# ") and "{{" not in bbbuild.header(r)
    path = os.path.join(ROOT, "builds", name, "build.yaml")
    if not os.path.exists(path):
        return
    spec = yaml.safe_load(open(path, encoding="utf-8"))
    for variant in spec["image"]:
        args = bbbuild.recipe_build_args(r, spec, variant)
        assert all("{{" not in v for v in args.values())
        for line in (r.get("smoke") or {}).get("script", []):
            assert "{{" not in bbbuild.fill(line, spec, variant)
        for label, rx in (r.get("smoke") or {}).get("checks", {}).items():
            re.compile(bbbuild.fill(rx, spec, variant))
            bbbuild.fill(label, spec, variant)


def test_fill():
    data = {"a": {"fips": {"v": "1.36.6"}}, "l": ["x", "y"], "n": 3}
    assert bbbuild.fill("{{a.$variant.v|line}} {{ l }} {{n}}", data, "fips") == "1.36 x y 3"
    assert bbbuild.fill("{{a.$variant.v|line|rx}}", data, "fips") == r"1\.36"
    assert bbbuild.fill("[{{a.standard.v?}}]", data) == "[]"
    with pytest.raises(SystemExit):
        bbbuild.fill("{{a.standard.v}}", data)
    assert bbbuild.fill("${a[1]} { x; }", data) == "${a[1]} { x; }"   # shell braces are not templates


def test_recipe_smoke_adds_the_fips_check(monkeypatch):
    seen = []
    monkeypatch.setattr(bbbuild, "run_checks", lambda image, script, checks, **kw: seen.append((script, checks, kw)))
    spec = yaml.safe_load(open(os.path.join(ROOT, "builds", "base", "build.yaml"), encoding="utf-8"))
    bbbuild.recipe_smoke_test(BASE, "img", "standard", spec)
    bbbuild.recipe_smoke_test(BASE, "img", "fips", spec)
    (std, std_checks, kw), (_, fips_checks, _) = seen
    assert std.endswith(bbbuild.FIPS_OPENSSL) and kw == {"shell": "bash", "docker_args": []}
    assert "openssl fips=yes" in fips_checks and "openssl fips=yes" not in std_checks
    kubectl = ".".join(spec["ironbank"]["kubectl"].split(".")[:2])
    assert std_checks[f"kubectl {kubectl}"] == '"v' + re.escape(kubectl) + r"\."


def test_bb_build_command_line(capsys):
    with pytest.raises(SystemExit):
        bb_build.parse_args([])                 # a name or --all
    with pytest.raises(SystemExit):
        bb_build.parse_args(["base", "--all"])
    a = bb_build.parse_args(["base", "--no-generate", "--build", "--test", "--push"])
    assert (a.name, a.no_generate, a.build, a.test, a.push) == ("base", True, True, True, True)
    assert bb_build.main(["--list"]) == 0
    assert capsys.readouterr().out.split() == RECIPES
    with pytest.raises(SystemExit):
        bb_build.main(["no-such-build"])


def workflow(name):
    return open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8").read()


def test_builds_workflow_matrix_lists_every_recipe():
    matrix = yaml.safe_load(workflow("builds.yml"))["jobs"]["build"]["strategy"]["matrix"]["name"]
    assert sorted(matrix) == sorted(RECIPES)


def test_update_workflow_regenerates_every_recipe_in_parallel():
    loops = re.findall(r"^\s*for b in ([\w -]+); do$", workflow("update.yml"), re.M)
    assert len(loops) == 2 and loops[0] == loops[1], "the run loop and the print loop must match"
    # The installer reads base's build.yaml, so it can't run in the parallel loop.
    assert sorted(loops[0].split()) == sorted(set(RECIPES) - {"installer"})
    assert 'python3 scripts/bb-build.py "$b"' in workflow("update.yml")
