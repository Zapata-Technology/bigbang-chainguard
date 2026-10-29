"""Mapping an Iron Bank image to the org, and the generated docs."""
import pytest

from conftest import FakeTags, load_script

convert = load_script("bb-convert")

ORG_TAGS = {
    "app": ["1.2.3", "1.2.5"], "app-fips": ["1.2.5"],
    "old": ["2.4.0"], "old-fips": ["2.4.0"],
    "nofips": ["3.0.1"],
    "ready": ["1.0.0"],
    "dated": ["0.20250101.000000", "0.20251015.172955"], "dated-fips": ["0.20251015.172955"],
}
MAPPING = {"images": {
    "ironbank/app": "app",
    "ironbank/old": "old",
    "ironbank/nofips": {"cg": "nofips", "fips": None},
    "ironbank/gone": "gone",
    "ironbank/vendor": {"stays": "proprietary"},
    "ironbank/later": {"stays": "no tags yet", "until": "ready"},
    "ironbank/pinned": {"ref": "ghcr.io/me/pinned", "fips": "ghcr.io/me/pinned-fips", "tag": "9"},
    "ironbank/dated": {"cg": "dated", "version": "*"},
}}


@pytest.fixture
def conv():
    return convert.Converter(MAPPING, FakeTags(ORG_TAGS))


def test_standard_and_fips(conv):
    assert conv.target("ironbank/app", "1.2.3", "pkg") == ("cgr.dev/example.com/app:1.2.5",
                                                           "cgr.dev/example.com/app-fips:1.2.5")


def test_minor_ahead_is_noted(conv):
    std, _ = conv.target("ironbank/old", "2.3.1", "pkg")
    assert std.endswith("old:2.4.0")
    assert "MINOR AHEAD" in conv.rows[-1][4]


def test_strict_versions_leaves_it_unresolved():
    c = convert.Converter(MAPPING, FakeTags(ORG_TAGS), strict=True)
    assert c.target("ironbank/old", "2.3.1", "pkg") is None
    assert c.rows[-1][2] == "UNRESOLVED"


def test_no_fips_variant_keeps_the_standard_image(conv):
    std, fips = conv.target("ironbank/nofips", "3.0.0", "pkg")
    assert std == fips


def test_missing_org_repo(conv):
    assert conv.target("ironbank/gone", "1.0.0", "pkg") is None
    assert {"gone", "gone-fips"} <= conv.missing


def test_unmapped(conv):
    assert conv.target("ironbank/new", "1.0", "pkg") is None
    assert "ironbank/new" in conv.unmapped


def test_stays_and_until(conv):
    assert conv.target("ironbank/vendor", "1", "pkg") is None
    assert conv.target("ironbank/later", "1", "pkg") is None
    assert conv.ready == {("ironbank/later", "ready")}


def test_ref_is_used_as_is(conv):
    assert conv.target("ironbank/pinned", "1", "pkg") == ("ghcr.io/me/pinned:9", "ghcr.io/me/pinned-fips:9")


def test_star_version_is_newest_and_noted(conv):
    std, _ = conv.target("ironbank/dated", "RELEASE.2025-10-15T17-29-55Z", "pkg")
    assert std.endswith("dated:0.20251015.172955")
    assert "newest org release" in conv.rows[-1][4]


def test_override_tokens(conv):
    spec = {"registry": "@registry", "tag": "@tag:app:1.2", "image": {"repository": "@repo:app"},
            "full": "@ref:app:*", "plain": 1}
    assert conv.overrides("pkg", spec, False) == {
        "registry": "cgr.dev/example.com", "tag": "1.2.5", "image": {"repository": "app"},
        "full": "cgr.dev/example.com/app:1.2.5", "plain": 1}
    assert conv.overrides("pkg", spec, True)["image"]["repository"] == "app-fips"


def test_allowed_registries():
    assert convert.allowed_registries(MAPPING, "cgr.dev/example.com", "ghcr.io/me") == [
        "cgr.dev/example.com", "ghcr.io/me"]
    assert convert.allowed_registries(MAPPING, "cgr.dev/example.com") == ["cgr.dev/example.com", "ghcr.io/me"]


