**Embeddable Incremental View Maintenance: Architecture and Research Plan**

*A proposed design and a phased plan. Last updated June 2026.*

## Goal and non-goals

**Goal.** A library you link into an application (Rust core with bindings, in the style of SQLite or DuckDB) that, given a declared SQL view and a stream of changes to its base tables, keeps the view current by applying only the deltas, with no external service.

**Note on first implementation.** Rust is the eventual home for the shippable library, but the first working version is built in Python to validate the algebra and correctness cheaply, then ported. See `ivm-build-plan.md` for the concrete build order.

**Approach validation (2026-07).** A verified deep-research pass (25/25 claims confirmed) endorsed this stack: DBSP's chain rule is deterministic and covers essentially all of SQL (no expressiveness wall); tier-1 operators (filter, project, COUNT, SUM) are provably their own incremental versions with no extra state; equi-join has a closed-form bilinear delta but requires retaining full state of both inputs. Three corrections adopted from the evidence: (1) start directly with Z-set operators — the classic counting algorithm is subsumed by DBSP linearity, so no separate counting step; (2) the SQLite adapter must use the session extension (via apsw) or trigger-based changelogs, never the update hook alone, which silently drops several classes of changes; (3) the Enzyme-style cost model is unvalidated by any public source — treat it as an experiment and possible contribution, not a known recipe. Known blind spot of Python-first: state-size behavior (the Materialize 96-bytes-per-record problem) only surfaces in the Rust port; the prototype should record state-size baselines in tests. Reference implementation: pydbsp (pure-Python DBSP, solo-built, actively maintained).

**Non-goals (for the first version).** Distributed execution, multi-node scaling, a query language of its own, and a managed cloud product. Those are later or separate concerns. The point of the first version is to prove the embeddable single-process case.

## Where it sits

Between "recompute on a timer" (simple, wasteful, stale) and "run a streaming cluster" (correct, heavy, operationally expensive). See the related-work note for why that slot is still empty.

## Design overview

Three layers, each independently testable:

```
   base-table changes (deltas)
            |
   [3] change-capture adapter   (SQLite update hook | Postgres WAL | in-process log)
            |
   [1] delta-algebra core       (Z-sets + differential operators)
            ^
   [2] SQL-to-plan compiler     (SELECT -> maintainable operator graph)
            |
        maintained view  +  [cost model: incremental or full recompute]
```

## Layer 1: delta-algebra core

The engine that, given a change to base data, produces the matching change to each view. Built on the DBSP model (Z-sets and differential operators), which is the current standard foundation and has formally specified operators (extended at POPL 2026). This layer is query-agnostic: it executes an operator graph, it does not parse SQL.

Key responsibilities: maintain per-operator state (for joins and aggregations), apply incoming deltas, emit output deltas, and expose hooks for serialization and compaction.

## Layer 2: SQL-to-incremental-plan compiler

Lowers an ordinary SELECT into a maintainable operator graph instead of re-executing the query. This is where SQL coverage is decided, and where the hard cases live. Coverage is staged (see roadmap). The OpenIVM prototype (SIGMOD 2024) is the closest reference for the SQL-to-plan step.

## Layer 3: change-capture adapters

Feeds deltas into the core. Pluggable by backend:
- SQLite update hook (the primary target; cleanest embedding story).
- PostgreSQL logical decoding / WAL stream.
- An in-process mutation log for applications that own their writes.

The adapter normalizes each backend's change feed into the core's delta format. This is the layer that decides whether the library can attach to an existing schema without a rewrite, which is the property cr-sqlite lacks.

## The hybrid cost model (carry this from Enzyme)

A core design rule, validated by the Enzyme system and by the SPARQL counting paper: **incremental is not always cheaper.** When a change touches a large fraction of the data, full recompute wins. The engine should estimate, per refresh, the cost of applying deltas versus recomputing, and pick the cheaper path. This turns "negligible overhead" from an overclaim into a guarantee: the engine is never worse than recompute by more than the estimate error.

This single feature also sharpens the value story: incremental wins for expensive views with small, frequent change sets (the derived-dashboard case), and the cost model protects the bulk-update case.

## State management

- **Serializable, resumable operator state.** A process restart must not force a full recompute. The operator graph and its state serialize to the same store as the data (or alongside it).
- **Query-aware compaction.** Bound memory by compacting operator state, with the compaction strategy informed by the view definition (for example, dropping history below an aggregation watermark).

## Correctness strategy

Incremental maintenance has the worst failure mode in databases: a silently wrong answer (for example, a wrong aggregate after a delete). The plan:
- **Differential testing against a recompute oracle.** For every supported query and random update sequence, assert the incrementally maintained view equals a full recompute. This is the primary safety net.
- **Property-based generation** of schemas, queries, and update streams (including deletes, which are where most IVM bugs live).
- **Lean on verified operators.** Where possible, use operators whose correctness is established (DBSP, and the POPL 2026 stateful-operator specification) rather than ad hoc deltas.

## SQL coverage roadmap

Staged from tractable to research-hard. The boundary between "engineering" and "research" sits around tier 3.

| Tier | Coverage | Difficulty |
|---|---|---|
| 1 | Select, project, filter; inner joins; SUM/COUNT aggregates; insert-only and update streams | Tractable; the MVP |
| 2 | Deletes everywhere; AVG/MIN/MAX (needs auxiliary state); outer joins | Hard; the correctness work concentrates here |
| 3 | Recursive CTEs; window functions; nested subqueries | Research-hard; candidate paper contributions |

## Open research questions

- How much of SQL can be incrementalized cleanly before operator state grows impractically large?
- What is the right correctness guarantee in practice: differential testing against a recompute oracle, or formal proof carried by the operator algebra (building on the POPL 2026 work)?
- Can a lightweight cost model match Enzyme's incremental-versus-recompute decisions in an embedded setting, without Enzyme's cluster-scale statistics?
- Is the in-process framing viable for all three backends, or does change capture eventually force an external component for Postgres?

## Phased plan

**Dev track (build the artifact).** The concrete, milestone-by-milestone version of this lives in `ivm-build-plan.md`. In short:
0. Weekend kernel in Python: one hand-wired GROUP BY view maintained incrementally, with the recompute-oracle test from day one (the go/no-go).
1. Generalize to a tier-1 engine (filter, project, inner join, COUNT, SUM), each operator property-tested against the oracle.
2. Attach a real change feed: an in-process mutation log first, then a SQLite adapter.
3. Tier-2 correctness (deletes in joins, AVG/MIN/MAX) and the incremental-versus-recompute cost model.
4. Decision point: port to Rust and ship, or push to tier-3 and write the paper.

**Research track (extract a contribution).** Either of:
- A systems paper on the embeddable engine plus the embedded cost model (the Enzyme idea, made open and in-process).
- A focused contribution on tier-3 coverage (recursive or windowed queries) or on the correctness story (verified operators applied to an embeddable engine), building on the POPL 2026 stateful-operator work.

Both tracks share the same codebase; the dev track produces the artifact and the GitHub profile value, the research track produces the paper.

## References

See `ivm-related-work.md` for the full annotated bibliography. Primary anchors: DBSP (VLDB 2023), OpenIVM (SIGMOD 2024), Stateful Differential Operators (POPL 2026), and Enzyme (SIGMOD 2026).
