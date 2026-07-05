# Paper outline (spine) — an embeddable, correctness-first IVM library

*A skeleton to make the advising conversation concrete. NOT the paper. Numbers
are from `bench/results.json` (CPython, 50k-row base); prior art is in
`ivm-related-work.md`. Venue is the advisor's call — candidate types only.*

## Working titles

- "ivm: An Embeddable, Oracle-Verified Incremental View Maintenance Library"
- "Differential Testing as the Safety Net for Incremental View Maintenance"
  (if we lead with the correctness-methodology contribution)

## Abstract (draft)

Incremental view maintenance (IVM) keeps a declared SQL view fresh by applying
only the changes to its base tables. The theory matured recently (DBSP, VLDB
2023), and production systems exist (Materialize, RisingWave, Feldera, Databricks
Enzyme) — but every one is heavyweight infrastructure: a separate service or
cloud platform. The lightweight slot, *a library you link into a process the way
you link SQLite*, is empty. We present `ivm`, an embeddable, in-process IVM engine
(Z-sets + differential operators) with a SQL front-end, change-capture adapters
(in-process and SQLite), snapshot/restore, and an experimental incremental-vs-
recompute cost model. Its distinguishing feature is a **correctness methodology**:
every operator, adapter, and SQL query is proven by differential testing against
an independent from-scratch recompute oracle over random insert/delete streams —
the failure mode IVM is infamous for (a silently wrong aggregate after a delete)
is made loud. We report throughput/latency/state-size and an incremental-vs-
recompute speedup study that both demonstrates IVM's win for small change sets
and exposes a concrete gap in naive cost models for joins.

## Contributions (the three claims)

1. **An embeddable, in-process, open-source IVM library for real SQL** — tier-1/2
   coverage (filter, project, inner + LEFT/RIGHT/FULL outer joins, GROUP BY with
   COUNT/SUM/AVG/MIN/MAX, DISTINCT, HAVING), a zero-dependency SQL front-end, an
   in-process and a SQLite change-capture adapter, and snapshot/restore so a
   restart doesn't force a full recompute. Fills the empty "library, not service"
   slot (§ positioning).
2. **A differential-testing correctness methodology for IVM** — a recompute
   oracle that shares no code with the incremental operators; every feature
   asserted `incremental == oracle` after each delta over random streams (deletes
   and empty groups always exercised), plus adversarial batched-multi-sign probes
   and, for the SQL layer, cross-checking against SQLite as an independent
   semantics oracle. We argue this is the right practical guarantee for IVM and
   show it catches real silent bugs (e.g. a float-precision capture bug we found
   and fixed). ~660 tests.
3. **An empirical incremental-vs-recompute study with an honest negative result** —
   incremental is ~3,600× faster than recompute for a small update but the
   advantage collapses on bulk updates; a naive cost model (changed-rows ÷
   base-rows) catches this for aggregates but *not* for joins, whose cost is
   driven by output amplification (input × fan-out), not input size. Concrete
   motivation for fan-out-aware cost estimation.

## Positioning (vs `ivm-related-work.md`)

| Prior art | Form | Gap we fill |
|---|---|---|
| DBSP (VLDB'23), Koch et al. (PODS'16) | theory / algebra | we build an *embeddable artifact* on it |
| Materialize, RisingWave, Feldera | streaming services | in-process library, no service |
| Enzyme (SIGMOD'26, Databricks) | closed Spark platform; has a cost model | open, embeddable; we study the cost model empirically and show where a naive one fails |
| OpenIVM (SIGMOD'24) | SQL-to-SQL on DuckDB, research artifact | stable library API + adapters + snapshotting |
| cr-sqlite, pg_ivm | must mark tables / Postgres-only | retrofit any schema via change capture |
| Stateful Diff. Operators (POPL'26) | verified operators | complementary: we use *testing*; verified operators are future work |

## Methodology (what to write up)

- Z-set kernel; operators as delta-in/delta-out; the bilinear join (retains both
  inputs); non-linear operators (MIN/MAX via per-group value multiset with
  delete-recovery; DISTINCT via per-row weight crossing zero).
- The oracle (`eval_plan`) and the discipline that keeps it independent.
- SQL compilation to the plan API (the plan API is the stable core).
- Change capture: the SQLite update-hook trap (silently misses WITHOUT ROWID,
  ON CONFLICT REPLACE, truncate DELETE) and why session-extension / trigger
  changelogs are needed; capture tested *through* the adapter.
- Snapshot/restore: what state each operator holds and why it's linear + leak-free.

## Evaluation plan (populated — `bench/results.json`)

- **Throughput / latency**: ~37k single-row deltas/sec through a 6-aggregate
  GROUP BY view; latency p50 21µs / p99 43µs (warm 50k rows).
- **State size**: aggregate view ~0.2 bytes/row (O(distinct groups)); join view
  ~31 bytes/input-row (O(rows)) — relate to Materialize's 0–16-bytes-per-record
  post-mortem as the Rust-port target.
- **Money graph (aggregate)**: incremental speedup vs full recompute = 3,612×,
  808×, 310×, 33×, 4.3×, 1.5× as the batch grows 1 → 50k (100% of base).
- **Money graph (fan-out join)**: 12.2× → 6.8× → 2.6× → 0.89× → 0.68× — incremental
  *loses* beyond ~batch 500; recompute wins on bulk.
- **Cost-model result (the honest one)**: the 0.5 input-ratio heuristic stays
  "incremental" at batch 2000 where recompute is 1.5× faster → naive cost models
  miss join output-amplification.
- **Correctness as evaluation**: report the test methodology + the concrete
  silently-wrong bug it caught (float-precision changelog) as evidence.

## Limitations (from the honest build log)

- Python prototype (Rust port future); tier-3 (recursive CTEs, window functions)
  out of scope; aggregates over NULL columns not yet SQL-correct (logged);
  identifiers case-sensitive; cost model experimental (see negative result);
  no formal proofs (testing, not verification).

## Candidate venue TYPES (advisor decides — do not commit)

- **CIDR** — systems-architecture, "innovative," single-track with a demo slot;
  best fit for the embeddable-library systems idea and the cost-model finding.
- **DBTest (VLDB workshop, 4–6 pp)** — near-perfect for the differential-testing
  correctness methodology as the lead contribution; smallest, most achievable
  first paper for a student + advisor.
- **SIGMOD / VLDB demo track** — the runnable `pip install` library + the live
  dashboard demo as the artifact.
- Fallbacks: DEEM / DaMoN / a short paper.

Sources for venue types: [CIDR 2026](https://www.cidrdb.org/cidr2026/),
[DBTest 2026](https://dbtest-workshop.github.io/),
[DBSP (VLDB Journal 2025)](https://dl.acm.org/doi/10.1007/s00778-025-00922-y).
