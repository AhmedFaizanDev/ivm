"""Milestone 3.5: RIGHT OUTER JOIN — the mirror of LEFT.

Every RIGHT row appears: combined with each matching left row, or NULL-padded
once (left non-key columns = None) when its key has no left match. The join key
is coalesced (USING-style): an unmatched right row still carries its key value in
the shared key column, with the left non-key columns NULL. The flip happens on
LEFT-side transitions: a left insert/delete that takes a key from "no left" to
"some left" (or back) flips every right row at that key between padded and
matched.

Uniform output schema with inner/LEFT: (left_schema + right_non_key).
"""

import random

import pytest

from ivm.zset import ZSet
from ivm.plan import Source, RightJoin, Aggregate, Count
from ivm.engine import Engine

from harness import add, oracle_result, check_every

USERS = ("uid", "uname")
ORDERS = ("oid", "uid", "amount")


def build():
    # users RIGHT JOIN orders on uid: every ORDER appears; output (uid, uname, oid, amount).
    plan = RightJoin(Source("users", USERS), Source("orders", ORDERS), ("uid",), ("uid",))
    eng = Engine()
    view = eng.add_view("v", plan)
    return eng, view, plan


def test_unmatched_right_row_is_null_padded():
    eng, view, plan = build()
    eng.apply("orders", ZSet({(10, 1, 5): +1}))  # no user uid=1
    # key coalesced from the right (uid=1); left non-key (uname) is NULL
    assert view.result() == {(1, None, 10, 5): 1}


def test_match_appearing_flips_pad_to_matched():
    eng, view, plan = build()
    eng.apply("orders", ZSet({(10, 1, 5): +1}))
    assert view.result() == {(1, None, 10, 5): 1}
    eng.apply("users", ZSet({(1, "ann"): +1}))  # left match appears
    assert view.result() == {(1, "ann", 10, 5): 1}


def test_last_left_match_disappearing_flips_back():
    eng, view, plan = build()
    eng.apply("orders", ZSet({(10, 1, 5): +1}))
    eng.apply("users", ZSet({(1, "ann"): +1}))
    assert view.result() == {(1, "ann", 10, 5): 1}
    eng.apply("users", ZSet({(1, "ann"): -1}))  # last left match gone
    assert view.result() == {(1, None, 10, 5): 1}


def test_unmatched_left_row_is_dropped():
    eng, view, plan = build()
    eng.apply("users", ZSet({(1, "ann"): +1}))  # user with no orders
    assert view.result() == {}  # RIGHT join drops unmatched left rows


def test_two_right_rows_same_key_both_flip():
    eng, view, plan = build()
    eng.apply("orders", ZSet({(10, 1, 5): +1, (11, 1, 7): +1}))
    assert view.result() == {(1, None, 10, 5): 1, (1, None, 11, 7): 1}
    eng.apply("users", ZSet({(1, "ann"): +1}))
    assert view.result() == {(1, "ann", 10, 5): 1, (1, "ann", 11, 7): 1}
    eng.apply("users", ZSet({(1, "ann"): -1}))
    assert view.result() == {(1, None, 10, 5): 1, (1, None, 11, 7): 1}


@pytest.mark.parametrize("seed", range(15))
def test_right_join_matches_oracle(seed):
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
