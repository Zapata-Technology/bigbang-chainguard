"""bb-install.py decisions that don't need a cluster (kubectl/helm calls are stubbed)."""
import argparse
import base64
import getpass
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.request

import pytest
import yaml

from bbinstall import chart, checkout, commands, credentials, gitops, images, preflight, releases, signatures, \
    sitefile, status, tls
from bbinstall import install as installer
from conftest import ROOT, load_script


def test_the_command_line_loads():
    # scripts/bb-install.py is the command line; the installer is the bbinstall package.
    assert callable(load_script("bb-install").main)


def test_checkout_reads_the_overlay_headers():
    co = checkout.Checkout(ROOT)
    assert co.registry.startswith("cgr.dev/") and co.org
    assert co.version.count(".") == 2
    if os.path.exists(co.object_storage):
        assert co.s3["endpoint"].startswith("http")
        assert "tempo" in co.s3["packages"]
        assert co.overlays(fips=True)[-1] == co.object_storage   # object storage layers last
    assert co.overlays(fips=True)[1].endswith("values-chainguard-fips.yaml")
    assert len(co.overlays(fips=False)) == len(co.overlays(fips=True)) - 1


def test_checkout_without_overlays_says_how_to_make_them(tmp_path):
    with pytest.raises(SystemExit, match="bb-run.py"):
        checkout.Checkout(str(tmp_path))


def test_enabled_packages_counts_true_and_auto():
    values = {"networkPolicies": {"enabled": True},
              "packages": {"istiod": {"enabled": True}, "kiali": {"enabled": False},
                           "keycloak": {"enabled": True}, "metricsServer": {"enabled": "auto"},
                         "velero": {"enabled": False}}}
    assert sitefile.enabled_packages(values) == ["packages.istiod", "packages.keycloak", "packages.metricsServer"]


def test_merged_values_layers_files_in_order(tmp_path):
    a, b = tmp_path / "a.yaml", tmp_path / "b.yaml"
    a.write_text("tempo: {enabled: true}\naddons: {velero: {enabled: true}}\n")
    b.write_text("tempo: {enabled: false}\n")
    merged = sitefile.merged_values({"tempo": {"enabled": False}, "domain": "x"}, [str(a), str(b)])
    assert merged["tempo"]["enabled"] is False and merged["addons"]["velero"]["enabled"] is True


def test_object_storage_credentials_only_for_enabled_packages():
    co = checkout.Checkout(ROOT)
    if not co.s3:
        pytest.skip("this checkout uses MinIO")
    vals = installer.object_storage_values(co, ["packages.tempo", "packages.velero", "packages.istiod"], ("AK", "SK"))
    pkgs = vals["packages"]
    assert pkgs["tempo"]["objectStorage"] == {"accessKey": "AK", "accessSecret": "SK"}
    assert "aws_access_key_id=AK" in pkgs["velero"]["values"]["upstream"]["credentials"]["secretContents"]["cloud"]
    assert "mimir" not in pkgs   # Big Bang creates Mimir's Secret whenever keys are set


class _Env:
    def __init__(self, monkeypatch, stored=None, env=None):
        monkeypatch.setattr(credentials, "read_secret", lambda ns, name: dict(stored or {}))
        for k in ("CGR_USERNAME", "CGR_PASSWORD", "S3_ACCESS_KEY", "S3_SECRET_KEY"):
            monkeypatch.delenv(k, raising=False)
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)


def test_credentials_prefer_environment_then_site_then_cluster(monkeypatch):
    stored = {"cgr-username": "stored-u", "cgr-password": "stored-p"}
    site = {"registryCredentials": [{"registry": "cgr.dev", "username": "site-u", "password": "site-p"}]}
    _Env(monkeypatch, stored, {"CGR_USERNAME": "env-u", "CGR_PASSWORD": "env-p"})
    assert credentials.Credentials(site, False).registry("cgr", "cgr.dev", "CGR_USERNAME", "CGR_PASSWORD", "x") \
        == ("env-u", "env-p")
    _Env(monkeypatch, stored)
    assert credentials.Credentials(site, False).registry("cgr", "cgr.dev", "CGR_USERNAME", "CGR_PASSWORD", "x") \
        == ("site-u", "site-p")
    assert credentials.Credentials({}, False).registry("cgr", "cgr.dev", "CGR_USERNAME", "CGR_PASSWORD", "x") \
        == ("stored-u", "stored-p")


def test_missing_required_credentials_name_the_variables(monkeypatch):
    _Env(monkeypatch)
    with pytest.raises(SystemExit, match="CGR_USERNAME and CGR_PASSWORD"):
        credentials.Credentials({}, False).registry("cgr", "cgr.dev", "CGR_USERNAME", "CGR_PASSWORD", "why")
    assert credentials.Credentials({}, False).registry("b", "ghcr.io", "B_U", "B_P", "why", required=False) is None


def test_s3_keys_are_generated_once_and_reused(monkeypatch):
    _Env(monkeypatch)
    c = credentials.Credentials({}, False)
    access, secret = c.s3(generate=True)
    assert access.startswith("bb") and len(secret) >= 32
    _Env(monkeypatch, {"s3-access-key": access, "s3-secret-key": secret})
    assert credentials.Credentials({}, False).s3(generate=True) == (access, secret)


def test_external_s3_needs_keys(monkeypatch):
    _Env(monkeypatch)
    with pytest.raises(SystemExit, match="S3_ACCESS_KEY"):
        credentials.Credentials({}, False).s3(generate=False)


def test_generated_values_are_kept(monkeypatch):
    _Env(monkeypatch, {"keycloak-admin-password": "kept-value-from-last-run"})
    c = credentials.Credentials({}, False)
    assert c.generated("keycloak-admin-password", lambda: "new") == "kept-value-from-last-run"
    _Env(monkeypatch)
    pw = credentials.Credentials({}, False).generated("keycloak-admin-password", lambda: "x" * 32)
    assert len(pw) >= 14   # Keycloak FIPS minimum


def test_site_keycloak_password_is_respected():
    site = {"packages": {"keycloak": {"values": {"upstream": {"secrets": {"env": {"stringData": {
        "KEYCLOAK_ADMIN_PASSWORD": "mine-and-long-enough"}}}}}}}}
    assert installer._site_sets_keycloak_password(site)
    assert not installer._site_sets_keycloak_password({"packages": {"keycloak": {"enabled": True}}})


