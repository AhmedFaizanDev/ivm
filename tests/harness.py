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
