import pytest

from djmanager import genres


def test_parse_and_normalize():
    assert genres.parse_key("techno_hard-techno") == ["techno", "hard-techno"]
    assert genres.normalize_key(" techno _ hard techno ") == "techno_hard-techno"
    assert genres.display_name("techno_hard-techno") == "hard techno"


@pytest.mark.parametrize("bad", ["", "techno__x", "_techno", "a/b", "x_.hidden", "_removed"])
def test_invalid_keys(bad):
    with pytest.raises(genres.GenreError):
        genres.parse_key(bad)


def test_lineage():
    assert genres.lineage("a_b_c") == ["a", "a_b", "a_b_c"]
    assert genres.parent("a_b") == "a"
    assert genres.parent("a") is None
    assert genres.is_descendant_or_self("a_b", "a")
    assert not genres.is_descendant_or_self("ab", "a")