def test_init_writes_a_site_file_without_credentials(tmp_path, monkeypatch):
    site = tmp_path / "site.yaml"
    args = argparse.Namespace(site=str(site), domain="bigbang.example.com", enable="keycloak,velero",
                              force=False, yes=True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert sitefile.init(args) == 0
    text = site.read_text()
    doc = yaml.safe_load(text)
    assert doc["domain"] == "bigbang.example.com"
    assert doc["packages"]["keycloak"]["enabled"] and doc["packages"]["velero"]["enabled"]
    assert "registryCredentials" not in doc and "password" not in text.lower().split("no credentials")[0]
    with pytest.raises(SystemExit, match="exists"):
        sitefile.init(args)


def test_init_rejects_unknown_packages(tmp_path):
    args = argparse.Namespace(site=str(tmp_path / "s.yaml"), domain="d", enable="not-a-package",
                              force=False, yes=True)
    with pytest.raises(SystemExit, match="unknown packages"):
        sitefile.init(args)


def test_site_files_with_big_bang_3_keys_are_named():
    defaults = {"packages": {"tempo": {}, "keycloak": {}}, "networkPolicies": {}}
    site = {"domain": "d", "tempo": {"enabled": True}, "addons": {"keycloak": {}}, "networkPolicies": {}}
    assert sitefile.legacy_keys(site, defaults) == ["addons.keycloak", "tempo"]
    assert sitefile.legacy_keys({"packages": {"tempo": {}}}, defaults) == []


def test_build_registry_is_read_from_the_overlays():
    co = checkout.Checkout(ROOT)
    text = open(os.path.join(ROOT, "overlays", "values-chainguard.yaml"), encoding="utf-8").read()
    if "/bigbang-" in text:
        assert checkout._build_registry(co) and checkout._build_registry_host(co) != "cgr.dev"


def test_status_says_when_the_cluster_is_behind(monkeypatch):
    co = checkout.Checkout(ROOT)
    import json as _json

    def fake_sh(*cmd, **kw):
        return _json.dumps([{"name": "bigbang", "chart": "bigbang-0.0.1"}])
    monkeypatch.setattr(status, "sh", fake_sh)
    assert "update available" in status.deployed_version_note(co) and co.version in status.deployed_version_note(co)
    monkeypatch.setattr(status, "sh", lambda *c, **k: _json.dumps([{"name": "bigbang", "chart": f"bigbang-{co.version}"}]))
    assert status.deployed_version_note(co) == ""


def test_keycloak_database_password_only_on_a_fresh_keycloak(monkeypatch):
    on = ["packages.keycloak"]
    _Env(monkeypatch)
    monkeypatch.setattr(installer, "_keycloak_installed", lambda: False)
    pw = installer.keycloak_database_password(credentials.Credentials({}, False), {}, on)
    assert pw and len(pw) >= 14                                        # fresh: generated, FIPS-long
    _Env(monkeypatch, {"keycloak-db-password": "kept-from-the-first-install"})
    monkeypatch.setattr(installer, "_keycloak_installed", lambda: True)
    assert installer.keycloak_database_password(credentials.Credentials({}, False), {}, on) == \
        "kept-from-the-first-install"                                  # re-run: the same one
    _Env(monkeypatch)
    assert installer.keycloak_database_password(credentials.Credentials({}, False), {}, on) is None  # running on the default
    assert installer.keycloak_database_password(credentials.Credentials({}, False), {}, []) is None  # Keycloak off
    external = {"packages": {"keycloak": {"database": {"host": "db.example.com"}}}}
    monkeypatch.setattr(installer, "_keycloak_installed", lambda: False)
    assert installer.keycloak_database_password(credentials.Credentials({}, False), external, on) is None


def _chart_archive(version):
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, text in ((f"bigbang-{version}-chart/chart/Chart.yaml", f"name: bigbang\nversion: {version}\n"),
                           (f"bigbang-{version}-chart/chart/values.yaml", "domain: x\n"),
                           (f"bigbang-{version}-chart/../evil", "no")):
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _Response:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_chart_comes_from_repo1_without_a_login_and_is_cached(tmp_path, monkeypatch):
    calls = []

    def urlopen(url, timeout):
        calls.append(url)
        return _Response(_chart_archive("9.8.7"))
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    got = chart.fetch_chart("9.8.7", cache=str(tmp_path))
    assert os.path.exists(os.path.join(got, "values.yaml"))
    assert calls == ["https://repo1.dso.mil/big-bang/bigbang/-/archive/9.8.7/bigbang-9.8.7.tar.gz?path=chart"]
    assert not (tmp_path / "evil").exists()                      # paths that climb out are skipped
    assert chart.fetch_chart("9.8.7", cache=str(tmp_path)) == got and len(calls) == 1


def test_chart_with_the_wrong_version_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout: _Response(_chart_archive("1.0.0")))
    with pytest.raises(SystemExit, match="holds chart version 1.0.0"):
        chart.fetch_chart("9.8.7", cache=str(tmp_path))


def test_chart_ref_passes_oci_and_local_charts_through():
    ns = argparse.Namespace
    assert chart.chart_ref(ns(chart="oci://r/bigbang", version="3.0.0")) == ("oci://r/bigbang", ["--version", "3.0.0"])
    assert chart.chart_ref(ns(chart="work/bigbang/chart", version="3.0.0")) == ("work/bigbang/chart", [])


def test_pull_token_is_made_when_none_is_given(monkeypatch):
    _Env(monkeypatch)
    c = credentials.Credentials({}, False)
    assert c.registry("cgr", "cgr.dev", "CGR_USERNAME", "CGR_PASSWORD", "x", make=lambda: ("id", "tok")) == ("id", "tok")
    assert c.values["cgr-created"]                                # so status can warn before it expires
    _Env(monkeypatch, {"cgr-username": "u", "cgr-password": "p", "cgr-created": "2026-01-01"},
         {"CGR_USERNAME": "env-u", "CGR_PASSWORD": "env-p"})
    c = credentials.Credentials({}, False)
    made = []
    assert c.registry("cgr", "cgr.dev", "CGR_USERNAME", "CGR_PASSWORD", "x",
                      make=lambda: made.append(1)) == ("env-u", "env-p")
    assert not made and "cgr-created" not in c.values             # someone else's token: no expiry warning


def test_new_pull_token_replaces_the_stored_one(monkeypatch):
    _Env(monkeypatch, {"cgr-username": "old", "cgr-password": "old", "cgr-created": "2026-01-01"})
    c = credentials.Credentials({}, False)
    c.forget("cgr")
    assert c.registry("cgr", "cgr.dev", "CGR_USERNAME", "CGR_PASSWORD", "x", make=lambda: ("new", "new")) == ("new", "new")


def test_pull_token_output_is_parsed_not_printed(monkeypatch, capsys):
    class P:
        returncode = 0
        stdout = '{"identity_id":"abc/123","token":"secret-token"}'
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: seen.append(cmd) or P())
    assert credentials.chainctl_pull_token("example.com") == ("abc/123", "secret-token")
    assert "--parent=example.com" in seen[0] and "--ttl=8760h" in seen[0]
    assert "secret-token" not in capsys.readouterr().out


def test_pull_token_maker_asks_unless_told_not_to(monkeypatch):
    monkeypatch.setattr(credentials, "chainctl_logged_in", lambda: True)
    monkeypatch.setattr(credentials, "chainctl_pull_token", lambda org: ("id", "tok"))
    assert credentials.pull_token_maker("o", interactive=False, ask=True)() is None   # can't ask: don't create
    assert credentials.pull_token_maker("o", interactive=False, ask=False)() == ("id", "tok")
    monkeypatch.setattr("builtins.input", lambda q: "n")
    assert credentials.pull_token_maker("o", interactive=True, ask=True)() is None
    monkeypatch.setattr(credentials, "chainctl_logged_in", lambda: False)
    assert credentials.pull_token_maker("o", interactive=False, ask=False)() is None


def test_pull_token_expiry_note():
    day = 86400
    start = time.mktime(time.strptime("2026-01-01", "%Y-%m-%d"))
    state = {"cgr-created": "2026-01-01"}
    assert credentials.pull_token_note(state, today=start + 100 * day) == ""
    assert "expires in 15 days" in credentials.pull_token_note(state, today=start + 350 * day)
    assert "expired" in credentials.pull_token_note(state, today=start + 400 * day)
    assert credentials.pull_token_note({}) == ""


def test_status_notes_a_cluster_that_only_audits_signatures():
    assert "--verify-signatures enforce" in signatures.signature_note({"signatures": "audit"})
    for state in ({"signatures": "enforce"}, {"signatures": "off"}, {}):
        assert signatures.signature_note(state) == ""


def test_access_report_lists_hosts_and_gateways(monkeypatch, capsys):
    import json as _json
    vs = {"items": [{"spec": {"hosts": ["grafana.example.com", "*.example.com"]}}]}
    svc = {"items": [{"metadata": {"name": "public-ingressgateway"}, "spec": {"type": "LoadBalancer"},
                      "status": {"loadBalancer": {"ingress": [{"ip": "203.0.113.5"}]}}}]}
    monkeypatch.setattr(status, "kubectl", lambda *a, **k: _json.dumps(vs if "virtualservice" in a else svc))
    status.access_report({"keycloak-admin-password": "never-shown"})
    out = capsys.readouterr().out
    assert "https://grafana.example.com" in out and "203.0.113.5" in out and "*.example.com" not in out
    assert "never-shown" not in out and "keycloak-admin-password" in out


