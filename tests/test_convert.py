"""Mapping an Iron Bank image to the org, and the generated docs."""
import json
import os
import sys

import pytest

from conftest import ROOT, FakeTags, load_script, require_bash

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


def test_both_registry_policies_admit_the_org_builds_and_iron_bank():
    # Gatekeeper's constraint refused every org image (compat/gatekeeper.yaml); Helm
    # replaces lists, so Iron Bank's registries must stay for the documented leftovers.
    policies = convert.registry_policies(MAPPING, "cgr.dev/example.com", "ghcr.io/me")
    # Each ends in "/": the policies match prefixes, and a bare "ghcr.io/me" also admitted
    # "ghcr.io/me-evil/..." and "registry1.dso.mil" admitted "registry1.dso.mil.evil.tld".
    expected = ["registry1.dso.mil/", "registry.dso.mil/", "cgr.dev/example.com/", "ghcr.io/me/"]
    assert policies == {
        ("packages", "kyvernoPolicies", "values", "policies", "restrict-image-registries", "parameters", "allow"):
            expected,
        ("packages", "gatekeeper", "values", "violations", "allowedDockerRegistries", "parameters", "repos"): expected,
    }


def test_images_built_here_are_digest_pinned_when_published():
    """0 of 55 ghcr.io refs were pinned (2026-11-20): a push over a tag reached clusters."""
    c = convert.Converter(MAPPING, FakeTags({}), "ghcr.io/me")
    c.used = {"r": {"standard": {"ghcr.io/me/bigbang-base:2.1", "ghcr.io/me/bigbang-new:9",
                                 "ghcr.io/other/x:1"}, "fips": set()}}
    published = {("ghcr.io/me/bigbang-base", "2.1"): "sha256:" + "c" * 64}
    c.build_digest = lambda repo, tag: published.get((repo, tag))
    assert c.digest_pins("r", "standard") == [{"name": "ghcr.io/me/bigbang-base", "digest": "sha256:" + "c" * 64}]
    # Without a login (or offline) nothing built here is pinned, as before.
    c.build_digest = None
    assert c.digest_pins("r", "standard") == []


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


@pytest.mark.parametrize("overlay", ["values-chainguard.yaml", "values-chainguard-fips.yaml"])
def test_istio_gateway_pods_pass_pod_security_restricted(overlay):
    """Istio's gateway chart sets no seccomp profile; PSA `restricted` refused the gateways."""
    import yaml
    shared = yaml.safe_load(open(os.path.join(ROOT, "overlays", overlay), encoding="utf-8"))[
        "packages"]["istioGateway"]["values"]["shared"]
    sc = shared["upstream"]["securityContext"]
    assert sc["seccompProfile"] == {"type": "RuntimeDefault"}
    assert {"name": "net.ipv4.ip_unprivileged_port_start", "value": "0"} in sc["sysctls"]   # the chart's default


@pytest.mark.parametrize("overlay", ["values-chainguard.yaml", "values-chainguard-fips.yaml"])
def test_every_wait_job_gets_the_runtime_default_seccomp_profile(overlay):
    """template_defaults' `beside`: gluon's wait job sets no seccomp profile, and PSA
    `restricted` (RKE2's CIS profile) refused kyverno-policies' wait job."""
    import yaml
    found = []

    def walk(node, path):
        if isinstance(node, dict):
            if isinstance(node.get("waitJob"), dict) and "image" in node["waitJob"]:
                found.append((".".join(path), node["waitJob"].get("seccompProfile")))
            for k, v in node.items():
                walk(v, path + [k])
    walk(yaml.safe_load(open(os.path.join(ROOT, "overlays", overlay), encoding="utf-8")), [])
    assert len(found) > 5
    # The MinIO subcharts' wait jobs (Loki, Mattermost) take their image from a chart value,
    # not template_defaults; with SeaweedFS for object storage they don't render.
    missing = [p for p, s in found if s != {"type": "RuntimeDefault"} and not p.endswith(".minio")]
    assert {"packages.kyvernoPolicies.values", "packages.kiali.values"} <= {p for p, _ in found}
    # Istio's gateway chart refuses waitJob.seccompProfile: a post-renderer patch instead.
    missing = [p for p in missing if p != "packages.istioGateway.values.shared"]
    gateway = yaml.safe_load(open(os.path.join(ROOT, "overlays", overlay), encoding="utf-8"))["packages"]["istioGateway"]
    patches = [p for pr in gateway.get("postRenderers", []) for p in pr.get("kustomize", {}).get("patches", [])]
    assert any("seccompProfile" in p["patch"] and p["target"]["kind"] == "Job" for p in patches)
    assert not missing, f"regenerate the overlays (scripts/bb-convert.py): {missing}"


@pytest.mark.parametrize("d", ["flux", "flux-fips"])
def test_flux_controllers_pass_pod_security_restricted(d):
    """Big Bang's Flux patch replaces the container securityContext and with it Flux's
    seccompProfile; PSA `restricted` (RKE2's CIS profile) refused every controller. The
    generated kustomization ends with a patch putting RuntimeDefault on each pod."""
    import yaml
    import fluxrelease
    patch = fluxrelease.seccomp_patch()
    assert patch["target"] == {"kind": "Deployment"}
    spec = yaml.safe_load(patch["patch"])["spec"]["template"]["spec"]
    assert spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    generated = yaml.safe_load(open(os.path.join(ROOT, d, "kustomization.yaml"), encoding="utf-8"))
    assert generated["patches"][-1] == patch, f"regenerate {d}/ (scripts/bb-convert.py)"


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


def test_progress_deadline_patches_deployments_beside_the_digest_pins():
    import json
    trees = _generate({"progress_deadline": {"packages.gitlab": 2400}}, key="packages.gitlab")
    for tree in trees:
        [p] = _patches(tree, "packages", "gitlab")
        assert p["target"] == {"kind": "Deployment"}
        assert json.loads(p["patch"]) == [{"op": "add", "path": "/spec/progressDeadlineSeconds", "value": 2400}]
    # A package whose template can't take post-renderers gets neither this patch nor digest pins.
    skipped = _generate({"progress_deadline": {"packages.keycloak": 2400},
                         "no_post_renderers": {"packages.keycloak": "template"}}, key="packages.keycloak")
    for tree in skipped:
        assert "postRenderers" not in tree.get("packages", {}).get("keycloak", {})


