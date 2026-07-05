"""Milestone 3.5: FULL OUTER JOIN — preserve BOTH sides.

Every left row AND every right row appears: matched rows combined; unmatched
left rows NULL-padded on the right; unmatched right rows NULL-padded on the left
(with the key coalesced from the right). A key can hold left-only rows (left-
padded), right-only rows (right-padded), or matched rows — never left-pads and
right-pads at once, since any key with both sides present is fully matched.

Both sides flip: a left change flips the right rows at a key between padded and
matched; a right change flips the left rows. Uniform output schema with the
other joins: (left_schema + right_non_key).
"""

import random

import pytest

from ivm.zset import ZSet
from ivm.plan import Source, FullJoin
from ivm.engine import Engine

from harness import add, oracle_result, check_every

USERS = ("uid", "uname")
ORDERS = ("oid", "uid", "amount")


def build():
    plan = FullJoin(Source("users", USERS), Source("orders", ORDERS), ("uid",), ("uid",))
    eng = Engine()
    view = eng.add_view("v", plan)
    return eng, view, plan


def test_unmatched_left_is_left_padded():
    eng, view, plan = build()
    eng.apply("users", ZSet({(1, "ann"): +1}))
    assert view.result() == {(1, "ann", None, None): 1}


def test_unmatched_right_is_right_padded():
    eng, view, plan = build()
    eng.apply("orders", ZSet({(10, 2, 5): +1}))  # no user uid=2
    assert view.result() == {(2, None, 10, 5): 1}


def test_left_only_and_right_only_coexist():
    eng, view, plan = build()
    eng.apply("users", ZSet({(1, "ann"): +1}))  # uid 1, no order
    eng.apply("orders", ZSet({(10, 2, 5): +1}))  # uid 2, no user
    assert view.result() == {(1, "ann", None, None): 1, (2, None, 10, 5): 1}


def test_match_absorbs_both_pads():
    eng, view, plan = build()
    eng.apply("users", ZSet({(1, "ann"): +1}))
    assert view.result() == {(1, "ann", None, None): 1}  # left-padded
    eng.apply("orders", ZSet({(10, 1, 5): +1}))  # arrives at same key -> match
    assert view.result() == {(1, "ann", 10, 5): 1}  # both pads gone, matched


def test_delete_left_flips_order_to_right_padded():
    eng, view, plan = build()
    eng.apply("users", ZSet({(1, "ann"): +1}))
    eng.apply("orders", ZSet({(10, 1, 5): +1}))
    assert view.result() == {(1, "ann", 10, 5): 1}
    eng.apply("users", ZSet({(1, "ann"): -1}))  # left gone -> order right-padded
    assert view.result() == {(1, None, 10, 5): 1}


def test_delete_right_flips_user_to_left_padded():
    eng, view, plan = build()
    eng.apply("users", ZSet({(1, "ann"): +1}))
    eng.apply("orders", ZSet({(10, 1, 5): +1}))
    assert view.result() == {(1, "ann", 10, 5): 1}
    eng.apply("orders", ZSet({(10, 1, 5): -1}))  # right gone -> user left-padded
    assert view.result() == {(1, "ann", None, None): 1}


def test_right_first_then_left():
    eng, view, plan = build()
    eng.apply("orders", ZSet({(10, 1, 5): +1}))
    assert view.result() == {(1, None, 10, 5): 1}  # right-padded
    eng.apply("users", ZSet({(1, "ann"): +1}))
    assert view.result() == {(1, "ann", 10, 5): 1}  # matched


@pytest.mark.parametrize("seed", range(15))
def test_full_join_matches_oracle(seed):
    rng = random.Random(seed)
    eng, view, plan = build()
    tables: dict = {}
    live = {"users": [], "orders": []}
    do_check = check_every(seed)

    step = 0
    for step in range(400):
        table = rng.choice(["users", "orders"])
        pool = live[table]
        if pool and rng.random() < 0.45:
            row = pool.pop(rng.randrange(len(pool)))
            delta = ZSet({row: -1})
        else:
            if table == "users":
                row = (rng.randint(0, 3), rng.choice(["ann", "bob", "cat"]))
            else:
                row = (rng.randint(0, 30), rng.randint(0, 3), rng.randint(-3, 9))
            pool.append(row)
            delta = ZSet({row: +1})
        eng.apply(table, delta)
        add(tables, table, delta)
        if do_check(step):
            assert view.result() == oracle_result(plan, tables)

    for table in ("orders", "users"):
        while live[table]:
            row = live[table].pop()
            delta = ZSet({row: -1})
            eng.apply(table, delta)
            add(tables, table, delta)
            step += 1
            if do_check(step):
                assert view.result() == oracle_result(plan, tables)
    assert view.result() == {}
