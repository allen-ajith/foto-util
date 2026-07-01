"""Time-gap grouping, including robustness to a wrong-but-consistent clock."""

from __future__ import annotations

from foto_util.grouping import GroupItem, assign_groups


def _items(times: list[float]) -> list[GroupItem]:
    return [GroupItem(ident=f"h{i}", time_value=t, seq=i) for i, t in enumerate(times)]


def test_scene_split_on_large_gap():
    # three frames ~1s apart, then a 10-min gap, then two more
    times = [0, 1, 2, 600, 601]
    groups = assign_groups(_items(times), gap_s=300)
    assert groups["h0"] == groups["h1"] == groups["h2"] == 0
    assert groups["h3"] == groups["h4"] == 1


def test_grouping_is_invariant_to_a_constant_clock_offset():
    times = [0, 1, 2, 600, 601, 1200]
    base = assign_groups(_items(times), gap_s=300)
    # Shift every timestamp by the same (large) constant, as a misset-once
    # camera clock would — intervals, and therefore groups, are unchanged.
    shifted = assign_groups(_items([t + 250_000_000 for t in times]), gap_s=300)
    assert base == shifted


def test_group_ids_are_contiguous_in_time_order():
    groups = assign_groups(_items([1000, 0, 500]), gap_s=100)
    # Reordered input, but ids must increase with capture time.
    assert groups["h1"] == 0  # t=0
    assert groups["h2"] == 1  # t=500
    assert groups["h0"] == 2  # t=1000