def test_plain_override_values_reach_both_overlays():
    spec = {"upstream": {"config": "receivers: {{ omit .Values.r \"opencensus\" }}", "x": None}}
    for tree in _generate({"overrides": {"pkg": spec}}, key="tempo"):
        assert tree["tempo"]["values"]["upstream"] == spec["upstream"]


def test_bash_wait_script_runs_sh_scripts_with_bash(tmp_path):
    import subprocess
    bash = require_bash()
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


def test_digest_pins_each_org_image_once_per_variant(conv):
    conv.target("ironbank/app", "1.2.3", "rel")
    conv.target("ironbank/nofips", "3.0.1", "rel")
    std = conv.digest_pins("rel", "standard")
    fips = conv.digest_pins("rel", "fips")
    assert [p["name"] for p in std] == ["cgr.dev/example.com/app", "cgr.dev/example.com/nofips"]
    assert [p["name"] for p in fips] == ["cgr.dev/example.com/app-fips", "cgr.dev/example.com/nofips"]
    assert all(p["digest"].startswith("sha256:") and len(p["digest"]) == 71 for p in std + fips)
    assert conv.digest_pins("other", "standard") == []


def test_digest_pins_skip_a_repo_used_at_two_tags():
    c = convert.Converter({"images": {"ironbank/a": "app", "ironbank/b": {"cg": "app", "tag": "1.2.3"}}},
                          FakeTags(ORG_TAGS))
    c.target("ironbank/a", "1.2.5", "rel")
    c.target("ironbank/b", "1.2.3", "rel")
    assert c.digest_pins("rel", "standard") == []     # kustomize matches by name: ambiguous


def test_render_emulates_kustomize_digest_pins():
    images = load_script("bb-images")
    pin = {"name": "cgr.dev/o/app", "digest": "sha256:" + "a" * 64}
    assert images.kustomize_image("cgr.dev/o/app", "1.2", pin) == "cgr.dev/o/app@sha256:" + "a" * 64
    assert images.kustomize_image("cgr.dev/o/app", "", pin).endswith("@sha256:" + "a" * 64)   # a bare name in a pod spec
    assert images.kustomize_image("r1/x", "1", {"newName": "cgr.dev/o/x", "newTag": "2"}) == "cgr.dev/o/x:2"
    assert images.kustomize_image("cgr.dev/o/x", "", {"newName": "cgr.dev/o/y"}) == "cgr.dev/o/y"
    assert images.kustomize_image("cgr.dev/o/x", "1", None) is None


def test_discovery_keeps_big_bangs_order_while_rendering_in_parallel(tmp_path, monkeypatch, capsys):
    """Packages render in parallel; errors and results land as a sequential loop puts them."""
    images = load_script("bb-images")
    (tmp_path / "pkgs" / "a" / ".git").mkdir(parents=True)
    (tmp_path / "pkgs" / "a" / ".git" / "config").write_text("url = https://repo/a.git\n")
    rels = [{"release": n, "repo": "https://repo/a" if n != "lost" else "https://repo/none", "path": "chart",
             "namespace": n, "values": {}, "renames": {}} for n in ("one", "lost", "two")]
    monkeypatch.setattr(images, "bigbang_releases", lambda *a: iter(rels))
    monkeypatch.setattr(images, "flux_images", lambda b: [])
    monkeypatch.setattr(images, "chart_defaults", lambda c: {})
    monkeypatch.setattr(images, "render_package", lambda c, v, rel, ns: (0, f"image: registry1.dso.mil/x/{rel}:1", ""))
    monkeypatch.setattr(sys, "argv", ["bb-images.py", "--bigbang", "b", "--pkgs", str(tmp_path / "pkgs")])
    images.main()
    out = json.loads(capsys.readouterr().out)
    assert out["errors"] == {"lost": "no clone for https://repo/none"}
    assert out["packages"]["two"]["rendered"] == ["registry1.dso.mil/x/two:1"]
    assert sorted(out["packages"]) == ["flux", "one", "two"]


def test_max_version_holds_the_group_and_lets_a_star_version_float():
    tags = {"web": ["19.5.3", "19.5.5"], "web-fips": ["19.5.3", "19.5.5"],
            "sidekiq": ["19.5.3", "19.5.5"], "sidekiq-fips": ["19.5.3", "19.5.5"],
            "mail": ["1.1.0", "1.2.0"], "mail-fips": ["1.1.0", "1.2.0"]}
    cap = {"group": "gl", "strict": True, "max_version": "19.5.3"}
    m = {"images": {"gitlab/web": {**cap, "cg": "web", "same_patch": "rails"},
                    "gitlab/sidekiq": {**cap, "cg": "sidekiq", "same_patch": "rails"},
                    "gitlab/mail": {**cap, "cg": "mail", "version": "*"}}}
    ib = {"gitlab/web": "v19.5.3-ubi", "gitlab/sidekiq": "v19.5.3-ubi", "gitlab/mail": "v19.5.3-ubi"}
    c = convert.Converter(m, FakeTags(tags), ib_tags=ib)
    assert c.target("gitlab/web", "v19.5.3-ubi", "gitlab")[0].endswith("web:19.5.3")
    assert c.target("gitlab/sidekiq", "v19.5.3-ubi", "gitlab")[0].endswith("sidekiq:19.5.3")
    assert c.target("gitlab/mail", "v19.5.3-ubi", "gitlab")[0].endswith("mail:1.2.0")


