"""bb-install.py decisions that don't need a cluster (kubectl/helm calls are stubbed)."""
import os

import pytest
import yaml

from conftest import ROOT, load_script

install = load_script("bb-install")


def test_checkout_reads_the_overlay_headers():
    co = install.Checkout(ROOT)
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
        install.Checkout(str(tmp_path))


def test_enabled_packages_counts_true_and_auto():
    values = {"istiod": {"enabled": True}, "kiali": {"enabled": False}, "networkPolicies": {"enabled": True},
              "addons": {"keycloak": {"enabled": True}, "metricsServer": {"enabled": "auto"},
                         "velero": {"enabled": False}}}
    assert install.enabled_packages(values) == ["addons.keycloak", "addons.metricsServer", "istiod"]


def test_merged_values_layers_files_in_order(tmp_path):
    a, b = tmp_path / "a.yaml", tmp_path / "b.yaml"
    a.write_text("tempo: {enabled: true}\naddons: {velero: {enabled: true}}\n")
    b.write_text("tempo: {enabled: false}\n")
    merged = install.merged_values({"tempo": {"enabled": False}, "domain": "x"}, [str(a), str(b)])
    assert merged["tempo"]["enabled"] is False and merged["addons"]["velero"]["enabled"] is True


def test_object_storage_credentials_only_for_enabled_packages():
    co = install.Checkout(ROOT)
    if not co.s3:
        pytest.skip("this checkout uses MinIO")
    vals = install.object_storage_values(co, ["tempo", "addons.velero", "istiod"], ("AK", "SK"))
    assert vals["tempo"]["objectStorage"] == {"accessKey": "AK", "accessSecret": "SK"}
    assert "aws_access_key_id=AK" in vals["addons"]["velero"]["values"]["upstream"]["credentials"]["secretContents"]["cloud"]
    assert "mimir" not in vals.get("addons", {})   # Big Bang creates Mimir's Secret whenever keys are set


class _Env:
    def __init__(self, monkeypatch, stored=None, env=None):
        monkeypatch.setattr(install, "read_secret", lambda ns, name: dict(stored or {}))
        for k in ("CGR_USERNAME", "CGR_PASSWORD", "S3_ACCESS_KEY", "S3_SECRET_KEY"):
            monkeypatch.delenv(k, raising=False)
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)


def test_credentials_prefer_environment_then_site_then_cluster(monkeypatch):
    stored = {"cgr-username": "stored-u", "cgr-password": "stored-p"}
    site = {"registryCredentials": [{"registry": "cgr.dev", "username": "site-u", "password": "site-p"}]}
    _Env(monkeypatch, stored, {"CGR_USERNAME": "env-u", "CGR_PASSWORD": "env-p"})
    assert install.Credentials(site, False).registry("cgr", "cgr.dev", "CGR_USERNAME", "CGR_PASSWORD", "x") \
        == ("env-u", "env-p")
    _Env(monkeypatch, stored)
    assert install.Credentials(site, False).registry("cgr", "cgr.dev", "CGR_USERNAME", "CGR_PASSWORD", "x") \
        == ("site-u", "site-p")
    assert install.Credentials({}, False).registry("cgr", "cgr.dev", "CGR_USERNAME", "CGR_PASSWORD", "x") \
        == ("stored-u", "stored-p")


def test_missing_required_credentials_name_the_variables(monkeypatch):
    _Env(monkeypatch)
    with pytest.raises(SystemExit, match="CGR_USERNAME and CGR_PASSWORD"):
        install.Credentials({}, False).registry("cgr", "cgr.dev", "CGR_USERNAME", "CGR_PASSWORD", "why")
    assert install.Credentials({}, False).registry("b", "ghcr.io", "B_U", "B_P", "why", required=False) is None


def test_s3_keys_are_generated_once_and_reused(monkeypatch):
    _Env(monkeypatch)
    c = install.Credentials({}, False)
    access, secret = c.s3(generate=True)
    assert access.startswith("bb") and len(secret) >= 32
    _Env(monkeypatch, {"s3-access-key": access, "s3-secret-key": secret})
    assert install.Credentials({}, False).s3(generate=True) == (access, secret)


def test_external_s3_needs_keys(monkeypatch):
    _Env(monkeypatch)
    with pytest.raises(SystemExit, match="S3_ACCESS_KEY"):
        install.Credentials({}, False).s3(generate=False)


