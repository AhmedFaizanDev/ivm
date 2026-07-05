"""Milestone 3 hardening: deletes in joins — the classic IVM bug farm.

The bilinear join already retracts correctly on single-side deltas (Milestone 1).
Tier-2 adds two adversarial angles this file stresses against the oracle:

  1. A CHAINED two-join whose RAW output is checked directly (no aggregate on
     top to collapse rows and hide a divergence), under heavy many-to-many
     overlap and a delete-heavy stream on all three tables.
  2. A two-join feeding the new NON-LINEAR MIN/MAX aggregate: deletes on the
     join inputs arrive at the aggregate as retractions (negative weights), which
     must correctly move the per-group extremes.
"""

import random

import pytest

from ivm.zset import ZSet
from ivm.plan import Source, Join, Aggregate, Count, Sum, Avg, Min, Max
from ivm.engine import Engine

from harness import add, oracle_result, check_every

ORDERS = ("oid", "uid", "pid")
USERS = ("uid", "uname")
PRODUCTS = ("pid", "pname")


def _two_join():
    j1 = Join(Source("orders", ORDERS), Source("users", USERS), ("uid",), ("uid",))
    return Join(j1, Source("products", PRODUCTS), ("pid",), ("pid",))


def _gen(rng, table):
    if table == "orders":
        return (rng.randint(0, 60), rng.randint(0, 2), rng.randint(0, 2))
    if table == "users":
        # small uid pool + several names => many users share a uid (many-to-many)
        return (rng.randint(0, 2), rng.choice(["ann", "bob", "cat", "dan"]))
    return (rng.randint(0, 2), rng.choice(["x", "y", "z"]))  # products


def _run_stream(seed, plan, ops=400):
    rng = random.Random(seed)
    eng = Engine()
    view = eng.add_view("v", plan)
    tables: dict = {}
    live = {"orders": [], "users": [], "products": []}
    do_check = check_every(seed)

    step = 0
    for step in range(ops):
        table = rng.choice(["orders", "users", "products"])
        pool = live[table]
        # delete-heavy so retractions dominate
        if pool and rng.random() < 0.5:
            row = pool.pop(rng.randrange(len(pool)))
            delta = ZSet({row: -1})
        else:
            row = _gen(rng, table)
            pool.append(row)
            delta = ZSet({row: +1})
        eng.apply(table, delta)
        add(tables, table, delta)
        if do_check(step):
            assert view.result() == oracle_result(plan, tables)

    for table in ("orders", "users", "products"):
        while live[table]:
            row = live[table].pop()
            delta = ZSet({row: -1})
            eng.apply(table, delta)
            add(tables, table, delta)
            step += 1
            if do_check(step):
                assert view.result() == oracle_result(plan, tables)
    assert view.result() == {}


@pytest.mark.parametrize("seed", range(12))
def test_chained_two_join_raw_output_deletes(seed):
    """Check the raw two-join Z-set (with multiplicities) directly."""
    _run_stream(seed, _two_join())


@pytest.mark.parametrize("seed", range(12))
def test_two_join_into_all_aggregates_deletes(seed):
    """Two joins feeding COUNT/SUM/AVG/MIN/MAX grouped by product name: join
    retractions must drive the non-linear aggregates correctly."""
    plan = Aggregate(
        _two_join(),
        ("pname",),
        (Count("n"), Sum("s", "uid"), Avg("a", "uid"), Min("lo", "oid"), Max("hi", "oid")),
    )
    _run_stream(seed, plan)


def test_join_partial_multiplicity_delete():
    """A row inserted with weight >1 must retract one copy at a time; combined
    rows keep the right multiplicity throughout."""
    eng = Engine()
    plan = Join(Source("orders", ORDERS), Source("users", USERS), ("uid",), ("uid",))
    view = eng.add_view("v", plan)
    tables: dict = {}

    d = ZSet({("o1", 1, "p1"): +2})  # two identical orders
    eng.apply("orders", d)
    add(tables, "orders", d)
    d = ZSet({(1, "ann"): +1, (1, "bob"): +1})  # two users share uid 1
    eng.apply("users", d)
    add(tables, "users", d)
    assert view.result() == oracle_result(plan, tables)  # 2 orders x 2 users = 4

    d = ZSet({("o1", 1, "p1"): -1})  # retract one order copy
    eng.apply("orders", d)
    add(tables, "orders", d)
    assert view.result() == oracle_result(plan, tables)  # now 1 x 2 = 2

    d = ZSet({(1, "ann"): -1})  # retract one user
    eng.apply("users", d)
    add(tables, "users", d)
    assert view.result() == oracle_result(plan, tables)  # 1 x 1 = 1
