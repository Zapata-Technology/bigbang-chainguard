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
