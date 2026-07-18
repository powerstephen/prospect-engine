"""
sources.py - the single, canonical definition of source tags.

Tonight found THREE separate lead pools that existed, had real emails,
but were completely invisible to every script in the pipeline: the
registry PDF import (source='registry:tx_permits'), an untagged Florida
batch (source=NULL), and 236 leads from the web harvester
(source='harvest'). Every one of those happened because a tag was
hand-typed somewhere that didn't match the wildcard every OTHER script
filters on ('google_maps:{vertical}%').

This file exists so that never happens again by construction: every
writer imports source_tag() instead of typing a string, and every
reader imports vertical_filter() instead of typing a LIKE pattern.
"""

# The only verticals that exist. Adding a new one means adding it here,
# nowhere else.
VERTICALS = {"hvac", "roofers", "tree_removal"}


def source_tag(vertical: str, subsource: str = "") -> str:
    """The one place a source string gets constructed.

    source_tag("roofers") -> "google_maps:roofers"
    source_tag("roofers", "premium") -> "google_maps:roofers_premium"
    source_tag("roofers", "registry") -> "google_maps:roofers_registry"
    source_tag("tree_removal", "harvest") -> "google_maps:tree_removal_harvest"
    """
    vertical = vertical.strip().lower()
    if vertical not in VERTICALS:
        raise ValueError(
            "Unknown vertical '" + vertical + "'. Known verticals: " +
            ", ".join(sorted(VERTICALS)) +
            ". Add it to VERTICALS in db/sources.py first, don't just type a new string."
        )
    if subsource:
        return "google_maps:" + vertical + "_" + subsource.strip().lower()
    return "google_maps:" + vertical


def vertical_filter(vertical: str) -> str:
    """The one place a source LIKE-pattern gets constructed. Every
    export/enrich/tag script should filter with this, not a hand-typed
    'like.google_maps:roofers*' string."""
    vertical = vertical.strip().lower()
    if vertical not in VERTICALS:
        raise ValueError("Unknown vertical '" + vertical + "'. See VERTICALS in db/sources.py.")
    return "google_maps:" + vertical + "*"


if __name__ == "__main__":
    # self-test
    assert source_tag("roofers") == "google_maps:roofers"
    assert source_tag("roofers", "premium") == "google_maps:roofers_premium"
    assert source_tag("tree_removal", "harvest") == "google_maps:tree_removal_harvest"
    assert vertical_filter("roofers") == "google_maps:roofers*"
    try:
        source_tag("plumbers")
        raise AssertionError("should have rejected an unknown vertical")
    except ValueError:
        pass
    print("sources.py self-test: OK")
