"""Tag selection: the pin policy every converted image goes through."""
from orgtags import parse, resolve

TAGS = ["1.20.3", "1.20.4", "1.20.4-dev", "1.20.4-r2", "1.20", "1.21.0", "1.21.2", "v1.22.1", "latest",
        "2.0.0", "1.20.5-slim", "sha256-abc.sig"]


def test_parse():
    assert parse("v1.20.3") == ((1, 20, 3), None)
    assert parse("8.10.2-slim") == ((8, 10, 2), "slim")
    assert parse("1.36") == ((1, 36), None)
    assert parse("latest") is None
    assert parse("sha256-abc.sig") is None


def test_newest_patch_on_the_iron_bank_line():
    assert resolve("v1.20.3", TAGS) == ("1.20.4", "Iron Bank v1.20.3 -> newest 1.20 patch")


def test_exact_match_has_no_note():
    assert resolve("1.21.2", TAGS) == ("1.21.2", None)


def test_revision_and_dev_tags_are_not_candidates():
    tag, _ = resolve("1.20.3", ["1.20.3", "1.20.9-r1", "1.20.9-dev"])
    assert tag == "1.20.3"


def test_major_minor_iron_bank_tag_takes_the_stream_tag():
    assert resolve("v1.20", TAGS) == ("1.20", None)


def test_iron_bank_suffixes_are_ignored():
    assert resolve("1.20.3-amd64", TAGS)[0] == "1.20.4"
    assert resolve("ubi-fips-v1.21.0", TAGS)[0] == "1.21.2"


def test_minor_ahead_when_the_line_is_missing():
    tag, note = resolve("1.19.1", TAGS)
    assert tag == "v1.22.1"  # the org's own spelling
    assert note.startswith("MINOR AHEAD")


def test_strict_versions_refuses_minor_ahead():
    tag, note = resolve("1.19.1", TAGS, allow_ahead=False)
    assert tag is None
    assert "--strict-versions" in note


def test_never_crosses_a_major():
    tag, note = resolve("3.1.0", TAGS)
    assert tag is None
    assert "no 3.x tag" in note


def test_older_patch_is_flagged():
    tag, note = resolve("1.21.5", TAGS)
    assert tag == "1.21.2"
    assert "older than Iron Bank" in note


def test_star_takes_the_newest_release():
    assert resolve("*", TAGS) == ("2.0.0", None)


def test_suffix_selects_a_variant():
    assert resolve("1.20.3", TAGS, suffix="slim")[0] == "1.20.5-slim"
