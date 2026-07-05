"""Incremental operators: each consumes input deltas and emits output deltas,
keeping only the state it needs. Wiring is push-based — an operator's output is
forwarded to whatever subscribed to it. `compile_plan` walks a plan and builds
the operator graph, sharing base-table sources across views."""

from ivm.zset import ZSet
from ivm.row import Row, index_of
from ivm import plan as P


class Operator:
    # Names of instance attributes that make up this operator's serializable
    # state. Stateless operators (filter/project/source) keep the empty default.
    _state_attrs = ()

    def __init__(self):
        self._subs = []

    def subscribe(self, fn):
        self._subs.append(fn)

    def _emit(self, delta):
        for fn in self._subs:
            fn(delta)

    def state(self):
        """A serializable snapshot of this operator's state (empty if stateless)."""
        return {name: getattr(self, name) for name in self._state_attrs}

    def load(self, snap):
        for name, value in snap.items():
            setattr(self, name, value)


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


class DistinctOp(Operator):
    """SELECT DISTINCT. Keeps the accumulated net weight per row; emits +1 for a
    row the instant its weight crosses 0 -> positive, and -1 the instant it
    crosses back positive -> 0, so the output is always the set of present rows
    each at weight 1. Batched multi-sign deltas resolve by net effect."""

    _state_attrs = ("_counts",)

    def __init__(self):
        super().__init__()
        self._counts = {}  # row -> accumulated net weight

    def on_input(self, delta):
        out = {}
        for row, w in delta.items():
            old = self._counts.get(row, 0)
            new = old + w
            if new == 0:
                self._counts.pop(row, None)
            else:
                self._counts[row] = new
            if new > 0 and old <= 0:
                out[row] = out.get(row, 0) + 1
            elif old > 0 and new <= 0:
                out[row] = out.get(row, 0) - 1
        self._emit(ZSet(out))


