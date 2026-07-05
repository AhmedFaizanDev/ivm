# ivm — embeddable Incremental View Maintenance

[![CI](https://github.com/AhmedFaizanDev/ivm/actions/workflows/ci.yml/badge.svg)](https://github.com/AhmedFaizanDev/ivm/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Declare a SQL view once. Mutate the base tables. The view stays correct — by
applying only the deltas, never recomputing.** No server, no streaming cluster,
no external service: a library you link into your process, in the spirit of
SQLite. Built on the DBSP model (Z-sets + differential operators).

```python
from ivm import Engine

eng = Engine()
catalog = {"sales": ("id", "region", "amount")}

# declare a maintained view in SQL
view = eng.add_sql_view(
    "by_region",
    "SELECT region, COUNT(*) AS n, SUM(amount) AS total "
    "FROM sales WHERE amount > 0 GROUP BY region",
    catalog,
)

eng.insert("sales", (1, "west", 100))
eng.insert("sales", (2, "west", 50))
eng.insert("sales", (3, "east", 200))
view.result()          # {('west', 2, 150): 1, ('east', 1, 200): 1}

eng.delete("sales", (2, "west", 50))   # retract one row...
view.result()          # {('west', 1, 100): 1, ('east', 1, 200): 1}   ← updated incrementally
```

No refresh, no re-scan of `sales`. The `SUM`, `COUNT`, and group set were patched
by the single delta.

## Why this exists

Keeping a derived view fresh has two usual answers, both unsatisfying:

- **Recompute on a timer** — simple, but wasteful and stale.
- **Run a streaming database** (Materialize, RisingWave, Feldera) — correct, but
  a separate distributed service with real operational cost.

The lightweight slot between them — *a library you just import* — has stayed
empty. The theory to fill it (DBSP, 2023) is recent, and the one production
system proving the cost-model idea (Enzyme) is closed and Spark-based. `ivm`
aims squarely at that gap. See [`ivm-related-work.md`](ivm-related-work.md).

## What it does

- **Real SQL joins and aggregates, incrementally maintained under inserts *and*
  deletes:** `SELECT` / projection / `WHERE`, `INNER` / `LEFT` / `RIGHT` / `FULL`
  outer joins, `GROUP BY` with `COUNT` / `SUM` / `AVG` / `MIN` / `MAX`.
  The hard cases are handled: deleting the current `MIN`/`MAX` recovers the next
  one; an outer-join row flips between NULL-padded and matched as its match
  comes and goes.
- **A zero-dependency SQL front-end** — write `SELECT …`, get a maintained view.
  The declarative plan API underneath is the stable core; SQL compiles to it.
- **Attach to real data.** Change-capture adapters turn ordinary writes into
  deltas: an in-process mutation log (pure stdlib), or **SQLite** — via the
  session extension (`apsw`) or portable changelog triggers, correctly handling
  the cases a naive `sqlite3_update_hook` silently drops (WITHOUT ROWID,
  `INSERT OR REPLACE`, truncate `DELETE`).
- **A cost model** that falls back to full recompute when a refresh touches a
  large fraction of the data (experimental).

## Correctness is the whole point

Incremental maintenance fails *silently* — a wrong aggregate after a delete looks
like a normal number. So the test suite is built around a **recompute oracle**:
an independent, from-scratch evaluator that shares no code with the incremental
operators. Every operator, adapter, and SQL query is checked by asserting
`incremental == oracle` after every delta, over random insert/delete streams
drained to empty, with deletes and empty groups always exercised. The SQL
front-end is additionally cross-checked against **real SQLite**.

**558+ tests, green.** When a change makes an oracle test red, that's the product
telling you it's wrong.

## Install

```bash
pip install git+https://github.com/AhmedFaizanDev/ivm.git
# optional: the SQLite session-extension capture backend
pip install "ivm[sqlite-session] @ git+https://github.com/AhmedFaizanDev/ivm.git"
```

Pure standard library at runtime (`apsw` is optional, only for the SQLite session
backend). Not yet on PyPI.

## Try the live demo

```bash
python examples/live_dashboard.py
```

Streams random sales events through a maintained SQL dashboard and prints it
updating in real time — then proves the incrementally maintained numbers exactly
equal a from-scratch recompute.

## Run the tests

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

## How it works (one paragraph)

Base-table changes are **Z-sets**: a row → integer weight, where insert is `+1`
and delete is `−1` (a row at weight 0 is absent). A view is an operator graph;
each operator consumes input deltas and emits output deltas while keeping only
the state it needs — a join retains both indexed inputs, an aggregate keeps
per-group accumulators (and, for `MIN`/`MAX`, the group's value multiset so it
can recover an extreme after a delete). The linear operators (filter, project,
`COUNT`, `SUM`) *are* their own incremental version; that's the DBSP result this
builds on.

## Status & design docs

Tier-1 and tier-2 SQL are complete and oracle-verified; a working SQL front-end
compiles to the plan. Detailed, honest build log and known limitations:

- [`ivm-build-plan.md`](ivm-build-plan.md) — milestone-by-milestone log (source of truth).
- [`ivm-sql-frontend.md`](ivm-sql-frontend.md) — the SQL compiler design note.
- [`ivm-architecture.md`](ivm-architecture.md) — layered design and research plan.
- [`ivm-related-work.md`](ivm-related-work.md) — prior art and the gap this targets.

## License

MIT — see [`LICENSE`](LICENSE).
