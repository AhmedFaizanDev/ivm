"""Adversarial probe for snapshot/restore — hunt for state that doesn't
round-trip. Strategy: snapshot -> real pickle -> restore into a FRESH engine at
EVERY step (not just once), so the round-trip is exercised at every state
configuration: mid MIN/MAX multiset, the instant a group vanishes, an empty
engine, NULL-padded outer-join rows, floats and negatives in the state. A
restored engine must always equal the running one.

(Aggregate columns are kept non-NULL here: SUM/MIN/MAX over a NULL value is a
separate, logged engine limitation, not a serialization issue.)
"""

import pickle
import random

import pytest

from ivm.zset import ZSet
from ivm.plan import Source, Join, LeftJoin, RightJoin, FullJoin, Aggregate, Count, Sum, Min, Max
from ivm.engine import Engine

from harness import add, oracle_result

USERS = ("uid", "uname")
ORDERS = ("oid", "uid", "amount")


def _roundtrip(eng, add_view_fn):
    blob = pickle.loads(pickle.dumps(eng.snapshot()))
    fresh = Engine()
    fview = add_view_fn(fresh)
    fresh.restore(blob)
    return fview


def inner_agg_plan():
    return Aggregate(
        Join(Source("orders", ORDERS), Source("users", USERS), ("uid",), ("uid",)),
        ("uname",),
        (Count("n"), Sum("s", "amount"), Min("lo", "amount"), Max("hi", "amount")),
    )


@pytest.mark.parametrize("seed", range(4))
def test_inner_join_aggregate_roundtrip_every_step(seed):
    rng = random.Random(seed)
    plan = inner_agg_plan()
    eng = Engine()
    view = eng.add_view("v", plan)
    tables: dict = {}
    live = {"users": [], "orders": []}
    next_oid = [0]

    for step in range(150):
        table = rng.choice(["users", "orders"])
        pool = live[table]
        if pool and rng.random() < 0.45:
            row = pool.pop(rng.randrange(len(pool)))
            d = ZSet({row: -1})
        elif table == "users":
            row = (rng.randint(0, 3), rng.choice(["ann", "bob", "cat"]))
            pool.append(row)
            d = ZSet({row: +1})
        else:
            row = (next_oid[0], rng.randint(0, 3), rng.randint(-5, 9))
            next_oid[0] += 1
            pool.append(row)
            d = ZSet({row: +1})
        eng.apply(table, d)
        add(tables, table, d)

        fview = _roundtrip(eng, lambda e: e.add_view("v", plan))
        assert fview.result() == view.result()
        if step % 15 == 0:
            assert view.result() == oracle_result(plan, tables)


@pytest.mark.parametrize("join_cls", [Join, LeftJoin, RightJoin, FullJoin])
@pytest.mark.parametrize("seed", range(3))
def test_raw_join_roundtrip_with_nulls_floats(join_cls, seed):
    """Raw join (no aggregate) so NULL-padded rows, floats and negatives live in
    the view + index state and must round-trip."""
    rng = random.Random(seed)
    plan = join_cls(Source("orders", ORDERS), Source("users", USERS), ("uid",), ("uid",))
    eng = Engine()
    view = eng.add_view("v", plan)
    tables: dict = {}
    live = {"users": [], "orders": []}
    next_oid = [0]

    for step in range(120):
        table = rng.choice(["users", "orders"])
        pool = live[table]
        if pool and rng.random() < 0.45:
            row = pool.pop(rng.randrange(len(pool)))
            d = ZSet({row: -1})
        elif table == "users":
            row = (rng.randint(0, 3), rng.choice(["ann", None, "cat"]))  # NULL name
            pool.append(row)
            d = ZSet({row: +1})
        else:
            row = (next_oid[0], rng.randint(0, 3), rng.choice([1.5, -2, None, 7]))
            next_oid[0] += 1
            pool.append(row)
            d = ZSet({row: +1})
        eng.apply(table, d)
        add(tables, table, d)

        fview = _roundtrip(eng, lambda e: e.add_view("v", plan))
        assert fview.result() == view.result()
        if step % 15 == 0:
            assert view.result() == oracle_result(plan, tables)


def test_multi_view_snapshot_restore():
    plan_a = Aggregate(Source("t", ("id", "cat", "amt")), ("cat",), (Count("n"), Sum("s", "amt")))
    plan_b = Join(Source("t", ("id", "cat", "amt")), Source("u", ("cat", "label")), ("cat",), ("cat",))

    def build(e):
        return e.add_view("a", plan_a), e.add_view("b", plan_b)

    eng = Engine()
    va, vb = build(eng)
    eng.apply("t", ZSet({(1, "x", 5): +1, (2, "x", 3): +1, (3, "y", 9): +1}))
    eng.apply("u", ZSet({("x", "X-LABEL"): +1}))

    blob = pickle.loads(pickle.dumps(eng.snapshot()))
    fresh = Engine()
    fa, fb = build(fresh)
    fresh.restore(blob)
    assert fa.result() == va.result()
    assert fb.result() == vb.result()
    # and it keeps maintaining correctly afterward
    fresh.apply("u", ZSet({("y", "Y-LABEL"): +1}))
    eng.apply("u", ZSet({("y", "Y-LABEL"): +1}))
    assert fa.result() == va.result()
    assert fb.result() == vb.result()


def test_empty_engine_snapshot():
    plan = inner_agg_plan()
    eng = Engine()
    view = eng.add_view("v", plan)
    blob = pickle.loads(pickle.dumps(eng.snapshot()))
    fresh = Engine()
    fview = fresh.add_view("v", plan)
    fresh.restore(blob)
    assert fview.result() == {} == view.result()


def test_restore_into_wrong_shape_raises():
    from ivm.plan import Filter
    eng = Engine()
    eng.add_view("v", inner_agg_plan())
    blob = eng.snapshot()
    other = Engine()
    other.add_view("v", Filter(Source("orders", ORDERS), lambda r: True))  # different graph
    with pytest.raises(ValueError):
        other.restore(blob)
