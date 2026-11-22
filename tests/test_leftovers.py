"""bb-leftovers.py: which `until` images Chainguard publishes now."""
from conftest import load_script

leftovers = load_script("bb-leftovers")

MAPPING = {"images": {
    "ironbank/a": {"stays": "no image", "until": "alpha"},
    "ironbank/b": {"stays": "no image", "until": "beta"},
    "ironbank/c": {"stays": "vendor only"},
    "ironbank/d": "delta",
}}


def run(monkeypatch, capsys, org_has=(), catalog_has=()):
    monkeypatch.setattr(leftovers, "load_mapping", lambda org, path: MAPPING)
    monkeypatch.setattr(leftovers, "in_org", lambda org, n: n in org_has)
    monkeypatch.setattr(leftovers, "in_catalog", lambda n: n in catalog_has)
    monkeypatch.setattr("sys.argv", ["bb-leftovers.py", "--org", "example.com"])
    assert leftovers.main() == 0
    return {line.split()[0]: line for line in capsys.readouterr().out.splitlines()[1:]}


def test_only_until_images_are_listed_with_fips(monkeypatch, capsys):
    out = run(monkeypatch, capsys)
    assert set(out) == {"alpha", "alpha-fips", "beta", "beta-fips"}
    assert all("not published yet" in line for line in out.values())


def test_published_and_in_org(monkeypatch, capsys):
    out = run(monkeypatch, capsys, org_has={"beta"}, catalog_has={"alpha", "beta"})
    assert "PUBLISHED: add it to the org" in out["alpha"]
    assert "in the org" in out["beta"]
    assert "not published yet" in out["alpha-fips"]


def test_a_chainctl_failure_is_reported_not_raised(monkeypatch, capsys):
    def boom(n):
        raise RuntimeError("not logged in")
    monkeypatch.setattr(leftovers, "in_catalog", boom)
    monkeypatch.setattr(leftovers, "load_mapping", lambda org, path: MAPPING)
    monkeypatch.setattr(leftovers, "in_org", lambda org, n: False)
    monkeypatch.setattr("sys.argv", ["bb-leftovers.py", "--org", "example.com"])
    assert leftovers.main() == 0
    assert "unknown (not logged in)" in capsys.readouterr().out


def test_a_build_with_until_is_listed_as_built_here(monkeypatch, capsys):
    """A build Chainguard publishes too (gitaly-init-cgroups) is reported like a leftover."""
    mapping = {"images": {**MAPPING["images"], "ironbank/e": {"build": "e", "until": "epsilon"}}}
    monkeypatch.setattr(leftovers, "load_mapping", lambda org, path: mapping)
    monkeypatch.setattr(leftovers, "in_org", lambda org, n: False)
    monkeypatch.setattr(leftovers, "in_catalog", lambda n: n == "epsilon")
    monkeypatch.setattr("sys.argv", ["bb-leftovers.py", "--org", "example.com"])
    assert leftovers.main() == 0
    out = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()[1:]}
    assert "PUBLISHED" in out["epsilon"] and "[ironbank/e (built here)]" in out["epsilon"]
