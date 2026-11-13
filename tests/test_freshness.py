"""bb-age.py (stale org images) and bb-verify.py's digest-pin coverage."""
import datetime

from conftest import load_script

age = load_script("bb-age")
verify = load_script("bb-verify")
images = load_script("bb-images")

P = "cgr.dev/example.com/"
D = "@sha256:" + "a" * 64

MAP = """Converted images are in `cgr.dev/example.com`.

| Package | Iron Bank image | Standard | FIPS | Notes |
|---|---|---|---|---|
| vault | ironbank/opensource/kubernetes/kubectl:v1.30.11 | kubectl:1.37.2-dev | kubectl-fips:1.37.2-dev | |
| grafana | ironbank/big-bang/grafana-plugins:13.2.5 | ghcr.io/o/grafana-plugins:13.2.5 | ghcr.io/o/grafana-plugins-fips:13.2.5 | |
| x | ironbank/x/anchore:1 | Iron Bank | Iron Bank | vendor |
"""


def test_org_refs_are_the_org_images_in_both_variants():
    assert age.org_refs(MAP, "example.com") == [("kubectl", "1.37.2-dev"), ("kubectl-fips", "1.37.2-dev")]


def test_age_and_staleness():
    now = datetime.datetime(2026, 10, 30, tzinfo=datetime.timezone.utc)
    assert age.age_days("2026-10-28T00:00:00Z", now) == 2
    assert age.age_days("2026-08-01T00:00:00+00:00", now) == 90
    assert age.age_days(None, now) is None and age.age_days("garbage", now) is None
    ages = {("a", "1"): 2, ("b", "1"): 90, ("c", "1"): None, ("d", "1"): 31}
    assert age.stale(ages, 30) == [("c", "1", None), ("b", "1", 90), ("d", "1", 31)]


def test_age_looks_up_every_image_in_parallel_and_reports_the_stale_one(tmp_path, monkeypatch, capsys):
    built = {"kubectl": "2026-01-01T00:00:00Z", "kubectl-fips": datetime.datetime.now(datetime.timezone.utc).isoformat()}

    class Tags:
        def __init__(self, org):
            pass

        def created(self, repo, tag):
            return built[repo]
    (tmp_path / "images.md").write_text(MAP)
    monkeypatch.setattr(age, "OrgTags", Tags)
    monkeypatch.setattr(age.sys, "argv", ["bb-age.py", "--org", "example.com", "--images", str(tmp_path / "images.md")])
    assert age.main() == 1
    out = capsys.readouterr().out
    assert "2 org images" in out and "STALE kubectl:1.37.2-dev" in out and "STALE kubectl-fips" not in out


def test_digest_gaps_find_org_images_left_on_tags():
    packages = {
        "loki": {"key": "loki", "rendered": [P + "loki:3.6" + D, P + "nginx:1.31.8"]},
        "keycloak": {"key": "keycloak", "rendered": [P + "keycloak:26.8.1"]},
        "argocd": {"key": "argocd", "rendered": [P + "redis:8.2", P + "redis:7.4"]},
        "grafana": {"key": "grafana", "rendered": ["ghcr.io/o/grafana-plugins:13.2.5", "registry1.dso.mil/x:1"]},
    }
    assert verify.digest_gaps(packages, P, {"keycloak"}) == [("loki", P + "nginx:1.31.8")]
    assert verify.digest_gaps(packages, P, {"keycloak"}, {"loki": {"nginx": "why"}}) == []
    fips = {"loki": {"key": "loki", "rendered": [P + "nginx-fips:1.31.8"]}}
    assert verify.digest_gaps(fips, P, set(), {"loki": {"nginx": "why"}}) == []


ARGOCD = """| Package | Iron Bank image | Standard | FIPS | Notes |
|---|---|---|---|---|
| argocd | ironbank/bitnami/analytics/redis-exporter:v1.86.0 | prometheus-redis-exporter-iamguarded:1.97.0 | prometheus-redis-exporter-iamguarded-fips:1.97.0 | MINOR AHEAD |
| argocd | ironbank/bitnami/analytics/redis-exporter:v1.96.0 | prometheus-redis-exporter-iamguarded:1.96.0 | prometheus-redis-exporter-iamguarded-fips:1.96.0 |  |
| argocd | ironbank/big-bang/base:2.1.0 | ghcr.io/o/bigbang-base:2.1.0 | ghcr.io/o/bigbang-base-fips:2.1.0 | built |
| argocd | ironbank/opensource/dexidp/dex:v2.46.1 | dex:2.46.1 | dex-fips:2.46.1 |  |
| harbor | ironbank/bitnami/analytics/redis-exporter:v1.93.0 | prometheus-redis-exporter-iamguarded:1.97.0 | prometheus-redis-exporter-iamguarded-fips:1.97.0 |  |
| x | ironbank/x/anchore:1 | Iron Bank | Iron Bank | vendor |
"""


def test_two_tag_repos_are_what_bb_convert_set_at_two_tags_in_one_release():
    assert verify.two_tag_repos(ARGOCD, P) == {
        "argocd": {P + "prometheus-redis-exporter-iamguarded"}, "harbor": set()}   # x: all Iron Bank
    assert verify.two_tag_repos(ARGOCD, P, fips=True)["argocd"] == {P + "prometheus-redis-exporter-iamguarded-fips"}


def test_bb_convert_s_own_two_tag_skip_is_a_note_and_anything_else_still_fails():
    # Argo CD renders only redis-bb's exporter (redis-ha is off): one tag rendered, no digest.
    packages = {"argocd": {"key": "argocd", "rendered": [P + "prometheus-redis-exporter-iamguarded:1.96.0",
                                                         P + "dex:2.46.1"]},
                "harbor": {"key": "harbor", "rendered": [P + "prometheus-redis-exporter-iamguarded:1.97.0"]}}
    gaps = verify.digest_gaps(packages, P, set())
    fail, notes = verify.split_deliberate(gaps, verify.two_tag_repos(ARGOCD, P))
    assert notes == [("argocd", P + "prometheus-redis-exporter-iamguarded:1.96.0")]
    # dex in argocd and the exporter in harbor were set at one tag: bb-convert should have pinned them.
    assert fail == [("argocd", P + "dex:2.46.1"), ("harbor", P + "prometheus-redis-exporter-iamguarded:1.97.0")]


def test_flux_images_show_their_digest_pins():
    k = {"images": [{"name": "ghcr.io/fluxcd/helm-controller", "newName": P + "flux-helm-controller",
                     "newTag": "1.4.3", "digest": "sha256:" + "b" * 64},
                    {"name": "ghcr.io/fluxcd/kustomize-controller", "newName": P + "flux-kustomize-controller",
                     "newTag": "1.7.1"}]}
    assert images.kustomization_images(k) == [P + "flux-helm-controller@sha256:" + "b" * 64,
                                              P + "flux-kustomize-controller:1.7.1"]
