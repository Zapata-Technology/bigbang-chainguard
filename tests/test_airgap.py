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
