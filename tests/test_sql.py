"""SQL front-end: parsed SQL must compile to a plan that means the same thing as
the hand-built plan. Primary check: run the COMPILED plan through the engine over
random insert/delete streams and assert it equals the recompute oracle of the
trusted HAND-BUILT plan — so the compiler both emits a correct plan and preserves
meaning. (SQLite is used as an independent SQL-semantics oracle in
test_sql_vs_sqlite.py.)
"""

import random

import pytest

from ivm.zset import ZSet
from ivm.plan import (
    Source, Filter, Project, Aggregate, Count, Sum, Avg, Min, Max,
    Join, LeftJoin, RightJoin, FullJoin,
)
from ivm.engine import Engine
from ivm.sql import compile_sql

from harness import add, oracle_result, check_every

T = ("id", "cat", "amount")
CATALOG = {"t": T}
USERS = ("uid", "uname")
ORDERS = ("oid", "uid", "amount")
JOIN_CATALOG = {"users": USERS, "orders": ORDERS}


# --- unit: exact results on tiny data ----------------------------------------


def _run(sql, rows, catalog=CATALOG, table="t"):
    plan = compile_sql(sql, catalog)
    eng = Engine()
    view = eng.add_view("v", plan)
    for row, w in rows:
        eng.apply(table, ZSet({row: w}))
    return view.result()


def test_engine_add_sql_view_convenience():
    eng = Engine()
    view = eng.add_sql_view("v", "SELECT cat, SUM(amount) AS s FROM t GROUP BY cat", CATALOG)
    eng.apply("t", ZSet({(1, "a", 5): +1, (2, "a", 3): +1}))
    assert view.result() == {("a", 8): 1}


def test_select_star_is_identity():
    assert _run("SELECT * FROM t", [((1, "a", 5), +1)]) == {(1, "a", 5): 1}


def test_select_columns_projects():
    assert _run("SELECT cat, amount FROM t", [((1, "a", 5), +1)]) == {("a", 5): 1}


def test_select_alias_keeps_values():
    assert _run("SELECT cat AS c, amount AS amt FROM t", [((1, "a", 5), +1)]) == {("a", 5): 1}


def test_qualified_column():
    assert _run("SELECT t.cat FROM t", [((1, "a", 5), +1)]) == {("a",): 1}


def test_where_greater_than():
    rows = [((1, "a", 5), +1), ((2, "a", 1), +1)]
    assert _run("SELECT * FROM t WHERE amount > 3", rows) == {(1, "a", 5): 1}


def test_where_and():
    rows = [((1, "a", 5), +1), ((2, "b", 5), +1)]
    assert _run("SELECT * FROM t WHERE amount > 3 AND cat = 'a'", rows) == {(1, "a", 5): 1}


def test_where_or():
    rows = [((1, "a", 5), +1), ((2, "b", 1), +1), ((3, "c", 0), +1)]
    got = _run("SELECT id FROM t WHERE amount > 3 OR cat = 'b'", rows)
    assert got == {(1,): 1, (2,): 1}


def test_where_string_equality_and_projection():
    rows = [((1, "food", 5), +1), ((2, "toys", 7), +1)]
    assert _run("SELECT amount FROM t WHERE cat = 'food'", rows) == {(5,): 1}


def test_where_null_operand_excluded_not_crash():
    rows = [((1, "a", None), +1), ((2, "a", 5), +1)]
    assert _run("SELECT id FROM t WHERE amount > 3", rows) == {(2,): 1}


def test_where_is_null():
    rows = [((1, "a", None), +1), ((2, "a", 5), +1)]
    assert _run("SELECT id FROM t WHERE amount IS NULL", rows) == {(1,): 1}


def test_where_is_not_null():
    rows = [((1, "a", None), +1), ((2, "a", 5), +1)]
    assert _run("SELECT id FROM t WHERE amount IS NOT NULL", rows) == {(2,): 1}


def test_parenthesized_where():
    rows = [((1, "a", 5), +1), ((2, "b", 5), +1), ((3, "c", 5), +1), ((4, "a", -1), +1)]
    got = _run("SELECT id FROM t WHERE (cat = 'a' OR cat = 'b') AND amount > 0", rows)
    assert got == {(1,): 1, (2,): 1}


def test_lowercase_keywords():
    assert _run("select cat from t where amount > 3", [((1, "a", 5), +1)]) == {("a",): 1}


def test_negative_literal_in_where():
    rows = [((1, "a", -5), +1), ((2, "a", 2), +1)]
    assert _run("SELECT id FROM t WHERE amount > -1", rows) == {(2,): 1}


def test_unknown_column_raises():
    from ivm.sql import SqlError
    with pytest.raises(SqlError):
        compile_sql("SELECT nope FROM t", CATALOG)


def test_unknown_table_raises():
    from ivm.sql import SqlError
    with pytest.raises(SqlError):
        compile_sql("SELECT * FROM nosuch", CATALOG)


# --- property: compiled plan == oracle of the hand-built plan -----------------

PAIRS = [
    ("SELECT * FROM t", Source("t", T)),
    (
        "SELECT cat, amount FROM t",
        Project(Source("t", T), (("cat", lambda r: r["cat"]), ("amount", lambda r: r["amount"]))),
    ),
    ("SELECT * FROM t WHERE amount > 0", Filter(Source("t", T), lambda r: r["amount"] > 0)),
    (
        "SELECT id, cat FROM t WHERE amount > 2 AND cat <> 'z'",
        Project(
            Filter(Source("t", T), lambda r: r["amount"] > 2 and r["cat"] != "z"),
            (("id", lambda r: r["id"]), ("cat", lambda r: r["cat"])),
        ),
    ),
]


