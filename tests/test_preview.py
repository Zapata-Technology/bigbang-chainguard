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


# --- a release branch's kind tests (compat.yml with bigbang_ref) ---

def _result(test, mode, res):
    return {"test": test, "mode": mode, "result": res, "bigbang": "release-4.2.0", "preview": True}


def _workflow(name):
    import os
    import yaml
    from conftest import ROOT
    return yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8"))


def test_kind_results_read_every_artifact_and_keep_errors(tmp_path):
    import json
    (tmp_path / "compat-result-a-standard").mkdir()
    (tmp_path / "compat-result-a-standard" / "a-standard.json").write_text(json.dumps(_result("a", "standard", "error")))
    (tmp_path / "junk.json").write_text("{not json")
    (tmp_path / "other.json").write_text(json.dumps({"hello": 1}))
    assert [(r["test"], r["result"]) for r in pv.kind_results(str(tmp_path))] == [("a", "error")]


def test_kind_section_passes_only_when_every_test_and_the_run_passed():
    ok_results = [_result("kyverno-enforce", "fips", "pass"), _result("vault-upgrade", "standard", "skipped")]
    text, ok = pv.kind_section("release-4.2.0", ok_results, "success", "https://run")
    assert ok and "| kyverno-enforce | fips | PASS |" in text and "https://run" in text
    assert text.startswith(pv.KIND_START) and text.endswith(pv.KIND_END)
    assert not pv.kind_section("r", ok_results + [_result("x", "standard", "fail")], "failure", "u")[1]
    # The plan failed, or nothing recorded a result: not a pass.
    assert not pv.kind_section("r", ok_results, "failure", "u")[1]
    text, ok = pv.kind_section("r", [], "success", "u")
    assert not ok and "No test recorded a result" in text


def test_the_kind_section_replaces_yesterdays_and_keeps_the_report():
    first = pv.with_kind_section("report\n", pv.KIND_START + "\nold\n" + pv.KIND_END)
    assert first == "report\n\n" + pv.KIND_START + "\nold\n" + pv.KIND_END + "\n"
    again = pv.with_kind_section(first, pv.KIND_START + "\nnew\n" + pv.KIND_END)
    assert "old" not in again and "new" in again and again.startswith("report\n")
    assert pv.with_kind_section("", "S") == "S\n"


def test_the_issue_closes_only_when_the_preview_and_the_kind_tests_are_clean():
    clean, broken = "x\n" + pv.NOTHING + "\n", "x\n**Something would break.**\n"
    assert pv.kind_action(True, clean, True) == "close"
    assert pv.kind_action(True, broken, True) == "edit"
    assert pv.kind_action(False, "", True) == "none"
    assert pv.kind_action(True, clean, False) == "edit"
    assert pv.kind_action(False, "", False) == "create"


def test_the_release_branch_is_deployed_on_kind_and_master_is_not():
    wf = _workflow("bigbang-preview.yml")
    refs = next(s for s in wf["jobs"]["refs"]["steps"] if s.get("id") == "refs")
    assert "release=release-$branch" in refs["run"] and "^release-" in refs["run"]
    kind = wf["jobs"]["kind"]
    assert kind["uses"] == "./.github/workflows/compat.yml"
    assert kind["with"] == {"bigbang_ref": "${{ needs.refs.outputs.release }}"}
    assert kind["if"] == "needs.refs.outputs.release != ''"
    assert "rke2" not in str(kind)
    report = wf["jobs"]["kind-report"]
    assert set(report["needs"]) == {"refs", "preview", "kind"}
    for step in report["steps"]:
        assert "${{" not in step.get("run", "")
    # The preview leaves a clean release branch's issue for kind-report to close.
    keep = next(s for s in wf["jobs"]["preview"]["steps"] if s.get("name") == "Keep the preview issue current")
    assert '"$REF" = "$RELEASE"' in keep["run"] and "${{" not in keep["run"]


def test_compat_takes_a_big_bang_ref_and_marks_its_results():
    wf = _workflow("compat.yml")
    on = wf[True]   # PyYAML reads the `on` key as True
    assert on["workflow_call"]["inputs"]["bigbang_ref"]["required"] is True
    assert "bigbang_ref" in on["workflow_dispatch"]["inputs"]
    for name in ("plan", "test"):
        assert wf["jobs"][name]["env"]["BIGBANG_REF"] == "${{ inputs.bigbang_ref }}"
    steps = {s.get("name") or s.get("id"): s for s in wf["jobs"]["test"]["steps"]}
    assert "--preview" in steps["Record the result"]["run"]
    assert "BIGBANG_VERSION" in steps["Big Bang ref in place of BIGBANG_VERSION"]["run"]
    for job in wf["jobs"].values():
        for step in job.get("steps", []):
            assert "${{ inputs." not in step.get("run", "")
