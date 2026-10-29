"""compat/*.yaml are well formed, so a typo fails here and not after a kind run."""
import glob
import os
import shlex

import pytest
import yaml

from conftest import ROOT

FILES = sorted(glob.glob(os.path.join(ROOT, "compat", "*.yaml")))
KNOWN = {"enable", "expect", "values", "values_files", "setup", "debug", "forbid_log", "no_pods", "namespaces"}


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
    for cmd in test.get("setup", []):
        script = shlex.split(cmd)[1]
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
