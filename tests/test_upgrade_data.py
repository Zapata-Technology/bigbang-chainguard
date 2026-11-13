"""compat/support/upgrade-data.py: the decisions of the release-upgrade data checks that need no cluster."""
import importlib.util
import json
import os

import pytest

from conftest import ROOT

spec = importlib.util.spec_from_file_location("upgrade_data", os.path.join(ROOT, "compat", "support", "upgrade-data.py"))
ud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ud)

PG, KC = "app.kubernetes.io/name=postgresql", "app.kubernetes.io/name=keycloak"


def test_only_pods_the_upgrade_left_in_place_count_as_unchanged():
    before = {PG: {"u1": ["pg", ["postgres:18@a"]]}, KC: {"u2": ["kc", ["keycloak:26@a"]]}}
    now = {PG: {"u1": ["pg", ["postgres:18@a"]]}, KC: {"u3": ["kc", ["keycloak:26@b"]]}}
    assert ud.unchanged(before, now) == [PG]
    # No pods at seed time is nothing to compare, not "unchanged".
    assert ud.unchanged({PG: {}}, {PG: {}}) == []


def test_verify_restarts_what_the_upgrade_did_not_replace_database_first(monkeypatch, capsys):
    before = {PG: {"u1": ["pg-0", ["postgres:18@a"]]}, KC: {"u2": ["kc-0", ["keycloak:26@a"]]}}
    live = {PG: {"u1": ("pg-0", ["postgres:18@a"])}, KC: {"u2": ("kc-0", ["keycloak:26@a"])}}
    deleted = []

    def run(*cmd, **kw):
        if cmd[3] == "delete":
            sel = cmd[6]
            deleted.append(sel)
            live[sel] = {"new-" + sel: (sel, ["new"])}
    monkeypatch.setattr(ud, "run", run)
    monkeypatch.setattr(ud, "pods", lambda ns, sel: live[sel])
    monkeypatch.setattr(ud, "ready", lambda ns, sel: True)
    ud.compare("keycloak", before)
    assert deleted == [PG, KC]
    out = capsys.readouterr().out
    assert "PostgreSQL: pods not replaced by the upgrade" in out and "restarting" in out


def test_verify_leaves_pods_the_upgrade_replaced(monkeypatch, capsys):
    before = {"app=mattermost": {"u1": ["mm-a", ["cgr.dev/o/mattermost:11.1@a"]]}}
    monkeypatch.setattr(ud, "pods", lambda ns, sel: {"u9": ("mm-b", ["cgr.dev/o/mattermost:11.2@b"])})
    monkeypatch.setattr(ud, "run", lambda *a, **k: pytest.fail("nothing to restart"))
    ud.compare("mattermost", before)
    assert "replaced by the upgrade; images cgr.dev/o/mattermost:11.1@a -> cgr.dev/o/mattermost:11.2@b" in (
        capsys.readouterr().out)


def test_keycloak_data_is_checked_by_id_user_and_role():
    realm = {"id": "r1", "realm": "compat-upgrade"}
    users = [{"username": "upgrade-user", "enabled": True}]
    roles = [{"name": "default-roles-compat-upgrade"}, {"name": "upgrade-role"}]
    assert ud.check_keycloak(realm, users, roles, "r1") == []
    assert ud.check_keycloak(realm, users, roles, None) == []        # seed: no id yet
    problems = ud.check_keycloak({"id": "r2"}, [{"username": "upgrade-user", "enabled": False}],
                                 roles[:1], "r1")
    assert [p.split(":")[0] for p in problems] == [
        "realm compat-upgrade has id r2, not r1", "no enabled user upgrade-user in realm compat-upgrade",
        "user upgrade-user lost role upgrade-role"]


def test_the_in_pod_script_needs_only_sh_and_kcadm():
    # Chainguard's keycloak image may have no grep or tr; the admin password stays in the
    # pod's environment; the user's (test-only) password is an argument.
    for tool in ("grep", "tr ", "sed", "awk"):
        assert tool not in ud.KCADM
    assert "$KEYCLOAK_ADMIN_PASSWORD" in ud.KCADM and len(ud.PASSWORD) >= 14   # FIPS


def test_multipart_upload_body():
    body, ctype = ud.multipart("files", "f.txt", b"hello\n", boundary="B")
    assert ctype == "multipart/form-data; boundary=B"
    assert body == (b'--B\r\nContent-Disposition: form-data; name="files"; filename="f.txt"\r\n'
                    b"Content-Type: text/plain\r\n\r\nhello\n\r\n--B--\r\n")


def test_verify_without_a_seed_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPAT_OUT", str(tmp_path))
    monkeypatch.setattr(ud.sys, "argv", ["upgrade-data.py", "keycloak", "verify"])
    with pytest.raises(SystemExit, match="didn't seed keycloak"):
        ud.main()


def fake_kcadm(realm_id, calls):
    def kcadm(pod, *args, what):
        calls.append(args)
        if args[1:3] == ("get", "users"):
            return '[{"username": "upgrade-user", "enabled": true}]'
        if args[1:2] == ("get",):
            return '{"id": "%s", "realm": "compat-upgrade"}' % realm_id
        if args[1:2] == ("get-roles",):
            return '[{"name": "upgrade-role"}]'
        return ""
    return kcadm


def test_seed_keeps_the_pods_and_the_ids_for_verify(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPAT_OUT", str(tmp_path))
    monkeypatch.setattr(ud.sys, "argv", ["upgrade-data.py", "keycloak", "seed"])
    monkeypatch.setattr(ud, "pods", lambda ns, sel: {"u-" + sel: ("p", ["img"])})
    calls = []
    monkeypatch.setattr(ud, "kcadm", fake_kcadm("r1", calls))
    ud.main()
    state = json.load(open(tmp_path / "upgrade-keycloak.json"))
    assert state["realm_id"] == "r1" and set(state["pods"]) == {PG, KC}
    assert [c[1] for c in calls[:5]] == ["create", "create", "create", "set-password", "add-roles"]
    assert calls[-1] == ("compat-upgrade", "upgrade-user", ud.PASSWORD)   # the user's own login
    # A realm with another id after the upgrade was recreated, not kept.
    monkeypatch.setattr(ud, "kcadm", fake_kcadm("r2", []))
    with pytest.raises(SystemExit, match="recreated"):
        ud.keycloak("verify", state)


def test_migrated_compares_tags_not_digests():
    assert ud.tags(["cgr.dev/o/keycloak:26.7.4@sha256:" + "a" * 64, "cgr.dev/o/keycloak:26.7.4"]) == ["26.7.4"]
    assert ud.tags(["cgr.dev/o/keycloak:26.8.2@sha256:" + "b" * 64]) != ud.tags(["cgr.dev/o/keycloak:26.7.4"])
