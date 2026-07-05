"""Milestone 1 acceptance: the whole engine, not one operator.

A two-join + one-aggregate view — orders JOIN users JOIN products, grouped by
region with COUNT and two SUMs — must equal a from-scratch recompute after every
delta across random insert/delete streams on ALL THREE base tables, drained to
empty. Simultaneously, two more views over the SAME tables (a per-user order
count, and a filtered/projected user list) must also stay correct — proving the
engine fans one base delta out to every view that reads the table.
"""

import random

import pytest

from ivm.zset import ZSet
from ivm.plan import Source, Filter, Project, Join, Aggregate, Count, Sum
from ivm.engine import Engine

from harness import add, oracle_result

USERS = ("uid", "uname", "region")
ORDERS = ("oid", "uid", "pid", "qty")
PRODUCTS = ("pid", "pname", "price")


def revenue_by_region_plan():
    """The done-criteria view: two joins feeding one aggregate."""
    joined = Join(
        Join(Source("orders", ORDERS), Source("users", USERS), ("uid",), ("uid",)),
        Source("products", PRODUCTS),
        ("pid",),
        ("pid",),
    )
    return Aggregate(
        joined,
        ("region",),
        (Count("n_orders"), Sum("total_qty", "qty"), Sum("total_price", "price")),
    )


def orders_per_user_plan():
    return Aggregate(Source("orders", ORDERS), ("uid",), (Count("n"),))


def west_users_plan():
    return Project(
        Filter(Source("users", USERS), lambda r: r["region"] == "west"),
        (("uid", lambda r: r["uid"]), ("uname", lambda r: r["uname"])),
    )


def build():
    eng = Engine()
    plans = {
        "revenue_by_region": revenue_by_region_plan(),
        "orders_per_user": orders_per_user_plan(),
        "west_users": west_users_plan(),
    }
    views = {name: eng.add_view(name, plan) for name, plan in plans.items()}
    return eng, views, plans


def test_two_join_one_aggregate_smoke():
    eng, views, plans = build()
    eng.apply("users", ZSet({(1, "ann", "west"): +1}))
    eng.apply("products", ZSet({(7, "widget", 3): +1}))
    eng.apply("orders", ZSet({(100, 1, 7, 4): +1}))
    # region "west": 1 order, qty 4, price 3
    assert views["revenue_by_region"].result() == {("west", 1, 4, 3): 1}
    assert views["orders_per_user"].result() == {(1, 1): 1}
    assert views["west_users"].result() == {(1, "ann"): 1}
    # retract the order — the aggregate group must vanish
    eng.apply("orders", ZSet({(100, 1, 7, 4): -1}))
    assert views["revenue_by_region"].result() == {}
    assert views["orders_per_user"].result() == {}


def _random_delta(rng, table, pool):
    if pool and rng.random() < 0.45:
        row = pool.pop(rng.randrange(len(pool)))
        return ZSet({row: -1})
    if table == "users":
        row = (rng.randint(0, 3), rng.choice(["ann", "bob", "cat"]),
               rng.choice(["west", "east"]))
    elif table == "orders":
        row = (rng.randint(0, 40), rng.randint(0, 3), rng.randint(0, 2), rng.randint(1, 5))
    else:  # products
        row = (rng.randint(0, 2), rng.choice(["x", "y", "z"]), rng.randint(1, 4))
    pool.append(row)
    return ZSet({row: +1})


@pytest.mark.parametrize("seed", range(15))
def test_all_views_match_oracle(seed):
    rng = random.Random(seed)
    eng, views, plans = build()
    tables: dict = {}
    live = {"users": [], "orders": [], "products": []}

    def check():
        for name, view in views.items():
            assert view.result() == oracle_result(plans[name], tables), (
                f"view {name} diverged from oracle"
            )

    for _ in range(500):
        table = rng.choice(["users", "orders", "products"])
        delta = _random_delta(rng, table, live[table])
        eng.apply(table, delta)
        add(tables, table, delta)
        check()

    # drain every table to empty; all views must end empty
    for table in ("orders", "users", "products"):
        while live[table]:
            row = live[table].pop()
            delta = ZSet({row: -1})
            eng.apply(table, delta)
            add(tables, table, delta)
            check()
    for view in views.values():
        assert view.result() == {}
