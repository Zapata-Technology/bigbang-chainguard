"""Mapping an Iron Bank image to the org, and the generated docs."""
import pytest

from conftest import FakeTags, load_script

convert = load_script("bb-convert")

ORG_TAGS = {
    "app": ["1.2.3", "1.2.5"], "app-fips": ["1.2.5"],
    "old": ["2.4.0"], "old-fips": ["2.4.0"],
    "nofips": ["3.0.1"],
    "ready": ["1.0.0"],
    "dated": ["0.20250101.000000", "0.20251015.172955"], "dated-fips": ["0.20251015.172955"],
}
MAPPING = {"images": {
    "ironbank/app": "app",
    "ironbank/old": "old",
    "ironbank/nofips": {"cg": "nofips", "fips": None},
    "ironbank/gone": "gone",
    "ironbank/vendor": {"stays": "proprietary"},
    "ironbank/later": {"stays": "no tags yet", "until": "ready"},
    "ironbank/pinned": {"ref": "ghcr.io/me/pinned", "fips": "ghcr.io/me/pinned-fips", "tag": "9"},
    "ironbank/dated": {"cg": "dated", "version": "*"},
}}


@pytest.fixture
def conv():
    return convert.Converter(MAPPING, FakeTags(ORG_TAGS))


def test_standard_and_fips(conv):
    assert conv.target("ironbank/app", "1.2.3", "pkg") == ("cgr.dev/example.com/app:1.2.5",
                                                           "cgr.dev/example.com/app-fips:1.2.5")


def test_minor_ahead_is_noted(conv):
    std, _ = conv.target("ironbank/old", "2.3.1", "pkg")
    assert std.endswith("old:2.4.0")
    assert "MINOR AHEAD" in conv.rows[-1][4]


def test_strict_versions_leaves_it_unresolved():
    c = convert.Converter(MAPPING, FakeTags(ORG_TAGS), strict=True)
    assert c.target("ironbank/old", "2.3.1", "pkg") is None
    assert c.rows[-1][2] == "UNRESOLVED"


def test_no_fips_variant_keeps_the_standard_image(conv):
    std, fips = conv.target("ironbank/nofips", "3.0.0", "pkg")
    assert std == fips


def test_missing_org_repo(conv):
    assert conv.target("ironbank/gone", "1.0.0", "pkg") is None
    assert {"gone", "gone-fips"} <= conv.missing


def test_unmapped(conv):
    assert conv.target("ironbank/new", "1.0", "pkg") is None
    assert "ironbank/new" in conv.unmapped


def test_stays_and_until(conv):
    assert conv.target("ironbank/vendor", "1", "pkg") is None
    assert conv.target("ironbank/later", "1", "pkg") is None
    assert conv.ready == {("ironbank/later", "ready")}


def test_ref_is_used_as_is(conv):
    assert conv.target("ironbank/pinned", "1", "pkg") == ("ghcr.io/me/pinned:9", "ghcr.io/me/pinned-fips:9")


def test_star_version_is_newest_and_noted(conv):
    std, _ = conv.target("ironbank/dated", "RELEASE.2025-10-15T17-29-55Z", "pkg")
    assert std.endswith("dated:0.20251015.172955")
    assert "newest org release" in conv.rows[-1][4]


def test_override_tokens(conv):
    spec = {"registry": "@registry", "tag": "@tag:app:1.2", "image": {"repository": "@repo:app"},
            "full": "@ref:app:*", "plain": 1}
    assert conv.overrides("pkg", spec, False) == {
        "registry": "cgr.dev/example.com", "tag": "1.2.5", "image": {"repository": "app"},
        "full": "cgr.dev/example.com/app:1.2.5", "plain": 1}
    assert conv.overrides("pkg", spec, True)["image"]["repository"] == "app-fips"


def test_allowed_registries():
    assert convert.allowed_registries(MAPPING, "cgr.dev/example.com", "ghcr.io/me") == [
        "cgr.dev/example.com", "ghcr.io/me"]
    assert convert.allowed_registries(MAPPING, "cgr.dev/example.com") == ["cgr.dev/example.com", "ghcr.io/me"]


def test_report_lists_version_differences(conv):
    conv.target("ironbank/old", "2.3.1", "pkg")
    conv.target("ironbank/dated", "RELEASE.2025", "pkg")
    conv.target("ironbank/vendor", "1", "pkg")
    md = convert.report(conv.rows, "3.0.0", conv.cg)
    assert "## Stays on Iron Bank (1)" in md
    assert "## Version differences (2)" in md
    assert "(newer minor)" in md and "(newest release" in md


