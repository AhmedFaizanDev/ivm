**IVM Build Plan — Start Here**

*The concrete path from zero to a working library. Read `ivm-architecture.md` for the design and `ivm-related-work.md` for prior art. This doc is the order you build in.*

## The one rule that governs everything

Incremental view maintenance fails silently: a wrong aggregate after a delete looks like a normal number. So the recompute oracle is not a later test, it is the first thing you write. Every milestone below is "done" only when, over random streams of inserts and deletes, the incrementally maintained view equals a full from-scratch recompute, including when a group empties to zero. If that assertion is not running, you have not built IVM, you have built a guess.

## Stack decision: Python first, Rust later

Build the first two milestones in **Python**, no Rust. Reasons: the hard part is the algebra and correctness, not raw speed; Python lets you get the operators right in days instead of weeks; the recompute oracle and property tests are trivial to write. Rust is the eventual home for the shippable library (that is the architecture doc's goal), but porting a correct, well-tested Python kernel to Rust is a known, bounded job. Porting a design you have not validated is not.

Port to Rust only after Milestone 3, and only if you still want to. See Milestone 4.

## Milestone 0 — the weekend kernel (proves the idea is real) ✅ DONE 2026-07-05

Built TDD (oracle tests written first, watched fail, then implemented). Lives in `ivm/` (zset.py, view.py, oracle.py) + `tests/test_oracle_equivalence.py`. 28 tests green, including 20 property-test seeds x 400 random insert/delete ops each (8,000 oracle-checked deltas) plus drain-to-empty, empty-group-vanishes, negative-amount (count>0/sum=0 stays), duplicate-row weights, and batched deltas. Run: `cd C:\RootVault\ivm && python -m pytest tests/ -q`. Next: Milestone 1.

Smallest real thing. No SQL parser, no SQLite, no files. In memory only.

Research note (validated 2026-07): start directly with Z-set operators, not a separate "classic counting algorithm" implementation. DBSP's linearity theorems subsume counting: for linear operators (filter, project, COUNT, SUM) the operator IS its own incremental version, and the Z-set weights ARE the counts. Building counting first would be throwaway machinery. Reference implementation to study (not depend on): pydbsp (github.com/brurucy/pydbsp), a pure-Python solo-built DBSP with incremental SQL operators and GROUP BY aggregation — proof this milestone is feasible in Python.

- A Z-set primitive: a mapping from a row (a tuple) to an integer weight. Insert is weight +1, delete is weight -1. A row with weight 0 is absent.
- One hand-wired view: `SELECT category, COUNT(*), SUM(amount) FROM t GROUP BY category`. You wire the operator graph by hand; you do not parse SQL yet.
- A delta loop: feed a stream of inserts and deletes as weighted rows; update the grouped counts and sums incrementally, never recomputing.
- The oracle test: after every delta, assert the incremental result equals a full recompute over the accumulated table, including a group whose count returns to zero (it must disappear, not sit at zero).

**Done when:** a property test runs thousands of random insert/delete sequences and the incremental result never diverges from the oracle.

**This milestone is also the go/no-go.** If getting the empty-group and delete cases right is satisfying, commit to the grind. If it is a slog, you learned in two days that database internals is not your thing, at almost no cost.

## Milestone 1 — a real tier-1 engine

Generalize the toy into a small engine that maintains arbitrary tier-1 views (see the coverage tiers in the architecture doc).

- Operators: filter, project (map), inner join, COUNT, SUM. Each operator consumes input deltas and emits output deltas, and keeps only the state it needs (join keeps its input indexes, aggregates keep per-group accumulators).
- A view is an operator graph. Build the graph from a small, explicit plan structure. A tiny SELECT-subset parser is optional sugar; a hand-built plan API is enough to start.
- Support several views over the same tables at once.

**Done when:** every operator ships with its own property test (incremental equals oracle over random insert/delete streams), and a two-join, one-aggregate view passes the oracle test.

**✅ DONE 2026-07-05.** Filter, Project, Aggregate (COUNT+SUM), bilinear Join, named-column plan API, shared-source engine with multi-view fan-out. 155 tests green (M0 28 + M1 127). Commit `e1f47ee`. Oracle (`eval_plan`) verified to share no code with operators/engine.

**KNOWN LIMITATIONS (must clear before the SQL front-end, which will allow these):**
1. **Self-joins / diamond plans are NOT trusted.** The engine wires each join's two inputs from disjoint base tables, so a single base delta only ever hits one side of any join and the ΔL⋈ΔR cross-term never arises in tests. A view that joins a table to itself (or a DAG where one stream feeds both sides of a join) needs explicit two-edge propagation to the same join node AND its own property tests before it is trustworthy. A silently-wrong join is the worst-case failure mode — do not enable self-joins until this is tested.
2. **Oracle recompute cost in tests.** The integration suite recomputes two joins from scratch each step (~7s of the run). Fine now; revisit if streams grow.

## Milestone 2 — attach it to a real data source

Give the engine a change feed instead of a hand-fed delta stream.

- Start with the easiest adapter: an **in-process mutation log**. The application writes through a thin API that records inserts and deletes and forwards them to the engine. No database hooks required, pure stdlib.
- Then, if you want the drop-in-on-an-existing-database story, add a **SQLite** adapter. Research finding (2026-07): this adapter is the **single biggest correctness risk in the whole plan**, so the choice of capture mechanism matters:
  - **Do NOT build on the update hook alone.** `sqlite3_update_hook` reports only op type, table, and rowid — no column values, and DELETE old-values are unrecoverable. Worse, it silently misses: WITHOUT ROWID tables, rows deleted via ON CONFLICT REPLACE, and truncate-optimized `DELETE` without WHERE. An adapter built solely on it produces wrong deltas on realistic workloads (per official SQLite docs).
  - **Primary path: the SQLite session extension** (`sqlite3session_*`) via `apsw` — it yields row-level insert/update/delete changesets *with values*. Constraints to note: needs `-DSQLITE_ENABLE_SESSION -DSQLITE_ENABLE_PREUPDATE_HOOK` compile flags (Python stdlib `sqlite3` lacks them; apsw PyPI wheels have them), only tracks tables with a declared PRIMARY KEY, and only sees writes on its own connection.
  - **Portable fallback: trigger-based changelog table** (works on any SQLite build, proven in production for reactivity), at the cost of write amplification.
  - Whichever you pick, the oracle test must run *through the adapter* — capture bugs look exactly like engine bugs.

**Done when:** an example app mutates data through the adapter and a maintained view stays correct against the oracle with zero manual delta feeding.

## Milestone 3 — the hard correctness cases and the cost model

This is where IVM stops being a toy.

- Deletes everywhere (not just in aggregates): joins must retract correctly, which is the classic bug farm.
- AVG, MIN, MAX: these need auxiliary state (AVG keeps sum and count separately; MIN/MAX need enough history to recover after the current extreme is deleted).
- The hybrid cost model: before each refresh, estimate the cost of applying deltas versus a full recompute, and take the cheaper path. This is the Enzyme idea and it is what makes "negligible overhead" honest rather than an overclaim. Research caveat (2026-07): no public source documents how production systems implement this decision — it survived zero verification. Treat it as an experiment (and a possible novel contribution), not a known recipe. Also note the practitioner evidence cuts the other way for the common case: Wonlaw benchmarked re-executing SQLite queries on change versus in-memory incremental and "the difference was massive" — so recompute is the escape hatch for bulk updates, never the default path.
- Watch state size from here on. The verified Materialize post-mortem: differential-dataflow-style IVM started at ~96 bytes of overhead per maintained record and took months of expert work to reach 0-16 bytes. Join/DISTINCT/MIN-MAX state growth is the long-term scaling wall — not algorithm correctness. The Python prototype will NOT surface this (that is the known blind spot of Python-first); just record state sizes in tests so the Rust port has baselines.

**Done when:** tier-2 coverage (from the architecture doc) passes the oracle test, and the cost model demonstrably falls back to recompute on a large bulk update.

## Milestone 4 — decision point

You now have a correct, tested tier-1/2 engine. Choose a direction with your professor:

- **Ship the library:** port the validated kernel to Rust, add bindings, package it. This is the GitHub-profile artifact.
- **Write the paper:** push into tier-3 (recursive CTEs, window functions) or the correctness story (verified operators applied to an embeddable engine, building on the POPL 2026 work). This is the research contribution.

Both start from the same Milestone 3 codebase. Do not decide this now; decide it when you get here.

## Starter repo layout (Python)

```
ivm/
  ivm/
    __init__.py
    zset.py          # Z-set: row tuple -> integer weight
    operators.py     # filter, project, join, count, sum (delta in -> delta out)
    view.py          # a view = an operator graph built from a plan
    oracle.py        # from-scratch recompute, for tests only
    adapters/
      inproc.py      # in-process mutation log (Milestone 2)
      sqlite.py      # SQLite change capture (Milestone 2, later)
  tests/
    test_oracle_equivalence.py   # property tests: incremental == oracle
  examples/
    grouped_totals.py
  pyproject.toml
  README.md
```

## Test discipline (non-negotiable)

- Every operator has a property test that runs random insert/delete streams and asserts equality with the oracle.
- Deletes and empty-group cases are always in the generated streams, never optional.
- A change that makes any oracle test flaky or red blocks the next milestone. In IVM, a red oracle test is not a nuisance, it is the product telling you it is wrong.

## What NOT to build yet (YAGNI)

- No Rust until after Milestone 3.
- No full SQL parser; support only the query shapes each milestone needs.
- No serialization, resumability, or compaction until the engine is correct (they are architecture-doc goals, not MVP).
- No Postgres adapter, no distributed anything, no cost model before Milestone 3.
- No packaging, CI, or docs polish until Milestone 1 passes. The oracle test is the only infrastructure that earns its place early.
