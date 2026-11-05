"""bb-org-repos.py: which repositories to add, and what a refused sync means."""
from conftest import load_script

org = load_script("bb-org-repos")

MISSING = """Org repositories needed but missing:
  argocd-fips
  kubectl
Optional (their images stay on Iron Bank until added):
  neuvector-registry-adapter
Something else:
  not-a-name
"""


def test_names_come_from_bb_convert_missing():
    assert org.missing_from(MISSING) == ["argocd-fips", "kubectl"]
    assert org.missing_from(MISSING, optional=True) == ["argocd-fips", "kubectl", "neuvector-registry-adapter"]
    assert org.missing_from("Org repositories needed but missing:\n  (none)\n") == []


def test_a_refused_sync_is_an_entitlement_not_an_error():
    refused = ("Error: [PermissionDenied] source repo either doesn't exist or organization does not have an "
               "active entitlement")
    assert org.outcome(1, refused) == "not entitled"
    assert org.outcome(0, "") == "ok"
    assert org.outcome(1, "Error: connection reset\n") == "Error: connection reset"


SUMMARY = {
    "plan": "PER_IMAGE", "swapQuota": 2,
    "quota": {"BASE": {"current": 2, "max": 2}, "FIPS": {"current": 1, "max": 3}},
    "activeImages": [
        {"name": "jdk", "catalogTier": "BASE"},
        {"name": "grafana-fips", "catalogTier": "FIPS"},
        {"name": "kubectl", "catalogTier": "BASE"},
    ],
}


def test_quota_names_full_tiers_unused_images_and_what_does_not_fit():
    lines, blocked = org.quota_report(SUMMARY, {"grafana-fips", "kubectl"},
                                      {"go": "BASE", "go-fips": "FIPS", "a-fips": "FIPS", "b-fips": "FIPS"})
    text = "\n".join(lines)
    assert "BASE         2/2  FULL" in text
    assert "FIPS         1/3  2 free" in text
    assert "BASE: jdk" in text and "grafana-fips" not in text.split("Counted")[1].split("Needed")[0]
    # Two FIPS slots: a-fips and b-fips fit (alphabetical), go-fips doesn't; BASE is full.
    assert blocked == ["go", "go-fips"]


def test_quota_with_nothing_needed_blocks_nothing():
    lines, blocked = org.quota_report(SUMMARY, set(), {})
    assert blocked == [] and "(none)" in lines[-1]


CATALOG = {
    "plan": "CATALOG", "activeTiers": ["APPLICATION", "BASE", "FIPS"], "maxTotalImages": -1, "swapQuota": -1,
    # Left over from the per-image plan: over max, and no longer a limit.
    "quota": {"AI": {}, "APPLICATION": {"current": 10, "max": 6}, "BASE": {"current": 2, "max": 2}},
    "activeImages": [{"name": "jdk", "catalogTier": "BASE"}],
}


def test_a_catalog_plan_is_unlimited_in_its_active_tiers():
    lines, blocked = org.quota_report(CATALOG, set(), {"go": "BASE", "go-fips": "FIPS", "model": "AI"})
    text = "\n".join(lines)
    assert "FULL" not in text and "swap quota" not in text and "Counted" not in text
    assert "BASE         2/-  unlimited" in text and "FIPS         0/-  unlimited" in text
    assert "AI           not entitled" in text
    assert blocked == ["model"]


def test_an_empty_tier_quota_is_no_limit():
    lines, blocked = org.quota_report({"plan": "PER_IMAGE", "quota": {"AI": {}}}, set(), {"m": "AI"})
    assert "AI           0/-  unlimited" in "\n".join(lines) and blocked == []


def test_used_names_cover_twins_tokens_and_build_bases():
    mapping = {"images": {"a/b": {"cg": "argocd"}, "c/d": {"cg": "kyvernopre", "fips": None},
                          "e/f": {"stays": "ironbank", "until": "redis"}},
               "overrides": {"x": {"image": "@repo:nginx"}}}
    used = org.used_by_bigbang(mapping, ["FROM cgr.dev/example.com/kubectl:1.35-dev"])
    assert {"argocd", "argocd-fips", "kyvernopre", "redis", "nginx", "nginx-fips", "kubectl", "go"} <= used
    assert "kyvernopre-fips" not in used
