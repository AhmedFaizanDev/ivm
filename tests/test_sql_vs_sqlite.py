"""Independent SQL-semantics oracle: run the SAME SQL against stdlib SQLite over
the SAME data and compare to the incrementally maintained view. This checks that
our front-end actually means SQL — a bug the self-consistency tests (engine ==
our own recompute oracle) cannot catch, because they'd agree with each other
even if our SQL semantics were wrong.

Rows use unique primary keys (delete by key) so both sides stay in lockstep with
no duplicate-row ambiguity. RIGHT/FULL use COALESCE on the SQLite side to match
our coalesced (USING-style) key representation. Global (GROUP BY-less) aggregates
are intentionally excluded: on an empty relation SQL yields one zero row while an
incremental group vanishes — a documented divergence.
"""

import random
import sqlite3
from collections import Counter

import pytest

from ivm.zset import ZSet
from ivm.engine import Engine
from ivm.sql import compile_sql


def _ms_view(view):
    return Counter(view.result())


def _ms_sqlite(conn, sql):
    return Counter(tuple(r) for r in conn.execute(sql).fetchall())


# --- single table: projection / filter / grouped aggregate -------------------

SINGLE = [
    ("SELECT id, cat FROM t WHERE amount > 2",
     "SELECT id, cat FROM t WHERE amount > 2"),
    ("SELECT amount FROM t WHERE cat = 'a' OR amount < 0",
     "SELECT amount FROM t WHERE cat = 'a' OR amount < 0"),
    ("SELECT cat, COUNT(*) AS n, SUM(amount) AS s, MIN(amount) AS lo, MAX(amount) AS hi FROM t GROUP BY cat",
     "SELECT cat, COUNT(*), SUM(amount), MIN(amount), MAX(amount) FROM t GROUP BY cat"),
]


@pytest.mark.parametrize("my_sql,lite_sql", SINGLE)
@pytest.mark.parametrize("seed", range(6))
def test_single_table_matches_sqlite(my_sql, lite_sql, seed):
    rng = random.Random(seed)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, cat TEXT, amount INTEGER)")
    eng = Engine()
    view = eng.add_view("v", compile_sql(my_sql, {"t": ("id", "cat", "amount")}))
    live, next_id = [], [0]

    def compare():
        assert _ms_view(view) == _ms_sqlite(conn, lite_sql), f"diverged from SQLite: {my_sql}"

    for step in range(160):
        if live and rng.random() < 0.4:
            row = live.pop(rng.randrange(len(live)))
            conn.execute("DELETE FROM t WHERE id = ?", (row[0],))
            eng.apply("t", ZSet({row: -1}))
        else:
            row = (next_id[0], rng.choice(["a", "b", "c"]), rng.randint(-3, 6))
            next_id[0] += 1
            conn.execute("INSERT INTO t VALUES(?, ?, ?)", row)
            eng.apply("t", ZSet({row: +1}))
            live.append(row)
        if step % 20 == 0:
            compare()
    compare()


# --- joins: inner / left / right / full --------------------------------------

JOINS = [
    ("SELECT orders.oid, orders.uid, orders.amount, users.uname "
     "FROM orders JOIN users ON orders.uid = users.uid",
     "SELECT orders.oid, orders.uid, orders.amount, users.uname "
     "FROM orders JOIN users ON orders.uid = users.uid"),
    ("SELECT orders.oid, orders.uid, orders.amount, users.uname "
     "FROM orders LEFT JOIN users ON orders.uid = users.uid",
     "SELECT orders.oid, orders.uid, orders.amount, users.uname "
     "FROM orders LEFT JOIN users ON orders.uid = users.uid"),
    ("SELECT users.uid, users.uname, orders.oid, orders.amount "
     "FROM users RIGHT JOIN orders ON users.uid = orders.uid",
     "SELECT COALESCE(users.uid, orders.uid), users.uname, orders.oid, orders.amount "
     "FROM users RIGHT JOIN orders ON users.uid = orders.uid"),
    ("SELECT users.uid, users.uname, orders.oid, orders.amount "
     "FROM users FULL JOIN orders ON users.uid = orders.uid",
     "SELECT COALESCE(users.uid, orders.uid), users.uname, orders.oid, orders.amount "
     "FROM users FULL JOIN orders ON users.uid = orders.uid"),
]


@pytest.mark.parametrize("my_sql,lite_sql", JOINS)
@pytest.mark.parametrize("seed", range(6))
def test_join_matches_sqlite(my_sql, lite_sql, seed):
    rng = random.Random(seed)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE users(uid INTEGER PRIMARY KEY, uname TEXT)")
    conn.execute("CREATE TABLE orders(oid INTEGER PRIMARY KEY, uid INTEGER, amount INTEGER)")
    catalog = {"users": ("uid", "uname"), "orders": ("oid", "uid", "amount")}
    eng = Engine()
    view = eng.add_view("v", compile_sql(my_sql, catalog))
    live = {"users": [], "orders": []}
    next_oid = [0]

    def compare():
        assert _ms_view(view) == _ms_sqlite(conn, lite_sql), f"diverged from SQLite: {my_sql}"

    for step in range(160):
        table = rng.choice(["users", "orders"])
        pool = live[table]
        if pool and rng.random() < 0.4:
            row = pool.pop(rng.randrange(len(pool)))
            key = row[0]
            conn.execute(f"DELETE FROM {table} WHERE {'uid' if table == 'users' else 'oid'} = ?", (key,))
            eng.apply(table, ZSet({row: -1}))
        elif table == "users":
            free = [u for u in range(4) if u not in [r[0] for r in pool]]
            if not free:
                continue
            row = (rng.choice(free), rng.choice(["ann", "bob", "cat"]))
            conn.execute("INSERT INTO users VALUES(?, ?)", row)
            eng.apply("users", ZSet({row: +1}))
            pool.append(row)
        else:
            row = (next_oid[0], rng.randint(0, 3), rng.randint(-3, 9))
            next_oid[0] += 1
            conn.execute("INSERT INTO orders VALUES(?, ?, ?)", row)
            eng.apply("orders", ZSet({row: +1}))
            pool.append(row)
        if step % 20 == 0:
            compare()
    compare()