def test_min_version_raises_an_end_of_life_line_to_the_newest_release():
    m = {"images": {"k/kubectl": {"cg": "kubectl", "suffix": "dev", "min_version": "1.34"}}}
    tags = {"kubectl": ["1.30.14-dev", "1.37.2-dev"], "kubectl-fips": ["1.30.14-dev", "1.37.2-dev"]}
    c = convert.Converter(m, FakeTags(tags))
    std, fips = c.target("k/kubectl", "v1.30.11", "vault")
    assert std.endswith("kubectl:1.37.2-dev") and fips.endswith("kubectl-fips:1.37.2-dev")
    assert "below min_version" in c.rows[-1][4]
    assert not convert.below_floor("v1.35", "1.34") and convert.below_floor("v1.30.11", "1.34")


def test_follow_major_takes_the_newest_release_of_iron_banks_major():
    # PostgreSQL: Iron Bank's 18.6 means 18; the org's 18.7 is its patch release. Never 19.
    m = {"images": {"p/postgresql": {"cg": "postgres", "follow": "major"},
                    "a/kafka-native": {"cg": "kafka-native", "fips": None, "follow": "major"}}}
    tags = {"postgres": ["17.12", "18", "18.6", "18.7", "19.1"], "postgres-fips": ["18.6", "18.7"],
            "kafka-native": ["4.3", "4.3.2", "v4.4.0", "4.4", "4.4.0"]}
    c = convert.Converter(m, FakeTags(tags), strict=True)
    std, fips = c.target("p/postgresql", "18.6", "keycloak")
    assert std.endswith("postgres:18.7") and fips.endswith("postgres-fips:18.7")
    assert "newest 18.x release (follow: major)" in c.rows[-1][4]
    # A minor line the org stopped rebuilding: the newer minor, plain spelling over v-prefixed,
    # even with --strict-versions (follow is chosen per image).
    assert c.target("a/kafka-native", "4.3.2", "mimir")[0].endswith("kafka-native:4.4.0")
    # Nothing at or above Iron Bank's tag in its MAJOR: unresolved, never an older one.
    assert convert.newest_in_major("18.8", tags["postgres"]) == (None, "no 18.x tag at or above 18.8 in org")
    assert convert.newest_in_major("18.7", tags["postgres"]) == ("18.7", None)


def test_follow_accepts_only_major():
    import bbcommon
    with pytest.raises(ValueError, match="follow: only 'major'"):
        bbcommon.validate_mapping({"images": {"p/postgresql": {"cg": "postgres", "follow": "minor"}}})


def test_override_images_are_pinned_unless_listed_unpinned(conv):
    conv.overrides("nv", {"registry": "@registry", "repository": "@repo:app", "tag": "@tag:app:1.2"}, False)
    assert [p["name"] for p in conv.digest_pins("nv", "standard")] == ["cgr.dev/example.com/app"]
    # A chart-wide tag reaches the @repo images under it (NeuVector's enforcer, manager).
    conv.overrides("nv2", {"tag": "@tag:app:1.2", "enforcer": {"image": {"repository": "@repo:old"}},
                           "manager": {"image": {"repository": "@repo:app"}}}, False)
    assert [p["name"] for p in conv.digest_pins("nv2", "standard")] == ["cgr.dev/example.com/app"]
    c = convert.Converter({**MAPPING, "unpinned": {"nv": {"app": "injected from a ConfigMap"}}}, FakeTags(ORG_TAGS))
    c.overrides("nv", {"image": "@ref:app:1.2"}, False)
    c.overrides("nv", {"image": "@ref:app:1.2"}, True)
    assert c.digest_pins("nv", "standard") == [] and c.digest_pins("nv", "fips") == []


def test_post_renderer_rename_carries_the_digest():
    discovered = {"packages": {"fortify": {"key": "addons.fortify", "values": [],
                                           "rendered": ["registry1.dso.mil/ironbank/app:1.2.3"]}}}
    mapping = {"images": {"ironbank/app": "app"}, "post_renderers": {"addons.fortify": ["ironbank/app"]}}
    files, c = convert.generate(discovered, mapping, FakeTags(ORG_TAGS), "3.35.1")
    import yaml
    images = yaml.safe_load(files["overlays/values-chainguard.yaml"])["addons"]["fortify"]["postRenderers"][0][
        "kustomize"]["images"]
    rename = next(i for i in images if i["name"] == "registry1.dso.mil/ironbank/app")
    assert rename["newName"] == "cgr.dev/example.com/app" and rename["digest"] == c.tags.pinned("app", "1.2.5")


REAL_PREFETCH = convert.prefetch


def _convert_repo(tmp_path, monkeypatch, with_prefetch):
    """bb-convert.py's main() on the repo's real mapping and discovery, against a fake
    registry (every Iron Bank version as a tag, some repositories missing); returns the
    files it writes and the registry lookups made after the prefetch."""
    import zlib

    import fluxrelease
    import orgtags
    versions = sorted({t.lstrip("v") for info in json.load(open(os.path.join(ROOT, "discovery", "images.json")))["packages"].values()
                       for ref in info["rendered"] if ":" in ref.rsplit("/", 1)[-1]
                       for t in [ref.split("@")[0].rsplit(":", 1)[1]]})
    calls, after = [], []

    def fetch_tags(self, repo):
        (after if after else calls).append(repo)
        return None if zlib.crc32(repo.encode()) % 9 == 0 else versions + [v + "-dev" for v in versions]

    def digest(self, repo, tag):
        (after if after else calls).append(f"{repo}:{tag}")
        return "sha256:" + format(zlib.crc32(f"{repo}:{tag}".encode()), "08x").ljust(64, "0")
    monkeypatch.setattr(orgtags.OrgTags, "_fetch_tags", fetch_tags)
    monkeypatch.setattr(orgtags.OrgTags, "digest", digest)
    monkeypatch.setattr(fluxrelease, "_release_for", lambda v: ("v2.10.0", dict(v)))
    monkeypatch.setattr(fluxrelease, "_RELEASES", {})
    monkeypatch.setattr(fluxrelease, "bigbang_patches", lambda *a: [])
    def prefetch(*a):
        if with_prefetch:
            REAL_PREFETCH(*a)
        after.append("-- real run --")
    monkeypatch.setattr(convert, "prefetch", prefetch)
    out = tmp_path / ("with" if with_prefetch else "without")
    monkeypatch.setattr(sys, "argv", ["bb-convert.py", "--org", "example.com", "--build-registry", "ghcr.io/o",
                                      "--object-storage", "seaweedfs", "--out", str(out)])
    rc = convert.main()
    files = {str(p.relative_to(out)): p.read_text() for p in out.rglob("*") if p.is_file()}
    return rc, files, after[1:]


