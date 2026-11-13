"""bb-preview.py: what Big Bang's next release would break, decided without a network."""
from conftest import load_script

pv = load_script("bb-preview")


def test_a_newer_kubernetes_requirement_is_reported():
    assert pv.kube_problem({"kubeVersion": ">=1.36.0-0"}, "1.35.8")
    assert pv.kube_problem({"kubeVersion": ">=1.35.0-0"}, "1.35.8") is None
    assert pv.kube_problem({}, "1.35.8") is None


def test_image_changes_ignore_tags_and_name_new_and_gone_packages():
    old = {"packages": {
        "loki": {"rendered": ["r/loki:3.5.0", "r/canary:3.5.0"]},
        "tempo": {"rendered": ["r/tempo:2.9"]},
    }}
    new = {"packages": {
        "loki": {"rendered": ["r/loki:3.6.0", "r/sidecar:1.0"]},   # tag moved: not a change
        "mimir": {"rendered": ["r/mimir:3.0"]},
    }}
    assert pv.image_changes(old, new) == {
        "loki": (["r/sidecar"], ["r/canary"]),
        "mimir": (["(new package) r/mimir"], []),
        "tempo": ([], ["(package gone)"]),
    }
    assert pv.image_changes(old, old) == {}


def test_a_test_renders_with_its_enable_list_then_its_values():
    seen = {}

    def test_values(chart_values, enable):
        seen["enable"] = enable
        return {"packages": {"kyverno": {"enabled": True}}, "domain": "bigbang.test"}

    chart = {"packages": {"kyverno": {"git": {"repo": "r"}}, "istioEgressGateway": {"git": {"repo": "r"}}}}
    vals = pv.test_values_for({"enable": ["kyverno"], "values": {"packages": {"kyverno": {"values": {"x": 1}}}}},
                              chart, test_values)
    assert seen["enable"] == ["kyverno"]
    assert vals["packages"]["kyverno"] == {"enabled": True, "values": {"x": 1}}
    # Discovery turns every package on; a test's render turns the rest off again.
    assert vals["packages"]["istioEgressGateway"] == {"enabled": False}


def test_report_says_nothing_for_empty_sections():
    text = pv.report("master", [("Kubernetes version", []), ("Images added or removed", ["loki: + r/x"])])
    assert "### Kubernetes version\nnothing" in text
    assert "```\nloki: + r/x\n```" in text


def test_the_preview_follows_master_and_an_untagged_release_branch():
    import os
    import yaml
    from conftest import ROOT
    wf = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "bigbang-preview.yml"), encoding="utf-8"))
    refs = next(s for s in wf["jobs"]["refs"]["steps"] if s.get("id") == "refs")
    assert "refs/heads/release-" in refs["run"] and '"master"' in refs["run"]
    job = wf["jobs"]["preview"]
    assert job["strategy"]["matrix"]["ref"] == "${{ fromJSON(needs.refs.outputs.refs) }}"
    # Inputs and the matrix reach the scripts as env vars, never interpolated into them.
    for step in [refs] + job["steps"]:
        run = step.get("run", "")
        assert "${{ inputs." not in run and "${{ matrix." not in run and "${{ steps." not in run
