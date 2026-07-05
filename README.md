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
  zset.py       # Z-set: row tuple -> integer weight (the DBSP primitive)
  row.py        # named-column access over a value-tuple row
  plan.py       # declarative plan nodes: Source/Filter/Project/Join/Aggregate
  operators.py  # incremental operators (delta in -> delta out) + compiler
  engine.py     # Engine (shared sources) + View (materialized result)
  view.py       # Milestone 0 hand-wired GROUP BY view
  oracle.py     # from-scratch recompute (M0) + eval_plan (M1), tests only
tests/
  test_oracle_equivalence.py   # M0: hand-wired GROUP BY vs oracle
  test_filter.py test_project.py test_aggregate.py test_join.py
  test_view_integration.py     # two-join + one-aggregate + multi-view
  harness.py                   # the oracle side of every property test
```

## Design docs

- `ivm-build-plan.md` — the milestone-by-milestone build order (source of truth).
- `ivm-architecture.md` — the layered design and research plan.
- `ivm-related-work.md` — annotated prior art and the gap this targets.
