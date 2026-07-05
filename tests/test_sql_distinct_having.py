"""SQL DISTINCT and HAVING, cross-checked against stdlib SQLite (independent
semantics oracle) and with exact-result unit tests. DISTINCT compiles to the
Distinct operator; HAVING compiles to a Filter on the aggregate output.
"""

import random
import sqlite3
from collections import Counter

import pytest

from ivm.zset import ZSet
from ivm.engine import Engine
from ivm.sql import compile_sql

T = ("id", "cat", "amount")
CATALOG = {"t": T}


def _run(sql, rows):
    eng = Engine()
    view = eng.add_view("v", compile_sql(sql, CATALOG))
    for row, w in rows:
        eng.apply("t", ZSet({row: w}))
    return view.result()


# --- exact-result unit tests -------------------------------------------------


def test_select_distinct_collapses_duplicates():
    rows = [((1, "a", 5), +1), ((2, "a", 7), +1), ((3, "b", 9), +1)]
    assert _run("SELECT DISTINCT cat FROM t", rows) == {("a",): 1, ("b",): 1}


def test_select_distinct_pair():
    rows = [((1, "a", 5), +1), ((2, "a", 5), +1), ((3, "a", 9), +1)]
    assert _run("SELECT DISTINCT cat, amount FROM t", rows) == {("a", 5): 1, ("a", 9): 1}


def test_select_distinct_star_weight_collapses():
    assert _run("SELECT DISTINCT * FROM t", [((1, "a", 5), +2)]) == {(1, "a", 5): 1}


def test_having_count_filters_small_groups():
    rows = [((1, "a", 1), +1), ((2, "a", 1), +1), ((3, "a", 1), +1), ((4, "b", 1), +1)]
    got = _run("SELECT cat, COUNT(*) AS n FROM t GROUP BY cat HAVING COUNT(*) > 2", rows)
    assert got == {("a", 3): 1}  # 'b' has one row, filtered out


def test_having_by_alias():
    rows = [((1, "a", 1), +1), ((2, "a", 1), +1), ((3, "b", 1), +1)]
    got = _run("SELECT cat, COUNT(*) AS n FROM t GROUP BY cat HAVING n > 1", rows)
    assert got == {("a", 2): 1}


def test_having_sum():
    rows = [((1, "a", 5), +1), ((2, "a", 4), +1), ((3, "b", 2), +1)]
    got = _run("SELECT cat, SUM(amount) AS s FROM t GROUP BY cat HAVING SUM(amount) >= 9", rows)
    assert got == {("a", 9): 1}


def test_having_boundary_oscillation():
    """A group repeatedly crossing the HAVING threshold must appear/disappear
    each time — the stateless Filter on the aggregate's retract/assert deltas."""
    eng = Engine()
    v = eng.add_view("v", compile_sql("SELECT cat, COUNT(*) AS n FROM t GROUP BY cat HAVING COUNT(*) > 1", CATALOG))
    eng.apply("t", ZSet({(1, "a", 0): +1}))
    assert v.result() == {}  # count 1, not > 1
    eng.apply("t", ZSet({(2, "a", 0): +1}))
    assert v.result() == {("a", 2): 1}  # crossed up
    eng.apply("t", ZSet({(2, "a", 0): -1}))
    assert v.result() == {}  # crossed back down
    eng.apply("t", ZSet({(3, "a", 0): +1, (4, "a", 0): +1}))
    assert v.result() == {("a", 3): 1}  # count now 3


def test_having_aggregate_not_in_select_raises():
    from ivm.sql import SqlError
    with pytest.raises(SqlError):
        compile_sql("SELECT cat FROM t GROUP BY cat HAVING SUM(amount) > 5", CATALOG)


# --- SQLite semantic cross-check ---------------------------------------------

QUERIES = [
    ("SELECT DISTINCT cat FROM t", "SELECT DISTINCT cat FROM t"),
    ("SELECT DISTINCT cat, amount FROM t", "SELECT DISTINCT cat, amount FROM t"),
    ("SELECT DISTINCT amount FROM t WHERE amount > 2", "SELECT DISTINCT amount FROM t WHERE amount > 2"),
    (
        "SELECT cat, COUNT(*) AS n FROM t GROUP BY cat HAVING COUNT(*) > 1",
        "SELECT cat, COUNT(*) FROM t GROUP BY cat HAVING COUNT(*) > 1",
    ),
    (
        "SELECT cat, SUM(amount) AS s FROM t GROUP BY cat HAVING SUM(amount) >= 3",
        "SELECT cat, SUM(amount) FROM t GROUP BY cat HAVING SUM(amount) >= 3",
    ),
    (
        "SELECT cat, COUNT(*) AS n FROM t GROUP BY cat HAVING COUNT(*) > 1 AND cat <> 'z'",
        "SELECT cat, COUNT(*) FROM t GROUP BY cat HAVING COUNT(*) > 1 AND cat <> 'z'",
    ),
]


@pytest.mark.parametrize("my_sql,lite_sql", QUERIES)
@pytest.mark.parametrize("seed", range(5))
def test_distinct_having_match_sqlite(my_sql, lite_sql, seed):
    rng = random.Random(seed)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, cat TEXT, amount INTEGER)")
    eng = Engine()
    view = eng.add_view("v", compile_sql(my_sql, CATALOG))
    live, next_id = [], [0]

    def compare():
        got = Counter(view.result())
        want = Counter(tuple(r) for r in conn.execute(lite_sql).fetchall())
        assert got == want, f"diverged from SQLite: {my_sql}"

    for step in range(160):
        if live and rng.random() < 0.4:
            row = live.pop(rng.randrange(len(live)))
            conn.execute("DELETE FROM t WHERE id = ?", (row[0],))
            eng.apply("t", ZSet({row: -1}))
        else:
            row = (next_id[0], rng.choice(["a", "b", "z"]), rng.randint(0, 5))
            next_id[0] += 1
            conn.execute("INSERT INTO t VALUES(?, ?, ?)", row)
            eng.apply("t", ZSet({row: +1}))
            live.append(row)
        if step % 20 == 0:
            compare()
    compare()
