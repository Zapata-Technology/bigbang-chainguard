"""scripts/security-report.py: pins, advisory matching and the report, from canned data."""
from conftest import load_script

sr = load_script("security-report")

D1, D2, D3 = ("sha256:" + c * 64 for c in "abc")


def test_pinned_refs_read_every_pin_shape():
    overlay = f"""
addons:
  keycloak:
    values:
      image:
        digest: {D1}
        repository: cgr.dev/org/keycloak
        tag: 26.8.2
  alloy:
    values:
      image:
        digest: {D2}
        registry: cgr.dev/org
        repository: grafana-alloy
        tag: v1.22.0
    postRenderers:
    - kustomize:
        images:
        - digest: {D3}
          name: ghcr.io/owner/bigbang-base
"""
    flux = f"images:\n- digest: {D1}\n  name: ghcr.io/fluxcd/helm-controller\n  newName: cgr.dev/org/flux-helm-controller\n"
    manifest = f"spec:\n  containers:\n  - image: cgr.dev/org/seaweedfs:v4.55@{D2}\n"
    assert sr.pinned_refs([overlay, flux, manifest]) == sorted([
        f"cgr.dev/org/keycloak@{D1}", f"cgr.dev/org/grafana-alloy@{D2}", f"ghcr.io/owner/bigbang-base@{D3}",
        f"cgr.dev/org/flux-helm-controller@{D1}", f"cgr.dev/org/seaweedfs@{D2}"])


def test_apk_versions_order_like_apk():
    order = ["1.12.1_rc1-r0", "1.12.1-r9", "1.12.1-r10", "1.12.2-r0", "1.13.0-r0", "20261020-r0"]
    assert sorted(order, key=sr.apk_key) == order


FIXES = {"prometheus-node-exporter": {"CVE-2026-1": "0", "CVE-2026-2": "1.12.1-r5", "CVE-2026-3": "1.12.1-r12"}}
APK = {"prometheus-node-exporter": "1.12.1-r10", "glibc": "2.44-r23"}


def test_advisory_says_not_affected_fixed_here_fixed_newer_or_none():
    assert sr.advisory(["CVE-2026-1"], APK, FIXES)[0] == "not-affected"
    assert sr.advisory(["CVE-2026-2"], APK, FIXES)[0] == "fixed-here"
    assert sr.advisory(["CVE-2026-3"], APK, FIXES) == (
        "fixed-newer", "prometheus-node-exporter 1.12.1-r10: fixed in 1.12.1-r12 (a newer build)")
    assert sr.advisory(["GHSA-x", "CVE-2026-1"], APK, FIXES)[0] == "not-affected"   # an alias matches
    assert sr.advisory(["CVE-2026-4"], APK, FIXES) == ("none", "")
    assert sr.advisory(["CVE-2026-1"], {}, FIXES) == ("n/a", "")                    # not an apk image


def test_secfixes_merges_databases_chainguard_first():
    cg = {"packages": [{"pkg": {"name": "a", "secfixes": {"0": ["CVE-1"], "1.0-r1": ["CVE-2 GHSA-x"]}}}]}
    wolfi = {"packages": [{"pkg": {"name": "a", "secfixes": {"2.0-r0": ["CVE-1"]}}}]}
    assert sr.secfixes([cg, wolfi]) == {"a": {"CVE-1": "0", "CVE-2": "1.0-r1", "GHSA-x": "1.0-r1"}}


def nv_row(**kw):
    row = {"image": "cgr.dev/org/prometheus-node-exporter", "digest": D1, "tag": "", "origin": "chainguard",
           "scan_status": "finished", "base_os": "chainguard", "packages": 5, "containers": 1, "deployed": True,
           "counts": {"Critical": 1, "High": 1, "Medium": 0, "Low": 0}, "fixable": 2,
           "vulnerabilities": [
               {"name": "CVE-2026-1", "severity": "Critical", "package_name": "go:golang.org/x/net",
                "package_version": "0.57.0", "fixed_version": ">=0.60.0", "link": ""},
               {"name": "CVE-2026-3", "severity": "High", "package_name": "go:golang.org/x/net",
                "package_version": "0.57.0", "fixed_version": ">=0.60.0", "link": ""}]}
    row.update(kw)
    return row


def test_assemble_merges_scanners_and_applies_advisories():
    scans = {sr.scan_key(f"cgr.dev/org/prometheus-node-exporter@{D1}"): {
        "apk": APK, "signed": True, "sbom": True,
        "grype": [], "trivy": [{"id": "CVE-2026-3", "aliases": ["CVE-2026-3"], "severity": "High",
                                "package": "golang.org/x/net", "installed": "v0.57.0", "fixed_in": "0.60.0"}]}}
    images, cves = sr.assemble([("standard", {"images": [nv_row()]})], scans, FIXES, {"CVE-2026-3"},
                               {"CVE-2026-1": (0.01, 0.5)}, tags={D1: "1.12.1"})
    (img,) = images
    assert (img["tag"], img["critical"], img["open_critical"], img["open_high"], img["signed"], img["kev"]) == \
        ("1.12.1", 1, 0, 1, True, 1)
    by = {c["cve"]: c for c in cves}
    assert len(cves) == 2                                    # NeuVector's and Trivy's x/net finding is one row
    assert (by["CVE-2026-1"]["chainguard_advisory"], by["CVE-2026-1"]["open"], by["CVE-2026-1"]["epss"]) == \
        ("not-affected", False, 0.01)
    assert (by["CVE-2026-3"]["neuvector"], by["CVE-2026-3"]["trivy"], by["CVE-2026-3"]["grype"]) == ("yes", "yes", "no")
    assert (by["CVE-2026-3"]["chainguard_advisory"], by["CVE-2026-3"]["open"], by["CVE-2026-3"]["kev"]) == \
        ("fixed-newer", True, True)
    html = sr.report_html(images, cves, {"bigbang": "4.1.1", "scanned": "2026-11-25", "cluster": "RKE2",
                                         "neuvector_db_version": "4.330", "neuvector_db_built": "x",
                                         "grype_db_built": "y", "run": ""})
    assert "CVE-2026-3" in html and "prometheus-node-exporter 1.12.1" in html


def test_rke2_images_get_no_chainguard_advisory():
    row = nv_row(image="docker.io/rancher/hardened-coredns:v1.14.7", digest="", origin="other")
    images, cves = sr.assemble([("standard", {"images": [row]})], {}, FIXES, set(), {})
    assert images[0]["tag"] == "v1.14.7" and images[0]["open_critical"] == 1
    assert {c["chainguard_advisory"] for c in cves} == {"n/a"}


def test_trivy_is_retried_while_its_cache_is_locked(monkeypatch):
    calls = []

    def fake_run(argv, timeout=3600):
        calls.append(argv[0])
        if argv[0] == "trivy" and calls.count("trivy") < 3:
            return 1, "", "FATAL unable to initialize fs cache: cache may be in use by another process: timeout"
        if argv[0] == "trivy":
            return 0, '{"Results": [{"Class": "os-pkgs", "Packages": [{"Name": "glibc", "Version": "2.44-r23"}]}]}', ""
        return 0, '{"matches": []}', ""

    monkeypatch.setattr(sr, "run", fake_run)
    monkeypatch.setattr(sr.time, "sleep", lambda _: None)
    out = sr.scan_one("cgr.dev/org/x@" + D1, "other", {})
    assert calls.count("trivy") == 3 and out["apk"] == {"glibc": "2.44-r23"} and "trivy_error" not in out
