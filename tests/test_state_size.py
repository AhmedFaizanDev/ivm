"""State-size baselines for the eventual Rust port (build-plan deferred item).

The Materialize post-mortem's lesson: state-size engineering, not algorithm
correctness, is the long-term cost of this approach. So we pin two properties per
stateful operator:

  * LINEAR — retained state is exactly the current inputs, no quadratic blow-up;
  * LEAK-FREE — after draining every row, the operator's state returns to empty.

These are whitebox (they read operator internals on purpose) and double as the
recorded baseline: join keeps one index entry per input row per side; an
aggregate keeps one group per live key (plus, for MIN/MAX, a per-group value
multiset). If a future change makes state grow super-linearly or leak on delete,
one of these turns red.
"""

import random

import pytest

from ivm.zset import ZSet
from ivm.operators import JoinOp, LeftJoinOp, RightJoinOp, FullJoinOp, AggregateOp
from ivm.plan import Count, Sum, Min, Max

LS = ("k", "lv")
RS = ("rk", "k", "rv")


def _entries(index):
    return sum(len(bucket) for bucket in index.values())


@pytest.mark.parametrize("cls", [JoinOp, LeftJoinOp, RightJoinOp, FullJoinOp])
def test_join_state_linear_and_leak_free(cls):
    rng = random.Random(1)
    op = cls(("k",), ("k",), LS, RS)
    left = [(rng.randint(0, 9), i) for i in range(300)]      # distinct (unique lv)
    right = [(i, rng.randint(0, 9), i) for i in range(300)]  # distinct (unique rk)
    for r in left:
        op.on_left(ZSet({r: +1}))
    for r in right:
        op.on_right(ZSet({r: +1}))

    # LINEAR: state is exactly the retained inputs on each side
    assert _entries(op._left_index) == len(left)
    assert _entries(op._right_index) == len(right)

    # LEAK-FREE: drain everything -> state empty
    for r in left:
        op.on_left(ZSet({r: -1}))
    for r in right:
        op.on_right(ZSet({r: -1}))
    assert op._left_index == {}
    assert op._right_index == {}


def test_aggregate_state_bounded_and_leak_free():
    op = AggregateOp(
        ("cat",),
        (Count("n"), Sum("s", "amt"), Min("lo", "amt"), Max("hi", "amt")),
        ("cat", "amt"),
    )
    rng = random.Random(2)
    rows = [(rng.choice(["a", "b", "c"]), rng.randint(-4, 4)) for _ in range(400)]
    for r in rows:
        op.on_input(ZSet({r: +1}))

    # BOUNDED: exactly one group per distinct live key (keys are group-by tuples)
    assert set(op._groups) == {(r[0],) for r in rows}

    # LEAK-FREE: drain -> every group (and its MIN/MAX multiset) vanishes
    for r in rows:
        op.on_input(ZSet({r: -1}))
    assert op._groups == {}