ISTIO = ["packages.certManager", "packages.istioGateway", "packages.istiod"]


def _tls_args(**kw):
    return argparse.Namespace(**{"tls_cert": None, "tls_key": None, "tls_issuer": None,
                                 "tls_self_signed": False, **kw})


def test_tls_defaults_to_self_signed_and_is_remembered(monkeypatch):
    _Env(monkeypatch)
    c = credentials.Credentials({}, False)
    assert tls.tls_choice(_tls_args(), c, {}, ISTIO) == "self-signed"
    _Env(monkeypatch, {"tls-mode": "issuer", "tls-issuer": "letsencrypt"})
    c = credentials.Credentials({}, False)
    assert tls.tls_choice(_tls_args(), c, {}, ISTIO) == "issuer" and c.values["tls-issuer"] == "letsencrypt"


def test_tls_own_certificate_goes_in_the_state_not_the_issuer(tmp_path, monkeypatch):
    _Env(monkeypatch, {"tls-mode": "issuer", "tls-issuer": "old"})
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("CERT")
    key.write_text("KEY")
    c = credentials.Credentials({}, False)
    assert tls.tls_choice(_tls_args(tls_cert=str(cert), tls_key=str(key)), c, {}, ISTIO) == "own"
    assert c.values["tls-cert"] == "CERT" and c.values["tls-key"] == "KEY" and "tls-issuer" not in c.values
    with pytest.raises(SystemExit, match="go together"):
        tls.tls_choice(_tls_args(tls_cert=str(cert)), c, {}, ISTIO)


def test_tls_is_skipped_without_a_gateway_and_respects_the_site_file(monkeypatch):
    _Env(monkeypatch)
    assert tls.tls_choice(_tls_args(), credentials.Credentials({}, False), {}, ["packages.tempo"]) is None
    site = {"packages": {"istioGateway": {"values": {"gateways": {"public": {
        "gatewayCerts": [{"name": "public-cert"}]}}}}}}
    assert tls.tls_choice(_tls_args(), credentials.Credentials({}, False), site, ISTIO) == "site"


def test_tls_from_cert_manager_needs_cert_manager(monkeypatch):
    _Env(monkeypatch)
    with pytest.raises(SystemExit, match="certManager"):
        tls.tls_choice(_tls_args(), credentials.Credentials({}, False), {}, ["packages.istioGateway",
                                                                             "packages.istiod"])


def test_tls_manifests():
    docs = tls.tls_manifests("example.com", "self-signed")
    cert = docs[-1]
    assert cert["metadata"] == {"name": "public-cert", "namespace": "istio-gateway"}
    assert cert["spec"]["dnsNames"] == ["*.example.com", "example.com"]
    assert cert["spec"]["issuerRef"]["name"] == "bb-install-ca"
    assert [d["kind"] for d in docs] == ["ClusterIssuer", "Certificate", "ClusterIssuer", "Certificate"]
    [only] = tls.tls_manifests("example.com", "issuer", "letsencrypt")
    assert only["spec"]["issuerRef"] == {"name": "letsencrypt", "kind": "ClusterIssuer"}


def test_tls_problem_reports_an_unissued_certificate(monkeypatch):
    import json as _json
    cert = {"status": {"conditions": [{"type": "Ready", "status": "False", "message": "Issuing"}]}}
    monkeypatch.setattr(tls, "kubectl", lambda *a, **k: _json.dumps(cert))
    assert "Issuing" in tls.tls_problem({"tls-mode": "self-signed"})
    assert tls.tls_problem({"tls-mode": "own"}) == ""
    cert["status"]["conditions"][0]["status"] = "True"
    assert tls.tls_problem({"tls-mode": "issuer"}) == ""


def test_gitops_directory_layers_values_like_the_helm_path(tmp_path):
    co = checkout.Checkout(ROOT)
    site = tmp_path / "my-site.yaml"
    site.write_text("domain: example.com\n")
    out = tmp_path / "gitops"
    gitops.write_gitops(str(out), co, True, str(site))
    kust = yaml.safe_load((out / "kustomization.yaml").read_text())
    files = [g["files"][0].split("=", 1)[1] for g in kust["configMapGenerator"]]
    expected = [os.path.basename(f) for f in co.overlays(True)] + ["my-site.yaml"]
    assert [f.split("-", 1)[1] for f in files] == [f"{e}" for e in expected]
    assert all((out / f).exists() for f in files)
    repo, release = yaml.safe_load_all((out / "bigbang.yaml").read_text())
    assert repo["spec"]["url"].startswith("https://repo1.dso.mil/") and repo["spec"]["ref"]["tag"] == co.version
    names = [v["name"] for v in release["spec"]["valuesFrom"]]
    assert names[:-1] == [g["name"] for g in kust["configMapGenerator"]]
    assert release["spec"]["valuesFrom"][-1] == {"kind": "Secret", "name": "bb-install-values"}  # credentials last
    text = "".join(p.read_text() for p in out.rglob("*") if p.is_file())
    assert "registryCredentials" not in text                       # nothing secret in what gets committed


def test_git_mirror_moves_every_repo1_package():
    defaults = {"packages": {
        "istiod": {"git": {"repo": "https://repo1.dso.mil/big-bang/product/packages/istiod.git"}},
        "kiali": {"git": {"repo": "https://other.example.com/kiali.git"}},
        "keycloak": {"git": {"repo": "https://repo1.dso.mil/big-bang/product/packages/keycloak.git"}}}}
    vals = chart.git_mirror_values(defaults, "https://git.example.com/bb/")
    assert vals == {"packages": {
        "istiod": {"git": {"repo": "https://git.example.com/bb/big-bang/product/packages/istiod.git"}},
        "keycloak": {"git": {"repo": "https://git.example.com/bb/big-bang/product/packages/keycloak.git"}}}}
    assert chart.mirrored("https://repo1.dso.mil/big-bang/bigbang.git", None) == "https://repo1.dso.mil/big-bang/bigbang.git"


def test_gitops_uses_the_mirror_for_big_bang():
    repo, _ = gitops.gitops_manifests("3.0.0", ["v0"], "https://git.example.com")
    assert repo["spec"]["url"] == "https://git.example.com/big-bang/bigbang.git"


def test_plan_lists_changes_without_secrets():
    defaults = {"packages": {"tempo": {"enabled": False}, "velero": {"enabled": False}}}
    old = {"packages": {"tempo": {"enabled": True}}, "registryCredentials": [{"password": "s3cret"}],
           "x": {"image": {"repository": "cgr.dev/o/tempo", "tag": "2.8.0"}}}
    new = {"packages": {"velero": {"enabled": True}}, "registryCredentials": [{"password": "other"}],
           "x": {"image": {"repository": "cgr.dev/o/tempo", "tag": "2.8.1"}}}
    lines = status.plan_lines("3.0.0", old, "3.1.0", new, defaults, {"mode": "standard"}, "fips")
    assert lines == ["Big Bang 3.0.0 -> 3.1.0", "images standard -> fips", "turn on packages.velero",
                     "turn off packages.tempo",
                     "image `cgr.dev/o/tempo`: 2.8.0 → 2.8.1"]
    assert not any("s3cret" in ln or "other" in ln for ln in lines)
    assert status.plan_lines("3.0.0", old, "3.0.0", old, defaults, {"mode": "fips"}, "fips") == []
    assert status.plan_lines(None, {}, "3.0.0", {"packages": {"tempo": {"enabled": True}}}, defaults, {},
                             "standard") == ["fresh install of Big Bang 3.0.0 (standard): packages.tempo"]


