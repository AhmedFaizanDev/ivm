# ivm — embeddable Incremental View Maintenance

A library that keeps a declared SQL view current by applying only the deltas to
its base tables, with no external service — in the spirit of SQLite or DuckDB.
Built on the DBSP model (Z-sets and differential operators).

Python first to validate the algebra and correctness cheaply; Rust is the
eventual home for the shippable library. See the design docs below.

## Status

- **Milestone 0 — done.** Z-set kernel + a hand-wired, oracle-checked
  `SELECT category, COUNT(*), SUM(amount) GROUP BY category` view.
- **Milestone 1 — done.** Composable operators (filter, project, inner join,
  COUNT, SUM), each consuming input deltas and emitting output deltas while
  keeping only the state it needs. A view is an operator graph compiled from an
  explicit plan; the engine fans one base delta out to many views over the same
  tables. The inner join is bilinear — it retains the full Z-set of both inputs,
  not a one-sided lookup. 155 tests green: every operator has its own recompute-
  oracle property test, and a two-join + one-aggregate view passes the oracle
  over random insert/delete streams (drained to empty).
- **Milestone 2 — done.** A real change feed, so the app mutates data instead of
  hand-feeding deltas. Two adapters, both with the recompute oracle running on
  the *far side* of the adapter (a capture bug looks exactly like an engine bug):
  - **In-process mutation log** (`adapters/inproc.py`) — pure stdlib, PK-keyed
    `insert`/`delete`/`update` (update = delete-old + insert-new); holds the
    authoritative table contents.
  - **SQLite adapter** (`adapters/sqlite.py`) — never uses `sqlite3_update_hook`
    (it misses WITHOUT ROWID tables, ON CONFLICT REPLACE deletes, and
    truncate-optimized deletes). Primary path is the **session extension via
    apsw**; a portable **trigger-based changelog** works on stdlib `sqlite3`. All
    three update-hook blind spots are covered and tested on both backends.

  199 tests green overall (M0+M1 155, in-process log 20, SQLite adapters 24),
  every maintained view checked against the oracle through the live adapter.
- **Milestone 3 — in progress (tier-2 correctness).**
  - **AVG** (running sum + count, divide at read) and **MIN/MAX** with correct
    delete-recovery — the non-linear case, done by keeping each group's full
    value multiset so deleting the current extreme recovers the next one.
  - **Deletes-in-joins hardened** and **self-joins / diamonds cleared**: one
    source may now feed both inputs of a join; the `ΔL⋈ΔR` cross-term is handled
    exactly once in either propagation order (property-tested).
  - **Hybrid cost model** (`cost_model.py`, *experimental*): per refresh, pick
    incremental vs. full recompute by a size-ratio heuristic and fall back to
    recompute on bulk updates — both paths are oracle-checked. Kept outside the
    validated core on purpose.
  - Trigger-backend capture rewritten to a **native-typed changelog** (fixes
    silent float drift and the BLOB crash the JSON changelog had).
  - 317 tests green overall.

## The one rule

IVM fails silently — a wrong aggregate after a delete looks like a normal
number. So every view is checked, after each delta, against a from-scratch
recompute oracle over random insert/delete streams (empty groups included). If
that assertion isn't running, it isn't IVM.

## Run the tests

```
python -m pytest tests/ -q
```

The SQLite session backend needs [apsw](https://pypi.org/project/apsw/) (its
wheels ship the `ENABLE_SESSION` + `PREUPDATE_HOOK` compile flags that the Python
stdlib `sqlite3` lacks): `python -m pip install apsw`. Without it, the session
tests skip and everything else — including the trigger-based SQLite adapter —
still runs on the standard library alone.

## Layout

```
ivm/
  zset.py       # Z-set: row tuple -> integer weight (the DBSP primitive)
  row.py        # named-column access over a value-tuple row
  plan.py       # declarative plan nodes: Source/Filter/Project/Join/Aggregate
  operators.py  # incremental operators (delta in -> delta out) + compiler
  engine.py     # Engine (shared sources) + View (materialized result)
  view.py       # Milestone 0 hand-wired GROUP BY view
  oracle.py     # from-scratch recompute (M0) + eval_plan (M1), tests only
  cost_model.py # experimental hybrid incremental-vs-recompute wrapper (M3)
  adapters/
    inproc.py   # in-process mutation log (M2)
    sqlite.py   # SQLite change capture: session (apsw) + trigger backends (M2)
tests/
  test_oracle_equivalence.py   # M0: hand-wired GROUP BY vs oracle
  test_filter.py test_project.py test_aggregate.py test_join.py
  test_view_integration.py     # two-join + one-aggregate + multi-view
  test_join_deletes.py test_self_join.py   # M3 join hardening + self-joins
  test_inproc_adapter.py       # oracle through the in-process mutation log
  test_sqlite_adapter.py       # oracle through SQLite (both capture backends)
  test_cost_model.py           # M3 hybrid cost model, both paths vs oracle
  harness.py                   # the oracle side of every property test
```

## Design docs

- `ivm-build-plan.md` — the milestone-by-milestone build order (source of truth).
- `ivm-architecture.md` — the layered design and research plan.
- `ivm-related-work.md` — annotated prior art and the gap this targets.