def test_required_images_is_org_independent():
    discovered = {"packages": {"pkg": {"rendered": ["registry1.dso.mil/ironbank/app:1.2.3"], "values": [
        {"shape": "repo", "value": {"repository": "registry1.dso.mil/ironbank/nofips", "tag": "3"}},
        {"shape": "repo", "value": {"repository": "registry1.dso.mil/ironbank/vendor", "tag": "1"}}]}}}
    md = convert.required_images(discovered, MAPPING, "3.0.0")
    assert "**2 standard images**" in md and "**1 FIPS images**" in md
    assert "| `app` | `app-fips` | pkg |" in md
    assert "| `nofips` | none: FIPS keeps the standard image | pkg |" in md
    assert "vendor" not in md


GROUP = {"images": {
    "gitlab/web": {"group": "gl", "strict": True, "fallback": "ironbank", "cg": "web"},
    "gitlab/shell": {"group": "gl", "strict": True, "fallback": "ironbank", "cg": "shell"},
    "ironbank/opt": {"cg": "opt", "fallback": "ironbank"},
    "ironbank/strict": {"cg": "old", "strict": True},
}}
IB_TAGS = {"gitlab/web": "v19.4.2-ubi", "gitlab/shell": "v14.57.3-ubi"}


def test_fallback_keeps_iron_bank_instead_of_failing():
    c = convert.Converter(GROUP, FakeTags({}), ib_tags=IB_TAGS)
    assert c.target("ironbank/opt", "1.0.0", "pkg") is None
    assert c.rows[-1][2] == "Iron Bank" and "falls back" in c.rows[-1][4]
    assert "opt" in c.optional_missing and not c.missing


def test_strict_per_image():
    c = convert.Converter(GROUP, FakeTags(ORG_TAGS))
    assert c.target("ironbank/strict", "2.3.1", "pkg") is None
    assert c.rows[-1][2] == "UNRESOLVED"


def test_group_converts_together():
    tags = {"web": ["19.4.5"], "web-fips": ["19.4.5"], "shell": ["14.57.4"], "shell-fips": ["14.57.4"]}
    c = convert.Converter(GROUP, FakeTags(tags), ib_tags=IB_TAGS)
    assert c.target("gitlab/web", "v19.4.2-ubi", "gitlab")[0].endswith("web:19.4.5")
    assert c.target("gitlab/shell", "v14.57.3-ubi", "gitlab")[0].endswith("shell:14.57.4")


def test_group_falls_back_together_when_one_member_is_off_the_line():
    tags = {"web": ["19.4.5"], "web-fips": ["19.4.5"], "shell": ["14.58.0"], "shell-fips": ["14.58.0"]}
    c = convert.Converter(GROUP, FakeTags(tags), ib_tags=IB_TAGS)
    assert c.target("gitlab/web", "v19.4.2-ubi", "gitlab") is None
    assert "move together" in c.rows[-1][4] and "shell" in c.rows[-1][4]


def test_version_from_matches_another_images_release():
    m = {"images": {"gitlab/shell": {"group": "gl", "strict": True, "cg": "shell",
                                     "version_from": "gitlab/base"}}}
    tags = {"shell": ["19.4.3", "19.5.1"], "shell-fips": ["19.4.3"]}
    c = convert.Converter(m, FakeTags(tags), ib_tags={"gitlab/shell": "v14.57.3-ubi", "gitlab/base": "v19.4.2-ubi"})
    assert c.target("gitlab/shell", "v14.57.3-ubi", "gitlab")[0].endswith("shell:19.4.3")


def test_flux_extras_are_deleted():
    import fluxrelease
    patches = [p["patch"] for p in fluxrelease.remove_extras()]
    assert len(patches) == 3 and all("$patch: delete" in p and "kind: Deployment" in p for p in patches)


def test_fips_override_keeps_standard_when_the_fips_repo_has_no_tags():
    c = convert.Converter({"images": {}}, FakeTags({"mgr": ["5.6", "5.6.2"], "mgr-fips": [], "ctl-fips": ["5.6"]}))
    assert c.overrides("neuvector", {"repo": "@repo:mgr", "tag": "@tag:ctl:5.6"}, True) == {"repo": "mgr", "tag": "5.6"}
    assert "keeps the standard image" in c.rows[-1][4]