def test_rollback_refuses_a_gitops_install(monkeypatch):
    monkeypatch.setattr(commands, "kubectl", lambda *a, **k: "helmrelease.helm.toolkit.fluxcd.io/bigbang")
    with pytest.raises(SystemExit, match="git repository"):
        commands.rollback(argparse.Namespace(overlays=ROOT, to=None, yes=True, timeout=1))


def test_sources_that_cant_fetch_are_named_and_retried(monkeypatch):
    import json as _json
    bbcluster = load_script("bbcluster")
    src = {"items": [
        {"kind": "GitRepository", "metadata": {"namespace": "bigbang", "name": "tempo"},
         "spec": {"url": "https://repo1.dso.mil/big-bang/product/packages/tempo.git"},
         "status": {"conditions": [{"type": "Ready", "status": "False", "message": "connection refused"}]}},
        {"kind": "GitRepository", "metadata": {"namespace": "bigbang", "name": "ok"}, "spec": {"url": "https://x/y"},
         "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]}
    calls = []
    monkeypatch.setattr(bbcluster, "kubectl", lambda *a, **k: calls.append(a) or _json.dumps(src))
    [line] = bbcluster.source_problems(nudge=True)
    assert line.startswith("repo1.dso.mil: bigbang/tempo") and "connection refused" in line
    assert any(a[:4] == ("-n", "bigbang", "annotate", "--overwrite") for a in calls)


def _sig_args(**kw):
    return argparse.Namespace(**{"verify_signatures": None, "org_id": None, **kw})


def test_signature_checks_let_kyverno_reach_sigstore_through_big_bangs_network_policies():
    """RKE2's Canal enforces Big Bang's network policies, which let Kyverno's admission
    controller reach only the API server and registry1: every verification timed out."""
    egress = signatures.signature_egress_values()["packages"]["kyverno"]["values"]["bb-common"][
        "networkPolicies"]["egress"]
    rule = egress["definitions"]["signature-verification"]
    assert rule["ports"] == [{"port": 443, "protocol": "TCP"}]
    assert egress["from"]["kyverno-admission-controller"]["to"]["definition"] == {"signature-verification": True}
    # install merges it only when signatures are checked
    src = open(os.path.join(ROOT, "scripts", "bbinstall", "install.py"), encoding="utf-8").read()
    assert 'if signatures != "off":\n            deep_merge(secret_values, signature_egress_values())' in src


def test_signature_policy_is_remembered_and_needs_kyverno(monkeypatch):
    _Env(monkeypatch)
    monkeypatch.delenv("CHAINGUARD_ORG_ID", raising=False)
    assert signatures.signature_choice(_sig_args(), credentials.Credentials({}, False), ["packages.kyverno"]) == ("enforce", None)
    assert signatures.signature_choice(_sig_args(), credentials.Credentials({}, False), ["packages.istiod"])[0] == "off"
    assert signatures.signature_choice(_sig_args(registry_mirror="r.site/bb"), credentials.Credentials({}, False),
                                       ["packages.kyverno"])[0] == "off"            # disconnected: no Sigstore
    c = credentials.Credentials({}, False)
    assert signatures.signature_choice(_sig_args(verify_signatures="enforce", org_id="abc"), c, ["packages.kyverno"]) == \
        ("enforce", "abc")
    _Env(monkeypatch, {"signatures": "audit"})
    assert signatures.signature_choice(_sig_args(), credentials.Credentials({}, False), ["packages.kyverno"])[0] == "audit"
    with pytest.raises(SystemExit, match="Kyverno"):
        signatures.signature_choice(_sig_args(), credentials.Credentials({}, False), ["packages.istiod"])


def test_signature_policy_matches_what_ci_checks():
    bbcommon = load_script("bbcommon")
    pol = signatures.signature_policy("example.com", "0123abc", "enforce")
    [rule] = pol["spec"]["rules"]
    [verify] = rule["verifyImages"]
    assert verify["imageReferences"] == ["cgr.dev/example.com/*"] and verify["failureAction"] == "Enforce"
    keyless = verify["attestors"][0]["entries"][0]["keyless"]
    assert keyless["issuerRegExp"] == bbcommon.SIGNATURE_ISSUERS
    assert keyless["subjectRegExp"] == bbcommon.signature_identity("0123abc")
    assert signatures.signature_policy("example.com", None, "audit")["spec"]["rules"][0]["verifyImages"][0][
        "failureAction"] == "Audit"


def test_registry_mirror_rewrites_images_not_headers(tmp_path):
    co = checkout.Checkout(ROOT)
    m = checkout.mirrored_checkout(co, "registry.site.example/bb/")
    text = open(m.overlays(False)[0], encoding="utf-8").read()
    assert f"cgr.dev/{co.org}/" not in text and "registry.site.example/bb/" in text
    assert m.org == co.org and m.version == co.version                       # header kept
    assert "- registry.site.example/bb\n" in text                             # Kyverno allow list
    build = checkout._build_registry(co)
    if build:
        assert build + "/" not in text
    flux = open(os.path.join(m.root, "flux", "kustomization.yaml"), encoding="utf-8").read()
    assert "newName: registry.site.example/bb/flux-" in flux


def test_install_writes_the_site_file_first(tmp_path, monkeypatch):
    site = tmp_path / "my-site.yaml"
    args = argparse.Namespace(overlays=ROOT, version=None, site=str(site), domain="bb.example.com",
                              enable="keycloak", yes=True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)

    class Stop(Exception):
        pass

    def stop(*a, **kw):
        raise Stop
    monkeypatch.setattr(preflight, "read_secret", stop)    # the first cluster call: the site file is written by then
    with pytest.raises(Stop):
        installer.install(args)
    doc = yaml.safe_load(site.read_text())
    assert doc["domain"] == "bb.example.com" and doc["packages"]["keycloak"]["enabled"]


def test_build_images_are_the_deployed_refs():
    co = checkout.Checkout(ROOT)
    prefix = checkout._build_registry(co)
    if not prefix:
        pytest.skip("these overlays use no images built here")
    std, fips = images.build_images(co, False), images.build_images(co, True)
    assert std and all(r.startswith(prefix + "/bigbang-") and ":" in r.rsplit("/", 1)[1] for r in std)
    assert all("-fips:" in r for r in fips)          # FIPS replaces the standard refs


class _Reply:
    def close(self):
        pass

    def read(self, *a):
        return b'{"token": "t"}'


def _registry(monkeypatch, token_code=200, manifest_code=200):
    """A fake registry: 401 with a Bearer challenge, a token endpoint, then the manifest."""
    seen = []

    def urlopen(req, timeout=None):
        seen.append(req)
        auth = req.headers.get("Authorization", "")
        if "/token?" in req.full_url:
            if token_code != 200:
                raise urllib.error.HTTPError(req.full_url, token_code, "no", {}, None)
            return _Reply()
        if auth.startswith("Bearer ") and manifest_code == 200:
            return _Reply()
        code = manifest_code if auth else 401
        hdrs = {"WWW-Authenticate": 'Bearer realm="https://r.example/token",service="r.example"'}
        raise urllib.error.HTTPError(req.full_url, code, "no", hdrs, None)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


def test_pull_problem_follows_the_token_flow(monkeypatch):
    seen = _registry(monkeypatch)
    assert images.pull_problem("r.example/o/bigbang-base:1", ("u", "p")) is None
    token = [r for r in seen if "/token?" in r.full_url][0]
    assert "scope=repository%3Ao%2Fbigbang-base%3Apull" in token.full_url
    assert token.headers["Authorization"].startswith("Basic ")


def test_pull_problem_says_what_is_wrong(monkeypatch):
    _registry(monkeypatch, token_code=401)
    assert "needs a login" in images.pull_problem("r.example/o/bigbang-base:1")
    assert "refused this login" in images.pull_problem("r.example/o/bigbang-base:1", ("u", "bad"))
    _registry(monkeypatch, manifest_code=404)
    assert "not found" in images.pull_problem("r.example/o/bigbang-base:1", ("u", "p"))


def test_pull_problem_ignores_an_unreachable_registry(monkeypatch):
    def down(*a, **kw):
        raise urllib.error.URLError("no route")
    monkeypatch.setattr(urllib.request, "urlopen", down)
    assert images.pull_problem("r.example/o/bigbang-base:1") is None


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="needs a POSIX bash")
def test_docker_wrapper_runs_this_checkouts_installer_image(tmp_path):
    import subprocess
    shim = tmp_path / "docker"
    shim.write_text('#!/usr/bin/env bash\nprintf "%s\n" "$@"\n', newline="\n")
    shim.chmod(0o755)
    spec = yaml.safe_load(open(os.path.join(ROOT, "builds", "installer", "build.yaml"), encoding="utf-8"))
    env = {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}", "CGR_PASSWORD": "secret-value",
           "BUILD_REGISTRY": "registry.example/o"}
    out = subprocess.run(["bash", os.path.join(ROOT, "scripts", "bb-docker.sh"), "status"], env=env,
                         capture_output=True, text=True, check=True).stdout.split("\n")
    assert f"registry.example/o/bigbang-installer:{spec['image']['standard']['tag']}" in out
    assert out[out.index("registry.example/o/bigbang-installer:" + spec["image"]["standard"]["tag"]) + 1] == "status"
    assert "CGR_PASSWORD" in out and "secret-value" not in "\n".join(out)   # names only, never values
    assert "/site" in out and "KUBECONFIG=/kube/config" in out


def _prompts(monkeypatch, answers):
    """Feed the hidden prompt and the username prompt from one list, in order."""
    it = iter(answers)
    monkeypatch.setattr(getpass, "getpass", lambda q: next(it))
    monkeypatch.setattr("builtins.input", lambda q: next(it))


def test_a_typed_login_is_tested_and_asked_again(monkeypatch, capsys):
    _Env(monkeypatch)
    _prompts(monkeypatch, ["bad", "u", "good", "u"])            # password, then username, per try

    def check(login):
        return None if login == ("u", "good") else "denied"
    c = credentials.Credentials({}, True)
    assert c.registry("cgr", "cgr.dev", "CGR_USERNAME", "CGR_PASSWORD", "why", check=check) == ("u", "good")
    out = capsys.readouterr().out
    assert "can't pull: denied" in out and "2 tries left" in out and "login works" in out
    assert "good" not in out                                      # never printed


def test_a_typed_login_gives_up_after_the_tries(monkeypatch):
    _Env(monkeypatch)
    _prompts(monkeypatch, ["p", "u"] * credentials.TRIES)
    with pytest.raises(SystemExit, match="after 3 tries"):
        credentials.Credentials({}, True).registry("cgr", "cgr.dev", "CGR_USERNAME", "CGR_PASSWORD", "why",
                                                   check=lambda login: "denied")


def test_a_token_alone_gets_the_token_username(monkeypatch):
    _Env(monkeypatch)
    monkeypatch.delenv("B_U", raising=False)
    monkeypatch.setenv("B_T", "tok")
    seen = []
    got = credentials.Credentials({}, False).registry("b", "ghcr.io", "B_U", "B_P", "why", required=False,
                                                      env_token="B_T", check=lambda login: seen.append(login))
    assert got == (credentials.TOKEN_USER, "tok") and seen == [got]
    monkeypatch.delenv("B_T")
    _prompts(monkeypatch, ["tok2", ""])                           # Enter for the username: the token alone
    assert credentials.Credentials({}, True).registry("b", "ghcr.io", "B_U", "B_P", "why", required=False,
                                                      env_token="B_T") == (credentials.TOKEN_USER, "tok2")


def test_a_stored_login_that_fails_stops_or_asks(monkeypatch):
    stored = {"cgr-username": "old", "cgr-password": "expired"}

    def check(login):
        return "denied" if login == ("old", "expired") else None
    _Env(monkeypatch, stored)
    with pytest.raises(SystemExit, match="stored .*can't pull: denied\n.*CGR_USERNAME"):
        credentials.Credentials({}, False).registry("cgr", "cgr.dev", "CGR_USERNAME", "CGR_PASSWORD", "why", check=check)
    _prompts(monkeypatch, ["new", "u"])
    c = credentials.Credentials({}, True)
    assert c.registry("cgr", "cgr.dev", "CGR_USERNAME", "CGR_PASSWORD", "why", check=check) == ("u", "new")
    assert c.values["cgr-password"] == "new"


def test_public_images_need_no_login_and_private_ones_do(monkeypatch):
    _Env(monkeypatch)
    asked = []
    monkeypatch.setattr(getpass, "getpass", lambda q: asked.append(q) or "")
    assert credentials.Credentials({}, True).registry("b", "ghcr.io", "B_U", "B_P", "why", required=False,
                                                      check=lambda login: None) is None
    assert asked == []                                            # anonymous pull works: no prompt
    with pytest.raises(SystemExit, match="without a login can't pull"):
        credentials.Credentials({}, False).registry("b", "ghcr.io", "B_U", "B_P", "why", required=False,
                                                    check=lambda login: None if login else "needs a login")


def test_signature_policy_checks_the_images_built_here():
    bbcommon = load_script("bbcommon")
    subject = bbcommon.build_signature_identity("ghcr.io/o", "O/r")
    pol = signatures.signature_policy("example.com", None, "enforce", ("ghcr.io/o/bigbang-*", subject))
    org, built = pol["spec"]["rules"]
    [verify] = built["verifyImages"]
    assert verify["imageReferences"] == ["ghcr.io/o/bigbang-*"] and verify["failureAction"] == "Enforce"
    keyless = verify["attestors"][0]["entries"][0]["keyless"]
    assert keyless == {"issuerRegExp": bbcommon.BUILD_SIGNATURE_ISSUER, "subjectRegExp": subject,
                       "rekor": {"url": "https://rekor.sigstore.dev"}}
    assert verify["imageRegistryCredentials"] == {"secrets": ["private-registry"]}


def test_build_signer_identity():
    import re
    b = load_script("bbcommon")
    exact = b.build_signature_identity("ghcr.io/zapata-technology", "Zapata-Technology/bigbang-chainguard")
    url = "https://github.com/Zapata-Technology/bigbang-chainguard/.github/workflows/builds.yml@refs/heads/main"
    assert re.match(exact, url)
    assert not re.match(exact, url.replace("bigbang-chainguard", "other"))
    assert not re.match(exact, url.replace("refs/heads/main", "refs/heads/feature"))   # main only
    owner = b.build_signature_identity("ghcr.io/zapata-technology")             # no signer: any repo of the owner
    assert re.match(owner, url) and re.match(owner, url.replace("bigbang-chainguard", "other"))
    assert not re.match(owner, url.replace("Zapata-Technology", "someone-else"))
    assert b.build_signature_identity("registry.example/o") is None


def test_built_signer_is_remembered(monkeypatch):
    _Env(monkeypatch)
    monkeypatch.delenv("BUILD_SIGNER", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    co = checkout.Checkout(ROOT)
    if not checkout._build_registry(co):
        pytest.skip("these overlays use no images built here")
    c = credentials.Credentials({}, False)
    pattern, subject = signatures.built_signer(_sig_args(build_signer="O/r"), c, co)
    assert pattern == checkout._build_registry(co) + "/bigbang-*" and "O/r" in subject
    assert c.values["build-signer"] == "O/r"
    assert "O/r" in signatures.built_signer(_sig_args(build_signer=None), c, co)[1]


def test_upgrade_path_needs_3_35_1_before_big_bang_4():
    assert preflight.upgrade_path_problem(None, "4.0.0") is None                   # fresh install
    assert preflight.upgrade_path_problem("3.35.1", "4.0.0") is None
    assert preflight.upgrade_path_problem("4.0.0", "4.1.0") is None
    assert "3.35.1 only" in preflight.upgrade_path_problem("3.34.0", "4.0.0")
    assert "older major" in preflight.upgrade_path_problem("4.0.0", "3.35.1")


def test_a_big_bang_3_site_file_moves_to_packages():
    defaults = {"packages": {"tempo": {}, "keycloak": {}, "mattermostOperator": {}, "istiod": {}},
                "networkPolicies": {}}
    site = {"domain": "d", "networkPolicies": {"enabled": True},
            "tempo": {"enabled": True, "values": {"istio": {"enabled": True}, "upstream": {"x": 1}}},
            "addons": {"keycloak": {"enabled": True, "values": {"routes": {"a": 1}}},
                       "mattermostoperator": {"enabled": True}},
            "packages": {"keycloak": {"values": {"bb-common": {"routes": {"a": 2}}}}, "myapp": {"git": {}}}}
    new, changes = sitefile.migrate_site(site, defaults)
    assert sitefile.legacy_keys(new, defaults) == []
    assert new["domain"] == "d" and new["networkPolicies"] == {"enabled": True}
    tempo = new["packages"]["tempo"]
    assert tempo["values"] == {"upstream": {"x": 1}, "bb-common": {"istio": {"enabled": True}}}
    # Big Bang 4's own place wins over the moved value; custom packages stay.
    assert new["packages"]["keycloak"]["values"]["bb-common"]["routes"] == {"a": 2}
    assert new["packages"]["mattermostOperator"] == {"enabled": True} and "myapp" in new["packages"]
    assert "tempo -> packages.tempo" in changes and "addons.keycloak -> packages.keycloak" in changes
    assert "tempo.values.istio -> packages.tempo.values.bb-common.istio" in changes
    assert site["tempo"]["values"]["istio"] == {"enabled": True}                  # the input is untouched


def test_render_problems_reports_what_helm_refuses(monkeypatch):
    class P:
        returncode, stderr = 1, "Error: values don't meet the specifications\n- (root): Additional property tempo\n"
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        return P()
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(preflight, "chart_ref", lambda a: ("chart-dir", []))
    [problem] = preflight.render_problems(None, ["a.yaml", "site.yaml"], "1.35.0")
    assert "Additional property tempo" in problem
    assert seen["cmd"][:4] == ["helm", "template", "bigbang", "chart-dir"] and "--kube-version" in seen["cmd"]
    P.returncode = 0
    assert preflight.render_problems(None, ["a.yaml"], "1.35.0") == []


def test_check_pulls_only_the_enabled_packages_images():
    co = checkout.Checkout(ROOT)
    every = images.deployed_images(co, False, co.registry)
    tempo = images.deployed_images(co, False, co.registry, ["packages.tempo"])
    assert tempo and set(tempo) < set(every)
    assert any("/tempo:" in r for r in tempo) and not any("keycloak" in r for r in tempo)


def test_status_judges_only_big_bangs_pods_in_kube_system(monkeypatch):
    """On RKE2, Istio's CNI agent runs in kube-system beside RKE2's own pods; status called
    RKE2's CoreDNS and etcd images "not from the conversion"."""
    import json

    def pod(name, kind, owner):
        return {"metadata": {"name": name, "ownerReferences": [{"kind": kind, "name": owner}]}}
    pods = [pod("istio-cni-node-x", "DaemonSet", "istio-cni-node"),
            pod("rke2-coredns-abc-1", "ReplicaSet", "rke2-coredns-abc"),
            pod("etcd-node", "Node", "node")]
    workloads = {"items": [{"kind": "DaemonSet", "metadata": {"name": "istio-cni-node"}}]}
    monkeypatch.setattr(status, "kubectl", lambda *a, **k: json.dumps(workloads))
    assert [p["metadata"]["name"] for p in status.release_pods("kube-system", pods)] == ["istio-cni-node-x"]
    assert status.release_pods("kiali", pods) == pods
    assert status.owner_workload(pods[1]) == ("Deployment", "rke2-coredns")


def test_inotify_limits_below_neuvectors_needs_are_problems():
    from bbinstall import nodes
    assert nodes.parse_output("8192\n524288\n") == {"max_user_instances": 8192, "max_user_watches": 524288}
    assert nodes.parse_output("cat: can't open") is None
    lines = nodes.inotify_problems({"a": {"max_user_instances": 128, "max_user_watches": 524288},
                                    "b": {"max_user_instances": 8192, "max_user_watches": 1048576}})
    assert len(lines) == 1 and "node a" in lines[0] and "max_user_instances=128" in lines[0]


def test_the_inotify_probe_passes_pod_security_restricted():
    from bbinstall import nodes
    pod = nodes.probe_pod("inotify-0", "node-1", "cgr.dev/org/busybox:1.38.0")
    spec, c = pod["spec"], pod["spec"]["containers"][0]
    assert spec["nodeName"] == "node-1" and spec["securityContext"]["runAsNonRoot"]
    assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert c["securityContext"]["capabilities"]["drop"] == ["ALL"] and not c["securityContext"]["allowPrivilegeEscalation"]
    assert c["resources"]["limits"] and spec["imagePullSecrets"]
    assert "imagePullSecrets" not in nodes.probe_pod("p", "n", "mirror/busybox:1", pull_secret=False)["spec"]


def test_the_inotify_probe_uses_the_overlays_busybox():
    from bbinstall import nodes
    co = checkout.Checkout(ROOT)
    std, fips = nodes.probe_image(co, False), nodes.probe_image(co, True)
    assert "/busybox:" in std and "/busybox-fips:" in fips
    assert std.split(":")[-1] != "latest"


def test_node_inotify_reads_each_node_and_deletes_its_namespace(monkeypatch):
    import json as _json
    from bbinstall import nodes
    calls = []
    logs = {"inotify-0": "128\n8192\n", "inotify-1": "8192\n524288\n"}

    def kubectl(*args, check=True):
        calls.append(args)
        if args[:2] == ("get", "node"):
            return _json.dumps({"items": [{"metadata": {"name": "n0"}}, {"metadata": {"name": "n1"}},
                                          {"metadata": {"name": "n2"}}]})
        if "get" in args and "pod" in args:
            return _json.dumps({"items": [{"metadata": {"annotations": {"bb-install/node": n}},
                                           "status": {"phase": "Succeeded"}} for n in ("n0", "n1")]})
        if "logs" in args:
            return logs.get(args[-1])
        return ""
    monkeypatch.setattr(nodes, "kubectl", kubectl)
    monkeypatch.setattr(nodes, "sh", lambda *a, **k: "")
    monkeypatch.setattr(nodes, "apply_secret", lambda *a: calls.append(("secret",) + a))
    t = iter(range(0, 1000, 100))
    readings, unread = nodes.node_inotify("cgr.dev/org/busybox:1", ("u", "p"), wait=150,
                                          clock=lambda: next(t), sleep=lambda s: None)
    assert readings["n0"]["max_user_instances"] == 128 and readings["n1"]["max_user_watches"] == 524288
    assert unread == ["n2"]                       # never finished: a note, not a problem
    assert any(c[0] == "secret" for c in calls)
    assert calls[-1][:3] == ("delete", "namespace", nodes.PROBE_NAMESPACE)


def velero_cluster(monkeypatch, phases, crd=True, location="Available"):
    """Stub kubectl for backup_before_upgrade: the Backup goes through `phases`."""
    applied = []
    seq = iter(phases)

    def kubectl(*a, **k):
        if a[:2] == ("get", "crd"):
            return "customresourcedefinition/backups.velero.io" if crd else ""
        if a[2:4] == ("get", "backupstoragelocations.velero.io"):
            return json.dumps({"items": [{"status": {"phase": location}}]})
        if a[2:4] == ("get", "backups.velero.io"):
            return json.dumps({"status": {"phase": next(seq)}})
        raise AssertionError(a)
    monkeypatch.setattr(commands, "kubectl", kubectl)
    monkeypatch.setattr(commands, "sh", lambda *a, stdin=None, **k: applied.append(json.loads(stdin)))
    return applied


def test_an_upgrade_backs_up_with_velero_first(monkeypatch):
    applied = velero_cluster(monkeypatch, ["New", "InProgress", "Completed"])
    name = commands.backup_before_upgrade(600, now=lambda: 0, sleep=lambda s: None)
    assert name.startswith("bb-upgrade-")
    assert applied[0]["kind"] == "Backup" and applied[0]["spec"]["includedNamespaces"] == ["*"]


def test_no_velero_upgrades_without_a_backup(monkeypatch):
    applied = velero_cluster(monkeypatch, [], crd=False)
    assert commands.backup_before_upgrade(600) is None and applied == []


# upgrade --to / --list: published releases (bbinstall.releases)

TAGS = ["4.1.0-cg.11", "4.1.0-cg.2", "4.1.0-cg.10", "4.0.0-cg.29", "3.35.1-cg.62", "not-a-release"]


def github_releases(tags=TAGS, extra=()):
    return [{"tag_name": t, "draft": False, "prerelease": False} for t in tags] + list(extra)


def test_releases_list_newest_first_and_page():
    pages = {1: github_releases([f"4.0.0-cg.{n}" for n in range(1, 101)]),
             2: github_releases(extra=[{"tag_name": "4.2.0-cg.1", "draft": True},
                                       {"tag_name": "4.2.0-cg.2", "prerelease": True}])}
    tags = releases.list_releases("o/r", get=lambda url: pages[int(url.rsplit("=", 1)[1])])
    assert tags[:3] == ["4.1.0-cg.11", "4.1.0-cg.10", "4.1.0-cg.2"]      # by number, not text
    assert "4.0.0-cg.100" in tags and "not-a-release" not in tags and "4.2.0-cg.1" not in tags
    assert tags[-1] == "3.35.1-cg.62"


def test_releases_list_failure_says_what_to_do():
    def fail(url):
        raise urllib.error.URLError("no route")
    with pytest.raises(SystemExit, match="--repo"):
        releases.list_releases("o/r", get=fail)


def test_to_resolves_a_tag_a_big_bang_version_or_latest():
    tags = releases.list_releases("o/r", get=lambda url: github_releases())
    assert releases.resolve("latest", tags) == "4.1.0-cg.11"
    assert releases.resolve("4.1.0", tags) == "4.1.0-cg.11"
    assert releases.resolve("4.0.0", tags) == "4.0.0-cg.29"
    assert releases.resolve("4.1.0-cg.2", tags) == "4.1.0-cg.2"
    with pytest.raises(SystemExit, match="no release deploys Big Bang 4.9.0"):
        releases.resolve("4.9.0", tags)
    with pytest.raises(SystemExit, match="isn't a published release"):
        releases.resolve("4.1.0-cg.99", tags)


def test_running_release_is_trusted_only_while_big_bang_matches():
    assert releases.running_release("4.1.0-cg.3", "4.1.0") == "4.1.0-cg.3"
    assert releases.running_release("4.1.0-cg.3", "4.0.0") is None     # rolled back
    assert releases.running_release(None, "4.1.0") is None
    assert releases.running_release("4.1.0-cg.3", None) is None


def test_recorded_release_reads_only_a_release_tag(monkeypatch):
    stored = {}

    def kubectl(*a, **k):
        assert a[-1] == "jsonpath={.data.release}"     # one key; never the credentials
        return stored.get("out")
    monkeypatch.setattr(releases, "kubectl", kubectl)
    assert releases.recorded_release() is None                       # no Secret, or no key
    stored["out"] = base64.b64encode(b"4.1.0-cg.3").decode()
    assert releases.recorded_release() == "4.1.0-cg.3"
    stored["out"] = base64.b64encode(b"hunter2").decode()
    assert releases.recorded_release() is None
    stored["out"] = "%%%"
    assert releases.recorded_release() is None


def test_downgrades_are_found_by_release_or_big_bang_version():
    assert releases.downgrade_problem("4.1.0-cg.11", "4.1.0-cg.3", "4.1.0") is None
    assert "older" in releases.downgrade_problem("4.1.0-cg.2", "4.1.0-cg.3", "4.1.0")
    assert releases.downgrade_problem("4.1.0-cg.2", None, "4.1.0") is None          # revision unknown
    assert "4.0.0" in releases.downgrade_problem("4.0.0-cg.29", None, "4.1.0")
    assert releases.downgrade_problem("3.35.1-cg.1", None, None) is None           # fresh install


def test_newer_releases_than_the_cluster():
    tags = ["4.1.0-cg.11", "4.1.0-cg.10", "4.0.0-cg.29"]
    assert releases.newer_releases(tags, "4.1.0-cg.10", "4.1.0") == ["4.1.0-cg.11"]
    assert releases.newer_releases(tags, None, "4.1.0") == ["4.1.0-cg.11", "4.1.0-cg.10"]
    assert releases.newer_releases(tags, None, None) == tags


def test_handoff_drops_only_upgrades_own_options():
    argv = ["--overlays", "x", "upgrade", "--to", "latest", "--site", "s.yaml", "--allow-downgrade",
            "--repo=o/r", "--fips", "--to=4.1.0", "--yes"]
    assert releases.handoff_args(argv) == ["upgrade", "--site", "s.yaml", "--fips", "--yes"]
    assert releases.unknown_options(["upgrade", "--site", "s", "--no-backup"], "'--site' ...") == ["--no-backup"]


def test_release_repo_from_the_git_remote(tmp_path):
    assert releases.release_repo(ROOT).count("/") == 1
    assert releases.release_repo(str(tmp_path)) == releases.DEFAULT_REPO        # a release archive: no .git


def release_archive(tmp_path, tag, release=None):
    """A release archive like scripts/release.py makes, and a fake download that serves it."""
    src = tmp_path / "src" / f"bigbang-chainguard-{tag}"
    (src / "scripts").mkdir(parents=True, exist_ok=True)
    (src / "scripts" / "bb-install.py").write_text("# --site --fips --yes --plan --no-backup\n")
    (src / "RELEASE").write_text((release or tag) + "\n")
    name = f"bigbang-chainguard-{tag}.tar.gz"
    path = tmp_path / name
    with tarfile.open(path, "w:gz") as t:
        t.add(src, arcname=src.name)
    data = path.read_bytes()
    files = {name: data, name + ".sha256": f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode(),
             name + ".sigstore.json": b"{}"}

    def download(url, dest):
        got = files.get(url.rsplit("/", 1)[1])
        if got is None:
            return False
        with open(dest, "wb") as f:
            f.write(got)
        return True
    return files, download


def test_fetch_release_verifies_with_cosign_then_extracts(tmp_path):
    files, download = release_archive(tmp_path, "4.1.0-cg.3")
    runs = []

    def ok(cmd, **k):
        runs.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "Verified OK", "")
    root, verified, note = releases.fetch_release("o/r", "4.1.0-cg.3", cache=str(tmp_path / "cache"),
                                                  download=download, which=lambda t: "/bin/cosign", run=ok)
    assert verified and "verified" in note
    assert open(os.path.join(root, "RELEASE")).read().strip() == "4.1.0-cg.3"
    cmd = runs[0]
    assert cmd[:2] == ["cosign", "verify-blob"]
    assert cmd[cmd.index("--certificate-identity-regexp") + 1] == \
        "^https://github.com/o/r/.github/workflows/release.yml@"
    with pytest.raises(SystemExit, match="doesn't verify"):
        releases.fetch_release("o/r", "4.1.0-cg.3", cache=str(tmp_path / "cache"), download=download,
                               which=lambda t: "/bin/cosign",
                               run=lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, "", "no matching signatures"))


