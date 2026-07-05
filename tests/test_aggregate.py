"""Aggregate operator: GROUP BY with COUNT and SUM. Linear per DBSP — the
per-group accumulators move by the delta alone — but stateful: it keeps a
running (net-weight, sums) per group and emits retract-old / assert-new diffs
so the materialized result is always the current set of group rows.

The two classic IVM traps, both asserted below:
  * a group whose COUNT returns to zero must VANISH, not linger at (0, ...);
  * a group's SUM reaching zero must NOT make it vanish — existence is keyed on
    COUNT (net weight), never on SUM.
"""

import random

import pytest

from ivm.zset import ZSet
from ivm.plan import Source, Aggregate, Count, Sum, Avg, Min, Max
from ivm.engine import Engine

from harness import add, oracle_result, check_every

SCHEMA = ("cat", "amount")


def count_plan():
    return Aggregate(Source("t", SCHEMA), ("cat",), (Count("n"),))


def sum_plan():
    return Aggregate(Source("t", SCHEMA), ("cat",), (Sum("total", "amount"),))


def combined_plan():
    return Aggregate(Source("t", SCHEMA), ("cat",), (Count("n"), Sum("total", "amount")))


def avg_plan():
    return Aggregate(Source("t", SCHEMA), ("cat",), (Avg("mean", "amount"),))


def count_sum_avg_plan():
    return Aggregate(
        Source("t", SCHEMA),
        ("cat",),
        (Count("n"), Sum("total", "amount"), Avg("mean", "amount")),
    )


def min_max_plan():
    return Aggregate(Source("t", SCHEMA), ("cat",), (Min("lo", "amount"), Max("hi", "amount")))


def everything_plan():
    return Aggregate(
        Source("t", SCHEMA),
        ("cat",),
        (
            Count("n"),
            Sum("total", "amount"),
            Avg("mean", "amount"),
            Min("lo", "amount"),
            Max("hi", "amount"),
        ),
    )


def build(plan):
    eng = Engine()
    view = eng.add_view("agg", plan)
    return eng, view


def test_count_and_sum_single_group():
    eng, view = build(combined_plan())
    eng.apply("t", ZSet({("a", 5): +1}))
    assert view.result() == {("a", 1, 5): 1}
    eng.apply("t", ZSet({("a", 3): +1}))
    assert view.result() == {("a", 2, 8): 1}


def test_group_vanishes_when_count_reaches_zero():
    eng, view = build(combined_plan())
    eng.apply("t", ZSet({("vlogs", 9): +1}))
    eng.apply("t", ZSet({("vlogs", 9): -1}))
    assert view.result() == {}


def test_sum_zero_with_positive_count_does_not_vanish():
    """count 2, sum 0 must survive — existence keyed on count, not sum."""
    eng, view = build(combined_plan())
    eng.apply("t", ZSet({("a", 5): +1}))
    eng.apply("t", ZSet({("a", -5): +1}))
    assert view.result() == {("a", 2, 0): 1}


def test_batch_delta_touching_two_groups():
    eng, view = build(combined_plan())
    eng.apply("t", ZSet({("a", 5): +1, ("b", 2): +1, ("a", 1): +1}))
    assert view.result() == {("a", 2, 6): 1, ("b", 1, 2): 1}
    # one batch that grows one group and empties another
    eng.apply("t", ZSet({("b", 2): -1, ("c", 4): +1}))
    assert view.result() == {("a", 2, 6): 1, ("c", 1, 4): 1}


def test_avg_single_group():
    eng, view = build(avg_plan())
    eng.apply("t", ZSet({("a", 5): +1}))
    eng.apply("t", ZSet({("a", 3): +1}))
    assert view.result() == {("a", 4.0): 1}  # (5+3)/2


def test_avg_recovers_after_delete():
    """AVG keeps sum and count separately; deleting a row must move the mean."""
    eng, view = build(count_sum_avg_plan())
    eng.apply("t", ZSet({("a", 5): +1}))
    eng.apply("t", ZSet({("a", 3): +1}))
    eng.apply("t", ZSet({("a", 10): +1}))
    assert view.result() == {("a", 3, 18, 6.0): 1}  # count 3, sum 18, avg 6.0
    eng.apply("t", ZSet({("a", 10): -1}))
    assert view.result() == {("a", 2, 8, 4.0): 1}  # count 2, sum 8, avg 4.0


def test_avg_group_vanishes_at_zero_count():
    eng, view = build(avg_plan())
    eng.apply("t", ZSet({("a", 5): +1}))
    eng.apply("t", ZSet({("a", 5): -1}))
    assert view.result() == {}