def test_override_tag_from_iron_banks_deployed_tag():
    c = convert.Converter({"images": {}}, FakeTags({"proxy": ["1.31.3", "1.32.0"], "proxy-fips": ["1.31.2"]}),
                          ib_tags={"ironbank/istio/proxyv2": "1.31.1"})
    spec = {"tag": "@tag:proxy:ib=ironbank/istio/proxyv2", "image": "@repo:proxy", "hub": "@registry"}
    assert c.overrides("istiod", spec, False) == {"tag": "1.31.3", "image": "proxy", "hub": "cgr.dev/example.com"}
    assert c.overrides("istiod", spec, True)["tag"] == "1.31.2"


def test_group_members_share_one_patch():
    tags = {"web": ["19.4.3", "19.4.5"], "web-fips": ["19.4.3", "19.4.5"],
            "shell": ["19.4.3"], "shell-fips": ["19.4.3", "19.4.5"]}
    m = {"images": {"gitlab/web": {"group": "gl", "strict": True, "cg": "web", "same_patch": "rails"},
                    "gitlab/shell": {"group": "gl", "strict": True, "cg": "shell", "version_from": "gitlab/web",
                                     "same_patch": "rails"}}}
    c = convert.Converter(m, FakeTags(tags), ib_tags={"gitlab/web": "v19.4.2-ubi", "gitlab/shell": "v14.57.3-ubi"})
    assert c.target("gitlab/web", "v19.4.2-ubi", "gitlab") == ("cgr.dev/example.com/web:19.4.3",
                                                               "cgr.dev/example.com/web-fips:19.4.3")


def _generate(mapping, key="kyvernoPolicies"):
    """generate() for one package with no images, so only the mapping's sections show."""
    discovered = {"packages": {"pkg": {"key": key, "rendered": [], "values": []}}}
    files, _ = convert.generate(discovered, {"images": {}, **mapping}, FakeTags({}), "3.35.1")
    import yaml
    return (yaml.safe_load(files["overlays/values-chainguard.yaml"]),
            yaml.safe_load(files["overlays/values-chainguard-fips.yaml"]))


def _patches(tree, *key):
    for k in key:
        tree = tree[k]
    return [p for r in tree["postRenderers"] for p in r["kustomize"].get("patches", [])]


def test_bash_wait_jobs_run_their_scripts_with_bash():
    import json
    for tree in _generate({"bash_wait_jobs": {"kyvernoPolicies": ["kp-wait-job"]}}):
        [p] = _patches(tree, "kyvernoPolicies")
        assert p["target"] == {"kind": "Job", "name": "kp-wait-job"}
        [op] = json.loads(p["patch"])
        assert op["op"] == "replace" and op["path"] == "/spec/template/spec/containers/0/command"
        shell, flag, script = op["value"]
        assert (shell, flag) == ("/bin/bash", "-c")
        assert 'bash "./$script" || exit 1' in script and '[ -f "$script" ] || continue' in script


def test_bash_commands_swap_sh_for_bash():
    import json
    ref = {"kind": "Job", "name": ".*-keystore-job", "container": "initContainers/1"}
    for tree in _generate({"bash_commands": {"addons.fortify": [ref]}}, key="addons.fortify"):
        [p] = _patches(tree, "addons", "fortify")
        assert p["target"] == {"kind": "Job", "name": ".*-keystore-job"}
        assert json.loads(p["patch"]) == [{"op": "replace", "value": "/bin/bash",
                                           "path": "/spec/template/spec/initContainers/1/command/0"}]


def test_plain_override_values_reach_both_overlays():
    spec = {"upstream": {"config": "receivers: {{ omit .Values.r \"opencensus\" }}", "x": None}}
    for tree in _generate({"overrides": {"pkg": spec}}, key="tempo"):
        assert tree["tempo"]["values"]["upstream"] == spec["upstream"]


def test_bash_wait_script_runs_sh_scripts_with_bash(tmp_path):
    import shutil
    import subprocess
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash")
    ok = tmp_path / "ok"
    ok.mkdir()
    (ok / "wait.sh").write_text('#!/bin/sh\nA=(x y)\necho "got ${A[1]}"\n', newline="\n")
    p = subprocess.run([bash, "-c", convert.BASH_WAIT], cwd=ok, capture_output=True, text=True)
    assert p.returncode == 0 and "got y" in p.stdout
    empty = tmp_path / "empty"
    empty.mkdir()
    assert subprocess.run([bash, "-c", convert.BASH_WAIT], cwd=empty).returncode == 0
    (ok / "z.sh").write_text("exit 3\n", newline="\n")
    assert subprocess.run([bash, "-c", convert.BASH_WAIT], cwd=ok, capture_output=True).returncode == 1
