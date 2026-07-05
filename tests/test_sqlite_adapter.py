"""Milestone 2, adapter #2: SQLite change capture.

The oracle sits on the FAR side of the adapter: the test mutates a real SQLite
database with ordinary SQL, the adapter captures the row changes and feeds the
engine, and the oracle recomputes over `SELECT *` of the actual table. No deltas
are ever hand-fed.

The docs' central warning is that `sqlite3_update_hook` silently misses whole
classes of changes, so this file deliberately exercises the three traps —
`DELETE` without WHERE (truncate optimization), `INSERT OR REPLACE` (a delete in
disguise), and `WITHOUT ROWID` tables — and asserts capture stays correct.

`capture` is parametrized so every case runs through each available backend:
  * "trigger" — portable changelog triggers on stdlib sqlite3;
  * "session" — the apsw session extension (added in the next task; skipped
    here until apsw + the backend exist).
"""

import random
import sqlite3

import pytest

from ivm.plan import Source, Join, Aggregate, Count, Sum
from ivm.engine import Engine
from ivm.adapters.sqlite import SqliteAdapter

from harness import oracle_result

BACKENDS = ["trigger", "session"]  # session runs only where apsw is installed

USERS = ("uid", "uname", "region")
ORDERS = ("oid", "uid", "amount")
T = ("id", "cat", "amount")
WT = ("k", "cat", "amount")


def open_db(capture):
    if capture == "trigger":
        return sqlite3.connect(":memory:", isolation_level=None)  # autocommit
    apsw = pytest.importorskip("apsw")
    return apsw.Connection(":memory:")


def by_cat_plan(source_table, schema):
    return Aggregate(Source(source_table, schema), ("cat",), (Count("n"), Sum("total", "amount")))


# --- the update_hook blind spots: each must be captured correctly -------------


@pytest.mark.parametrize("capture", BACKENDS)
def test_delete_without_where_is_captured(capture):
    """`DELETE FROM t` with no WHERE hits SQLite's truncate optimization, which
    the update hook cannot see. A row trigger disables that optimization; the
    session extension records each row. Either way the view must empty."""
    conn = open_db(capture)
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, cat TEXT, amount INTEGER)")
    eng = Engine()
    plan = by_cat_plan("t", T)
    view = eng.add_view("by_cat", plan)
    adapter = SqliteAdapter(eng, conn, capture=capture)
    adapter.register("t", T, ("id",))

    conn.execute("INSERT INTO t VALUES(1, 'a', 5)")
    conn.execute("INSERT INTO t VALUES(2, 'a', 3)")
    conn.execute("INSERT INTO t VALUES(3, 'b', 7)")
    adapter.flush()
    assert view.result() == {("a", 2, 8): 1, ("b", 1, 7): 1}

    conn.execute("DELETE FROM t")  # no WHERE
    adapter.flush()
    assert view.result() == {}
    assert oracle_result(plan, adapter.all_contents()) == {}


@pytest.mark.parametrize("capture", BACKENDS)
def test_insert_or_replace_is_captured(capture):
    """INSERT OR REPLACE deletes the conflicting row then inserts — the delete
    is invisible to a naive hook. The view's SUM must reflect the new value."""
    conn = open_db(capture)
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, cat TEXT, amount INTEGER)")
    eng = Engine()
    plan = by_cat_plan("t", T)
    view = eng.add_view("by_cat", plan)
    adapter = SqliteAdapter(eng, conn, capture=capture)
    adapter.register("t", T, ("id",))

    conn.execute("INSERT INTO t VALUES(1, 'a', 5)")
    adapter.flush()
    assert view.result() == {("a", 1, 5): 1}

    conn.execute("INSERT OR REPLACE INTO t VALUES(1, 'a', 9)")
    adapter.flush()
    assert view.result() == {("a", 1, 9): 1}
    assert view.result() == oracle_result(plan, adapter.all_contents())


@pytest.mark.parametrize("capture", BACKENDS)
def test_without_rowid_table_is_captured(capture):
    """WITHOUT ROWID tables have no rowid for the update hook to report; triggers
    and the session extension handle them normally."""
    conn = open_db(capture)
    conn.execute("CREATE TABLE wt(k TEXT PRIMARY KEY, cat TEXT, amount INTEGER) WITHOUT ROWID")
    eng = Engine()
    plan = by_cat_plan("wt", WT)
    view = eng.add_view("by_cat", plan)
    adapter = SqliteAdapter(eng, conn, capture=capture)
    adapter.register("wt", WT, ("k",))

    conn.execute("INSERT INTO wt VALUES('x', 'a', 5)")
    conn.execute("INSERT INTO wt VALUES('y', 'a', 3)")
    adapter.flush()
    assert view.result() == {("a", 2, 8): 1}

    conn.execute("DELETE FROM wt WHERE k = 'x'")
    adapter.flush()
    assert view.result() == {("a", 1, 3): 1}
    assert view.result() == oracle_result(plan, adapter.all_contents())


