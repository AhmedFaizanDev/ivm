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

**✅ DONE 2026-07-05.** In-process mutation log (PK-keyed) + SQLite adapter with two backends (trigger changelog on stdlib, session/apsw as primary). Oracle runs through the adapter over real DB contents. All three update-hook blind spots (truncate DELETE, INSERT OR REPLACE, WITHOUT ROWID) covered on both backends. 199 tests green (44 adapter), 0 skipped (apsw genuinely runs). Commit `d92d8d1`.

**BUGS FOUND IN REVIEW (2026-07-05) — trigger backend only, fix before relying on it:**
1. **Float precision loss (SILENT WRONG ANSWER).** The trigger captures rows via `json_array(...)` → `json.loads`, but SQLite's JSON formats reals lossily: `0.1+0.2` reads native as `0.30000000000000004` yet comes back from JSON as `0.3`. The captured delta row then never cancels against `contents()`'s native row, so the view silently diverges from the oracle. Masked today because every property test uses integer amounts. This is the exact failure mode the project exists to prevent.
2. **BLOB crash.** `json_array` raises "JSON cannot hold BLOB values", so any maintained table with a BLOB column errors on every write through the trigger backend.
   Root cause: JSON serialization in the triggers. The session/apsw backend is unaffected (native values, matches `contents()`). Proper fix: replace the JSON changelog with a native-typed per-table changelog (store real column values, not JSON), OR at minimum guard `register()` to raise loudly on REAL/BLOB columns for the trigger backend and document the limitation. Do this TDD: add a failing oracle test with float + blob columns through the trigger backend first.
3. **Backend concurrency divergence (document, not a bug).** Triggers capture writes from *any* connection to the DB; the session backend only sees writes on its own connection. The two "equivalent" adapters differ here — users should choose knowingly.