def test_fetch_release_without_cosign_says_it_is_not_verified(tmp_path):
    files, download = release_archive(tmp_path, "4.1.0-cg.3")
    root, verified, note = releases.fetch_release("o/r", "4.1.0-cg.3", cache=str(tmp_path / "cache"),
                                                  download=download, which=lambda t: None)
    assert not verified and note.startswith("NOT VERIFIED") and "cosign" in note


def test_fetch_release_refuses_a_bad_digest_or_the_wrong_archive(tmp_path):
    files, download = release_archive(tmp_path, "4.1.0-cg.3")
    files["bigbang-chainguard-4.1.0-cg.3.tar.gz.sha256"] = b"0" * 64 + b"  x\n"
    with pytest.raises(SystemExit, match="sha256"):
        releases.fetch_release("o/r", "4.1.0-cg.3", cache=str(tmp_path / "c"), download=download,
                               which=lambda t: None)
    _, download = release_archive(tmp_path / "other", "4.1.0-cg.3", release="4.1.0-cg.1")
    with pytest.raises(SystemExit, match="isn't release"):
        releases.fetch_release("o/r", "4.1.0-cg.3", cache=str(tmp_path / "c"), download=download,
                               which=lambda t: None)


def upgrade_args(**kw):
    a = argparse.Namespace(overlays=ROOT, repo="o/r", to=None, list=False, allow_downgrade=False, yes=True,
                           plan=False, argv=["upgrade", "--yes"])
    vars(a).update(kw)
    return a


