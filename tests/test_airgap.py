"""bb-airgap.py: which git repositories and images a disconnected site needs."""
from conftest import load_script

airgap = load_script("bb-airgap")

MAP = """Standard and FIPS names without a registry are in `cgr.dev/example.com`.

| Package | Iron Bank image | Standard | FIPS | Notes |
|---|---|---|---|---|
| tempo | ironbank/x/tempo:2.8.0 | tempo:2.8.1 | tempo-fips:2.8.1 |  |
| tempo | ironbank/big-bang/base:2.1.0 | ghcr.io/o/bigbang-base:2.1.0 | ghcr.io/o/bigbang-base-fips:2.1.0 | built |
| kiali | ironbank/x/kiali:2.0 | kiali:2.0 | kiali-fips:2.0 |  |
| tempo | ironbank/vendor/thing:1 | Iron Bank | Iron Bank | vendor |
"""


def test_image_list_for_enabled_packages_only():
    assert airgap.image_list(MAP, {"tempo"}, False, ["cgr.dev/example.com/flux-source-controller:1.10.1"]) == [
        ("cgr.dev/example.com/flux-source-controller:1.10.1", "flux-source-controller:1.10.1"),
        ("cgr.dev/example.com/tempo:2.8.1", "tempo:2.8.1"),
        ("ghcr.io/o/bigbang-base:2.1.0", "bigbang-base:2.1.0")]
    assert ("cgr.dev/example.com/tempo-fips:2.8.1", "tempo-fips:2.8.1") in airgap.image_list(MAP, {"tempo"}, True, [])


def test_package_repos_follow_enabled_keys():
    defaults = {"tempo": {"git": {"repo": "https://repo1.dso.mil/big-bang/product/packages/tempo.git", "tag": "2.1.0-bb.0"}},
                "kiali": {"git": {"repo": "https://repo1.dso.mil/big-bang/product/packages/kiali.git", "tag": "1"}},
                "addons": {"velero": {"git": {"repo": "https://repo1.dso.mil/big-bang/product/packages/velero.git",
                                              "tag": "9.0.0-bb.1"}}}}
    assert airgap.package_repos(defaults, ["tempo", "addons.velero"]) == [
        ("https://repo1.dso.mil/big-bang/product/packages/tempo.git", "2.1.0-bb.0"),
        ("https://repo1.dso.mil/big-bang/product/packages/velero.git", "9.0.0-bb.1")]


DIGEST = "sha256:" + "a" * 64


def test_sources_are_pinned_by_the_overlays_digests():
    # The overlays pull by digest: a copy made by tag after a Chainguard rebuild wouldn't match.
    pins = {"cgr.dev/example.com/tempo:2.8.1": DIGEST}
    extra = ["cgr.dev/example.com/flux-source-controller:1.10.1@sha256:" + "b" * 64]
    got = airgap.image_list(MAP, {"tempo"}, False, extra, pins)
    assert ("cgr.dev/example.com/tempo@" + DIGEST, "tempo:2.8.1") in got
    assert ("cgr.dev/example.com/flux-source-controller@sha256:" + "b" * 64, "flux-source-controller:1.10.1") in got
    assert ("ghcr.io/o/bigbang-base:2.1.0", "bigbang-base:2.1.0") in got    # nothing pins it: by tag
    # A pin known by name only (the post-renderer matches names) still applies, when unique.
    assert airgap.digest_of("cgr.dev/example.com/tempo:9", pins) == DIGEST
    assert airgap.digest_of("cgr.dev/example.com/tempo:9", {**pins, "cgr.dev/example.com/tempo:8": "x"}) is None


def test_images_txt_marks_the_installer_and_iron_bank_images():
    text = airgap.images_txt([("cgr.dev/o/tempo@" + DIGEST, "tempo:2.8.1")], ["ghcr.io/o/bigbang-installer:4.1.1"],
                             airgap.iron_bank_refs(MAP, {"tempo"}))
    lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
    assert lines == ["cgr.dev/o/tempo@" + DIGEST + " tempo:2.8.1", "ghcr.io/o/bigbang-installer:4.1.1 bigbang-installer:4.1.1"]
    assert "# iron-bank registry1.dso.mil/ironbank/vendor/thing:1" in text.splitlines()
    assert "# installer:" in text


def test_installer_refs_follow_the_mode(tmp_path):
    d = tmp_path / "builds" / "installer"
    d.mkdir(parents=True)
    (d / "build.yaml").write_text("image:\n  standard: {name: bigbang-installer, tag: '1'}\n"
                                  "  fips: {name: bigbang-installer-fips, tag: '1'}\n")
    assert airgap.installer_refs(str(tmp_path), "ghcr.io/o", False) == ["ghcr.io/o/bigbang-installer:1"]
    assert airgap.installer_refs(str(tmp_path), "ghcr.io/o", True) == ["ghcr.io/o/bigbang-installer-fips:1"]
    assert airgap.installer_refs(str(tmp_path), "", False) == []


def test_flux_refs_keep_tag_and_digest(tmp_path):
    (tmp_path / "kustomization.yaml").write_text(
        f"images:\n- {{name: ghcr.io/fluxcd/helm-controller, newName: cgr.dev/o/flux-helm-controller, "
        f"newTag: 1.7.1, digest: '{DIGEST}'}}\n")
    assert airgap.flux_refs(str(tmp_path)) == [f"cgr.dev/o/flux-helm-controller:1.7.1@{DIGEST}"]


def test_the_bundle_gets_a_sha256_manifest(tmp_path):
    import hashlib
    (tmp_path / "git" / "x").mkdir(parents=True)
    (tmp_path / "images.txt").write_bytes(b"a b\n")
    (tmp_path / "git" / "x" / "HEAD").write_bytes(b"ref\n")
    (tmp_path / "SHA256SUMS").write_bytes(b"old")
    head, images_txt = hashlib.sha256(b"ref\n").hexdigest(), hashlib.sha256(b"a b\n").hexdigest()
    assert airgap.sha256_manifest(str(tmp_path)) == [f"{head}  git/x/HEAD", f"{images_txt}  images.txt"]