# --- basic op coverage --------------------------------------------------------


@pytest.mark.parametrize("capture", BACKENDS)
def test_update_flows_through_adapter(capture):
    conn = open_db(capture)
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, cat TEXT, amount INTEGER)")
    eng = Engine()
    plan = by_cat_plan("t", T)
    view = eng.add_view("by_cat", plan)
    adapter = SqliteAdapter(eng, conn, capture=capture)
    adapter.register("t", T, ("id",))

    conn.execute("INSERT INTO t VALUES(1, 'a', 5)")
    adapter.flush()
    assert view.result() == {("a", 1, 5): 1}
    # update the grouping column: the row must move groups
    conn.execute("UPDATE t SET cat = 'b' WHERE id = 1")
    adapter.flush()
    assert view.result() == {("b", 1, 5): 1}


# --- the oracle-through-adapter property test ---------------------------------


def _revenue_by_region_plan():
    joined = Join(Source("orders", ORDERS), Source("users", USERS), ("uid",), ("uid",))
    return Aggregate(joined, ("region",), (Count("n"), Sum("total", "amount")))


@pytest.mark.parametrize("capture", BACKENDS)
@pytest.mark.parametrize("seed", range(8))
def test_sqlite_matches_oracle(capture, seed):
    rng = random.Random(seed)
    conn = open_db(capture)
    conn.execute("CREATE TABLE users(uid INTEGER PRIMARY KEY, uname TEXT, region TEXT)")
    conn.execute("CREATE TABLE orders(oid INTEGER PRIMARY KEY, uid INTEGER, amount INTEGER)")
    eng = Engine()
    plans = {
        "revenue_by_region": _revenue_by_region_plan(),
        "orders_per_user": Aggregate(Source("orders", ORDERS), ("uid",), (Count("n"),)),
    }
    views = {name: eng.add_view(name, plan) for name, plan in plans.items()}
    adapter = SqliteAdapter(eng, conn, capture=capture)
    adapter.register("users", USERS, ("uid",))
    adapter.register("orders", ORDERS, ("oid",))

    live = {"users": set(), "orders": set()}
    next_oid = [0]
    names = ["ann", "bob", "cat", "dan"]
    regions = ["west", "east"]

    def check():
        tables = adapter.all_contents()
        for name, view in views.items():
            assert view.result() == oracle_result(plans[name], tables), (
                f"[{capture}] view {name} diverged from oracle"
            )

    for _ in range(200):
        table = rng.choice(["users", "orders"])
        pks = list(live[table])
        r = rng.random()
        if pks and r < 0.3:  # delete
            pk = rng.choice(pks)
            col = "uid" if table == "users" else "oid"
            conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (pk,))
            live[table].discard(pk)
        elif pks and r < 0.6:  # update (stable PK)
            pk = rng.choice(pks)
            if table == "users":
                conn.execute("UPDATE users SET uname = ?, region = ? WHERE uid = ?",
                             (rng.choice(names), rng.choice(regions), pk))
            else:
                conn.execute("UPDATE orders SET uid = ?, amount = ? WHERE oid = ?",
                             (rng.randint(0, 3), rng.randint(-3, 9), pk))
        else:  # insert with a fresh PK
            if table == "users":
                free = [u for u in range(4) if u not in live["users"]]
                if not free:
                    continue
                uid = rng.choice(free)
                conn.execute("INSERT INTO users VALUES(?, ?, ?)",
                             (uid, rng.choice(names), rng.choice(regions)))
                live["users"].add(uid)
            else:
                oid = next_oid[0]
                next_oid[0] += 1
                conn.execute("INSERT INTO orders VALUES(?, ?, ?)",
                             (oid, rng.randint(0, 3), rng.randint(-3, 9)))
                live["orders"].add(oid)
        adapter.flush()
        check()

    for table in ("orders", "users"):
        col = "uid" if table == "users" else "oid"
        for pk in list(live[table]):
            conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (pk,))
            adapter.flush()
        live[table].clear()
    check()
    for view in views.values():
        assert view.result() == {}
