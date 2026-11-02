"""compat/*.yaml are well formed, so a typo fails here and not after a kind run."""
import glob
import os
import shlex

import pytest
import yaml

from conftest import ROOT

FILES = sorted(glob.glob(os.path.join(ROOT, "compat", "*.yaml")))
KNOWN = {"enable", "expect", "values", "values_files", "setup", "debug", "forbid_log", "no_pods", "namespaces",
         "installer", "installer_args", "check", "large", "timeout", "registry1", "upgrade_from"}
UPGRADE_FROM = {"images", "check", "upgrade"}


@pytest.mark.parametrize("path", FILES, ids=lambda p: os.path.basename(p))
def test_compat_file(path):
    doc = yaml.safe_load(open(path, encoding="utf-8"))
    test = doc["test"]
    assert set(test) <= KNOWN, f"unknown keys {set(test) - KNOWN}"
    assert test["enable"] and isinstance(test["enable"], list)
    # What the test is about: images it expects to run, images it maps (an upgrade test),
    # or no_pods (a CronJob that only installs).
    assert test.get("no_pods") or test.get("expect") or doc.get("images"), "nothing to check"
    for f in test.get("values_files", []):
        assert f.startswith("$COMPAT_OUT/"), f
    frm = test.get("upgrade_from")
    if frm:
        assert set(frm) <= UPGRADE_FROM, f"unknown upgrade_from keys {set(frm) - UPGRADE_FROM}"
        assert frm.get("images") and not test.get("installer"), "upgrade_from needs images, and no installer"
        # Starting from an Iron Bank image needs Iron Bank pulls: mark it, so CI skips the
        # test without an Iron Bank account instead of failing on the pull.
        if any(isinstance(v, dict) and "stays" in v for v in frm["images"].values()):
            assert test.get("registry1"), "upgrade_from keeps an image on Iron Bank: set registry1: true"
    for cmd in test.get("setup", []):
        words = shlex.split(cmd)
        if words[0] not in ("python3", "python", "sh", "bash"):
            continue   # a plain command (kubectl ...)
        script = words[1]
        assert os.path.exists(os.path.join(ROOT, script)), script
        if script.endswith("seaweedfs.py"):
            # Credentials only go to the packages named here; each must be one the test runs.
            enabled = {e.split(".")[-1] for e in test["enable"]}
            names = shlex.split(cmd)[2:]
            assert names and set(names) <= enabled, (names, enabled)
            assert "$COMPAT_OUT/overlays/values-object-storage.yaml" in test.get("values_files", [])


def test_scan_reviewed_entries_explain_the_fix():
    reviewed = yaml.safe_load(open(os.path.join(ROOT, "mapping.yaml"), encoding="utf-8"))["scan_reviewed"]
    assert reviewed and all(isinstance(v, str) and v.strip() for v in reviewed.values())


def source(entry):
    """Where a mapping entry takes an image from: a Chainguard name, a build, or Iron Bank."""
    if entry is None or isinstance(entry, str):
        return ("cg", entry)
    for key in ("build", "cg"):
        if key in entry:
            return (key, entry[key])
    return ("stays", None)


@pytest.mark.parametrize("path", FILES, ids=lambda p: os.path.basename(p))
def test_trial_mapping_does_not_undo_mapping_yaml(path):
    # A trial mapping tries out what mapping.yaml doesn't do yet: converting an image that
    # stays on Iron Bank, or another version line of the same source (vault-2). Once
    # mapping.yaml takes an image from elsewhere, a trial naming the old source tests an
    # image clusters no longer run (sonarqube kept Chainguard's jmx-exporter-iamguarded
    # after the mapping moved to the bigbang-jmx-exporter build).
    mapping = yaml.safe_load(open(os.path.join(ROOT, "mapping.yaml"), encoding="utf-8"))["images"]
    for image, trial in (yaml.safe_load(open(path, encoding="utf-8")).get("images") or {}).items():
        if image not in mapping or source(mapping[image])[0] == "stays":
            continue
        assert source(trial) == source(mapping[image]), (
            f"{image}: the trial takes it from {source(trial)}, mapping.yaml from {source(mapping[image])}")


@pytest.mark.parametrize("path", FILES, ids=lambda p: os.path.basename(p))
def test_every_kind_test_checks_the_app_works(path):
    # Ready pods aren't a working app: GitLab Runner stayed Ready while it panicked without
    # a token, Thanos Query had no stores, Gatekeeper refused every org image. Each test
    # needs a `check` command that proves the package does its job.
    test = yaml.safe_load(open(path, encoding="utf-8")).get("test", {})
    assert test.get("check"), f"{os.path.basename(path)}: add a `check:` that proves the app works"
