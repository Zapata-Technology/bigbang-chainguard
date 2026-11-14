"""bb-provenance.py: the SLSA v1 predicate the build job attests to each pushed image."""
from conftest import load_script

prov = load_script("bb-provenance")

ENV = {"GITHUB_REPOSITORY": "o/r", "GITHUB_SHA": "abc", "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ID": "7"}
D = "0" * 64
SPEC = {"ironbank": {"chainsaw": f"ghcr.io/kyverno/chainsaw@sha256:{'1' * 64}"},   # replaced, not an input
        "image": {"standard": {"name": "bigbang-x", "base": f"cgr.dev/org/x:1@sha256:{D}"},
                  "fips": {"name": "bigbang-x-fips", "base": f"cgr.dev/org/x-fips:1@sha256:{'2' * 64}"}},
        "plugins": [{"url": "https://e.example/p.zip", "sha256": "3" * 64,
                     "rebuild": {"source": "https://e.example/src.tgz", "sha256": "4" * 64}}]}


def test_predicate_lists_the_run_commit_and_the_variants_pinned_inputs():
    p = prov.predicate("x", f"ghcr.io/o/bigbang-x@sha256:{'9' * 64}", SPEC, ENV)
    deps = p["buildDefinition"]["resolvedDependencies"]
    assert deps[0] == {"uri": "git+https://github.com/o/r@refs/heads/main", "digest": {"gitCommit": "abc"}}
    uris = [d["uri"] for d in deps[1:]]
    assert uris == ["oci://cgr.dev/org/x:1", "https://e.example/p.zip", "https://e.example/src.tgz"]
    assert p["buildDefinition"]["externalParameters"]["variant"] == "standard"
    assert p["runDetails"]["metadata"]["invocationId"].endswith("/o/r/actions/runs/7/attempts/1")


def test_fips_image_takes_the_fips_inputs():
    p = prov.predicate("x", f"ghcr.io/o/bigbang-x-fips@sha256:{'9' * 64}", SPEC, ENV)
    uris = [d["uri"] for d in p["buildDefinition"]["resolvedDependencies"]]
    assert "oci://cgr.dev/org/x-fips:1" in uris and "oci://cgr.dev/org/x:1" not in uris
    assert p["buildDefinition"]["externalParameters"]["variant"] == "fips"
