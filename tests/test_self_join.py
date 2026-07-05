"""Milestone 3, KNOWN LIMITATION #1: self-joins / diamond plans.

A self-join makes ONE base table feed BOTH inputs of a join, so a single base
delta hits both sides at once and the delta-left ⋈ delta-right cross-term
appears — the case never exercised before. The engine propagates synchronously
and depth-first, so the two sides run sequentially: each side emits against the
other side's CURRENT state, then integrates its own. Whichever side runs second
sees the first side's integration, so the cross-term is counted exactly once,
in EITHER order. These tests verify that against the oracle instead of trusting
the argument.

The plan is the canonical employee -> manager self-join. A Project renames one
side's columns (id->mid, name->mname) so the join output has no duplicate names.
"""

import random

import pytest

from ivm.zset import ZSet
from ivm.plan import Source, Project, Join, Aggregate, Count
from ivm.engine import Engine

from harness import add, oracle_result, check_every

EMP = ("id", "name", "mgr_id")


def _managers():
    """emp projected to the manager side: (mid, mname)."""
    return Project(
        Source("emp", EMP),
        (("mid", lambda r: r["id"]), ("mname", lambda r: r["name"])),
    )


def manager_pairs_plan():
    # each employee row joined to the row of their manager: emp.mgr_id = mgr.id
    # output: (id, name, mgr_id, mname)
    return Join(Source("emp", EMP), _managers(), ("mgr_id",), ("mid",))


def test_self_join_manager_pairs_basic():
    eng = Engine()
    plan = manager_pairs_plan()
    view = eng.add_view("v", plan)
    tables: dict = {}
    for row in [(1, "ann", 3), (3, "bob", 3)]:  # bob (3) manages ann and himself
        d = ZSet({row: +1})
        eng.apply("emp", d)
        add(tables, "emp", d)
    assert view.result() == oracle_result(plan, tables)
    assert view.result() == {(1, "ann", 3, "bob"): 1, (3, "bob", 3, "bob"): 1}


def test_self_join_delete_manager_retracts_all_reports():
    eng = Engine()
    plan = manager_pairs_plan()
    view = eng.add_view("v", plan)
    tables: dict = {}
    for row in [(1, "ann", 3), (2, "cat", 3), (3, "bob", 3)]:
        d = ZSet({row: +1})
        eng.apply("emp", d)
        add(tables, "emp", d)
    assert view.result() == oracle_result(plan, tables)  # ann,cat,bob all -> bob
    # delete the manager row: every pair pointing at manager 3 must retract,
    # including the cross-term (bob's own self-pair)
    d = ZSet({(3, "bob", 3): -1})
    eng.apply("emp", d)
    add(tables, "emp", d)
    assert view.result() == oracle_result(plan, tables)
    assert view.result() == {}


def test_self_join_batched_delta_cross_term():
    """Two employees inserted in ONE delta, one managing the other and himself —
    the batch's own delta-left ⋈ delta-right cross-term must be captured."""
    eng = Engine()
    plan = manager_pairs_plan()
    view = eng.add_view("v", plan)
    tables: dict = {}
    d = ZSet({(1, "ann", 2): +1, (2, "bob", 2): +1})  # ann->bob(2), bob->bob(2)
    eng.apply("emp", d)
    add(tables, "emp", d)
    assert view.result() == oracle_result(plan, tables)
    assert view.result() == {(1, "ann", 2, "bob"): 1, (2, "bob", 2, "bob"): 1}


@pytest.mark.parametrize("seed", range(12))
def test_self_join_matches_oracle(seed):
    rng = random.Random(seed)
    eng = Engine()
    plan = manager_pairs_plan()
    view = eng.add_view("v", plan)
    tables: dict = {}
    live: list[tuple] = []
    do_check = check_every(seed)

    # small id pool so self-join matches heavily (many rows share ids / mgr_ids)
    step = 0
    for step in range(400):
        if live and rng.random() < 0.5:
            row = live.pop(rng.randrange(len(live)))
            delta = ZSet({row: -1})
        else:
            row = (rng.randint(0, 4), rng.choice(["ann", "bob", "cat"]), rng.randint(0, 4))
            live.append(row)
            delta = ZSet({row: +1})
        eng.apply("emp", delta)
        add(tables, "emp", delta)
        if do_check(step):
            assert view.result() == oracle_result(plan, tables)

    while live:
        row = live.pop()
        delta = ZSet({row: -1})
        eng.apply("emp", delta)
        add(tables, "emp", delta)
        step += 1
        if do_check(step):
            assert view.result() == oracle_result(plan, tables)
    assert view.result() == {}


@pytest.mark.parametrize("seed", range(6))
def test_self_join_into_aggregate(seed):
    """A self-join feeding an aggregate: count reports per manager name."""
    rng = random.Random(seed)
    plan = Aggregate(manager_pairs_plan(), ("mname",), (Count("reports"),))
    eng = Engine()
    view = eng.add_view("v", plan)
    tables: dict = {}
    live: list[tuple] = []
    do_check = check_every(seed)

    step = 0
    for step in range(300):
        if live and rng.random() < 0.5:
            row = live.pop(rng.randrange(len(live)))
            delta = ZSet({row: -1})
        else:
            row = (rng.randint(0, 4), rng.choice(["ann", "bob", "cat"]), rng.randint(0, 4))
            live.append(row)
            delta = ZSet({row: +1})
        eng.apply("emp", delta)
        add(tables, "emp", delta)
        if do_check(step):
            assert view.result() == oracle_result(plan, tables)

    while live:
        row = live.pop()
        delta = ZSet({row: -1})
        eng.apply("emp", delta)
        add(tables, "emp", delta)
        step += 1
        if do_check(step):
            assert view.result() == oracle_result(plan, tables)
    assert view.result() == {}
