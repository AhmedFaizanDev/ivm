"""Filter operator: keeps input rows where a predicate holds, preserving Z-set
weights. Filter is linear (DBSP): the incremental version applies the predicate
to the delta alone and emits the survivors — no state. The oracle re-filters the
whole accumulated table from scratch; the two must agree after every delta.
"""

import random

import pytest

from ivm.zset import ZSet
from ivm.plan import Source, Filter
from ivm.engine import Engine

from harness import add, oracle_result

SCHEMA = ("id", "amount", "status")


def build():
    """A filter view: keep active rows with a positive amount."""
    plan = Filter(
        Source("t", SCHEMA),
        lambda r: r["amount"] > 0 and r["status"] == "active",
    )
    eng = Engine()
    view = eng.add_view("positive_active", plan)
    return eng, view, plan


def test_filter_single_insert_passes_predicate():
    eng, view, plan = build()
    eng.apply("t", ZSet({(1, 5, "active"): +1}))
    assert view.result() == {(1, 5, "active"): 1}


def test_filter_single_insert_fails_predicate():
    eng, view, plan = build()
    eng.apply("t", ZSet({(1, 5, "archived"): +1}))
    eng.apply("t", ZSet({(2, -3, "active"): +1}))
    assert view.result() == {}


def test_filter_delete_removes_row():
    eng, view, plan = build()
    eng.apply("t", ZSet({(1, 5, "active"): +1}))
    eng.apply("t", ZSet({(1, 5, "active"): -1}))
    assert view.result() == {}


@pytest.mark.parametrize("seed", range(20))
def test_filter_matches_oracle(seed):
    rng = random.Random(seed)
    eng, view, plan = build()
    tables: dict = {}
    live: list[tuple] = []

    for _ in range(300):
        if live and rng.random() < 0.45:
            row = live.pop(rng.randrange(len(live)))
            delta = ZSet({row: -1})
        else:
            row = (
                rng.randint(0, 4),
                rng.randint(-3, 10),
                rng.choice(["active", "archived"]),
            )
            live.append(row)
            delta = ZSet({row: +1})
        eng.apply("t", delta)
        add(tables, "t", delta)
        assert view.result() == oracle_result(plan, tables)

    while live:
        row = live.pop()
        delta = ZSet({row: -1})
        eng.apply("t", delta)
        add(tables, "t", delta)
        assert view.result() == oracle_result(plan, tables)
    assert view.result() == {}