@pytest.mark.parametrize("sql,hand", PAIRS)
@pytest.mark.parametrize("seed", range(6))
def test_compiled_sql_matches_hand_built(sql, hand, seed):
    rng = random.Random(seed)
    plan = compile_sql(sql, CATALOG)
    eng = Engine()
    view = eng.add_view("v", plan)
    tables: dict = {}
    live: list = []
    do_check = check_every(seed)

    step = 0
    for step in range(200):
        if live and rng.random() < 0.45:
            row = live.pop(rng.randrange(len(live)))
            delta = ZSet({row: -1})
        else:
            row = (rng.randint(0, 50), rng.choice(["a", "b", "z"]), rng.randint(-2, 6))
            live.append(row)
            delta = ZSet({row: +1})
        eng.apply("t", delta)
        add(tables, "t", delta)
        if do_check(step):
            assert view.result() == oracle_result(hand, tables)

    while live:
        row = live.pop()
        delta = ZSet({row: -1})
        eng.apply("t", delta)
        add(tables, "t", delta)
        step += 1
        if do_check(step):
            assert view.result() == oracle_result(hand, tables)
    assert view.result() == {}


# --- aggregates / GROUP BY ----------------------------------------------------

AGG_PAIRS = [
    (
        "SELECT cat, COUNT(*) AS n, SUM(amount) AS s FROM t GROUP BY cat",
        Aggregate(Source("t", T), ("cat",), (Count("n"), Sum("s", "amount"))),
    ),
    (
        "SELECT cat, AVG(amount) AS a, MIN(amount) AS lo, MAX(amount) AS hi FROM t GROUP BY cat",
        Aggregate(Source("t", T), ("cat",),
                  (Avg("a", "amount"), Min("lo", "amount"), Max("hi", "amount"))),
    ),
    (
        "SELECT cat, SUM(amount) AS s FROM t WHERE amount > 0 GROUP BY cat",
        Aggregate(Filter(Source("t", T), lambda r: r["amount"] > 0), ("cat",), (Sum("s", "amount"),)),
    ),
]


@pytest.mark.parametrize("sql,hand", AGG_PAIRS)
@pytest.mark.parametrize("seed", range(6))
def test_compiled_aggregate_matches_hand_built(sql, hand, seed):
    rng = random.Random(seed)
    eng = Engine()
    view = eng.add_view("v", compile_sql(sql, CATALOG))
    tables: dict = {}
    live: list = []
    do_check = check_every(seed)

    step = 0
    for step in range(250):
        if live and rng.random() < 0.45:
            row = live.pop(rng.randrange(len(live)))
            delta = ZSet({row: -1})
        else:
            row = (rng.randint(0, 50), rng.choice(["a", "b", "c"]), rng.randint(-3, 8))
            live.append(row)
            delta = ZSet({row: +1})
        eng.apply("t", delta)
        add(tables, "t", delta)
        if do_check(step):
            assert view.result() == oracle_result(hand, tables)

    while live:
        row = live.pop()
        delta = ZSet({row: -1})
        eng.apply("t", delta)
        add(tables, "t", delta)
        step += 1
        if do_check(step):
            assert view.result() == oracle_result(hand, tables)
    assert view.result() == {}


# --- joins (all four kinds) ---------------------------------------------------

JOIN_PAIRS = [
    (
        "SELECT * FROM orders JOIN users ON orders.uid = users.uid",
        Join(Source("orders", ORDERS), Source("users", USERS), ("uid",), ("uid",)),
    ),
    (
        "SELECT * FROM orders LEFT JOIN users ON orders.uid = users.uid",
        LeftJoin(Source("orders", ORDERS), Source("users", USERS), ("uid",), ("uid",)),
    ),
    (
        "SELECT * FROM users RIGHT JOIN orders ON users.uid = orders.uid",
        RightJoin(Source("users", USERS), Source("orders", ORDERS), ("uid",), ("uid",)),
    ),
    (
        "SELECT * FROM users FULL OUTER JOIN orders ON users.uid = orders.uid",
        FullJoin(Source("users", USERS), Source("orders", ORDERS), ("uid",), ("uid",)),
    ),
]


@pytest.mark.parametrize("sql,hand", JOIN_PAIRS)
@pytest.mark.parametrize("seed", range(8))
def test_compiled_join_matches_hand_built(sql, hand, seed):
    rng = random.Random(seed)
    eng = Engine()
    view = eng.add_view("v", compile_sql(sql, JOIN_CATALOG))
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
            assert view.result() == oracle_result(hand, tables)

    for table in ("orders", "users"):
        while live[table]:
            row = live[table].pop()
            delta = ZSet({row: -1})
            eng.apply(table, delta)
            add(tables, table, delta)
            step += 1
            if do_check(step):
                assert view.result() == oracle_result(hand, tables)
    assert view.result() == {}


def test_join_where_groupby_end_to_end():
    """A realistic query: filter, join, group, aggregate — exact result."""
    sql = ("SELECT uname, COUNT(*) AS orders, SUM(amount) AS spend "
           "FROM users JOIN orders ON users.uid = orders.uid "
           "WHERE amount > 0 GROUP BY uname")
    eng = Engine()
    view = eng.add_view("v", compile_sql(sql, JOIN_CATALOG))
    eng.apply("users", ZSet({(1, "ann"): +1, (2, "bob"): +1}))
    eng.apply("orders", ZSet({(10, 1, 5): +1, (11, 1, 3): +1, (12, 2, -9): +1, (13, 2, 4): +1}))
    # ann: 2 orders >0 (5,3) -> spend 8; bob: 1 order >0 (4) -> spend 4 (the -9 filtered out)
    assert view.result() == {("ann", 2, 8): 1, ("bob", 1, 4): 1}
