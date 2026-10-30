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
