**Incremental View Maintenance: Related Work and Open Gaps**

*A survey to scope the research project. Last updated June 2026.*

## Scope

The question this survey answers: for keeping a derived SQL view fresh as base data changes, what is already solved, and what is still open? The target we care about is an *embeddable, in-process, open-source library* for arbitrary SQL, with no external service. We sort the literature by how close it gets to that target.

## 1. Foundations (theory)

These give the algebra for turning a query into its incremental form. They are building blocks, not usable products.

- **DBSP** (VLDB 2023, best paper). A stream-of-changes algebra (Z-sets and differential operators) that derives the incremental version of a query mechanically, covering a rich query language. This is the current standard foundation.
- **Differential dataflow / timely dataflow** (McSherry et al.). A powerful Rust dataflow model for incremental computation. No SQL surface; steep learning curve; used as an engine, not a library you point at a database.
- **Incremental View Maintenance for Collection Programming** (Koch et al., PODS 2016). Higher-order delta processing; the theory behind DBToaster.
- **Stateful Differential Operators for Incremental Computing** (Xu and Erdweg, POPL 2026). [link](https://dl.acm.org/doi/10.1145/3776728) A formally verified (in Rocq/Coq) specification for differential operators that carry internal state. Significant because it shows the operator toolbox is *still being extended and proven* in 2026, with room for new, more efficient operators. This is a research opening, not a product.

## 2. Systems (heavyweight / infrastructure)

These work in production but are services or platforms, not embeddable libraries.

- **Materialize, RisingWave.** Standalone streaming databases that maintain views incrementally and correctly. They require running a separate distributed service with a real minimum footprint and cost.
- **Feldera.** A containerized runtime built directly on the DBSP framework. Closest to the theory, but still infrastructure rather than a library.
- **Enzyme: Incremental View Maintenance for Data Engineering** (SIGMOD 2026, Databricks). [link](https://arxiv.org/html/2603.27775) A closed, internal Databricks system built on Apache Spark, validated on thousands of production pipelines. Broad SQL coverage (projections, filters, joins including outer, aggregations, window functions; deletions via change data feed). Two takeaways for us: (a) it is the opposite of embeddable (heavyweight cloud platform, not open source, aimed at data engineers); (b) it keeps a **cost model that falls back to full recompute** when a large fraction of the data changes. That cost-based hybrid is an idea worth carrying into an embeddable design.
- **Noria** (Gjengset et al., OSDI 2018). Partially-stateful dataflow for read-heavy web apps; demonstrated strong results and real OSS interest, but runs as a server, not an in-process library.

## 2b. Practitioner evidence (what actually stalls these projects)

Added 2026-07 from a verified deep-research pass (25/25 claims confirmed).

- **pydbsp** (brurucy). [link](https://github.com/brurucy/pydbsp) A pure-Python, zero-dependency, MIT-licensed DBSP implementation: single contributor, ~156 stars, actively released through June 2026, covering incremental SQL operators, doubly-incremental joins, and incremental GROUP BY aggregation. Explicitly "primarily meant for research." **The existence proof that our Python-first prototype is feasible, and the reference implementation to study.** It does not demonstrate outer joins or MIN/MAX — our tier-2 rests on the DBSP paper's formal results, not pydbsp's code.
- **Materialite** (Matt Wonlaw, cr-sqlite author). [link](https://github.com/vlcn-io/materialite) A solo-built JavaScript DBSP-style IVM library (filter/map/reduce/join, reactive updates). Wonlaw's verdict on the delta algebra: "once you get into it... it's not so bad." What stalled it was not the math: (a) user-provided lambdas are opaque, blocking index use — which motivates a declarative SQL front-end, exactly our plan; (b) it never covered window functions or recursive queries. He also benchmarked re-executing SQLite queries on change versus incremental in-memory updates: "the difference was massive" — validating the delta-engine design over re-query reactivity.
- **Materialize memory post-mortem.** [link](https://materialize.com/blog/materialize-and-memory/) First-party admission: maintained state started at ~96 bytes of overhead per record; months of optimization (led by differential dataflow's own creator) brought it to 0-16 bytes. Lesson: **state-size engineering, not algorithm correctness, is the dominant long-term cost** of this approach — the wall to expect at joins/DISTINCT/MIN-MAX.
- **SQLite change capture** (official docs). [update hook](https://sqlite.org/c3ref/update_hook.html), [session extension](https://sqlite.org/sessionintro.html). The update hook silently misses WITHOUT ROWID tables, ON CONFLICT REPLACE deletes, and truncate-optimized deletes, and never carries column values. The session extension is the documented in-process path (row-level changesets with values) but needs non-default compile flags (apsw wheels have them; Python stdlib does not) and declared primary keys. This makes naive hook-based capture the biggest correctness trap for an embeddable IVM adapter.

## 3. Embeddable or lightweight attempts (closest to our target, all incomplete)

- **DBToaster.** Generates C++ for aggressive incremental maintenance. Academically dormant since roughly 2017; the generated code is impractical to embed in a modern polyglot stack.
- **OpenIVM** (SIGMOD 2024). A SQL-to-SQL incremental compiler demonstrated on DuckDB. The most relevant prototype, but it is a research artifact without a stable library API or packaging.
- **cr-sqlite** (vlcn-io). A SQLite extension adding CRDT support and partial incremental maintenance. Requires marking tables at creation time, cannot retrofit an existing schema, and the maintainer lists full IVM as still missing (Discussion #309). [link](https://github.com/vlcn-io/cr-sqlite/discussions/309)
- **pg_ivm.** An incremental view maintenance extension for PostgreSQL that lives outside core. Postgres-only and not embeddable elsewhere; core PostgreSQL still recomputes materialized views in full.

## 4. Adjacent data models and niches

These confirm IVM is an active topic but target different problems.

- **Incremental View Maintenance for SPARQL Queries: Adapting the Counting Algorithm** (2026). [link](https://dl.acm.org/doi/pdf/10.1145/3796549) Brings the classic counting algorithm to RDF/SPARQL graph queries. Important finding for us: for small change sets, computing from scratch can be faster than incremental maintenance; incremental wins as the change volume grows. Direct evidence that "negligible overhead" is workload-dependent.
- **In-memory Incremental Maintenance of Provenance Sketches** (Li, Glavic et al., arXiv 2025). [link](https://arxiv.org/abs/2505.20683) Keeps provenance sketches (a data-skipping optimization) fresh after updates. A narrow optimization, not general IVM.

## What is solved versus open

| Capability | Status |
|---|---|
| Theory to incrementalize rich SQL | Solved and still advancing (DBSP 2023; new verified operators at POPL 2026) |
| IVM inside a heavyweight cloud or streaming platform | Solved (Materialize, RisingWave, Feldera, Enzyme) |
| IVM for graph / SPARQL, and niche optimizations | Active, separate line of work |
| Cost-based choice between incremental and full recompute | Demonstrated in a closed system (Enzyme), not in any embeddable library |
| An embeddable, in-process, open-source IVM library for arbitrary SQL with no external service | **Open. No production-quality artifact exists.** |

## The gap this project targets

Every group that has made IVM work in practice did it inside heavyweight infrastructure (a cluster, a cloud platform, a separate database). The lightweight slot, a library you link into an application the way SQLite or DuckDB is linked, remains empty. The theory needed to fill it matured only recently (DBSP in 2023, new verified operators in 2026), and the one production system that proves the hybrid cost-model idea (Enzyme) is closed and Spark-based. That combination, a real and recurring need, recent-enough foundations, and no open embeddable artifact, is what makes this worth a dev project, a research project, or both. See the architecture note for the proposed design.

## References

- *DBSP: Automatic Incremental View Maintenance for Rich Query Languages.* VLDB 2023.
- *OpenIVM: a SQL-to-SQL compiler for incremental computation on DuckDB.* SIGMOD 2024.
- *Incremental View Maintenance for Collection Programming.* PODS 2016.
- [*Stateful Differential Operators for Incremental Computing.* POPL 2026.](https://dl.acm.org/doi/10.1145/3776728)
- [*Enzyme: Incremental View Maintenance for Data Engineering.* SIGMOD 2026 (Databricks).](https://arxiv.org/html/2603.27775)
- [*Incremental View Maintenance for SPARQL Queries: Adapting the Counting Algorithm.* 2026.](https://dl.acm.org/doi/pdf/10.1145/3796549)
- [*In-memory Incremental Maintenance of Provenance Sketches.* arXiv 2025.](https://arxiv.org/abs/2505.20683)
- *Noria: dynamic, partially-stateful dataflow.* OSDI 2018.
- [cr-sqlite, Discussion #309: reactivity and live queries.](https://github.com/vlcn-io/cr-sqlite/discussions/309)
- *pg_ivm:* incremental view maintenance extension for PostgreSQL.
- *DBToaster*, *Materialize*, *RisingWave*, *Feldera*: industry and prior-art systems.
