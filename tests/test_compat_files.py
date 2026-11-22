"""compat/*.yaml are well formed, so a typo fails here and not after a kind run."""
import glob
import os
import re
import shlex

import pytest
import yaml

from conftest import ROOT

FILES = sorted(glob.glob(os.path.join(ROOT, "compat", "*.yaml")))
KNOWN = {"enable", "expect", "values", "values_files", "setup", "debug", "forbid_log", "no_pods", "namespaces",
         "installer", "installer_args", "installer_release", "before_upgrade", "check", "large", "timeout", "registry1", "upgrade_from",
         "weekly_and_release"}
UPGRADE_FROM = {"images", "release", "check", "upgrade", "values"}


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
    assert not test.get("before_upgrade") or test.get("installer_release"), "before_upgrade needs installer_release"
    # compat-plan.py and results-gate.py read the flags as `  <flag>: true` lines.
    for flag in ("large", "registry1", "weekly_and_release"):
        assert test.get(flag) in (None, True), f"{flag}: true or leave it out"
    frm = test.get("upgrade_from")
    if frm:
        assert set(frm) <= UPGRADE_FROM, f"unknown upgrade_from keys {set(frm) - UPGRADE_FROM}"
        assert not test.get("installer"), "upgrade_from doesn't work with installer: true"
        # Start from an older mapping (`images`) or from the previous release (`release`).
        assert bool(frm.get("images")) != bool(frm.get("release")), "upgrade_from needs images or release"
        assert frm.get("release") in (None, "previous", "previous-bigbang", "oldest-supported") \
            or re.fullmatch(r"\d+\.\d+\.\d+-cg\.\d+", str(frm.get("release"))), frm.get("release")
        # Starting from an Iron Bank image needs Iron Bank pulls: mark it, so CI skips the
        # test without an Iron Bank account instead of failing on the pull.
        if any(isinstance(v, dict) and "stays" in v for v in (frm.get("images") or {}).values()):
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


def _leaves(tree, path=()):
    """Every dotted path to a non-mapping value in a nested mapping."""
    if isinstance(tree, dict) and tree:
        for k, v in tree.items():
            yield from _leaves(v, path + (str(k),))
    else:
        yield ".".join(path)


@pytest.mark.parametrize("path", FILES, ids=lambda p: os.path.basename(p))
def test_values_do_not_shadow_a_setup_written_values_file(path):
    """bb-compat.py passes a test's `values` to helm after every `values_files` entry, so a
    key set in both wins from `values` and the file's copy is dead (compat/eck-kibana.yaml:
    kibana.count 0 in the FIPS site file never applied). Only the flow-style YAML that a
    setup line echoes into a file is read."""
    test = yaml.safe_load(open(path, encoding="utf-8"))["test"]
    in_files = set()
    for cmd in test.get("setup", []):
        for flow in re.findall(r"echo '(\{.*?\})' >", cmd):
            in_files |= set(_leaves(yaml.safe_load(flow)))
    shadowed = in_files & set(_leaves(test.get("values") or {}))
    assert not shadowed, f"set in `values` and in a setup-written file; `values` wins: {sorted(shadowed)}"
