"""Incremental operators: each consumes input deltas and emits output deltas,
keeping only the state it needs. Wiring is push-based — an operator's output is
forwarded to whatever subscribed to it. `compile_plan` walks a plan and builds
the operator graph, sharing base-table sources across views."""

from ivm.zset import ZSet
from ivm.row import Row, index_of
from ivm import plan as P


class Operator:
    def __init__(self):
        self._subs = []

    def subscribe(self, fn):
        self._subs.append(fn)

    def _emit(self, delta):
        for fn in self._subs:
            fn(delta)


class SourceOp(Operator):
    """A base table's entry point. Shared across every view that reads it."""

    def __init__(self, schema):
        super().__init__()
        self.schema = tuple(schema)

    def push(self, delta):
        self._emit(delta)


class FilterOp(Operator):
    """Linear, stateless: apply the predicate to the delta and pass survivors."""

    def __init__(self, predicate, schema):
        super().__init__()
        self._pred = predicate
        self._idx = index_of(schema)

    def on_input(self, delta):
        kept = {row: w for row, w in delta.items() if self._pred(Row(row, self._idx))}
        self._emit(ZSet(kept))


class ProjectOp(Operator):
    """Linear, stateless: map each delta row to its output tuple, merging
    weights when distinct inputs collapse to the same output row."""

    def __init__(self, outputs, in_schema):
        super().__init__()
        self._idx = index_of(in_schema)
        self._exprs = [expr for _name, expr in outputs]

    def on_input(self, delta):
        acc = {}
        for row, w in delta.items():
            r = Row(row, self._idx)
            out_row = tuple(expr(r) for expr in self._exprs)
            acc[out_row] = acc.get(out_row, 0) + w
        self._emit(ZSet(acc))


class AggregateOp(Operator):
    """Stateful GROUP BY. Per group keeps [net_weight, running agg values].
    On a delta it snapshots the affected groups, applies the row updates, then
    emits, per affected group, a retraction of the old result row and an
    assertion of the new one — so the materialized output is always the current
    set of group rows. A group is dropped the instant its net weight hits 0."""

    def __init__(self, group_by, aggregates, in_schema):
        super().__init__()
        idx = index_of(in_schema)
        self._key_idx = [idx[c] for c in group_by]
        self._specs = []  # (kind, column_index): count|sum|avg|min|max
        for a in aggregates:
            if isinstance(a, P.Count):
                self._specs.append(("count", None))
            elif isinstance(a, P.Sum):
                self._specs.append(("sum", idx[a.column]))
            elif isinstance(a, P.Avg):
                self._specs.append(("avg", idx[a.column]))
            elif isinstance(a, P.Min):
                self._specs.append(("min", idx[a.column]))
            elif isinstance(a, P.Max):
                self._specs.append(("max", idx[a.column]))
            else:
                raise NotImplementedError(f"unknown aggregate {type(a).__name__}")
        self._groups = {}  # key -> [net_weight, per-aggregate slot...]

    def _fresh(self):
        # slot 0 is the group's net weight (COUNT); min/max keep a value multiset
        # {value: weight}, the rest keep a scalar running total.
        return [0] + [{} if kind in ("min", "max") else 0 for kind, _ci in self._specs]

    def _row_for(self, key):
        """The current output row for a group, or None if the group is absent."""
        st = self._groups.get(key)
        if st is None:
            return None
        values = tuple(_agg_value(st, i, kind) for i, (kind, ci) in enumerate(self._specs))
        return key + values

    def on_input(self, delta):
        # Snapshot each affected group BEFORE any of this delta's updates land.
        before = {}
        for row, _w in delta.items():
            key = tuple(row[i] for i in self._key_idx)
            if key not in before:
                before[key] = self._row_for(key)

        # Apply the updates.
        for row, w in delta.items():
            key = tuple(row[i] for i in self._key_idx)
            st = self._groups.get(key)
            if st is None:
                st = self._groups[key] = self._fresh()
            st[0] += w
            for i, (kind, ci) in enumerate(self._specs):
                if kind in ("sum", "avg"):
                    st[1 + i] += row[ci] * w
                elif kind in ("min", "max"):
                    ms = st[1 + i]
                    val = row[ci]
                    nw = ms.get(val, 0) + w
                    if nw == 0:
                        ms.pop(val, None)
                    else:
                        ms[val] = nw
            if st[0] == 0:
                del self._groups[key]

        # Emit retract-old / assert-new per affected group (net-zero rows drop).
        out = {}
        for key, old_row in before.items():
            new_row = self._row_for(key)
            if old_row is not None:
                out[old_row] = out.get(old_row, 0) - 1
            if new_row is not None:
                out[new_row] = out.get(new_row, 0) + 1
        self._emit(ZSet(out))


