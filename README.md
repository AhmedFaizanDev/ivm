# ivm — embeddable Incremental View Maintenance

A library that keeps a declared SQL view current by applying only the deltas to
its base tables, with no external service — in the spirit of SQLite or DuckDB.
Built on the DBSP model (Z-sets and differential operators).

Python first to validate the algebra and correctness cheaply; Rust is the
eventual home for the shippable library. See the design docs below.

## Status

- **Milestone 0 — done.** Z-set kernel + a hand-wired, oracle-checked
  `SELECT category, COUNT(*), SUM(amount) GROUP BY category` view. 28 tests
  green, including property tests over thousands of random insert/delete deltas.
- **Milestone 1 — in progress.** Composable operators (filter, project, inner
  join, COUNT, SUM) assembled into per-view operator graphs from an explicit
  plan.

## The one rule

IVM fails silently — a wrong aggregate after a delete looks like a normal
number. So every view is checked, after each delta, against a from-scratch
recompute oracle over random insert/delete streams (empty groups included). If
that assertion isn't running, it isn't IVM.

## Run the tests

```
python -m pytest tests/ -q
```

## Layout

```
ivm/
  zset.py      # Z-set: row tuple -> integer weight (the DBSP primitive)
  view.py      # Milestone 0 hand-wired GROUP BY view
  oracle.py    # from-scratch recompute, for tests only
tests/
  test_oracle_equivalence.py   # property tests: incremental == oracle
```

## Design docs

- `ivm-build-plan.md` — the milestone-by-milestone build order (source of truth).
- `ivm-architecture.md` — the layered design and research plan.
- `ivm-related-work.md` — annotated prior art and the gap this targets.
