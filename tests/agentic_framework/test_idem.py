"""Idempotency-key helper (util/idem.py).

The contract that matters for approval/audit dedup: the same logical action ->
the same key (so a Temporal activity retry or a LangGraph node re-run collapses
to ONE row); any difference -> a different key (so genuinely distinct actions in
one session all survive).
"""

from util.idem import make_idem_key


def test_same_parts_are_stable_and_deterministic():
    a = make_idem_key("sess-1", "bed_agent", "assign_bed", "bed-7")
    b = make_idem_key("sess-1", "bed_agent", "assign_bed", "bed-7")
    assert a == b
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)


def test_different_actions_differ():
    base = ("sess-1", "bed_agent", "assign_bed", "bed-7")
    assert make_idem_key(*base) != make_idem_key("sess-2", *base[1:])
    assert make_idem_key(*base) != make_idem_key(base[0], "icu_agent", *base[2:])
    assert make_idem_key(*base) != make_idem_key(*base[:3], "bed-8")


def test_order_is_significant():
    """('a','b') and ('b','a') are different actions, not the same one reordered."""
    assert make_idem_key("a", "b") != make_idem_key("b", "a")


def test_non_string_parts_are_coerced_stably():
    """Ids reach this helper as UUIDs, ints or None depending on the caller; the
    same logical action must not produce two keys because of a type change."""
    assert make_idem_key(1, 2) == make_idem_key(1, 2)
    assert make_idem_key(None) == make_idem_key(None)
    assert make_idem_key({"b": 1, "a": 2}) == make_idem_key({"a": 2, "b": 1})


def test_arity_is_significant():
    """A trailing part must not be absorbed — otherwise two distinct approvals in
    one session could dedup into a single row and one would be lost."""
    assert make_idem_key("sess-1", "assign") != make_idem_key("sess-1", "assign", "")


def test_no_delimiter_collision_between_adjacent_parts():
    """Parts are canonicalised structurally, not concatenated, so a value that
    contains the separator cannot forge a different action's key."""
    assert make_idem_key("a", "b") != make_idem_key("ab")
    assert make_idem_key("a,b") != make_idem_key("a", "b")
    assert make_idem_key("a:b") != make_idem_key("a", "b")
