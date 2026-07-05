"""Milestone 2, adapter #1: the in-process mutation log.

The app writes through a thin PK-keyed API (insert / delete / update); each write
becomes a Z-set delta fed to the engine, and UPDATE is delete-old + insert-new.
The log holds the authoritative table contents, so the oracle recomputes over
the LOG's data — the oracle sits on the far side of the adapter. A capture bug
therefore shows up exactly as an engine-vs-oracle divergence, which is the whole
point: no deltas are ever hand-fed in these tests.
"""

import random

import pytest

from ivm.plan import Source, Join, Aggregate, Count, Sum
from ivm.engine import Engine
from ivm.adapters.inproc import MutationLog

from harness import oracle_result, check_every

USERS = ("uid", "uname", "region")
ORDERS = ("oid", "uid", "amount")


def revenue_by_region_plan():
    joined = Join(Source("orders", ORDERS), Source("users", USERS), ("uid",), ("uid",))
    return Aggregate(joined, ("region",), (Count("n"), Sum("total", "amount")))


def orders_per_user_plan():
    return Aggregate(Source("orders", ORDERS), ("uid",), (Count("n"),))


def build():
    eng = Engine()
    plans = {
        "revenue_by_region": revenue_by_region_plan(),
        "orders_per_user": orders_per_user_plan(),
    }
    views = {name: eng.add_view(name, plan) for name, plan in plans.items()}
    log = MutationLog(eng)
    log.register("users", USERS, ("uid",))
    log.register("orders", ORDERS, ("oid",))
    return log, views, plans


def check(views, plans, log):
    tables = log.all_contents()
    for name, view in views.items():
        assert view.result() == oracle_result(plans[name], tables), (
            f"view {name} diverged from oracle"
        )


def test_insert_then_view_matches():
    log, views, plans = build()
    log.insert("users", (1, "ann", "west"))
    log.insert("orders", (100, 1, 5))
    assert views["revenue_by_region"].result() == {("west", 1, 5): 1}
    assert views["orders_per_user"].result() == {(1, 1): 1}
    check(views, plans, log)


def test_delete_by_key_retracts():
    log, views, plans = build()
    log.insert("users", (1, "ann", "west"))
    log.insert("orders", (100, 1, 5))
    log.delete("orders", (100,))  # key is the PK tuple
    assert views["revenue_by_region"].result() == {}
    assert views["orders_per_user"].result() == {}
    check(views, plans, log)


def test_update_amount_flows_to_aggregate():
    log, views, plans = build()
    log.insert("users", (1, "ann", "west"))
    log.insert("orders", (100, 1, 5))
    log.update("orders", (100, 1, 8))  # same PK, new amount
    assert views["revenue_by_region"].result() == {("west", 1, 8): 1}
    check(views, plans, log)


def test_update_join_key_moves_row_across_groups():
    """Updating an order's uid (the join key) must retract it from the old
    user's region and add it to the new one."""
    log, views, plans = build()
    log.insert("users", (1, "ann", "west"))
    log.insert("users", (2, "bob", "east"))
    log.insert("orders", (100, 1, 5))
    assert views["revenue_by_region"].result() == {("west", 1, 5): 1}
    log.update("orders", (100, 2, 5))  # move order from user 1 (west) to user 2 (east)
    assert views["revenue_by_region"].result() == {("east", 1, 5): 1}
    check(views, plans, log)


def test_delete_missing_key_raises():
    log, views, plans = build()
    with pytest.raises(KeyError):
        log.delete("orders", (999,))


@pytest.mark.parametrize("seed", range(15))
def test_inproc_matches_oracle(seed):
    rng = random.Random(seed)
    log, views, plans = build()
    live = {"users": {}, "orders": {}}
    next_oid = [0]

    def gen_users(uid):
        return (uid, rng.choice(["ann", "bob", "cat", "dan"]), rng.choice(["west", "east"]))

    def gen_orders(oid):
        return (oid, rng.randint(0, 3), rng.randint(-3, 9))

    do_check = check_every(seed)
    step = 0
    for step in range(400):
        table = rng.choice(["users", "orders"])
        pks = list(live[table])
        r = rng.random()
        if pks and r < 0.3:  # delete
            pk = rng.choice(pks)
            log.delete(table, pk)
            del live[table][pk]
        elif pks and r < 0.6:  # update (stable PK)
            pk = rng.choice(pks)
            row = gen_users(pk[0]) if table == "users" else gen_orders(pk[0])
            log.update(table, row)
            live[table][pk] = row
        else:  # insert with a fresh PK
            if table == "users":
                free = [u for u in range(4) if (u,) not in live["users"]]
                if not free:
                    continue
                uid = rng.choice(free)
                row = gen_users(uid)
                pk = (uid,)
            else:
                oid = next_oid[0]
                next_oid[0] += 1
                row = gen_orders(oid)
                pk = (oid,)
            log.insert(table, row)
            live[table][pk] = row
        if do_check(step):
            check(views, plans, log)

    # drain both tables to empty
    for table in ("orders", "users"):
        for pk in list(live[table]):
            log.delete(table, pk)
            del live[table][pk]
            step += 1
            if do_check(step):
                check(views, plans, log)
    for view in views.values():
        assert view.result() == {}