def test_prefetch_changes_nothing_and_leaves_the_real_run_no_lookups(tmp_path, monkeypatch):
    rc, plain, sequential = _convert_repo(tmp_path, monkeypatch, with_prefetch=False)
    assert len(sequential) > 100     # without it, every lookup happens one at a time
    rc2, fetched, lookups = _convert_repo(tmp_path, monkeypatch, with_prefetch=True)
    assert len(plain) >= 5 and fetched == plain and rc2 == rc
    assert lookups == []    # everything came from the parallel prefetch


def test_version_difference_compares_major_minor_patch():
    vd = convert.version_difference
    assert vd("v1.20.3", "1.20.3") == "same"
    assert vd("1.21.3-amd64", "1.21.3") == "same"                       # Iron Bank's arch suffix
    assert vd("8.10.2", "8.10.3") == "newer patch"
    assert vd("v1.9.4", "1.10.0") == "newer minor"
    assert vd("1.21.4", "v2.2.1") == "newer major"
    assert vd("1.14.3", "0.9") == "older major"
    assert vd("RELEASE.2025-09-07T16-13-09Z", "7.1.1") == "no version to compare"
    assert vd("ubi-fips-v19.2.2", "19.5.2") == "newer minor"                 # GitLab Runner's prefix
    assert vd("1.0", None) == "stays on Iron Bank"


def test_version_report_groups_and_explains_other_numbering():
    rows = [("tempo", "ironbank/opensource/grafana/tempo:3.1.1", "cgr.dev/o/tempo:3.1.2", "cgr.dev/o/tempo-fips:3.1.2", ""),
            ("flux", "ironbank/fluxcd/source-controller:v1.9.5", "cgr.dev/o/flux-source-controller:1.10.1",
             "cgr.dev/o/flux-source-controller-fips:1.10.1", "MINOR AHEAD"),
            ("gitlab", "gitlab/gitlab-org/build/cng/gitlab-shell:v14.58.0-ubi", "cgr.dev/o/gitlab-shell:19.5.3",
             "cgr.dev/o/gitlab-shell-fips:19.5.3", ""),
            ("twistlock", "ironbank/twistlock/console/console:34.05.168", "Iron Bank", "Iron Bank", "vendor"),
            ("neuvector", "override @repo:neuvector-manager", "neuvector-manager", "neuvector-manager", "note")]
    images = {"gitlab/gitlab-org/build/cng/gitlab-shell": {"cg": "gitlab-shell",
                                                           "version_from": "gitlab/gitlab-org/build/cng/gitlab-base"}}
    doc = convert.version_report(rows, "4.0.0", "cgr.dev/o", images)
    assert "| newer minor | 1 |" in doc and "| newer patch | 1 |" in doc and "| **total** | **4** |" in doc
    assert doc.index("## Different version line (1)") < doc.index("## Newer patch, same line (1)")
    assert "numbered differently | gitlab (follows `gitlab/gitlab-org/build/cng/gitlab-base`'s version)" in doc
    assert "## Stays on Iron Bank (1)" in doc and "override" not in doc


def test_chart_registry_builds_move_the_built_image_to_build_registry(monkeypatch):
    # NeuVector's chart renders <its one registry>/<repository>: the org's registry, not ours.
    monkeypatch.setattr(convert, "build_target", lambda m, reg: (f"{reg}/bigbang-x:v1", f"{reg}/bigbang-x-fips:v1")
                        if reg else None)
    mapping = {"chart_registry_builds": {"packages.neuvector": ["ironbank/x"]}}
    c = type("C", (), {"images": {"ironbank/x": {"build": "x"}}, "cg": "cgr.dev/org", "build_registry": "ghcr.io/o",
                       "build_digest_of": staticmethod(lambda repo, tag: None)})
    assert convert.chart_registry_builds(mapping, c, "packages.neuvector", "standard") == \
        [{"name": "cgr.dev/org/bigbang-x", "newName": "ghcr.io/o/bigbang-x", "newTag": "v1"}]
    # Published: pinned by digest instead of the tag.
    c.build_digest_of = staticmethod(lambda repo, tag: "sha256:" + "b" * 64)
    assert convert.chart_registry_builds(mapping, c, "packages.neuvector", "standard") == \
        [{"name": "cgr.dev/org/bigbang-x", "newName": "ghcr.io/o/bigbang-x", "digest": "sha256:" + "b" * 64}]
    c.build_digest_of = staticmethod(lambda repo, tag: None)
    assert convert.chart_registry_builds(mapping, c, "packages.neuvector", "fips")[0]["newName"] == "ghcr.io/o/bigbang-x-fips"
    assert convert.chart_registry_builds(mapping, c, "packages.argocd", "standard") == []
    # No BUILD_REGISTRY: the image stays on Iron Bank, but the chart still puts the org's
    # registry in front of its Iron Bank repository; it goes back to registry1 at the chart's tag.
    c.build_registry = ""
    assert convert.chart_registry_builds(mapping, c, "packages.neuvector", "standard") ==         [{"name": "cgr.dev/org/ironbank/x", "newName": "registry1.dso.mil/ironbank/x"}]