**RUNTIME TAX (now material).** Full suite is ~2 min (was ~7s at M1); adapter tests alone ~26s. This is the oracle-recompute cost (KNOWN LIMITATION #2) and it will worsen every milestone. Cheap fix before M3 adds more: in the long property tests, check the oracle every N steps instead of every step (keep a couple of full-every-step seeds), and/or trim seed counts. Self-contained, ~30 min.

## Milestone 3 — the hard correctness cases and the cost model

This is where IVM stops being a toy.

- Deletes everywhere (not just in aggregates): joins must retract correctly, which is the classic bug farm.
- AVG, MIN, MAX: these need auxiliary state (AVG keeps sum and count separately; MIN/MAX need enough history to recover after the current extreme is deleted).
- The hybrid cost model: before each refresh, estimate the cost of applying deltas versus a full recompute, and take the cheaper path. This is the Enzyme idea and it is what makes "negligible overhead" honest rather than an overclaim. Research caveat (2026-07): no public source documents how production systems implement this decision — it survived zero verification. Treat it as an experiment (and a possible novel contribution), not a known recipe. Also note the practitioner evidence cuts the other way for the common case: Wonlaw benchmarked re-executing SQLite queries on change versus in-memory incremental and "the difference was massive" — so recompute is the escape hatch for bulk updates, never the default path.
- Watch state size from here on. The verified Materialize post-mortem: differential-dataflow-style IVM started at ~96 bytes of overhead per maintained record and took months of expert work to reach 0-16 bytes. Join/DISTINCT/MIN-MAX state growth is the long-term scaling wall — not algorithm correctness. The Python prototype will NOT surface this (that is the known blind spot of Python-first); just record state sizes in tests so the Rust port has baselines.

**Done when:** tier-2 coverage (from the architecture doc) passes the oracle test, and the cost model demonstrably falls back to recompute on a large bulk update.

**✅ MOSTLY DONE 2026-07-05** (commits 9cd7163…0c795fc, 317 tests green in ~36s). Delivered: trigger float/BLOB fix (native-typed changelog), runtime throttle, AVG (linear), MIN/MAX (non-linear, per-group value multiset with delete-recovery), deletes-in-joins hardening, self-join/diamond, experimental hybrid cost model.

**Independently verified in review (2026-07-05), not taken on trust:**
- Self-join cross-term (KNOWN LIMITATION #1) — was already correct, not broken. Confirmed by hand-proof (both propagation orders capture ΔL⋈ΔR exactly once) AND a genuinely adversarial shared-source property test (small id pool, 400 steps, 50% deletes, batched cross-term). KNOWN LIMITATION #1 is now CLEARED.
- Hybrid cost model's "recompute leaves state consistent for a later incremental refresh" — confirmed by an independent reviewer probe (30 seeds × 300 steps forcing incremental↔recompute alternation with deletes and non-linear MAX, oracle-checked every refresh, drained to empty).
- Oracle/operator separation still intact (no shared code; independent AVG/MIN/MAX value readers), so "317 green" is real evidence.

**✅ TIER-2 OUTER JOINS COMPLETE (Milestone 3.5).** LEFT (`c25d1c2`), then RIGHT + FULL (this session) — each a distinct plan node + operator with an independent from-scratch oracle branch; inner join and LEFT left untouched. All four join types share a uniform output schema (`left_schema + right_non_key`) with a **coalesced (USING-style) key**: an unmatched right row still carries its key value in the shared key column, left non-key columns NULL. Verified: flip-on-match-appear/disappear unit tests, 15-seed random both-sided property tests per join type, and an ADVERSARIAL batched-delta probe (multi-sign deltas at a shared key, NULL-in-data, floats, negatives, weight>1 duplicates) — zero divergence from the oracle. Per the architecture doc's tiers, **tier-2 is now complete.**

DESIGN NOTE (coalesced key): equijoin keys merge into one column (USING semantics), not separate l.k / r.k (ON semantics) — keeps column layout identical across inner/LEFT/RIGHT/FULL, which the SQL front-end depends on. LOGGED representational tie (not a bug): a genuine NULL in a left non-key column produces the same tuple as a NULL pad; oracle and engine treat it identically, so equivalence holds. RIGHT/FULL self-outer-join (one source feeding both sides of an outer join) is NOT separately tested — deferred with inner-join diamond coverage.

## SQL front-end — ✅ DONE (this session)

A hand-written, zero-dependency SELECT-subset compiler (`ivm/sql.py`) turns SQL into the plan API — the feature that makes this "an IVM library you write SQL against." Design note: `ivm-sql-frontend.md`.
- Supports: `SELECT` (columns / aggregates / `*`), `FROM t [alias]`, `[INNER|LEFT|RIGHT|FULL [OUTER]] JOIN … ON a.k=b.k [AND …]`, `WHERE` (`= != <> < <= > >=`, `IS [NOT] NULL`, `AND`/`OR`, parens, literals incl. negatives), `GROUP BY` + `COUNT/SUM/AVG/MIN/MAX`. Plus `engine.add_sql_view(name, sql, catalog)` sugar.
- Verified THREE ways: (1) compiled plan == recompute oracle of the trusted hand-built plan over random insert/delete streams; (2) an INDEPENDENT SQL-semantics oracle — the same SQL run in stdlib SQLite matches the maintained view (projection / filter / grouped-agg / all four join kinds; RIGHT/FULL via `COALESCE` to match our coalesced key); (3) adversarial edge probes (NULL-in-WHERE excluded not crashing, `IS NULL`, parens, lowercase keywords, negative literals, unknown-column/table errors). ~126 SQL tests.
- LOGGED limitations: identifiers are case-SENSITIVE (must match the catalog); no `HAVING`/`ORDER BY`/`DISTINCT`/subqueries/`SELECT`-arithmetic; self-join via SQL is blocked by the plan's unique-column-name rule; a GROUP BY-less GLOBAL aggregate over an EMPTY relation yields no row (SQL yields a zero row) — the standard incremental-aggregate boundary.
- TDD honesty note: the parser/compiler is one cohesive unit. Cycle-1 (projection/filter) was built failing-test-first and caught a real alias bug (getter read the alias, not the source column). Aggregates and joins were then added and verified by the oracle-equivalence + SQLite cross-check rather than one-failing-test-per-grammar-clause.

## Perf / state-size baselines (Rust-port targets) — this session

Recorded so the eventual Rust port has numbers to beat (`tests/test_state_size.py`):
- **State is LINEAR and LEAK-FREE.** A join retains exactly one index entry per input row per side; an aggregate keeps one group per live key (MIN/MAX additionally keep a per-group value multiset). After draining every row, all operator state returns to empty — verified whitebox for all four join kinds and a COUNT+SUM+MIN+MAX aggregate. This is the guard against Materialize-style state blow-up.
- **Throughput (CPython prototype): ~115,000 single-row deltas/sec** through a `GROUP BY` view (40k insert+delete deltas in ~0.35 s, one Engine, one view). Unoptimized — the Python baseline; the Rust port should beat it by orders of magnitude. Full suite: ~570 tests in <10 s (property checks throttled via `check_every`), and CI is green on Python 3.10/3.11/3.12.

## Serialization / resumability — ✅ DONE (this session)

`engine.snapshot()` / `engine.restore(snap)`. A process restart no longer forces a full recompute: `snapshot()` captures every stateful operator's state (join indexes, aggregate accumulators + MIN/MAX multisets) and each view's materialized Z-set as a point-in-time, picklable object. `restore()` injects it into an engine whose views were re-added with the SAME plans (plans carry lambdas → code/config; only state is data). Format: stdlib **`pickle`** (handles arbitrary hashable row tuples / bytes / floats / None that JSON can't; documented trusted-input caveat). Uniform `_state_attrs` protocol on operators; `compile_plan` threads an `ops` collector in deterministic post-order; the four join branches were consolidated into one dispatch.
- Oracle-verified: **restore-then-continue == never-restarted** over 10 random seeds (join → COUNT/SUM/MIN/MAX, drained to empty), == the recompute oracle at every checkpoint, and point-in-time (a snapshot doesn't drift as the engine runs on).
- ADVERSARIAL (`test_serialization_adversarial.py`): snapshot→pickle→restore into a FRESH engine at EVERY step, across all four join kinds, with NULL-padded outer-join rows + floats + negatives in state, multi-view engines, empty engines, and a wrong-shape-restore error. Zero divergence.

**LOGGED LIMITATION (found in review, deferred — not a serialization issue): aggregates over a NULL-valued column crash.** `SUM`/`AVG`/`MIN`/`MAX` over a column that contains `NULL` raises `TypeError` (e.g. `MIN(amount)` when an amount is NULL — which outer joins can produce). SQL ignores NULLs in these aggregates; we don't yet. `COUNT(*)` is unaffected. Cheap fix when prioritized: skip `None` values in the sum/min/max accumulation. Until then, aggregate over non-nullable columns.

## HAVING + DISTINCT — ✅ DONE (this session)

Higher SQL coverage, TDD.
- **DISTINCT** is a new stateful operator (`DistinctOp`): keeps per-row net weight and flips a row in/out only as its weight crosses zero (collapse multiplicities to set semantics). New `plan.Distinct`, independent oracle branch (`{row: 1 for w>0}`), compile branch, snapshot state (`_state_attrs=("_counts",)`), and `SELECT DISTINCT` in the front-end. Verified: 10-seed property test vs oracle + an adversarial BATCHED multi-sign probe (many presence crossings per delta) + SQLite cross-check.
- **HAVING** is front-end only — a `Filter` on the aggregate output (the stateless filter on the aggregate's retract-old/assert-new deltas correctly adds/removes a group as its aggregate crosses the predicate boundary, verified by a boundary-oscillation test). HAVING operands resolve against the post-aggregate schema; `COUNT(*)`/`SUM(col)` in HAVING are matched to the SELECTed aggregate's output column. Aggregates are now rejected in `WHERE`.
- Verified against **stdlib SQLite** (independent semantics oracle) over random insert/delete streams for DISTINCT (incl. with WHERE) and HAVING (COUNT/SUM, alias and `COUNT(*)` forms, `AND` with a group-column predicate). ~50 new tests.
- LOGGED v1 limitation: a HAVING aggregate must also appear in SELECT (else a clear error). `COUNT(col)` is treated as `COUNT(*)` (pre-existing).

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
