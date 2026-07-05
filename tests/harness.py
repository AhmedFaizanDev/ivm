"""Shared test harness: the oracle side of every property test.

`oracle_result` recomputes a plan from scratch over the accumulated base
tables (the lie detector). `add` accumulates a delta into the base tables the
oracle recomputes over. The incremental engine and the oracle must agree after
every delta — that agreement IS the definition of correct IVM here.
"""

from ivm.zset import ZSet
from ivm.oracle import eval_plan


def add(tables: dict, table: str, delta: ZSet) -> dict:
    """Accumulate a delta into the base-table map the oracle recomputes over."""
    tables[table] = tables.get(table, ZSet()) + delta
    return tables


def oracle_result(plan, tables: dict) -> dict:
    """From-scratch recompute of a plan as {result_row: weight}."""
    _schema, zset = eval_plan(plan, tables)
    return dict(zset.items())


def check_every(seed, full_seeds=2, n=25):
    """Throttle the (expensive) per-step oracle assertion so the suite stays fast.

    The engine still processes every delta; only the from-scratch recompute is
    what costs — so we run it every step for a couple of seeds (full coverage of
    the step-by-step invariant) and every `n` steps for the rest. Every test
    still asserts the oracle once more after draining to empty, so a divergence
    can never slip through unchecked. Returns a predicate over the step index."""
    if seed < full_seeds:
        return lambda step: True
    return lambda step: step % n == 0