def release_cluster(monkeypatch, tmp_path, state, running):
    """Stub GitHub, the cluster and the release's installer for upgrade --to / --list."""
    calls = {"run": [], "recorded": []}
    monkeypatch.setattr(releases, "list_releases", lambda repo: ["4.1.0-cg.11", "4.1.0-cg.10", "4.0.0-cg.29"])
    monkeypatch.setattr(releases, "recorded_release", lambda: state.get("release"))
    monkeypatch.setattr(commands, "deployed", lambda: (running, {}))
    monkeypatch.setattr(commands.shutil, "which", lambda t: "/bin/" + t)
    real_fetch = releases.fetch_release

    def fetch(repo, tag):
        _, download = release_archive(tmp_path / tag, tag)
        return real_fetch(repo, tag, cache=str(tmp_path / "cache"), download=download, which=lambda t: None)
    monkeypatch.setattr(releases, "fetch_release", fetch)

    def run(cmd, **k):
        calls["run"].append(cmd)
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(commands.subprocess, "run", run)
    monkeypatch.setattr(releases, "record_release", calls["recorded"].append)
    return calls


def test_upgrade_to_runs_the_releases_own_installer(monkeypatch, tmp_path, capsys):
    calls = release_cluster(monkeypatch, tmp_path, {"release": "4.1.0-cg.10"}, "4.1.0")
    assert commands.upgrade(upgrade_args(to="latest", argv=["upgrade", "--to", "latest", "--yes"])) == 0
    cmd = calls["run"][0]
    assert cmd[1].replace(os.sep, "/").endswith("4.1.0-cg.11/bigbang-chainguard-4.1.0-cg.11/scripts/bb-install.py")
    assert cmd[2:] == ["upgrade", "--yes"]
    assert calls["recorded"] == ["4.1.0-cg.11"]
    assert "NOT VERIFIED" in capsys.readouterr().out


