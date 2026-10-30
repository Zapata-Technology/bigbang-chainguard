"""release.py: tag numbering and the image changes in the notes."""
from conftest import load_script

release = load_script("release")


def test_next_tag_counts_per_big_bang_version():
    assert release.next_tag("3.35.1", []) == "3.35.1-cg.1"
    assert release.next_tag("3.35.1", ["3.35.1-cg.1", "3.35.1-cg.2", "3.34.0-cg.7"]) == "3.35.1-cg.3"
    assert release.next_tag("3.36.0", ["3.35.1-cg.9"]) == "3.36.0-cg.1"


def test_previous_tag_orders_by_version_not_text():
    assert release.previous_tag(["3.35.1-cg.9", "3.35.1-cg.10", "3.9.0-cg.20", "other"]) == "3.35.1-cg.10"
    assert release.previous_tag(["v1"]) is None


def test_image_changes_from_the_overlay():
    old = "a:\n  image:\n    repository: cgr.dev/o/x\n    tag: 1.0.0\nb:\n  image: cgr.dev/o/y:2.0\n"
    new = "a:\n  image:\n    repository: cgr.dev/o/x\n    tag: 1.0.1\nc:\n  hub: cgr.dev/o\n  tag: 3.0\n"
    assert release.image_changes(old, new) == ["- `cgr.dev/o/x`: 1.0.0 → 1.0.1", "- `cgr.dev/o`: new at 3.0",
                                               "- `cgr.dev/o/y`: removed"]
