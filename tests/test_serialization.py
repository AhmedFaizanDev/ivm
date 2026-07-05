"""Serialization / resumability: a process restart must not force a full
recompute. `engine.snapshot()` captures every operator's state (join indexes,
aggregate accumulators + MIN/MAX multisets) and each view's materialized Z-set;
after rebuilding the same views in a fresh engine, `engine.restore(snap)` injects
that state. The bar: a restored engine, fed the rest of a random stream, must
stay byte-for-byte identical to an engine that was never restarted — and to the
recompute oracle. The snapshot must also survive a real `pickle` round-trip.
"""

import pickle
import random

import pytest

from ivm.zset import ZSet
from ivm.plan import Source, Join, Aggregate, Count, Sum, Min, Max
from ivm.engine import Engine

from harness import add, oracle_result, check_every

USERS = ("uid", "uname")
ORDERS = ("oid", "uid", "amount")


def make_plan():
    joined = Join(Source("orders", ORDERS), Source("users", USERS), ("uid",), ("uid",))
    return Aggregate(
        joined,
        ("uname",),
        (Count("n"), Sum("s", "amount"), Min("lo", "amount"), Max("hi", "amount")),
    )


def _generate_stream(rng, n):
    live = {"users": [], "orders": []}
    next_oid = [0]
    ops = []
    for _ in range(n):
        table = rng.choice(["users", "orders"])
        pool = live[table]
        if pool and rng.random() < 0.45:
            row = pool.pop(rng.randrange(len(pool)))
            ops.append((table, ZSet({row: -1})))
        else:
            if table == "users":
                row = (rng.randint(0, 3), rng.choice(["ann", "bob", "cat"]))
            else:
                row = (next_oid[0], rng.randint(0, 3), rng.randint(-5, 9))
                next_oid[0] += 1
            pool.append(row)
            ops.append((table, ZSet({row: +1})))
    # a drain tail so both engines end empty
    for table in ("orders", "users"):
        for row in live[table]:
            ops.append((table, ZSet({row: -1})))
    return ops


@pytest.mark.parametrize("seed", range(10))
def test_restore_then_continue_equals_never_restarted(seed):
    rng = random.Random(seed)
    plan = make_plan()
    ops = _generate_stream(rng, 300)
    split = len(ops) // 2

    never = Engine()
    v_never = never.add_view("v", plan)
    snapshotted = Engine()
    v_snap = snapshotted.add_view("v", plan)

    tables: dict = {}
    for table, delta in ops[:split]:
        never.apply(table, delta)
        snapshotted.apply(table, delta)
        add(tables, table, delta)

    # snapshot -> real pickle round-trip -> restore into a FRESH engine
    blob = pickle.loads(pickle.dumps(snapshotted.snapshot()))
    restored = Engine()
    v_restored = restored.add_view("v", plan)
    restored.restore(blob)

    # restore reproduces the exact maintained state, matching oracle + the running engines
    assert v_restored.result() == v_never.result()
    assert v_restored.result() == oracle_result(plan, tables)

    # feed the REST of the stream to the never-restarted and the restored engines
    do_check = check_every(seed)
    step = 0
    for table, delta in ops[split:]:
        never.apply(table, delta)
        restored.apply(table, delta)
        add(tables, table, delta)
        step += 1
        if do_check(step):
            assert v_restored.result() == v_never.result()
            assert v_restored.result() == oracle_result(plan, tables)

    assert v_restored.result() == {}
    assert v_never.result() == {}


def test_snapshot_is_a_point_in_time_copy():
    """A snapshot taken now must not change as the engine keeps running."""
    plan = make_plan()
    eng = Engine()
    view = eng.add_view("v", plan)
    eng.apply("users", ZSet({(1, "ann"): +1}))
    eng.apply("orders", ZSet({(10, 1, 5): +1}))
    blob = eng.snapshot()
    before = pickle.dumps(blob)
    # keep mutating after snapshot
    eng.apply("orders", ZSet({(11, 1, 99): +1}))
    eng.apply("users", ZSet({(2, "bob"): +1}))
    assert pickle.dumps(blob) == before  # snapshot object is frozen at capture time