def test_upgrade_to_refuses_a_downgrade_unless_allowed(monkeypatch, tmp_path):
    calls = release_cluster(monkeypatch, tmp_path, {"release": "4.1.0-cg.11"}, "4.1.0")
    with pytest.raises(SystemExit, match="--allow-downgrade"):
        commands.upgrade(upgrade_args(to="4.1.0-cg.10"))
    with pytest.raises(SystemExit, match="4.0.0-cg.29 is older"):
        commands.upgrade(upgrade_args(to="4.0.0"))
    assert calls["run"] == []
    assert commands.upgrade(upgrade_args(to="4.1.0-cg.10", allow_downgrade=True)) == 0
    assert calls["recorded"] == ["4.1.0-cg.10"]


def test_upgrade_to_with_plan_records_nothing_and_refuses_unknown_flags(monkeypatch, tmp_path):
    calls = release_cluster(monkeypatch, tmp_path, {}, "4.1.0")
    assert commands.upgrade(upgrade_args(to="latest", plan=True, argv=["upgrade", "--plan"])) == 0
    assert calls["recorded"] == []
    with pytest.raises(SystemExit, match="doesn't have --gitops"):
        commands.upgrade(upgrade_args(to="latest", argv=["upgrade", "--gitops", "d"]))
    with pytest.raises(SystemExit, match="pick one"):
        commands.upgrade(upgrade_args(to="latest", overlays=str(tmp_path)))


