"""Project (map) operator: rewrite each row into a new tuple of named output
columns. Linear and stateless. The interesting case is a projection that drops
a column: two distinct input rows can collapse to the same output row, so their
Z-set weights must MERGE — and cancel to absent when they net to zero. The
oracle re-projects the whole table; the two must agree after every delta.
"""

import random

import pytest

from ivm.zset import ZSet
from ivm.plan import Source, Project
from ivm.engine import Engine

from harness import add, oracle_result

SCHEMA = ("id", "cat", "amount")


def build():
    """Drop `id`, keep `cat`, and compute a derived column. Dropping id means
    rows differing only by id merge in the output."""
    plan = Project(
        Source("t", SCHEMA),
        (
            ("cat", lambda r: r["cat"]),
            ("double_amount", lambda r: r["amount"] * 2),
        ),
    )
    eng = Engine()
    view = eng.add_view("cat_double", plan)
    return eng, view, plan


def test_project_rewrites_row():
    eng, view, plan = build()
    eng.apply("t", ZSet({(1, "a", 5): +1}))
    assert view.result() == {("a", 10): 1}


def test_project_merges_colliding_rows():
    eng, view, plan = build()
    eng.apply("t", ZSet({(1, "a", 5): +1}))  # id 1 -> ("a", 10)
    eng.apply("t", ZSet({(2, "a", 5): +1}))  # id 2 -> ("a", 10), same output row
    assert view.result() == {("a", 10): 2}
    eng.apply("t", ZSet({(1, "a", 5): -1}))
    assert view.result() == {("a", 10): 1}


@pytest.mark.parametrize("seed", range(20))
def test_project_matches_oracle(seed):
    rng = random.Random(seed)
    eng, view, plan = build()
    tables: dict = {}
    live: list[tuple] = []

    for _ in range(300):
        if live and rng.random() < 0.45:
            row = live.pop(rng.randrange(len(live)))
            delta = ZSet({row: -1})
        else:
            # small id/amount pools so distinct rows collide in the projection
            row = (rng.randint(0, 2), rng.choice(["a", "b"]), rng.randint(-2, 3))
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
