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


def test_digest_gaps_find_org_images_left_on_tags():
    packages = {
        "loki": {"key": "loki", "rendered": [P + "loki:3.6" + D, P + "nginx:1.31.8"]},
        "keycloak": {"key": "keycloak", "rendered": [P + "keycloak:26.8.1"]},
        "argocd": {"key": "argocd", "rendered": [P + "redis:8.2", P + "redis:7.4"]},
        "grafana": {"key": "grafana", "rendered": ["ghcr.io/o/grafana-plugins:13.2.5", "registry1.dso.mil/x:1"]},
    }
    assert verify.digest_gaps(packages, P, {"keycloak"}) == [("loki", P + "nginx:1.31.8")]


def test_flux_images_show_their_digest_pins():
    k = {"images": [{"name": "ghcr.io/fluxcd/helm-controller", "newName": P + "flux-helm-controller",
                     "newTag": "1.4.3", "digest": "sha256:" + "b" * 64},
                    {"name": "ghcr.io/fluxcd/kustomize-controller", "newName": P + "flux-kustomize-controller",
                     "newTag": "1.7.1"}]}
    assert images.kustomization_images(k) == [P + "flux-helm-controller@sha256:" + "b" * 64,
                                              P + "flux-kustomize-controller:1.7.1"]