class AggregateOp(Operator):
    """Stateful GROUP BY. Per group keeps [net_weight, running agg values].
    On a delta it snapshots the affected groups, applies the row updates, then
    emits, per affected group, a retraction of the old result row and an
    assertion of the new one — so the materialized output is always the current
    set of group rows. A group is dropped the instant its net weight hits 0."""

    _state_attrs = ("_groups",)

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
    combined rows they produced, which requires the full opposite-side state.

    This also makes diamonds / self-joins correct (one source feeding both
    inputs). Propagation is synchronous and depth-first, so the two sides run
    sequentially; because each side emits against the OTHER side's current state
    and integrates after, whichever side runs second sees the first's update and
    contributes the delta-left ⋈ delta-right cross-term exactly once — in either
    order. Verified in tests/test_self_join.py."""

    _state_attrs = ("_left_index", "_right_index")

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


class LeftJoinOp(Operator):
    """LEFT OUTER equi-join. Retains both inputs like the inner join, and adds
    NULL-padded output for left rows whose key has no right match.

    A left delta behaves like the inner join, plus: any delta'd left row landing
    on an unmatched key contributes (or retracts) its NULL-padded form. A right
    delta is the hard case — besides the inner-join change, if it flips a key
    between empty and non-empty, EVERY left row at that key flips between padded
    and matched: empty -> non-empty retracts the pads (the inner part asserts the
    matched rows), non-empty -> empty asserts the pads (the inner part retracts
    the matched rows). Emptiness is snapshotted before integrating the delta and
    re-checked after, so a batched right delta is handled by net effect."""

    _state_attrs = ("_left_index", "_right_index")

    def __init__(self, left_keys, right_keys, left_schema, right_schema):
        super().__init__()
        lidx = index_of(left_schema)
        ridx = index_of(right_schema)
        self._lk = [lidx[c] for c in left_keys]
        self._rk = [ridx[c] for c in right_keys]
        right_keyset = set(right_keys)
        self._r_nonkey = [i for i, name in enumerate(right_schema) if name not in right_keyset]
        self._pad = (None,) * len(self._r_nonkey)
        self._left_index = {}   # join key -> {left_row: weight}
        self._right_index = {}  # join key -> {right_row: weight}

    def _combine(self, lrow, rrow):
        return lrow + tuple(rrow[i] for i in self._r_nonkey)

    def _nullpad(self, lrow):
        return lrow + self._pad

    @staticmethod
    def _empty(index, key):
        return not index.get(key)  # missing or {} -> no match

    def on_left(self, delta):
        out = {}
        for lrow, lw in delta.items():
            key = tuple(lrow[i] for i in self._lk)
            bucket = self._right_index.get(key)
            if bucket:  # matched: combine with every current right row
                for rrow, rw in bucket.items():
                    combined = self._combine(lrow, rrow)
                    out[combined] = out.get(combined, 0) + lw * rw
            else:  # unmatched: NULL-pad
                pad = self._nullpad(lrow)
                out[pad] = out.get(pad, 0) + lw
        _integrate(self._left_index, self._lk, delta)
        self._emit(ZSet(out))

    def on_right(self, delta):
        out = {}
        keys = {tuple(rrow[i] for i in self._rk) for rrow, _rw in delta.items()}
        before_empty = {k: self._empty(self._right_index, k) for k in keys}

        # inner-join change: current left rows joined with the right delta
        for rrow, rw in delta.items():
            key = tuple(rrow[i] for i in self._rk)
            for lrow, lw in self._left_index.get(key, {}).items():
                combined = self._combine(lrow, rrow)
                out[combined] = out.get(combined, 0) + lw * rw

        _integrate(self._right_index, self._rk, delta)

        # NULL-pad flips for keys whose match-status changed
        for key in keys:
            after_empty = self._empty(self._right_index, key)
            if before_empty[key] and not after_empty:
                sign = -1  # became matched: retract pads
            elif not before_empty[key] and after_empty:
                sign = +1  # became unmatched: assert pads
            else:
                continue
            for lrow, lw in self._left_index.get(key, {}).items():
                pad = self._nullpad(lrow)
                out[pad] = out.get(pad, 0) + sign * lw
        self._emit(ZSet(out))


class RightJoinOp(Operator):
    """RIGHT OUTER equi-join — the mirror of LeftJoinOp. Preserves all right
    rows; an unmatched right row is right-padded (left non-key NULL, the shared
    key coalesced from the right row). The flip is driven by LEFT-side
    transitions: a left insert/delete that flips a key between "no left" and
    "some left" flips every right row at that key between padded and matched."""

    _state_attrs = ("_left_index", "_right_index")

    def __init__(self, left_keys, right_keys, left_schema, right_schema):
        super().__init__()
        lidx = index_of(left_schema)
        ridx = index_of(right_schema)
        self._lk = [lidx[c] for c in left_keys]
        self._rk = [ridx[c] for c in right_keys]
        right_keyset = set(right_keys)
        self._r_nonkey = [i for i, name in enumerate(right_schema) if name not in right_keyset]
        self._left_len = len(left_schema)
        self._left_index = {}   # join key -> {left_row: weight}
        self._right_index = {}  # join key -> {right_row: weight}

    def _combine(self, lrow, rrow):
        return lrow + tuple(rrow[i] for i in self._r_nonkey)

    def _rightpad(self, rrow):
        # left non-key columns are NULL; the shared key comes from the right row
        parts = [None] * self._left_len
        for lpos, rpos in zip(self._lk, self._rk):
            parts[lpos] = rrow[rpos]
        return tuple(parts) + tuple(rrow[i] for i in self._r_nonkey)

    @staticmethod
    def _empty(index, key):
        return not index.get(key)

    def on_right(self, delta):  # easy side: match against current left, else pad
        out = {}
        for rrow, rw in delta.items():
            key = tuple(rrow[i] for i in self._rk)
            bucket = self._left_index.get(key)
            if bucket:
                for lrow, lw in bucket.items():
                    combined = self._combine(lrow, rrow)
                    out[combined] = out.get(combined, 0) + lw * rw
            else:
                pad = self._rightpad(rrow)
                out[pad] = out.get(pad, 0) + rw
        _integrate(self._right_index, self._rk, delta)
        self._emit(ZSet(out))

    def on_left(self, delta):  # hard side: left transitions flip right-pads
        out = {}
        keys = {tuple(lrow[i] for i in self._lk) for lrow, _lw in delta.items()}
        before_empty = {k: self._empty(self._left_index, k) for k in keys}
        for lrow, lw in delta.items():
            key = tuple(lrow[i] for i in self._lk)
            for rrow, rw in self._right_index.get(key, {}).items():
                combined = self._combine(lrow, rrow)
                out[combined] = out.get(combined, 0) + lw * rw
        _integrate(self._left_index, self._lk, delta)
        for key in keys:
            after_empty = self._empty(self._left_index, key)
            if before_empty[key] and not after_empty:
                sign = -1  # left match appeared: retract right-pads
            elif not before_empty[key] and after_empty:
                sign = +1  # left match gone: assert right-pads
            else:
                continue
            for rrow, rw in self._right_index.get(key, {}).items():
                pad = self._rightpad(rrow)
                out[pad] = out.get(pad, 0) + sign * rw
        self._emit(ZSet(out))


class FullJoinOp(Operator):
    """FULL OUTER equi-join. Preserves BOTH sides: matched rows combined,
    unmatched left rows left-padded, unmatched right rows right-padded. A key is
    never both left- and right-padded (any key with both sides present is fully
    matched). Both sides flip: a left change flips the right rows at a key, a
    right change flips the left rows."""

    _state_attrs = ("_left_index", "_right_index")

    def __init__(self, left_keys, right_keys, left_schema, right_schema):
        super().__init__()
        lidx = index_of(left_schema)
        ridx = index_of(right_schema)
        self._lk = [lidx[c] for c in left_keys]
        self._rk = [ridx[c] for c in right_keys]
        right_keyset = set(right_keys)
        self._r_nonkey = [i for i, name in enumerate(right_schema) if name not in right_keyset]
        self._left_len = len(left_schema)
        self._pad = (None,) * len(self._r_nonkey)
        self._left_index = {}
        self._right_index = {}

    def _combine(self, lrow, rrow):
        return lrow + tuple(rrow[i] for i in self._r_nonkey)

    def _leftpad(self, lrow):
        return lrow + self._pad

    def _rightpad(self, rrow):
        parts = [None] * self._left_len
        for lpos, rpos in zip(self._lk, self._rk):
            parts[lpos] = rrow[rpos]
        return tuple(parts) + tuple(rrow[i] for i in self._r_nonkey)

    @staticmethod
    def _empty(index, key):
        return not index.get(key)

    def on_left(self, delta):
        out = {}
        keys = {tuple(lrow[i] for i in self._lk) for lrow, _lw in delta.items()}
        before_empty = {k: self._empty(self._left_index, k) for k in keys}
        # inner-join change + left-pad for the delta'd left rows themselves
        for lrow, lw in delta.items():
            key = tuple(lrow[i] for i in self._lk)
            rbucket = self._right_index.get(key)
            if rbucket:
                for rrow, rw in rbucket.items():
                    combined = self._combine(lrow, rrow)
                    out[combined] = out.get(combined, 0) + lw * rw
            else:
                pad = self._leftpad(lrow)
                out[pad] = out.get(pad, 0) + lw
        _integrate(self._left_index, self._lk, delta)
        # left transitions flip the RIGHT rows at the key
        for key in keys:
            after_empty = self._empty(self._left_index, key)
            if before_empty[key] and not after_empty:
                sign = -1  # left appeared: right rows go matched -> retract right-pads
            elif not before_empty[key] and after_empty:
                sign = +1  # left gone: right rows go unmatched -> assert right-pads
            else:
                continue
            for rrow, rw in self._right_index.get(key, {}).items():
                pad = self._rightpad(rrow)
                out[pad] = out.get(pad, 0) + sign * rw
        self._emit(ZSet(out))

    def on_right(self, delta):
        out = {}
        keys = {tuple(rrow[i] for i in self._rk) for rrow, _rw in delta.items()}
        before_empty = {k: self._empty(self._right_index, k) for k in keys}
        # inner-join change + right-pad for the delta'd right rows themselves
        for rrow, rw in delta.items():
            key = tuple(rrow[i] for i in self._rk)
            lbucket = self._left_index.get(key)
            if lbucket:
                for lrow, lw in lbucket.items():
                    combined = self._combine(lrow, rrow)
                    out[combined] = out.get(combined, 0) + lw * rw
            else:
                pad = self._rightpad(rrow)
                out[pad] = out.get(pad, 0) + rw
        _integrate(self._right_index, self._rk, delta)
        # right transitions flip the LEFT rows at the key
        for key in keys:
            after_empty = self._empty(self._right_index, key)
            if before_empty[key] and not after_empty:
                sign = -1  # right appeared: left rows go matched -> retract left-pads
            elif not before_empty[key] and after_empty:
                sign = +1  # right gone: left rows go unmatched -> assert left-pads
            else:
                continue
            for lrow, lw in self._left_index.get(key, {}).items():
                pad = self._leftpad(lrow)
                out[pad] = out.get(pad, 0) + sign * lw
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


def compile_plan(node, engine, ops=None):
    """Build an operator graph for a plan. Returns (root_operator, schema).

    If `ops` is a list, every STATEFUL operator created is appended to it in a
    deterministic (post-order) sequence. Rebuilding the same plan yields the same
    sequence, so the engine can snapshot/restore per-operator state by position."""
    if isinstance(node, P.Source):
        op = engine._get_source(node.table, node.schema)
        return op, op.schema

    if isinstance(node, P.Filter):
        child, schema = compile_plan(node.input, engine, ops)
        op = FilterOp(node.predicate, schema)
        child.subscribe(op.on_input)
        return op, schema

    if isinstance(node, P.Project):
        child, schema = compile_plan(node.input, engine, ops)
        op = ProjectOp(node.outputs, schema)
        child.subscribe(op.on_input)
        out_schema = tuple(name for name, _ in node.outputs)
        return op, out_schema

    if isinstance(node, P.Distinct):
        child, schema = compile_plan(node.input, engine, ops)
        op = DistinctOp()
        child.subscribe(op.on_input)
        if ops is not None:
            ops.append(op)
        return op, schema

    if isinstance(node, P.Aggregate):
        child, schema = compile_plan(node.input, engine, ops)
        op = AggregateOp(node.group_by, node.aggregates, schema)
        child.subscribe(op.on_input)
        if ops is not None:
            ops.append(op)
        out_schema = tuple(node.group_by) + tuple(a.name for a in node.aggregates)
        return op, out_schema

    if isinstance(node, (P.Join, P.LeftJoin, P.RightJoin, P.FullJoin)):
        lop, ls = compile_plan(node.left, engine, ops)
        rop, rs = compile_plan(node.right, engine, ops)
        right_keyset = set(node.right_keys)
        r_nonkey = [i for i, name in enumerate(rs) if name not in right_keyset]
        out_schema = tuple(ls) + tuple(rs[i] for i in r_nonkey)
        if len(set(out_schema)) != len(out_schema):
            raise ValueError(f"join output has duplicate column names: {out_schema}")
        op_cls = {
            P.Join: JoinOp,
            P.LeftJoin: LeftJoinOp,
            P.RightJoin: RightJoinOp,
            P.FullJoin: FullJoinOp,
        }[type(node)]
        op = op_cls(node.left_keys, node.right_keys, ls, rs)
        lop.subscribe(op.on_left)
        rop.subscribe(op.on_right)
        if ops is not None:
            ops.append(op)
        return op, out_schema

    raise NotImplementedError(f"compiler has no rule for {type(node).__name__}")