def test_an_image_beside_an_unused_repository_default_maps_by_image_and_sets_both():
    """Big Bang 4.1's GitLab Runner values carry Big Bang's `image` and the upstream chart's
    default `repository`; the chart renders `image`. Map by `image`, point both at the org."""
    import yaml
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import bbcommon
    values = {"upstream": {"image": {"registry": "registry1.dso.mil", "image": "gitlab/gitlab-org/gitlab-runner",
                                     "repository": "gitlab-org/gitlab-runner", "tag": "ubi-fips-v19.5.2"}}}
    nodes = [{"path": list(p), "shape": shape, "value": v} for p, shape, v in bbcommon.image_nodes(values)]
    discovered = {"packages": {"gitlab-runner": {
        "key": "gitlabRunner", "values": nodes,
        "rendered": ["registry1.dso.mil/gitlab/gitlab-org/gitlab-runner:ubi-fips-v19.5.2"]}}}
    mapping = {"images": {"gitlab/gitlab-org/gitlab-runner": "gitlab-runner"}}
    files, c = convert.generate(discovered, mapping, FakeTags({"gitlab-runner": ["19.5.2"],
                                                               "gitlab-runner-fips": ["19.5.2"]}), "4.1.0")
    assert not c.unmapped
    std = yaml.safe_load(files["overlays/values-chainguard.yaml"])
    image = std["gitlabRunner"]["values"]["upstream"]["image"]
    assert image["image"] == image["repository"] == "gitlab-runner"
    assert image["registry"].startswith("cgr.dev/") and image["tag"] == "19.5.2"
def test_a_version_pin_behind_iron_bank_is_noted_and_reported_once_the_org_catches_up():
    m = {"images": {"ironbank/sq": {"cg": "sq", "fips": None, "version": "26.10"}}}
    c = convert.Converter(m, FakeTags({"sq": ["26.10.0", "26.10.1"]}))
    std, _ = c.target("ironbank/sq", "26.11.0", "sonarqube")
    assert std.endswith("sq:26.10.1") and not c.unpin
    assert "VERSION PINNED to 26.10 (Iron Bank 26.11.0)" in c.rows[-1][-1]
    c = convert.Converter(m, FakeTags({"sq": ["26.10.1", "26.11.2"]}))
    std, _ = c.target("ironbank/sq", "26.11.0", "sonarqube")
    assert std.endswith("sq:26.10.1")          # still pinned until a person drops it
    assert c.unpin == {("ironbank/sq", "sq", "26.11.0")}


def test_a_version_pin_on_iron_banks_own_line_says_nothing():
    c = convert.Converter({"images": {"ironbank/sq": {"cg": "sq", "fips": None, "version": "26.10"}}},
                          FakeTags({"sq": ["26.10.1"]}))
    c.target("ironbank/sq", "26.10.0", "sonarqube")
    assert "PINNED" not in c.rows[-1][-1] and not c.unpin


def test_a_package_without_post_renderers_is_pinned_through_its_digest_values():
    """Keycloak (mapping no_post_renderers) ran on tags; its charts take image.digest."""
    c = convert.Converter(MAPPING, FakeTags({"keycloak": ["26.8.2"], "keycloak-fips": ["26.8.2"],
                                             "postgres": ["18.7"], "postgres-fips": ["18.7"]},
                                            org="example.com"))
    c.cg = "cgr.dev/example.com"
    std = {"upstream": {"image": {"repository": "cgr.dev/example.com/keycloak", "tag": "26.8.2"}},
           "postgresql": {"image": {"registry": "cgr.dev/example.com", "repository": "postgres", "tag": "18.7"}},
           "bbtests": {"image": "ghcr.io/me/bigbang-base:1"}}
    # The FIPS overlay layers on top and changes only the repositories here.
    fips = {"upstream": {"image": {"repository": "cgr.dev/example.com/keycloak-fips"}},
            "postgresql": {"image": {"repository": "postgres-fips"}}}
    convert.value_digest_pins(std, c)
    convert.value_digest_pins(fips, c, base=std)
    pin = c.tags.pinned
    assert std["upstream"]["image"]["digest"] == pin("keycloak", "26.8.2")
    assert std["postgresql"]["image"]["digest"] == pin("postgres", "18.7")
    assert fips["upstream"]["image"]["digest"] == pin("keycloak-fips", "26.8.2") != std["upstream"]["image"]["digest"]
    assert fips["postgresql"]["image"]["digest"] == pin("postgres-fips", "18.7")
    assert std["bbtests"]["image"] == "ghcr.io/me/bigbang-base:1"   # a plain string: untouched
    # mapping unpinned keeps a tag.
    other = {"image": {"repository": "cgr.dev/example.com/keycloak", "tag": "26.8.2"}}
    convert.value_digest_pins(other, c, skip={"keycloak"})
    assert "digest" not in other["image"]


def test_same_patch_holds_members_with_their_own_version_line_to_the_rails_patch():
    """Gitaly, KAS and Pages are tagged with the GitLab release themselves; GitLab Shell and the
    registry follow gitlab-base's. With same_patch they all run the patch the Rails images
    have (19.5.6), not their own newest (19.5.8: gitaly 19.5.6 beside gitaly-fips 19.5.8)."""
    tags = {"web": ["19.5.5", "19.5.6"], "web-fips": ["19.5.5", "19.5.6"],
            "gitaly": ["19.5.6", "19.5.8"], "gitaly-fips": ["19.5.5", "19.5.6", "19.5.8"],
            "shell": ["19.5.6", "19.5.8"], "shell-fips": ["19.5.6", "19.5.8"]}
    m = {"images": {
        "gitlab/web": {"group": "gl", "strict": True, "cg": "web", "same_patch": "rails"},
        "gitlab/gitaly": {"group": "gl", "strict": True, "cg": "gitaly", "same_patch": "rails"},
        "gitlab/shell": {"group": "gl", "strict": True, "cg": "shell", "same_patch": "rails",
                         "version_from": "gitlab/web"}}}
    ib = {"gitlab/web": "v19.5.5-ubi", "gitlab/gitaly": "v19.5.5-ubi", "gitlab/shell": "v14.58.0-ubi"}
    c = convert.Converter(m, FakeTags(tags), ib_tags=ib)
    for repo, name in (("gitlab/web", "web"), ("gitlab/gitaly", "gitaly"), ("gitlab/shell", "shell")):
        assert c.target(repo, ib[repo], "gitlab") == (f"cgr.dev/example.com/{name}:19.5.6",
                                                      f"cgr.dev/example.com/{name}-fips:19.5.6")


