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


def test_distinct_images_and_sizes():
    pairs = cve.image_pairs(MAP)
    results = {"registry1.dso.mil/ironbank/x/tempo:2.8.0": {"Critical": 1, "High": 5, "Medium": 0, "Low": 0,
                                                            "size": 412_000_000},
               "registry1.dso.mil/ironbank/big-bang/base:2.1.0": {"Critical": 0, "High": 2, "Medium": 0, "Low": 0,
                                                                  "size": 300_000_000},
               "cgr.dev/example.com/tempo:2.8.1": {"Critical": 0, "High": 0, "Medium": 1, "Low": 0, "size": 98_000_000},
               "ghcr.io/o/bigbang-base:2.1.0": {"Critical": 0, "High": 1, "Medium": 0, "Low": 0}}
    text = cve.report(pairs, results, False, True)
    assert "| Iron Bank | 2 | 1 | 0 | 712 MB |" in text
    assert "| Chainguard | 2 | 2 | 1 | 98 MB |" in text
    assert "| 412 MB / 98 MB |" in text and "| 300 MB / ? |" in text
    assert cve.image_size({"source": {"target": {"imageSize": 5}}}) == 5 and cve.image_size({}) is None


def test_public_bases_from_iron_bank_recipes(monkeypatch):
    assert cve.public_base({"BASE_IMAGE": "redhat/ubi/ubi9-minimal", "BASE_TAG": "9.8"}) == \
        "registry.access.redhat.com/ubi9/ubi-minimal:9.8"
    assert cve.public_base({"BASE_IMAGE": "docker/scratch", "BASE_TAG": "ironbank"}) == "scratch"
    assert cve.public_base({"BASE_IMAGE": "opensource/grafana/grafana", "BASE_TAG": "13.3.0"}) is None
    monkeypatch.setattr(cve.bbbuild, "ironbank_recipe",
                        lambda project, ref: ("c", {"args": {"BASE_IMAGE": "redhat/ubi/ubi9", "BASE_TAG": "9.8"}}, ""))
    assert cve.ironbank_base("registry1.dso.mil/ironbank/opensource/x/y:1.0") == (
        "registry.access.redhat.com/ubi9/ubi:9.8", "registry.access.redhat.com/ubi9/ubi:9.8")
    assert cve.ironbank_base("registry1.dso.mil/gitlab/gitlab-org/x:1")[0] is None


def test_a_base_floor_is_marked_as_one():
    pairs = cve.image_pairs(MAP)
    floor = {"Critical": 0, "High": 3, "Medium": 40, "Low": 9, "size": 120_000_000,
             "base": "registry.access.redhat.com/ubi9/ubi-minimal:9.8"}
    results = {"registry1.dso.mil/ironbank/x/tempo:2.8.0": floor,
               "registry1.dso.mil/ironbank/big-bang/base:2.1.0": "recipe not found (404)",
               "cgr.dev/example.com/tempo:2.8.1": {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "size": 40_000_000}}
    text = cve.report(pairs, results, False, True, bases=True)
    assert "| Iron Bank (public base only: a floor) | 0 | 3 | 40 | 9 |" in text
    assert "≥ 0 / 3 / 40 / 9 (ubi-minimal:9.8)" in text and "1 of 2 images resolved" in text
    assert "error: recipe not found (404)" in text and "| 120 MB / 40 MB |" in text


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


def test_gate_skips_images_built_here(monkeypatch):
    monkeypatch.setenv("BUILD_REGISTRY", "ghcr.io/example")
    monkeypatch.setattr(cve, "new_refs", lambda old, new: ["ghcr.io/example/bigbang-x:1"])
    monkeypatch.setattr(cve.subprocess, "run", lambda *a, **k: type("P", (), {"stdout": "", "returncode": 0})())
    lines, failed = cve.gate("origin/main")
    assert not failed and lines[1].startswith("skipped  ghcr.io/example/bigbang-x:1")


def test_the_highs_in_the_most_chainguard_images_come_first():
    # Nov 2026: one new High in a shared package showed as "1 High" in half the images.
    def high(i, pkg, fix=None):
        return {"vulnerability": {"id": i, "severity": "High", "fix": {"versions": [fix] if fix else []}},
                "artifact": {"name": pkg, "version": "1"}}
    shared = high("CVE-7", "glibc", "2.42-r5")
    a, b = {"matches": [shared, high("CVE-6", "openssl")]}, {"matches": [shared]}
    results = {"cgr.dev/example.com/tempo:2.8.1": {**cve.counts(a), "high": cve.findings(a, "High")},
               "ghcr.io/o/bigbang-base:2.1.0": {**cve.counts(b), "high": cve.findings(b, "High")}}
    section = cve.report(cve.image_pairs(MAP), results, False, False).split("## Most common High")[1]
    assert section.index("| CVE-7 | glibc | 2.42-r5 | 2 |") < section.index("| CVE-6 | openssl | no fix yet | 1 |")
