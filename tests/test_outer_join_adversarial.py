"""Adversarial probe for the outer joins — hunting the silent wrong answer.

The per-join property tests apply ONE row per step. This probe instead applies
BATCHED deltas (several rows at once, mixed inserts and deletes at a shared key),
which is the code path where the before/after match-status snapshot must resolve
by NET effect. It also throws in the type edge cases that have bitten this
project before: NULLs in data columns (indistinguishable in shape from a NULL
pad), floats, negatives, and duplicate rows (weight > 1). The bar is unchanged:
incremental == oracle after every batch, for LEFT, RIGHT and FULL.
"""

import random

import pytest

from ivm.zset import ZSet
from ivm.plan import Source, LeftJoin, RightJoin, FullJoin
from ivm.engine import Engine

from harness import add, oracle_result

L = ("k", "lval")
R = ("rk", "k", "rval")

JOIN_NODES = {"left": LeftJoin, "right": RightJoin, "full": FullJoin}

# data pools chosen to be nasty: None (NULL in data), floats, negatives, 0
LVALS = [None, "x", -1, 0.5]
RVALS = [None, 3, -2, 1.5]


def _rand_left(rng):
    return (rng.randint(0, 2), rng.choice(LVALS))


def _rand_right(rng):
    return (rng.randint(0, 100), rng.randint(0, 2), rng.choice(RVALS))


@pytest.mark.parametrize("kind", ["left", "right", "full"])
@pytest.mark.parametrize("seed", range(12))
def test_outer_join_batched_adversarial(kind, seed):
    rng = random.Random(seed)
    plan = JOIN_NODES[kind](Source("l", L), Source("r", R), ("k",), ("k",))
    eng = Engine()
    view = eng.add_view("v", plan)
    tables: dict = {}
    live = {"l": [], "r": []}

    def build_batch(table):
        """A batch mixing inserts (some duplicating live rows -> weight>1) and
        deletes of live rows, all landing on the tiny key pool."""
        batch = ZSet()
        for _ in range(rng.randint(1, 5)):
            pool = live[table]
            if pool and rng.random() < 0.5:
                row = pool.pop(rng.randrange(len(pool)))
                batch = batch + ZSet({row: -1})
            else:
                row = _rand_left(rng) if table == "l" else _rand_right(rng)
                pool.append(row)
                batch = batch + ZSet({row: +1})
        return batch

    for _ in range(120):
        table = rng.choice(["l", "r"])
        batch = build_batch(table)
        eng.apply(table, batch)
        add(tables, table, batch)
        assert view.result() == oracle_result(plan, tables), (
            f"{kind} join diverged from oracle after a batch on {table}"
        )

    # drain both sides to empty
    for table in ("l", "r"):
        while live[table]:
            row = live[table].pop()
            d = ZSet({row: -1})
            eng.apply(table, d)
            add(tables, table, d)
            assert view.result() == oracle_result(plan, tables)
    assert view.result() == {}


def test_full_join_null_data_vs_null_pad_matches_oracle():
    """A left row whose non-key column is genuinely NULL produces a matched row
    shaped like a right-pad. The engine must still equal the oracle (both treat
    it identically) — documents that NULL-in-data and NULL-pad are the same
    tuple, an inherent representational tie, not a divergence."""
    plan = FullJoin(Source("l", L), Source("r", R), ("k",), ("k",))
    eng = Engine()
    view = eng.add_view("v", plan)
    tables: dict = {}

    d = ZSet({(1, None): +1})  # left row, lval is NULL
    eng.apply("l", d)
    add(tables, "l", d)
    d = ZSet({(9, 1, 7): +1})  # right row matching key 1
    eng.apply("r", d)
    add(tables, "r", d)
    assert view.result() == oracle_result(plan, tables)
    # now an unmatched right row at key 2: right-pad has left cols NULL too
    d = ZSet({(10, 2, 7): +1})
    eng.apply("r", d)
    add(tables, "r", d)
    assert view.result() == oracle_result(plan, tables)
