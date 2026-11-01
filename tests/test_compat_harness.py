"""bb-compat.py's decisions that need no cluster: the Iron Bank skip, its credentials, and which images count as run."""
import sys

import yaml

from conftest import load_script

compat = load_script("bb-compat")
BOTH = {"REGISTRY1_USERNAME": "u", "REGISTRY1_PASSWORD": "p"}


def test_registry1_skip_only_for_tests_that_need_it():
    assert compat.registry1_skip({}, {}) is None
    assert compat.registry1_skip({"registry1": True}, BOTH) is None
    why = compat.registry1_skip({"registry1": True}, {})
    assert "REGISTRY1_USERNAME and REGISTRY1_PASSWORD not set" in why
    assert compat.registry1_skip({"registry1": True}, {"REGISTRY1_USERNAME": "u"}).endswith("REGISTRY1_PASSWORD not set")


def test_registry1_test_without_credentials_is_skipped_before_anything_runs(tmp_path, monkeypatch, capsys):
    f = tmp_path / "needs-ironbank.yaml"
    f.write_text(yaml.safe_dump({"images": {"ironbank/x": {"build": "x"}},
                                 "test": {"registry1": True, "enable": ["addons.x"]}}))
    for k in compat.REGISTRY1_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    ran = []
    # Nothing may be built, converted or deployed: any subprocess call fails the test.
    monkeypatch.setattr(compat.subprocess, "run", lambda *a, **k: ran.append(a) or (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(sys, "argv", ["bb-compat.py", str(f), "--fips"])
    assert compat.main() == 0
    out = capsys.readouterr().out
    assert "RESULT needs-ironbank.yaml (FIPS): SKIPPED (needs Iron Bank pulls" in out
    assert "::notice title=SKIPPED needs-ironbank.yaml (FIPS)::" in out
    assert not ran


def test_registry1_credentials_reach_the_cluster_pull_secret(monkeypatch):
    monkeypatch.setenv("CGR_TOKEN", "t")
    monkeypatch.delenv("GHCR_TOKEN", raising=False)
    for k, v in BOTH.items():
        monkeypatch.setenv(k, v)
    values, cfg = compat.credentials()
    assert [r["registry"] for r in values["registryCredentials"]] == ["cgr.dev", "registry1.dso.mil"]
    assert set(cfg["auths"]) == {"cgr.dev", "registry1.dso.mil"}
    monkeypatch.delenv("REGISTRY1_PASSWORD")
    values, _ = compat.credentials()
    assert [r["registry"] for r in values["registryCredentials"]] == ["cgr.dev"]


def test_only_started_containers_count_as_exercised():
    started_images = load_script("bbcluster").started_images
    pod = lambda status: {"spec": {"initContainers": [{"name": "init", "image": "org/init:1"}],
                                   "containers": [{"name": "app", "image": "org/app:1"}]},
                          "status": status}
    assert started_images(pod({"phase": "Pending"})) == set()
    waiting = {"name": "app", "image": "org/app:1", "state": {"waiting": {"reason": "ImagePullBackOff"}}}
    failed_init = {"name": "init", "image": "org/init:1", "state": {"terminated": {"exitCode": 1}}}
    assert started_images(pod({"containerStatuses": [waiting], "initContainerStatuses": [failed_init]})) == set()
    done_init = {"name": "init", "image": "org/init:1", "state": {"terminated": {"exitCode": 0}}}
    running = {"name": "app", "image": "org/app@sha256:ab", "state": {"running": {}}}
    assert started_images(pod({"containerStatuses": [running], "initContainerStatuses": [done_init]})) == \
        {"org/init:1", "org/app:1", "org/app@sha256:ab"}
