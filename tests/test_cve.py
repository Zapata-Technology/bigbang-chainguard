"""bb-cve.py: pairs from the image map, severity counts and the report."""
import json

from conftest import load_script

cve = load_script("bb-cve")

MAP = """Standard and FIPS names without a registry are in `cgr.dev/example.com`.

| Package | Iron Bank image | Standard | FIPS | Notes |
|---|---|---|---|---|
| a | ironbank/x/tempo:2.8.0 | tempo:2.8.1 | tempo-fips:2.8.1 |  |
| b | ironbank/x/tempo:2.8.0 | tempo:2.8.1 | tempo-fips:2.8.1 |  |
| c | ironbank/big-bang/base:2.1.0 | ghcr.io/o/bigbang-base:2.1.0 | ghcr.io/o/bigbang-base-fips:2.1.0 | built |
| d | ironbank/anchore/enterprise:6.1.1 | Iron Bank | Iron Bank | vendor |
"""


def test_pairs_are_unique_and_qualified():
    assert cve.image_pairs(MAP) == [
        ("registry1.dso.mil/ironbank/anchore/enterprise:6.1.1", None),
        ("registry1.dso.mil/ironbank/big-bang/base:2.1.0", "ghcr.io/o/bigbang-base:2.1.0"),
        ("registry1.dso.mil/ironbank/x/tempo:2.8.0", "cgr.dev/example.com/tempo:2.8.1")]
    assert cve.image_pairs(MAP, fips=True)[2][1] == "cgr.dev/example.com/tempo-fips:2.8.1"


def test_counts_unique_vulnerabilities_by_severity():
    m = lambda i, sev, pkg="p": {"vulnerability": {"id": i, "severity": sev}, "artifact": {"name": pkg}}  # noqa: E731
    out = cve.counts({"matches": [m("CVE-1", "High"), m("CVE-1", "High"), m("CVE-2", "Critical"),
                                  m("CVE-3", "Negligible"), m("CVE-1", "High", "q")]})
    assert out == {"Critical": 1, "High": 2, "Medium": 0, "Low": 0}


def test_criticals_list_what_to_fix():
    m = {"vulnerability": {"id": "CVE-9", "severity": "Critical", "fix": {"versions": ["1.2.4"]}},
         "artifact": {"name": "golang.org/x/net", "version": "1.2.3"}}
    found = {"matches": [m, m, {"vulnerability": {"id": "CVE-8", "severity": "High"}, "artifact": {}}]}
    assert cve.criticals(found) == [("CVE-9", "golang.org/x/net", "1.2.3", "1.2.4")]
    pairs = cve.image_pairs(MAP)
    text = cve.report(pairs, {"cgr.dev/example.com/tempo:2.8.1": {**cve.counts(found), "critical": cve.criticals(found)}},
                      False, False)
    assert "| cgr.dev/example.com/tempo:2.8.1 | CVE-9 | golang.org/x/net | 1.2.3 | 1.2.4 |" in text


def test_report_totals_and_rows():
    pairs = cve.image_pairs(MAP)
    results = {"registry1.dso.mil/ironbank/x/tempo:2.8.0": {"Critical": 1, "High": 5, "Medium": 9, "Low": 2},
               "cgr.dev/example.com/tempo:2.8.1": {"Critical": 0, "High": 0, "Medium": 1, "Low": 0},
               "ghcr.io/o/bigbang-base:2.1.0": "unauthorized"}
    text = cve.report(pairs, results, False, True)
    assert "| Iron Bank | 1 | 5 | 9 | 2 |" in text and "| Chainguard | 0 | 0 | 1 | 0 |" in text
    assert "| ironbank/x/tempo:2.8.0 | 1 / 5 / 9 / 2 | cgr.dev/example.com/tempo:2.8.1 | 0 / 0 / 1 / 0 |" in text
    assert "error: unauthorized" in text and "## Still on Iron Bank" in text and "anchore/enterprise" in text
    assert "| Iron Bank | not scanned |" in cve.report(pairs, results, False, False)


def test_new_refs_are_only_the_images_a_change_adds():
    newer = MAP.replace("tempo:2.8.1 | tempo-fips:2.8.1", "tempo:2.8.2 | tempo-fips:2.8.2")
    assert cve.new_refs(MAP, newer) == ["cgr.dev/example.com/tempo-fips:2.8.2", "cgr.dev/example.com/tempo:2.8.2"]
    assert cve.new_refs(MAP, MAP) == []


def test_gate_tells_a_failed_scan_from_a_finding():
    crit = {"matches": [{"vulnerability": {"id": "CVE-1", "severity": "Critical", "fix": {"versions": ["1.2"]}},
                         "artifact": {"name": "x", "version": "1.1"}}]}
    assert cve.gate_result(0, json.dumps({"matches": []}), "") == ("ok", [])
    assert cve.gate_result(0, json.dumps(crit), "") == ("FIXABLE CRITICAL", ["CVE-1 x 1.1 (fixed in 1.2)"])
    assert cve.gate_result(1, "", "failed to pull: UNAUTHORIZED")[0] == "SCAN FAILED"