def test_gitlab_components_follow_the_rails_patch_in_the_real_mapping():
    import yaml
    images = yaml.safe_load(open(os.path.join(ROOT, "mapping.yaml"), encoding="utf-8"))["images"]
    cng = "gitlab/gitlab-org/build/cng/"
    for name in ("gitaly", "gitlab-base", "gitlab-container-registry", "gitlab-kas", "gitlab-pages", "gitlab-shell",
                 "gitlab-sidekiq-ee", "gitlab-toolbox-ee", "gitlab-webservice-ee", "gitlab-workhorse-ee"):
        assert images[cng + name].get("same_patch") == "rails", name


def test_images_set_by_overrides_are_in_the_version_report():
    """NeuVector's and Istio's proxy images are set by `overrides`, not a value node the
    generic rules convert: they were missing from docs/version-differences.md."""
    tags = {"nv-ctl": ["5.6.4", "5.6.5"], "nv-ctl-fips": ["5.6.5"], "nv-scan": ["4.1"], "nv-scan-fips": ["4.1"],
            "proxy": ["1.31.4"], "proxy-fips": ["1.31.4"]}
    m = {"images": {"ironbank/nv/controller": "nv-ctl", "ironbank/nv/scanner": "nv-scan",
                    "ironbank/istio/proxyv2": "proxy", "ironbank/istio/pilot": "pilot"},
         "overrides": {
             "neuvector": {"upstream": {"registry": "@registry", "tag": "@tag:nv-ctl:5.6",
                                        "controller": {"image": {"repository": "@repo:nv-ctl"}},
                                        "cve": {"scanner": {"image": {"repository": "@repo:nv-scan",
                                                                      "tag": "@tag:nv-scan:*"}}}}},
             "istiod": {"upstream": {"global": {"hub": "@registry", "proxy": {"image": "@repo:proxy"},
                                                "proxy_init": {"image": "@ref:proxy:ib=ironbank/istio/pilot"},
                                                "tag": "@tag:proxy:ib=ironbank/istio/pilot"}}}}}
    discovered = {"packages": {
        "neuvector": {"key": "packages.neuvector", "rendered": ["registry1.dso.mil/ironbank/nv/controller:5.6.4",
                                                                "registry1.dso.mil/ironbank/nv/scanner:6"],
                      "values": [{"path": ["upstream", "controller", "image"], "shape": "repo",
                                  "value": {"repository": "ironbank/nv/controller"}},
                                 {"path": ["upstream", "cve", "scanner", "image"], "shape": "repo",
                                  "value": {"registry": "", "repository": "ironbank/nv/scanner", "tag": "6"}}]},
        "istiod": {"key": "packages.istiod", "rendered": ["registry1.dso.mil/ironbank/istio/pilot:1.31.3"],
                   "values": [{"path": ["upstream", "global"], "shape": "hub",
                               "value": {"hub": "registry1.dso.mil/ironbank/istio", "tag": "1.31.3"}}]}}}
    m["ignore"] = [{"release": "neuvector", "path": "upstream.controller.image"},
                   {"release": "neuvector", "path": "upstream.cve.scanner.image"}]
    m["hubs"] = {"istiod": {"upstream.image": "ironbank/istio/pilot"}}
    files, c = convert.generate(discovered, m, FakeTags({**tags, "pilot": ["1.31.4"], "pilot-fips": ["1.31.4"]}),
                                "4.1.1")
    md = files["docs/version-differences.md"]
    assert "| `ironbank/nv/controller:5.6.4` | `nv-ctl:5.6.5` | `nv-ctl-fips:5.6.5` | Chainguard | newer patch |" in md
    assert "| `ironbank/nv/scanner:6` | `nv-scan:4.1` | `nv-scan-fips:4.1` | Chainguard | older major |" in md
    # Istio's proxy is never rendered (injected): its Iron Bank tag is istiod's release.
    assert "| `ironbank/istio/proxyv2:1.31.3` | `proxy:1.31.4` | `proxy-fips:1.31.4` |" in md
    assert md.count("ironbank/istio/proxyv2") == 1   # proxy.image and proxy_init.image: one row
    assert "| neuvector | ironbank/nv/controller:5.6.4 | nv-ctl:5.6.5 |" in files["docs/images.md"]


def test_retarget_deploys_another_mapped_image_at_a_value_path():
    """Grafana's chown init container ran Iron Bank's UBI for `chown` alone."""
    m = {"images": {"ironbank/ubi": {"stays": "UBI"}, "ironbank/busybox": "bb",
                    "ironbank/base": {"build": "base"}},
         "retarget": {"grafana": {"upstream.initChownData.image": "ironbank/busybox",
                                  "upstream.curl": "ironbank/base"}}}
    tags = {"bb": ["1.38.0"], "bb-fips": ["1.38.0"]}
    ib = {"ironbank/busybox": "1.38.0", "ironbank/base": "2.1.0"}
    c = convert.Converter(m, FakeTags(tags), ib_tags=ib)
    std, fips = {}, {}
    info = {"key": "packages.grafana", "rendered": [], "values": [
        {"path": ["upstream", "initChownData", "image"], "shape": "repo",
         "value": {"registry": "registry1.dso.mil", "repository": "ironbank/ubi", "tag": "9.8"}},
        {"path": ["upstream", "curl"], "shape": "string", "value": "registry1.dso.mil/ironbank/ubi:9.8"}]}
    c.package("grafana", info, std, fips)
    assert std["upstream"]["initChownData"]["image"] == {"registry": "cgr.dev/example.com", "repository": "bb",
                                                         "tag": "1.38.0"}
    assert fips["upstream"]["initChownData"]["image"]["repository"] == "bb-fips"
    assert any(r[1] == "ironbank/busybox:1.38.0" and "in place of ironbank/ubi:9.8" in r[4] for r in c.rows)
    # A build with no BUILD_REGISTRY stays on Iron Bank: the path keeps Big Bang's own image.
    assert "curl" not in std["upstream"]
    assert ("grafana", "ironbank/ubi:9.8", "Iron Bank", "Iron Bank", "UBI") in c.rows
    assert not any(r[1].startswith("ironbank/base") for r in c.rows)
    c = convert.Converter(m, FakeTags(tags), "ghcr.io/me", ib_tags=ib)
    std, fips = {}, {}
    c.package("grafana", info, std, fips)
    assert std["upstream"]["curl"].startswith("ghcr.io/me/bigbang-base:")


