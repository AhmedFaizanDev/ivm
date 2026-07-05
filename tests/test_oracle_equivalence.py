"""Milestone 0 oracle tests: the incremental view must equal a full recompute
after EVERY delta, including when a group empties to zero.

The view under test is the hand-wired equivalent of:
    SELECT category, COUNT(*), SUM(amount) FROM t GROUP BY category
"""

import random

import pytest

from ivm.zset import ZSet
from ivm.view import GroupCountSumView
from ivm.oracle import recompute


def apply_and_check(table: ZSet, view: GroupCountSumView, delta: ZSet) -> ZSet:
    """Apply one delta to both the base table and the incremental view,
    then assert the view equals the from-scratch oracle."""
    table = table + delta
    view.apply(delta)
    assert view.result() == recompute(table)
    return table


def test_single_insert():
    table, view = ZSet(), GroupCountSumView()
    table = apply_and_check(table, view, ZSet({("gaming", 5): +1}))
    assert view.result() == {"gaming": (1, 5)}


def test_inserts_accumulate_per_group():
    table, view = ZSet(), GroupCountSumView()
    table = apply_and_check(table, view, ZSet({("gaming", 5): +1}))
    table = apply_and_check(table, view, ZSet({("gaming", 3): +1}))
    table = apply_and_check(table, view, ZSet({("music", 7): +1}))
    assert view.result() == {"gaming": (2, 8), "music": (1, 7)}


def test_delete_updates_counts_and_sums():
    table, view = ZSet(), GroupCountSumView()
    table = apply_and_check(table, view, ZSet({("gaming", 5): +1}))
    table = apply_and_check(table, view, ZSet({("gaming", 3): +1}))
    table = apply_and_check(table, view, ZSet({("gaming", 5): -1}))
    assert view.result() == {"gaming": (1, 3)}


def test_group_emptying_to_zero_disappears():
    """The classic IVM bug: a group whose count hits zero must VANISH,
    not linger with (0, 0)."""
    table, view = ZSet(), GroupCountSumView()
    table = apply_and_check(table, view, ZSet({("vlogs", 9): +1}))
    table = apply_and_check(table, view, ZSet({("vlogs", 9): -1}))
    assert view.result() == {}
    assert "vlogs" not in view.result()


def test_duplicate_rows_carry_weight():
    """Two identical rows = one Z-set entry with weight 2; deleting one
    leaves weight 1."""
    table, view = ZSet(), GroupCountSumView()
    table = apply_and_check(table, view, ZSet({("gaming", 5): +1}))
    table = apply_and_check(table, view, ZSet({("gaming", 5): +1}))
    assert view.result() == {"gaming": (2, 10)}
    table = apply_and_check(table, view, ZSet({("gaming", 5): -1}))
    assert view.result() == {"gaming": (1, 5)}


def test_multi_row_delta_in_one_batch():
    """A single delta may carry several changes at once (a mini-transaction)."""
    table, view = ZSet(), GroupCountSumView()
    delta = ZSet({("gaming", 5): +1, ("music", 2): +1, ("gaming", 1): +1})
    table = apply_and_check(table, view, delta)
    assert view.result() == {"gaming": (2, 6), "music": (1, 2)}
    # batch that inserts into one group while emptying another
    delta2 = ZSet({("music", 2): -1, ("vlogs", 4): +1})
    table = apply_and_check(table, view, delta2)
    assert view.result() == {"gaming": (2, 6), "vlogs": (1, 4)}


def test_negative_amounts_sum_correctly():
    """SUM must be correct even when amounts are negative — a group can have
    a nonzero count with a zero sum, and must NOT disappear."""
    table, view = ZSet(), GroupCountSumView()
    table = apply_and_check(table, view, ZSet({("gaming", 5): +1}))
    table = apply_and_check(table, view, ZSet({("gaming", -5): +1}))
    assert view.result() == {"gaming": (2, 0)}


@pytest.mark.parametrize("seed", range(20))
def test_property_random_streams(seed):
    """The lie detector: thousands of random inserts/deletes across a small
    category pool (so groups empty out often). After EVERY delta the
    incremental view must equal the oracle. apply_and_check asserts each step."""
    rng = random.Random(seed)
    categories = ["a", "b", "c"]
    table, view = ZSet(), GroupCountSumView()
    live: list[tuple] = []  # rows currently in the table, with multiplicity

    for _ in range(400):
        do_delete = live and rng.random() < 0.45
        if do_delete:
            row = live.pop(rng.randrange(len(live)))
            delta = ZSet({row: -1})
        else:
            row = (rng.choice(categories), rng.randint(-3, 10))
            live.append(row)
            delta = ZSet({row: +1})
        table = apply_and_check(table, view, delta)

    # drain to empty: every group must eventually disappear
    while live:
        row = live.pop()
        table = apply_and_check(table, view, ZSet({row: -1}))
    assert view.result() == {}


def test_zset_drops_zero_weights():
    """Z-set invariant: weight 0 means absent."""
    z = ZSet({("gaming", 5): +1}) + ZSet({("gaming", 5): -1})
    assert dict(z.items()) == {}