def test_min_max_basic():
    eng, view = build(min_max_plan())
    eng.apply("t", ZSet({("a", 5): +1, ("a", 3): +1, ("a", 8): +1}))
    assert view.result() == {("a", 3, 8): 1}


def test_min_max_recover_after_deleting_extremes():
    """The non-linear case: deleting the current MIN (or MAX) must recover the
    next extreme from the remaining values — a single accumulator can't do this,
    so the operator keeps the group's full value multiset."""
    eng, view = build(min_max_plan())
    eng.apply("t", ZSet({("a", 5): +1, ("a", 3): +1, ("a", 8): +1}))
    assert view.result() == {("a", 3, 8): 1}
    eng.apply("t", ZSet({("a", 3): -1}))  # delete current MIN
    assert view.result() == {("a", 5, 8): 1}  # MIN recovers to 5
    eng.apply("t", ZSet({("a", 8): -1}))  # delete current MAX
    assert view.result() == {("a", 5, 5): 1}  # MAX recovers to 5
    eng.apply("t", ZSet({("a", 5): -1}))  # last row leaves -> group vanishes
    assert view.result() == {}


def test_min_max_duplicate_values():
    """A repeated extreme must survive until ALL its copies are deleted."""
    eng, view = build(min_max_plan())
    eng.apply("t", ZSet({("a", 3): +2, ("a", 5): +1}))  # two 3's and a 5
    assert view.result() == {("a", 3, 5): 1}
    eng.apply("t", ZSet({("a", 3): -1}))  # one 3 remains
    assert view.result() == {("a", 3, 5): 1}
    eng.apply("t", ZSet({("a", 3): -1}))  # last 3 gone -> MIN recovers to 5
    assert view.result() == {("a", 5, 5): 1}


@pytest.mark.parametrize("seed", range(8))
def test_min_max_extreme_deletion_stress(seed):
    """Deliberately delete the CURRENT min or max most of the time, so the
    recovery path runs constantly, and check against the oracle."""
    rng = random.Random(seed)
    plan = everything_plan()
    eng, view = build(plan)
    tables: dict = {}
    live: list[tuple] = []
    do_check = check_every(seed)

    def rows_in(cat):
        return [r for r in live if r[0] == cat]

    step = 0
    for step in range(300):
        cats = sorted({r[0] for r in live})
        r = rng.random()
        if cats and r < 0.35:  # delete the current MIN of some category
            target = min(rows_in(rng.choice(cats)), key=lambda x: x[1])
            live.remove(target)
            delta = ZSet({target: -1})
        elif cats and r < 0.70:  # delete the current MAX
            target = max(rows_in(rng.choice(cats)), key=lambda x: x[1])
            live.remove(target)
            delta = ZSet({target: -1})
        else:  # insert
            row = (rng.choice(["a", "b", "c"]), rng.randint(0, 9))
            live.append(row)
            delta = ZSet({row: +1})
        eng.apply("t", delta)
        add(tables, "t", delta)
        if do_check(step):
            assert view.result() == oracle_result(plan, tables)

    while live:
        row = live.pop()
        delta = ZSet({row: -1})
        eng.apply("t", delta)
        add(tables, "t", delta)
        step += 1
        if do_check(step):
            assert view.result() == oracle_result(plan, tables)
    assert view.result() == {}


@pytest.mark.parametrize(
    "make_plan",
    [count_plan, sum_plan, combined_plan, avg_plan, count_sum_avg_plan, min_max_plan],
)
@pytest.mark.parametrize("seed", range(12))
def test_aggregate_matches_oracle(make_plan, seed):
    rng = random.Random(seed)
    plan = make_plan()
    eng, view = build(plan)
    tables: dict = {}
    live: list[tuple] = []

    do_check = check_every(seed)
    step = 0
    for step in range(400):
        if live and rng.random() < 0.45:
            row = live.pop(rng.randrange(len(live)))
            delta = ZSet({row: -1})
        else:
            # small category pool so groups empty out often; negative amounts
            # so SUM can hit zero while COUNT stays positive
            row = (rng.choice(["a", "b", "c"]), rng.randint(-3, 10))
            live.append(row)
            delta = ZSet({row: +1})
        eng.apply("t", delta)
        add(tables, "t", delta)
        if do_check(step):
            assert view.result() == oracle_result(plan, tables)

    while live:
        row = live.pop()
        delta = ZSet({row: -1})
        eng.apply("t", delta)
        add(tables, "t", delta)
        step += 1
        if do_check(step):
            assert view.result() == oracle_result(plan, tables)
    assert view.result() == {}