def test_a_build_with_until_is_reported_once_the_org_has_the_image():
    m = {"images": {"ironbank/op": {"build": "minio-operator", "until": "op"}}}
    c = convert.Converter(m, FakeTags({}))
    c.target("ironbank/op", "v7.1.1", "minio-operator")
    assert not c.ready
    c = convert.Converter(m, FakeTags({"op": ["7.1.1"]}))
    c.target("ironbank/op", "v7.1.1", "minio-operator")
    assert c.ready == {("ironbank/op", "op")}


def test_until_needs_stays_or_build():
    import bbcommon
    bbcommon.validate_mapping({"images": {"a": {"build": "x", "until": "y"}, "b": {"stays": "x", "until": "y"}}})
    with pytest.raises(ValueError, match="until: only with stays or build"):
        bbcommon.validate_mapping({"images": {"a": {"cg": "x", "until": "y"}}})


def test_fortify_mysql_wait_init_container_is_renamed_to_the_mysql8_mapping():
    """Big Bang's fortify post-renderer adds a mysql-wait init container with a hardcoded
    registry1 mysql8 image (chart/templates/fortify/_postrenderers.tpl, 4.1.1); no value
    reaches it. A kustomize rename after Big Bang's own post-renderer moves it to whatever
    the mysql8 entry in mapping.yaml resolves to."""
    import yaml
    real = yaml.safe_load(open(os.path.join(ROOT, "mapping.yaml"), encoding="utf-8"))
    mysql8 = "ironbank/opensource/mysql/mysql8"
    assert mysql8 in real["post_renderers"]["packages.fortify"]
    m = real["images"][mysql8]
    m = {"cg": m} if isinstance(m, str) else m
    name, fips = m["cg"], m.get("fips", m["cg"] + "-fips") if "fips" in m else m["cg"] + "-fips"
    fips = fips or name
    discovered = {"packages": {"fortify": {"key": "packages.fortify", "values": [],
                                           "rendered": [f"registry1.dso.mil/{mysql8}:8.4.13"]}}}
    tags = {name: ["8.4.13"], fips: ["8.4.13"]}
    files, c = convert.generate(discovered, {"images": {mysql8: m},
                                             "post_renderers": {"packages.fortify": [mysql8]}}, FakeTags(tags), "4.1.1")
    for f, repo in (("values-chainguard.yaml", name), ("values-chainguard-fips.yaml", fips)):
        images = yaml.safe_load(files[f"overlays/{f}"])["packages"]["fortify"]["postRenderers"][0]["kustomize"]["images"]
        rename = next(i for i in images if i["name"] == f"registry1.dso.mil/{mysql8}")
        assert rename["newName"] == f"cgr.dev/example.com/{repo}" and rename["newTag"] == "8.4.13"
        assert rename["digest"] == c.tags.pinned(repo, "8.4.13")


def test_render_pins_only_what_kustomize_reaches_and_applies_the_cr_patches():
    """Kustomize rewrites only containers/initContainers images (kubectl kustomize 5.8, any
    depth): a custom resource's spec.image or an operator flag keeps the chart's tag, which
    bb-verify used to count as pinned (Prometheus, Alertmanager, ECK, Kiali, ... ran on tags)."""
    images = load_script("bb-images")
    pin = "sha256:" + "a" * 64
    text = """
kind: Prometheus
metadata: {name: p}
spec:
  image: cgr.dev/o/prometheus:3.16.0
  containers:
  - {name: side, image: cgr.dev/o/sidecar:1}
---
kind: Deployment
metadata: {name: op}
spec:
  template:
    spec:
      containers:
      - name: op
        image: cgr.dev/o/operator:1
        args: [--prometheus-config-reloader=cgr.dev/o/reloader:2]
---
kind: Elasticsearch
metadata: {name: es}
spec: {image: "cgr.dev/o/elasticsearch:9.5.6", version: 9.5.6}
---
kind: Mattermost
metadata: {name: mm}
spec: {image: cgr.dev/o/mattermost, version: 12.0.2}
"""
    reach = set()
    found = images.images_in(text, reach)
    assert reach == {"cgr.dev/o/sidecar:1", "cgr.dev/o/operator:1"}
    assert {"cgr.dev/o/prometheus:3.16.0", "cgr.dev/o/reloader:2", "cgr.dev/o/elasticsearch:9.5.6"} <= found
    patches = [
        {"target": {"kind": "Elasticsearch"},
         "patch": json.dumps([{"op": "replace", "path": "/spec/image", "value": f"cgr.dev/o/elasticsearch:9.5.6@{pin}"}])},
        {"target": {"kind": "Mattermost"}, "patch": json.dumps([{"op": "replace", "path": "/spec/version", "value": pin}])},
        {"target": {"kind": "Deployment", "name": "other"},
         "patch": json.dumps([{"op": "add", "path": "/spec/x", "value": 1}])},     # another target: untouched
        {"target": {"kind": "Deployment"}, "patch": "spec: {replicas: 2}"},          # strategic merge: skipped
    ]
    after = images.images_in(images.apply_patches(text, patches))
    assert f"cgr.dev/o/elasticsearch:9.5.6@{pin}" in after
    assert f"cgr.dev/o/mattermost@{pin}" in after          # the operator runs image@version
    assert "cgr.dev/o/mattermost" not in after
    kiali = "kind: Kiali\nspec:\n  deployment: {image_name: cgr.dev/o/kiali, image_version: '%s', image_digest: sha256}\n" % ("b" * 64)
    assert images.images_in(kiali) == {"cgr.dev/o/kiali@sha256:" + "b" * 64}
    alloy = "kind: Alloy\nspec:\n  image: {registry: cgr.dev/o, repository: alloy, tag: 1.21.3, digest: '%s'}\n" % ("c" * 64)
    assert images.images_in(alloy) == {"cgr.dev/o/alloy:1.21.3@sha256:" + "c" * 64}