class JoinOp(Operator):
    """Inner equi-join, retaining BOTH inputs as key-indexed Z-sets (bilinear).
    A left delta joins against the current right state; a right delta against the
    current left state; each side integrates its own delta after emitting. This
    is NOT a one-sided lookup — deletes on either side must retract the exact
    combined rows they produced, which requires the full opposite-side state."""

    def __init__(self, left_keys, right_keys, left_schema, right_schema):
        super().__init__()
        lidx = index_of(left_schema)
        ridx = index_of(right_schema)
        self._lk = [lidx[c] for c in left_keys]
        self._rk = [ridx[c] for c in right_keys]
        right_keyset = set(right_keys)
        self._r_nonkey = [i for i, name in enumerate(right_schema) if name not in right_keyset]
        self._left_index = {}   # join key -> {left_row: weight}
        self._right_index = {}  # join key -> {right_row: weight}

    def _combine(self, lrow, rrow):
        return lrow + tuple(rrow[i] for i in self._r_nonkey)

    def on_left(self, delta):
        out = {}
        for lrow, lw in delta.items():
            key = tuple(lrow[i] for i in self._lk)
            for rrow, rw in self._right_index.get(key, {}).items():
                combined = self._combine(lrow, rrow)
                out[combined] = out.get(combined, 0) + lw * rw
        _integrate(self._left_index, self._lk, delta)
        self._emit(ZSet(out))

    def on_right(self, delta):
        out = {}
        for rrow, rw in delta.items():
            key = tuple(rrow[i] for i in self._rk)
            for lrow, lw in self._left_index.get(key, {}).items():
                combined = self._combine(lrow, rrow)
                out[combined] = out.get(combined, 0) + lw * rw
        _integrate(self._right_index, self._rk, delta)
        self._emit(ZSet(out))


def _integrate(index, key_idx, delta):
    """Fold a delta into a key -> {row: weight} index, dropping zeros."""
    for row, w in delta.items():
        key = tuple(row[i] for i in key_idx)
        bucket = index.get(key)
        if bucket is None:
            bucket = index[key] = {}
        new = bucket.get(row, 0) + w
        if new == 0:
            bucket.pop(row, None)
            if not bucket:
                del index[key]
        else:
            bucket[row] = new


def _agg_value(st, i, kind):
    """Read aggregate i's value from a group accumulator [net_weight, v0, v1, ...]."""
    if kind == "count":
        return st[0]  # COUNT(*) is the group's net weight
    if kind == "avg":
        return st[1 + i] / st[0]  # running sum / count
    if kind == "min":
        return min(st[1 + i])  # smallest present value in the multiset
    if kind == "max":
        return max(st[1 + i])  # largest present value in the multiset
    return st[1 + i]  # SUM


def compile_plan(node, engine):
    """Build an operator graph for a plan. Returns (root_operator, schema)."""
    if isinstance(node, P.Source):
        op = engine._get_source(node.table, node.schema)
        return op, op.schema

    if isinstance(node, P.Filter):
        child, schema = compile_plan(node.input, engine)
        op = FilterOp(node.predicate, schema)
        child.subscribe(op.on_input)
        return op, schema

    if isinstance(node, P.Project):
        child, schema = compile_plan(node.input, engine)
        op = ProjectOp(node.outputs, schema)
        child.subscribe(op.on_input)
        out_schema = tuple(name for name, _ in node.outputs)
        return op, out_schema

    if isinstance(node, P.Aggregate):
        child, schema = compile_plan(node.input, engine)
        op = AggregateOp(node.group_by, node.aggregates, schema)
        child.subscribe(op.on_input)
        out_schema = tuple(node.group_by) + tuple(a.name for a in node.aggregates)
        return op, out_schema

    if isinstance(node, P.Join):
        lop, ls = compile_plan(node.left, engine)
        rop, rs = compile_plan(node.right, engine)
        right_keyset = set(node.right_keys)
        r_nonkey = [i for i, name in enumerate(rs) if name not in right_keyset]
        out_schema = tuple(ls) + tuple(rs[i] for i in r_nonkey)
        if len(set(out_schema)) != len(out_schema):
            raise ValueError(f"join output has duplicate column names: {out_schema}")
        op = JoinOp(node.left_keys, node.right_keys, ls, rs)
        lop.subscribe(op.on_left)
        rop.subscribe(op.on_right)
        return op, out_schema

    raise NotImplementedError(f"compiler has no rule for {type(node).__name__}")
