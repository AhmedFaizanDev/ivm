"""Milestone 3.5: LEFT OUTER JOIN (the last tier-2 correctness challenge).

Every left row appears in the output: combined with each matching right row, or
NULL-padded once if it has no match. The bug farm is the FLIP — a left row must
move between its padded-NULL form and its matched form(s) exactly when its key
gains or loses its only right match. So a right-side insert/delete that takes a
key from "no matches" to "some match" (or back) must retract the padded rows and
assert the matched ones (or vice versa), for every left row at that key.

Checked against the recompute oracle over random insert/delete streams on BOTH
sides, with a small key pool so keys flip constantly.
"""

import random

import pytest

from ivm.zset import ZSet
from ivm.plan import Source, LeftJoin, Aggregate, Count
from ivm.engine import Engine

from harness import add, oracle_result, check_every

USERS = ("uid", "uname")
ORDERS = ("oid", "uid", "amount")


def build():
    # LEFT JOIN users -> orders on uid. Output: (uid, uname, oid, amount);
    # a user with no orders is (uid, uname, None, None).
    plan = LeftJoin(Source("users", USERS), Source("orders", ORDERS), ("uid",), ("uid",))
    eng = Engine()
    view = eng.add_view("v", plan)
    return eng, view, plan


def test_unmatched_left_row_is_null_padded():
    eng, view, plan = build()
    eng.apply("users", ZSet({(1, "ann"): +1}))
    assert view.result() == {(1, "ann", None, None): 1}


def test_match_appearing_flips_null_to_matched():
    eng, view, plan = build()
    eng.apply("users", ZSet({(1, "ann"): +1}))
    assert view.result() == {(1, "ann", None, None): 1}
    eng.apply("orders", ZSet({(10, 1, 5): +1}))  # first match appears
    assert view.result() == {(1, "ann", 10, 5): 1}  # null-pad retracted


def test_last_match_disappearing_flips_back_to_null():
    eng, view, plan = build()
    eng.apply("users", ZSet({(1, "ann"): +1}))
    eng.apply("orders", ZSet({(10, 1, 5): +1}))
    assert view.result() == {(1, "ann", 10, 5): 1}
    eng.apply("orders", ZSet({(10, 1, 5): -1}))  # last match gone
    assert view.result() == {(1, "ann", None, None): 1}  # flip back to padded


def test_multiple_matches_then_drain_to_null():
    eng, view, plan = build()
    eng.apply("users", ZSet({(1, "ann"): +1}))
    eng.apply("orders", ZSet({(10, 1, 5): +1, (11, 1, 7): +1}))
    assert view.result() == {(1, "ann", 10, 5): 1, (1, "ann", 11, 7): 1}
    eng.apply("orders", ZSet({(10, 1, 5): -1}))  # still one match -> no pad
    assert view.result() == {(1, "ann", 11, 7): 1}
    eng.apply("orders", ZSet({(11, 1, 7): -1}))  # now unmatched -> pad
    assert view.result() == {(1, "ann", None, None): 1}


def test_right_row_without_left_match_produces_nothing():
    eng, view, plan = build()
    eng.apply("orders", ZSet({(10, 1, 5): +1}))  # no such user
    assert view.result() == {}
    eng.apply("users", ZSet({(1, "ann"): +1}))  # now the user arrives
    assert view.result() == {(1, "ann", 10, 5): 1}


def test_delete_unmatched_left_row_retracts_pad():
    eng, view, plan = build()
    eng.apply("users", ZSet({(1, "ann"): +1}))
    assert view.result() == {(1, "ann", None, None): 1}
    eng.apply("users", ZSet({(1, "ann"): -1}))
    assert view.result() == {}


def test_two_left_rows_same_key_both_flip():
    """A single right match must flip EVERY left row at that key."""
    eng, view, plan = build()
    eng.apply("users", ZSet({(1, "ann"): +1, (1, "bob"): +1}))
    assert view.result() == {(1, "ann", None, None): 1, (1, "bob", None, None): 1}
    eng.apply("orders", ZSet({(10, 1, 5): +1}))
    assert view.result() == {(1, "ann", 10, 5): 1, (1, "bob", 10, 5): 1}
    eng.apply("orders", ZSet({(10, 1, 5): -1}))
    assert view.result() == {(1, "ann", None, None): 1, (1, "bob", None, None): 1}


@pytest.mark.parametrize("seed", range(15))
def test_left_join_matches_oracle(seed):
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
            else:  # small uid pool so matches appear/disappear constantly
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


@pytest.mark.parametrize("seed", range(6))
def test_left_join_into_aggregate_matches_oracle(seed):
    """Flip deltas (retract padded / assert matched) must compose downstream:
    COUNT per user name over the left join, including unmatched (padded) users."""
    rng = random.Random(seed)
    plan = Aggregate(
        LeftJoin(Source("users", USERS), Source("orders", ORDERS), ("uid",), ("uid",)),
        ("uname",),
        (Count("n"),),
    )
    eng = Engine()
    view = eng.add_view("v", plan)
    tables: dict = {}
    live = {"users": [], "orders": []}
    do_check = check_every(seed)

    step = 0
    for step in range(300):
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