def test_cr_digests_pin_images_kustomize_cannot_reach():
    """mapping cr_digests: a digest value beside the image, a full reference, or a JSON patch."""
    org = {"prometheus": ["3.16.0"], "prometheus-fips": ["3.16.0"], "alloy": ["1.21.3"], "alloy-fips": ["1.21.3"],
           "elasticsearch": ["9.5.6"], "elasticsearch-fips": ["9.5.6"], "mattermost": ["12.0.2"],
           "kiali": ["2.34.1"], "kiali-fips": ["2.34.1"], "istio-proxy": ["1.31.4"], "istio-proxy-fips": ["1.31.4"],
           "thanos": ["0.42.5"]}
    c = convert.Converter(MAPPING, FakeTags(org))
    c.cg = "cgr.dev/example.com"
    pin = c.tags.pinned
    cg = c.cg
    std = {"upstream": {"prometheus": {"prometheusSpec": {"image": {"registry": cg, "repository": "prometheus",
                                                                     "tag": "3.16.0"}}},
                        "thanosImage": {"registry": cg, "repository": "thanos", "tag": "0.42.5"},
                        "alloy": {"image": {"registry": cg, "repository": "alloy", "tag": "1.21.3"}},
                        "global": {"proxy": {"image": f"{cg}/istio-proxy:1.31.4"}},
                        "cr": {"spec": {"deployment": {"image_name": f"{cg}/kiali", "image_version": "2.34.1"}}}},
           "elasticsearch": {"image": {"repository": f"{cg}/elasticsearch", "tag": "9.5.6"}},
           "image": {"name": f"{cg}/mattermost", "tag": "12.0.2"},
           "missing": {"image": {"repository": f"{cg}/nothere", "tag": "1"}}}
    fips = {"upstream": {"prometheus": {"prometheusSpec": {"image": {"repository": "prometheus-fips"}}},
                         "global": {"proxy": {"image": f"{cg}/istio-proxy-fips:1.31.4"}}},
            "elasticsearch": {"image": {"repository": f"{cg}/elasticsearch-fips"}}}
    entries = {
        "upstream.prometheus.prometheusSpec.image": "sha",
        "upstream.thanosImage": "sha",
        "upstream.alloy.image": "digest",
        "upstream.global.proxy.image": "ref",
        "upstream.cr.spec.deployment": {"patch": "Kiali", "field": "kiali"},
        "elasticsearch.image": {"patch": "Elasticsearch", "field": "image"},
        "image": {"patch": "Mattermost", "field": "version"},
        "missing.image": "digest",
    }
    patches = convert.cr_digest_pins(entries, c, std, fips)
    up = std["upstream"]
    assert up["prometheus"]["prometheusSpec"]["image"]["sha"] == pin("prometheus", "3.16.0").removeprefix("sha256:")
    assert fips["upstream"]["prometheus"]["prometheusSpec"]["image"]["sha"] == \
        pin("prometheus-fips", "3.16.0").removeprefix("sha256:")
    assert up["thanosImage"]["sha"] == pin("thanos", "0.42.5").removeprefix("sha256:")
    assert "thanosImage" not in fips["upstream"]          # same image in FIPS: inherited, not repeated
    assert up["alloy"]["image"]["digest"] == pin("alloy", "1.21.3")
    assert up["global"]["proxy"]["image"] == f"{cg}/istio-proxy:1.31.4@" + pin("istio-proxy", "1.31.4")
    assert fips["upstream"]["global"]["proxy"]["image"] == f"{cg}/istio-proxy-fips:1.31.4@" + pin("istio-proxy-fips", "1.31.4")
    assert "digest" not in std["missing"]["image"]        # the org can't resolve it: stays on its tag
    ops = {p["target"]["kind"]: json.loads(p["patch"]) for p in patches["standard"]}
    assert ops["Elasticsearch"] == [{"op": "replace", "path": "/spec/image",
                                     "value": f"{cg}/elasticsearch:9.5.6@" + pin("elasticsearch", "9.5.6")}]
    assert ops["Mattermost"] == [{"op": "replace", "path": "/spec/version", "value": pin("mattermost", "12.0.2")}]
    assert {o["path"]: o["value"] for o in ops["Kiali"]} == {
        "/spec/deployment/image_version": pin("kiali", "2.34.1").removeprefix("sha256:"),
        "/spec/deployment/image_digest": "sha256", "/spec/deployment/version_label": "2.34.1"}
    # FIPS gets a whole list of its own (Helm replaces lists), with the FIPS image's digest.
    fips_ops = {p["target"]["kind"]: json.loads(p["patch"]) for p in patches["fips"]}
    assert fips_ops["Elasticsearch"][0]["value"] == f"{cg}/elasticsearch-fips:9.5.6@" + pin("elasticsearch-fips", "9.5.6")
    assert fips_ops["Mattermost"] == ops["Mattermost"]   # FIPS uses the same image here: the same patch
    # mapping unpinned wins.
    other = {"image": {"repository": f"{cg}/alloy", "tag": "1.21.3"}}
    convert.cr_digest_pins({"image": "digest"}, c, other, {}, skip={"alloy"})
    assert "digest" not in other["image"]


def test_cr_digests_entries_are_validated():
    bbcommon = load_script("bbcommon")
    good = {"images": {}, "cr_digests": {"packages.x": {"a.image": "sha", "b": {"patch": "K", "field": "image"}}}}
    bbcommon.validate_mapping(good)
    for bad in ({"packages.x": {"a": "hex"}}, {"packages.x": {"a": {"patch": "K", "field": "tag"}}},
                {"x": {"a": "sha"}}, {"packages.x": ["a"]}):
        with pytest.raises(ValueError, match="cr_digests"):
            bbcommon.validate_mapping({"images": {}, "cr_digests": bad})
