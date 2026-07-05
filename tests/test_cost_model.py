"""Milestone 3, the hybrid cost model — EXPERIMENTAL.

Carry the Enzyme idea into the embeddable setting: before each refresh, estimate
the cost of applying deltas incrementally versus recomputing the view from
scratch, and take the cheaper path. Research caveat (in the build plan): no
public source documents how production systems make this decision, so this is a
heuristic experiment, not a validated recipe.

The correctness bar is unchanged and absolute: WHICHEVER path is chosen, the
maintained view must equal the recompute oracle. The cost model may only change
which path runs, never the answer. These tests assert that on every refresh, and
separately assert the model actually falls back to recompute on a bulk update.
"""

import random

import pytest

from ivm.zset import ZSet
from ivm.plan import Source, Join, Aggregate, Count, Sum
from ivm.oracle import eval_plan
from ivm.cost_model import HybridView

T = ("id", "cat", "amt")


def oracle(plan, tables):
    return dict(eval_plan(plan, tables)[1].items())


def add(tables, table, delta):
    tables[table] = tables.get(table, ZSet()) + delta
    return tables


def cat_totals_plan():
    return Aggregate(Source("t", T), ("cat",), (Count("n"), Sum("s", "amt")))


def test_bulk_update_falls_back_to_recompute_but_stays_correct():
    plan = cat_totals_plan()
    hv = HybridView(plan, recompute_threshold=0.5)
    tables: dict = {}

    # 1) initial bulk load into an empty view -> recompute
    load = ZSet({(i, "a" if i % 2 else "b", i): +1 for i in range(100)})
    hv.refresh({"t": load})
    add(tables, "t", load)
    assert hv.last_strategy == "recompute"
    assert hv.result() == oracle(plan, tables)

    # 2) a tiny refresh against a large base -> incremental
    small = ZSet({(500, "a", 5): +1, (501, "b", 7): +1})
    hv.refresh({"t": small})
    add(tables, "t", small)
    assert hv.last_strategy == "incremental"
    assert hv.result() == oracle(plan, tables)

    # 3) a bulk update touching most rows (delete old + insert new) -> recompute
    bulk = ZSet()
    for i in range(100):
        bulk = bulk + ZSet({(i, "a" if i % 2 else "b", i): -1, (i, "c", i * 2): +1})
    hv.refresh({"t": bulk})
    add(tables, "t", bulk)
    assert hv.last_strategy == "recompute"
    assert hv.result() == oracle(plan, tables)


def test_small_refresh_prefers_incremental():
    plan = cat_totals_plan()
    hv = HybridView(plan, recompute_threshold=0.5)
    tables: dict = {}
    load = ZSet({(i, "a", i): +1 for i in range(50)})
    hv.refresh({"t": load})
    add(tables, "t", load)
    hv.refresh({"t": ZSet({(999, "b", 1): +1})})
    add(tables, "t", ZSet({(999, "b", 1): +1}))
    assert hv.last_strategy == "incremental"
    assert hv.result() == oracle(plan, tables)


@pytest.mark.parametrize("seed", range(10))
def test_hybrid_matches_oracle_under_mixed_refreshes(seed):
    """Random mix of small and bulk refreshes; both strategies must always agree
    with the oracle over the accumulated contents."""
    rng = random.Random(seed)
    plan = cat_totals_plan()
    hv = HybridView(plan, recompute_threshold=0.5)
    tables: dict = {}
    live: list[tuple] = []
    next_id = [0]
    saw = {"incremental": 0, "recompute": 0}

    for _ in range(40):
        if live and rng.random() < 0.3:
            # bulk: delete a large slice at once
            k = min(len(live), rng.randint(5, 30))
            batch = ZSet()
            for _ in range(k):
                row = live.pop(rng.randrange(len(live)))
                batch = batch + ZSet({row: -1})
        else:
            # small-to-medium insert batch
            batch = ZSet()
            for _ in range(rng.randint(1, 6)):
                row = (next_id[0], rng.choice(["a", "b", "c"]), rng.randint(-3, 9))
                next_id[0] += 1
                live.append(row)
                batch = batch + ZSet({row: +1})
        hv.refresh({"t": batch})
        add(tables, "t", batch)
        saw[hv.last_strategy] += 1
        assert hv.result() == oracle(plan, tables)

    # both code paths should have run at least once over the whole stream
    assert saw["incremental"] > 0
    assert saw["recompute"] > 0


@pytest.mark.parametrize("seed", range(6))
def test_hybrid_over_join_matches_oracle(seed):
    """The cost model over a join+aggregate view, mixed refreshes on two tables."""
    rng = random.Random(seed)
    users = ("uid", "region")
    orders = ("oid", "uid", "amount")
    plan = Aggregate(
        Join(Source("orders", orders), Source("users", users), ("uid",), ("uid",)),
        ("region",),
        (Count("n"), Sum("s", "amount")),
    )
    hv = HybridView(plan, recompute_threshold=0.5)
    tables: dict = {}
    live = {"users": [], "orders": []}
    next_oid = [0]

    for _ in range(40):
        table = rng.choice(["users", "orders"])
        pool = live[table]
        if pool and rng.random() < 0.35:
            k = min(len(pool), rng.randint(1, 12))
            batch = ZSet()
            for _ in range(k):
                row = pool.pop(rng.randrange(len(pool)))
                batch = batch + ZSet({row: -1})
        else:
            batch = ZSet()
            for _ in range(rng.randint(1, 5)):
                if table == "users":
                    row = (rng.randint(0, 3), rng.choice(["west", "east"]))
                else:
                    row = (next_oid[0], rng.randint(0, 3), rng.randint(-3, 9))
                    next_oid[0] += 1
                pool.append(row)
                batch = batch + ZSet({row: +1})
        hv.refresh({table: batch})
        add(tables, table, batch)
        assert hv.result() == oracle(plan, tables)
