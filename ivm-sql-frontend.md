# IVM SQL front-end — design note

*A hand-written SELECT-subset compiler that turns SQL into the existing plan API.
The plan API stays the stable core; SQL is a thin compiler on top. Zero
dependencies (stdlib only), per the project's decision framework.*

## Why

The engine already maintains views declared as an explicit Python plan
(`Source/Filter/Project/Join/LeftJoin/RightJoin/FullJoin/Aggregate`). That is
powerful but not what people expect from an "IVM library." A SQL surface —
`compile_sql("SELECT category, SUM(amount) FROM t GROUP BY category", catalog)`
— is the single highest-leverage usability feature. It makes the one-liner in the
README real.

## Scope (v1 — YAGNI)

Supported:
- `SELECT [DISTINCT] * | items` where an item is a column (`col` or `table.col`),
  or an aggregate `COUNT(*) | COUNT(col) | SUM(col) | AVG(col) | MIN(col) |
  MAX(col)`, each with optional `AS alias`. `DISTINCT` compiles to the `Distinct`
  operator (collapse multiplicities to presence).
- `FROM table [alias]`.
- Zero or more joins: `[INNER|LEFT|RIGHT|FULL] [OUTER] JOIN table [alias] ON
  a.k = b.k [AND a.k2 = b.k2]*`. Bare `JOIN` = inner.
- `WHERE expr`: comparisons (`= != <> < <= > >=`, `IS NULL`, `IS NOT NULL`) over
  a column and a column-or-literal, combined with `AND` / `OR`, parenthesised.
- `GROUP BY col [, col]*` with the aggregates listed in SELECT.
- `HAVING expr` — a predicate over the aggregate output; compiles to a `Filter`
  on the aggregate. Operands are group columns, aggregate output names/aliases,
  or aggregate calls (`COUNT(*)`, `SUM(col)`) matched to a SELECTed aggregate.

Deferred (logged, not silently capped): `ORDER BY`/`LIMIT` (views are unordered
bags — irrelevant to IVM), subqueries, arithmetic expressions in SELECT,
self-joins via SQL (blocked by the plan's unique-column-name rule), and HAVING
aggregates that are not also in the SELECT list.

**Known semantic divergence (logged):** a GROUP BY-less *global* aggregate
(`SELECT COUNT(*) FROM t`) over an EMPTY relation yields no row here, whereas SQL
yields a single zero row (`COUNT=0`, `SUM=NULL`). Grouped aggregates match SQL
exactly (an absent group produces no row on both sides). This is the standard
incremental-aggregate boundary — the group vanishes at net weight 0 — and is why
the SQLite semantic cross-check covers grouped, not global, aggregates.

## Pipeline

1. **Lexer** — SQL text → tokens (case-insensitive keywords, identifiers,
   numbers, strings, operators, punctuation).
2. **Parser** — recursive descent → a small `Select` AST (select items, from,
   joins, where expr, group by).
3. **Compiler** — AST + `catalog {table: schema}` → a plan:
   - build the left-deep join tree from FROM + JOINs, tracking the accumulated
     left schema so each `ON a.k = b.k` resolves one side to the left input and
     one to the new right table (→ `left_keys` / `right_keys`);
   - `WHERE` → `Filter` with a compiled NULL-safe predicate closure;
   - `GROUP BY` + aggregates → `Aggregate`;
   - `SELECT` list → `Project` (order + aliases). `SELECT *` with no aggregate is
     the identity (no Project).

Column references (`table.col` / bare `col`) resolve to bare column names against
the catalog; ambiguous or unknown columns raise a clear error. This preserves the
plan core's bare-named-column model.

## Correctness strategy

- **Primary:** the compiled plan, run through the engine over random
  insert/delete streams, must equal BOTH the recompute oracle (`eval_plan` of the
  compiled plan) AND the trusted hand-built plan's result — proving the compiler
  emits a correct plan that means the same thing as the hand-built one.
- **Adversarial (independent SQL-semantics oracle):** execute the same SQL text
  against stdlib **SQLite** over the same data and compare the maintained view to
  SQLite's result, for the cases whose row representation matches cleanly
  (projection, filter, inner join, aggregates). This checks that our SQL actually
  means SQL, not just that it round-trips our own plan.

## Public API

`ivm.sql.compile_sql(sql: str, catalog: dict[str, tuple]) -> plan`, used as
`engine.add_view(name, compile_sql(sql, catalog))`. A thin
`engine.add_sql_view(name, sql, catalog)` convenience wraps the two.