def test_generated_values_are_kept(monkeypatch):
    _Env(monkeypatch, {"keycloak-admin-password": "kept-value-from-last-run"})
    c = install.Credentials({}, False)
    assert c.generated("keycloak-admin-password", lambda: "new") == "kept-value-from-last-run"
    _Env(monkeypatch)
    pw = install.Credentials({}, False).generated("keycloak-admin-password", lambda: "x" * 32)
    assert len(pw) >= 14   # Keycloak FIPS minimum


def test_site_keycloak_password_is_respected():
    site = {"addons": {"keycloak": {"values": {"upstream": {"secrets": {"env": {"stringData": {
        "KEYCLOAK_ADMIN_PASSWORD": "mine-and-long-enough"}}}}}}}}
    assert install._site_sets_keycloak_password(site)
    assert not install._site_sets_keycloak_password({"addons": {"keycloak": {"enabled": True}}})


def test_init_writes_a_site_file_without_credentials(tmp_path, monkeypatch):
    site = tmp_path / "site.yaml"
    args = install.argparse.Namespace(site=str(site), domain="bigbang.example.com", enable="keycloak,velero",
                                      force=False, yes=True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert install.init(args) == 0
    text = site.read_text()
    doc = yaml.safe_load(text)
    assert doc["domain"] == "bigbang.example.com"
    assert doc["addons"]["keycloak"]["enabled"] and doc["addons"]["velero"]["enabled"]
    assert "registryCredentials" not in doc and "password" not in text.lower().split("no credentials")[0]
    with pytest.raises(SystemExit, match="exists"):
        install.init(args)


def test_init_rejects_unknown_packages(tmp_path):
    args = install.argparse.Namespace(site=str(tmp_path / "s.yaml"), domain="d", enable="not-a-package",
                                      force=False, yes=True)
    with pytest.raises(SystemExit, match="unknown packages"):
        install.init(args)


def test_build_registry_is_read_from_the_overlays():
    co = install.Checkout(ROOT)
    text = open(os.path.join(ROOT, "overlays", "values-chainguard.yaml"), encoding="utf-8").read()
    if "/bigbang-" in text:
        assert install._build_registry(co) and install._build_registry_host(co) != "cgr.dev"


def test_status_says_when_the_cluster_is_behind(monkeypatch):
    co = install.Checkout(ROOT)
    import json as _json

    def fake_sh(*cmd, **kw):
        return _json.dumps([{"name": "bigbang", "chart": "bigbang-0.0.1"}])
    monkeypatch.setattr(install, "sh", fake_sh)
    assert "update available" in install.deployed_version_note(co) and co.version in install.deployed_version_note(co)
    monkeypatch.setattr(install, "sh", lambda *c, **k: _json.dumps([{"name": "bigbang", "chart": f"bigbang-{co.version}"}]))
    assert install.deployed_version_note(co) == ""


def test_keycloak_database_password_only_on_a_fresh_keycloak(monkeypatch):
    on = ["addons.keycloak"]
    _Env(monkeypatch)
    monkeypatch.setattr(install, "_keycloak_installed", lambda: False)
    pw = install.keycloak_database_password(install.Credentials({}, False), {}, on)
    assert pw and len(pw) >= 14                                        # fresh: generated, FIPS-long
    _Env(monkeypatch, {"keycloak-db-password": "kept-from-the-first-install"})
    monkeypatch.setattr(install, "_keycloak_installed", lambda: True)
    assert install.keycloak_database_password(install.Credentials({}, False), {}, on) == \
        "kept-from-the-first-install"                                  # re-run: the same one
    _Env(monkeypatch)
    assert install.keycloak_database_password(install.Credentials({}, False), {}, on) is None  # running on the default
    assert install.keycloak_database_password(install.Credentials({}, False), {}, []) is None  # Keycloak off
    external = {"addons": {"keycloak": {"database": {"host": "db.example.com"}}}}
    monkeypatch.setattr(install, "_keycloak_installed", lambda: False)
    assert install.keycloak_database_password(install.Credentials({}, False), external, on) is None


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
    monkeypatch.setattr(install.urllib.request, "urlopen", urlopen)
    chart = install.fetch_chart("9.8.7", cache=str(tmp_path))
    assert os.path.exists(os.path.join(chart, "values.yaml"))
    assert calls == ["https://repo1.dso.mil/big-bang/bigbang/-/archive/9.8.7/bigbang-9.8.7.tar.gz?path=chart"]
    assert not (tmp_path / "evil").exists()                      # paths that climb out are skipped
    assert install.fetch_chart("9.8.7", cache=str(tmp_path)) == chart and len(calls) == 1


def test_chart_with_the_wrong_version_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(install.urllib.request, "urlopen", lambda url, timeout: _Response(_chart_archive("1.0.0")))
    with pytest.raises(SystemExit, match="holds chart version 1.0.0"):
        install.fetch_chart("9.8.7", cache=str(tmp_path))


def test_chart_ref_passes_oci_and_local_charts_through():
    ns = install.argparse.Namespace
    assert install.chart_ref(ns(chart="oci://r/bigbang", version="3.0.0")) == ("oci://r/bigbang", ["--version", "3.0.0"])
    assert install.chart_ref(ns(chart="work/bigbang/chart", version="3.0.0")) == ("work/bigbang/chart", [])


def test_pull_token_is_made_when_none_is_given(monkeypatch):
    _Env(monkeypatch)
    c = install.Credentials({}, False)
    assert c.registry("cgr", "cgr.dev", "CGR_USERNAME", "CGR_PASSWORD", "x", make=lambda: ("id", "tok")) == ("id", "tok")
    assert c.values["cgr-created"]                                # so status can warn before it expires
    _Env(monkeypatch, {"cgr-username": "u", "cgr-password": "p", "cgr-created": "2026-01-01"},
         {"CGR_USERNAME": "env-u", "CGR_PASSWORD": "env-p"})
    c = install.Credentials({}, False)
    made = []
    assert c.registry("cgr", "cgr.dev", "CGR_USERNAME", "CGR_PASSWORD", "x",
                      make=lambda: made.append(1)) == ("env-u", "env-p")
    assert not made and "cgr-created" not in c.values             # someone else's token: no expiry warning


def test_new_pull_token_replaces_the_stored_one(monkeypatch):
    _Env(monkeypatch, {"cgr-username": "old", "cgr-password": "old", "cgr-created": "2026-01-01"})
    c = install.Credentials({}, False)
    c.forget("cgr")
    assert c.registry("cgr", "cgr.dev", "CGR_USERNAME", "CGR_PASSWORD", "x", make=lambda: ("new", "new")) == ("new", "new")


def test_pull_token_output_is_parsed_not_printed(monkeypatch, capsys):
    class P:
        returncode = 0
        stdout = '{"identity_id":"abc/123","token":"secret-token"}'
    seen = []
    monkeypatch.setattr(install.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or P())
    assert install.chainctl_pull_token("example.com") == ("abc/123", "secret-token")
    assert "--parent=example.com" in seen[0] and "--ttl=8760h" in seen[0]
    assert "secret-token" not in capsys.readouterr().out


def test_pull_token_maker_asks_unless_told_not_to(monkeypatch):
    monkeypatch.setattr(install, "chainctl_logged_in", lambda: True)
    monkeypatch.setattr(install, "chainctl_pull_token", lambda org: ("id", "tok"))
    assert install.pull_token_maker("o", interactive=False, ask=True)() is None   # can't ask: don't create
    assert install.pull_token_maker("o", interactive=False, ask=False)() == ("id", "tok")
    monkeypatch.setattr("builtins.input", lambda q: "n")
    assert install.pull_token_maker("o", interactive=True, ask=True)() is None
    monkeypatch.setattr(install, "chainctl_logged_in", lambda: False)
    assert install.pull_token_maker("o", interactive=False, ask=False)() is None


def test_pull_token_expiry_note():
    day = 86400
    start = install.time.mktime(install.time.strptime("2026-01-01", "%Y-%m-%d"))
    state = {"cgr-created": "2026-01-01"}
    assert install.pull_token_note(state, today=start + 100 * day) == ""
    assert "expires in 15 days" in install.pull_token_note(state, today=start + 350 * day)
    assert "expired" in install.pull_token_note(state, today=start + 400 * day)
    assert install.pull_token_note({}) == ""


def test_access_report_lists_hosts_and_gateways(monkeypatch, capsys):
    import json as _json
    vs = {"items": [{"spec": {"hosts": ["grafana.example.com", "*.example.com"]}}]}
    svc = {"items": [{"metadata": {"name": "public-ingressgateway"}, "spec": {"type": "LoadBalancer"},
                      "status": {"loadBalancer": {"ingress": [{"ip": "203.0.113.5"}]}}}]}
    monkeypatch.setattr(install, "kubectl", lambda *a, **k: _json.dumps(vs if "virtualservice" in a else svc))
    install.access_report({"keycloak-admin-password": "never-shown"})
    out = capsys.readouterr().out
    assert "https://grafana.example.com" in out and "203.0.113.5" in out and "*.example.com" not in out
    assert "never-shown" not in out and "keycloak-admin-password" in out


ISTIO = ["certManager", "istioGateway", "istiod"]


def _tls_args(**kw):
    return install.argparse.Namespace(**{"tls_cert": None, "tls_key": None, "tls_issuer": None,
                                         "tls_self_signed": False, **kw})


def test_tls_defaults_to_self_signed_and_is_remembered(monkeypatch):
    _Env(monkeypatch)
    c = install.Credentials({}, False)
    assert install.tls_choice(_tls_args(), c, {}, ISTIO) == "self-signed"
    _Env(monkeypatch, {"tls-mode": "issuer", "tls-issuer": "letsencrypt"})
    c = install.Credentials({}, False)
    assert install.tls_choice(_tls_args(), c, {}, ISTIO) == "issuer" and c.values["tls-issuer"] == "letsencrypt"


def test_tls_own_certificate_goes_in_the_state_not_the_issuer(tmp_path, monkeypatch):
    _Env(monkeypatch, {"tls-mode": "issuer", "tls-issuer": "old"})
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("CERT")
    key.write_text("KEY")
    c = install.Credentials({}, False)
    assert install.tls_choice(_tls_args(tls_cert=str(cert), tls_key=str(key)), c, {}, ISTIO) == "own"
    assert c.values["tls-cert"] == "CERT" and c.values["tls-key"] == "KEY" and "tls-issuer" not in c.values
    with pytest.raises(SystemExit, match="go together"):
        install.tls_choice(_tls_args(tls_cert=str(cert)), c, {}, ISTIO)


def test_tls_is_skipped_without_a_gateway_and_respects_the_site_file(monkeypatch):
    _Env(monkeypatch)
    assert install.tls_choice(_tls_args(), install.Credentials({}, False), {}, ["tempo"]) is None
    site = {"istioGateway": {"values": {"gateways": {"public": {"gatewayCerts": [{"name": "public-cert"}]}}}}}
    assert install.tls_choice(_tls_args(), install.Credentials({}, False), site, ISTIO) == "site"


def test_tls_from_cert_manager_needs_cert_manager(monkeypatch):
    _Env(monkeypatch)
    with pytest.raises(SystemExit, match="certManager"):
        install.tls_choice(_tls_args(), install.Credentials({}, False), {}, ["istioGateway", "istiod"])


def test_tls_manifests():
    docs = install.tls_manifests("example.com", "self-signed")
    cert = docs[-1]
    assert cert["metadata"] == {"name": "public-cert", "namespace": "istio-gateway"}
    assert cert["spec"]["dnsNames"] == ["*.example.com", "example.com"]
    assert cert["spec"]["issuerRef"]["name"] == "bb-install-ca"
    assert [d["kind"] for d in docs] == ["ClusterIssuer", "Certificate", "ClusterIssuer", "Certificate"]
    [only] = install.tls_manifests("example.com", "issuer", "letsencrypt")
    assert only["spec"]["issuerRef"] == {"name": "letsencrypt", "kind": "ClusterIssuer"}


def test_tls_problem_reports_an_unissued_certificate(monkeypatch):
    import json as _json
    status = {"status": {"conditions": [{"type": "Ready", "status": "False", "message": "Issuing"}]}}
    monkeypatch.setattr(install, "kubectl", lambda *a, **k: _json.dumps(status))
    assert "Issuing" in install.tls_problem({"tls-mode": "self-signed"})
    assert install.tls_problem({"tls-mode": "own"}) == ""
    status["status"]["conditions"][0]["status"] = "True"
    assert install.tls_problem({"tls-mode": "issuer"}) == ""


def test_gitops_directory_layers_values_like_the_helm_path(tmp_path):
    co = install.Checkout(ROOT)
    site = tmp_path / "my-site.yaml"
    site.write_text("domain: example.com\n")
    out = tmp_path / "gitops"
    install.write_gitops(str(out), co, True, str(site))
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
    defaults = {"istiod": {"git": {"repo": "https://repo1.dso.mil/big-bang/product/packages/istiod.git"}},
                "kiali": {"git": {"repo": "https://other.example.com/kiali.git"}},
                "addons": {"keycloak": {"git": {"repo": "https://repo1.dso.mil/big-bang/product/packages/keycloak.git"}}}}
    vals = install.git_mirror_values(defaults, "https://git.example.com/bb/")
    assert vals == {"istiod": {"git": {"repo": "https://git.example.com/bb/big-bang/product/packages/istiod.git"}},
                    "addons": {"keycloak": {"git": {"repo":
                                                    "https://git.example.com/bb/big-bang/product/packages/keycloak.git"}}}}
    assert install.mirrored("https://repo1.dso.mil/big-bang/bigbang.git", None) == "https://repo1.dso.mil/big-bang/bigbang.git"


def test_gitops_uses_the_mirror_for_big_bang():
    repo, _ = install.gitops_manifests("3.0.0", ["v0"], "https://git.example.com")
    assert repo["spec"]["url"] == "https://git.example.com/big-bang/bigbang.git"


def test_plan_lists_changes_without_secrets():
    defaults = {"tempo": {"enabled": False}, "addons": {"velero": {"enabled": False}}}
    old = {"tempo": {"enabled": True}, "registryCredentials": [{"password": "s3cret"}],
           "x": {"image": {"repository": "cgr.dev/o/tempo", "tag": "2.8.0"}}}
    new = {"addons": {"velero": {"enabled": True}}, "registryCredentials": [{"password": "other"}],
           "x": {"image": {"repository": "cgr.dev/o/tempo", "tag": "2.8.1"}}}
    lines = install.plan_lines("3.0.0", old, "3.1.0", new, defaults, {"mode": "standard"}, "fips")
    assert lines == ["Big Bang 3.0.0 -> 3.1.0", "images standard -> fips", "turn on addons.velero", "turn off tempo",
                     "image `cgr.dev/o/tempo`: 2.8.0 → 2.8.1"]
    assert not any("s3cret" in ln or "other" in ln for ln in lines)
    assert install.plan_lines("3.0.0", old, "3.0.0", old, defaults, {"mode": "fips"}, "fips") == []
    assert install.plan_lines(None, {}, "3.0.0", {"tempo": {"enabled": True}}, defaults, {}, "standard") == \
        ["fresh install of Big Bang 3.0.0 (standard): tempo"]


def test_rollback_refuses_a_gitops_install(monkeypatch):
    monkeypatch.setattr(install, "kubectl", lambda *a, **k: "helmrelease.helm.toolkit.fluxcd.io/bigbang")
    with pytest.raises(SystemExit, match="git repository"):
        install.rollback(install.argparse.Namespace(overlays=ROOT, to=None, yes=True, timeout=1))


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
    return install.argparse.Namespace(**{"verify_signatures": None, "org_id": None, **kw})


def test_signature_policy_is_remembered_and_needs_kyverno(monkeypatch):
    _Env(monkeypatch)
    monkeypatch.delenv("CHAINGUARD_ORG_ID", raising=False)
    c = install.Credentials({}, False)
    assert install.signature_choice(_sig_args(), c, ["kyverno"]) == ("off", None)
    assert install.signature_choice(_sig_args(verify_signatures="enforce", org_id="abc"), c, ["kyverno"]) == \
        ("enforce", "abc")
    _Env(monkeypatch, {"signatures": "audit"})
    assert install.signature_choice(_sig_args(), install.Credentials({}, False), ["kyverno"])[0] == "audit"
    with pytest.raises(SystemExit, match="Kyverno"):
        install.signature_choice(_sig_args(), install.Credentials({}, False), ["istiod"])


def test_signature_policy_matches_what_ci_checks():
    bbcommon = load_script("bbcommon")
    pol = install.signature_policy("example.com", "0123abc", "enforce")
    [rule] = pol["spec"]["rules"]
    [verify] = rule["verifyImages"]
    assert verify["imageReferences"] == ["cgr.dev/example.com/*"] and verify["failureAction"] == "Enforce"
    keyless = verify["attestors"][0]["entries"][0]["keyless"]
    assert keyless["issuerRegExp"] == bbcommon.SIGNATURE_ISSUERS
    assert keyless["subjectRegExp"] == bbcommon.signature_identity("0123abc")
    assert install.signature_policy("example.com", None, "audit")["spec"]["rules"][0]["verifyImages"][0][
        "failureAction"] == "Audit"