def test_upgrade_list_shows_newer_releases(monkeypatch, tmp_path, capsys):
    release_cluster(monkeypatch, tmp_path, {"release": "4.1.0-cg.10"}, "4.1.0")
    assert commands.upgrade(upgrade_args(list=True)) == 0
    out = capsys.readouterr().out
    assert "runs release 4.1.0-cg.10" in out and "4.1.0-cg.11  (latest)" in out and "4.0.0-cg.29" not in out


def test_a_failed_or_impossible_backup_stops_the_upgrade(monkeypatch):
    velero_cluster(monkeypatch, ["InProgress", "PartiallyFailed"])
    with pytest.raises(SystemExit, match="PartiallyFailed"):
        commands.backup_before_upgrade(600, now=lambda: 0, sleep=lambda s: None)
    velero_cluster(monkeypatch, [], location="Unavailable")
    with pytest.raises(SystemExit, match="--no-backup"):
        commands.backup_before_upgrade(600)
    clock = [0]
    velero_cluster(monkeypatch, ["InProgress"] * 10)
    with pytest.raises(SystemExit, match="didn't finish"):
        commands.backup_before_upgrade(30, now=lambda: clock[0],
                                       sleep=lambda s: clock.__setitem__(0, clock[0] + s))


def test_seaweedfs_replaces_the_bucket_job_before_applying(monkeypatch):
    """A Job's pod template is immutable: applying a new SeaweedFS digest over the old Job
    failed every upgrade that moved it (compat/installer-upgrade-to.yaml found it)."""
    calls = []
    monkeypatch.setattr(installer, "apply_secret", lambda *a, **k: None)
    monkeypatch.setattr(installer, "sh", lambda *a, **k: calls.append(("sh",) + a))
    monkeypatch.setattr(installer, "kubectl", lambda *a, **k: calls.append(("kubectl",) + a) or "condition met")

    class Co:
        def path(self, p):
            return "/x/" + p
    installer.seaweedfs(Co(), False, {}, ("k", "s"))
    delete = next(i for i, c in enumerate(calls) if "delete" in c and "seaweedfs-buckets" in c)
    apply = next(i for i, c in enumerate(calls) if "apply" in c)
    assert delete < apply and "--ignore-not-found" in calls[delete]


def test_the_release_is_recorded_only_after_the_install_succeeds(monkeypatch):
    """A failed upgrade used to leave the new release recorded while the old one ran
    (compat/installer-upgrade-to.yaml): the tag is now written after the packages are Ready."""
    import inspect
    calls = []
    monkeypatch.setattr(releases, "record_release", lambda tag: calls.append(("record", tag)))
    monkeypatch.setattr(releases, "forget_release", lambda: calls.append(("forget",)))

    class Co:
        release = "4.1.0-cg.9"
    installer.record_release(Co())
    Co.release = None
    installer.record_release(Co())
    assert calls == [("record", "4.1.0-cg.9"), ("forget",)]
    src = inspect.getsource(installer.install)
    assert 'creds.values["release"]' not in src
    assert src.rindex("record_release(co)") > src.index("wait_ready(") > src.index("creds.save(")
