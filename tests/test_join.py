"""Inner equi-join operator. The join is BILINEAR: its delta rule needs the full
state of BOTH inputs, so the operator retains both as indexed Z-sets. On a left
delta it emits (delta_left join current_right); on a right delta it emits
(current_left join delta_right); each side integrates its own delta after
emitting. Weights multiply (bag semantics), so a retract on either side must
cancel exactly the combined rows it produced — the delete cases are the whole
point of this operator.
"""

import random

import pytest

from ivm.zset import ZSet
from ivm.plan import Source, Join
from ivm.engine import Engine

from harness import add, oracle_result, check_every

USERS = ("uid", "uname")
ORDERS = ("oid", "uid", "amount")


def build():
    plan = Join(Source("users", USERS), Source("orders", ORDERS), ("uid",), ("uid",))
    eng = Engine()
    view = eng.add_view("user_orders", plan)
    return eng, view, plan


def test_join_basic_match():
    eng, view, plan = build()
    eng.apply("users", ZSet({(1, "ann"): +1}))
    eng.apply("orders", ZSet({(10, 1, 5): +1}))
    # output = left cols + right non-key cols: (uid, uname, oid, amount)
    assert view.result() == {(1, "ann", 10, 5): 1}


def test_join_no_match_emits_nothing():
    eng, view, plan = build()
    eng.apply("users", ZSet({(1, "ann"): +1}))
    eng.apply("orders", ZSet({(10, 2, 5): +1}))  # different uid
    assert view.result() == {}


def test_join_right_arriving_before_left_still_matches():
    eng, view, plan = build()
    eng.apply("orders", ZSet({(10, 1, 5): +1}))  # right first
    eng.apply("users", ZSet({(1, "ann"): +1}))
    assert view.result() == {(1, "ann", 10, 5): 1}


def test_join_delete_left_retracts_all_its_combinations():
    eng, view, plan = build()
    eng.apply("users", ZSet({(1, "ann"): +1}))
    eng.apply("orders", ZSet({(10, 1, 5): +1, (11, 1, 7): +1}))
    assert view.result() == {(1, "ann", 10, 5): 1, (1, "ann", 11, 7): 1}
    eng.apply("users", ZSet({(1, "ann"): -1}))  # retract the user
    assert view.result() == {}


def test_join_delete_right_retracts():
    eng, view, plan = build()
    eng.apply("users", ZSet({(1, "ann"): +1}))
    eng.apply("orders", ZSet({(10, 1, 5): +1}))
    eng.apply("orders", ZSet({(10, 1, 5): -1}))
    assert view.result() == {}


def test_join_weights_multiply():
    eng, view, plan = build()
    eng.apply("users", ZSet({(1, "ann"): +2}))  # weight 2
    eng.apply("orders", ZSet({(10, 1, 5): +1}))
    assert view.result() == {(1, "ann", 10, 5): 2}


@pytest.mark.parametrize("seed", range(20))
def test_join_matches_oracle(seed):
    rng = random.Random(seed)
    eng, view, plan = build()
    tables: dict = {}
    live = {"users": [], "orders": []}

    def mutate():
        table = rng.choice(["users", "orders"])
        pool = live[table]
        if pool and rng.random() < 0.45:
            row = pool.pop(rng.randrange(len(pool)))
            return table, ZSet({row: -1})
        if table == "users":
            row = (rng.randint(0, 3), rng.choice(["ann", "bob", "cat"]))
        else:
            row = (rng.randint(0, 20), rng.randint(0, 3), rng.randint(-3, 9))
        pool.append(row)
        return table, ZSet({row: +1})

    do_check = check_every(seed)
    step = 0
    for step in range(500):
        table, delta = mutate()
        eng.apply(table, delta)
        add(tables, table, delta)
        if do_check(step):
            assert view.result() == oracle_result(plan, tables)

    # drain both sides to empty
    for table in ("users", "orders"):
        while live[table]:
            row = live[table].pop()
            delta = ZSet({row: -1})
            eng.apply(table, delta)
            add(tables, table, delta)
            step += 1
            if do_check(step):
                assert view.result() == oracle_result(plan, tables)
    assert view.result() == {}