def test_report_lists_version_differences(conv):
    conv.target("ironbank/old", "2.3.1", "pkg")
    conv.target("ironbank/dated", "RELEASE.2025", "pkg")
    conv.target("ironbank/vendor", "1", "pkg")
    md = convert.report(conv.rows, "3.0.0", conv.cg)
    assert "## Stays on Iron Bank (1)" in md
    assert "## Version differences (2)" in md
    assert "(newer minor)" in md and "(newest release" in md


def test_required_images_is_org_independent():
    discovered = {"packages": {"pkg": {"rendered": ["registry1.dso.mil/ironbank/app:1.2.3"], "values": [
        {"shape": "repo", "value": {"repository": "registry1.dso.mil/ironbank/nofips", "tag": "3"}},
        {"shape": "repo", "value": {"repository": "registry1.dso.mil/ironbank/vendor", "tag": "1"}}]}}}
    md = convert.required_images(discovered, MAPPING, "3.0.0")
    assert "**2 standard images**" in md and "**1 FIPS images**" in md
    assert "| `app` | `app-fips` | pkg |" in md
    assert "| `nofips` | none: FIPS keeps the standard image | pkg |" in md
    assert "vendor" not in md


GROUP = {"images": {
    "gitlab/web": {"group": "gl", "strict": True, "fallback": "ironbank", "cg": "web"},
    "gitlab/shell": {"group": "gl", "strict": True, "fallback": "ironbank", "cg": "shell"},
    "ironbank/opt": {"cg": "opt", "fallback": "ironbank"},
    "ironbank/strict": {"cg": "old", "strict": True},
}}
IB_TAGS = {"gitlab/web": "v19.4.2-ubi", "gitlab/shell": "v14.57.3-ubi"}


def test_fallback_keeps_iron_bank_instead_of_failing():
    c = convert.Converter(GROUP, FakeTags({}), ib_tags=IB_TAGS)
    assert c.target("ironbank/opt", "1.0.0", "pkg") is None
    assert c.rows[-1][2] == "Iron Bank" and "falls back" in c.rows[-1][4]
    assert "opt" in c.optional_missing and not c.missing


def test_strict_per_image():
    c = convert.Converter(GROUP, FakeTags(ORG_TAGS))
    assert c.target("ironbank/strict", "2.3.1", "pkg") is None
    assert c.rows[-1][2] == "UNRESOLVED"


def test_group_converts_together():
    tags = {"web": ["19.4.5"], "web-fips": ["19.4.5"], "shell": ["14.57.4"], "shell-fips": ["14.57.4"]}
    c = convert.Converter(GROUP, FakeTags(tags), ib_tags=IB_TAGS)
    assert c.target("gitlab/web", "v19.4.2-ubi", "gitlab")[0].endswith("web:19.4.5")
    assert c.target("gitlab/shell", "v14.57.3-ubi", "gitlab")[0].endswith("shell:14.57.4")


def test_group_falls_back_together_when_one_member_is_off_the_line():
    tags = {"web": ["19.4.5"], "web-fips": ["19.4.5"], "shell": ["14.58.0"], "shell-fips": ["14.58.0"]}
    c = convert.Converter(GROUP, FakeTags(tags), ib_tags=IB_TAGS)
    assert c.target("gitlab/web", "v19.4.2-ubi", "gitlab") is None
    assert "move together" in c.rows[-1][4] and "shell" in c.rows[-1][4]


def test_version_from_matches_another_images_release():
    m = {"images": {"gitlab/shell": {"group": "gl", "strict": True, "cg": "shell",
                                     "version_from": "gitlab/base"}}}
    tags = {"shell": ["19.4.3", "19.5.1"], "shell-fips": ["19.4.3"]}
    c = convert.Converter(m, FakeTags(tags), ib_tags={"gitlab/shell": "v14.57.3-ubi", "gitlab/base": "v19.4.2-ubi"})
    assert c.target("gitlab/shell", "v14.57.3-ubi", "gitlab")[0].endswith("shell:19.4.3")


def test_flux_extras_are_deleted():
    import fluxrelease
    patches = [p["patch"] for p in fluxrelease.remove_extras()]
    assert len(patches) == 3 and all("$patch: delete" in p and "kind: Deployment" in p for p in patches)
