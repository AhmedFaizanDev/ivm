"""DISTINCT operator: collapse Z-set multiplicities to presence — a row is in the
output (weight 1) iff its net input weight is positive. Non-linear: the operator
keeps the accumulated per-row weight and emits +1 only when a row crosses from
absent to present, -1 only when it crosses back. Proven by incremental == oracle
over random streams full of duplicates and deletes (which drive the crossings).
"""

import random

import pytest

from ivm.zset import ZSet
from ivm.plan import Source, Distinct, Project
from ivm.engine import Engine

from harness import add, oracle_result, check_every

T = ("id", "cat", "amount")


def _cat_plan():
    return Distinct(Project(Source("t", T), (("cat", lambda r: r["cat"]),)))


def test_distinct_collapses_duplicates():
    eng = Engine()
    v = eng.add_view("v", _cat_plan())
    eng.apply("t", ZSet({(1, "a", 5): +1, (2, "a", 7): +1, (3, "b", 9): +1}))
    assert v.result() == {("a",): 1, ("b",): 1}  # two 'a' rows collapse to one


def test_distinct_retracts_only_when_last_copy_deleted():
    eng = Engine()
    v = eng.add_view("v", _cat_plan())
    eng.apply("t", ZSet({(1, "a", 5): +1, (2, "a", 7): +1}))
    assert v.result() == {("a",): 1}
    eng.apply("t", ZSet({(1, "a", 5): -1}))  # one 'a' remains -> still present
    assert v.result() == {("a",): 1}
    eng.apply("t", ZSet({(2, "a", 7): -1}))  # last 'a' gone -> retract
    assert v.result() == {}


def test_distinct_batched_multisign_delta_nets_correctly():
    eng = Engine()
    v = eng.add_view("v", _cat_plan())
    eng.apply("t", ZSet({(1, "a", 5): +1}))
    assert v.result() == {("a",): 1}
    # a batch that removes the 'a' and adds a 'b' at once
    eng.apply("t", ZSet({(1, "a", 5): -1, (9, "b", 2): +1}))
    assert v.result() == {("b",): 1}


@pytest.mark.parametrize("seed", range(6))
def test_distinct_batched_multisign_matches_oracle(seed):
    """Adversarial: each step applies a BATCH mixing inserts and deletes, driving
    many presence crossings per delta."""
    plan = Distinct(Project(Source("t", T), (("cat", lambda r: r["cat"]),)))
    rng = random.Random(seed)
    eng = Engine()
    v = eng.add_view("v", plan)
    tables: dict = {}
    live: list = []
    for _ in range(120):
        batch = ZSet()
        for _ in range(rng.randint(1, 5)):
            if live and rng.random() < 0.5:
                row = live.pop(rng.randrange(len(live)))
                batch = batch + ZSet({row: -1})
            else:
                row = (rng.randint(0, 10), rng.choice(["a", "b", "c"]), rng.randint(0, 5))
                live.append(row)
                batch = batch + ZSet({row: +1})
        eng.apply("t", batch)
        add(tables, "t", batch)
        assert v.result() == oracle_result(plan, tables)
    for row in list(live):
        d = ZSet({row: -1})
        eng.apply("t", d)
        add(tables, "t", d)
    assert v.result() == {}


@pytest.mark.parametrize("seed", range(10))
def test_distinct_matches_oracle(seed):
    # project to (cat, amount%3) so distinct rows collapse heavily
    plan = Distinct(
        Project(Source("t", T), (("cat", lambda r: r["cat"]), ("bucket", lambda r: r["amount"] % 3)))
    )
    rng = random.Random(seed)
    eng = Engine()
    v = eng.add_view("v", plan)
    tables: dict = {}
    live: list = []
    do_check = check_every(seed)

    step = 0
    for step in range(300):
        if live and rng.random() < 0.45:
            row = live.pop(rng.randrange(len(live)))
            d = ZSet({row: -1})
        else:
            row = (rng.randint(0, 20), rng.choice(["a", "b", "c"]), rng.randint(0, 8))
            live.append(row)
            d = ZSet({row: +1})
        eng.apply("t", d)
        add(tables, "t", d)
        if do_check(step):
            assert v.result() == oracle_result(plan, tables)

    while live:
        row = live.pop()
        d = ZSet({row: -1})
        eng.apply("t", d)
        add(tables, "t", d)
        step += 1
        if do_check(step):
            assert v.result() == oracle_result(plan, tables)
    assert v.result() == {}
